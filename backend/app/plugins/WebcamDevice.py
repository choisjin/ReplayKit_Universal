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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """웹캠 오픈. 성공 시 캡처 가능 상태가 되고 자체적인 capture loop는 없음
        (단발 캡처 시 lock 하에 cap.read())."""
        if self._is_connected and self._cap is not None:
            return "Already connected"

        cap = _open_capture(self._device_index)
        if cap is None:
            raise RuntimeError(
                f"Webcam open failed: device {self._device_index} "
                f"(tried backends {_CV_CAM_BACKENDS})"
            )

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
        logger.info("Webcam device connected: index=%d %dx%d",
                    self._device_index, actual_w, actual_h)
        return f"Connected: webcam {self._device_index} ({actual_w}x{actual_h})"

    def Disconnect(self) -> str:
        """웹캠 해제."""
        with self._lock:
            if self._cap is not None:
                try:
                    self._cap.release()
                except Exception:
                    pass
                self._cap = None
            self._is_connected = False
        logger.info("Webcam device disconnected: index=%d", self._device_index)
        return "Disconnected"

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
        if not self._is_connected or self._cap is None:
            raise RuntimeError("Webcam not connected")
        with self._lock:
            cap = self._cap
            if cap is None or not cap.isOpened():
                raise RuntimeError("Webcam not connected")
            # DirectShow 카메라는 가끔 첫 read가 stale → 한 번 버리고 다시
            cap.grab()
            ret, frame = cap.read()
        if not ret or frame is None:
            raise RuntimeError("Webcam read failed")
        return frame

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
