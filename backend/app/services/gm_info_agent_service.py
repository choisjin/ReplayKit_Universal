"""GM Info Agent Service — GM Info(QNX) 유닛 TCP 제어.

레퍼런스: Reference/gm_info_agent/GM_INFO_QNX_Agent_python310.py
(`QNXStandaloneController`). 다른 에이전트(SSH+ksend, HKMC 커스텀 프레임)와 달리
**protobuf 스타일 페이로드 + 4바이트 빅엔디안 길이 프리픽스** 하나로 터치/하드키/캡처를
모두 처리한다.

와이어 포맷
-----------
프레임:  [4B big-endian payload_len][payload]

payload 공통 헤더 `08 f0 01 12 04 08 01 10 09 18 <op> 22 <len> <body>`
  - field1(0x08) = 0xF0 0x01 (=240)  : 서비스 id 로 추정 (고정)
  - field2(0x12) = 4바이트 서브메시지 `08 01 10 09` (고정)
  - field3(0x18) = op  : 0x17/0x01 = 세션 초기화, 0x05 = 입력 이벤트, 0x03 = 화면 캡처
  - field4(0x22) = body (길이 prefix)

입력 body 는 action 의 나열(repeated field1 = 0x0A + len + action):
  action = `08 <type> 12 <len> <inner> 18 <ms_delay> 28 00`
      type 0 = 하드키, 1 = 터치
      ms_delay = 이 action 을 수행한 **뒤** 다음 action 까지의 대기(ms).
                 하드키 press 의 ms_delay 가 곧 누름 지속시간, 터치 Down 의 ms_delay 가
                 곧 홀드 시간이 된다.
  하드키 inner = `08 <key_code> 10 <state>`        (state 1=press, 0=release)
  터치   inner = `08 <touch_action> 10 00 18 <x> 20 <y>`
      touch_action: 0=MoveTo, 2=Click(단발 탭), 4=Down, 5=Up
  → 여러 action 을 **한 페이로드**에 담아 보낸다. 하드키는 press+release,
    드래그는 Down + MoveTo×N + Up 이 한 번에 나간다.

캡처는 op=0x03 고정 페이로드를 보내면 응답 스트림 안에 PNG 가 실려 온다. 레퍼런스는
소켓 타임아웃(3초)까지 무조건 읽었지만, 여기서는 PNG 시그니처~IEND 를 만나는 즉시
중단해 미러링 프레임률을 확보한다.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import struct
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── 서브 커맨드 (ICAS/MIB 서비스와 동일한 값 — 프론트 하드키 패널 공용) ──
SHORT_KEY = 0x43
LONG_KEY = 0x44
PRESS_KEY = 0x41
RELEASE_KEY = 0x42

# ── 페이로드 op 코드 ──
_OP_INIT_A = 0x17
_OP_INIT_B = 0x01
_OP_CAPTURE = 0x03
_OP_INPUT = 0x05

# 액션 타입 (action 의 field1)
_ACT_KEY = 0x00
_ACT_TOUCH = 0x01

# 터치 action 코드 (터치 inner props 의 field1 — 레퍼런스 _touch_action 인자)
_TOUCH_MOVE = 0x00   # MoveTo
_TOUCH_CLICK = 0x02  # 단발 탭 (레퍼런스 touch())
_TOUCH_DOWN = 0x04   # Down (press)
_TOUCH_UP = 0x05     # Up (release)

# 드래그/슬라이드 기본 분할 (레퍼런스 touch_drag steps=4 / touch_slide steps=10, msPause=10)
_DRAG_STEPS = 4
_SLIDE_STEPS = 10
_SLIDE_PAUSE_MS = 10

# 하드키 기본 타이밍 (레퍼런스 key_press_by_alias / _ex 기준)
_SHORT_DURATION_MS = 300
_SHORT_PAUSE_MS = 200
_LONG_DURATION_MS = 2000
_LONG_PAUSE_MS = 2200

# ── GM Info 하드키 테이블 ──
# key = alias_map 코드(레퍼런스 key_press_by_alias). class 는 프론트 하드키 패널 표기용
# 기본 동작(short/long) — long_pause 는 레퍼런스가 키마다 다르게 쓰던 release pause.
GM_INFO_KEYS: dict[str, dict] = {
    "HOME":        {"key": 3,   "class": "short", "alias": "info35l_home"},
    "VOLUME_UP":   {"key": 32,  "class": "short", "alias": "info35l_vol_up"},
    "VOLUME_DOWN": {"key": 33,  "class": "short", "alias": "info35l_vol_down"},
    # 레퍼런스 HK_PREV = seek_up, HK_NEXT = seek_down (long 시 pause 4200ms)
    "SEEK_UP":     {"key": 34,  "class": "short", "alias": "info35l_seek_up",   "long_pause": 4200},
    "SEEK_DOWN":   {"key": 35,  "class": "short", "alias": "info35l_seek_down", "long_pause": 4200},
    "PHONE":       {"key": 102, "class": "short", "alias": "info35l_phone"},
    "POWER":       {"key": 120, "class": "short", "alias": "info35l_power"},
}

# 별칭 → 정식 키 이름 (레퍼런스 HK_* 함수명 / alias 문자열 모두 허용)
_KEY_ALIASES: dict[str, str] = {
    "HK_HOME": "HOME",
    "HK_VOLUME_UP": "VOLUME_UP",
    "HK_VOLUME_DOWN": "VOLUME_DOWN",
    "HK_SEEK_UP": "SEEK_UP",
    "HK_SEEK_DOWN": "SEEK_DOWN",
    "HK_PREV": "SEEK_UP",
    "HK_NEXT": "SEEK_DOWN",
    "HK_PHONE": "PHONE",
    "HK_POWER": "POWER",
    "PREV": "SEEK_UP",
    "NEXT": "SEEK_DOWN",
}
for _n, _i in GM_INFO_KEYS.items():
    _KEY_ALIASES[_i["alias"].upper()] = _n
del _n, _i

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def encode_varint(value: int) -> bytes:
    """protobuf base-128 varint (레퍼런스 encode_varint 동일)."""
    value = int(value)
    if value <= 0:
        return b"\x00"
    out = bytearray()
    while value:
        byte = value & 0x7F
        value >>= 7
        if value:
            byte |= 0x80
        out.append(byte)
    return bytes(out)


def _enable_tcp_keepalive(sock: socket.socket,
                          idle: int = 5, interval: int = 2, count: int = 3) -> None:
    """half-open(피어 전원 OFF) 연결을 OS 가 ~11초 안에 감지하게 한다.

    keepalive 가 없으면 유닛 전원을 내려도 sendall 이 조용히 성공해 `_connected` 가
    거짓 True 로 남고 자동 재연결이 발동하지 않는다 (HKMC6th 에서 겪은 회귀와 동일).
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        return
    try:
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        else:  # Linux / POSIX
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    except Exception:
        pass


