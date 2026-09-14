# -*- coding: utf-8 -*-
"""Webcam — 웹캠 영상 캡처 모듈 (판정 없음, 설정한 시간만큼 영상 저장).

주 디바이스로 웹캠(type="webcam")을 연결하면 가상 모듈처럼 자동으로 붙는다
(module_service.PRIMARY_VIRTUAL_MODULES, 프론트 RecordPage moduleDevices). 스텝의 device_id 가
그 주 디바이스이고, 재생 엔진이 생성자 인자 device_index/device_id 를 채워 준다.

Capture(time) 스텝이 웹캠 영상을 time 초 동안 **카메라 fps 고정(CFR)** mp4(H.264)로 저장한다.
  - 재생 중:     {run_dir}/Capture/c{사이클}/step{스텝}_{시각}.mp4
  - 스텝 테스트: results/Temp_logs/Capture/step{스텝}_{시각}.mp4 (최근 N개만 유지)
결과 문자열 마지막 줄 `video=<절대경로>` 를 재생 엔진이 읽어 StepResult.capture_video 에
results 기준 상대경로로 기록하고, 결과창이 영상 플레이어/프레임 보기로 표시한다.

카메라 점유 규칙 (DirectShow 는 같은 카메라 이중 오픈을 거부):
  카메라는 주 디바이스 WebcamDevice 가 소유한다 — 이 모듈은 절대 직접 열지 않고 그 캡처 스레드의
  최신 프레임을 공유받는다. (device_id 없이 만들어진 경우에만 번호로 소유자를 찾고, 소유자가
  없으면 직접 연다 — 레거시/수동 호출용 폴백)

CFR 타임라인: 슬롯 k(시각 t0 + k/fps)에는 그 시각(±반 프레임)까지 도착한 가장 최근 프레임을
배치한다. 카메라가 실제로 fps 보다 느리게 주면 직전 프레임이 반복되고, 빠르면 남는 프레임은
버린다 — 어느 쪽이든 영상 길이 = 설정 시간, 프레임 수 = time × fps 로 정확히 맞는다.
"""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import sys
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import cv2
import numpy as np

logger = logging.getLogger(__name__)

if sys.platform == "win32":
    _CV_CAM_BACKENDS: tuple[int, ...] = (cv2.CAP_DSHOW,)
elif sys.platform.startswith("linux"):
    _CV_CAM_BACKENDS = (cv2.CAP_V4L2, cv2.CAP_ANY)
else:
    _CV_CAM_BACKENDS = (cv2.CAP_ANY,)

_DEFAULT_FPS = 30.0
# 스텝 테스트 캡처(Temp_logs/Capture) 보존 개수 — 초과분은 오래된 것부터 삭제
_TEMP_KEEP = 30
# 시작 후 첫 프레임 대기 한도
_FIRST_FRAME_TIMEOUT_S = 3.0


