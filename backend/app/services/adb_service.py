"""ADB Service — Android Debug Bridge 연결 관리 및 명령 실행."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# 주의: build_dist.py가 배포 시 모든 __init__.py를 빈 파일로 덮어쓰므로
# `from .capture import ...` 형태는 ImportError를 일으킨다. 반드시 서브모듈을
# 직접 명시해서 import해야 .pyd 컴파일 배포본에서도 정상 동작한다.
from .capture.scrcpy_server import (
    ScrcpyServerBackend, detect_scrcpy_server, SCRCPY_V1, SCRCPY_V3,
)
from .adb_path import resolve_adb_path

logger = logging.getLogger(__name__)

# 전 PC 동일 adb 보장 — 번들 tools/platform-tools/adb 우선, 미배치 시 PATH 'adb' 폴백.
# (adb 서버 포트는 기본 5037 공유 — 격리 시 USB 디바이스 경합으로 스캔 실패)
ADB_PATH = resolve_adb_path()


def resolve_sf_display_id(dev_info: dict | None, logical_id: int | None) -> str | None:
    """우리 displays 배열 인덱스 → SurfaceFlinger display ID(`screencap -d`용) 변환.

    dev_info: ManagedDevice.info dict (displays 리스트 포함).
    SF display ID를 찾지 못하면 logical_id를 문자열로 폴백 반환.
    멀티 디스플레이에서 logical_id=None이면 display 0의 sf_id 반환.
    """
    if not dev_info:
        return None
    displays = dev_info.get("displays", [])
    is_multi = len(displays) > 1
    # logical_id가 None이고 멀티 디스플레이면 display 0 사용
    if logical_id is None:
        if is_multi and displays:
            return displays[0].get("sf_id")
        return None
    for d in displays:
        if d.get("id") == logical_id:
            sf_id = d.get("sf_id")
            if sf_id is not None:
                return sf_id
    # SF display ID를 찾지 못한 경우 logical ID를 직접 사용 (display 0 제외)
    if logical_id and logical_id != 0:
        logger.warning("SF display ID not found for logical_id=%d, using logical ID as fallback", logical_id)
        return str(logical_id)
    return None


def resolve_input_display_id(dev_info: dict | None, our_index: int | None) -> int | None:
    """우리 displays 배열 인덱스 → Android DisplayManager logical ID(`input -d`용).

    `screencap -d`(SurfaceFlinger uniqueId)와 `input -d`(DisplayManager logical ID)는
    서로 다른 ID 체계를 쓴다. 폴더블처럼 두 internal display가 있는 기기에서는
    dumpsys 파싱 순서와 Android logical ID가 어긋날 수 있어, 우리 배열 인덱스를
    그대로 `input -d`에 넘기면 엉뚱한 디스플레이로 이벤트가 간다.

    dev_info: ManagedDevice.info dict (displays 리스트 포함).
    our_index: 프론트엔드가 보낸 screen_type을 int 변환한 값(우리 배열의 id).
    찾지 못하면 our_index를 그대로 반환(단일 디스플레이 폴백).
    """
    if dev_info is None or our_index is None:
        return our_index
    displays = dev_info.get("displays", [])
    for d in displays:
        if d.get("id") == our_index:
            android_id = d.get("logical_id")
            if android_id is not None:
                return int(android_id)
            break
    return our_index


_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _run_sync(cmd: str, timeout: int = 10) -> tuple[str, str, int]:
    """Run a command synchronously and return (stdout, stderr, returncode)."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return (
            proc.stdout.decode(errors="replace"),
            proc.stderr.decode(errors="replace"),
            proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return ("", f"Command timed out after {timeout}s: {cmd}", 1)


def _run_sync_bytes(cmd: str, timeout: int = 10) -> tuple[bytes, str, int]:
    """Run a command synchronously and return raw stdout bytes."""
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
        )
        return (
            proc.stdout,
            proc.stderr.decode(errors="replace"),
            proc.returncode,
        )
    except subprocess.TimeoutExpired:
        return (b"", f"Command timed out after {timeout}s: {cmd}", 1)


class ADBDevice:
    """Represents a single connected ADB device."""

    def __init__(self, serial: str, status: str, model: str = ""):
        self.serial = serial
        self.status = status
        self.model = model

    def to_dict(self) -> dict:
        return {
            "serial": self.serial,
            "status": self.status,
            "model": self.model,
        }


