"""iPhone Agent Service — pymobiledevice3 기반 iOS 디바이스 제어.

iphone_controller/controller.py 의 PymobileDevice3Controller 를 ReplayKit
DeviceManager 호환 async 서비스로 이식한 것이다. RSD 터널(또는 remote tunneld)을
통해 iPhone 의 터치/하드웨어 버튼/스크린샷을 제어한다.

주요 기능:
  - tap / drag (픽셀 좌표 → HID 좌표 자동 변환)
  - button (home/lock/volume-up/volume-down/mute/siri)
  - button_sequence (여러 버튼 연속 전송)
  - session (스크립트 기반 HID 세션)
  - screenshot (DVT/Instruments 채널 또는 CLI 폴백)

연결 방식:
  - address 필드에 iPhone UDID 를 저장한다.
  - 연결 시 RSD 터널(또는 tunneld)을 시작하고 UDID 를 확인한다.
  - 해상도는 info["resolution"] 을 사용하되, 명시되지 않으면 연결된 iPhone 의
    ProductType 을 기반으로 자동 감지한다 (controller.get_device_info()).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import tempfile
import time
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── pymobiledevice3 controller ───────────────────────────────────
# iphone_controller.controller 는 pymobiledevice3 를 import 하느라 ~0.7s 걸린다.
# device_manager 가 이 모듈을 기동 시 import 하므로, 실제 iPhone 을 연결할 때까지
# 지연 import 한다 (_load_controller_class).
_PymdController = None
_ctrl_import_error: Optional[Exception] = None


def _load_controller_class():
    global _PymdController, _ctrl_import_error
    if _PymdController is None and _ctrl_import_error is None:
        try:
            from ..iphone_controller.controller import PymobileDevice3Controller
            _PymdController = PymobileDevice3Controller
        except Exception as e:
            _ctrl_import_error = e
            logger.warning("iPhone: PymobileDevice3Controller import failed: %s", e)
    return _PymdController


# ── iPhone hardware button registry (for ReplayKit UI / playback) ─────────────────
# controller.button() 에 전달하는 pymobiledevice3 HID button 이름을 그대로 사용한다.
# HKMC_KEYS 와 동일한 형태로 관리해 frontend 의 버튼 패널에서 일관되게 렌더링한다.
IPHONE_BUTTONS: dict[str, dict] = {
    "HOME":        {"name": "home",        "label": "홈",        "group": "IPHONE"},
    "LOCK":        {"name": "lock",        "label": "잠금",      "group": "IPHONE"},
    "VOLUME_UP":   {"name": "volume-up",   "label": "볼륨+",     "group": "IPHONE"},
    "VOLUME_DOWN": {"name": "volume-down", "label": "볼륨-",     "group": "IPHONE"},
    "MUTE":        {"name": "mute",        "label": "무음",      "group": "IPHONE"},
    "SIRI":        {"name": "siri",        "label": "Siri",      "group": "IPHONE"},
}


def _convert_image(data: bytes, want: str) -> bytes:
    """PNG/TIFF 원본을 want("png"|"jpeg") 로 변환. 이미 그 포맷이면 그대로."""
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = data[:2] == b"\xff\xd8"
    if (want == "png" and is_png) or (want == "jpeg" and is_jpeg):
        return data
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            if want == "jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=85)
            else:
                im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:
        return data


class iPhoneAgentService:
    """pymobiledevice3 기반 iPhone 제어 서비스.

    DeviceManager/playback_service 가 generic tap/swipe/long_press/repeat_tap/
    screenshot 스텝을 디스패치할 수 있도록 async API surface 를 제공한다.
    """

    default_screen = "main"

    def __init__(
        self,
        udid: str,
        device_id: str = "",
        resolution: str = "1170x2532",
        python: str = "",
    ) -> None:
        self.udid = udid
        self.device_id = device_id or f"iPhone_{udid}"
        self.agent_version = "iPhone Agent"
        self._connected = False
        self.last_error = ""
        # 해상도 파싱 (픽셀→HID 변환용)
        # 사용자가 명시적으로 해상도를 지정했는지 여부를 추적한다.
        # 기본값 "1170x2532" 가 그대로 전달되면 자동 감지 대상으로 간주한다.
        self._resolution_explicit = bool(resolution) and str(resolution).strip() not in ("", "1170x2532")
        try:
            rw, rh = str(resolution).upper().split("X")
            self._resolution = (int(rw), int(rh))
        except Exception:
            self._resolution = (1170, 2532)
        # 자동 감지된 디바이스 정보 (모델/이름/해상도)
        self._device_info = {}
        # pymobiledevice3 controller 인스턴스
        self._controller = None
        # PYMD_PYTHON 환경변수 (기본 sys.executable)
        self._python = python or os.environ.get("PYMD_PYTHON") or sys.executable
        # 마지막 입력 시각 (라이브 미러 적응형 리프레시용)
        self.last_input_ts = 0.0
        # DDI(Developer Disk Image) 마운트 상태 — mounter auto-mount 결과
        self._ddi_mounted = False
        self._mount_error = ""
        self._mount_timeout = int(os.environ.get("PYMD_MOUNT_TIMEOUT", "60"))
        # get_screen_frame 변환 캐시: (원본 bytes 객체, 포맷, 변환 결과)
        self._frame_cache: Optional[tuple] = None

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def resolution(self) -> Tuple[int, int]:
        return self._resolution

    def _ensure_controller(self):
        """PymobileDevice3Controller 인스턴스를 지연 생성."""
        if self._controller is not None:
            return self._controller
        # PYMD_PYTHON 은 controller 모듈 import 시점에 읽히므로 import 전에 설정한다.
        if self._python and not os.environ.get("PYMD_PYTHON"):
            os.environ["PYMD_PYTHON"] = self._python
        cls = _load_controller_class()
        if cls is None:
            raise RuntimeError(
                f"PymobileDevice3Controller not available: {_ctrl_import_error}. "
                "Ensure pymobiledevice3 is installed (pip install pymobiledevice3==11.9.2)."
            )
        self._controller = cls(udid=self.udid)
        return self._controller

    # ------------------------------------------------------------------
    # Connect / info
    # ------------------------------------------------------------------
    def _require_mounted(self) -> None:
        """터치/HID 동작 전 DDI 마운트가 완료되었는지 확인."""
        if not self._ddi_mounted:
            msg = self._mount_error or "DDI not mounted. Run 'pymobiledevice3 mounter auto-mount' first."
            raise RuntimeError(f"iPhone input blocked: {msg}")

    def _auto_mount_sync(self) -> bool:
        """pymobiledevice3 mounter auto-mount 를 실행해 DDI 를 마운트한다."""
        self._mount_error = ""
        cmd = [self._python, "-m", "pymobiledevice3", "mounter", "auto-mount"]
        if self.udid:
            # 여러 대 연결 시 usbmux 첫 기기가 아닌 대상 기기에 마운트
            cmd += ["--udid", self.udid]
        logger.info("iPhone auto-mount: running %s", " ".join(cmd))
        try:
            kwargs = {"capture_output": True, "text": True, "timeout": self._mount_timeout}
            if sys.platform == "win32":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            r = subprocess.run(cmd, **kwargs)
            if r.returncode == 0:
                self._ddi_mounted = True
                logger.info("iPhone auto-mount: success")
                return True
            err = (r.stderr or r.stdout or "unknown error").strip()
        except subprocess.TimeoutExpired:
            err = f"timeout after {self._mount_timeout}s"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
        self._ddi_mounted = False
        self._mount_error = err
        logger.warning("iPhone auto-mount: failed: %s", err)
        return False

    def _connect_sync(self) -> bool:
        self.last_error = ""
        try:
            ctrl = self._ensure_controller()
            # 1) UDID 확인 — usbmux list 에서 해당 UDID 가 있는지 먼저 본다 (빠르고 원인이 명확).
            # controller 가 self.udid 기기만 매칭하므로 여러 대 연결돼도 대상 기기를 찾는다.
            udid = ctrl._get_udid(timeout=10)
            if not udid:
                self.last_error = (
                    f"iPhone {self.udid} not found via usbmux. "
                    "Ensure device is connected, unlocked and trusted."
                    if self.udid else
                    "No iPhone detected via usbmux. Ensure device is connected and unlocked."
                )
                logger.warning("iPhone connect failed: %s", self.last_error)
                return False
            # 2) DDI 자동 마운트 — developer(HID/DVT) 명령 전 필수
            if not self._auto_mount_sync():
                self.last_error = f"DDI auto-mount failed: {self._mount_error}"
                logger.warning("iPhone connect failed: %s", self.last_error)
                return False
            # 3) 터널 시작 (RSD 터널 또는 tunneld)
            try:
                ctrl.ensure_tunnel()
            except Exception as e:
                # tunneld 폴백
                try:
                    ctrl.start_tunneld(timeout=30)
                except Exception as e2:
                    self.last_error = f"Tunnel start failed: {e} / tunneld: {e2}"
                    logger.warning("iPhone connect failed: %s", self.last_error)
                    return False
            # 해상도 자동 감지 — 사용자가 명시적으로 지정하지 않은 경우에만
            if not self._resolution_explicit:
                try:
                    info = ctrl.get_device_info(timeout=10)
                    self._device_info = info
                    if info.get("screen_width") and info.get("screen_height"):
                        self._resolution = (int(info["screen_width"]), int(info["screen_height"]))
                        logger.info(
                            "iPhone resolution auto-detected: %sx%s (source=%s, product_type=%s)",
                            self._resolution[0], self._resolution[1],
                            info.get("source"), info.get("product_type"),
                        )
                    else:
                        logger.warning(
                            "iPhone resolution auto-detect failed (source=%s). Using %sx%s",
                            info.get("source"), self._resolution[0], self._resolution[1],
                        )
                except Exception as e:
                    logger.warning("iPhone resolution auto-detect error: %s", e)
            else:
                # 명시적 해상도가 있더라도 디바이스 정보는 수집한다
                try:
                    self._device_info = ctrl.get_device_info(timeout=10)
                except Exception:
                    pass
            logger.info("iPhone connect OK: udid=%s resolution=%s", udid, self._resolution)
            return True
        except Exception as e:
            self.last_error = str(e)
            logger.warning("iPhone connect failed: %s", e)
            return False

    async def async_connect(self) -> bool:
        loop = asyncio.get_running_loop()
        try:
            ok = await loop.run_in_executor(None, self._connect_sync)
            self._connected = bool(ok)
            if ok:
                logger.info("iPhoneAgentService(%s): connected", self.udid)
            return self._connected
        except Exception as e:
            logger.warning("iPhoneAgentService(%s): connect failed: %s", self.udid, e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._connected = False
        self._ddi_mounted = False
        try:
            if self._controller is not None:
                self._controller.close()
        except Exception:
            pass
        self._controller = None
        logger.debug("iPhoneAgentService(%s): disconnected", self.udid)

    def get_info(self) -> dict:
        """프론트용 displays + screens 반환."""
        w, h = self._resolution
        displays = [{
            "id": 0,
            "sf_id": "0",
            "logical_id": 0,
            "is_active": True,
            "name": "main",
            "width": w,
            "height": h,
        }]
        info = {
            "agent": "iPhone",
            "udid": self.udid,
            "displays": displays,
            "screens": {"main": {"width": w, "height": h}},
            "default_screen": self.default_screen,
        }
        # 자동 감지된 디바이스 정보 (모델/이름) 포함
        if self._device_info:
            info["device"] = {
                "product_type": self._device_info.get("product_type"),
                "device_name": self._device_info.get("device_name"),
                "resolution_source": self._device_info.get("source"),
            }
        return info

    def detect_resolution(self, screen_id: int = 0) -> Tuple[int, int]:
        return self._resolution

    # ------------------------------------------------------------------
    # 입력 (pymobiledevice3 HID)
    # ------------------------------------------------------------------
    def _run_sync(self, fn, *args, **kwargs):
        """동기 controller 메서드를 executor 에서 실행."""
        loop = asyncio.get_running_loop()
        return loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    async def async_tap(self, x: int, y: int, screen_type=None) -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        w, h = self._resolution
        r = await self._run_sync(ctrl.tap, int(x), int(y), w, h)
        self.last_input_ts = time.monotonic()
        if not r.get("success"):
            raise RuntimeError(f"iPhone tap failed: {r.get('stderr', 'unknown')}")

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type=None, duration_ms: int = 0,
                          hold_ms: int = 0) -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        w, h = self._resolution
        duration = (duration_ms or 0) / 1000.0 if duration_ms else None
        hold = (hold_ms or 0) / 1000.0 if hold_ms else None
        r = await self._run_sync(ctrl.drag, int(x1), int(y1), int(x2), int(y2),
                                 w, h, duration=duration, hold=hold)
        self.last_input_ts = time.monotonic()
        if not r.get("success"):
            raise RuntimeError(f"iPhone swipe failed: {r.get('stderr', 'unknown')}")

    async def async_long_press(self, x: int, y: int, duration_ms: int = 1000,
                               screen_type=None) -> None:
        # long-press 는 동일점 swipe 로 근사 (BMW 와 동일 전략)
        await self.async_swipe(x, y, x, y, screen_type,
                               duration_ms=duration_ms, hold_ms=duration_ms)

    async def async_repeat_tap(self, x: int, y: int, count: int = 5,
                               interval_ms: int = 100, screen_type=None) -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        w, h = self._resolution
        for i in range(int(count)):
            r = await self._run_sync(ctrl.tap, int(x), int(y), w, h)
            if not r.get("success"):
                raise RuntimeError(f"iPhone repeat_tap failed at {i}: {r.get('stderr', 'unknown')}")
            if i < int(count) - 1:
                await asyncio.sleep((interval_ms or 100) / 1000.0)
        self.last_input_ts = time.monotonic()

    async def async_button(self, name: str, state: str = "press") -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        r = await self._run_sync(ctrl.button, name, state)
        self.last_input_ts = time.monotonic()
        if not r.get("success"):
            raise RuntimeError(f"iPhone button '{name}' failed: {r.get('stderr', 'unknown')}")

    async def async_button_sequence(self, sequence: List[tuple], interval: float = 0.25) -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        r = await self._run_sync(ctrl.button_sequence, sequence, interval)
        self.last_input_ts = time.monotonic()
        if not r.get("success"):
            raise RuntimeError(f"iPhone button_sequence failed: {r.get('stderr', 'unknown')}")

    async def async_session(self, commands: List[str]) -> None:
        ctrl = self._ensure_controller()
        self._require_mounted()
        w, h = self._resolution
        r = await self._run_sync(ctrl.session, commands, w, h)
        self.last_input_ts = time.monotonic()
        if not r.get("success"):
            raise RuntimeError(f"iPhone session failed: {r.get('stderr', 'unknown')}")

    # ------------------------------------------------------------------
    # 스크린샷
    # ------------------------------------------------------------------
    async def async_screencap_bytes(self, screen_type=None, fmt: str = "png") -> bytes:
        """iPhone 화면 캡처 → PNG/JPEG 바이트.

        DVT/Instruments screenshot 채널을 우선 사용하고, 실패 시 CLI 폴백.
        """
        ctrl = self._ensure_controller()
        loop = asyncio.get_running_loop()

        def _capture() -> bytes:
            # 1) DVT/Instruments screenshot 채널
            try:
                if ctrl.screen_stream and ctrl.screen_stream.is_running():
                    frame, _meta = ctrl.screen_stream.get_latest_frame()
                    if frame:
                        return frame
            except Exception:
                pass
            # 2) CLI 폴백 — pymobiledevice3 screenshot 명령 (임시 파일)
            tmp_path = ""
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="iphone_ss_")
                os.close(fd)
                r = ctrl.run(["developer", "dvt", "screenshot", tmp_path], timeout=20, retries=1)
                if r.get("success") and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "rb") as f:
                        return f.read()
                raise RuntimeError(f"iPhone screenshot failed: {r.get('stderr', 'unknown')}")
            finally:
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        try:
            data = await loop.run_in_executor(None, _capture)
        except Exception as e:
            raise RuntimeError(f"iPhone screenshot failed: {e}")
        want = (fmt or "png").lower()
        if want in ("jpg", "jpeg"):
            want = "jpeg"
        return await loop.run_in_executor(None, _convert_image, data, want)

    # ------------------------------------------------------------------
    # 라이브 스크린 스트림 (WebSocket 미러링용)
    # ------------------------------------------------------------------
    def is_screen_stream_running(self) -> bool:
        if self._controller is not None:
            try:
                return bool(self._controller.screen_stream.is_running())
            except Exception:
                pass
        return False

    async def start_screen_stream(self, fps: float = 5.0) -> dict:
        """DVT/Instruments 채널로 주기적 캡처를 시작한다."""
        ctrl = self._ensure_controller()
        loop = asyncio.get_running_loop()

        def _start() -> dict:
            return ctrl.screen_stream.start(fps)

        return await loop.run_in_executor(None, _start)

    def stop_screen_stream(self) -> dict:
        """라이브 캡처 스레드를 중지한다."""
        if self._controller is not None:
            try:
                return self._controller.screen_stream.stop()
            except Exception as e:
                logger.warning("iPhone stop_screen_stream error: %s", e)
        return {"success": True, "running": False}

    async def get_screen_frame(self, fmt: str = "jpeg") -> bytes:
        """라이브 스트림 최신 프레임을 반환. 준비 전이면 폴백 캡처."""
        ctrl = self._ensure_controller()
        loop = asyncio.get_running_loop()

        def _get() -> bytes:
            # 1) 라이브 스트림이 실행 중이면 최신 프레임 사용
            try:
                if ctrl.screen_stream and ctrl.screen_stream.is_running():
                    frame, _meta = ctrl.screen_stream.get_latest_frame()
                    if frame:
                        return frame
            except Exception:
                pass
            # 2) 스트림이 없거나 프레임이 없으면 1회 CLI 캡처 (임시 파일)
            tmp_path = ""
            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".png", prefix="iphone_ss_")
                os.close(fd)
                r = ctrl.run(["developer", "dvt", "screenshot", tmp_path], timeout=20, retries=1)
                if r.get("success") and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 0:
                    with open(tmp_path, "rb") as f:
                        return f.read()
                raise RuntimeError(f"iPhone get_screen_frame failed: {r.get('stderr', 'unknown')}")
            finally:
                if tmp_path and os.path.isfile(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        data = await loop.run_in_executor(None, _get)
        want = (fmt or "jpeg").lower()
        if want in ("jpg", "jpeg"):
            want = "jpeg"
        # 스트림 fps 보다 미러 루프가 빠르면 같은 원본 프레임이 반복된다 — 재인코딩 없이
        # 같은 객체를 돌려줘 호출측이 `is` 로 중복 전송을 거를 수 있게 한다.
        cache = self._frame_cache
        if cache is not None and cache[0] is data and cache[1] == want:
            return cache[2]
        out = await loop.run_in_executor(None, _convert_image, data, want)
        self._frame_cache = (data, want, out)
        return out