def _results_dir() -> Path:
    try:
        from backend.app.services.playback_service import RESULTS_DIR
        return Path(RESULTS_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "results"


def _run_output_dir() -> Optional[Path]:
    try:
        from backend.app.services.playback_service import get_run_output_dir
        return get_run_output_dir()
    except Exception:
        return None


def _step_context() -> tuple[Optional[int], int]:
    try:
        from backend.app.services.playback_service import get_current_step_context
        return get_current_step_context()
    except Exception:
        return None, 1


def _playback_stop_requested() -> bool:
    """재생 중지 요청 여부 — 긴 캡처 도중 사용자가 중지하면 거기서 끊고 저장한다."""
    try:
        from backend.app.dependencies import playback_service
        return bool(getattr(playback_service, "_should_stop", False))
    except Exception:
        return False


def _find_ffmpeg() -> Optional[str]:
    try:
        from backend.app.services.capture.ffmpeg_runtime import detect_ffmpeg
        return detect_ffmpeg()
    except Exception:
        return shutil.which("ffmpeg")


def _sane_fps(value) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if 1.0 <= f <= 240.0 else None


def _device_peek(cam) -> tuple[Callable, Optional[float]]:
    """WebcamDevice 캡처 스레드의 최신 프레임을 복사 없이 보는 peek + 카메라 fps."""
    def _peek():
        with cam._frame_lock:
            return cam._latest_frame, cam._latest_ts

    fps = None
    try:
        fps = _sane_fps(cam._cap.get(cv2.CAP_PROP_FPS)) if cam._cap is not None else None
    except Exception:
        pass
    return _peek, fps


class Webcam:
    """웹캠 영상 캡처 모듈 (주 디바이스 웹캠에 자동으로 붙는 가상 모듈)."""

    def __init__(self, device_index: str = "0", device_id: str = ""):
        try:
            self._camera_index = int(str(device_index).strip() or 0)
        except (TypeError, ValueError):
            self._camera_index = 0
        # 바인딩된 주 디바이스 id — 있으면 그 디바이스의 카메라만 쓴다 (직접 오픈 금지)
        self._device_id = str(device_id or "").strip()

        # 직접 오픈 모드 상태 (device_id 없는 레거시 호출 폴백 전용)
        self._cap: Optional[cv2.VideoCapture] = None
        self._cap_fps: Optional[float] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._frame: Optional[np.ndarray] = None
        self._frame_ts = 0.0

        self._capture_lock = threading.Lock()  # Capture 동시 실행 직렬화

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """카메라 확보 — 소유자(주 디바이스/PIP) 프레임 공유. 소유자 없음 + 바인딩 없음이면 직접 연다."""
        owner = self._resolve_owner()
        if owner is not None:
            return f"Connected: camera {self._camera_index} (shared with {owner[2]})"
        if self._device_id:
            raise RuntimeError(f"Webcam device '{self._device_id}' is not connected")
        self._open_own()
        return f"Connected: camera {self._camera_index}"

    def Disconnect(self) -> str:
        self._close_own()
        return "Disconnected"

    def IsConnected(self) -> bool:
        if self._cap is not None:
            try:
                return bool(self._cap.isOpened())
            except Exception:
                return False
        return self._resolve_owner() is not None

    # ------------------------------------------------------------------
    # Step function
    # ------------------------------------------------------------------

    def Capture(self, time: float = 5) -> str:
        """time 초 동안 웹캠 영상을 카메라 fps 로 저장한다 (판정 없음)."""
        try:
            duration = float(time)
        except (TypeError, ValueError):
            raise ValueError(f"time must be a number of seconds, got {time!r}")
        if duration <= 0:
            raise ValueError("time must be > 0")

        with self._capture_lock:
            peek, fps, source = self._acquire_source()
            out_path = self._output_path()
            return self._record(peek, fps, source, duration, out_path)

    # ------------------------------------------------------------------
    # Frame source
    # ------------------------------------------------------------------

    def _resolve_owner(self) -> Optional[tuple[Callable, Optional[float], str]]:
        """카메라를 이미 열고 있는 소유자 → (peek, fps, 설명). 없으면 None.

        peek() 은 복사 없이 (frame 객체, 도착시각 monotonic) 를 돌려준다 — 새 프레임 판별은
        객체 identity 로 한다(소유자 캡처 스레드가 매 read 마다 새 배열로 교체).
        """
        if self._cap is not None:
            return None  # 우리가 직접 열었으면 다른 소유자는 있을 수 없다
        try:
            from backend.app.dependencies import device_manager as dm
        except Exception as e:
            logger.debug("Webcam module: device_manager import failed: %s", e)
            dm = None

        # 1) 바인딩된 주 디바이스 (자동 연결 경로 — 이것만 본다)
        if self._device_id:
            if dm is None:
                return None
            dev = dm.get_device(self._device_id)
            cam = dm.get_webcam_device(self._device_id) if dev is not None else None
            if dev is None or dev.status != "connected" or cam is None or not getattr(cam, "_is_connected", False):
                return None
            peek, fps = _device_peek(cam)
            return peek, fps, f"primary:{self._device_id}"

        # 2) 번호가 같은 주 디바이스 WebcamDevice (레거시 폴백)
        if dm is not None:
            try:
                for d in dm.list_primary():
                    if d.type != "webcam" or d.status != "connected":
                        continue
                    try:
                        idx = int((d.info or {}).get("device_index", -1))
                    except (TypeError, ValueError):
                        continue
                    cam = dm.get_webcam_device(d.id)
                    if idx != self._camera_index or cam is None or not getattr(cam, "_is_connected", False):
                        continue
                    peek, fps = _device_peek(cam)
                    return peek, fps, f"primary:{d.id}"
            except Exception as e:
                logger.debug("Webcam module: primary owner probe failed: %s", e)
        # 3) PIP 녹화 싱글톤
        try:
            from backend.app.services.webcam_service import get_webcam_service
            svc = get_webcam_service()
            if svc.is_open() and getattr(svc, "_device_index", None) == self._camera_index:
                def _peek_pip(svc=svc):
                    # PIP 는 도착시각을 기록하지 않아 조회 시각으로 대신한다
                    with svc._latest_frame_lock:
                        return svc._latest_frame, _time.monotonic()

                return _peek_pip, _sane_fps(getattr(svc, "_actual_fps", None)), "pip"
        except Exception as e:
            logger.debug("Webcam module: pip owner probe failed: %s", e)
        return None

    def _acquire_source(self) -> tuple[Callable, float, str]:
        owner = self._resolve_owner()
        if owner is not None:
            peek, fps, desc = owner
            return peek, fps or _DEFAULT_FPS, desc
        if self._device_id:
            raise RuntimeError(
                f"Webcam device '{self._device_id}' is not connected — 디바이스 페이지에서 웹캠 연결 확인")
        if self._cap is None:
            self._open_own()

        def _peek_own():
            with self._cond:
                return self._frame, self._frame_ts

        return _peek_own, self._cap_fps or _DEFAULT_FPS, f"camera {self._camera_index}"

    def _open_own(self) -> None:
        if self._cap is not None:
            return
        cap = None
        for backend in _CV_CAM_BACKENDS:
            try:
                c = cv2.VideoCapture(self._camera_index, backend)
            except Exception:
                continue
            if c.isOpened():
                cap = c
                break
            try:
                c.release()
            except Exception:
                pass
        if cap is None:
            raise RuntimeError(
                f"Webcam open failed: camera {self._camera_index} — 연결 안 됨 또는 다른 프로그램이 사용 중")
        # MJPG 우선 — YUY2 무압축은 USB 대역폭을 독점해 다른 웹캠 공존이 불가 (WebcamDevice 와 동일)
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
        self._cap_fps = _sane_fps(cap.get(cv2.CAP_PROP_FPS))
        for _ in range(3):  # 웜업 — DSHOW 첫 프레임 깨짐 대비
            ok, _f = cap.read()
            if ok:
                break
        self._cap = cap
        self._stop.clear()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True,
                                        name=f"WebcamModule-{self._camera_index}")
        self._thread.start()
        logger.info("Webcam module opened camera %d: %dx%d @%s fps", self._camera_index,
                    int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
                    self._cap_fps)

    def _close_own(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None
        if self._cap is not None:
            try:
                self._cap.release()
            except Exception:
                pass
            self._cap = None
        with self._cond:
            self._frame = None
            self._frame_ts = 0.0

    def _capture_loop(self) -> None:
        fails = 0
        while not self._stop.is_set():
            cap = self._cap
            if cap is None:
                break
            try:
                ok, frame = cap.read()
            except Exception:
                ok, frame = False, None
            if ok and frame is not None:
                fails = 0
                with self._cond:
                    self._frame = frame
                    self._frame_ts = _time.monotonic()
                    self._cond.notify_all()
            else:
                fails += 1
                _time.sleep(0.1 if fails < 30 else 0.5)

    def _wait_new(self, peek: Callable, last_obj, timeout: float):
        """last_obj 와 다른 새 프레임을 timeout 까지 기다린다 → (frame, ts) 또는 (None, 0)."""
        deadline = _time.monotonic() + timeout
        own = self._cap is not None
        while True:
            frame, ts = peek()
            if frame is not None and frame is not last_obj:
                return frame, ts
            remain = deadline - _time.monotonic()
            if remain <= 0:
                return None, 0.0
            if own:
                with self._cond:
                    self._cond.wait(min(remain, 0.05))
            else:
                _time.sleep(min(remain, 0.003))

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def _output_path(self) -> Path:
        step_id, cycle = _step_context()
        run_dir = _run_output_dir()
        if run_dir:
            out_dir = run_dir / "Capture" / f"c{int(cycle or 1)}"
        else:
            out_dir = _results_dir() / "Temp_logs" / "Capture"
        out_dir.mkdir(parents=True, exist_ok=True)
        if not run_dir:
            self._prune_temp(out_dir)
        step_part = f"step{int(step_id):03d}" if step_id is not None else "step"
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        return out_dir / f"{step_part}_{ts}.mp4"

    @staticmethod
    def _prune_temp(out_dir: Path) -> None:
        try:
            videos = sorted(out_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
            for old in videos[:max(0, len(videos) - (_TEMP_KEEP - 1))]:
                old.unlink(missing_ok=True)
                shutil.rmtree(old.with_name(old.stem + "_frames"), ignore_errors=True)
        except Exception as e:
            logger.debug("Webcam temp prune failed: %s", e)

    def _record(self, peek: Callable, fps: float, source: str, duration: float, out_path: Path) -> str:
        from backend.app.services import playback_service as _pb

        first, _ts = self._wait_new(peek, None, _FIRST_FRAME_TIMEOUT_S)
        if first is None:
            return f"FAIL: Webcam capture — no frame from {source} within {_FIRST_FRAME_TIMEOUT_S:.0f}s"
        h, w = first.shape[:2]
        w2, h2 = w - (w % 2), h - (h % 2)  # yuv420p 는 짝수 해상도만 허용
        total = max(1, int(round(duration * fps)))
        period = 1.0 / fps

        writer = _FrameWriter(out_path, w2, h2, fps, queue_max=max(30, int(fps * 5)))
        if not writer.start():
            return f"FAIL: Webcam capture — video writer start failed ({writer.error})"

        # Windows 기본 타이머 해상도(15.6ms)로는 30fps 폴링이 뭉개지므로 캡처 동안 1ms 로 상향
        _pb._set_timer_resolution(True)
        written = 0
        unique = 1
        try:
            prev = first
            t0 = _time.monotonic()
            while written < total:
                if _playback_stop_requested():
                    break
                end_at = t0 + total * period
                frame, ts = self._wait_new(peek, prev, max(0.0, min(end_at - _time.monotonic(), 0.5)))
                if frame is None:
                    if _time.monotonic() >= end_at:
                        break
                    continue
                # 새 프레임 도착 시각 이전(반 프레임 여유)에 시작된 슬롯은 직전 프레임으로 채운다
                while written < total and t0 + (written + 0.5) * period <= ts:
                    writer.put(prev[:h2, :w2])
                    written += 1
                prev = frame
                unique += 1
            while written < total:  # 남은 슬롯(카메라 정체/종료 경계)은 마지막 프레임으로
                writer.put(prev[:h2, :w2])
                written += 1
        finally:
            _pb._set_timer_resolution(False)
            ok = writer.finish()

        if not ok or not out_path.exists() or out_path.stat().st_size == 0:
            return f"FAIL: Webcam capture — encoding failed ({writer.error or 'empty file'})"
        note = "" if writer.mode == "ffmpeg" else " (ffmpeg 없음 — mp4v, 브라우저 재생 불가할 수 있음)"
        real_fps = unique / duration
        logger.info("Webcam capture saved: %s (%.1fs @ %.2ffps, %d frames, unique %d, %dx%d, src=%s)",
                    out_path, duration, fps, written, unique, w2, h2, source)
        return (
            f"Webcam capture saved — {duration:g}s @ {fps:g}fps, {written} frames "
            f"(camera delivered ~{real_fps:.1f}fps), {w2}x{h2}, source={source}{note}\n"
            f"video={out_path}"
        )


class _FrameWriter:
    """프레임 큐 → 인코더(ffmpeg H.264 우선, 없으면 cv2 mp4v). 캡처 루프가 인코딩에 막히지 않게 분리."""

    def __init__(self, path: Path, width: int, height: int, fps: float, queue_max: int):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.mode = ""
        self.error = ""
        self._q: queue.Queue = queue.Queue(maxsize=queue_max)
        self._proc: Optional[subprocess.Popen] = None
        self._cv: Optional[cv2.VideoWriter] = None
        self._thread: Optional[threading.Thread] = None
        self._stderr_tail = b""

    def start(self) -> bool:
        ffmpeg = _find_ffmpeg()
        if ffmpeg:
            cmd = [
                ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", f"{self.width}x{self.height}",
                "-framerate", f"{self.fps:g}",
                "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p",
                "-r", f"{self.fps:g}",
                "-movflags", "+faststart",
                "-an",
                str(self.path),
            ]
            try:
                self._proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                threading.Thread(target=self._drain_stderr, daemon=True).start()
                self.mode = "ffmpeg"
            except Exception as e:
                logger.warning("Webcam capture: ffmpeg spawn failed (%s) — cv2 fallback", e)
                self._proc = None
        if self._proc is None:
            vw = cv2.VideoWriter(str(self.path), cv2.VideoWriter_fourcc(*"mp4v"),
                                 self.fps, (self.width, self.height))
            if not vw.isOpened():
                self.error = "cv2.VideoWriter open failed"
                return False
            self._cv = vw
            self.mode = "cv2"
        self._thread = threading.Thread(target=self._run, daemon=True, name="WebcamCaptureWriter")
        self._thread.start()
        return True

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in proc.stderr:
            self._stderr_tail = (self._stderr_tail + line)[-2000:]

    def put(self, frame: np.ndarray) -> None:
        # 인코더가 멈춰 큐가 가득 차면 캡처 쪽도 잠시 기다린다 (메모리 무한 증가 방지)
        self._q.put(np.ascontiguousarray(frame))

    def _run(self) -> None:
        while True:
            frame = self._q.get()
            if frame is None:
                break
            if self.error:
                continue  # 실패 후엔 큐만 비워 put 이 막히지 않게
            try:
                if self._proc is not None:
                    self._proc.stdin.write(frame.tobytes())
                elif self._cv is not None:
                    self._cv.write(frame)
            except Exception as e:
                self.error = f"write failed: {e} {self._stderr_tail.decode(errors='replace')[-300:]}"

    def finish(self) -> bool:
        self._q.put(None)
        if self._thread:
            self._thread.join(timeout=60)
        if self._proc is not None:
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                rc = self._proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self.error = self.error or "ffmpeg finalize timeout"
                return False
            if rc != 0:
                self.error = self.error or f"ffmpeg rc={rc}: {self._stderr_tail.decode(errors='replace')[-300:]}"
                return False
        if self._cv is not None:
            self._cv.release()
        return not self.error
