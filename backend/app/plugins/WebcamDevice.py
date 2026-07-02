# -*- coding: utf-8 -*-
"""WebcamDevice 플러그인 — 웹캠(cv2.VideoCapture)을 주 디바이스(스크린 소스)로 사용.

VisionCamera와 동일한 인터페이스(Connect/Disconnect/IsConnected/Capture/CaptureBytes/
CaptureToFile)를 제공하여 playback/screenshot 경로의 기존 분기 로직을 최소 변경으로
재사용한다.

주의: 같은 physical device_index를 녹화용 singleton WebcamService와 동시에 열면
DirectShow가 두 번째 오픈을 거부할 수 있다. 녹화용 웹캠과 주 디바이스 웹캠은 서로
다른 device_index를 사용할 것.

connect_type: "webcam"
"""

from __future__ import annotations

import io
import logging
import sys
import tempfile
import threading
import time as _time
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2

# OS 별 OpenCV VideoCapture 백엔드 후보 — 앞에서부터 순차 시도.
# Linux 에서 CAP_V4L2 가 거부되는 빌드/환경 대비로 CAP_ANY 를 폴백으로 둠.
# (webcam_service._open_capture 와 동일 정책 — 분리되어 있는 모듈이지만 동일 카메라
#  하드웨어를 다루므로 동일 backend 후보를 사용해야 일관된 동작.)
if sys.platform == "win32":
    _CV_CAM_BACKENDS: tuple[int, ...] = (cv2.CAP_DSHOW,)
elif sys.platform.startswith("linux"):
    _CV_CAM_BACKENDS = (cv2.CAP_V4L2, cv2.CAP_ANY)
else:
    _CV_CAM_BACKENDS = (cv2.CAP_ANY,)

_CV_CAM_BACKEND = _CV_CAM_BACKENDS[0]


def _open_capture(index: int):
    """후보 backend 들을 순회하며 첫 번째 isOpened()=True 캡처를 반환."""
    for backend in _CV_CAM_BACKENDS:
        try:
            cap = cv2.VideoCapture(index, backend)
        except Exception as e:
            logger.debug("VideoCapture(%d, backend=%d) raised: %s", index, backend, e)
            continue
        if cap.isOpened():
            if backend != _CV_CAM_BACKENDS[0]:
                logger.info("Webcam index %d opened via fallback backend %d", index, backend)
            return cap
        try:
            cap.release()
        except Exception:
            pass
    return None
import numpy as np

logger = logging.getLogger(__name__)


