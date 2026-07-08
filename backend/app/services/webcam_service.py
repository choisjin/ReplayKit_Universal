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
import re
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


# dshow 장치 열거 결과 캐시 — 열거는 ffmpeg subprocess 실행(수백 ms)이라
# 녹화 시작(auto 모드) 때마다 돌리면 시작이 지연된다. TTL 내에서는 캐시 재사용.
_DSHOW_DEV_CACHE: dict = {"ts": 0.0, "video": [], "audio": []}
_DSHOW_DEV_CACHE_TTL = 30.0
_dshow_dev_cache_lock = threading.Lock()


def _list_dshow_devices(force: bool = False) -> dict:
    """Windows DirectShow 장치 열거 — {"video": [...], "audio": [...]}.

    `ffmpeg -list_devices true -f dshow -i dummy` 의 stderr 를 파싱한다.
    각 항목: {"name": 표시명, "alt": "@device_..." (없으면 "")}
    - alt(alternative name)는 ASCII 고정 식별자 — 한글 표시명의 인코딩 왕복 문제를
      피하기 위해 캡처 시에는 alt 를 우선 사용한다.
    - 신형("Name" (audio)) / 구형(DirectShow audio devices 섹션) 출력 형식 모두 지원.
    - video 목록은 dshow 비디오 카테고리 열거 순서 그대로 — OpenCV CAP_DSHOW 인덱스와
      같은 열거자를 쓰므로 순서가 일치한다(웹캠 인덱스 → 장치명 매핑에 사용).
    비 Windows / ffmpeg 부재 시 빈 목록들.
    """
    empty = {"video": [], "audio": []}
    if sys.platform != "win32":
        return empty
    now = time.monotonic()
    with _dshow_dev_cache_lock:
        if not force and now - _DSHOW_DEV_CACHE["ts"] < _DSHOW_DEV_CACHE_TTL:
            return {"video": list(_DSHOW_DEV_CACHE["video"]), "audio": list(_DSHOW_DEV_CACHE["audio"])}
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        return empty
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        text = proc.stderr.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("dshow device enumeration failed: %s", e)
        return empty
    video: list[dict] = []
    audio: list[dict] = []
    section = ""
    pending: Optional[dict] = None  # 직후의 Alternative name 라인을 붙일 대상
    for raw in text.splitlines():
        # "[dshow @ 0x...] 내용" → 내용만
        line = raw.split("]", 1)[-1].strip() if raw.lstrip().startswith("[") else raw.strip()
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        alt_m = re.search(r'Alternative name\s+"(.+)"', line)
        if alt_m:
            if pending is not None:
                pending["alt"] = alt_m.group(1)
            continue
        name_m = re.match(r'^"(.+?)"\s*(\([^)]*\))?$', line)
        if not name_m:
            continue
        name, kinds = name_m.group(1), name_m.group(2) or ""
        pending = {"name": name, "alt": ""}
        if kinds:
            # 신형: 타입 태그. "(none)"(가상캠 등)도 비디오 카테고리 열거 항목이므로
            # 비-오디오는 전부 video 목록에 넣어 OpenCV 인덱스와의 순서 정합을 지킨다.
            if "audio" in kinds:
                audio.append(pending)
            if "audio" not in kinds or "video" in kinds:
                video.append(pending)
        elif section == "audio":
            audio.append(pending)
        elif section == "video":
            video.append(pending)
    with _dshow_dev_cache_lock:
        _DSHOW_DEV_CACHE["ts"] = time.monotonic()
        _DSHOW_DEV_CACHE["video"] = list(video)
        _DSHOW_DEV_CACHE["audio"] = list(audio)
    logger.debug("dshow devices: video=%s audio=%s",
                 [d["name"] for d in video], [d["name"] for d in audio])
    return {"video": video, "audio": audio}


def list_audio_input_devices(force: bool = False) -> list[dict]:
    """Windows DirectShow 오디오 입력(마이크) 장치 열거. _list_dshow_devices 참조."""
    return _list_dshow_devices(force=force)["audio"]


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


