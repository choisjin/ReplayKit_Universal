"""BMW RSE Agent Service — ADB 기반 후석(rear-seat) 듀얼 디스플레이 IVI 제어.

Reference/BMWRSE_service.py(WebOS + ADB 혼용 standalone)를 ReplayKit DeviceManager
호환 async 서비스로 이식한 것이다. MIB/ICAS(SSH 기반)와 달리 **ADB serial** 로 연결하며,
일반 ADB 디바이스처럼 generic tap/swipe/long_press/screenshot 스텝을 그대로 재사용한다
(프론트는 멀티 디스플레이 ADB 디바이스로 취급).

디스플레이 매핑(후석 좌/우 전용 모델):
  - screen_id 0 = 후석 좌측 (rear_left)   ← lxc 컨테이너 webos1
  - screen_id 1 = 후석 우측 (rear_right)  ← lxc 컨테이너 webos2

screen_type 인자는 프론트의 ADB 멀티 디스플레이 선택값("0"/"1")을 그대로 받으며,
"rear_left"/"left"/"rear_right"/"right" 같은 이름도 허용한다(미지정=0).

캡처 백엔드(device.info["capture_backend"]):
  - "adb"   : lxc-attach android1 -- screencap -d <display_id>  (빠름, 기본값)
  - "webos" : luna-send captureCompositorOutput (WebOS 컴포지터 원본, 느림 ~1-2s)

터치/스와이프는 디바이스에 사전 배포된 touch simulator 스크립트
(/log_data/webos{n}/touch_simulator_update_webos_event{id}.py)를 통해 주입한다.
로컬에 스크립트 파일이 있으면(scripts_dir) async_connect 시 best-effort 업로드한다.

⚠️ ppadb 의존 제거: 모든 ADB I/O 는 번들 adb(adb_path.resolve_adb_path) subprocess 로 수행.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

from .adb_path import resolve_adb_path

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 캡처 임시파일은 시스템 임시폴더에 (프로젝트 오염 방지)
_TMP_DIR = Path(tempfile.gettempdir()) / "replaykit_bmw"

# screen_id ↔ WebOS lxc 컨테이너 (0=webos1, 1=webos2)
_DEFAULT_SCREEN_NAMES = {0: "rear_left", 1: "rear_right"}


def _encode_to(data: bytes, fmt: str) -> bytes:
    """이미지 바이트를 요청한 포맷(png/jpeg)으로 변환. 이미 일치하면 그대로 반환."""
    want = (fmt or "png").lower()
    if want in ("jpg", "jpeg"):
        want = "jpeg"
    # 시그니처로 현재 포맷 판별
    is_png = data[:8] == b"\x89PNG\r\n\x1a\n"
    is_jpeg = data[:2] == b"\xff\xd8"
    if (want == "png" and is_png) or (want == "jpeg" and is_jpeg):
        return data
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            buf = io.BytesIO()
            if want == "jpeg":
                im.convert("RGB").save(buf, format="JPEG", quality=85)
            else:
                im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as e:
        logger.debug("BMW image re-encode failed (%s), returning raw: %s", want, e)
        return data


class BMWAgentService:
    """ADB 기반 BMW 후석 듀얼 디스플레이 제어 서비스.

    DeviceManager/playback_service 가 일반 ADB 디바이스와 동일한 generic 스텝
    (tap/swipe/long_press/repeat_tap/screenshot)을 디스패치할 수 있도록
    async API surface 를 제공한다.
    """

    default_screen = "rear_left"

    def __init__(
        self,
        serial: str,
        host: str = "127.0.0.1",
        port: int = 5037,
        device_id: str = "",
        resolution: str = "1920x1080",
        capture_backend: str = "adb",
        scripts_dir: Optional[str] = None,
    ) -> None:
        self.serial = serial
        self.host = host
        self.port = int(port or 5037)
        self.device_id = device_id or f"BMW_{serial}"
        self.capture_backend = (capture_backend or "adb").strip().lower()
        if self.capture_backend not in ("adb", "webos"):
            self.capture_backend = "adb"
        self.scripts_dir = scripts_dir
        self.agent_version = "BMWRSE Agent"
        self._connected = False
        # 입력(터치) 시각 — 라이브 미러 적응형 리프레시용 (main.py _adaptive_*_pace 호환)
        self.last_input_ts = 0.0
        # screen_id → (width, height). connect 시 채워지며 실패 시 기본 해상도.
        try:
            rw, rh = str(resolution).upper().split("X")
            self._default_res = (int(rw), int(rh))
        except Exception:
            self._default_res = (1920, 1080)
        self._screen_sizes: dict[int, Tuple[int, int]] = {}
        try:
            _TMP_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    def _screen_id(self, screen_type) -> int:
        """프론트 screen_type("0"/"1"/이름) → screen_id(0/1). 미지정/오류=0."""
        if screen_type is None or screen_type == "":
            return 0
        if isinstance(screen_type, int):
            return 1 if screen_type == 1 else 0
        s = str(screen_type).strip().lower()
        if s in ("1", "rear_right", "right", "r"):
            return 1
        if s in ("0", "rear_left", "left", "l"):
            return 0
        try:
            return 1 if int(s) == 1 else 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Low-level ADB (bundled adb subprocess; ppadb 비의존)
    # ------------------------------------------------------------------
    def _adb(self, args: List[str], *, text: bool = True,
             timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        cmd = [resolve_adb_path(), "-s", self.serial] + args
        return subprocess.run(
            cmd, capture_output=True, text=text, encoding="utf-8" if text else None,
            errors="replace" if text else None, creationflags=_NO_WINDOW, timeout=timeout,
        )

    def _adb_shell(self, shell_cmd: str, *, text: bool = True,
                   timeout: Optional[float] = None) -> subprocess.CompletedProcess:
        return self._adb(["shell", shell_cmd], text=text, timeout=timeout)

    def _adb_root(self) -> None:
        try:
            self._adb(["root"], timeout=10)
            time.sleep(0.4)
        except Exception as e:
            logger.debug("BMW adb root failed (ignored): %s", e)

    # ------------------------------------------------------------------
    # Display 조회 / 해상도
    # ------------------------------------------------------------------
    def _get_display_ids(self) -> List[Tuple[str, ...]]:
        """SurfaceFlinger --display-id 파싱 → [(disp, hwc, port, pnpId, name), ...]."""
        self._adb_root()
        remote_file = "/tmp/bmw_sf_display.txt"
        gen_cmd = f"lxc-attach -n android1 -- dumpsys SurfaceFlinger --display-id > {remote_file}"
        try:
            self._adb_shell(gen_cmd, timeout=15)
            r = self._adb_shell(f"cat {remote_file}", timeout=10)
            output = (r.stdout or "").strip()
            if not output or "No such file" in output:
                direct = ("lxc-attach -n android1 -- /system/bin/sh -c "
                          "'dumpsys SurfaceFlinger --display-id'")
                r = self._adb_shell(direct, timeout=15)
                output = (r.stdout or "").strip()
            self._adb_shell(f"rm -f {remote_file}", timeout=5)
        except Exception as e:
            logger.debug("BMW _get_display_ids failed: %s", e)
            return []
        pattern = r'Display (\d+) \(HWC display (\d+)\): port=(\d+) pnpId=(\w+) displayName="(.+?)"'
        return re.findall(pattern, output)

    def _get_screen_size(self, screen_id: int) -> Tuple[int, int]:
        """WebOS 컨테이너 DRM modes 파싱으로 해상도 조회. 실패 시 기본 해상도."""
        self._adb_root()
        remote_file = "/tmp/bmw_screen_size.txt"
        container = f"webos{int(screen_id) + 1}"
        try:
            self._adb_shell(f"rm -f {remote_file}", timeout=5)
            gen_cmd = (f"lxc-attach -n {container} -- sh -c "
                       f"'cat /sys/class/drm/*/modes' > {remote_file}")
            self._adb_shell(gen_cmd, timeout=10)
            r = self._adb_shell(f"cat {remote_file}", timeout=10)
            output = (r.stdout or "").strip()
            self._adb_shell(f"rm -f {remote_file}", timeout=5)
            if output and "No such file" not in output:
                m = re.search(r'(\d+)x(\d+)', output)
                if m:
                    return int(m.group(1)), int(m.group(2))
        except Exception as e:
            logger.debug("BMW _get_screen_size(%s) failed: %s", screen_id, e)
        return self._default_res

    # ------------------------------------------------------------------
    # 캡처
    # ------------------------------------------------------------------
    def _screencap_adb(self, screen_id: int) -> bytes:
        """android1 컨테이너 screencap -d <display_id> → PNG 바이트."""
        self._adb_root()
        try:
            self._adb_shell("setenforce 0", timeout=5)
        except Exception:
            pass
        display_id: Optional[str] = None
        try:
            ids = self._get_display_ids()
            if ids and int(screen_id) < len(ids):
                display_id = ids[int(screen_id)][0]
        except Exception as e:
            logger.debug("BMW screencap: display id lookup failed: %s", e)

        remote_temp = f"/tmp/bmw_cap_{screen_id}.png"
        if display_id is not None:
            cap_cmd = f"lxc-attach -n android1 -- screencap -d {display_id} -p > {remote_temp}"
            r = self._adb_shell(cap_cmd, text=False, timeout=20)
        else:
            r = self._adb(["shell", "screencap", "-p", remote_temp], text=False, timeout=20)
        if r.returncode != 0:
            raise RuntimeError(f"BMW screencap 실패 (screen {screen_id})")

        local = str(_TMP_DIR / f"bmw_adb_{screen_id}_{uuid.uuid4().hex[:8]}.png")
        try:
            pull = self._adb(["pull", remote_temp, local], timeout=20)
            if pull.returncode != 0:
                raise RuntimeError(f"BMW adb pull 실패: {pull.stderr}")
            with open(local, "rb") as f:
                return f.read()
        finally:
            self._adb_shell(f"rm -f {remote_temp}", timeout=5)
            try:
                os.remove(local)
            except OSError:
                pass

    def _screencap_webos(self, screen_id: int) -> bytes:
        """WebOS luna-send captureCompositorOutput → JPG 바이트."""
        container = f"webos{int(screen_id) + 1}"
        remote_host_path = f"/log_data/{container}/screenshot.JPG"
        try:
            self._adb_shell(f"rm -f {remote_host_path}", timeout=5)
        except Exception:
            pass
        payload = r'{"output":"/tmp/screenshot.JPG","format":"JPG"}'
        capture_cmd = (
            f"lxc-attach -n {container} -- "
            f"luna-send -n 1 -f luna://com.webos.surfacemanager/captureCompositorOutput "
            f"'{payload}'"
        )
        self._adb_shell(capture_cmd, timeout=15)
        # 컴포지터가 비동기로 파일을 쓰므로 잠시 대기 (standalone 은 2.0s 고정)
        time.sleep(1.2)
        copy_cmd = f"lxc-attach -n {container} -- cp /tmp/screenshot.JPG /var/log/screenshot.JPG"
        self._adb_shell(copy_cmd, timeout=10)
        local = str(_TMP_DIR / f"bmw_webos_{screen_id}_{uuid.uuid4().hex[:8]}.jpg")
        try:
            pull = self._adb(["pull", remote_host_path, local], timeout=20)
            if pull.returncode != 0:
                raise RuntimeError(f"WebOS 캡처 pull 실패: {pull.stderr}")
            with open(local, "rb") as f:
                return f.read()
        finally:
            try:
                os.remove(local)
            except OSError:
                pass

    def _capture(self, screen_id: int, backend: Optional[str] = None) -> bytes:
        be = (backend or self.capture_backend or "adb").lower()
        if be == "webos":
            try:
                return self._screencap_webos(screen_id)
            except Exception as e:
                logger.warning("BMW WebOS capture failed (screen %s), falling back to adb: %s",
                               screen_id, e)
                return self._screencap_adb(screen_id)
        return self._screencap_adb(screen_id)

    # ------------------------------------------------------------------
    # 입력 (touch simulator 스크립트 경유)
    # ------------------------------------------------------------------
    def _input_script(self, screen_id: int) -> str:
        return (f"/log_data/webos{int(screen_id) + 1}/"
                f"touch_simulator_update_webos_event{screen_id}.py")

    def _run_input(self, sub_args: str, screen_id: int, success_marker: str) -> str:
        """webos/infotainment/android1/host 컨테이너 순으로 입력 스크립트 실행."""
        script = self._input_script(screen_id)
        containers: List[Optional[str]] = [
            f"webos{int(screen_id) + 1}", "infotainment", "android1", None,
        ]
        last_err = ""
        for container in containers:
            if container:
                cmd = f"lxc-attach -n {container} -- python3 {script} {sub_args}"
            else:
                cmd = f"python3 {script} {sub_args}"
            try:
                r = self._adb_shell(cmd, timeout=15)
                out = (r.stdout or "").strip()
                if out and success_marker in out:
                    self.last_input_ts = time.monotonic()
                    return out
                last_err = out or (r.stderr or "").strip()
            except Exception as e:
                last_err = str(e)
        raise RuntimeError(
            f"BMW input 실패 (screen {screen_id}, args={sub_args!r}): {last_err[:200]}")

    def _touch(self, x: int, y: int, screen_id: int) -> str:
        return self._run_input(f"tap {int(x)} {int(y)}", screen_id, "TAP at")

    def _swipe(self, x1: int, y1: int, x2: int, y2: int, screen_id: int) -> str:
        return self._run_input(
            f"swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)}", screen_id, "SWIPE from")

    # ------------------------------------------------------------------
    # Touch 스크립트 업로드 (best-effort)
    # ------------------------------------------------------------------
    def _upload_touch_scripts(self) -> None:
        """로컬에 스크립트 파일이 있으면 디바이스로 업로드. 없으면 조용히 skip
        (디바이스에 사전 배포된 스크립트를 사용)."""
        candidates: list[str] = []
        if self.scripts_dir:
            candidates.append(self.scripts_dir)
        env_dir = os.environ.get("BMW_TOUCH_SCRIPTS_DIR")
        if env_dir:
            candidates.append(env_dir)
        candidates.append(str(Path(__file__).resolve().parent / "bmw_touch_scripts"))

        scripts = [
            ("touch_simulator_update_webos_event0.py",
             "/log_data/webos1/touch_simulator_update_webos_event0.py"),
            ("touch_simulator_update_webos_event1.py",
             "/log_data/webos2/touch_simulator_update_webos_event1.py"),
        ]
        for filename, remote_path in scripts:
            local_path: Optional[str] = None
            for d in candidates:
                p = os.path.join(d, filename)
                if os.path.exists(p):
                    local_path = p
                    break
            if local_path is None:
                logger.debug("BMW touch script not found locally, skip upload: %s", filename)
                continue
            remote_dir = os.path.dirname(remote_path)
            try:
                self._adb_shell(f"mkdir -p {remote_dir} && chmod 755 {remote_dir}", timeout=10)
                self._adb(["push", local_path, remote_path], timeout=20)
                self._adb_shell(f"chmod 755 {remote_path}", timeout=5)
                logger.info("BMW uploaded touch script: %s → %s", local_path, remote_path)
            except Exception as e:
                logger.warning("BMW touch script upload failed (%s): %s", filename, e)

    # ------------------------------------------------------------------
    # Connect / info
    # ------------------------------------------------------------------
    def _connect_sync(self) -> bool:
        # serial 이 adb devices 에 보이는지 확인
        try:
            r = self._adb(["get-state"], timeout=10)
            state = (r.stdout or "").strip()
        except Exception as e:
            logger.warning("BMW connect: adb get-state failed for %s: %s", self.serial, e)
            return False
        if state != "device":
            logger.warning("BMW connect: device %s not ready (state=%s)", self.serial, state)
            return False
        self._adb_root()
        # 터치 스크립트 best-effort 업로드
        try:
            self._upload_touch_scripts()
        except Exception as e:
            logger.debug("BMW touch script upload skipped: %s", e)
        # 해상도 조회 (실패해도 연결은 유지)
        for sid in (0, 1):
            try:
                self._screen_sizes[sid] = self._get_screen_size(sid)
            except Exception:
                self._screen_sizes[sid] = self._default_res
        return True

    async def async_connect(self) -> bool:
        loop = asyncio.get_event_loop()
        try:
            ok = await loop.run_in_executor(None, self._connect_sync)
            self._connected = bool(ok)
            if ok:
                logger.info("BMWAgentService(%s): connected (backend=%s)",
                            self.serial, self.capture_backend)
            return self._connected
        except Exception as e:
            logger.warning("BMWAgentService(%s): connect failed: %s", self.serial, e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._connected = False
        logger.debug("BMWAgentService(%s): disconnected", self.serial)

    def get_info(self) -> dict:
        """프론트(ADB 멀티 디스플레이 취급)용 displays + screens 반환."""
        names = _DEFAULT_SCREEN_NAMES
        displays: list[dict] = []
        screens: dict = {}
        for sid in (0, 1):
            w, h = self._screen_sizes.get(sid, self._default_res)
            name = names[sid]
            displays.append({
                "id": sid,
                "sf_id": str(sid),
                "logical_id": sid,
                "is_active": True,
                "name": name,
                "width": w,
                "height": h,
            })
            screens[name] = {"width": w, "height": h}
        return {
            "agent": "BMWRSE",
            "serial": self.serial,
            "displays": displays,
            "screens": screens,
            "default_screen": self.default_screen,
            "capture_backend": self.capture_backend,
        }

    def detect_resolution(self, screen_id: int = 0) -> Tuple[int, int]:
        w, h = self._get_screen_size(screen_id)
        self._screen_sizes[screen_id] = (w, h)
        return w, h

    # ------------------------------------------------------------------
    # Async API surface (DeviceManager/playback 호환)
    # ------------------------------------------------------------------
    async def async_screencap_bytes(self, screen_type=None, fmt: str = "png",
                                    timeout: float = 0.0) -> bytes:
        loop = asyncio.get_event_loop()
        sid = self._screen_id(screen_type)
        data = await loop.run_in_executor(None, self._capture, sid, None)
        return _encode_to(data, fmt)

    async def async_tap(self, x: int, y: int, screen_type=None) -> None:
        loop = asyncio.get_event_loop()
        sid = self._screen_id(screen_type)
        await loop.run_in_executor(None, self._touch, x, y, sid)

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type=None, duration_ms: int = 0) -> None:
        loop = asyncio.get_event_loop()
        sid = self._screen_id(screen_type)
        await loop.run_in_executor(None, self._swipe, x1, y1, x2, y2, sid)

    async def async_long_press(self, x: int, y: int, duration_ms: int = 1000,
                               screen_type=None) -> None:
        """touch simulator 스크립트가 long-press 미지원이라 동일점 swipe 로 근사.
        실패 시 단순 tap 으로 폴백."""
        loop = asyncio.get_event_loop()
        sid = self._screen_id(screen_type)

        def _do() -> None:
            try:
                self._swipe(x, y, x, y, sid)
            except Exception:
                self._touch(x, y, sid)

        await loop.run_in_executor(None, _do)

    async def async_repeat_tap(self, x: int, y: int, count: int = 5,
                               interval_ms: int = 100, screen_type=None) -> None:
        loop = asyncio.get_event_loop()
        sid = self._screen_id(screen_type)

        def _do() -> None:
            for i in range(max(1, int(count))):
                self._touch(x, y, sid)
                if i < count - 1 and interval_ms > 0:
                    time.sleep(interval_ms / 1000.0)

        await loop.run_in_executor(None, _do)