class WebcamDevice:
    """웹캠 플러그인 (주 디바이스로 등록 가능)."""

    def __init__(self, device_index: int = 0, width: int = 640, height: int = 480):
        self._device_index = int(device_index)
        self._width = int(width) if width else 0
        self._height = int(height) if height else 0
        self._cap: Optional[cv2.VideoCapture] = None
        self._is_connected = False
        self._lock = threading.Lock()  # cap.read() 직렬화

        # 연속 캡처 스레드가 유지하는 최신 프레임 (+도착 시각).
        # 단발 cap.read()는 드라이버/그래프가 마지막으로 '전달해 둔' 프레임을 반환할 뿐
        # '지금' 프레임을 보장하지 않는다 — 재생 중(미러링 없음) 캡처가 이전 스텝 시점
        # 화면으로 나오던 원인. 캡처 스레드가 카메라 fps로 상시 read하여 스트림을 살아있게
        # 유지하고, _read_frame은 '요청 시점 이후 도착한' 프레임만 반환한다.
        self._capture_thread: Optional[threading.Thread] = None
        self._stop_capture = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame: Optional[np.ndarray] = None
        self._latest_ts = 0.0  # _latest_frame 도착 시각 (monotonic)

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """웹캠 오픈 + 연속 캡처 스레드 시작 (최신 프레임 유지)."""
        if self._is_connected and self._cap is not None:
            return "Already connected"

        cap = _open_capture(self._device_index)
        if cap is None:
            raise RuntimeError(
                f"Webcam open failed: device {self._device_index} "
                f"(tried backends {_CV_CAM_BACKENDS})"
            )

        # MJPG(압축) 포맷 우선 — DirectShow 기본 YUY2(무압축)는 1080p 하나가 USB2
        # 대역폭(480Mbps)을 거의 다 예약해, 같은 허브의 두 번째 웹캠이 아예 열리지
        # 못한다(주 디바이스 연결 시 PIP 웹캠 실종의 원인). MJPG는 대역폭이 ~1/10이라
        # 멀티 웹캠 공존 가능. 미지원 카메라는 set이 조용히 무시되어 기본 포맷 유지.
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        except Exception:
            pass

        # 해상도 설정 (실패해도 무시 — driver가 지원하는 범위로 고정됨)
        if self._width > 0 and self._height > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._height)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or self._width or 640
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or self._height or 480

        # 웜업 — DirectShow 카메라는 첫 프레임이 깨지는 경우가 있음
        for _ in range(3):
            ret, _frame = cap.read()
            if ret:
                break

        self._cap = cap
        self._width = actual_w
        self._height = actual_h
        self._is_connected = True
        # 연속 캡처 스레드 시작 — 최신 프레임 유지 + 스트림 활성 유지
        self._stop_capture.clear()
        with self._frame_lock:
            self._latest_frame = None
            self._latest_ts = 0.0
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name=f"WebcamDevice-{self._device_index}", daemon=True
        )
        self._capture_thread.start()
        logger.info("Webcam device connected: index=%d %dx%d",
                    self._device_index, actual_w, actual_h)
        return f"Connected: webcam {self._device_index} ({actual_w}x{actual_h})"

    def Disconnect(self) -> str:
        """웹캠 해제."""
        # 캡처 스레드 먼저 정지 (현재 read가 끝나면 루프 탈출)
        self._stop_capture.set()
        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=3)
        self._capture_thread = None
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._is_connected = False
        with self._frame_lock:
            self._latest_frame = None
            self._latest_ts = 0.0
        logger.info("Webcam device disconnected: index=%d", self._device_index)
        return "Disconnected"

    def _capture_loop(self) -> None:
        """카메라 fps로 상시 read하여 최신 프레임을 유지하는 백그라운드 루프.

        read 실패가 이어져도 스레드를 죽이지 않고 짧게 쉬며 재시도 — 일시적 USB
        드롭에서 스트림이 복귀하면 자동으로 이어진다.
        """
        fail_count = 0
        while not self._stop_capture.is_set():
            with self._lock:
                cap = self._cap
                if cap is None:
                    break
                try:
                    ret, frame = cap.read()
                except Exception:
                    ret, frame = False, None
            if ret and frame is not None:
                fail_count = 0
                with self._frame_lock:
                    self._latest_frame = frame
                    self._latest_ts = _time.monotonic()
            else:
                fail_count += 1
                # 연속 실패 시 backoff — busy-spin 방지 (0.1s → 최대 0.5s)
                _time.sleep(0.1 if fail_count < 30 else 0.5)
        logger.debug("WebcamDevice capture loop ended: index=%d", self._device_index)

    def IsConnected(self) -> bool:
        if not self._is_connected or self._cap is None:
            return False
        try:
            return bool(self._cap.isOpened())
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Capture
    # ------------------------------------------------------------------

    def _read_frame(self) -> np.ndarray:
        """'지금' 시점의 프레임 반환 — 요청 시각 이후에 캡처 스레드로 도착한 프레임만 사용.

        단발 cap.read()는 드라이버/그래프에 남아 있던 과거 프레임을 반환할 수 있어
        (재생 중 스텝 캡처가 직전 스텝 시점 화면으로 나오던 원인), 요청 시각(req_ts)
        이후 도착 프레임을 최대 1초 대기한다. 30fps면 ~33ms 안에 도착하므로 정상
        상태에선 지연이 거의 없다. 1초 내 새 프레임이 없으면 스트림 정체(USB 대역폭/
        드라이버) — 가장 최근 프레임으로 폴백하되 경고 로그로 정체를 가시화한다.
        """
        if not self._is_connected:
            raise RuntimeError("Webcam not connected")
        req_ts = _time.monotonic()
        deadline = req_ts + 1.0
        while _time.monotonic() < deadline:
            with self._frame_lock:
                frame = self._latest_frame
                ts = self._latest_ts
            if frame is not None and ts >= req_ts:
                return frame.copy()
            _time.sleep(0.01)
        # 폴백: 요청 이후 프레임이 안 옴 — 스트림 정체. 최신 보유 프레임 사용 + 경고.
        with self._frame_lock:
            frame = self._latest_frame
            ts = self._latest_ts
        if frame is None:
            raise RuntimeError("Webcam read failed (no frame from capture thread)")
        age = _time.monotonic() - ts
        logger.warning(
            "Webcam frame STALE: %.1fs old (index=%d) — 스트림 정체. "
            "USB 대역폭(웹캠 2대 동일 허브)/드라이버 확인 필요",
            age, self._device_index,
        )
        return frame.copy()

    def Capture(self, save_path: str = "") -> str:
        """이미지 캡처. save_path 비어있으면 임시 파일."""
        if not save_path:
            tmp_dir = Path(tempfile.gettempdir()) / "webcam_device"
            tmp_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%y%m%d_%H%M%S_%f")
            save_path = str(tmp_dir / f"{ts}.png")

        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        frame = self._read_frame()
        ok = cv2.imwrite(save_path, frame)
        if not ok:
            raise RuntimeError(f"Failed to write image: {save_path}")
        return save_path

    def CaptureBytes(self, fmt: str = "png") -> bytes:
        """캡처 후 바이트로 반환 (WebSocket 스트리밍용)."""
        frame = self._read_frame()
        ext = ".jpg" if fmt.lower() in ("jpg", "jpeg") else ".png"
        params = [cv2.IMWRITE_JPEG_QUALITY, 80] if ext == ".jpg" else []
        ok, buf = cv2.imencode(ext, frame, params)
        if not ok:
            raise RuntimeError(f"Failed to encode image as {fmt}")
        return bytes(buf.tobytes())

    def CaptureToFile(self, save_path: str) -> str:
        """지정된 경로에 PNG 이미지 캡처."""
        return self.Capture(save_path)

    def CropCapture(self, save_path: str, left: int, top: int, right: int, bottom: int) -> str:
        """크롭된 이미지 캡처."""
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        frame = self._read_frame()
        h, w = frame.shape[:2]
        l = max(0, min(int(left), w))
        t = max(0, min(int(top), h))
        r = max(l, min(int(right), w))
        b = max(t, min(int(bottom), h))
        cropped = frame[t:b, l:r]
        ok = cv2.imwrite(save_path, cropped)
        if not ok:
            raise RuntimeError(f"Failed to write cropped image: {save_path}")
        return save_path

    # ------------------------------------------------------------------
    # Info
    # ------------------------------------------------------------------

    def GetInfo(self) -> dict:
        return {
            "device_index": self._device_index,
            "width": self._width,
            "height": self._height,
            "connected": self._is_connected,
        }

    # ------------------------------------------------------------------
    # Exposure (DSHOW 기준 — CAP_PROP_AUTO_EXPOSURE: 0.25=manual, 0.75=auto)
    # ------------------------------------------------------------------

    def GetExposure(self) -> dict:
        """현재 노출값/모드 반환."""
        if not self._is_connected or self._cap is None:
            return {"supported": False}
        try:
            value = self._cap.get(cv2.CAP_PROP_EXPOSURE)
            auto = self._cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)
            return {
                "supported": True,
                "value": float(value),
                "auto": auto >= 0.5,
                "min": -13.0,
                "max": 0.0,
                "step": 1.0,
            }
        except Exception as e:
            logger.warning("WebcamDevice GetExposure failed: %s", e)
            return {"supported": False}

    def SetExposure(self, value: Optional[float] = None, auto: Optional[bool] = None) -> bool:
        """노출값 설정. value를 주면 manual 모드로 전환 후 적용. auto=True면 자동 모드."""
        if not self._is_connected or self._cap is None:
            return False
        try:
            with self._lock:
                if auto is True:
                    self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75)
                    logger.info("WebcamDevice exposure: AUTO (index=%d)", self._device_index)
                    return True
                if value is not None:
                    self._cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25)
                    self._cap.set(cv2.CAP_PROP_EXPOSURE, float(value))
                    actual = self._cap.get(cv2.CAP_PROP_EXPOSURE)
                    logger.info("WebcamDevice exposure: MANUAL value=%.2f (actual=%.2f, index=%d)",
                                value, actual, self._device_index)
                    return True
        except Exception as e:
            logger.warning("WebcamDevice SetExposure failed: %s", e)
        return False

    @staticmethod
    def list_available(max_index: int = 8, max_consecutive_fail: int = 2) -> list[dict]:
        """연결된 웹캠 index 스캔.

        max_consecutive_fail: 연속 실패 횟수가 이 값에 도달하면 이른 종료
        (DirectShow 실패 open은 각각 0.5~2초 걸리므로 불필요한 낭비 차단).
        """
        found: list[dict] = []
        consecutive_fail = 0
        for idx in range(max_index):
            cap = cv2.VideoCapture(idx, _CV_CAM_BACKEND)
            try:
                if cap.isOpened():
                    consecutive_fail = 0
                    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
                    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
                    # 첫 프레임을 실제로 읽어서 유효성 최종 확인
                    ret, _ = cap.read()
                    if ret:
                        found.append({
                            "index": idx,
                            "label": f"Camera {idx} ({w}x{h})",
                            "width": w,
                            "height": h,
                        })
                    else:
                        consecutive_fail += 1
                else:
                    consecutive_fail += 1
            finally:
                cap.release()
            if consecutive_fail >= max_consecutive_fail and not found:
                # 맨 앞 몇 개가 연속 실패면 더 이상 시도하지 않음
                break
        return found