def _probe_audio_device(device: str) -> bool:
    """dshow 오디오 장치가 실제로 열리는지 짧게(0.2s) 캡처해 확인.

    녹화 writer 는 stdin 프레임이 들어오기 전까지 dshow 입력을 열지 않으므로
    (입력 순차 오픈), 잘못된 장치는 녹화가 시작된 '뒤에' ffmpeg 를 죽여
    BrokenPipe 로 그 회차 녹화 전체를 날린다. 스폰 전에 여기서 미리 걸러낸다.
    """
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        return False
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    try:
        proc = subprocess.run(
            [ffmpeg, "-hide_banner",
             "-f", "dshow", "-audio_buffer_size", "80", "-i", f"audio={device}",
             "-t", "0.2", "-f", "null", "-"],
            capture_output=True, timeout=10, creationflags=creationflags,
        )
        if proc.returncode != 0:
            logger.warning("Audio device probe failed for '%s': %s", device,
                           proc.stderr.decode(errors="replace")[-400:])
            return False
        return True
    except Exception as e:
        logger.warning("Audio device probe error for '%s': %s", device, e)
        return False


def _spawn_ffmpeg_writer(
    output_path: Path,
    width: int,
    height: int,
    fps: float,
    audio_device: Optional[str] = None,
) -> Optional[_FfmpegProc]:
    """ffmpeg subprocess를 열어 raw BGR 프레임 → H.264 mp4 직접 인코딩.

    stderr drain thread로 파이프 블로킹을 방지한다 (장시간 녹화 시 ffmpeg가 stall되는 것을 막음).
    종료 시 stdin을 닫으면 ffmpeg가 flush 후 +faststart moov atom을 작성한다.

    audio_device: Windows dshow 오디오 장치 식별자(표시명 또는 @device_ alternative name).
    주어지면 마이크 입력을 두 번째 입력으로 붙여 AAC 로 함께 먹싱한다. 양쪽 입력 모두
    -use_wallclock_as_timestamps 1 로 같은 벽시계 기준 PTS 를 쓰므로 A/V 싱크가 맞는다.
    -shortest: 영상(stdin) EOF 후 라이브 오디오 입력이 ffmpeg 를 붙잡고 있지 않게 종료 트리거.
    장치 유효성은 호출 전에 _probe_audio_device 로 확인할 것 — ffmpeg 는 입력을 순차로
    열기 때문에 여기서 스폰 직후 생존 확인을 해도 dshow 오픈 실패를 감지할 수 없다.
    """
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        logger.warning("ffmpeg not found — falling back to OpenCV mp4v writer (browser playback may fail)")
        return None
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    # VFR(가변 프레임레이트) + 벽시계 타임스탬프로 인코딩:
    #  - 입력에 고정 -r 을 주지 않고 -use_wallclock_as_timestamps 1 로 "프레임이 파이프에
    #    도착한 실제 시각"을 PTS 로 사용하고, 출력 -vsync vfr 로 그 타이밍을 그대로 보존한다.
    #  → 카메라가 30fps 라고 보고하지만 실제로 14fps 만 들어오던 경우의 '2배속/끊김' 문제가
    #    사라지고, 영상 길이가 실제 녹화(시나리오) 시간과 1:1 로 일치한다. 프레임 스킵/점프가
    #    생겨도 그 간격이 영상에 그대로 반영된다(최대 fps 로 캡처하되 레이트를 강제하지 않음).
    cmd = [
        ffmpeg, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-s", f"{int(width)}x{int(height)}",
        "-pix_fmt", "bgr24",
        "-use_wallclock_as_timestamps", "1",
        "-i", "-",  # stdin
    ]
    if audio_device:
        cmd += [
            "-f", "dshow",
            "-use_wallclock_as_timestamps", "1",
            "-thread_queue_size", "1024",
            "-audio_buffer_size", "80",  # ms — 기본(500ms)은 A/V 오프셋이 커짐
            "-i", f"audio={audio_device}",
        ]
    cmd += [
        "-map", "0:v",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-vsync", "vfr",
    ]
    if audio_device:
        cmd += [
            "-map", "1:a",
            "-c:a", "aac",
            "-b:a", "128k",
            "-shortest",
        ]
    else:
        cmd += ["-an"]
    cmd += [
        "-movflags", "+faststart",
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
        logger.info("ffmpeg writer spawned: %s (%dx%d, VFR/wallclock, audio=%s)",
                    output_path, width, height, audio_device or "off")
        return _FfmpegProc(proc)
    except Exception as e:
        logger.warning("Failed to spawn ffmpeg writer: %s", e)
        return None


# OS 별 OpenCV VideoCapture 백엔드 후보 — 앞에서부터 순차 시도.
#   Windows: DirectShow (CAP_DSHOW) — USB 카메라 매끄럽게 열거/오픈.
#   Linux:   V4L2 (CAP_V4L2) → CAP_ANY 폴백.
#            opencv-python 일부 빌드/배포 환경에서 CAP_V4L2 가 거부되거나, 노트북 처럼
#            카메라 1개당 /dev/videoN 노드가 여러 개(capture/metadata/IR) 인 경우
#            특정 index 에서 V4L2 strict 가 isOpened()=False 로 떨어진다. CAP_ANY 가
#            autodetect 로 V4L/V4L2/FFmpeg 중 하나를 골라 잡아주는 것을 폴백으로 둠.
#   macOS:   AVFoundation/CAP_ANY (자동).
# 잘못된 backend 만 쓰면 list_devices() 가 빈 배열 반환 — "USB 웹캠을 못 찾음" 증상.
if sys.platform == "win32":
    _CV_CAM_BACKENDS: tuple[int, ...] = (cv2.CAP_DSHOW,)
elif sys.platform.startswith("linux"):
    _CV_CAM_BACKENDS = (cv2.CAP_V4L2, cv2.CAP_ANY)
else:
    _CV_CAM_BACKENDS = (cv2.CAP_ANY,)

# 단일 backend 가 필요한 위치 (기록 호환용) — 첫 번째 후보.
_CV_CAM_BACKEND = _CV_CAM_BACKENDS[0]


def _open_capture(index: int) -> Optional[cv2.VideoCapture]:
    """후보 backend 들을 순회하며 첫 번째 isOpened()=True 캡처를 반환.

    각 시도의 실패 사유를 debug 로그로 남겨, 빈 list_devices() 원인 진단 가능.
    """
    last_backend = None
    for backend in _CV_CAM_BACKENDS:
        last_backend = backend
        try:
            cap = cv2.VideoCapture(index, backend)
        except Exception as e:
            logger.debug("VideoCapture(%d, backend=%d) raised: %s", index, backend, e)
            continue
        if cap.isOpened():
            if backend != _CV_CAM_BACKENDS[0]:
                logger.info("Webcam index %d opened via fallback backend %d", index, backend)
            return cap
        logger.debug("VideoCapture(%d, backend=%d) isOpened=False", index, backend)
        try:
            cap.release()
        except Exception:
            pass
    logger.debug("Webcam index %d failed on all backends (last=%s)", index, last_backend)
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

        # Audio (마이크) 녹음 설정 — Windows dshow 한정. ffmpeg writer에 두 번째 입력으로 붙는다.
        self._audio_enabled: bool = True
        self._audio_device: str = ""  # "" = auto (첫 번째 dshow 오디오 장치)
        self._audio_validated: set[str] = set()  # 이 세션에서 오픈 프로브를 통과한 장치
        self._audio_failed: set[str] = set()     # 프로브 실패한 장치 — 설정 변경 전까지 skip
        self._recording_has_audio: bool = False

        # Overlay config (matches frontend preferences)
        # 기본값: top-left + 24px (frontend useWebcam.ts와 동기화)
        self._overlay_position: str = "top-left"  # top-left|top-right|bottom-left|bottom-right|off
        self._overlay_color: tuple[int, int, int] = (255, 255, 255)  # BGR for cv2
        self._overlay_font_scale: float = 1.0  # 1.0 == 24px (frontend syncOverlayToBackend 기준)

    # ------------------------------------------------------------
    # Device enumeration / probe
    # ------------------------------------------------------------
    def list_devices(self, max_index: int = 10, exclude: Optional[set[int]] = None) -> list[dict]:
        """장착된 카메라 index 탐지 (0..max_index 순회).

        exclude: 프로브를 건너뛸 인덱스 집합. 다른 곳에서 이미 점유 중인 인덱스를
        재오픈하면 기존 점유자의 캡처가 끊어질 수 있으므로 반드시 전달할 것.

        Linux 노트북은 카메라 1개당 /dev/videoN 노드가 여러 개(capture/metadata) 인 경우가
        많아 max_index 를 5→10 으로 상향 (예: 내장 + USB 가 video0,1,2,3 + video4,5 인 경우).
        Linux 한정으로 /dev/video* 가 하나도 없으면 빈 목록을 빠르게 반환.
        """
        exclude = exclude or set()

        # Linux: /dev/video* 존재 확인 — 노드 자체가 없으면 즉시 빈 결과.
        # 노드는 있는데 모두 열기 실패하면 권한/backend 문제이므로 경고 로그를 남긴다.
        if sys.platform.startswith("linux"):
            try:
                import os
                video_nodes = sorted([n for n in os.listdir("/dev") if n.startswith("video") and n[5:].isdigit()])
                if not video_nodes:
                    logger.warning("Webcam list_devices: no /dev/video* nodes detected on this host.")
                    return []
                logger.debug("Webcam list_devices: /dev video nodes = %s", video_nodes)
            except Exception as e:
                logger.debug("Webcam list_devices: /dev scan failed: %s", e)

        found: list[dict] = []
        failed_indices: list[int] = []
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
            cap = _open_capture(idx)
            if cap is None:
                failed_indices.append(idx)
                continue
            try:
                w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
                h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
                found.append({"index": idx, "label": f"Camera {idx} ({w}x{h})"})
            finally:
                try:
                    cap.release()
                except Exception:
                    pass
        if not found:
            logger.warning(
                "Webcam list_devices: 0 cameras found (tried indices 0..%d, exclude=%s, failed=%s, backends=%s). "
                "Linux 일 경우 사용자가 'video' 그룹에 속해 있는지 / opencv 가 V4L2 지원으로 빌드되었는지 확인.",
                max_index - 1, sorted(exclude), failed_indices, _CV_CAM_BACKENDS,
            )
        return found

    def probe_resolutions(self, device_index: int) -> list[str]:
        """카메라가 지원하는 대표 해상도 후보를 set/get으로 검증."""
        candidates = [
            (3840, 2160), (2560, 1440), (1920, 1080),
            (1280, 720), (960, 540), (640, 480), (320, 240),
        ]
        supported: list[str] = []
        cap = _open_capture(device_index)
        if cap is None:
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
        cap = _open_capture(device_index)
        if cap is None:
            logger.warning("Webcam open failed: device %d (all backends rejected)", device_index)
            return False
        # MJPG(압축) 포맷 우선 — 기본 YUY2(무압축)는 USB 대역폭을 크게 예약해
        # 주 디바이스 웹캠과 같은 허브에서 공존이 불가능해진다 (WebcamDevice와 동일 정책).
        # 미지원 카메라는 set이 조용히 무시됨.
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        # 카메라의 최대 fps 를 요청 — 드라이버가 지원 최댓값으로 클램프한다.
        # (실제 녹화 타이밍은 VFR/벽시계가 보존하므로, 이 값은 보고용/폴백용 힌트일 뿐이다)
        try:
            cap.set(cv2.CAP_PROP_FPS, 120.0)
        except Exception:
            pass
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
        """백그라운드 스레드: 카메라가 주는 대로(=최대 fps) 프레임을 읽어 최신본 유지 + 녹화 중이면 기록.

        cap.read() 가 하드웨어 프레임 도착까지 블로킹하므로 인위적 pacing 없이도 busy-spin 이
        아니다. 프레임 타이밍 보존은 ffmpeg(VFR, 벽시계 PTS)이 담당하므로 여기서 레이트를
        고정하지 않는다 — 이렇게 해야 카메라 최대 fps 가 그대로 살아난다.

        자가복구: USB 카메라가 장시간 stress run 중 일시적으로 끊기면 cap.read() 가 계속
        실패한다. 이전에는 sleep 후 무한 재시도만 했기 때문에 한 번 끊기면 그 이후의 모든
        회차 녹화가 프레임 0개 → 0바이트/재생불가 mp4 가 되는 문제가 있었다. 연속 read 실패가
        임계치를 넘으면 같은 디바이스를 재오픈해 복구한다.
        """
        # 연속 read 실패 카운트 — 임계치 초과 시 카메라 재오픈 시도.
        # 0.03s sleep × ~100 ≈ 3초 무프레임이면 끊긴 것으로 판단.
        read_failures = 0
        _REOPEN_THRESHOLD = 100
        while not self._stop_flag.is_set():
            cap = self._cap
            if cap is None or not cap.isOpened():
                time.sleep(0.05)
                read_failures += 1
                if read_failures >= _REOPEN_THRESHOLD:
                    self._try_reopen_capture()
                    read_failures = 0
                continue
            ret, frame = cap.read()
            if not ret or frame is None:
                time.sleep(0.03)
                read_failures += 1
                if read_failures >= _REOPEN_THRESHOLD:
                    self._try_reopen_capture()
                    read_failures = 0
                continue
            read_failures = 0
            # 최신 프레임 저장
            with self._latest_frame_lock:
                self._latest_frame = frame
            # 녹화 중이면 오버레이 후 writer 기록
            with self._recording_lock:
                if (self._ffmpeg_proc is not None or self._cv_writer is not None) and not self._recording_paused:
                    self._write_frame_unlocked(frame)

    def _try_reopen_capture(self) -> None:
        """캡처 루프 내에서 끊긴 카메라를 같은 디바이스/해상도로 재오픈한다.

        녹화는 그대로 유지된다(_ffmpeg_proc/_cv_writer 는 건드리지 않음) — 복구 후
        프레임이 다시 들어오면 진행 중이던 녹화에 이어서 기록된다. 재오픈 실패 시
        다음 임계치에서 다시 시도한다.
        """
        if self._stop_flag.is_set():
            return
        logger.warning("Webcam read stalled — attempting reopen of device %d", self._device_index)
        old = self._cap
        self._cap = None
        if old is not None:
            try:
                old.release()
            except Exception:
                pass
        cap = _open_capture(self._device_index)
        if cap is None:
            logger.warning("Webcam reopen failed: device %d (will retry)", self._device_index)
            return
        try:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)
            cap.set(cv2.CAP_PROP_FPS, 120.0)
        except Exception:
            pass
        self._cap = cap
        logger.info("Webcam reopened: device %d %dx%d", self._device_index, self._width, self._height)

    # ------------------------------------------------------------
    # Preview
    # ------------------------------------------------------------
    def get_latest_frame(self) -> Optional["np.ndarray"]:
        """최신 프레임 BGR ndarray 복사본 반환 (오버레이 미적용).

        Compositor가 이 싱글톤이 점유 중인 카메라를 소스로 쓸 때 이중 오픈 대신
        여기서 프레임을 공유받는다 (DirectShow는 같은 카메라 이중 오픈 거부)."""
        with self._latest_frame_lock:
            frame = self._latest_frame
            return None if frame is None else frame.copy()

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
    # Audio (마이크) 설정
    # ------------------------------------------------------------
    def list_audio_devices(self, force: bool = False) -> list[dict]:
        """dshow 오디오 입력 장치 목록 (비 Windows / ffmpeg 부재 시 빈 목록)."""
        return list_audio_input_devices(force=force)

    def set_audio(self, enabled: Optional[bool] = None, device: Optional[str] = None) -> None:
        """마이크 녹음 on/off + 장치 선택 ("" = auto). 설정 변경 시 실패 캐시를 비워 재시도를 허용."""
        if enabled is not None:
            self._audio_enabled = bool(enabled)
        if device is not None:
            self._audio_device = device
        if enabled is not None or device is not None:
            self._audio_failed.clear()
            logger.info("Webcam audio config: enabled=%s device=%s",
                        self._audio_enabled, self._audio_device or "(auto)")

    def _auto_audio_candidates(self) -> list[str]:
        """auto 모드 오디오 장치 후보 목록 (우선순위 순 식별자).

        1순위: 현재 웹캠의 내장 마이크 — dshow 오디오 장치명에 비디오 장치명이
        포함되는 관례로 매칭한다 (예: 웹캠 "USB CAMERA" ↔ 마이크 "마이크(USB CAMERA)").
        비디오 장치명은 dshow 비디오 열거 순서 == OpenCV CAP_DSHOW 인덱스 가정으로
        self._device_index 에서 얻는다 (같은 열거자라 순서가 일치; 틀려도 폴백이라 안전).
        이후: 나머지 오디오 장치를 열거 순서대로 (웹캠 마이크가 못 열리면 다음 후보로).
        """
        devs = _list_dshow_devices()
        audio = devs["audio"]
        if not audio:
            return []
        ordered = list(audio)
        video = devs["video"]
        if 0 <= self._device_index < len(video):
            cam_name = video[self._device_index]["name"].strip()
            if cam_name:
                matched = [a for a in audio if cam_name.lower() in a["name"].lower()]
                if matched:
                    logger.info("Webcam audio: matched webcam mic '%s' for camera '%s'",
                                matched[0]["name"], cam_name)
                    ordered = matched + [a for a in audio if a not in matched]
                else:
                    logger.info("Webcam audio: no mic matching camera '%s' — falling back to enumeration order",
                                cam_name)
        # 인코딩 왕복 문제가 없는 ASCII alternative name 우선
        return [a.get("alt") or a["name"] for a in ordered]

    def _resolve_audio_device(self) -> Optional[str]:
        """이번 녹화에 사용할 dshow 오디오 장치 식별자. 비활성/미지원/장치없음/프로브실패 → None.

        장치당 세션 최초 1회 실제 오픈 프로브를 수행한다(_probe_audio_device 참조).
        auto 모드는 후보(웹캠 마이크 → 나머지)를 순서대로 시도한다.
        락 밖에서 호출할 것 — 열거/프로브는 ffmpeg subprocess 실행이다.
        """
        if not self._audio_enabled or sys.platform != "win32":
            return None
        if self._audio_device:
            candidates = [self._audio_device]
        else:
            candidates = self._auto_audio_candidates()
            if not candidates:
                logger.info("Webcam audio: no dshow audio devices found — recording video-only")
                return None
        for target in candidates:
            if target in self._audio_failed:
                continue
            if target not in self._audio_validated:
                if not _probe_audio_device(target):
                    logger.warning("Webcam audio device '%s' unusable — trying next candidate", target)
                    self._audio_failed.add(target)
                    continue
                self._audio_validated.add(target)
            return target
        logger.warning("Webcam audio: no usable audio device — recording video-only")
        return None

    # ------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------
    def start_recording(self, output_path: str | Path) -> bool:
        """녹화 시작. output_path의 상위 폴더는 자동 생성. 우선 ffmpeg subprocess로 H.264 인코딩 시도, 실패 시 cv2.VideoWriter(mp4v)로 폴백."""
        # 오디오 장치 결정은 락 밖에서 — auto 모드의 장치 열거는 ffmpeg subprocess 실행이라
        # 락을 잡은 채 돌리면 캡처 루프(프리뷰/프레임 공유)가 그동안 통째로 멈춘다.
        audio_dev = self._resolve_audio_device()
        with self._recording_lock:
            if self._ffmpeg_proc is not None or self._cv_writer is not None:
                logger.warning("Webcam recording already in progress")
                return False
            if not self.is_open():
                logger.warning("Webcam not open — cannot start recording")
                return False
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)

            # 1순위: ffmpeg subprocess (브라우저 호환 H.264) — 마이크 오디오 동시 캡처
            # (audio_dev 는 위에서 프로브 검증 완료 — 여기서 실패하면 ffmpeg 자체 문제)
            proc = _spawn_ffmpeg_writer(
                path, self._width, self._height, self._actual_fps,
                audio_device=audio_dev,
            )
            if proc is not None:
                self._recording_has_audio = audio_dev is not None
                self._ffmpeg_proc = proc
            else:
                # 폴백: cv2.VideoWriter mp4v
                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                writer = cv2.VideoWriter(str(path), fourcc, self._actual_fps, (self._width, self._height))
                if not writer.isOpened():
                    logger.error("Failed to open VideoWriter: %s", path)
                    return False
                self._cv_writer = writer
                self._recording_has_audio = False

            self._recording_path = path
            self._recording_paused = False
            self._record_start_ts = time.monotonic()
            self._frames_written = 0
            logger.info("Webcam recording started: %s (%dx%d @%.1ffps, mode=%s, audio=%s)",
                        path, self._width, self._height, self._actual_fps,
                        "ffmpeg-h264" if self._ffmpeg_proc else "cv2-mp4v",
                        "on" if self._recording_has_audio else "off")
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
            self._recording_has_audio = False

        # ffmpeg flush + 종료 대기 (lock 외부) — +faststart moov atom 재작성 포함
        # (오디오 포함 시 stdin close → 영상 EOF → -shortest 가 라이브 오디오 입력을 끊는다)
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

        avg_fps = (frames / duration) if duration > 0 else 0.0
        logger.info("Webcam recording stopped: %s frames=%d duration=%.1fs (avg %.1ffps, VFR)",
                    path, frames, duration, avg_fps)

        # 빈/손상 녹화 정리: 프레임이 한 장도 안 들어왔거나(카메라 끊김 등) 결과 파일이
        # 0바이트면 재생 불가(SRC_NOT_SUPPORTED)한 쓰레기 파일이다. 목록에 남아 깨진 회차로
        # 보이지 않도록 즉시 삭제하고 None 을 반환해 호출 측이 이동/등록하지 않게 한다.
        if path is not None:
            try:
                size = path.stat().st_size if path.exists() else 0
            except Exception:
                size = 0
            if frames == 0 or size == 0:
                logger.warning("Webcam recording empty (frames=%d, size=%d) — deleting %s", frames, size, path)
                try:
                    if path.exists():
                        path.unlink()
                except Exception as e:
                    logger.warning("Failed to delete empty recording %s: %s", path, e)
                return None
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
                    if self._recording_has_audio:
                        # 오디오 입력이 녹화 도중 ffmpeg 를 죽였을 수 있다(마이크 분리/점유 등)
                        # — 다음 녹화에서 프로브를 다시 거치도록 검증 캐시를 비운다.
                        self._audio_validated.clear()
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
            "audio_enabled": self._audio_enabled,
            "audio_device": self._audio_device,
            "recording_audio": self._recording_has_audio if recording else False,
        }


# Singleton
_webcam_service: Optional[WebcamService] = None


def get_webcam_service() -> WebcamService:
    global _webcam_service
    if _webcam_service is None:
        _webcam_service = WebcamService()
    return _webcam_service
