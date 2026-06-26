"""iSAP Agent protocol service — Connected Wide 계열 IVI 디바이스용 TCP 프로토콜.

References/Connected_Wide_iSAP_Agent.docx 문서의 프로토콜 정의를 구현한다.
HKMC6th 프로토콜과 유사하지만 Monitor 필드(1 byte)와 포트 할당(20000~20004)이 다르다.

- START/END: 0x61 / 0x6F (2 bytes each)
- Packet Length: 4 bytes BE (cmd 필드부터 CRC 이전까지)
- CRC16: key 0xC659, 16bit
- Port 할당:
    20000 = 전석(front_center)
    20001 = 후석 좌측(rear_left)
    20002 = 후석 우측(rear_right)
    20003 = 클러스터(cluster)
    20004 = HUD(hud)
- Monitor 필드(1 byte): 0x00=front, 0x01=rear_l, 0x02=rear_r, 0x03=cluster, 0x04=hud
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import threading
import time
import queue
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (표 68 ~ 표 138)
# ---------------------------------------------------------------------------
START_BIT = 0x61
END_BIT = 0x6F

# Main Commands (표 82)
NOTI_CONNECTED = 0x5E
CMD_HKEY = 0x60
CMD_GETIMG = 0x6A
CMD_SWRC = 0x70
CMD_OPSW = 0x71
CMD_GRIPSW = 0x72
CMD_CCP = 0x80
CMD_RRC = 0x90
CMD_MIRROR = 0x92
CMD_OVERHEADCONSOLE = 0x94
CMD_GETVERSION = 0xA0
CMD_GETMAINVERSION = 0xA1
CMD_GETSUBVERSION = 0xA2
CMD_GETSCREENWIDTHHEIGHT = 0xA3
CMD_LCDTOUCHEXT = 0xB0
CMD_LCDTOUCH_DRAG = 0xD6
CMD_LCDTOUCH_FAST = 0xD7

# Sub Commands (표 72)
RELEASE_KEY = 0x41
PRESS_KEY = 0x42
SHORT_KEY = 0x43
LONG_KEY = 0x44
KNOB_KEY = 0x80

# Touch actions (표 134)
TOUCH_RELEASE = 0x41
TOUCH_PRESS = 0x42
TOUCH_MOVE = 0x45

# GET IMAGE capture format sub-commands (표 86)
IMG_PNG = 0x42
IMG_BMP24 = 0x43
IMG_JPEG = 0x44

# Response values (표 74)
RESPONSE_FAIL = 0x20
RESPONSE_SUCCESS = 0x21
RESPONSE_NOTHING = 0xFE

# DIR field (표 79)
DIR_CLOCKWISE = 0x00
DIR_COUNTER_CW = 0x01

# Monitor field (표 78) — 1 byte
MONITOR_MAP: dict[str, int] = {
    "front_center": 0x00,
    "rear_left":    0x01,
    "rear_right":   0x02,
    "cluster":      0x03,
    "hud":          0x04,
}

# Screen type → TCP port (표 68)
SCREEN_PORT_MAP: dict[str, int] = {
    "front_center": 20000,
    "rear_left":    20001,
    "rear_right":   20002,
    "cluster":      20003,
    "hud":          20004,
}


# Key tables from spec (Connected_Wide_iSAP_Agent.docx).
# 각 키의 group은 이름 앞부분(첫 "_" 전)으로 판별된다.
# - 이 딕셔너리는 global default 이며, 각 디바이스는 info["isap_keys"]로
#   cmd/key/visible을 개별 오버라이드 할 수 있다 (차종별 키 값 차이 대응).
ISAP_KEYS: dict[str, dict] = {
    # MKBD (CMD_HKEY=0x60) — 표 113
    "MKBD_HOME":        {"cmd": CMD_HKEY, "key": 0x0A},
    "MKBD_MAP":         {"cmd": CMD_HKEY, "key": 0x0B},
    "MKBD_NAV":         {"cmd": CMD_HKEY, "key": 0x0C},
    "MKBD_RADIO":       {"cmd": CMD_HKEY, "key": 0x0D},
    "MKBD_MEDIA":       {"cmd": CMD_HKEY, "key": 0x0E},
    "MKBD_CUSTOM":      {"cmd": CMD_HKEY, "key": 0x11},
    "MKBD_SETUP":       {"cmd": CMD_HKEY, "key": 0x12},
    "MKBD_SEARCH":      {"cmd": CMD_HKEY, "key": 0x13},
    "MKBD_SEEK_UP":     {"cmd": CMD_HKEY, "key": 0x14},
    "MKBD_TRACK_UP":    {"cmd": CMD_HKEY, "key": 0x15},
    "MKBD_SEEK_DOWN":   {"cmd": CMD_HKEY, "key": 0x16},
    "MKBD_TRACK_DOWN":  {"cmd": CMD_HKEY, "key": 0x17},
    "MKBD_POWER":       {"cmd": CMD_HKEY, "key": 0x1D},
    "MKBD_TUNE_PUSH":   {"cmd": CMD_HKEY, "key": 0x1E},
    "MKBD_VOLUME":      {"cmd": CMD_HKEY, "key": 0x01, "dial": True},
    "MKBD_TUNE":        {"cmd": CMD_HKEY, "key": 0x04, "dial": True},

    # CCP (CMD_CCP=0x80) — 표 115
    "CCP_UP":           {"cmd": CMD_CCP, "key": 0x00},
    "CCP_DOWN":         {"cmd": CMD_CCP, "key": 0x01},
    "CCP_LEFT":         {"cmd": CMD_CCP, "key": 0x03},
    "CCP_RIGHT":        {"cmd": CMD_CCP, "key": 0x06},
    "CCP_ENTER":        {"cmd": CMD_CCP, "key": 0x08},
    "CCP_BACK":         {"cmd": CMD_CCP, "key": 0x09},
    "CCP_MENU":         {"cmd": CMD_CCP, "key": 0x0A},
    "CCP_HOME":         {"cmd": CMD_CCP, "key": 0x14},
    "CCP_POWER":        {"cmd": CMD_CCP, "key": 0x19},
    "CCP_TUNE_PUSH":    {"cmd": CMD_CCP, "key": 0x1E},
    "CCP_JOGDIAL":      {"cmd": CMD_CCP, "key": 0x00, "dial": True},
    "CCP_VOLUME":       {"cmd": CMD_CCP, "key": 0x01, "dial": True},
    "CCP_TUNE":         {"cmd": CMD_CCP, "key": 0x04, "dial": True},

    # RRC (CMD_RRC=0x90) — 표 118, 2 Monitor용 리어 리모콘
    "RRC_UP":           {"cmd": CMD_RRC, "key": 0x00},
    "RRC_DOWN":         {"cmd": CMD_RRC, "key": 0x01},
    "RRC_LEFT":         {"cmd": CMD_RRC, "key": 0x03},
    "RRC_RIGHT":        {"cmd": CMD_RRC, "key": 0x06},
    "RRC_ENTER":        {"cmd": CMD_RRC, "key": 0x08},
    "RRC_BACK":         {"cmd": CMD_RRC, "key": 0x09},
    "RRC_MENU":         {"cmd": CMD_RRC, "key": 0x0A},
    "RRC_HOME":         {"cmd": CMD_RRC, "key": 0x14},
    "RRC_POWER_LEFT":   {"cmd": CMD_RRC, "key": 0x1A},
    "RRC_POWER_RIGHT":  {"cmd": CMD_RRC, "key": 0x1B},
    "RRC_VOLUME_LEFT":  {"cmd": CMD_RRC, "key": 0x17},
    "RRC_VOLUME_RIGHT": {"cmd": CMD_RRC, "key": 0x18},
    "RRC_JOGDIAL":          {"cmd": CMD_RRC, "key": 0x00, "dial": True},
    "RRC_VOLUME_LEFT_DIAL":  {"cmd": CMD_RRC, "key": 0x02, "dial": True},
    "RRC_VOLUME_RIGHT_DIAL": {"cmd": CMD_RRC, "key": 0x03, "dial": True},

    # SWRC (CMD_SWRC=0x70) — 표 121, 스티어링 휠 리모콘
    "SWRC_PTT":         {"cmd": CMD_SWRC, "key": 0x22},
    "SWRC_MODE":        {"cmd": CMD_SWRC, "key": 0x23},
    "SWRC_MUTE":        {"cmd": CMD_SWRC, "key": 0x24},
    "SWRC_SEEK_UP":     {"cmd": CMD_SWRC, "key": 0x0F},
    "SWRC_SEEK_DOWN":   {"cmd": CMD_SWRC, "key": 0x10},
    "SWRC_CUSTOM":      {"cmd": CMD_SWRC, "key": 0x11},
    "SWRC_SEND":        {"cmd": CMD_SWRC, "key": 0x25},
    "SWRC_END":         {"cmd": CMD_SWRC, "key": 0x26},
    "SWRC_VOLUME":      {"cmd": CMD_SWRC, "key": 0x01, "dial": True},

    # MIRROR (CMD_MIRROR=0x92) — 표 123
    "MIRROR_SOS":                   {"cmd": CMD_MIRROR, "key": 0x27},
    "MIRROR_CONCIERGE":              {"cmd": CMD_MIRROR, "key": 0x2A},
    "MIRROR_CONCIERGE_POI":          {"cmd": CMD_MIRROR, "key": 0x2B},
    "MIRROR_VOICE_LOCAL_SEARCH":     {"cmd": CMD_MIRROR, "key": 0x2C},
    "MIRROR_ROADSIDE_ASSISTANT":     {"cmd": CMD_MIRROR, "key": 0x2D},

    # OVERHEAD CONSOLE (CMD_OVERHEADCONSOLE=0x94) — 표 125
    "OVERHEAD_SOS":                 {"cmd": CMD_OVERHEADCONSOLE, "key": 0x27},
    "OVERHEAD_CONCIERGE":           {"cmd": CMD_OVERHEADCONSOLE, "key": 0x2A},
    "OVERHEAD_CONCIERGE_POI":       {"cmd": CMD_OVERHEADCONSOLE, "key": 0x2B},
    "OVERHEAD_VOICE_LOCAL_SEARCH":  {"cmd": CMD_OVERHEADCONSOLE, "key": 0x2C},
    "OVERHEAD_ROADSIDE_ASSISTANT":  {"cmd": CMD_OVERHEADCONSOLE, "key": 0x2D},

    # TRIP SWITCH (CMD_SWRC=0x70, Monitor 필드로 구분) — 표 126
    "TRIP_MENU":         {"cmd": CMD_SWRC, "key": 0x31},
    "TRIP_UP":           {"cmd": CMD_SWRC, "key": 0x32},
    "TRIP_DOWN":         {"cmd": CMD_SWRC, "key": 0x33},
    "TRIP_OK":           {"cmd": CMD_SWRC, "key": 0x34},
    "TRIP_LFA":          {"cmd": CMD_SWRC, "key": 0x35},
    "TRIP_CANCEL":       {"cmd": CMD_SWRC, "key": 0x36},
    "TRIP_SET_MINUS":    {"cmd": CMD_SWRC, "key": 0x37},
    "TRIP_RES_PLUS":     {"cmd": CMD_SWRC, "key": 0x38},
    "TRIP_CRUISE":       {"cmd": CMD_SWRC, "key": 0x39},
    "TRIP_CRUISE_SLD":   {"cmd": CMD_SWRC, "key": 0x3A},
    "TRIP_SCC_DIST":     {"cmd": CMD_SWRC, "key": 0x3B},
    "TRIP_PADD_DN":      {"cmd": CMD_SWRC, "key": 0x3C},
    "TRIP_PADD_UP":      {"cmd": CMD_SWRC, "key": 0x3D},
    "TRIP_ERROR":        {"cmd": CMD_SWRC, "key": 0x3E},
    "TRIP_RESERVED":     {"cmd": CMD_SWRC, "key": 0x3F},

    # GRIP CONTROL SWITCH (CMD_GRIPSW=0x72) — 표 128
    "GRIP_RIGHT":        {"cmd": CMD_GRIPSW, "key": 0x41},
    "GRIP_LEFT":         {"cmd": CMD_GRIPSW, "key": 0x42},
    "GRIP_PUSH":         {"cmd": CMD_GRIPSW, "key": 0x43},

    # OPTICAL SWITCH (CMD_OPSW=0x71) — 표 130
    "OPTICAL_SWIPE_UP":    {"cmd": CMD_OPSW, "key": 0x51},
    "OPTICAL_SWIPE_DOWN":  {"cmd": CMD_OPSW, "key": 0x52},
    "OPTICAL_SWIPE_LEFT":  {"cmd": CMD_OPSW, "key": 0x53},
    "OPTICAL_SWIPE_RIGHT": {"cmd": CMD_OPSW, "key": 0x54},
    "OPTICAL_TOUCH":       {"cmd": CMD_OPSW, "key": 0x55},
    "OPTICAL_OK":          {"cmd": CMD_OPSW, "key": 0x56},

    # RHEOSTAT SWITCH (CMD_HKEY=0x60 per spec sample; 별도 CMD_RHEOSTAT 언급되나
    # 실제 패킷 샘플은 0x60을 사용) — 표 131
    "RHEOSTAT_UP":       {"cmd": CMD_HKEY, "key": 0x61},
    "RHEOSTAT_DOWN":     {"cmd": CMD_HKEY, "key": 0x62},
}


def _calc_crc16(data: list[int]) -> int:
    """CRC16 with 0xC659 polynomial (공통 IVI agent 프로토콜)."""
    crc = 0xFFFF
    key = 0xC659
    for b in data:
        tmp = (b & 0xFF) ^ (crc & 0x00FF)
        for _ in range(8):
            if tmp & 1:
                tmp = (tmp >> 1) ^ key
            else:
                tmp = tmp >> 1
        crc = (crc >> 8) ^ tmp
    return crc


def _parse_int32(data: list[int], offset: int) -> int:
    return ((data[offset] << 24) | (data[offset + 1] << 16) |
            (data[offset + 2] << 8) | data[offset + 3])


class ISAPAgentService:
    """TCP socket client for iSAP Agent (Connected Wide IVI systems).

    하나의 인스턴스가 하나의 TCP 연결(=하나의 모니터)에 대응한다.
    - screencap, tap, swipe, hardkey 지원
    - 각 호출은 Monitor 필드(1 byte)로 대상 모니터를 지정
    """

    _DEFAULT_SCREEN_SIZES = {
        "front_center": (1920, 720),
        "rear_left":    (1920, 720),
        "rear_right":   (1920, 720),
        "cluster":      (1920, 720),
        "hud":          (1920, 720),
    }

    def __init__(self, host: str, port: int = 20000, device_id: str = "",
                 key_overrides: Optional[dict[str, dict]] = None):
        """
        Args:
            key_overrides: 디바이스별 키 오버라이드. 각 항목은
                {name: {"cmd": int, "key": int, "dial": bool, "visible": bool}}
                visible=False면 UI에 표시되지 않음. cmd/key/dial 중 지정된 필드만
                spec default를 덮어쓴다. 차종별로 키 값이 다를 때 사용.
        """
        self.host = host
        self.port = port
        self.device_id = device_id
        self.default_screen = "front_center"
        for name, p in SCREEN_PORT_MAP.items():
            if p == port:
                self.default_screen = name
                break
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})

        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._recv_thread: Optional[threading.Thread] = None
        self._exit_flag = False
        self._send_lock = threading.Lock()
        self._capture_lock = threading.Lock()

        self._recv_queue: queue.Queue = queue.Queue()
        self._recv_complete = True
        self._recv_packet_len = 0
        self._recv_data = ""

        self._img_event = threading.Event()
        self._img_filename = ""
        self._img_made = False
        self._img_buffer: bytes = b""

        self._screen_size_event = threading.Event()
        self.screen_width_front = 0
        self.screen_height_front = 0
        self.screen_width_rear_l = 0
        self.screen_height_rear_l = 0
        self.screen_width_rear_r = 0
        self.screen_height_rear_r = 0
        self.screen_width_cluster = 0
        self.screen_height_cluster = 0

        self.agent_version = ""

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        if self._socket:
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.settimeout(timeout)
            self._socket.connect((self.host, self.port))
            self._socket.settimeout(None)
        except Exception as e:
            logger.error("iSAP connect failed %s:%d: %s", self.host, self.port, e)
            self._socket = None
            return False

        # NOTI_CONNECTED 핸드셰이크 (13 byte) — 선택적 (에이전트가 즉시 보내지 않을 수 있음)
        deadline = time.time() + min(timeout, 3.0)
        self._connected = False
        self._socket.settimeout(1.0)
        try:
            while not self._connected and time.time() < deadline:
                try:
                    raw = self._socket.recv(13)
                    if not raw:
                        break
                    if len(raw) >= 7 and raw[0] == START_BIT and raw[1] == START_BIT and raw[6] == NOTI_CONNECTED:
                        self._connected = True
                        logger.info("iSAP NOTI_CONNECTED received: %s:%d", self.host, self.port)
                        break
                except socket.timeout:
                    continue
                except Exception:
                    break
        finally:
            try:
                self._socket.settimeout(None)
            except Exception:
                pass

        # NOTI_CONNECTED를 못 받아도 TCP 연결은 성립된 것으로 간주 (일부 Agent는 푸시하지 않음)
        if not self._connected:
            logger.info("iSAP handshake not received, proceeding (TCP connected): %s:%d",
                        self.host, self.port)
            self._connected = True

        self._exit_flag = False
        self._recv_thread = threading.Thread(
            target=self._receive_thread, name=f"isap-recv-{self.device_id}", daemon=True
        )
        self._recv_thread.start()

        # 초기화 시퀀스: version + screen size (미응답 Agent 대비 짧은 대기)
        try:
            self._req_agent_version()
            time.sleep(0.3)
            self._req_screen_size()
        except Exception as e:
            logger.debug("iSAP init probes failed (non-fatal): %s", e)

        return True

    def disconnect(self) -> None:
        self._exit_flag = True
        if self._socket:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
        self._connected = False
        if self._recv_thread and self._recv_thread.is_alive():
            self._recv_thread.join(timeout=3)
        self._recv_thread = None
        logger.info("iSAP disconnected: %s:%d", self.host, self.port)

    @property
    def is_connected(self) -> bool:
        return self._connected and self._socket is not None

    # ------------------------------------------------------------------
    # Packet send
    # ------------------------------------------------------------------

    def _send_raw(self, packet: list[int]) -> None:
        if not self._socket:
            raise ConnectionError("iSAP not connected")
        msg = bytearray(packet)
        try:
            self._socket.send(msg)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            logger.warning("iSAP connection lost (device=%s): %s", self.device_id, e)
            self.disconnect()
            raise ConnectionError(f"iSAP connection lost: {e}")

    def _make_send_packet(self, cmd: int, sub_cmd: int, resp: int, data: list[int]) -> None:
        agent_cmd = [cmd, sub_cmd, resp] + data
        crc = _calc_crc16(agent_cmd)
        packet_len = len(agent_cmd)

        packet = [START_BIT, START_BIT]
        packet.append((packet_len >> 24) & 0xFF)
        packet.append((packet_len >> 16) & 0xFF)
        packet.append((packet_len >> 8) & 0xFF)
        packet.append(packet_len & 0xFF)
        packet.extend(agent_cmd)
        packet.append((crc >> 8) & 0xFF)
        packet.append(crc & 0xFF)
        packet.append(END_BIT)
        packet.append(END_BIT)

        logger.debug("[iSAP SEND] cmd=0x%02X sub=0x%02X len=%d", cmd, sub_cmd, packet_len)
        self._send_raw(packet)

    # ------------------------------------------------------------------
    # Receive thread
    # ------------------------------------------------------------------

    def _receive_thread(self) -> None:
        logger.info("iSAP receive thread started: %s:%d", self.host, self.port)
        # 주기적 타임아웃으로 _exit_flag 검사 + 피어 silent 시 무한 블록 방지
        try:
            self._socket.settimeout(1.0)
        except Exception:
            pass
        while not self._exit_flag:
            try:
                if self._recv_complete:
                    try:
                        header = self._socket.recv(6)
                    except socket.timeout:
                        continue
                    if self._exit_flag or not header:
                        break
                    header_str = header.decode("iso-8859-1")

                    if ord(header_str[0]) == START_BIT and ord(header_str[1]) == START_BIT:
                        self._recv_packet_len = (
                            (ord(header_str[2]) << 24) | (ord(header_str[3]) << 16) |
                            (ord(header_str[4]) << 8) | ord(header_str[5])
                        )
                        self._recv_complete = False
                        self._recv_data = header_str
                    else:
                        logger.warning("iSAP bad packet header")
                        self._recv_complete = True
                        self._recv_data = ""
                else:
                    remaining = self._recv_packet_len + 4  # CRC(2) + END(2)
                    try:
                        payload = self._socket.recv(remaining)
                    except socket.timeout:
                        continue
                    if self._exit_flag or not payload:
                        break
                    payload_str = payload.decode("iso-8859-1")
                    self._recv_data += payload_str

                    if len(payload_str) == remaining:
                        self._recv_complete = True
                        self._recv_queue.put(self._recv_data)
                        self._decode_response()
                    elif len(payload_str) < remaining:
                        self._recv_complete = False
                        self._recv_packet_len -= len(payload_str)
                    else:
                        self._recv_complete = True

            except (socket.error, OSError):
                if not self._exit_flag:
                    logger.error("iSAP receive thread socket error")
                break
            except Exception as e:
                if not self._exit_flag:
                    logger.error("iSAP receive thread error: %s", e)
                break

        logger.info("iSAP receive thread ended: %s:%d", self.host, self.port)

    def _decode_response(self) -> None:
        while not self._recv_queue.empty():
            msg = self._recv_queue.get()
            if len(msg) < 10:
                continue

            if ord(msg[0]) != START_BIT or ord(msg[1]) != START_BIT:
                continue

            packet_len = (
                (ord(msg[2]) << 24) | (ord(msg[3]) << 16) |
                (ord(msg[4]) << 8) | ord(msg[5])
            )
            cmd = ord(msg[6])
            # payload = from index 9 (after cmd, sub, resp) length = packet_len - 3
            data_str = msg[9:9 + packet_len - 3]
            data_len = len(data_str)

            if cmd == NOTI_CONNECTED:
                self._connected = True
                logger.info("iSAP NOTI_CONNECTED")

            elif cmd == CMD_GETVERSION:
                self.agent_version = data_str if data_len > 0 else ""
                logger.info("iSAP agent version: %s", self.agent_version)

            elif cmd == CMD_GETSCREENWIDTHHEIGHT:
                data = [ord(c) for c in data_str]
                if len(data) >= 8:
                    self.screen_width_front = _parse_int32(data, 0)
                    self.screen_height_front = _parse_int32(data, 4)
                if len(data) >= 24:
                    self.screen_width_rear_l = _parse_int32(data, 8)
                    self.screen_height_rear_l = _parse_int32(data, 12)
                    self.screen_width_rear_r = _parse_int32(data, 16)
                    self.screen_height_rear_r = _parse_int32(data, 20)
                if len(data) >= 32:
                    self.screen_width_cluster = _parse_int32(data, 24)
                    self.screen_height_cluster = _parse_int32(data, 28)
                logger.info("iSAP screen: front=%dx%d",
                            self.screen_width_front, self.screen_height_front)
                self._screen_size_event.set()

            elif cmd == CMD_GETIMG:
                raw_bytes = data_str.encode("iso-8859-1")
                self._img_buffer = raw_bytes
                if self._img_filename:
                    try:
                        with open(self._img_filename, "wb") as f:
                            f.write(raw_bytes)
                    except Exception as e:
                        logger.warning("iSAP img write failed: %s", e)
                self._img_made = True
                self._img_event.set()
                logger.debug("iSAP image received: %d bytes", len(raw_bytes))

    # ------------------------------------------------------------------
    # Info requests
    # ------------------------------------------------------------------

    def _req_agent_version(self) -> None:
        self._make_send_packet(CMD_GETVERSION, 0, 0, [])

    def _req_screen_size(self) -> None:
        self._screen_size_event.clear()
        self._make_send_packet(CMD_GETSCREENWIDTHHEIGHT, 0, 0, [])
        self._screen_size_event.wait(timeout=3)

    def get_screen_size(self, screen_type: str = "front_center") -> tuple[int, int]:
        mapping = {
            "front_center": (self.screen_width_front, self.screen_height_front),
            "rear_left":    (self.screen_width_rear_l, self.screen_height_rear_l),
            "rear_right":   (self.screen_width_rear_r, self.screen_height_rear_r),
            "cluster":      (self.screen_width_cluster, self.screen_height_cluster),
            "hud":          (self.screen_width_front, self.screen_height_front),
        }
        w, h = mapping.get(screen_type, (0, 0))
        if w == 0 or h == 0:
            return self._DEFAULT_SCREEN_SIZES.get(screen_type, (1920, 720))
        return w, h

    def _monitor_byte(self, screen_type: str) -> int:
        return MONITOR_MAP.get(screen_type, 0x00)

    # ------------------------------------------------------------------
    # Screenshot (CMD_GETIMG, 표 84/85)
    # ------------------------------------------------------------------

    def _request_img(self, left: int, top: int, right: int, bottom: int,
                     screen_type: str, sub_cmd: int) -> None:
        """GET IMAGE 요청. Data = Left(2) Top(2) Right(2) Bottom(2) + Monitor(1)."""
        self._img_made = False
        self._img_buffer = b""
        self._img_event.clear()

        data = []
        for v in (left, top, right, bottom):
            data.append((v >> 8) & 0xFF)
            data.append(v & 0xFF)
        data.append(self._monitor_byte(screen_type))

        with self._send_lock:
            self._make_send_packet(CMD_GETIMG, sub_cmd, 0, data)

    def screencap_bytes(self, screen_type: str = "front_center",
                        fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        """화면 캡쳐 후 지정 포맷의 바이트 반환.

        Agent가 JPEG/PNG/BMP를 직접 지원하면 변환 없이 반환.
        """
        fmt_map = {"jpeg": IMG_JPEG, "png": IMG_PNG, "bmp": IMG_BMP24}
        sub_cmd = fmt_map.get(fmt, IMG_JPEG)

        with self._capture_lock:
            w, h = self.get_screen_size(screen_type)
            self._img_filename = ""
            self._request_img(0, 0, w, h, screen_type, sub_cmd)

            if not self._img_event.wait(timeout=timeout):
                raise TimeoutError(f"iSAP screenshot timeout ({timeout}s) for {screen_type}")

            raw = self._img_buffer
            if not raw:
                raise ValueError("iSAP empty image buffer")

        # Agent가 요청한 포맷으로 직접 보내므로 보통 그대로 반환 가능.
        # 혹시 BMP로만 응답하는 agent면 변환.
        if fmt in ("jpeg", "png") and raw[:3] not in (b"\xff\xd8\xff", b"\x89PN"):
            try:
                import cv2
                import numpy as np
                arr = np.frombuffer(raw, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    ext = ".jpg" if fmt == "jpeg" else ".png"
                    params = [cv2.IMWRITE_JPEG_QUALITY, 60] if fmt == "jpeg" else []
                    _, buf = cv2.imencode(ext, img, params)
                    return buf.tobytes()
            except Exception:
                pass
        return raw

    def screencap(self, output_path: str, screen_type: str = "front_center",
                  timeout: float = 10.0, fmt: str = "png") -> str:
        data = self.screencap_bytes(screen_type=screen_type, fmt=fmt, timeout=timeout)
        with open(output_path, "wb") as f:
            f.write(data)
        return output_path

    # ------------------------------------------------------------------
    # Touch input (CMD_LCDTOUCHEXT, 표 133/134)
    # ------------------------------------------------------------------

    def _lcd_touch_ext(self, x: int, y: int, action: int, screen_type: str,
                       finger_num: int = 1, finger_index: int = 0) -> None:
        """표 133: FINGER_NUMBER, FINGER_INDEX, X(2), Y(2), Action, Monitor."""
        data = [
            finger_num & 0xFF,
            finger_index & 0xFF,
            (x >> 8) & 0xFF, x & 0xFF,
            (y >> 8) & 0xFF, y & 0xFF,
            action & 0xFF,
            self._monitor_byte(screen_type),
        ]
        self._make_send_packet(CMD_LCDTOUCHEXT, 0, RESPONSE_NOTHING, data)

    def _lcd_drag(self, sx: int, sy: int, ex: int, ey: int,
                  screen_type: str, duration_ms: int = 0) -> None:
        """표 136: Start_X(2), Start_Y(2), End_X(2), End_Y(2), Monitor, DraggingTime(4)."""
        data = []
        for v in (sx, sy, ex, ey):
            data.append((v >> 8) & 0xFF)
            data.append(v & 0xFF)
        data.append(self._monitor_byte(screen_type))
        if duration_ms > 0:
            data.append((duration_ms >> 24) & 0xFF)
            data.append((duration_ms >> 16) & 0xFF)
            data.append((duration_ms >> 8) & 0xFF)
            data.append(duration_ms & 0xFF)
        self._make_send_packet(CMD_LCDTOUCH_DRAG, 0, 0, data)

    def tap(self, x: int, y: int, screen_type: str = "front_center") -> None:
        x, y = int(x), int(y)
        with self._capture_lock:
            time.sleep(0.1)
            with self._send_lock:
                self._lcd_touch_ext(x, y, TOUCH_PRESS, screen_type)
                time.sleep(0.05)
                self._lcd_touch_ext(x, y, TOUCH_RELEASE, screen_type)
                logger.info("[iSAP TAP] (%d,%d) screen=%s", x, y, screen_type)
            time.sleep(0.05)

    def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100,
                   screen_type: str = "front_center") -> None:
        x, y = int(x), int(y)
        interval_sec = max(interval_ms, 0) / 1000.0
        with self._capture_lock:
            with self._send_lock:
                for i in range(count):
                    self._lcd_touch_ext(x, y, TOUCH_PRESS, screen_type)
                    self._lcd_touch_ext(x, y, TOUCH_RELEASE, screen_type)
                    if i < count - 1 and interval_sec > 0:
                        time.sleep(interval_sec)
                logger.info("[iSAP REPEAT_TAP] (%d,%d) ×%d", x, y, count)
            time.sleep(0.05)

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "front_center") -> None:
        x, y = int(x), int(y)
        with self._capture_lock:
            time.sleep(0.1)
            with self._send_lock:
                self._lcd_touch_ext(x, y, TOUCH_PRESS, screen_type)
                time.sleep(duration_ms / 1000.0)
                self._lcd_touch_ext(x, y, TOUCH_RELEASE, screen_type)
                logger.info("[iSAP LONG_PRESS] (%d,%d) %dms", x, y, duration_ms)
            time.sleep(0.05)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "front_center", duration_ms: int = 0,
              hold_ms: int = 0) -> None:
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        if hold_ms and hold_ms > 0:
            # 드래그앤드롭(앱카드 이동): TOUCH_PRESS → hold → TOUCH_MOVE(보간) → TOUCH_RELEASE.
            # _lcd_drag(고정 fling)는 시작 hold를 표현 못해 ext 터치 시퀀스로 직접 구성.
            move_dur = max(int(duration_ms or 0), 200)
            steps = max(3, min(20, max(1, move_dur // 20)))
            dx = (x2 - x1) / steps
            dy = (y2 - y1) / steps
            interval_s = max(0.01, (move_dur / 1000.0) / steps)
            with self._capture_lock:
                time.sleep(0.1)
                with self._send_lock:
                    self._lcd_touch_ext(x1, y1, TOUCH_PRESS, screen_type)
                    time.sleep(hold_ms / 1000.0)
                    for i in range(1, steps):
                        ix = int(round(x1 + dx * i))
                        iy = int(round(y1 + dy * i))
                        self._lcd_touch_ext(ix, iy, TOUCH_MOVE, screen_type)
                        time.sleep(interval_s)
                    self._lcd_touch_ext(x2, y2, TOUCH_RELEASE, screen_type)
                    logger.info("[iSAP DRAG_DROP] (%d,%d)->(%d,%d) hold=%dms screen=%s",
                                x1, y1, x2, y2, hold_ms, screen_type)
                time.sleep(0.05)
            return
        with self._capture_lock:
            time.sleep(0.1)
            with self._send_lock:
                self._lcd_drag(x1, y1, x2, y2, screen_type, duration_ms)
                logger.info("[iSAP SWIPE] (%d,%d)->(%d,%d) screen=%s", x1, y1, x2, y2, screen_type)
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Hardware keys (표 111)
    # ------------------------------------------------------------------

    def send_key(self, cmd: int, sub_cmd: int, key_data: int,
                 screen_type: str = "front_center",
                 direction: Optional[int] = None) -> None:
        """DataValue(4 BE) + [Dir(1)] + Monitor(1) — 표 111/112."""
        data = [
            (key_data >> 24) & 0xFF,
            (key_data >> 16) & 0xFF,
            (key_data >> 8) & 0xFF,
            key_data & 0xFF,
        ]
        if direction is not None:
            data.append(direction & 0xFF)
        data.append(self._monitor_byte(screen_type))
        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, RESPONSE_NOTHING, data)

    def resolve_key(self, key_name: str) -> Optional[dict]:
        """spec default + device override 병합된 키 정보 반환.

        override는 cmd/key/dial 개별 필드만 덮어쓴다 (visible은 무시 — UI 전용).
        반환 dict에 cmd/key가 없으면 None.
        """
        base = ISAP_KEYS.get(key_name, {})
        ov = self._key_overrides.get(key_name, {})
        merged = dict(base)
        for k in ("cmd", "key", "dial"):
            if k in ov:
                merged[k] = ov[k]
        if "cmd" not in merged or "key" not in merged:
            return None
        return merged

    def set_key_overrides(self, overrides: Optional[dict[str, dict]]) -> None:
        """디바이스 키 오버라이드 일괄 갱신 (설정 모달 저장 시 호출)."""
        self._key_overrides = dict(overrides or {})

    def get_key_overrides(self) -> dict[str, dict]:
        return dict(self._key_overrides)

    def send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                         screen_type: str = "front_center",
                         direction: Optional[int] = None,
                         key_source: Optional[int] = None) -> None:
        # key_source는 HKMC CCRC 전용 — iSAP에선 무시 (시그니처 통일)
        _ = key_source
        info = self.resolve_key(key_name)
        if not info:
            raise ValueError(f"Unknown iSAP key: {key_name}")
        cmd = info["cmd"]
        key_data = info["key"]

        with self._capture_lock:
            time.sleep(0.1)
            if info.get("dial"):
                dir_val = direction if direction is not None else DIR_CLOCKWISE
                self.send_key(cmd, KNOB_KEY, key_data, screen_type, dir_val)
            elif sub_cmd == SHORT_KEY:
                # 프로토콜 사양: PRESS → SHORT → RELEASE 순서로 송신
                self.send_key(cmd, PRESS_KEY, key_data, screen_type, direction)
                time.sleep(0.05)
                self.send_key(cmd, SHORT_KEY, key_data, screen_type, direction)
                time.sleep(0.05)
                self.send_key(cmd, RELEASE_KEY, key_data, screen_type, direction)
            elif sub_cmd == LONG_KEY:
                # PRESS~LONG 간격을 짧게(0.05초) — 길게 두면 리어 모니터 포커스가
                # 풀려 LONG 이 엉뚱한 화면에서 처리됨. sub_cmd=LONG_KEY 로 의미 전달.
                self.send_key(cmd, PRESS_KEY, key_data, screen_type, direction)
                time.sleep(0.05)
                self.send_key(cmd, LONG_KEY, key_data, screen_type, direction)
                time.sleep(0.05)
                self.send_key(cmd, RELEASE_KEY, key_data, screen_type, direction)
            else:
                self.send_key(cmd, sub_cmd, key_data, screen_type, direction)
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Async wrappers
    # ------------------------------------------------------------------

    async def async_connect(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.connect, timeout)

    async def async_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    async def async_screencap_bytes(self, screen_type: str = "front_center",
                                    fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap_bytes, screen_type, fmt, timeout)

    async def async_screencap(self, output_path: str, screen_type: str = "front_center",
                              timeout: float = 10.0, fmt: str = "png") -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap, output_path, screen_type, timeout, fmt)

    async def async_tap(self, x: int, y: int, screen_type: str = "front_center") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.tap, x, y, screen_type)

    async def async_repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100,
                               screen_type: str = "front_center") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.repeat_tap, x, y, count, interval_ms, screen_type)

    async def async_long_press(self, x: int, y: int, duration_ms: int = 3000,
                               screen_type: str = "front_center") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.long_press, x, y, duration_ms, screen_type)

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type: str = "front_center", duration_ms: int = 0,
                          hold_ms: int = 0) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.swipe, x1, y1, x2, y2, screen_type, duration_ms, hold_ms)

    async def async_send_key(self, cmd: int, sub_cmd: int, key_data: int,
                             screen_type: str = "front_center",
                             direction: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.send_key, cmd, sub_cmd, key_data, screen_type, direction)

    async def async_send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                                     screen_type: str = "front_center",
                                     direction: Optional[int] = None,
                                     key_source: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.send_key_by_name, key_name, sub_cmd, screen_type, direction, key_source)

    # ------------------------------------------------------------------

    def get_info(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.is_connected,
            "agent_version": self.agent_version,
            "default_screen": self.default_screen,
            "screens": {
                "front_center": {"width": self.screen_width_front or self._DEFAULT_SCREEN_SIZES["front_center"][0],
                                 "height": self.screen_height_front or self._DEFAULT_SCREEN_SIZES["front_center"][1]},
                "rear_left":    {"width": self.screen_width_rear_l or self._DEFAULT_SCREEN_SIZES["rear_left"][0],
                                 "height": self.screen_height_rear_l or self._DEFAULT_SCREEN_SIZES["rear_left"][1]},
                "rear_right":   {"width": self.screen_width_rear_r or self._DEFAULT_SCREEN_SIZES["rear_right"][0],
                                 "height": self.screen_height_rear_r or self._DEFAULT_SCREEN_SIZES["rear_right"][1]},
                "cluster":      {"width": self.screen_width_cluster or self._DEFAULT_SCREEN_SIZES["cluster"][0],
                                 "height": self.screen_height_cluster or self._DEFAULT_SCREEN_SIZES["cluster"][1]},
            },
        }