def _read_png_size(data: bytes) -> Optional[tuple[int, int]]:
    """PNG 바이트에서 IHDR width/height 를 읽는다 (실패 시 None)."""
    idx = data.find(_PNG_MAGIC)
    if idx < 0 or len(data) < idx + 24:
        return None
    try:
        w, h = struct.unpack(">II", data[idx + 16: idx + 24])
    except struct.error:
        return None
    if w <= 0 or h <= 0:
        return None
    return int(w), int(h)


def _encode_image(png_bytes: bytes, fmt: str) -> bytes:
    """PNG 바이트 → 요청 포맷. png 면 그대로, jpeg 면 PIL 로 재인코딩."""
    if (fmt or "png").lower() != "jpeg":
        return png_bytes
    from PIL import Image
    img = Image.open(io.BytesIO(png_bytes))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    return buf.getvalue()


class GMInfoAgentService:
    """GM Info(QNX) 유닛 — TCP 단일 소켓으로 터치/하드키/캡처.

    API 는 ICAS/MIB 서비스와 호환(async_tap / async_long_press / async_send_key_by_name /
    async_screencap_bytes …)이라 재생 엔진의 `icas_*` 스텝 디스패처를 그대로 재사용한다.
    화면은 하나뿐이라 screen_type 은 "HU" 로 고정 취급하고 값은 무시한다.
    """

    default_screen = "HU"

    def __init__(self, host: str, port: int = 4445, device_id: str = "",
                 resolution: str = "1280x720",
                 device_model: str = "",
                 key_overrides: Optional[dict] = None,
                 capture_timeout: float = 8.0,
                 on_resolution_changed: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = int(port or 4445)
        self.device_id = device_id or f"GMInfo_{host}"
        self.device_model = device_model or ""
        self._resolution = str(resolution or "1280x720").upper()
        self._parse_resolution()
        self.agent_version = "GM Info QNX Agent"

        self._socket: Optional[socket.socket] = None
        self._connected = False
        # 소켓 하나를 입력·캡처가 공유한다 — 응답 인터리브를 막기 위해 전 구간 직렬화.
        self._io_lock = threading.RLock()
        # 미러링 적응형 페이싱(main.py `_adaptive_ssh_pace`)이 읽는 마지막 입력 시각.
        self.last_input_ts = 0.0

        try:
            self.capture_timeout = float(
                os.environ.get("GM_INFO_CAPTURE_TIMEOUT", capture_timeout))
        except Exception:
            self.capture_timeout = 8.0
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})
        # 첫 캡처에서 실제 화면 크기를 알게 되면 디바이스 info 에 반영하기 위한 콜백.
        # 등록 해상도가 틀린 채로 남으면 프론트 미러의 클릭 좌표 환산이 어긋난다.
        self._on_resolution_changed = on_resolution_changed

    # ------------------------------------------------------------------
    # 기본 속성
    # ------------------------------------------------------------------
    def _parse_resolution(self) -> None:
        try:
            w_s, h_s = self._resolution.upper().split("X")
            self.res_x, self.res_y = int(w_s), int(h_s)
        except Exception:
            self.res_x, self.res_y = 1280, 720

    @property
    def resolution(self) -> str:
        return self._resolution

    @resolution.setter
    def resolution(self, value: str) -> None:
        self._resolution = str(value).upper()
        self._parse_resolution()

    @property
    def is_connected(self) -> bool:
        return self._connected and self._socket is not None

    def _maybe_autoupdate_resolution(self, width: int, height: int) -> None:
        """캡처 PNG 실제 크기로 등록 해상도를 보정 (프론트 좌표 매핑용)."""
        if width <= 0 or height <= 0 or (width == self.res_x and height == self.res_y):
            return
        old = self._resolution
        self._resolution = f"{width}x{height}"
        self._parse_resolution()
        logger.info("GM Info resolution auto-detected: %s → %s (%s)",
                    old, self._resolution, self.device_id)
        if self._on_resolution_changed:
            try:
                self._on_resolution_changed(self._resolution)
            except Exception as e:
                logger.warning("GM Info resolution callback failed: %s", e)

    # ------------------------------------------------------------------
    # 연결 / 해제
    # ------------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> bool:
        with self._io_lock:
            self._close_socket()
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                _enable_tcp_keepalive(sock)
                sock.settimeout(timeout)
                sock.connect((self.host, self.port))
            except Exception as e:
                logger.warning("GM Info connect failed (%s:%s): %r", self.host, self.port, e)
                self._connected = False
                return False
            self._socket = sock
            try:
                self._init_session()
            except Exception as e:
                logger.warning("GM Info handshake failed (%s:%s): %r", self.host, self.port, e)
                self._close_socket()
                self._connected = False
                return False
            self._connected = True
        logger.info("GM Info connected: %s (%s:%d)", self.device_id, self.host, self.port)
        return True

    def _init_session(self) -> None:
        """레퍼런스 `_init_session` — 세션 초기화 페이로드 2발."""
        self._send_payload(self._envelope(_OP_INIT_A, b""))
        time.sleep(0.1)   # 레퍼런스 _send_payload 가 매 전송 뒤 두던 간격
        self._send_payload(self._envelope(_OP_INIT_B, b""))
        time.sleep(0.5)
        self._drain()

    def disconnect(self) -> None:
        with self._io_lock:
            self._close_socket()
            self._connected = False
        logger.info("GM Info disconnected: %s", self.device_id)

    def _close_socket(self) -> None:
        sock, self._socket = self._socket, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

    def _ensure_socket(self) -> socket.socket:
        """살아있는 소켓 반환 — 끊겼으면 1회 재연결 시도."""
        with self._io_lock:
            if self._socket is not None:
                return self._socket
            logger.info("GM Info socket lost — reconnecting %s", self.device_id)
            if not self.connect():
                raise RuntimeError(
                    f"GM Info device {self.device_id} not connected ({self.host}:{self.port})")
            assert self._socket is not None
            return self._socket

    async def async_connect(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.connect(timeout))

    async def async_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    # ------------------------------------------------------------------
    # 와이어 I/O
    # ------------------------------------------------------------------
    @staticmethod
    def _envelope(op: int, body: bytes) -> bytes:
        """공통 헤더 + op + body(길이 prefix) 페이로드 조립."""
        return (b"\x08\xf0\x01\x12\x04\x08\x01\x10\x09\x18" + bytes([op])
                + b"\x22" + encode_varint(len(body)) + body)

    def _send_payload(self, payload: bytes) -> None:
        """[4B len][payload] 전송. 소켓이 죽었으면 닫아서 다음 호출이 재연결하게 한다."""
        sock = self._socket
        if sock is None:
            raise RuntimeError(f"GM Info device {self.device_id} not connected")
        try:
            sock.sendall(struct.pack(">I", len(payload)) + payload)
        except Exception:
            self._close_socket()
            self._connected = False
            raise

    def _drain(self) -> None:
        """소켓 수신 버퍼 비우기.

        입력 페이로드에 대한 ack 가 남아 있으면 다음 캡처 응답 앞에 섞여 들어와
        PNG 파싱을 방해한다. 캡처 직전에 항상 비운다.
        """
        sock = self._socket
        if sock is None:
            return
        old = sock.gettimeout()
        try:
            sock.setblocking(False)
            while True:
                try:
                    if not sock.recv(65536):
                        break  # 피어가 닫음
                except (BlockingIOError, socket.timeout):
                    break
                except OSError:
                    break
        finally:
            try:
                sock.setblocking(True)
                sock.settimeout(old)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 입력 (터치 / 하드키)
    # ------------------------------------------------------------------
    @staticmethod
    def _action(act_type: int, inner: bytes, time_ms: int) -> bytes:
        """repeated action 한 개 (`0A <len> 08 <type> 12 <len> <inner> 18 <time> 28 00`)."""
        body = (b"\x08" + encode_varint(act_type)
                + b"\x12" + encode_varint(len(inner)) + inner
                + b"\x18" + encode_varint(time_ms) + b"\x28\x00")
        return b"\x0a" + encode_varint(len(body)) + body

    def _send_input(self, actions: bytes) -> None:
        with self._io_lock:
            self._ensure_socket()
            self._send_payload(self._envelope(_OP_INPUT, actions))
            self.last_input_ts = time.time()
            # 레퍼런스 _send_payload 가 매 전송 후 두던 간격 — 유닛이 이벤트를 흘리지 않게.
            time.sleep(0.1)

    def _touch_action(self, action: int, x: int, y: int, ms_delay: int = 0) -> bytes:
        """터치 action 한 개 (레퍼런스 `_touch_action`)."""
        props = (b"\x08" + encode_varint(action) + b"\x10\x00"
                 + b"\x18" + encode_varint(int(x))
                 + b"\x20" + encode_varint(int(y)))
        return self._action(_ACT_TOUCH, props, max(0, int(ms_delay)))

    def tap(self, x: int, y: int, screen_type: str = "HU", **kwargs) -> None:
        """단일 탭 (레퍼런스 `touch` — Down/Up 없이 Click action 하나)."""
        self._send_input(self._touch_action(_TOUCH_CLICK, x, y, 0))
        logger.info("[GM TAP] (%d,%d) device=%s", int(x), int(y), self.device_id)

    def touch_press(self, x: int, y: int, hold_ms: int = 0) -> None:
        """Down 이벤트만 전송 (레퍼런스 `touch_press`)."""
        self._send_input(self._touch_action(_TOUCH_DOWN, x, y, hold_ms))

    def touch_release(self, x: int, y: int, ms_delay: int = 0) -> None:
        """Up 이벤트만 전송 (레퍼런스 `touch_release`)."""
        self._send_input(self._touch_action(_TOUCH_UP, x, y, ms_delay))

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "HU", **kwargs) -> None:
        """롱터치 — Down(ms_delay=지속시간) + Up 을 한 페이로드로.

        action 의 ms_delay 는 '수행 후 다음 action 까지의 대기'라, Down 에 지속시간을
        실으면 그만큼 누른 상태가 유지된다 (하드키 press 의 duration 과 같은 의미).
        """
        dur = max(1, int(duration_ms))
        self._send_input(self._touch_action(_TOUCH_DOWN, x, y, dur)
                         + self._touch_action(_TOUCH_UP, x, y, 0))
        logger.info("[GM LONG_PRESS] (%d,%d) %dms device=%s", int(x), int(y), dur, self.device_id)

    def repeat_tap(self, x: int, y: int, count: int = 5,
                   interval_ms: int = 100, screen_type: str = "HU", **kwargs) -> None:
        """같은 좌표 연속 탭."""
        n = max(1, int(count))
        gap = max(0, int(interval_ms)) / 1000.0
        for i in range(n):
            self.tap(x, y, screen_type)
            if i < n - 1 and gap > 0:
                time.sleep(gap)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "HU", duration_ms: int = 0,
              hold_ms: int = 0, steps: int = 0, **kwargs) -> None:
        """스와이프/드래그 — Down + MoveTo×N + Up 을 한 페이로드로 전송.

        레퍼런스 `touch_drag`(steps=4, 무지연) / `touch_slide`(steps=10, 스텝당 msPause)
        와 동일한 구조이며, 두 변형을 duration_ms 하나로 흡수한다:
          - duration_ms<=0 → drag  (4분할, 지연 없음 = 최속)
          - duration_ms>0  → slide (10분할, 스텝당 duration_ms/steps 대기)
        hold_ms>0 이면 Down 직후 그만큼 누른 채 대기했다가 이동한다(드래그앤드롭).
        """
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        dur = max(0, int(duration_ms))
        n = int(steps) if int(steps) > 0 else (_SLIDE_STEPS if dur > 0 else _DRAG_STEPS)
        pause = max(0, dur // n) if dur > 0 else 0
        down_delay = max(0, int(hold_ms)) if int(hold_ms) > 0 else pause

        actions = self._touch_action(_TOUCH_DOWN, x1, y1, down_delay)
        for s in range(1, n):
            actions += self._touch_action(
                _TOUCH_MOVE, x1 + (x2 - x1) * s // n, y1 + (y2 - y1) * s // n, pause)
        actions += self._touch_action(_TOUCH_MOVE, x2, y2, pause)
        actions += self._touch_action(_TOUCH_UP, x2, y2, pause)
        self._send_input(actions)
        logger.info("[GM SWIPE] (%d,%d)→(%d,%d) steps=%d pause=%dms hold=%dms device=%s",
                    x1, y1, x2, y2, n, pause, down_delay, self.device_id)

    # ── 하드키 ──
    def resolve_key(self, key_name: str) -> Optional[dict]:
        """키 이름/별칭 → 스펙 dict (per-device override 병합)."""
        name = (key_name or "").strip()
        if not name:
            return None
        canon = name if name in GM_INFO_KEYS else _KEY_ALIASES.get(name.upper())
        if not canon:
            canon = _KEY_ALIASES.get(name.upper().replace("-", "_"))
        if not canon or canon not in GM_INFO_KEYS:
            return None
        info = dict(GM_INFO_KEYS[canon])
        ov = self._key_overrides.get(canon) or {}
        if "key" in ov and ov["key"] is not None:
            try:
                info["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if ov.get("class") in ("short", "long"):
            info["class"] = ov["class"]
        info["name"] = canon
        return info

    def set_key_overrides(self, overrides: Optional[dict[str, dict]]) -> None:
        self._key_overrides = dict(overrides or {})

    def get_key_overrides(self) -> dict[str, dict]:
        return dict(self._key_overrides)

    def send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                         screen_type: Optional[str] = None,
                         direction: Optional[int] = None,
                         hold_ms: Optional[int] = None) -> None:
        """이름으로 하드키 전송 (press + release 를 한 페이로드에 담아 송신).

        sub_cmd=LONG_KEY(0x44) 또는 hold_ms 지정 시 long press. hold_ms 가 우선.
        """
        info = self.resolve_key(key_name)
        if info is None:
            raise ValueError(f"GM Info: unknown key '{key_name}' "
                             f"(available: {', '.join(GM_INFO_KEYS)})")
        is_long = (sub_cmd == LONG_KEY) or (info.get("class") == "long" and sub_cmd != SHORT_KEY)
        if hold_ms is not None and int(hold_ms) > 0:
            duration = int(hold_ms)
            is_long = True
        else:
            duration = _LONG_DURATION_MS if is_long else _SHORT_DURATION_MS
        pause = (int(info.get("long_pause", _LONG_PAUSE_MS)) if is_long else _SHORT_PAUSE_MS)
        self.send_key(int(info["key"]), duration, pause)
        logger.info("[GM KEY] %s (code=%d %s dur=%dms pause=%dms) device=%s",
                    info["name"], int(info["key"]), "long" if is_long else "short",
                    duration, pause, self.device_id)

    def send_key(self, key_code: int, duration_ms: int = _SHORT_DURATION_MS,
                 pause_ms: int = _SHORT_PAUSE_MS, *args, **kwargs) -> None:
        """키코드 직접 전송 (레퍼런스 `key_press_by_alias_ex` 와 동일 구성).

        press(state=1, duration) + release(state=0, pause) 두 action 을 한 페이로드로.
        """
        code = int(key_code)

        def _inner(state: int) -> bytes:
            return b"\x08" + encode_varint(code) + b"\x10" + encode_varint(state)

        actions = (self._action(_ACT_KEY, _inner(1), max(1, int(duration_ms)))
                   + self._action(_ACT_KEY, _inner(0), max(0, int(pause_ms))))
        self._send_input(actions)

    # ------------------------------------------------------------------
    # 캡처
    # ------------------------------------------------------------------
    # op=0x03 캡처 요청 body (레퍼런스 cmd3 의 0x22 이후 20바이트 고정 페이로드)
    _CAPTURE_BODY = (b"\x08\x00\x10\x00\x18\x00\x20\x00\x28\x00"
                     b"\x30\x00\x38\x06\x40\x00\x48\x00\x5a\x00")

    def screencap_bytes(self, screen_type: str = "HU", fmt: str = "png",
                        timeout: Optional[float] = None) -> bytes:
        """현재 화면을 PNG/JPEG 바이트로 반환."""
        deadline = time.time() + float(timeout or self.capture_timeout)
        t0 = time.time()
        with self._io_lock:
            sock = self._ensure_socket()
            self._drain()
            self._send_payload(self._envelope(_OP_CAPTURE, self._CAPTURE_BODY))
            png = self._recv_png(sock, deadline)
        size = _read_png_size(png)
        if size:
            self._maybe_autoupdate_resolution(*size)
        out = _encode_image(png, fmt)
        logger.debug("GM Info screencap %s: %s %dB in %.0fms",
                     self.device_id, f"{size[0]}x{size[1]}" if size else "?",
                     len(out), (time.time() - t0) * 1000)
        return out

    def _recv_png(self, sock: socket.socket, deadline: float) -> bytes:
        """응답 스트림에서 PNG 를 추출한다 (IEND 를 만나는 즉시 종료).

        레퍼런스는 소켓 타임아웃까지 무조건 읽어 프레임당 3초를 버렸다. 미러링에서
        그 비용이 그대로 fps 가 되므로 완결 시점을 직접 판정한다.
        """
        buf = b""
        start = -1
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            try:
                sock.settimeout(min(1.0, remaining))
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                self._close_socket()
                self._connected = False
                raise
            if not chunk:
                self._close_socket()
                self._connected = False
                raise RuntimeError(f"GM Info capture: peer closed ({self.device_id})")
            buf += chunk
            if start < 0:
                start = buf.find(_PNG_MAGIC)
            if start >= 0:
                end = buf.find(b"IEND", start)
                if end >= 0:
                    return buf[start:end + 8]
        if start >= 0:
            # IEND 를 못 봤지만 PNG 는 시작됐다 — 잘린 프레임은 디코드가 깨지므로 실패 처리.
            raise RuntimeError(
                f"GM Info capture truncated: {len(buf) - start}B without IEND ({self.device_id})")
        raise RuntimeError(
            f"GM Info capture failed: no PNG in {len(buf)}B response ({self.device_id})")

    # ------------------------------------------------------------------
    # Async wrappers (ICAS/MIB API 호환 — 재생 엔진 디스패처 공용)
    # ------------------------------------------------------------------
    async def async_screencap_bytes(self, screen_type: str = "HU", fmt: str = "png",
                                    timeout: Optional[float] = None) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.screencap_bytes(screen_type, fmt, timeout))

    async def async_tap(self, x: int, y: int, screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.tap, x, y, screen_type)

    async def async_long_press(self, x: int, y: int, duration_ms: int = 3000,
                               screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.long_press, x, y, duration_ms, screen_type)

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type: str = "HU", duration_ms: int = 0,
                          hold_ms: int = 0, **kwargs) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, lambda: self.swipe(x1, y1, x2, y2, screen_type, duration_ms, hold_ms))

    async def async_repeat_tap(self, x: int, y: int, count: int = 5,
                               interval_ms: int = 100, screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.repeat_tap, x, y, count, interval_ms, screen_type)

    async def async_send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                                     screen_type: Optional[str] = None,
                                     direction: Optional[int] = None,
                                     hold_ms: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.send_key_by_name, key_name, sub_cmd, screen_type, direction, hold_ms)

    async def async_send_key(self, cmd: int, sub_cmd: int = SHORT_KEY, key_data: int = 0,
                             *args, **kwargs) -> None:
        """ICAS 호환 시그니처 — (cmd, sub_cmd, key_data) 중 cmd 를 키코드로 사용."""
        loop = asyncio.get_event_loop()
        duration = _LONG_DURATION_MS if sub_cmd == LONG_KEY else _SHORT_DURATION_MS
        pause = _LONG_PAUSE_MS if sub_cmd == LONG_KEY else _SHORT_PAUSE_MS
        await loop.run_in_executor(None, self.send_key, int(cmd), duration, pause)

    # ------------------------------------------------------------------
    def get_info(self) -> dict:
        return {
            "type": "gm_info_agent",
            "host": self.host,
            "port": self.port,
            "device_id": self.device_id,
            "device_model": self.device_model,
            "connected": self.is_connected,
            "agent_version": self.agent_version,
            "resolution": self._resolution,
            "input_supported": True,
            "swipe_supported": True,
            "screens": {"HU": {"width": self.res_x, "height": self.res_y}},
        }
