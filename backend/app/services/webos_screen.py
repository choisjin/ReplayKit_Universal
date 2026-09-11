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
                 default_serial: str = "", fallback_size: tuple = (0, 0)):
        self._info: dict = info if isinstance(info, dict) else {}
        self.device_id = device_id
        # ADB 로 붙인 디바이스는 그 자신이 HU 라 별도 시리얼 입력이 필요 없다.
        self._default_serial = (default_serial or "").strip()
        # 패널 크기를 아직 모를 때 쓸 폴백(예: iSAP 전석 화면 크기 — 같은 물리 패널).
        self._fallback_size = tuple(fallback_size or (0, 0))
        self._svc = None
        self.auto_active = False     # 자동 전환으로 WebOS 를 보여주는 중인지
        # Android 오버레이 캐시 (검정 키 합성용) — (시각, BGR 프레임)
        self._ov_cache: tuple = (0.0, None)
        self._restart_needed = False   # 설정 변경으로 공유 스트림까지 내려야 하는지

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
            self._restart_needed = True      # 접속/화질 변경 → 공유 스트림도 내린다
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

    @staticmethod
    def _size_of(d) -> tuple[int, int]:
        if isinstance(d, dict) and d.get("width") and d.get("height"):
            try:
                return int(d["width"]), int(d["height"])
            except (TypeError, ValueError):
                pass
        return 0, 0

    def _native_size(self) -> tuple[int, int]:
        """Linux VM 프레임버퍼 = 물리 패널 크기 (linuxStream 로그의 native=WxH)."""
        if self._svc is not None:
            nw, nh = self._svc.native_size
            if nw and nh:
                return nw, nh
        return 0, 0

    def panel_size(self) -> tuple[int, int]:
        """**패널 좌표계** — 미러 이미지와 webOS(evdev) 터치가 쓰는 기준.

        webOS 는 Linux VM 프레임버퍼를 꽉 채우고 그 크기는 팝업 상태와 무관하게
        고정이다. 그래서 native 를 최우선으로 본다.
        """
        for v in (self._native_size(),
                  self._size_of(self._info.get("webos_android_size")),
                  self._size_of(self._info.get("resolution"))):
            if v[0] and v[1]:
                return v
        fw, fh = self._fallback_size
        return (int(fw), int(fh)) if (fw and fh) else (0, 0)

    def android_size(self) -> tuple[int, int]:
        """**Android 논리 디스플레이 크기** — `input tap/swipe` 가 쓰는 기준.

        팝업(Extended) 상태에 따라 3840x850 ↔ 3840x1440 으로 바뀐다. 감지값이 없으면
        패널 크기로 폴백(두 값이 같은 상태라면 결과가 같다).
        """
        v = self._size_of(self._info.get("webos_android_size"))
        if v[0] and v[1]:
            return v
        return self.panel_size()

    # ------------------------------------------------------------------
    # 스트림
    # ------------------------------------------------------------------

    def stream(self):
        """WebOS 화면 스트림(lazy). 미지원 디바이스면 ValueError."""
        if not self.enabled:
            raise ValueError("WebOS 화면이 설정되지 않은 디바이스입니다 (webos_adb_serial 없음)")
        if self._svc is None:
            from .webos_stream_service import get_shared_stream
            cfg = self._info
            # 같은 HU 를 보는 디바이스가 둘 이상이어도(iSAP + ADB 동시 등록) 디바이스측
            # linuxStream 은 하나여야 한다 — 아니면 서로 pkill 로 끄는 핑퐁이 난다.
            self._svc = get_shared_stream(
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
        """이 디바이스의 참조만 놓는다.

        스트림 인스턴스는 serial 단위 공유라 여기서 끄지 않는다(다른 디바이스가
        보고 있을 수 있다). 아무도 안 쓰면 스트림 쪽 idle reaper 가 정리한다.
        설정이 바뀐 경우엔 drop_shared_stream 으로 확실히 내린다.
        """
        svc, self._svc = self._svc, None
        self.auto_active = False
        self._ov_cache = (0.0, None)
        if svc is not None and self._restart_needed:
            self._restart_needed = False
            from .webos_stream_service import drop_shared_stream
            drop_shared_stream(svc)

    def screen_size(self) -> tuple[int, int]:
        """미러 좌표계 크기 = 패널 크기. **연결 순간부터 끝까지 같은 값**이어야 한다.

        중간에 값이 바뀌면 프론트(옛 값)와 백엔드(새 값)의 기준이 어긋나 터치가
        배율만큼 빗나간다 — iSAP 연결에서 실제로 겪은 회귀다. 그래서 스트림 출력
        (-scale 축소본)은 절대 쓰지 않고 패널 크기만 돌려준다.
        """
        return self.panel_size()

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

    # --- Android 오버레이 합성 --------------------------------------

    @property
    def overlay_android(self) -> bool:
        """webOS 프레임 위에 Android UI 를 합성할지 (기본 OFF).

        실제 화면과 같은 그림이 되지만 Android 캡처가 adb 링크를 쓴다.
        """
        return bool(self._info.get("webos_overlay_android", False))

    @property
    def overlay_interval(self) -> float:
        """Android 오버레이 갱신 간격(초). 사이드바/시계는 거의 정적이라 길게 잡는다."""
        try:
            return max(0.5, float(self._info.get("webos_overlay_interval", 3.0)))
        except (TypeError, ValueError):
            return 3.0

    @property
    def overlay_threshold(self) -> int:
        """이 밝기 이하를 '투명(webOS 가 비치는 영역)'으로 본다."""
        try:
            return max(0, min(255, int(self._info.get("webos_overlay_threshold", 16))))
        except (TypeError, ValueError):
            return 16

    async def _android_frame(self):
        """Android 화면 캡처(BGR). 갱신 간격 내에는 캐시를 쓴다."""
        import time as _t

        import cv2
        import numpy as np

        now = _t.monotonic()
        ts, cached = self._ov_cache
        if cached is not None and (now - ts) < self.overlay_interval:
            return cached
        adb, serial, _did = self._adb()
        try:
            raw = await adb.streaming_screencap_bytes(serial=serial, fmt="png")
            img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
        except Exception as e:
            logger.debug("WebOS 오버레이 캡처 실패: %s", e)
            img = None
        if img is not None:
            self._ov_cache = (now, img)
            return img
        # 실패 시 직전 프레임 유지(있으면), 없으면 None
        self._ov_cache = (now, cached)
        return cached

    async def screencap_bytes(self, fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        data = await self.stream().async_screencap_bytes(fmt=fmt, timeout=timeout)
        if not self.overlay_android:
            return data
        try:
            import cv2
            import numpy as np

            base = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            andr = await self._android_frame()
            if base is None or andr is None:
                return data
            h, w = base.shape[:2]
            if (andr.shape[1], andr.shape[0]) != (w, h):
                andr = cv2.resize(andr, (w, h), interpolation=cv2.INTER_AREA)
            # 검정(=webOS 가 비치는 영역)만 통과시키고 나머지는 Android 를 덮는다
            mask = cv2.cvtColor(andr, cv2.COLOR_BGR2GRAY) > self.overlay_threshold
            base[mask] = andr[mask]
            ext = ".png" if fmt == "png" else ".jpg"
            params = [] if fmt == "png" else [cv2.IMWRITE_JPEG_QUALITY, 70]
            ok, buf = cv2.imencode(ext, base, params)
            return buf.tobytes() if ok else data
        except Exception as e:
            logger.debug("WebOS 오버레이 합성 실패: %s", e)
            return data

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
        aw, ah = self.panel_size()
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

    def _to_android(self, x: int, y: int) -> tuple[int, int]:
        """패널 좌표 → Android 입력 좌표.

        팝업 상태에서 Android 논리 크기가 패널보다 작으면(예: 3840x850) 그 비율로
        줄여야 `input tap/swipe` 가 제자리에 들어간다.
        """
        pw, ph = self.panel_size()
        aw, ah = self.android_size()
        if pw and aw and pw != aw:
            x = x * aw / pw
        if ph and ah and ph != ah:
            y = y * ah / ph
        ix, iy = int(round(x)), int(round(y))
        if aw and ah:
            ix, iy = max(0, min(aw - 1, ix)), max(0, min(ah - 1, iy))
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
        tx, ty = self._to_android(px, py)
        await adb.tap(tx, ty, serial=serial, display_id=did)
        logger.info("[WebOS TAP/adb] panel(%s,%s) -> android(%s,%s) serial=%s",
                    px, py, tx, ty, serial)

    async def repeat_tap(self, x: int, y: int, count: int = 5,
                         interval_ms: int = 100) -> None:
        px, py = self._clamp(x, y)
        if self.touch_via_evdev:
            await self._run(self.stream().repeat_tap, px, py, count, interval_ms)
            return
        adb, serial, did = self._adb()
        tx, ty = self._to_android(px, py)
        await adb.repeat_tap(tx, ty, count, interval_ms, serial=serial, display_id=did)

    async def long_press(self, x: int, y: int, duration_ms: int = 3000) -> None:
        px, py = self._clamp(x, y)
        if self.touch_via_evdev:
            await self._run(self.stream().long_press, px, py, duration_ms)
            return
        adb, serial, did = self._adb()
        tx, ty = self._to_android(px, py)
        await adb.long_press(tx, ty, duration_ms, serial=serial, display_id=did)

    def _edge_snap(self, x: int, y: int, tox: int, toy: int) -> tuple[int, int]:
        """시작점이 가장자리 근처면 **정확한 가장자리로 붙인다**.

        "화면 끝에서 안쪽으로 쓸어 리모콘 열기" 같은 엣지 제스처는 시작점이 진짜 끝
        (0 또는 max-1)이어야 인식된다. 미러에서 드래그하면 보통 몇 px 안쪽에서 시작해
        실패하므로, 안쪽으로 향하는 스와이프에 한해 시작점을 끝으로 보정한다.
        info["webos_edge_snap"]=0 이면 끈다(기본 24px, 패널 좌표 기준).
        """
        try:
            th = int(self._info.get("webos_edge_snap", 24))
        except (TypeError, ValueError):
            th = 24
        if th <= 0:
            return x, y
        aw, ah = self.panel_size()
        sx, sy = x, y
        if aw:
            if x <= th and tox > x:
                sx = 0
            elif x >= aw - 1 - th and tox < x:
                sx = aw - 1
        if ah:
            if y <= th and toy > y:
                sy = 0
            elif y >= ah - 1 - th and toy < y:
                sy = ah - 1
        if (sx, sy) != (x, y):
            logger.info("[WebOS] 엣지 스냅 (%s,%s) -> (%s,%s)", x, y, sx, sy)
        return sx, sy

    def _is_edge_start(self, x: int, y: int) -> bool:
        """시작점이 화면 가장자리에 정확히 붙어 있는가 (엣지 스냅 후 판정, 패널 기준)."""
        aw, ah = self.panel_size()
        return bool((aw and (x <= 0 or x >= aw - 1))
                    or (ah and (y <= 0 or y >= ah - 1)))

    @property
    def edge_via_adb(self) -> bool:
        """엣지 스와이프를 Android 로 보낼지 (기본 ON).

        리모콘 호출 같은 **시스템 제스처는 Android 레이어가 처리**한다 — webOS
        터치스크린(evdev)으로 보내면 반응이 없다(실기 확인). 콘텐츠 스크롤은 반대로
        evdev 라야 먹으므로, 시작점이 가장자리인 스와이프만 Android 로 보낸다.
        info["webos_edge_via"]="evdev" 로 끌 수 있다.
        """
        return str(self._info.get("webos_edge_via") or "adb").lower() == "adb"

    async def swipe(self, x1: int, y1: int, x2: int, y2: int,
                    duration_ms: int = 300, hold_ms: int = 0) -> None:
        ax, ay = self._clamp(x1, y1)
        bx, by = self._clamp(x2, y2)
        ax, ay = self._edge_snap(ax, ay, bx, by)
        use_adb = (not self.touch_via_evdev) or (
            self.edge_via_adb and self._is_edge_start(ax, ay))
        if not use_adb:
            await self._run(self.stream().swipe, ax, ay, bx, by,
                            int(duration_ms or 300), hold_ms)
            return
        adb, serial, did = self._adb()
        tx1, ty1 = self._to_android(ax, ay)
        tx2, ty2 = self._to_android(bx, by)
        if self.touch_via_evdev:
            logger.info("[WebOS SWIPE] 엣지 제스처 → Android 경유 "
                        "panel(%s,%s)->(%s,%s) = android(%s,%s)->(%s,%s)",
                        ax, ay, bx, by, tx1, ty1, tx2, ty2)
        await adb.swipe(tx1, ty1, tx2, ty2, duration_ms=int(duration_ms or 300),
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
        pts = [dict(zip(("x", "y"), self._to_android(p["x"], p["y"]))) for p in pts]
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
        conv = []
        for f in fs:
            a = self._to_android(f["x1"], f["y1"])
            b = self._to_android(f["x2"], f["y2"])
            conv.append({"x1": a[0], "y1": a[1], "x2": b[0], "y2": b[1]})
        await adb.multi_finger_swipe(conv, duration_ms, serial=serial, display_id=did)

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
