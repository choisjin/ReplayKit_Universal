"""Backend webcam service — OpenCV 기반 캡처/녹화.

Frontend MediaRecorder를 대체하여 WebSocket 연결 상태와 무관하게
녹화가 계속 유지되도록 한다.

- 캡처 스레드 1개가 백그라운드에서 프레임을 계속 읽음
- 최신 프레임은 _latest_frame에 저장 (미리보기 JPEG 생성용)
- 녹화 중에는 각 프레임에 타임스탬프 오버레이 후 VideoWriter에 기록
- 녹화 파일 포맷: mp4 (mp4v 코덱) — 브라우저 재생 호환
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)


_TOOLS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "tools"


def _find_ffmpeg() -> Optional[str]:
    """ffmpeg 실행 파일 경로 — capture.ffmpeg_runtime.get_ffmpeg_path() 위임.

    이전 구현은 (PATH → <repo>/tools/ffmpeg.exe) 만 탐색해 .exe 배포본의 설치 경로
    (C:\\ReplayKit\\tools\\ffmpeg.exe) 를 못 찾는 경우가 있었음. 그 결과 ffmpeg writer
    가 None 으로 떨어져 cv2.VideoWriter(mp4v) fallback → 브라우저에서 mp4v fourcc 코덱
    재생 불가. ffmpeg_runtime.get_ffmpeg_path() 는 5단계 탐색 (FFMPEG_PATH 환경변수 →
    repo/tools → CWD/tools → C:\\ReplayKit\\tools / /opt/ReplayKit/tools → PATH) 이라
    배포/dev 둘 다 안정적.
    """
    try:
        from .capture.ffmpeg_runtime import detect_ffmpeg
        return detect_ffmpeg()
    except Exception:
        # ffmpeg_runtime import 실패 시 안전한 폴백 (이전 동작)
        found = shutil.which("ffmpeg")
        if found:
            return found
        local = _TOOLS_DIR / "ffmpeg.exe"
        if local.is_file():
            return str(local)
        return None


class _FfmpegProc:
    """ffmpeg subprocess + stderr drain thread + 최근 로그 링버퍼."""
    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self._stderr_tail: list[bytes] = []
        self._stderr_lock = threading.Lock()
        self._drain_thread = threading.Thread(
            target=self._drain_stderr, daemon=True, name="ffmpeg-stderr-drain"
        )
        self._drain_thread.start()

    def _drain_stderr(self) -> None:
        if self.proc.stderr is None:
            return
        try:
            for line in iter(self.proc.stderr.readline, b""):
                if not line:
                    break
                with self._stderr_lock:
                    self._stderr_tail.append(line)
                    if len(self._stderr_tail) > 40:
                        self._stderr_tail.pop(0)
        except Exception:
            pass

    def stderr_tail(self) -> bytes:
        with self._stderr_lock:
            return b"".join(self._stderr_tail)[-600:]


def _spawn_ffmpeg_writer(output_path: Path, width: int, height: int, fps: float) -> Optional[_FfmpegProc]:
    """ffmpeg subprocess를 열어 raw BGR 프레임 → H.264 mp4 직접 인코딩.

    stderr drain thread로 파이프 블로킹을 방지한다 (장시간 녹화 시 ffmpeg가 stall되는 것을 막음).
    종료 시 stdin을 닫으면 ffmpeg가 flush 후 +faststart moov atom을 작성한다.
    """
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        logger.warning("ffmpeg not found — falling back to OpenCV mp4v writer (browser playback may fail)")
        return None
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    fps_val = fps if fps and fps > 0 else 30.0
    cmd = [
        ffmpeg, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{int(width)}x{int(height)}",
        "-pix_fmt", "bgr24",
        "-r", f"{fps_val:.3f}",
        "-i", "-",  # stdin
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-an",  # 오디오 없음
        str(output_path),
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=creationflags,
            bufsize=0,  # stdin은 unbuffered — 프레임 즉시 전송
        )
        logger.info("ffmpeg writer spawned: %s (%dx%d @%.1ffps)", output_path, width, height, fps_val)
        return _FfmpegProc(proc)
    except Exception as e:
        logger.warning("Failed to spawn ffmpeg writer: %s", e)
        return None


class WebcamService:
    """OpenCV 기반 webcam 캡처/녹화 싱글톤."""

    def __init__(self) -> None:
        self._device_index: int = 0
        self._width: int = 640
        self._height: int = 480
        self._requested_fps: float = 30.0
        self._actual_fps: float = 30.0
        self._cap: Optional[cv2.VideoCapture] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_frame_lock = threading.Lock()

        # Recording state — ffmpeg subprocess pipe (libx264 직접 인코딩)
        self._ffmpeg_proc: Optional[_FfmpegProc] = None
        # OpenCV mp4v fallback (ffmpeg 없을 때만 사용)
        self._cv_writer: Optional[cv2.VideoWriter] = None
        self._recording_path: Optional[Path] = None
        self._recording_paused = False
        self._recording_lock = threading.Lock()
        self._record_start_ts: float = 0.0
        self._frames_written: int = 0

        # Overlay config (matches frontend preferences)
        # 기본값: top-left + 24px (frontend useWebcam.ts와 동기화)
        self._overlay_position: str = "top-left"  # top-left|top-right|bottom-left|bottom-right|off
        self._overlay_color: tuple[int, int, int] = (255, 255, 255)  # BGR for cv2
        self._overlay_font_scale: float = 1.0  # 1.0 == 24px (frontend syncOverlayToBackend 기준)

    # ------------------------------------------------------------
    # Device enumeration / probe
    # ------------------------------------------------------------
    def list_devices(self, max_index: int = 5, exclude: Optional[set[int]] = None) -> list[dict]:
        """장착된 카메라 index 탐지 (간이 — DSHOW로 0..N을 순회).

        exclude: 프로브를 건너뛸 인덱스 집합. 다른 곳에서 이미 점유 중인 인덱스를
        DirectShow로 재오픈하면 기존 점유자의 캡처가 끊어질 수 있으므로 반드시 전달할 것.
        """
        exclude = exclude or set()
        found = []
        for idx in range(max_index):
            if idx in exclude:
                continue
            # 싱글톤 자체가 이 인덱스를 쓰고 있으면 재오픈 금지
            if self.is_open() and self._device_index == idx:
                found.append({
                    "index": idx,
                    "label": f"Camera {idx} ({self._width}x{self._height})",
                })
                continue
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if cap.isOpened():
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
                found.append({"index": idx, "label": f"Camera {idx} ({w}x{h})"})
                cap.release()
            else:
                cap.release()
        return found

    def probe_resolutions(self, device_index: int) -> list[str]:
        """카메라가 지원하는 대표 해상도 후보를 set/get으로 검증."""
        candidates = [
            (3840, 2160), (2560, 1440), (1920, 1080),
            (1280, 720), (960, 540), (640, 480), (320, 240),
        ]
        supported: list[str] = []
        cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            return []
        try:
            for w, h in candidates:
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                aw = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                ah = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
                if aw == w and ah == h:
                    supported.append(f"{w}x{h}")
        finally:
            cap.release()
        return supported

    # ------------------------------------------------------------
    # Capture lifecycle
    # ------------------------------------------------------------
    def is_open(self) -> bool:
        return self._cap is not None and self._cap.isOpened()

    def open(self, device_index: int = 0, width: int = 640, height: int = 480) -> bool:
        """카메라 오픈 + 캡처 스레드 시작. 이미 열려 있으면 close 후 재오픈."""
        self.close()
        cap = cv2.VideoCapture(device_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            logger.warning("Webcam open failed: device %d", device_index)
            return False
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or width
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or height
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._cap = cap
        self._device_index = device_index
        self._width = actual_w
        self._height = actual_h
        self._actual_fps = float(fps) if fps > 0 else 30.0
        self._requested_fps = self._actual_fps
        self._stop_flag.clear()
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="webcam-capture")
        self._capture_thread.start()
        logger.info("Webcam opened: device=%d %dx%d @%.1ffps", device_index, actual_w, actual_h, self._actual_fps)
        return True

    def close(self) -> None:
        """캡처 스레드 종료 + 카메라 해제 (녹화 중이면 먼저 정지)."""
        self.stop_recording()
        self._stop_flag.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=2.0)
        self._capture_thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        with self._latest_frame_lock:
            self._latest_frame = None
        logger.info("Webcam closed")

    def _capture_loop(self) -> None:
        """백그라운드 스레드: 프레임을 계속 읽어 최신본 유지 + 녹화 중이면 writer에 기록."""
        frame_interval = 1.0 / max(1.0, self._actual_fps)
        next_deadline = time.monotonic()
        while not self._stop_flag.is_set():
            cap = self._cap
            if cap is None or not cap.isOpened():
                time.sleep(0.05)
                continue
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.03)
                continue
            # 최신 프레임 저장
            with self._latest_frame_lock:
                self._latest_frame = frame
            # 녹화 중이면 오버레이 후 writer 기록
            with self._recording_lock:
                if (self._ffmpeg_proc is not None or self._cv_writer is not None) and not self._recording_paused:
                    self._write_frame_unlocked(frame)
            # 간이 frame pacing (과도한 CPU 방지)
            now = time.monotonic()
            next_deadline += frame_interval
            sleep_s = next_deadline - now
            if sleep_s > 0:
                time.sleep(sleep_s)
            else:
                next_deadline = now  # 드리프트 리셋

    # ------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------
    def get_latest_jpeg(self, quality: int = 80) -> Optional[bytes]:
        """최신 프레임을 JPEG bytes로 인코딩. 카메라 미오픈 or 프레임 없음 시 None."""
        with self._latest_frame_lock:
            frame = self._latest_frame
            if frame is None:
                return None
            frame_copy = frame.copy()
        # 프리뷰에도 오버레이 적용 (사용자가 최종 출력과 동일한 모습 확인)
        self._apply_overlay(frame_copy)
        ok, buf = cv2.imencode(".jpg", frame_copy, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return buf.tobytes()

    # ------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------
    def start_recording(self, output_path: str | Path) -> bool:
        """녹화 시작. output_path의 상위 폴더는 자동 생성. 우선 ffmpeg subprocess로 H.264 인코딩 시도, 실패 시 cv2.VideoWriter(mp4v)로 폴백."""
        with self._recording_lock:
            if self._ffmpeg_proc is not None or self._cv_writer is not None:
                logger.warning("Webcam recording already in progress")
                return False
            if not self.is_open():
                logger.warning("Webcam not open — cannot start recording")
                return False
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            # 1순위: ffmpeg subprocess (브라우저 호환 H.264)
            proc = _spawn_ffmpeg_writer(path, self._width, self._height, self._actual_fps)
            if proc is not None:
                self._ffmpeg_proc = proc
            else:
                # 폴백: cv2.VideoWriter mp4v
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(path), fourcc, self._actual_fps, (self._width, self._height))
                if not writer.isOpened():
                    logger.error("Failed to open VideoWriter: %s", path)
                    return False
                self._cv_writer = writer

            self._recording_path = path
            self._recording_paused = False
            self._record_start_ts = time.monotonic()
            self._frames_written = 0
            logger.info("Webcam recording started: %s (%dx%d @%.1ffps, mode=%s)",
                        path, self._width, self._height, self._actual_fps,
                        "ffmpeg-h264" if self._ffmpeg_proc else "cv2-mp4v")
            return True

    def stop_recording(self) -> Optional[str]:
        """녹화 정지 + 파일 경로 반환. ffmpeg를 사용 중이면 stdin close + flush 대기."""
        with self._recording_lock:
            if self._ffmpeg_proc is None and self._cv_writer is None:
                return None
            path = self._recording_path
            duration = time.monotonic() - self._record_start_ts
            frames = self._frames_written
            proc = self._ffmpeg_proc
            cv_writer = self._cv_writer
            self._ffmpeg_proc = None
            self._cv_writer = None
            self._recording_path = None
            self._recording_paused = False

        # ffmpeg flush + 종료 대기 (lock 외부) — +faststart moov atom 재작성 포함
        if proc is not None:
            sp = proc.proc
            try:
                if sp.stdin and not sp.stdin.closed:
                    try:
                        sp.stdin.flush()
                    except Exception:
                        pass
                    try:
                        sp.stdin.close()
                    except Exception:
                        pass
                # +faststart는 녹화 종료 후 moov atom을 파일 앞쪽으로 이동시키므로
                # 큰 파일은 수초가 걸릴 수 있음 → 넉넉하게 대기
                rc = sp.wait(timeout=60)
                if rc != 0:
                    logger.warning("ffmpeg writer exited with rc=%d: %s", rc,
                                   proc.stderr_tail().decode(errors="replace"))
                else:
                    logger.debug("ffmpeg writer finalized: %s", proc.stderr_tail().decode(errors="replace"))
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg writer flush timeout — killing (moov atom may be missing)")
                try:
                    sp.kill()
                    sp.wait(timeout=3)
                except Exception:
                    pass
            except Exception as e:
                logger.warning("ffmpeg writer stop error: %s", e)
        if cv_writer is not None:
            try:
                cv_writer.release()
            except Exception as e:
                logger.warning("VideoWriter release error: %s", e)

        logger.info("Webcam recording stopped: %s frames=%d duration=%.1fs", path, frames, duration)
        return str(path) if path else None

    def pause_recording(self) -> None:
        with self._recording_lock:
            if self._ffmpeg_proc is not None or self._cv_writer is not None:
                self._recording_paused = True

    def resume_recording(self) -> None:
        with self._recording_lock:
            if self._ffmpeg_proc is not None or self._cv_writer is not None:
                self._recording_paused = False

    def is_recording(self) -> bool:
        with self._recording_lock:
            return self._ffmpeg_proc is not None or self._cv_writer is not None

    def _write_frame_unlocked(self, frame: np.ndarray) -> None:
        """녹화 writer에 프레임 기록 (lock 내에서 호출). 오버레이 포함."""
        if self._ffmpeg_proc is None and self._cv_writer is None:
            return
        # ffmpeg에 전달할 프레임은 ffmpeg cmd에 지정된 `-s WxH`와 정확히 일치해야 함.
        # 카메라가 요청과 다른 해상도를 반환하면 resize로 맞춤 (잘못된 크기는 ffmpeg를 즉시 죽임).
        display = frame
        if display.ndim != 3 or display.shape[2] != 3:
            # BGRA/grayscale 등 → BGR로 변환
            if display.ndim == 2:
                display = cv2.cvtColor(display, cv2.COLOR_GRAY2BGR)
            elif display.shape[2] == 4:
                display = cv2.cvtColor(display, cv2.COLOR_BGRA2BGR)
        if display.shape[0] != self._height or display.shape[1] != self._width:
            display = cv2.resize(display, (self._width, self._height))
        display = display.copy()  # contiguous 보장 + overlay가 원본을 건드리지 않게
        self._apply_overlay(display)
        # ffmpeg subprocess pipe 우선
        if self._ffmpeg_proc is not None:
            sp = self._ffmpeg_proc.proc
            if sp.stdin is not None:
                try:
                    sp.stdin.write(display.tobytes())
                    self._frames_written += 1
                except (BrokenPipeError, OSError) as e:
                    logger.warning("ffmpeg pipe write failed: %s — recording aborted (stderr: %s)",
                                   e, self._ffmpeg_proc.stderr_tail().decode(errors="replace"))
                    try:
                        sp.kill()
                    except Exception:
                        pass
                    self._ffmpeg_proc = None
                except Exception as e:
                    logger.warning("ffmpeg write error: %s", e)
        elif self._cv_writer is not None:
            try:
                self._cv_writer.write(display)
                self._frames_written += 1
            except Exception as e:
                logger.warning("VideoWriter write error: %s", e)

    # ------------------------------------------------------------
    # Overlay
    # ------------------------------------------------------------
    def set_overlay(self, position: Optional[str] = None,
                    color_hex: Optional[str] = None,
                    font_scale: Optional[float] = None) -> None:
        if position is not None:
            self._overlay_position = position
        if color_hex is not None:
            self._overlay_color = self._hex_to_bgr(color_hex)
        if font_scale is not None:
            self._overlay_font_scale = float(font_scale)

    @staticmethod
    def _hex_to_bgr(color_hex: str) -> tuple[int, int, int]:
        s = color_hex.lstrip("#")
        if len(s) == 3:
            s = "".join(c * 2 for c in s)
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            return (b, g, r)  # cv2 uses BGR
        except Exception:
            return (255, 255, 255)

    def _apply_overlay(self, frame: np.ndarray) -> None:
        """frame에 타임스탬프 오버레이 in-place."""
        pos = self._overlay_position
        if pos == "off":
            return
        h, w = frame.shape[:2]
        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        font = cv2.FONT_HERSHEY_SIMPLEX
        # 폰트 스케일 auto: 높이의 ~3%를 목표
        auto_scale = max(0.4, h * 0.0014)
        scale = self._overlay_font_scale if self._overlay_font_scale > 0 else auto_scale
        thickness = max(1, int(scale * 2))
        (text_w, text_h), baseline = cv2.getTextSize(ts, font, scale, thickness)
        pad = 4
        margin = 6
        box_w = text_w + pad * 2
        box_h = text_h + pad * 2

        if pos == "top-right":
            bx, by = w - box_w - margin, margin
            tx, ty = bx + pad, by + pad + text_h
        elif pos == "bottom-left":
            bx, by = margin, h - box_h - margin
            tx, ty = bx + pad, by + pad + text_h
        elif pos == "bottom-right":
            bx, by = w - box_w - margin, h - box_h - margin
            tx, ty = bx + pad, by + pad + text_h
        else:  # top-left (default)
            bx, by = margin, margin
            tx, ty = bx + pad, by + pad + text_h

        # 반투명 박스
        overlay = frame.copy()
        cv2.rectangle(overlay, (bx, by), (bx + box_w, by + box_h), (0, 0, 0), thickness=-1)
        cv2.addWeighted(overlay, 0.5, frame, 0.5, 0, frame)
        cv2.putText(frame, ts, (tx, ty), font, scale, self._overlay_color, thickness, cv2.LINE_AA)

    # ------------------------------------------------------------
    # Exposure (Level 2 — 노출만 노출)
    # ------------------------------------------------------------
    def get_exposure(self) -> dict:
        """현재 노출값/모드 + 카메라 지원 범위 반환.

        OpenCV의 CAP_PROP_EXPOSURE 의미:
        - DSHOW 백엔드 기준: 음수 값 (예: -13 ~ -1, log2 1/sec)
        - CAP_PROP_AUTO_EXPOSURE: 0.25 = manual, 0.75 = auto (DSHOW)
        - 카메라마다 다르므로 min/max는 set/get 시 cap.get으로 추정
        """
        if not self.is_open() or self._cap is None:
            return {"supported": False}
        try:
            value = self._cap.get(cv2.CAP_PROP_EXPOSURE)
            auto = self._cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            return {
                "supported": True,
                "value": float(value),
                "auto": auto >= 0.5,  # 0.75 = auto, 0.25 = manual
                "min": -13.0,  # DSHOW 일반 범위
                "max": 0.0,
                "step": 1.0,
            }
        except Exception as e:
            logger.warning("get_exposure failed: %s", e)
            return {"supported": False}

    def set_exposure(self, value: Optional[float] = None, auto: Optional[bool] = None) -> bool:
        """노출값 설정. value를 주면 manual 모드로 전환 후 적용. auto=True면 자동 모드."""
        if not self.is_open() or self._cap is None:
            return False
        try:
            if auto is True:
                # 자동 노출 모드 (DSHOW: 0.75)
                self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
                logger.info("Webcam exposure: AUTO")
                return True
            if value is not None:
                # 수동 모드 + 값 설정
                self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
                self._cap.set(cv2.CAP_PROP_EXPOSURE, float(value))
                actual = self._cap.get(cv2.CAP_PROP_EXPOSURE)
                logger.info("Webcam exposure: MANUAL value=%.2f (actual=%.2f)", value, actual)
                return True
        except Exception as e:
            logger.warning("set_exposure failed: %s", e)
        return False

    # ------------------------------------------------------------
    # Status
    # ------------------------------------------------------------
    def status(self) -> dict:
        with self._recording_lock:
            recording = self._ffmpeg_proc is not None or self._cv_writer is not None
            mode = "ffmpeg-h264" if self._ffmpeg_proc is not None else ("cv2-mp4v" if self._cv_writer is not None else "")
            rec_path = str(self._recording_path) if self._recording_path else ""
            duration = time.monotonic() - self._record_start_ts if recording else 0.0
            frames = self._frames_written
        return {
            "open": self.is_open(),
            "device_index": self._device_index,
            "width": self._width,
            "height": self._height,
            "fps": self._actual_fps,
            "recording": recording,
            "recording_mode": mode,
            "recording_path": rec_path,
            "recording_duration_s": duration,
            "frames_written": frames,
            "overlay_position": self._overlay_position,
        }


# Singleton
_webcam_service: Optional[WebcamService] = None


def get_webcam_service() -> WebcamService:
    global _webcam_service
    if _webcam_service is None:
        _webcam_service = WebcamService()
    return _webcam_service