class ADBService:
    """Manages ADB connections and command execution."""

    def __init__(self):
        self._active_serial: Optional[str] = None
        self._touch_device_cache: dict[str, tuple[str, int, int]] = {}  # serial → (dev_path, max_x, max_y)
        self._display_size_cache: dict[str, tuple[int, int]] = {}  # serial → (width, height)
        self._sendevent_mode: dict[str, str] = {}  # serial → "direct" | "su" | "none"
        self._gvm_container: dict[str, str | None] = {}  # serial → container name or None
        # 장기 screencap 세션 (serial|sf_display_id → streamer) — 연결 시 선제 생성
        self._streamers: dict[str, "AdbScreencapStreamer"] = {}
        self._streamer_lock = asyncio.Lock()
        # 1순위 라이브 미러링: scrcpy-server. 미지원/실패 시 screencap PNG 폴링으로 폴백.
        self._scrcpy_backends: dict[str, ScrcpyServerBackend] = {}
        self._scrcpy_lock = asyncio.Lock()
        # scrcpy 사용 불가 디바이스 캐시 — N회 연속 실패 시 영구 disable.
        # 일시적인 push/forward 실패와 진짜 미지원 디바이스를 구분하기 위해 카운터 기반.
        self._scrcpy_disabled: set[str] = set()
        self._scrcpy_failure_count: dict[str, int] = {}
        # scrcpy 재시도 쿨다운(serial → event-loop time). WS 세션 로컬이 아니라 serial
        # 단위로 두어, 프론트 재연결로 WS 세션이 새로 떠도 쿨다운이 리셋되지 않게 한다
        # (리셋되면 재연결마다 즉시 try_start → app_process 반복 spawn → OOM).
        self._scrcpy_retry_after: dict[str, float] = {}
        # 디바이스 Android SDK 캐시 (scrcpy 버전 선택용). SDK 는 변하지 않으므로 1회 조회.
        self._sdk_cache: dict[str, Optional[int]] = {}
        # scrcpy 가 한 번이라도 성공한 serial — "scrcpy 가능 기기". 이 기기는 일시적
        # 스트림 끊김/재시작이 있어도 영구 disable 하지 않고 짧은 쿨다운으로 즉시 scrcpy
        # 로 복귀한다 (장기 screencap 폴링으로 눌러앉지 않게 — 사용자 요구).
        self._scrcpy_capable: set[str] = set()
        # idle reaper — WS 종료(장치 전환)만으로는 백엔드를 닫지 않고(정식 scrcpy 처럼
        # 연결 유지) 살려두되, 일정 시간 아무 WS 도 소비하지 않는 백엔드만 닫아 누수/불필요
        # 인코더 점유를 막는다. 장치 분리 정리는 device_manager 가 별도로 담당.
        self._scrcpy_reaper_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Device management
    # ------------------------------------------------------------------

    async def list_devices(self) -> list[ADBDevice]:
        """List connected ADB devices."""
        output = await self._run("devices -l")
        devices: list[ADBDevice] = []
        for line in output.strip().splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) < 2:
                continue
            serial = parts[0]
            status = parts[1]
            model_match = re.search(r"model:(\S+)", line)
            model = model_match.group(1) if model_match else ""
            devices.append(ADBDevice(serial=serial, status=status, model=model))
        return devices

    async def restart_server(self) -> None:
        """Kill and restart the ADB server to recover stuck devices."""
        logger.info("Restarting ADB server (kill-server && start-server)")
        await self._run("kill-server")
        await self._run("start-server")
        logger.info("ADB server restarted")

    async def connect_device(self, address: str) -> str:
        """Connect to a device via 'adb connect <address>'."""
        return await self._run(f"connect {address}")

    async def disconnect_device(self, address: str) -> str:
        """Disconnect a device via 'adb disconnect <address>'."""
        return await self._run(f"disconnect {address}")

    async def get_active_device(self) -> Optional[str]:
        """Return the currently selected device serial."""
        return self._active_serial

    async def set_active_device(self, serial: str) -> bool:
        """Set the active device by serial number."""
        devices = await self.list_devices()
        serials = [d.serial for d in devices]
        if serial not in serials:
            return False
        self._active_serial = serial
        return True

    async def get_device_info(self, serial: Optional[str] = None) -> dict:
        """Get device properties."""
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        model = await self._run_device(s, "shell getprop ro.product.model")
        brand = await self._run_device(s, "shell getprop ro.product.brand")
        android_ver = await self._run_device(s, "shell getprop ro.build.version.release")
        resolution = await self._run_device(s, "shell wm size")
        # "Override size"가 있으면 스크린샷/터치가 이 해상도 기준이므로 우선 사용
        # 없으면 "Physical size" 사용
        override_match = re.search(r"Override size:\s*(\d+)x(\d+)", resolution)
        physical_match = re.search(r"Physical size:\s*(\d+)x(\d+)", resolution)
        res_match = override_match or physical_match
        width, height = (int(res_match.group(1)), int(res_match.group(2))) if res_match else (0, 0)
        # 디스플레이 목록 조회
        displays = await self.list_displays(s)
        # 멀티 디스플레이: 첫 번째 디스플레이의 해상도를 기본 해상도로 사용
        if displays and displays[0].get("width") and displays[0].get("height"):
            width = displays[0]["width"]
            height = displays[0]["height"]
        return {
            "serial": s,
            "model": model.strip(),
            "brand": brand.strip(),
            "android_version": android_ver.strip(),
            "resolution": {"width": width, "height": height},
            "displays": displays,
        }

    async def list_displays(self, serial: Optional[str] = None) -> list[dict]:
        """디바이스의 디스플레이 목록 조회 (SurfaceFlinger display ID 포함)."""
        s = serial or self._active_serial
        if not s:
            return []
        disp_output = await self._run_device(s, "shell dumpsys display")

        displays: list[dict] = []
        seen_sf_ids: set[str] = set()

        # 1) mViewports에서 논리 크기 + Android DisplayManager logical ID 추출
        # logicalFrame = 실제 터치/screencap 좌표계 (deviceWidth/Height는 물리 해상도)
        # displayId = `input -d`가 받는 Android logical ID
        # isActive = 현재 활성 여부 (폴더블에서 비활성 디스플레이는 input 무시)
        viewport_map: dict[str, dict] = {}  # uniqueId → {width, height, logical_id, is_active}
        for line in disp_output.split("\n"):
            if "DisplayViewport{" not in line:
                continue
            for vp_m in re.finditer(
                r"DisplayViewport\{([^}]*?uniqueId='local:(\d+)'[^}]*)\}",
                line
            ):
                inner = vp_m.group(1)
                sf = vp_m.group(2)
                lf_m = re.search(r"logicalFrame=Rect\(\d+,\s*\d+\s*-\s*(\d+),\s*(\d+)\)", inner)
                if not lf_m:
                    continue
                did_m = re.search(r"displayId=(\d+)", inner)
                act_m = re.search(r"isActive=(true|false)", inner)
                viewport_map[sf] = {
                    "width": int(lf_m.group(1)),
                    "height": int(lf_m.group(2)),
                    "logical_id": int(did_m.group(1)) if did_m else None,
                    "is_active": act_m.group(1) == "true" if act_m else None,
                }

        # 2) DisplayDeviceInfo 라인에서 SF ID, 해상도, 이름 추출
        for line in disp_output.split("\n"):
            if "DisplayDeviceInfo" not in line or 'uniqueId="local:' not in line:
                continue
            sf_m = re.search(r'uniqueId="local:(\d+)"', line)
            if not sf_m or sf_m.group(1) in seen_sf_ids:
                continue
            sf_id = sf_m.group(1)
            seen_sf_ids.add(sf_id)

            res_m = re.search(r"(\d{3,5})\s*x\s*(\d{3,5})", line)
            name_m = re.search(r"DeviceProductInfo\{name=(\S+?)[,}]", line)

            # 물리 해상도 (회전 전)
            phys_w = int(res_m.group(1)) if res_m else 0
            phys_h = int(res_m.group(2)) if res_m else 0
            name = name_m.group(1) if name_m else f"Display {len(displays)}"

            # viewport에서 논리 크기(회전 적용) 가져오기, 없으면 물리 크기 사용
            vp = viewport_map.get(sf_id) or {}
            w = vp.get("width") or phys_w
            h = vp.get("height") or phys_h

            displays.append({
                "id": len(displays),
                "sf_id": sf_id,                       # screencap -d 용 (uniqueId 숫자)
                "logical_id": vp.get("logical_id"),   # input -d 용 (Android DisplayManager ID)
                "is_active": vp.get("is_active"),     # 비활성이면 input 무시됨
                "name": name,
                "width": w,
                "height": h,
            })

        # 파싱 실패 시 기본 디스플레이
        if not displays:
            displays.append({
                "id": 0, "sf_id": None, "logical_id": None,
                "is_active": None, "name": "Default",
            })

        return displays

    # ------------------------------------------------------------------
    # Input commands
    # ------------------------------------------------------------------

    def _display_flag(self, display_id: Optional[int]) -> str:
        """display_id가 지정된 경우 -d 플래그 반환 (멀티 디스플레이에서 display 0도 명시)."""
        if display_id is not None:
            return f"-d {display_id} "
        return ""

    async def tap(self, x: int, y: int, serial: Optional[str] = None, display_id: Optional[int] = None) -> str:
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = self._display_flag(display_id)
        return await self._run_device(s, f"shell input {dflag}tap {x} {y}")

    async def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100,
                         serial: Optional[str] = None, display_id: Optional[int] = None) -> str:
        """단일 shell 세션에서 연속 탭 — 프로세스 생성 오버헤드 없음."""
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = self._display_flag(display_id)
        # 따옴표로 감싸서 &&가 Android shell 안에서 실행되도록 함
        sleep_sec = interval_ms / 1000.0
        tap_cmd = f"input {dflag}tap {x} {y}"
        parts = []
        for i in range(count):
            parts.append(tap_cmd)
            if i < count - 1 and sleep_sec > 0:
                parts.append(f"sleep {sleep_sec:.3f}")
        cmd = 'shell "' + " && ".join(parts) + '"'
        return await self._run_device(s, cmd, timeout=max(10, count * (sleep_sec + 1)))

    async def swipe(
        self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
        serial: Optional[str] = None, display_id: Optional[int] = None,
        hold_ms: int = 0,
    ) -> str:
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = self._display_flag(display_id)
        if hold_ms and hold_ms > 0:
            # 드래그앤드롭(앱카드 이동): 시작점을 길게 눌러 "집어 올린" 뒤 이동.
            # `input draganddrop`의 duration 인자는 **이동(드래그) 시간**이며, pickup용
            # long-press 대기는 명령이 시작 시 자체적으로 수행한다. 따라서 hold_ms를
            # 이동 duration에 더하면 안 된다(더하면 드래그가 그만큼 느려짐 — 회귀 원인).
            # 사용자가 빠르게 끈 이동 속도를 보존하려면 duration_ms(=이동시간)만 사용.
            move_ms = max(int(duration_ms or 0), 150)
            out = await self._run_device(
                s, f"shell input {dflag}draganddrop {x1} {y1} {x2} {y2} {move_ms}"
            )
            low = (out or "").lower()
            # 구버전 빌드는 draganddrop 미지원 → swipe로 폴백. swipe는 자체 pickup이 없어
            # duration이 짧으면 런처가 드래그로 인식 못 하므로, 폴백에서만 hold+이동으로
            # 충분히 길게 잡아 pickup을 유발한다(이동이 느려지지만 폴백 한정).
            if "unknown command" in low or "error" in low or "not found" in low:
                total = max(int(hold_ms) + move_ms, 1000)
                return await self._run_device(
                    s, f"shell input {dflag}swipe {x1} {y1} {x2} {y2} {total}"
                )
            return out
        return await self._run_device(s, f"shell input {dflag}swipe {x1} {y1} {x2} {y2} {duration_ms}")

    async def _probe_sendevent_mode(self, serial: str) -> str:
        """sendevent 권한 모드를 탐지하고 캐시에 저장. 'direct'|'su'|'none' 반환.
        실제 터치 입력은 일으키지 않는 0-byte SYN 만으로 권한 확인."""
        cached = self._sendevent_mode.get(serial)
        if cached is not None:
            return cached
        touch = await self._find_touch_device(serial)
        if not touch:
            self._sendevent_mode[serial] = "none"
            return "none"
        loop = asyncio.get_event_loop()
        dev = touch[0]
        test_cmd = f'{ADB_PATH} -s {serial} shell "sendevent {dev} 0 0 0"'
        _, test_err, test_rc = await loop.run_in_executor(None, functools.partial(_run_sync, test_cmd, 3))
        if test_rc == 0 and "Permission denied" not in test_err:
            self._sendevent_mode[serial] = "direct"
            return "direct"
        test_su = f'{ADB_PATH} -s {serial} shell "su 0 sendevent {dev} 0 0 0"'
        _, test_err, test_rc = await loop.run_in_executor(None, functools.partial(_run_sync, test_su, 3))
        if test_rc == 0 and "not found" not in test_err and "Permission denied" not in test_err:
            self._sendevent_mode[serial] = "su"
            return "su"
        self._sendevent_mode[serial] = "none"
        return "none"

    async def prewarm_touch_input(self, serial: str) -> None:
        """디바이스 연결 직후 백그라운드에서 호출 — touch device 경로와 sendevent 권한을
        미리 캐시에 적재해서 첫 pattern_swipe 시 ADB shell 점유로 인한 화면 멈춤을 방지."""
        try:
            await self._find_touch_device(serial)
            await self._probe_sendevent_mode(serial)
            logger.info("touch input prewarmed: serial=%s mode=%s", serial, self._sendevent_mode.get(serial))
        except Exception as e:
            logger.debug("touch input prewarm failed for %s: %s", serial, e)

    async def pattern_swipe(
        self, points: list[dict], duration_ms: int = 600,
        serial: Optional[str] = None, display_id: Optional[int] = None,
    ) -> str:
        """다구간(waypoint) 연속 스와이프. L자/Z자 등 손가락을 떼지 않는 패턴 입력.

        points: [{"x": int, "y": int}, ...] — 최소 2개.
        sendevent 기반(권한 자동 폴백). sendevent 사용 불가시 input swipe로 구간별 분할
        실행하지만 손가락이 떼지므로 진정한 연속 입력이 아니라는 점 주의.
        """
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        clean = [
            {"x": int(p.get("x", 0)), "y": int(p.get("y", 0))}
            for p in points
            if "x" in p and "y" in p
        ]
        if len(clean) < 2:
            raise ValueError("pattern_swipe requires at least 2 points")

        cached = await self._probe_sendevent_mode(s)

        if cached in ("direct", "su"):
            return await self._sendevent_pattern_raw(clean, duration_ms, s, su=(cached == "su"))

        # fallback: 구간 분할 input swipe (연속 아님)
        dflag = self._display_flag(display_id)
        cmds: list[str] = []
        per_segment = max(50, duration_ms // max(1, len(clean) - 1))
        for i in range(len(clean) - 1):
            a, b = clean[i], clean[i + 1]
            cmds.append(f"input {dflag}swipe {a['x']} {a['y']} {b['x']} {b['y']} {per_segment}")
        joined = " && ".join(cmds)
        return await self._run_device(s, f'shell "{joined}"')

    def _build_sendevent_pattern_cmd(
        self, points: list[dict], duration_ms: int,
        touch: tuple[str, int, int],
        display_size: tuple[int, int] = (0, 0),
    ) -> str:
        """다구간 연속 터치(single-finger) sendevent 시퀀스 생성.
        BTN_TOUCH DOWN → SLOT/TRACKING_ID/POSITION → 각 구간을 단계별로 MOVE → UP.
        """
        dev, max_x, max_y = touch
        dw, dh = display_size if display_size[0] > 0 else (max_x + 1, max_y + 1)

        def sx(x: float) -> int:
            return max(0, min(max_x, int(x * max_x / dw)))
        def sy(y: float) -> int:
            return max(0, min(max_y, int(y * max_y / dh)))

        SE = f"sendevent {dev}"
        cmds: list[str] = []

        # DOWN at points[0]
        cmds.append(f"{SE} 1 330 1")
        cmds += [
            f"{SE} 3 47 0",
            f"{SE} 3 57 0",
            f"{SE} 3 53 {sx(points[0]['x'])}",
            f"{SE} 3 54 {sy(points[0]['y'])}",
            f"{SE} 3 48 5",
        ]
        cmds.append(f"{SE} 0 0 0")

        # 각 segment 를 시간에 비례하여 분할
        total_len = 0.0
        seg_lens: list[float] = []
        for i in range(len(points) - 1):
            dx = points[i + 1]["x"] - points[i]["x"]
            dy = points[i + 1]["y"] - points[i]["y"]
            d = (dx * dx + dy * dy) ** 0.5
            seg_lens.append(d)
            total_len += d
        if total_len <= 0:
            total_len = 1.0

        # 전체 step 수 ~ duration / 30ms, 최소 6 최대 60
        total_steps = max(6, min(60, duration_ms // 25))
        sleep_s = duration_ms / 1000 / total_steps
        use_sleep = sleep_s > 0.02

        for i in range(len(points) - 1):
            seg_steps = max(2, int(round(total_steps * seg_lens[i] / total_len)))
            a, b = points[i], points[i + 1]
            for step in range(1, seg_steps + 1):
                t = step / seg_steps
                ix = a["x"] + (b["x"] - a["x"]) * t
                iy = a["y"] + (b["y"] - a["y"]) * t
                if use_sleep:
                    cmds.append(f"sleep {sleep_s:.3f}")
                cmds += [
                    f"{SE} 3 47 0",
                    f"{SE} 3 53 {sx(ix)}",
                    f"{SE} 3 54 {sy(iy)}",
                ]
                cmds.append(f"{SE} 0 0 0")

        # UP
        cmds.append("sleep 0.03")
        cmds += [f"{SE} 3 47 0", f"{SE} 3 57 -1"]
        cmds.append(f"{SE} 1 330 0")
        cmds.append(f"{SE} 0 0 0")
        return "\n".join(cmds)

    async def _sendevent_pattern_raw(
        self, points: list[dict], duration_ms: int, serial: str, su: bool = False,
    ) -> str:
        touch = await self._find_touch_device(serial)
        if not touch:
            return ""
        display_size = await self._get_display_size(serial)
        script = self._build_sendevent_pattern_cmd(points, duration_ms, touch, display_size)
        loop = asyncio.get_event_loop()
        timeout = max(15, duration_ms // 1000 + 10)

        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, newline="\n") as f:
            f.write(script)
            local_path = f.name
        try:
            push_cmd = f'{ADB_PATH} -s {serial} push "{local_path}" {self._MT_SCRIPT_REMOTE}'
            await loop.run_in_executor(None, functools.partial(_run_sync, push_cmd, 5))
            if su:
                adb_cmd = f'{ADB_PATH} -s {serial} shell "su 0 sh {self._MT_SCRIPT_REMOTE}"'
            else:
                adb_cmd = f'{ADB_PATH} -s {serial} shell "sh {self._MT_SCRIPT_REMOTE}"'
            stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync, adb_cmd, timeout))
            if rc != 0:
                logger.error("pattern sendevent failed: %s", stderr.strip())
            return stdout
        finally:
            Path(local_path).unlink(missing_ok=True)

    # ------------------------------------------------------------------
    # 멀티터치 (sendevent 기반)
    # ------------------------------------------------------------------

    async def _find_touch_device(self, serial: str) -> tuple[str, int, int] | None:
        """터치 입력 디바이스 경로와 좌표 범위 검출. 결과 캐시.
        Returns (dev_path, max_x, max_y) or None.
        """
        if serial in self._touch_device_cache:
            return self._touch_device_cache[serial]

        # getevent는 호스트 레벨 /dev/input/ 접근 → GVM 래핑 없이 직접 실행
        cmd = f"{ADB_PATH} -s {serial} shell getevent -lp"
        loop = asyncio.get_event_loop()
        output, _, _ = await loop.run_in_executor(None, functools.partial(_run_sync, cmd, 10))
        current_device: str | None = None
        devices: dict[str, dict[str, int]] = {}

        for line in output.splitlines():
            m = re.match(r"add device \d+:\s+(.+)", line)
            if m:
                current_device = m.group(1).strip()
                continue
            if not current_device:
                continue
            if "ABS_MT_POSITION_X" in line:
                m2 = re.search(r"max\s+(\d+)", line)
                if m2:
                    devices.setdefault(current_device, {})["max_x"] = int(m2.group(1))
            elif "ABS_MT_POSITION_Y" in line:
                m2 = re.search(r"max\s+(\d+)", line)
                if m2:
                    devices.setdefault(current_device, {})["max_y"] = int(m2.group(1))

        for path, info in devices.items():
            mx, my = info.get("max_x", 0), info.get("max_y", 0)
            if mx > 0 and my > 0:
                result = (path, mx, my)
                self._touch_device_cache[serial] = result
                logger.info("Touch device: %s max=(%d,%d)", path, mx, my)
                return result
        return None

    async def _get_display_size(self, serial: str) -> tuple[int, int]:
        """디스플레이 논리 해상도 (wm size 기준). 결과 캐시."""
        if serial in self._display_size_cache:
            return self._display_size_cache[serial]
        raw = await self._run_device(serial, "shell wm size")
        override = re.search(r"Override size:\s*(\d+)x(\d+)", raw)
        physical = re.search(r"Physical size:\s*(\d+)x(\d+)", raw)
        m = override or physical
        if m:
            result = (int(m.group(1)), int(m.group(2)))
            self._display_size_cache[serial] = result
            return result
        return 0, 0

    async def multi_finger_swipe(
        self, fingers: list[dict], duration_ms: int = 500,
        serial: Optional[str] = None, display_id: Optional[int] = None,
    ) -> str:
        """sendevent 기반 멀티핑거 스와이프 (진짜 멀티터치).

        fingers: [{"x1": .., "y1": .., "x2": .., "y2": ..}, ...]
        """
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        return await self._sendevent_multitouch(fingers, duration_ms, s)

    async def multi_finger_tap(
        self, points: list[dict], serial: Optional[str] = None, display_id: Optional[int] = None,
    ) -> str:
        """sendevent 기반 멀티핑거 탭.

        points: [{"x": .., "y": ..}, ...]
        """
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        fingers = [{"x1": p["x"], "y1": p["y"], "x2": p["x"], "y2": p["y"]} for p in points]
        return await self._sendevent_multitouch(fingers, 50, s)

    async def _sendevent_multitouch(
        self, fingers: list[dict], duration_ms: int, serial: str,
    ) -> str:
        """멀티터치 제스처 실행. 우선순위:
        1) sendevent direct (shell이 /dev/input 쓰기 가능한 경우)
        2) sendevent + su 0 (root 사용)
        3) parallel input swipe fallback (진짜 멀티터치 아님)
        """
        cached = self._sendevent_mode.get(serial)

        # 캐시된 모드 사용
        if cached == "direct":
            return await self._sendevent_raw(fingers, duration_ms, serial, su=False)
        if cached == "su":
            return await self._sendevent_raw(fingers, duration_ms, serial, su=True)
        if cached == "none":
            return await self._parallel_input_swipe(fingers, duration_ms, serial)

        # 최초 시도: direct → su 0 → fallback (권한 테스트 후 캐시)
        touch = await self._find_touch_device(serial)
        if touch:
            loop = asyncio.get_event_loop()
            dev = touch[0]
            # 1) direct 권한 테스트
            test_cmd = f'{ADB_PATH} -s {serial} shell "sendevent {dev} 0 0 0"'
            _, test_err, test_rc = await loop.run_in_executor(None, functools.partial(_run_sync, test_cmd, 3))
            if test_rc == 0 and "Permission denied" not in test_err:
                self._sendevent_mode[serial] = "direct"
                logger.info("multitouch mode: sendevent direct (device %s)", serial)
                return await self._sendevent_raw(fingers, duration_ms, serial, su=False)

            # 2) su 0 권한 테스트
            test_su = f'{ADB_PATH} -s {serial} shell "su 0 sendevent {dev} 0 0 0"'
            _, test_err, test_rc = await loop.run_in_executor(None, functools.partial(_run_sync, test_su, 3))
            if test_rc == 0 and "not found" not in test_err and "Permission denied" not in test_err:
                self._sendevent_mode[serial] = "su"
                logger.info("multitouch mode: sendevent su (device %s)", serial)
                return await self._sendevent_raw(fingers, duration_ms, serial, su=True)

        # 3) Fallback
        logger.warning("multitouch: no method available (device %s), using parallel input", serial)
        self._sendevent_mode[serial] = "none"
        return await self._parallel_input_swipe(fingers, duration_ms, serial)

    # ---- sendevent 기반 (커널 레벨) ----

    def _build_sendevent_cmd(
        self, fingers: list[dict], duration_ms: int, serial: str,
        touch: tuple[str, int, int],
        display_size: tuple[int, int] = (0, 0),
    ) -> str:
        """sendevent 명령 시퀀스 문자열 생성."""
        dev, max_x, max_y = touch
        # 디스플레이 좌표(Override 기준) → 터치 디바이스 좌표로 변환
        dw, dh = display_size if display_size[0] > 0 else (max_x + 1, max_y + 1)

        def sx(x: float) -> int:
            return max(0, min(max_x, int(x * max_x / dw)))
        def sy(y: float) -> int:
            return max(0, min(max_y, int(y * max_y / dh)))

        # 스텝 수 = duration 기반 동적 조절 (sendevent 오버헤드 고려)
        # 각 sendevent 호출 ~5-10ms 오버헤드, 핑거 수에 비례
        cmds_per_step = len(fingers) * 3 + 1  # slot+x+y per finger + SYN
        overhead_per_step_ms = cmds_per_step * 8  # ~8ms per sendevent
        effective_ms = max(50, duration_ms - overhead_per_step_ms * 5)
        steps = max(3, min(12, effective_ms // 30))  # 목표 ~30ms 간격

        cmds: list[str] = []
        SE = f"sendevent {dev}"

        # BTN_TOUCH down
        cmds.append(f"{SE} 1 330 1")

        for i, f in enumerate(fingers):
            cmds += [
                f"{SE} 3 47 {i}", f"{SE} 3 57 {i}",
                f"{SE} 3 53 {sx(f['x1'])}", f"{SE} 3 54 {sy(f['y1'])}",
                f"{SE} 3 48 5",
            ]
        cmds.append(f"{SE} 0 0 0")

        sleep_s = duration_ms / 1000 / steps
        use_sleep = sleep_s > 0.02  # sendevent 오버헤드만으로 충분하면 sleep 생략
        for step in range(1, steps + 1):
            t = step / steps
            if use_sleep:
                cmds.append(f"sleep {sleep_s:.3f}")
            for i, f in enumerate(fingers):
                ix = f["x1"] + (f["x2"] - f["x1"]) * t
                iy = f["y1"] + (f["y2"] - f["y1"]) * t
                cmds += [f"{SE} 3 47 {i}", f"{SE} 3 53 {sx(ix)}", f"{SE} 3 54 {sy(iy)}"]
            cmds.append(f"{SE} 0 0 0")

        # 릴리즈 전 대기
        cmds.append("sleep 0.03")
        for i in range(len(fingers)):
            cmds += [f"{SE} 3 47 {i}", f"{SE} 3 57 -1"]
        # BTN_TOUCH up
        cmds.append(f"{SE} 1 330 0")
        cmds.append(f"{SE} 0 0 0")

        return "\n".join(cmds)

    _MT_SCRIPT_REMOTE = "/data/local/tmp/_mt.sh"

    async def _sendevent_raw(
        self, fingers: list[dict], duration_ms: int, serial: str, su: bool = False,
    ) -> str:
        touch = await self._find_touch_device(serial)
        if not touch:
            return ""
        display_size = await self._get_display_size(serial)
        script = self._build_sendevent_cmd(fingers, duration_ms, serial, touch, display_size)
        loop = asyncio.get_event_loop()
        timeout = max(15, duration_ms // 1000 + 10)

        # 스크립트를 디바이스에 push하고 실행 (인라인보다 안정적, 프로세스 오버헤드 없음)
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sh", delete=False, newline="\n") as f:
            f.write(script)
            local_path = f.name
        try:
            push_cmd = f'{ADB_PATH} -s {serial} push "{local_path}" {self._MT_SCRIPT_REMOTE}'
            await loop.run_in_executor(None, functools.partial(_run_sync, push_cmd, 5))

            if su:
                adb_cmd = f'{ADB_PATH} -s {serial} shell "su 0 sh {self._MT_SCRIPT_REMOTE}"'
            else:
                adb_cmd = f'{ADB_PATH} -s {serial} shell "sh {self._MT_SCRIPT_REMOTE}"'
            stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync, adb_cmd, timeout))
            if rc != 0:
                logger.error("sendevent failed: %s", stderr.strip())
            return stdout
        finally:
            Path(local_path).unlink(missing_ok=True)

    async def _parallel_input_swipe(
        self, fingers: list[dict], duration_ms: int, serial: str,
    ) -> str:
        """Fallback: parallel input swipe (진짜 멀티터치 아님)."""
        is_tap = all(f.get("x1") == f.get("x2") and f.get("y1") == f.get("y2") for f in fingers)
        if is_tap:
            cmds = [f"input tap {f['x1']} {f['y1']}" for f in fingers]
        else:
            cmds = [f"input swipe {f['x1']} {f['y1']} {f['x2']} {f['y2']} {duration_ms}" for f in fingers]
        parallel = " & ".join(cmds) + " & wait"
        return await self._run_device(serial, f'shell "{parallel}"')

    async def long_press(self, x: int, y: int, duration_ms: int = 1000,
                         serial: Optional[str] = None, display_id: Optional[int] = None) -> str:
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = self._display_flag(display_id)
        return await self._run_device(s, f"shell input {dflag}swipe {x} {y} {x} {y} {duration_ms}")

    async def input_text(self, text: str, serial: Optional[str] = None, display_id: Optional[int] = None) -> str:
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        escaped = text.replace(" ", "%s").replace("&", "\\&").replace("<", "\\<").replace(">", "\\>")
        dflag = self._display_flag(display_id)
        return await self._run_device(s, f'shell input {dflag}text "{escaped}"')

    async def key_event(self, keycode: str, serial: Optional[str] = None, display_id: Optional[int] = None) -> str:
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = self._display_flag(display_id)
        return await self._run_device(s, f"shell input {dflag}keyevent {keycode}")

    async def run_shell_command(self, command: str, serial: Optional[str] = None) -> str:
        """Run an arbitrary adb command on the device."""
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        return await self._run_device(s, command)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    def _gvm_screencap_cmd(self, container: str | None, screencap_args: str) -> str:
        """screencap 명령에 GVM lxc-attach 래핑 적용."""
        if container:
            return f"lxc-attach -n {container} -- screencap {screencap_args}"
        return f"screencap {screencap_args}"

    async def screencap(self, save_path: str, serial: Optional[str] = None,
                        display_id: Optional[int] = None,
                        sf_display_id: Optional[str] = None) -> str:
        """Capture a screenshot and save as PNG.

        sf_display_id: SurfaceFlinger display ID (긴 숫자). 제공 시 -d 플래그로 사용.
        """
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)
        dflag = f"-d {sf_display_id} " if sf_display_id else ""
        container = await self._detect_gvm_container(s)
        sc = self._gvm_screencap_cmd(container, f"{dflag}-p")
        loop = asyncio.get_event_loop()
        # 먼저 exec-out 시도 (빠름)
        cmd = f'{ADB_PATH} -s {s} exec-out {sc} > "{save_path}"'
        stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd))
        # 깨진 PNG 확인 → 파일 경유 폴백
        try:
            with open(save_path, "rb") as f:
                header = f.read(4)
            if header != b'\x89PNG':
                raise ValueError("corrupted")
        except Exception:
            logger.debug("exec-out screencap corrupted, falling back to file method")
            remote_path = "/data/local/tmp/_rk_screencap.png"
            cmd_save = f'{ADB_PATH} -s {s} shell "{sc} {remote_path}"'
            await loop.run_in_executor(None, functools.partial(_run_sync, cmd_save))
            cmd_pull = f'{ADB_PATH} -s {s} pull {remote_path} "{save_path}"'
            _, stderr2, rc2 = await loop.run_in_executor(None, functools.partial(_run_sync, cmd_pull))
            if rc2 != 0:
                logger.error("screencap pull error: %s", stderr2)
        return save_path

    async def screencap_bytes(self, serial: Optional[str] = None, fmt: str = "png",
                              display_id: Optional[int] = None,
                              sf_display_id: Optional[str] = None) -> bytes:
        """Capture a screenshot and return image bytes (png or jpeg).

        sf_display_id: SurfaceFlinger display ID (긴 숫자). 제공 시 -d 플래그로 사용.
        """
        s = serial or self._active_serial
        if not s:
            raise ValueError("No device selected")
        dflag = f"-d {sf_display_id} " if sf_display_id else ""
        container = await self._detect_gvm_container(s)
        sc = self._gvm_screencap_cmd(container, f"{dflag}-p")

        # 먼저 exec-out (빠름) 시도
        cmd = f"{ADB_PATH} -s {s} exec-out {sc}"
        loop = asyncio.get_event_loop()
        stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync_bytes, cmd))

        # exec-out 실패 또는 깨진 PNG → 파일 경유 폴백 (멀티 디스플레이에서 안정적)
        if rc != 0 or (stdout and len(stdout) > 0 and stdout[:4] != b'\x89PNG'):
            logger.debug("exec-out screencap failed or corrupted, falling back to file method")
            remote_path = "/data/local/tmp/_rk_screencap.png"
            cmd_save = f'{ADB_PATH} -s {s} shell "{sc} {remote_path}"'
            _, stderr2, rc2 = await loop.run_in_executor(None, functools.partial(_run_sync, cmd_save))
            if rc2 == 0:
                cat_cmd = "cat" if not container else f"lxc-attach -n {container} -- cat"
                cmd_cat = f"{ADB_PATH} -s {s} exec-out {cat_cmd} {remote_path}"
                stdout, _, _ = await loop.run_in_executor(None, functools.partial(_run_sync_bytes, cmd_cat))
            else:
                raise RuntimeError(f"screencap failed: {stderr2}")
            # 파일 폴백의 cat 결과도 raw 바이너리라 동일 채널 손상 가능 — PNG 매직바이트
            # 재검증해 깨진 바이트를 그대로 흘려보내지 않는다(= 호출부의 "Cannot decode
            # screenshot" 대신 명확한 실패 → device.py는 빈 이미지 반환/재시도).
            if not stdout or stdout[:4] != b'\x89PNG':
                raise RuntimeError(
                    "screencap returned corrupted/non-PNG data (adb binary channel "
                    "corruption — check bundled adb version / USB cable)"
                )

        if fmt == "jpeg" and stdout:
            import cv2
            import numpy as np
            arr = np.frombuffer(stdout, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                _, jpeg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
                return jpeg.tobytes()
        return stdout

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _run(self, args: str) -> str:
        cmd = f"{ADB_PATH} {args}"
        logger.debug("ADB cmd: %s", cmd)
        loop = asyncio.get_event_loop()
        stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd))
        if rc != 0:
            # stderr가 ADB 도움말 dump면 매우 길어지므로 첫 줄만. 명령도 함께 출력해
            # 어떤 cmd가 거부됐는지 식별 가능하게 한다.
            err_short = (stderr.split("\n", 1)[0] if stderr else "").strip()
            logger.error("ADB error (args=%r): %s", args, err_short or stderr[:200])
        return stdout

    async def _detect_gvm_container(self, serial: str) -> str | None:
        """GVM 디바이스인지 감지. Android LXC 컨테이너 이름 반환, 비GVM이면 None."""
        if serial in self._gvm_container:
            return self._gvm_container[serial]

        # 일반 Android인지 확인: getprop 응답이 있으면 비GVM
        loop = asyncio.get_event_loop()
        cmd = f'{ADB_PATH} -s {serial} shell getprop ro.build.version.sdk'
        stdout, _, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd, 5))
        if rc == 0 and stdout.strip():
            self._gvm_container[serial] = None
            return None

        # lxc-ls로 컨테이너 목록 조회
        cmd = f'{ADB_PATH} -s {serial} shell lxc-ls'
        stdout, _, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd, 5))
        if rc != 0 or not stdout.strip():
            self._gvm_container[serial] = None
            return None

        # 각 컨테이너에서 Android 확인
        for container in stdout.strip().split():
            cmd = f'{ADB_PATH} -s {serial} shell "lxc-attach -n {container} -- getprop ro.build.version.sdk"'
            out, _, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd, 5))
            if rc == 0 and out.strip():
                self._gvm_container[serial] = container
                logger.info("GVM detected: device %s → container '%s'", serial, container)
                return container

        self._gvm_container[serial] = None
        return None

    def _wrap_shell_cmd(self, args: str, container: str | None) -> str:
        """GVM 컨테이너가 있으면 shell 명령을 lxc-attach로 래핑."""
        if not container:
            return args
        # "shell ..." → "shell lxc-attach -n {container} -- ..."
        # "exec-out ..." → "exec-out lxc-attach -n {container} -- ..."
        for prefix in ("shell ", "exec-out "):
            if args.startswith(prefix):
                inner = args[len(prefix):]
                # 따옴표 안의 명령이면 풀어서 래핑
                if inner.startswith('"') and inner.endswith('"'):
                    inner = inner[1:-1]
                return f'{prefix}"lxc-attach -n {container} -- {inner}"'
        return args

    async def _run_device(self, serial: str, args: str, timeout: int = 10) -> str:
        # GVM 컨테이너 감지 (shell/exec-out 명령만 래핑)
        if args.startswith("shell ") or args.startswith("exec-out "):
            container = await self._detect_gvm_container(serial)
            args = self._wrap_shell_cmd(args, container)
        cmd = f"{ADB_PATH} -s {serial} {args}"
        logger.debug("ADB cmd: %s", cmd)
        loop = asyncio.get_event_loop()
        stdout, stderr, rc = await loop.run_in_executor(None, functools.partial(_run_sync, cmd, timeout))
        if rc != 0:
            err_short = (stderr.split("\n", 1)[0] if stderr else "").strip()
            logger.error(
                "ADB error (device %s, args=%r): %s",
                serial, args, err_short or stderr[:200],
            )
        return stdout

    # ------------------------------------------------------------------
    # Long-lived screencap streamer (화면 미러링용)
    # ------------------------------------------------------------------

    async def ensure_streamer(self, serial: str,
                              sf_display_id: Optional[str] = None) -> "AdbScreencapStreamer":
        """해당 (serial, display) 조합의 streamer가 없으면 생성 + 세션 시작.
        이미 있고 살아있으면 재사용. 죽어있으면 재시작.
        """
        key = f"{serial}|{sf_display_id or ''}"
        async with self._streamer_lock:
            s = self._streamers.get(key)
            if s is None:
                container = await self._detect_gvm_container(serial)
                s = AdbScreencapStreamer(serial, sf_display_id, container)
                self._streamers[key] = s
        if not s.is_alive():
            await s.start()
        return s

    async def streaming_screencap_bytes(self, serial: str, fmt: str = "jpeg",
                                        sf_display_id: Optional[str] = None) -> bytes:
        """장기 adb shell 세션 기반 screencap.

        - capture + 네트워크 전송이 끝날 때까지 다음 capture가 호출되지 않도록 streamer 내부 lock으로 직렬화
          (여러 WebSocket이 동시에 호출해도 하나씩 처리 — "무턱대고" 발사 방지)
        - 실패 시 세션 재시작 1회 → 그래도 실패하면 기존 spawn 방식(screencap_bytes)으로 폴백
        """
        s = await self.ensure_streamer(serial, sf_display_id)
        png: Optional[bytes] = None
        for attempt in range(2):
            try:
                png = await s.capture_png()
                break
            except IOError as e:
                logger.warning("ADB streamer %s attempt %d failed: %s", serial, attempt + 1, e)
                await s.close()
                if attempt == 1:
                    key = f"{serial}|{sf_display_id or ''}"
                    async with self._streamer_lock:
                        self._streamers.pop(key, None)
                    logger.warning("ADB streamer 복구 실패 → spawn 방식 폴백: %s", serial)
                    return await self.screencap_bytes(serial=serial, fmt=fmt, sf_display_id=sf_display_id)
                try:
                    await s.start()
                except Exception:
                    pass

        if fmt == "jpeg" and png:
            try:
                import cv2
                import numpy as np
                arr = np.frombuffer(png, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    _, jpeg = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
                    return jpeg.tobytes()
            except Exception as e:
                logger.warning("JPEG encode failed, returning PNG: %s", e)
        return png or b""

    async def close_streamer(self, serial: str) -> None:
        """해당 serial의 모든 display streamer를 닫고 pool에서 제거."""
        async with self._streamer_lock:
            keys = [k for k in self._streamers if k.split("|", 1)[0] == serial]
            streamers = [self._streamers.pop(k) for k in keys]
        for s in streamers:
            try:
                await s.close()
                logger.info("ADB screen streamer closed: %s", serial)
            except Exception as e:
                logger.debug("streamer close error: %s", e)

    async def close_all_streamers(self) -> None:
        """셧다운용 — 모든 streamer 정리."""
        async with self._streamer_lock:
            streamers = list(self._streamers.values())
            self._streamers.clear()
        for s in streamers:
            try:
                await s.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # H.264 라이브 미러링 백엔드 (scrcpy-server)
    # ------------------------------------------------------------------

    # scrcpy 영구 disable 임계치 — 연속 N회 ensure 실패 시 디바이스 단위 차단.
    # 1회만으로 영구 disable하면 일시적 push/forward 실패에 너무 민감하다 (실제로
    # ADB 서버 reset, 디바이스 일시 busy 등으로 1~2회 실패가 종종 발생).
    SCRCPY_FAILURE_THRESHOLD = 3

    # idle reaper — 아무 WS 도 stream 을 소비하지 않는(보지 않는) 백엔드를 이 시간 후
    # 닫는다. 장치 전환 시 잠깐(이 시간 이내) 다른 기기를 보다 돌아오면 살아있는 스트림을
    # 즉시 재사용(재시작 갭 0). 그보다 오래 안 보면 인코더 점유를 풀어준다.
    SCRCPY_IDLE_REAP_SECONDS = 90.0
    SCRCPY_REAPER_INTERVAL = 15.0

    def _ensure_scrcpy_reaper(self) -> None:
        """idle reaper 백그라운드 태스크를 1회 기동(이벤트 루프 필요 — ensure 시점 호출)."""
        if self._scrcpy_reaper_task is None or self._scrcpy_reaper_task.done():
            self._scrcpy_reaper_task = asyncio.create_task(self._scrcpy_reaper_loop())

    async def _scrcpy_reaper_loop(self) -> None:
        """주기적으로 죽었거나 오래 소비되지 않은 scrcpy 백엔드를 닫는다."""
        while True:
            try:
                await asyncio.sleep(self.SCRCPY_REAPER_INTERVAL)
                now = asyncio.get_event_loop().time()
                async with self._scrcpy_lock:
                    victims = []
                    for serial, backend in list(self._scrcpy_backends.items()):
                        if not backend.is_alive():
                            victims.append((serial, backend, "dead"))
                        elif backend.idle_seconds(now) > self.SCRCPY_IDLE_REAP_SECONDS:
                            victims.append((serial, backend, "idle"))
                    for serial, _, _ in victims:
                        self._scrcpy_backends.pop(serial, None)
                for serial, backend, why in victims:
                    logger.info(
                        "scrcpy backend reaped (%s): serial=%s idle=%.0fs",
                        why, serial, backend.idle_seconds(now),
                    )
                    try:
                        await backend.close()
                    except Exception as e:
                        logger.debug("scrcpy reap close error (%s): %s", serial, e)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("scrcpy reaper loop error: %s", e)

    async def _get_android_sdk(self, serial: str) -> Optional[int]:
        """디바이스 Android API 레벨(ro.build.version.sdk) 조회 + 캐시. 실패 시 None."""
        if serial in self._sdk_cache:
            return self._sdk_cache[serial]
        sdk: Optional[int] = None
        try:
            out = await self._run_device(
                serial, "shell getprop ro.build.version.sdk", timeout=5,
            )
            sdk = int(out.strip())
        except (ValueError, AttributeError, Exception):
            sdk = None
        self._sdk_cache[serial] = sdk
        return sdk

    async def _scrcpy_version_for(self, serial: str) -> str:
        """이 디바이스에서 쓸 scrcpy 버전 — Android 버전으로 **결정적** 선택.

        SurfaceControl.createDisplay(String, boolean) 가 Android 16(API 36)에서
        제거돼 v1.25 는 Android 16+ 에서 즉사한다. 그래서 버전을 1:1로 못박는다:
          * Android 16+ (SDK>=36) → v3.3.4
          * Android 15 이하 (SDK<=35, 또는 SDK 불명) → v1.25
        선택한 버전의 jar 이 없으면 가용한 다른 버전으로만 보정(미러링 유지 목적).
        """
        sdk = await self._get_android_sdk(serial)
        primary = SCRCPY_V3 if (sdk is not None and sdk >= 36) else SCRCPY_V1
        if detect_scrcpy_server(primary):
            return primary
        other = SCRCPY_V1 if primary == SCRCPY_V3 else SCRCPY_V3
        return other if detect_scrcpy_server(other) else primary

    def is_scrcpy_capable(self, serial: str) -> bool:
        """scrcpy 가 한 번이라도 성공한 기기인가 (→ 폴링으로 눌러앉지 말고 scrcpy 유지)."""
        return serial in self._scrcpy_capable

    async def ensure_scrcpy_backend(
        self,
        serial: str,
        logical_id: Optional[int],
        *,
        bitrate: int = 4_000_000,
        max_fps: Optional[int] = None,
    ) -> Optional[ScrcpyServerBackend]:
        """디바이스의 scrcpy 백엔드를 보장.

        - 같은 (serial, logical_id) 조합으로 살아있으면 재사용.
        - 디스플레이 변경 시 기존 close 후 새로 시작.
        - jar 부재, push 실패, server 실행 실패, 첫 프레임 timeout 등으로 실패 시 None →
          호출자는 screencap PNG 폴링 폴백 사용.
        - 누적 실패 카운트가 SCRCPY_FAILURE_THRESHOLD에 도달하면 영구 disable 자동 마킹.
        """
        if not detect_scrcpy_server():
            return None
        if serial in self._scrcpy_disabled:
            return None
        # idle reaper 기동(이벤트 루프가 있는 이 시점에 1회).
        self._ensure_scrcpy_reaper()

        async with self._scrcpy_lock:
            existing = self._scrcpy_backends.get(serial)
            if existing and existing.is_alive():
                if existing.logical_id == (logical_id or 0):
                    return existing
                # FCFS — scrcpy v1.25는 single-instance다 (socket name "scrcpy" 고정 +
                # `adb reverse localabstract:scrcpy` 단일 매핑). 같은 serial에 두 디스플
                # 레이를 동시 구동할 수 없다. 기존 인스턴스를 즉시 evict하면 서로 다른
                # 디스플레이를 보는 두 WS 세션이 상대의 app_process를 pkill하며 무한 재
                # spawn(핑퐁) → 인코더 전송버퍼 누적 → OOM. 따라서 먼저 점유한 디스플레
                # 이를 유지하고, 다른 디스플레이 요청은 screencap 폴링으로 폴백(None).
                logger.info(
                    "scrcpy already serving display=%s on %s — display=%s falls back "
                    "to screencap polling (v1.25 single-instance)",
                    existing.logical_id, serial, logical_id,
                )
                return None
            if existing:
                # 죽은 backend 잔존 → 정리 후 새로 시작.
                try:
                    await existing.close()
                except Exception as e:
                    logger.debug("scrcpy existing close error: %s", e)
                self._scrcpy_backends.pop(serial, None)

            # 버전은 Android 버전으로 결정적 선택(≤15→v1.25, ≥16→v3.3.4). 일시적
            # push/forward 장애에 대비해 같은 버전으로 1회 자동 retry.
            version = await self._scrcpy_version_for(serial)
            for attempt in range(2):
                backend = ScrcpyServerBackend(
                    serial, logical_id, version=version, bitrate=bitrate,
                    **({"max_fps": max_fps} if max_fps is not None else {}),
                )
                ok = await backend.try_start()
                if ok:
                    self._scrcpy_backends[serial] = backend
                    # 한 번이라도 성공 → "scrcpy 가능 기기"로 영구 기록 (이후 폴링으로
                    # 눌러앉지 않고 항상 scrcpy 로 복귀).
                    self._scrcpy_capable.add(serial)
                    # 성공 시 실패 카운터 reset — 한 번 정상 동작했다면 일시 장애 카운트 의미 없음.
                    self._scrcpy_failure_count.pop(serial, None)
                    return backend
                try:
                    await backend.close()
                except Exception:
                    pass
                if attempt == 0:
                    await asyncio.sleep(0.5)

            # scrcpy 가능 기기(이전 성공)는 영구 disable 하지 않는다 — 일시 장애로 보고
            # 다음 요청에서 다시 scrcpy 시도 (사용자 요구: scrcpy 되는 기기는 무조건 scrcpy).
            if serial in self._scrcpy_capable:
                logger.info(
                    "scrcpy try_start failed for %s (v%s) — capable device, "
                    "transient; will retry scrcpy (no disable)",
                    serial, version,
                )
                return None

            # 한 번도 성공한 적 없는 기기만 누적 실패 카운터 → 임계치 도달 시 영구 disable.
            count = self._scrcpy_failure_count.get(serial, 0) + 1
            self._scrcpy_failure_count[serial] = count
            logger.info(
                "scrcpy try_start failed for %s (v%s, attempt %d/%d) — %s",
                serial, version, count, self.SCRCPY_FAILURE_THRESHOLD,
                "permanently disabled" if count >= self.SCRCPY_FAILURE_THRESHOLD
                else "will retry on next request",
            )
            if count >= self.SCRCPY_FAILURE_THRESHOLD:
                self.mark_scrcpy_disabled(serial)
            return None

    async def close_scrcpy_backend(
        self, serial: str, expected: Optional[ScrcpyServerBackend] = None,
    ) -> None:
        """serial의 scrcpy backend 종료.

        expected 지정 시, 현재 dict 항목이 expected와 동일 객체일 때만 종료한다.
        FCFS 하에서 한 WS 세션의 disconnect 정리가 다른 세션이 새로 띄운 backend를
        실수로 닫는 것을 방지하기 위함.
        """
        async with self._scrcpy_lock:
            current = self._scrcpy_backends.get(serial)
            if expected is not None and current is not expected:
                return
            backend = self._scrcpy_backends.pop(serial, None)
        if backend:
            try:
                await backend.close()
            except Exception as e:
                logger.debug("scrcpy close error (%s): %s", serial, e)

    async def close_scrcpy_backends_for_playback(self) -> None:
        """시나리오 재생 시작 시 호출 — 모든 scrcpy 백엔드를 닫는다.

        재생 중에는 각 스텝이 screencap 으로 캡처/검증하는데, scrcpy 인코더가 같은
        디바이스에서 계속 돌면 USB/인코더 경합(특히 IVI OOM)을 유발한다. 그래서 재생
        진입 시 미러 백엔드를 명시적으로 회수한다. ensure_scrcpy_backend 는 재생 중
        게이트(not playback_service.is_running)로 막혀 재기동되지 않으며, 재생 종료 후
        미러가 다시 붙으면 자동 복귀한다. reaper 는 끄지 않는다(평상시 동작 유지)."""
        async with self._scrcpy_lock:
            backends = list(self._scrcpy_backends.values())
            self._scrcpy_backends.clear()
        for b in backends:
            try:
                await b.close()
            except Exception:
                pass
        if backends:
            logger.info(
                "scrcpy backends closed for playback (%d) — screencap 검증과 경합 방지",
                len(backends),
            )

    async def close_all_scrcpy_backends(self) -> None:
        # idle reaper 정지 (shutdown).
        if self._scrcpy_reaper_task is not None and not self._scrcpy_reaper_task.done():
            self._scrcpy_reaper_task.cancel()
            try:
                await self._scrcpy_reaper_task
            except (asyncio.CancelledError, Exception):
                pass
        self._scrcpy_reaper_task = None
        async with self._scrcpy_lock:
            backends = list(self._scrcpy_backends.values())
            self._scrcpy_backends.clear()
        for b in backends:
            try:
                await b.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # scrcpy 비활성 캐시 (GVM 등 scrcpy 동작 불가 디바이스)
    # ------------------------------------------------------------------

    def is_scrcpy_disabled(self, serial: str) -> bool:
        return serial in self._scrcpy_disabled

    def get_scrcpy_retry_after(self, serial: str) -> float:
        """serial 단위 scrcpy 재시도 쿨다운 만료 시각(event-loop time). WS 세션 간 공유."""
        return self._scrcpy_retry_after.get(serial, 0.0)

    def set_scrcpy_retry_after(self, serial: str, ts: float) -> None:
        self._scrcpy_retry_after[serial] = ts

    def mark_scrcpy_disabled(self, serial: str) -> None:
        """디바이스가 scrcpy를 지원 못 함을 캐시. 다음 시도부터 즉시 screencap PNG 폴링."""
        # scrcpy 가 한 번이라도 됐던 기기는 절대 영구 disable 하지 않는다 — 일시 장애일
        # 뿐, 다음에 다시 scrcpy 로 복귀해야 한다 (사용자 요구).
        if serial in self._scrcpy_capable:
            return
        if serial not in self._scrcpy_disabled:
            self._scrcpy_disabled.add(serial)
            logger.info(
                "scrcpy permanently disabled for %s (try_start failed). "
                "Using screencap PNG polling fallback until device disconnects.",
                serial,
            )

    def clear_scrcpy_disabled(self, serial: str) -> None:
        """디바이스 disconnect 시 호출 — 다음 연결에서 다시 시도 가능하게.

        실패 카운터도 함께 reset해 disconnect → 재연결 사이클에서 누적되지 않도록 한다.
        """
        self._scrcpy_disabled.discard(serial)
        self._scrcpy_failure_count.pop(serial, None)
        self._scrcpy_retry_after.pop(serial, None)


class AdbScreencapStreamer:
    """하나의 `adb shell` 프로세스를 장기 유지하며 screencap을 반복 수행.

    - 기존: 프레임마다 `adb.exe exec-out screencap -p` → 프로세스 spawn/kill 수십 회/초
    - 개선: adb shell 1회 실행 → stdin으로 명령 반복 전송, stdout으로 PNG base64 수신

    TTY 변환(\\r\\n) 문제를 회피하기 위해 **base64 인코딩**으로 전송하고, 프레임 경계는
    고유 마커(START/END)로 구분. 중복 요청은 asyncio.Lock으로 직렬화되어
    "현재 capture + 다운로드가 끝나기 전에는 다음 명령을 보내지 않음".
    """

    START = b"__RK_FRAME_START__"
    END = b"__RK_FRAME_END__"

    def __init__(self, serial: str, sf_display_id: Optional[str] = None,
                 container: Optional[str] = None):
        self.serial = serial
        self.sf_display_id = sf_display_id
        self.container = container
        self._proc: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _start_sync(self) -> None:
        cmd_list = [ADB_PATH, "-s", self.serial, "shell"]
        self._proc = subprocess.Popen(
            cmd_list,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=_NO_WINDOW,
            bufsize=0,
        )

    async def start(self) -> None:
        """adb shell 세션 개시 (이미 살아있으면 no-op)."""
        if self.is_alive():
            return
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._start_sync)
        logger.info(
            "ADB screen streamer started: serial=%s sf_id=%s pid=%s",
            self.serial, self.sf_display_id or "default",
            self._proc.pid if self._proc else "?",
        )

    def _build_cmd(self) -> bytes:
        dflag = f"-d {self.sf_display_id} " if self.sf_display_id else ""
        base = f"screencap -p {dflag}".rstrip()
        if self.container:
            base = f"lxc-attach -n {self.container} -- {base}"
        # base64 로 인코딩 → TTY CRLF 변환 문제 회피
        return (
            f"echo {self.START.decode()}; {base} | base64; "
            f"echo {self.END.decode()}\n"
        ).encode()

    def _read_frame_sync(self) -> bytes:
        """stdin에 명령 쓰고 stdout에서 end marker까지 수집 (blocking)."""
        if not self.is_alive():
            self._start_sync()
        assert self._proc is not None
        try:
            self._proc.stdin.write(self._build_cmd())
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise IOError(f"adb stdin write failed: {e}")

        collecting = False
        buf = bytearray()
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise IOError("adb shell closed unexpectedly")
            stripped = line.rstrip(b"\r\n")
            if not collecting:
                # 세션 초기 쓰레기(모티드 등)는 START 마커 전까지 모두 버림
                if stripped == self.START:
                    collecting = True
                continue
            if stripped == self.END:
                break
            buf.extend(stripped)

        import base64 as _b64
        try:
            png = _b64.b64decode(bytes(buf), validate=False)
        except Exception as e:
            raise IOError(f"base64 decode failed: {e}")
        if len(png) < 8 or png[:4] != b"\x89PNG":
            raise IOError("captured data is not a valid PNG")
        return png

    async def capture_png(self) -> bytes:
        """한 프레임 PNG 캡처. lock으로 중복 호출 직렬화."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._read_frame_sync)

    async def close(self) -> None:
        async with self._lock:
            if not self._proc:
                return
            proc = self._proc
            self._proc = None

            def _sync_close():
                try:
                    try:
                        proc.stdin.write(b"exit\n")
                        proc.stdin.flush()
                        proc.stdin.close()
                    except Exception:
                        pass
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=1)
                        except Exception:
                            pass
                except Exception:
                    pass

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _sync_close)
