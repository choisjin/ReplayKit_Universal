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
import struct
import subprocess
import sys
import tempfile
import threading
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

# ── device-side live 스트리머 (host python3 에서 상주 실행) ──
# 디바이스 host(root)에 python3 3.10 이 있고 lxc-attach 로 컨테이너 진입 가능 → 프레임 캡처
# 루프를 디바이스에서 돌려 단일 `adb exec-out` 파이프로 push (프레임당 adb 프로세스 스폰/왕복 제거).
# 프레임 형식: b"BMWF" + struct('<BI', fmt, length) + data  (fmt 0=JPEG(WebOS) / 1=PNG(Android))
# 백엔드(_live_reader)가 PNG→JPEG 통일 후 최신본 보관. 전환 판별은 dumpsys topResumedActivity
# (Display #SCREEN, 1s 캐시) 권위 — webosprojectionhmi=WebOS, 그 외(settingshmi)=Android(screencap).
# 컨테이너엔 python/sh 없음 → 직접 바이너리(dumpsys/screencap/luna-send/cat)만 호출.
# __SCREEN__/__ANDROID__/__WEBOS__/__DISPLAY_ID__ 는 start_live_stream 에서 .replace() 주입.
_BMW_LIVE_STREAMER = r'''
import sys, os, time, struct, subprocess, re
SCREEN = __SCREEN__
ANDROID = "__ANDROID__"
WEBOS = "__WEBOS__"
DISPLAY_ID = "__DISPLAY_ID__"   # screencap -d 용 SF display id (빈 문자열이면 -d 생략)

def run(args, timeout=10):
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=timeout)
        return p.stdout or b""
    except Exception:
        return b""

def cap_android():
    cmd = ["lxc-attach", "-n", ANDROID, "--", "screencap"]
    if DISPLAY_ID:
        cmd += ["-d", DISPLAY_ID]
    cmd += ["-p"]
    d = run(cmd, 8)
    return d if (len(d) > 100 and d[:8] == b"\x89PNG\r\n\x1a\n") else b""

_WEBOS_TMP = "/tmp/bmw_live.JPG"   # 단발 캡처(/tmp/screenshot.JPG)와 분리 — 파일 경합 방지
def cap_webos():
    run(["lxc-attach", "-n", WEBOS, "--", "rm", "-f", _WEBOS_TMP], 5)
    run(["lxc-attach", "-n", WEBOS, "--", "luna-send", "-n", "1", "-f",
         "luna://com.webos.surfacemanager/captureCompositorOutput",
         '{"output":"' + _WEBOS_TMP + '","format":"JPG"}'], 10)
    deadline = time.time() + 2.0
    last = b""
    time.sleep(0.05)
    while time.time() < deadline:
        d = run(["lxc-attach", "-n", WEBOS, "--", "cat", _WEBOS_TMP], 8)
        if len(d) > 1000 and d[:2] == b"\xff\xd8" and d[-2:] == b"\xff\xd9":
            return d
        last = d
        time.sleep(0.05)
    return last if (len(last) > 1000 and last[:2] == b"\xff\xd8") else b""

_fg = [None, 0.0]
def fg_backend():
    now = time.time()
    if _fg[0] is not None and now - _fg[1] < 1.0:
        return _fg[0]
    txt = run(["lxc-attach", "-n", ANDROID, "--", "dumpsys", "activity", "activities"], 8)
    txt = txt.decode("utf-8", "replace")
    cur = None; res = None
    for line in txt.splitlines():
        m = re.search(r"Display\s+#(\d+)", line)
        if m:
            cur = int(m.group(1)); continue
        if "topResumedActivity" in line and cur is not None:
            be = "webos" if "webosprojectionhmi" in line else "adb"
            if cur == SCREEN:
                res = be
            cur = None
    _fg[0] = res; _fg[1] = now
    return res

out = sys.stdout.buffer
cached = None
_last_force = [0.0]   # 빈 프레임 시 dumpsys 강제갱신 rate-limit(전환 빠르게 + spam 방지)

def emit(d, b):
    out.write(b"BMWF" + struct.pack("<BI", 0 if b == "webos" else 1, len(d)) + d)
    out.flush()

def cap(b):
    return cap_webos() if b == "webos" else cap_android()

while True:
    try:
        fg = fg_backend()
        if fg in ("webos", "adb"):
            # 권위 백엔드만 캡처 — 반대편으로 폴백 금지(블링크/디스플레이 혼입 방지).
            d = cap(fg)
            if d:
                cached = fg
                emit(d, fg)
            else:
                # 권위 백엔드가 빈 프레임 → 전환 의심: dumpsys 강제 갱신(0.5s rate-limit)해
                # 빠르게 전환 감지. 같은 백엔드면 프레임 skip(직전 프레임 유지) — 깜빡임 방지.
                now = time.time()
                if now - _last_force[0] > 0.5:
                    _last_force[0] = now
                    _fg[1] = 0.0
                    fg2 = fg_backend()
                    if fg2 in ("webos", "adb") and fg2 != fg:
                        d2 = cap(fg2)
                        if d2:
                            cached = fg2
                            emit(d2, fg2)
        else:
            # dumpsys 불명 → 캐시→webos→adb 중 첫 유효 프레임
            order = [cached] if cached in ("webos", "adb") else []
            for b in ("webos", "adb"):
                if b not in order:
                    order.append(b)
            for b in order:
                d = cap(b)
                if d:
                    cached = b
                    emit(d, b)
                    break
        time.sleep(0.02)
    except (BrokenPipeError, IOError):
        break   # 백엔드가 파이프를 닫음 → 스트리머 종료
    except Exception:
        time.sleep(0.1)
'''


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
        self.capture_backend = (capture_backend or "auto").strip().lower()
        if self.capture_backend not in ("adb", "webos", "auto"):
            self.capture_backend = "auto"
        self.scripts_dir = scripts_dir
        self.agent_version = "BMWRSE Agent"
        self._connected = False
        self.last_error = ""
        # auto 모드: screen_id → 직전에 "내용 있는 프레임"을 준 백엔드. 평상시 1회 캡처로
        # 끝내고, 화면 전환(WebOS↔Android Setting) 순간에만 반대쪽을 추가 시도하기 위함.
        self._auto_backend: dict[int, str] = {}
        # dumpsys 포그라운드 판별 캐시 (TTL 내 재사용). topResumedActivity 가
        # webosprojectionhmi(=WebOS 프로젝션) 인지 settingshmi 등 네이티브인지로 백엔드 결정.
        self._fg_cache: Optional[Tuple[float, dict]] = None
        self._fg_ttl = 1.0
        # 디바이스 Android 컨테이너 이름 (lxc-attach 대상). 환경변수로 override 가능.
        self._android_container = os.environ.get("BMW_ANDROID_CONTAINER", "android1")
        # 속도 최적화 캐시: screen_id → SurfaceFlinger display id, adb root 1회 플래그.
        # 매 프레임 root/setenforce/get_display_ids(무거운 dumpsys) 반복 제거용.
        self._display_id_map: dict[int, str] = {}
        self._rooted = False
        # ── device-side 라이브 스트림 상태 (host python 스트리머) ──
        self._live_proc: Optional[subprocess.Popen] = None
        self._live_thread: Optional[threading.Thread] = None
        self._live_stop = threading.Event()
        self._live_lock = threading.Lock()
        self._latest_live_jpeg: Optional[bytes] = None
        self._live_frame_id = 0
        self._live_screen: Optional[int] = None  # 현재 스트리밍 중인 screen_id
        # 스트림 세대(epoch): 화면 전환/정지로 새 스트림이 뜨면 증가. 옛 리더 스레드가
        # join 타임아웃 후에도 살아 있어도 epoch 불일치면 최신 프레임에 못 쓰게 차단(겹침 방지).
        self._live_epoch = 0
        try:
            self._live_jpeg_q = int(os.environ.get("BMW_LIVE_JPEG_Q") or 75)
        except Exception:
            self._live_jpeg_q = 75
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
    def _ensure_root(self) -> None:
        """adb root + setenforce 0 를 연결당 1회만 수행 (매 프레임 반복 제거)."""
        if self._rooted:
            return
        self._adb_root()
        try:
            self._adb_shell("setenforce 0", timeout=5)
        except Exception:
            pass
        self._rooted = True

    def _ensure_display_ids(self) -> None:
        """screen_id → SurfaceFlinger display id 매핑을 1회 채운다 (비면 캡처 때까지 재시도)."""
        if self._display_id_map:
            return
        try:
            ids = self._get_display_ids()
            for idx, t in enumerate(ids):
                if t and t[0]:
                    self._display_id_map[idx] = t[0]
        except Exception as e:
            logger.debug("BMW display-id map populate failed: %s", e)

    def _display_id_for(self, screen_id: int) -> Optional[str]:
        self._ensure_display_ids()
        return self._display_id_map.get(int(screen_id))

    def _screencap_adb(self, screen_id: int) -> bytes:
        """android1 컨테이너 screencap → PNG 바이트.

        exec-out 으로 PNG 를 stdout 으로 직접 받아 temp 파일/pull/rm 왕복을 제거(1회 왕복).
        (adb shell 의 PTY LF→CRLF 변환에 의한 PNG 손상도 exec-out 으로 회피.)
        root/setenforce/display-id 조회는 연결당 1회 캐시.
        """
        self._ensure_root()
        display_id = self._display_id_for(screen_id)
        if display_id is not None:
            inner = f"screencap -d {display_id} -p"
        else:
            inner = "screencap -p"
        cmd = f"lxc-attach -n {self._android_container} -- {inner}"
        r = self._adb(["exec-out", cmd], text=False, timeout=20)
        data = r.stdout or b""
        if r.returncode != 0 or len(data) < 100:
            raise RuntimeError(
                f"BMW screencap 실패 (screen {screen_id}, rc={r.returncode}, {len(data)}B)")
        return bytes(data)

    def _screencap_webos(self, screen_id: int) -> bytes:
        """WebOS luna-send captureCompositorOutput → JPG 바이트.

        컴포지터가 /tmp/screenshot.JPG 를 비동기로 쓰므로, 고정 sleep 대신 exec-out cat 으로
        짧게 폴링하며 완전한 JPEG(SOI..EOI)이 보이는 즉시 반환(cp/pull/temp 파일 제거).
        """
        container = f"webos{int(screen_id) + 1}"
        remote = "/tmp/screenshot.JPG"
        try:
            self._adb_shell(f"lxc-attach -n {container} -- rm -f {remote}", timeout=5)
        except Exception:
            pass
        payload = r'{"output":"/tmp/screenshot.JPG","format":"JPG"}'
        self._adb_shell(
            f"lxc-attach -n {container} -- "
            f"luna-send -n 1 -f luna://com.webos.surfacemanager/captureCompositorOutput "
            f"'{payload}'",
            timeout=15,
        )
        cat_cmd = ["exec-out", f"lxc-attach -n {container} -- cat {remote}"]
        deadline = time.monotonic() + 2.5
        last = b""
        time.sleep(0.1)  # luna-send 직후 첫 write 여유
        while time.monotonic() < deadline:
            r = self._adb(cat_cmd, text=False, timeout=10)
            data = r.stdout or b""
            if len(data) > 1000 and data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9":
                return bytes(data)   # 완전한 JPEG
            last = data
            time.sleep(0.1)
        if last and last[:2] == b"\xff\xd8" and len(last) > 1000:
            return bytes(last)       # EOI 못 봤어도 SOI+충분한 크기면 부분 허용
        raise RuntimeError(f"WebOS 캡처 실패 (screen {screen_id})")

    @staticmethod
    def _has_content(data: Optional[bytes]) -> bool:
        """이미지가 디코딩 가능하고 '거의 검정'이 아니면 True (=현재 떠 있는 화면).

        WebOS 화면일 때 ADB screencap 은 무효/검정, Android Setting 화면일 때 WebOS 캡처는
        검정 → 이 판정으로 활성 백엔드를 가린다. 디코딩 실패(무효 PNG)도 '내용 없음'으로 본다.
        """
        if not data:
            return False
        try:
            from PIL import Image
            im = Image.open(io.BytesIO(data))
            im.load()
            g = im.convert("L")
            g.thumbnail((64, 64))
            px = list(g.getdata())
            if not px:
                return False
            dark = sum(1 for p in px if p <= 12)
            return dark < len(px) * 0.99   # 99% 이상이 검정이면 '내용 없음'
        except Exception:
            return False

    def _capture_one(self, backend: str, screen_id: int) -> Optional[bytes]:
        try:
            if backend == "webos":
                return self._screencap_webos(screen_id)
            return self._screencap_adb(screen_id)
        except Exception as e:
            logger.debug("BMW %s capture failed (screen %s): %s", backend, screen_id, e)
            return None

    def _dumpsys_foreground(self) -> dict:
        """android1 컨테이너 dumpsys 로 Display 별 활성 백엔드 매핑 반환.

        반환: {display_id: "webos"|"adb"} (Display #N ↔ screen_id N).
          - topResumedActivity 에 webosprojectionhmi 포함 → "webos"(WebOS 프로젝션)
          - 그 외 패키지(settingshmi 등 네이티브 Android) → "adb"
        TTL(1.5s) 캐시. dumpsys 실패/파싱불가 시 빈 dict(=unknown → 픽셀 휴리스틱 폴백).

        dumpsys 출력 형식:
          Display #0 (activities from top to bottom):
                topResumedActivity=ActivityRecord{... com.lge.app.car.settingshmi/... }
          Display #1 (...):
                topResumedActivity=ActivityRecord{... com.lge.app.car.webosprojectionhmi/... }
        """
        now = time.monotonic()
        if self._fg_cache and (now - self._fg_cache[0]) < self._fg_ttl:
            return self._fg_cache[1]
        mapping: dict = {}
        try:
            cmd = (f"lxc-attach -n {self._android_container} -- "
                   f"dumpsys activity activities | grep -iE 'Display #|topResumedActivity'")
            r = self._adb_shell(cmd, timeout=8)
            cur_display: Optional[int] = None
            for line in (r.stdout or "").splitlines():
                dm = re.search(r"Display\s+#(\d+)", line)
                if dm:
                    cur_display = int(dm.group(1))
                    continue
                if "topResumedActivity" in line and cur_display is not None:
                    be = "webos" if "webosprojectionhmi" in line else "adb"
                    mapping[cur_display] = be
                    cur_display = None
        except Exception as e:
            logger.debug("BMW dumpsys foreground probe failed: %s", e)
        self._fg_cache = (now, mapping)
        return mapping

    def _capture(self, screen_id: int, backend: Optional[str] = None) -> bytes:
        be = (backend or self.capture_backend or "auto").lower()

        if be in ("webos", "adb"):
            data = self._capture_one(be, screen_id)
            if data is not None:
                return data
            # 단일 백엔드 실패 시 반대쪽 1회 폴백
            other = "adb" if be == "webos" else "webos"
            data = self._capture_one(other, screen_id)
            if data is not None:
                return data
            raise RuntimeError(f"BMW capture failed (screen {screen_id}, backend {be})")

        # ── auto 빠른 경로: 캐시된 백엔드를 dumpsys 없이 먼저 캡처. 내용이 있으면 즉시 반환
        # (정상 상태 = 1회 캡처, dumpsys 0회). 비었을 때만 전환 의심 → dumpsys 로 판단. ──
        cached = self._auto_backend.get(screen_id)
        if cached in ("webos", "adb"):
            data = self._capture_one(cached, screen_id)
            if data is not None and self._has_content(data):
                return data

        # ── 전환 의심/최초: dumpsys per-display 힌트로 시도 순서 결정 + 내용검사로 확정 ──
        # Display #N ↔ screen_id N. 힌트가 있으면 그 백엔드를 1순위로(권위). 어느 쪽도
        # '내용'이 없으면(둘 다 검정) 힌트 백엔드 프레임 반환(어두운 WebOS 화면 보존).
        fg = self._dumpsys_foreground()
        hint = fg.get(screen_id)
        if hint in ("webos", "adb"):
            order = [hint, "adb" if hint == "webos" else "webos"]
        else:
            cached = self._auto_backend.get(screen_id)
            order = [cached] if cached in ("webos", "adb") else []
            for b in ("webos", "adb"):
                if b not in order:
                    order.append(b)

        captured: dict = {}
        for b in order:
            data = self._capture_one(b, screen_id)
            if data is None:
                continue
            captured[b] = data
            if self._has_content(data):
                if self._auto_backend.get(screen_id) != b:
                    logger.info("BMW auto: screen %s → %s (hint=%s)", screen_id, b, hint)
                self._auto_backend[screen_id] = b
                return data
        # 모두 내용 없음 → 권위(dumpsys 힌트) 백엔드 우선, 없으면 얻은 것 아무거나
        if hint in captured:
            return captured[hint]
        for b in order:
            if b in captured:
                return captured[b]
        raise RuntimeError(f"BMW auto capture failed (screen {screen_id})")

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
    def _list_adb_devices(self) -> list[tuple[str, str]]:
        """`adb devices` 파싱 → [(serial, state), ...]. -s 없이 서버 전체 조회."""
        try:
            cmd = [resolve_adb_path(), "devices"]
            r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", creationflags=_NO_WINDOW, timeout=10)
        except Exception as e:
            logger.warning("BMW connect: `adb devices` failed: %s", e)
            return []
        out = []
        for line in (r.stdout or "").splitlines():
            line = line.strip()
            if not line or line.lower().startswith("list of devices"):
                continue
            parts = line.split()
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
        return out

    def _connect_sync(self) -> bool:
        self.last_error = ""
        # 네트워크 ADB(serial 이 ip:port) 면 먼저 connect 시도
        if ":" in self.serial:
            try:
                cr = self._adb(["connect", self.serial], timeout=10)
                logger.info("BMW connect: adb connect %s → %s", self.serial,
                            (cr.stdout or "").strip())
            except Exception as e:
                logger.debug("BMW connect: adb connect %s failed: %s", self.serial, e)

        # adb 서버에서 serial 의 상태 확인 (get-state 단독보다 견고하게 devices 목록 파싱)
        devices = self._list_adb_devices()
        state = next((st for sn, st in devices if sn == self.serial), None)
        if state is None:
            # get-state 폴백 (일부 환경에서 devices 파싱이 비는 경우)
            try:
                r = self._adb(["get-state"], timeout=10)
                state = (r.stdout or "").strip() or None
            except Exception as e:
                state = None
                logger.debug("BMW connect: get-state fallback failed: %s", e)
        if state != "device":
            known = ", ".join(f"{sn}({st})" for sn, st in devices) or "(none)"
            self.last_error = (
                f"serial '{self.serial}' not in 'device' state (state={state}). "
                f"adb devices: {known}"
            )
            logger.warning("BMW connect failed: %s", self.last_error)
            return False

        logger.info("BMW connect: %s state=device — probing displays", self.serial)
        self._ensure_root()
        self._ensure_display_ids()  # display-id 매핑 1회 워밍(매 프레임 dumpsys 제거)
        # 터치 스크립트 best-effort 업로드
        try:
            self._upload_touch_scripts()
        except Exception as e:
            logger.debug("BMW touch script upload skipped: %s", e)
        # 해상도 조회 (실패해도 연결은 유지 — displays 는 get_info 가 항상 2개 반환)
        for sid in (0, 1):
            try:
                self._screen_sizes[sid] = self._get_screen_size(sid)
            except Exception as e:
                logger.debug("BMW connect: screen %s size probe failed: %s", sid, e)
                self._screen_sizes[sid] = self._default_res
        logger.info("BMW connect OK: %s screen_sizes=%s", self.serial, self._screen_sizes)
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
        try:
            self.stop_live_stream()
        except Exception:
            pass
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
    # Device-side live stream (host python streamer over `adb exec-out`)
    # ------------------------------------------------------------------
    def is_live_running(self) -> bool:
        t = self._live_thread
        p = self._live_proc
        return bool(t and t.is_alive() and p and p.poll() is None)

    def live_screen(self) -> Optional[int]:
        return self._live_screen

    def start_live_stream(self, screen_type=None) -> bool:
        """host python 스트리머를 push+실행하고 리더 스레드 기동. 이미 같은 screen이면 True.

        screen 이 바뀌면 기존 스트림을 정리하고 재시작. 실패 시 False(호출자 단발 캡처 폴백).
        """
        sid = self._screen_id(screen_type)
        with self._live_lock:
            if (self._live_thread and self._live_thread.is_alive()
                    and self._live_proc and self._live_proc.poll() is None
                    and self._live_screen == sid):
                return True
        # 다른 screen 이거나 죽었으면 정리 후 재시작
        self.stop_live_stream()
        try:
            self._ensure_root()
            display_id = self._display_id_for(sid) or ""
            script = (_BMW_LIVE_STREAMER
                      .replace("__SCREEN__", str(sid))
                      .replace("__ANDROID__", self._android_container)
                      .replace("__WEBOS__", f"webos{sid + 1}")
                      .replace("__DISPLAY_ID__", str(display_id)))
            # 스크립트를 host /tmp 에 push 후 실행 (shell 파싱 회피 — 컨테이너/host sh 무관).
            local = _TMP_DIR / f"bmw_streamer_s{sid}_{uuid.uuid4().hex[:6]}.py"
            local.write_text(script, encoding="utf-8")
            remote = f"/tmp/bmw_streamer_s{sid}.py"
            push = self._adb(["push", str(local), remote], timeout=20)
            try:
                os.remove(local)
            except OSError:
                pass
            if push.returncode != 0:
                logger.warning("BMW live: streamer push failed: %s", push.stderr)
                return False
            self._live_stop.clear()
            proc = subprocess.Popen(
                [resolve_adb_path(), "-s", self.serial, "exec-out",
                 "python3", "-u", remote],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
            )
            with self._live_lock:
                self._live_epoch += 1
                epoch = self._live_epoch
                self._live_proc = proc
                self._live_screen = sid
                self._latest_live_jpeg = None
            t = threading.Thread(target=self._live_reader, args=(proc, epoch), daemon=True)
            with self._live_lock:
                self._live_thread = t
            t.start()
            logger.info("BMW live stream started (screen=%d, display_id=%s)", sid, display_id or "-")
            return True
        except Exception as e:
            logger.warning("BMW live stream start failed: %r", e)
            self.stop_live_stream()
            return False

    def _live_reader(self, proc: subprocess.Popen, epoch: int) -> None:
        """exec-out stdout 에서 BMWF 프레임을 파싱 → (PNG면 JPEG 변환) 최신본 보관.

        프레임: b"BMWF" + struct('<BI', fmt, length) + data. fmt 0=JPEG / 1=PNG.
        epoch 가 현재 세대와 다르면(전환으로 새 스트림이 떴음) 쓰기 중단(겹침 방지).
        """
        from PIL import Image
        HDR = 9  # b"BMWF"(4) + '<BI'(5: fmt 1B + length 4B)
        stdout = proc.stdout
        buf = b""
        while not self._live_stop.is_set():
            try:
                chunk = stdout.read(262144) if stdout else b""
            except Exception:
                break
            if not chunk:
                break  # 파이프 닫힘/스트리머 종료
            buf += chunk
            while True:
                idx = buf.find(b"BMWF")
                if idx < 0:
                    if len(buf) > (1 << 24):
                        buf = buf[-8:]
                    break
                if len(buf) < idx + HDR:
                    break
                fmt, length = struct.unpack("<BI", buf[idx + 4: idx + HDR])
                if length <= 0 or length > (1 << 25):
                    buf = buf[idx + 4:]  # 비정상 길이 → 동기 재탐색
                    continue
                total = idx + HDR + length
                if len(buf) < total:
                    break
                data = buf[idx + HDR: total]
                buf = buf[total:]
                try:
                    if fmt == 0 and data[:2] == b"\xff\xd8":
                        jpg = data  # 이미 JPEG(WebOS)
                    else:
                        im = Image.open(io.BytesIO(data))
                        im.load()
                        bio = io.BytesIO()
                        im.convert("RGB").save(bio, format="JPEG", quality=self._live_jpeg_q)
                        jpg = bio.getvalue()
                    with self._live_lock:
                        if epoch != self._live_epoch:
                            return  # 옛 세대 리더 → 최신 프레임 오염 금지
                        self._latest_live_jpeg = jpg
                        self._live_frame_id += 1
                except Exception:
                    continue
        logger.info("BMW live reader exited (screen=%s, epoch=%d)", self._live_screen, epoch)

    def get_live_frame(self) -> Tuple[Optional[bytes], int]:
        with self._live_lock:
            return self._latest_live_jpeg, self._live_frame_id

    def stop_live_stream(self) -> None:
        self._live_stop.set()
        with self._live_lock:
            self._live_epoch += 1   # 살아남은 옛 리더의 프레임 쓰기 무효화
            proc = self._live_proc
            t = self._live_thread
            self._live_proc = None
            self._live_thread = None
            self._live_screen = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if t and t.is_alive():
            t.join(timeout=1.5)
        with self._live_lock:
            self._latest_live_jpeg = None

    async def async_start_live_stream(self, screen_type=None) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.start_live_stream, screen_type)

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
