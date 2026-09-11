"""WebOS 화면 지원 — iSAP / ADB 두 연결 방식이 공유하는 얇은 래퍼.

같은 HU(HKMC Connect Wide)를 **iSAP Agent 로 붙이든 ADB 로 붙이든** WebOS 화면은
동일하게 동작해야 한다. 그래서 "어떤 화면을 쓸지 결정 + 캡처/터치 위임" 로직을 여기
한 곳에 모으고, 두 경로가 이 클래스를 쓴다.

핵심 사실 (2026-09-11 실기 확정 — 자세한 배경은 webos_stream_service 참고):
  * **화면**: webOS 는 별도 Linux VM 이고 Android 레이어엔 그 자리가 **hole** 로 뚫려
    있다. 그래서 iSAP CMD_GETIMG 로도, scrcpy/screencap 으로도 안 잡힌다
    → Linux VM 의 linuxStream 스트림에서 가져온다.
  * **터치**: Android `input tap` 은 그 영역에서 무반응. linuxStream 의 uinput 장치는
    스펙 부족(INPUT_PROP_DIRECT/ABS_MT_* 없음)으로 webOS 가 무시한다
    → **실제 터치스크린 evdev 노드에 MT 이벤트를 직접 write** 한다.
  * **자동 전환**: Android `topResumedActivity` 가 webosprojectionhmi 면 webOS 화면,
    아니면(설정 등) 네이티브 Android 화면. 전석/기본 화면을 보는 중이면 이 판별로
    화면·터치를 자동으로 갈아탄다(양방향).

디바이스 설정(dev.info)은 **참조로** 들고 있는다 — 편집 모달 저장분과 device_manager 가
채우는 값이 즉시 반영되도록.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

WEBOS_SCREEN = "webos"
# WebOS 화면을 가진 모델 — 현재 Connect Wide 한정.
WEBOS_MODELS = ("Connect Wide",)


class WebOSScreen:
    """디바이스 하나의 WebOS 화면 상태(스트림 + 전환 판정)."""

    # 터치 경로가 허용하는 전면 판별 캐시 나이(초). 캡처(1.5s)보다 짧게 둔다 —
    # webOS ↔ Android(설정 등) 전환 직후 낡은 캐시로 반대쪽에 주입하면
    # 아무 반응 없는 무증상 실패가 된다.
    TOUCH_FG_MAX_AGE = 0.3

    def __init__(self, info: Optional[dict] = None, device_id: str = "",
                 default_serial: str = ""):
        self._info: dict = info if isinstance(info, dict) else {}
        self.device_id = device_id
        # ADB 로 붙인 디바이스는 그 자신이 HU 라 별도 시리얼 입력이 필요 없다.
        self._default_serial = (default_serial or "").strip()
        self._svc = None
        self.auto_active = False     # 자동 전환으로 WebOS 를 보여주는 중인지

    # ------------------------------------------------------------------
    # 설정
    # ------------------------------------------------------------------

    def set_info(self, info: Optional[dict]) -> None:
        """설정 변경 반영. 접속 정보가 바뀌면 기존 스트림을 내린다."""
        prev = self._info
        self._info = info if isinstance(info, dict) else {}
        keys = ("webos_adb_serial", "webos_linux_ip", "webos_linux_user",
                "webos_linux_password", "webos_scale", "webos_quality", "webos_fps",
                "webos_evdev")
        if any(self._info.get(k) != prev.get(k) for k in keys):
            self.stop()

    @property
    def enabled(self) -> bool:
        """WebOS 화면을 지원하는 디바이스인지 (모델 + 대상 시리얼)."""
        model = str(self._info.get("device_model") or "").strip()
        return model in WEBOS_MODELS and bool(self.serial)

    @property
    def serial(self) -> str:
        """webOS 를 태운 HU 의 ADB 시리얼. ADB 디바이스는 자기 주소가 기본값."""
        return (str(self._info.get("webos_adb_serial") or "").strip()
                or self._default_serial)

    @property
    def display_id(self) -> Optional[int]:
        try:
            did = int(self._info.get("webos_display_id") or 0)
        except (TypeError, ValueError):
            return None
        return did or None

    @property
    def auto(self) -> bool:
        """기본 화면 시청 중 webOS 가 뜨면 자동 전환할지 (기본 ON)."""
        v = self._info.get("webos_auto")
        return True if v is None else bool(v)

    @property
    def touch_via_evdev(self) -> bool:
        """터치를 webOS 터치스크린 evdev 에 직접 주입할지 (기본). 'adb' 면 Android 경유."""
        return str(self._info.get("webos_touch_via") or "evdev").lower() != "adb"

    def android_size(self) -> tuple[int, int]:
        """Android 디스플레이 크기 = 패널 좌표계 = 터치 좌표계.

        device_manager 가 wm size 로 채운 webos_android_size, 없으면 일반 resolution.
        """
        for key in ("webos_android_size", "resolution"):
            a = self._info.get(key) or {}
            if isinstance(a, dict) and a.get("width") and a.get("height"):
                try:
                    return int(a["width"]), int(a["height"])
                except (TypeError, ValueError):
                    continue
        return 0, 0

    # ------------------------------------------------------------------
    # 스트림
    # ------------------------------------------------------------------

    def stream(self):
        """WebOS 화면 스트림(lazy). 미지원 디바이스면 ValueError."""
        if not self.enabled:
            raise ValueError("WebOS 화면이 설정되지 않은 디바이스입니다 (webos_adb_serial 없음)")
        if self._svc is None:
            from .webos_stream_service import WebOSStreamService
            cfg = self._info
            self._svc = WebOSStreamService(
                adb_serial=self.serial,
                linux_ip=str(cfg.get("webos_linux_ip") or "172.16.4.1").strip(),
                linux_user=str(cfg.get("webos_linux_user") or "root").strip(),
                linux_password=str(cfg.get("webos_linux_password") or "root"),
                scale=int(cfg.get("webos_scale") or 2),
                quality=int(cfg.get("webos_quality") or 60),
                fps=int(cfg.get("webos_fps") or 30),
                device_id=self.device_id,
                evdev_path=str(cfg.get("webos_evdev") or "").strip(),
            )
        return self._svc

    def stop(self) -> None:
        svc, self._svc = self._svc, None
        self.auto_active = False
        if svc is not None:
            try:
                svc.stop()
            except Exception as e:
                logger.debug("WebOS stream stop failed: %s", e)

    def screen_size(self) -> tuple[int, int]:
        """미러 좌표계 크기 — Android 디스플레이 크기로 **고정**한다.

        스트림 출력(-scale 축소본)을 보고하면 스트림 기동 전후로 값이 바뀌어,
        프론트가 옛 값을 쥔 동안 터치가 배율만큼 어긋난다. 프론트는 캔버스 비율로
        좌표를 만들기 때문에 실제 이미지 해상도와 달라도 무방하다.
        """
        aw, ah = self.android_size()
        if aw and ah:
            return aw, ah
        if self._svc is not None:
            nw, nh = self._svc.native_size
            if nw and nh:
                return nw, nh
        return 0, 0

    # ------------------------------------------------------------------
    # 화면 결정
    # ------------------------------------------------------------------

    def is_webos(self, screen_type: Optional[str]) -> bool:
        return (screen_type or "") == WEBOS_SCREEN

    def resolve(self, screen_type: Optional[str], base_screen: str,
                max_age: Optional[float] = None) -> str:
        """실제로 쓸 화면을 결정한다.

        base_screen(전석/기본 화면)을 보고 있고 webOS 가 전면이면 'webos' 로 바꾼다.
        **캡처와 터치가 같은 판단을 써야** 한다 — 화면은 webOS 인데 터치가 Android 로
        나가면(또는 반대) 엉뚱한 곳이 눌린다.
        """
        st = screen_type or base_screen
        if self.is_webos(st):
            return WEBOS_SCREEN
        if st != base_screen or not self.enabled or not self.auto:
            self.auto_active = False
            return st
        try:
            active = self.stream().is_foreground(self.display_id, max_age=max_age)
        except Exception as e:
            logger.debug("WebOS auto-switch probe failed: %s", e)
            active = False
        if active != self.auto_active:
            logger.info("WebOS 자동 전환: %s (device=%s)",
                        "WebOS 화면" if active else "기본 화면", self.device_id)
        self.auto_active = active
        return WEBOS_SCREEN if active else st

    # ------------------------------------------------------------------
    # 캡처 / 터치
    # ------------------------------------------------------------------

    async def screencap_bytes(self, fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        return await self.stream().async_screencap_bytes(fmt=fmt, timeout=timeout)

    def client_size(self) -> tuple[int, int]:
        """프론트가 좌표를 만들 때 쓰는 기준 크기 = dev.info["screens"]["webos"].

        이 값은 갱신 시점에 따라 스트림 크기(축소본)일 수도, 패널 크기일 수도 있다.
        그래서 백엔드가 임의로 가정하지 않고 **이 값 그대로** 소스 좌표계로 삼는다.
        없으면 screen_size() 로 폴백.
        """
        src = (self._info.get("screens") or {}).get(WEBOS_SCREEN) or {}
        try:
            w, h = int(src.get("width") or 0), int(src.get("height") or 0)
        except (TypeError, ValueError):
            w = h = 0
        return (w, h) if (w and h) else self.screen_size()

    def _clamp(self, x: float, y: float) -> tuple[int, int]:
        """클라이언트 좌표 → 패널(터치) 좌표. 기준이 다르면 배율 환산 후 범위 클램프."""
        sw, sh = self.client_size()
        aw, ah = self.android_size()
        if sw and aw and sw != aw:
            x = x * aw / sw
        if sh and ah and sh != ah:
            y = y * ah / sh
        ix, iy = int(round(x)), int(round(y))
        if aw and ah:
            cx, cy = max(0, min(aw - 1, ix)), max(0, min(ah - 1, iy))
            if (cx, cy) != (ix, iy):
                logger.warning("[WebOS] 터치 좌표가 화면 밖(%s,%s) → 클램프(%s,%s) %sx%s",
                               ix, iy, cx, cy, aw, ah)
            return cx, cy
        return ix, iy

    def _adb(self):
        """(adb_service, serial, display_id) — 런타임 import (순환 import 회피)."""
        from ..dependencies import adb_service
        return adb_service, self.serial, self.display_id

    async def _run(self, fn, *args):
        return await asyncio.get_event_loop().run_in_executor(None, fn, *args)

    async def tap(self, x: int, y: int) -> None:
        px, py = self._clamp(x, y)
        if (px, py) != (int(x), int(y)):
            logger.info("[WebOS] 좌표 환산 client(%s,%s)/%sx%s -> panel(%s,%s)/%sx%s",
                        x, y, *self.client_size(), px, py, *self.android_size())
        if self.touch_via_evdev:
            await self._run(self.stream().tap, px, py)
            return
        adb, serial, did = self._adb()
        await adb.tap(px, py, serial=serial, display_id=did)
        logger.info("[WebOS TAP/adb] (%s,%s) serial=%s", px, py, serial)

    async def repeat_tap(self, x: int, y: int, count: int = 5,
                         interval_ms: int = 100) -> None:
        px, py = self._clamp(x, y)
        if self.touch_via_evdev:
            await self._run(self.stream().repeat_tap, px, py, count, interval_ms)
            return
        adb, serial, did = self._adb()
        await adb.repeat_tap(px, py, count, interval_ms, serial=serial, display_id=did)

    async def long_press(self, x: int, y: int, duration_ms: int = 3000) -> None:
        px, py = self._clamp(x, y)
        if self.touch_via_evdev:
            await self._run(self.stream().long_press, px, py, duration_ms)
            return
        adb, serial, did = self._adb()
        await adb.long_press(px, py, duration_ms, serial=serial, display_id=did)

    async def swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300, hold_ms: int = 0) -> None:
        ax, ay = self._clamp(x1, y1)
        bx, by = self._clamp(x2, y2)
        if self.touch_via_evdev:
            await self._run(self.stream().swipe, ax, ay, bx, by,
                            int(duration_ms or 300), hold_ms)
            return
        adb, serial, did = self._adb()
        await adb.swipe(ax, ay, bx, by, duration_ms=int(duration_ms or 300),
                        serial=serial, display_id=did, hold_ms=hold_ms)

    async def multi_finger_tap(self, points: list) -> None:
        pts = []
        for p in points:
            px, py = self._clamp(p["x"], p["y"])
            pts.append({"x": px, "y": py})
        if self.touch_via_evdev:
            await self._run(self.stream().multi_finger_tap, pts)
            return
        adb, serial, did = self._adb()
        await adb.multi_finger_tap(pts, serial=serial, display_id=did)

    async def multi_finger_swipe(self, fingers: list, duration_ms: int = 500,
                                 hold_ms: int = 0) -> None:
        fs = []
        for f in fingers:
            ax, ay = self._clamp(f["x1"], f["y1"])
            bx, by = self._clamp(f["x2"], f["y2"])
            fs.append({"x1": ax, "y1": ay, "x2": bx, "y2": by})
        if self.touch_via_evdev:
            await self._run(self.stream().multi_finger_swipe, fs,
                            int(duration_ms or 500), hold_ms)
            return
        adb, serial, did = self._adb()
        await adb.multi_finger_swipe(fs, duration_ms, serial=serial, display_id=did)

    def get_info(self) -> dict:
        info = {
            "enabled": self.enabled,
            "serial": self.serial,
            "auto": self.auto,
            "auto_active": self.auto_active,
            "touch_via": "evdev" if self.touch_via_evdev else "adb",
            "screen_size": list(self.screen_size()),
        }
        if self._svc is not None:
            info["stream"] = self._svc.get_info()
        return info
