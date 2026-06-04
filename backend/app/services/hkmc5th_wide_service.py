"""HKMC 5th generation (Wide) protocol service — TCP 소켓 기반 IVI 디바이스 통신.

IVILGECommonAgentPlugin (IVILGECommonAgentClient.py / IVILGECommonAgentProtocol.py)
에서 프로토콜 로직을 추출하여 ATS 프레임워크 의존성을 제거한 Python 3 포팅.
HKMC6thService와 동일한 async API를 제공하여 playback_service/device_manager와 호환.

5th gen vs 6th gen 주요 차이점:
  - 화면: front_center 단일 스크린 (리어 모니터 없음)
  - 키 구조: CMD_HKEY(0x60)/SWC(0x70)/CCP(0x80)/RRC(0x90) + 개별 메시지 커맨드
  - 리소스 모니터링: reqResourceInfo, resourceMonitor 지원
  - 버전 정보: agent version + set main/sub version
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import queue
import socket
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (from IVILGECommonAgentProtocol.py)
# ---------------------------------------------------------------------------
START_BIT = 0x61
END_BIT   = 0x6F

# 초기화 시퀀스 커맨드
CMD_ATSA_GETVERSION           = 0xA0
CMD_ATSA_GETSETMAINVERSION    = 0xA1
CMD_ATSA_GETSETSUBVERSION     = 0xA2
CMD_ATSA_GETSCREENWIDTHHEIGHT = 0xA3
CMD_ATSA_GETCPUINFO           = 0xA4
CMD_ATSA_GETMEMINFO           = 0xA5
CMD_ATSA_GETRESOURCEINFO      = 0xA6

# 리소스 모니터링
CMD_RESOURCEINFO_START = 0xE0
CMD_RESOURCEINFO_STOP  = 0xE1

# 이미지 캡처
CMD_GETIMG   = 0x6A
CMD_GETFILE  = 0xE3

# 터치 / 드래그
CMD_LCDTOUCH      = 0x69
CMD_LCDTOUCH_DRAG = 0xD6
CMD_LCDTOUCH_FAST = 0xD7
CMD_LCDTOUCHEXT   = 0xB0

# 하드웨어 키 그룹 커맨드
CMD_HKEY = 0x60   # Hardware Key (FMAM, MEDIA, TUNE, VOLUME, …)
CMD_SWC  = 0x70   # Steering Wheel Control
CMD_CCP  = 0x80   # Control Command Panel (조그 다이얼)
CMD_RRC  = 0x90   # Rear Remote Control

# 개별 메시지 키 커맨드 (데이터 없이 cmd+subCmd만 전송)
CMD_DAYNIGHT_KEY        = 0x60
CMD_RADIO_KEY           = 0x61
CMD_MAP_KEY             = 0x62
CMD_NAVI_KEY            = 0x63
CMD_SETTING_KEY         = 0x64
CMD_INFO_KEY            = 0x65
CMD_PHONE_KEY           = 0x66
CMD_PERCEIVED_POWER_ON  = 0x6B
CMD_PERCEIVED_POWER_OFF = 0x6C
CMD_TUNE_NEXT_KEY       = 0x6D
CMD_TUNE_BEFORE_KEY     = 0x6E
CMD_TUNE_PRESS_KEY      = 0x6F
CMD_DMB_KEY             = 0xD1
CMD_MEDIA_KEY           = 0xD2
CMD_SEEK_KEY            = 0xD3
CMD_TRACK_KEY           = 0xD4
CMD_KOECN_APP_MEMORY_INFO = 0xD5
CMD_CALL_KEY            = 0xD7
CMD_CALLLONG_KEY        = 0xD8
CMD_CALLEND_KEY         = 0xD9

# 볼륨 / 시크 / 모드 요청 커맨드
CMD_VOLUME_UP_REQ   = 0x20
CMD_VOLUME_DOWN_REQ = 0x21
CMD_SEEK_UP_REQ     = 0x22
CMD_SEEK_DOWN_REQ   = 0x23
CMD_MODE_REQ        = 0x24

# CDP (0x44b = 1099)
CMD_CDP_REQ         = 0x44b

# SWC 추가 메시지 키
CMD_SWRC_NEXT        = 0x95
CMD_SWRC_PREV        = 0x96
CMD_SWRC_PTT         = 0x94
CMD_SWRC_CALL_SEND   = 0x97
CMD_SWRC_CALL_END    = 0x98
CMD_SWRC_MUTE        = 0x92
CMD_SWRC_MODE        = 0x93
CMD_SWRC_VOLUP       = 0xB4
CMD_SWRC_VOLDN       = 0xB5
CMD_SWRC_SRC         = 0xB3

# CCP 추가
CMD_CCP_HOME_KEY     = 0x55

# 연결 알림
NOTI_CONNECTED   = 0x5E
NOTI_HKEY        = 0x65
NOTI_SWC         = 0x75
NOTI_CCP         = 0x85
NOTI_RRC         = 0x90
NOTI_LCDTOUCHEXT = 0xB5

# 응답 값
RESPONSE_FAIL         = 0x20
RESPONSE_SUCCESS      = 0x21
RESPONSE_INITIAL_HEX  = 0x22

# 패킷 파싱 상태
FULL_PACKET  = 1
NEED_DATA    = 2
MORE_PACKET  = 3

# 서브 커맨드 (키 동작)
RELEASE_KEY  = 0x41  # Release
PRESS_KEY    = 0x42  # Press
SHORT_KEY    = 0x43  # Short key
LONG_KEY     = 0x44  # Long key
MOVE_KEY     = 0x45
PRESS_LONG   = 0x46
DIAL_ACTION  = 0x80

# 다이얼 방향
CLOCK      = 0x00
ANTI_CLOCK = 0x01

# HKEY 키 코드
HKEY_FMAM              = 0x11
HKEY_MEDIA             = 0x12
HKEY_SEEK              = 0x13
HKEY_TRACK             = 0x14
HKEY_MAPVOICE          = 0x15
HKEY_NAV               = 0x16
HKEY_EJECT             = 0x17
HKEY_SETUP             = 0x18
HKEY_TUNE_CENTER          = 0x1C
HKEY_TUNE_DIAL            = 0xA0  # dial (encoder)
HKEY_TUNE_DIAL_ENCODER    = 0xA0  # 원본명 별칭
HKEY_VOLUME_CENTER        = 0xF0
HKEY_VOLUME_DIAL          = 0xF1  # dial (encoder)
HKEY_VOLUME_DIAL_ENCODER  = 0xF1  # 원본명 별칭

# SWC 키 코드 (CMD_SWC=0x70 keyExt 방식)
SWC_PHONE_SEND         = 0x01
SWC_PHONE_END          = 0x02
SWC_PTT                = 0x03
SWC_VOLUME_DOWN        = 0x04
SWC_VOLUME_UP          = 0x05
SWC_MUTE               = 0x06
SWC_MODE               = 0x07
SWC_SEEK_DOWN          = 0x08
SWC_SEEK_UP            = 0x09
SWC_VOLUME_SCROLL_DOWN = 0x10
SWC_VOLUME_SCROLL_UP   = 0x11

# Hyundai Premier 5th — MKBD (Media Keyboard)
MKBD_MAP                  = 0x11
MKBD_NAV                  = 0x13
MKBD_RADIO                = 0x12
MKBD_MEDIA                = 0x14
MKBD_CUSTOM               = 0x16
MKBD_SETUP                = 0x18
MKBD_POWER_VOLUME_PRESS       = 0xF0
MKBD_POWER_VOLUME_CLOCK       = 0xF1
MKBD_POWER_VOLUME_ANTI        = 0xF1
MKBD_POWER_VOLUME_ANTICLOCK   = 0xF1  # 원본명 별칭
MKBD_TUNE_PRESS               = 0x1C
MKBD_TUNE_CLOCK               = 0xA0
MKBD_TUNE_ANTI                = 0xA0
MKBD_TUNE_ANTICLOCK           = 0xA0  # 원본명 별칭

# CCP — Control Command Panel (조그/홈/백)
CCP_BACK          = 0x11
CCP_HOME          = 0x21
CCP_MENU          = 0x31
CCP_JOG_PRESS     = 0x61
CCP_JOG_UP        = 0x64
CCP_JOG_DOWN      = 0x64
CCP_JOG_LEFT      = 0x64
CCP_JOG_RIGHT     = 0x64
CCP_JOG_CLOCK     = 0x62
CCP_JOG_ANTI      = 0x62
CCP_JOG_ANTICLOCK = 0x62  # 원본명 별칭

# RRC — Rear Remote Control
RRC_BACK          = 0x32
RRC_HOME          = 0x12
RRC_MENU          = 0x13
RRC_MODE          = 0x22
RRC_PWR_L         = 0x41
RRC_SEEK_L        = 0x31
RRC_SEEK_R        = 0x11
RRC_MUTE          = 0x33
RRC_PWR_R         = 0x42
RRC_VOL_DOWN      = 0xF2
RRC_VOL_UP        = 0xF3
RRC_JOG_PRESS     = 0x61
RRC_JOG_UP        = 0x64
RRC_JOG_DOWN      = 0x64
RRC_JOG_LEFT      = 0x64
RRC_JOG_RIGHT     = 0x64
RRC_JOG_CLOCK     = 0x62
RRC_JOG_ANTI      = 0x62
RRC_JOG_ANTICLOCK = 0x62  # 원본명 별칭

# ---------------------------------------------------------------------------
# HKMC 5th Wide 앱 목록 (리소스 모니터링 — from IVILGECommonAgentAppMemInfo.py)
# ---------------------------------------------------------------------------
HKMC_5TH_WIDE_APP_LIST = [
    "AppCamera",
    "AppClimate",
    "AppDMClient",
    "AppEarlyCamera",
    "AppHomeScreen",
    "AppLauncher",
    "ApplicationManager",
    "AppMediaPlayer",
    "AppNavi",
    "AppOSDManager",
    "AppProjection",
    "AppSetup",
    "AppStandbyClock",
    "AppVH",
    "AudioManager",
    "BluetoothHMI",
    "connmand",
    "dlt-daemon",
    "iAP2Service-2.0",
    "iPodServiceDaemon",
    "lightmediascannerd",
    "LocationManager",
    "MediaBrowser",
    "mediamanager",
    "micommanager_userspace",
    "MOFManager",
    "NotificationManager",
    "playerengine",
    "thumbnail_extractor",
    "TimeManager",
    "TmsRemoteApp",
    "usb-automount",
    "watchdogd",
    "weston",
    "weston-keyboard",
]

# ---------------------------------------------------------------------------
# Key definitions
# ---------------------------------------------------------------------------

# "msg": True  → keyMessage 방식 (cmd+subCmd, 데이터 없음)
# "key": int   → keyExt 방식 (cmd+subCmd+keyData as int)
# "dial": True → DIAL_ACTION 서브커맨드 사용, direction 필드 필요
HKMC5TH_WIDE_KEYS: dict[str, dict] = {
    # ── 개별 메시지 키 (데이터 페이로드 없음) ─────────────────────────────
    "DAYNIGHT":     {"cmd": CMD_DAYNIGHT_KEY,    "msg": True},
    "RADIO":        {"cmd": CMD_RADIO_KEY,       "msg": True},
    "MAP":          {"cmd": CMD_MAP_KEY,         "msg": True},
    "NAVI":         {"cmd": CMD_NAVI_KEY,        "msg": True},
    "SETTING":      {"cmd": CMD_SETTING_KEY,     "msg": True},
    "INFO":         {"cmd": CMD_INFO_KEY,        "msg": True},
    "PHONE":        {"cmd": CMD_PHONE_KEY,       "msg": True},
    "POWER_ON":     {"cmd": CMD_PERCEIVED_POWER_ON,  "msg": True},
    "POWER_OFF":    {"cmd": CMD_PERCEIVED_POWER_OFF, "msg": True},
    "TUNE_NEXT":    {"cmd": CMD_TUNE_NEXT_KEY,   "msg": True},
    "TUNE_BEFORE":  {"cmd": CMD_TUNE_BEFORE_KEY, "msg": True},
    "TUNE_PRESS":   {"cmd": CMD_TUNE_PRESS_KEY,  "msg": True},
    "DMB":          {"cmd": CMD_DMB_KEY,         "msg": True},
    "MEDIA":        {"cmd": CMD_MEDIA_KEY,       "msg": True},
    "SEEK":         {"cmd": CMD_SEEK_KEY,        "msg": True},
    "TRACK":        {"cmd": CMD_TRACK_KEY,       "msg": True},
    "CALL":         {"cmd": CMD_CALL_KEY,        "msg": True},
    "CALL_LONG":    {"cmd": CMD_CALLLONG_KEY,    "msg": True},
    "CALL_END":     {"cmd": CMD_CALLEND_KEY,     "msg": True},

    # ── HKEY (CMD_HKEY=0x60) — keyExt int 방식 ────────────────────────────
    "HKEY_FMAM":              {"cmd": CMD_HKEY, "key": HKEY_FMAM},
    "HKEY_MEDIA":             {"cmd": CMD_HKEY, "key": HKEY_MEDIA},
    "HKEY_SEEK":              {"cmd": CMD_HKEY, "key": HKEY_SEEK},
    "HKEY_TRACK":             {"cmd": CMD_HKEY, "key": HKEY_TRACK},
    "HKEY_MAPVOICE":          {"cmd": CMD_HKEY, "key": HKEY_MAPVOICE},
    "HKEY_NAV":               {"cmd": CMD_HKEY, "key": HKEY_NAV},
    "HKEY_EJECT":             {"cmd": CMD_HKEY, "key": HKEY_EJECT},
    "HKEY_SETUP":             {"cmd": CMD_HKEY, "key": HKEY_SETUP},
    "HKEY_TUNE_CENTER":       {"cmd": CMD_HKEY, "key": HKEY_TUNE_CENTER},
    "HKEY_TUNE_CLOCK":        {"cmd": CMD_HKEY, "key": HKEY_TUNE_DIAL, "dial": True, "direction": CLOCK},
    "HKEY_TUNE_ANTI":         {"cmd": CMD_HKEY, "key": HKEY_TUNE_DIAL, "dial": True, "direction": ANTI_CLOCK},
    "HKEY_VOLUME_CENTER":     {"cmd": CMD_HKEY, "key": HKEY_VOLUME_CENTER},
    "HKEY_VOLUME_CLOCK":      {"cmd": CMD_HKEY, "key": HKEY_VOLUME_DIAL, "dial": True, "direction": CLOCK},
    "HKEY_VOLUME_ANTI":       {"cmd": CMD_HKEY, "key": HKEY_VOLUME_DIAL, "dial": True, "direction": ANTI_CLOCK},

    # ── SWC (CMD_SWC=0x70) — keyExt int 방식 ─────────────────────────────
    "SWC_PHONE_SEND":         {"cmd": CMD_SWC, "key": SWC_PHONE_SEND},
    "SWC_PHONE_END":          {"cmd": CMD_SWC, "key": SWC_PHONE_END},
    "SWC_PTT":                {"cmd": CMD_SWC, "key": SWC_PTT},
    "SWC_VOLUME_DOWN":        {"cmd": CMD_SWC, "key": SWC_VOLUME_DOWN},
    "SWC_VOLUME_UP":          {"cmd": CMD_SWC, "key": SWC_VOLUME_UP},
    "SWC_MUTE":               {"cmd": CMD_SWC, "key": SWC_MUTE},
    "SWC_MODE":               {"cmd": CMD_SWC, "key": SWC_MODE},
    "SWC_SEEK_DOWN":          {"cmd": CMD_SWC, "key": SWC_SEEK_DOWN},
    "SWC_SEEK_UP":            {"cmd": CMD_SWC, "key": SWC_SEEK_UP},
    "SWC_VOL_SCROLL_DOWN":    {"cmd": CMD_SWC, "key": SWC_VOLUME_SCROLL_DOWN},
    "SWC_VOL_SCROLL_UP":      {"cmd": CMD_SWC, "key": SWC_VOLUME_SCROLL_UP},

    # ── CCP (CMD_CCP=0x80) — 조그 다이얼 (DIAL_ACTION) ───────────────────
    "CCP_JOGDIAL_CLOCK":      {"cmd": CMD_CCP, "key": 0x00, "dial": True, "direction": CLOCK},
    "CCP_JOGDIAL_ANTI":       {"cmd": CMD_CCP, "key": 0x00, "dial": True, "direction": ANTI_CLOCK},
    "CCP_VOLUME_UP":          {"cmd": CMD_CCP, "key": 0x01, "dial": True, "direction": CLOCK},
    "CCP_VOLUME_DOWN":        {"cmd": CMD_CCP, "key": 0x01, "dial": True, "direction": ANTI_CLOCK},
    "CCP_TUNE_UP":            {"cmd": CMD_CCP, "key": 0x04, "dial": True, "direction": CLOCK},
    "CCP_TUNE_DOWN":          {"cmd": CMD_CCP, "key": 0x04, "dial": True, "direction": ANTI_CLOCK},
    # ── CCP — 일반 버튼 (BACK / HOME / MENU / JOG PRESS) ─────────────────
    "CCP_BACK":               {"cmd": CMD_CCP, "key": CCP_BACK},
    "CCP_HOME":               {"cmd": CMD_CCP, "key": CCP_HOME},
    "CCP_MENU":               {"cmd": CMD_CCP, "key": CCP_MENU},
    "CCP_JOG_PRESS":          {"cmd": CMD_CCP, "key": CCP_JOG_PRESS},
    # ── 별칭 — 사용자 친화 (HOME/BACK 만으로도 호출 가능하게 함) ─────────
    "HOME":                   {"cmd": CMD_CCP, "key": CCP_HOME},
    "BACK":                   {"cmd": CMD_CCP, "key": CCP_BACK},
    "MENU":                   {"cmd": CMD_CCP, "key": CCP_MENU},

    # ── RRC (CMD_RRC=0x90) — keyExt int 방식 ─────────────────────────────
    "RRC_ENTER":              {"cmd": CMD_RRC, "key": 0x08},
    "RRC_UP":                 {"cmd": CMD_RRC, "key": 0x00},
    "RRC_DOWN":               {"cmd": CMD_RRC, "key": 0x01},
    "RRC_LEFT":               {"cmd": CMD_RRC, "key": 0x03},
    "RRC_RIGHT":              {"cmd": CMD_RRC, "key": 0x06},
    "RRC_BACK":               {"cmd": CMD_RRC, "key": 0x09},
    "RRC_HOME":               {"cmd": CMD_RRC, "key": 0x14},
}


# ---------------------------------------------------------------------------
# CRC16
# ---------------------------------------------------------------------------

def _calc_crc16(data: list[int]) -> int:
    """CRC16 with 0xC659 polynomial (from IVILGECommonAgentClient)."""
    crc = 0xFFFF
    key = 0xC659
    for b in data:
        tmp = (b & 0xFF) ^ (crc & 0x00FF)
        for _ in range(8):
            if tmp & 1:
                tmp = (tmp >> 1) ^ key
            else:
                tmp >>= 1
        crc = (crc >> 8) ^ tmp
    return crc


def _parse_int32(data: list[int], offset: int) -> int:
    """Parse big-endian 32-bit integer."""
    return ((data[offset] << 24) | (data[offset + 1] << 16) |
            (data[offset + 2] << 8) | data[offset + 3])


# ---------------------------------------------------------------------------
# HKMC5thWideService
# ---------------------------------------------------------------------------

class HKMC5thWideService:
    """TCP socket client for HKMC 5th generation (Wide) IVI devices.

    IVILGECommonAgentPlugin을 ATS 의존성 없이 Python 3으로 포팅.
    HKMC6thService와 동일한 async API를 제공하여 device_manager/playback_service 호환.
    5th gen은 단일 front_center 화면만 지원한다 (리어 모니터 없음).
    """

    def __init__(self, host: str, port: int, device_id: str = "",
                 key_overrides: Optional[dict[str, dict]] = None,
                 device_model: str = ""):
        """
        Args:
            host: 디바이스 IP 주소
            port: TCP 포트 (통상 6655 또는 5000)
            device_id: 디바이스 식별자 (로그/디버그용)
            key_overrides: 디바이스별 키 오버라이드 {name: {cmd, key, dial, visible}}
            device_model: 디바이스 모델명 (로그용)
        """
        self.host = host
        self.port = port
        self.device_id = device_id
        self.device_model = device_model
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})

        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._recv_thread: Optional[threading.Thread] = None
        self._exit_flag = False
        self._send_lock = threading.Lock()
        self._capture_lock = threading.Lock()

        # Receive state
        self._recv_queue: queue.Queue = queue.Queue()
        self._recv_complete = True
        self._recv_packet_len = 0
        self._recv_data = ""

        # Image capture state
        self._img_event = threading.Event()
        self._img_filename = ""
        self._img_made = False
        self._img_buffer: bytes = b""

        # File transfer state (get_file)
        self._file_event = threading.Event()
        self.path_pc: str = ""
        self.bGetFile: bool = False

        # Screen size (populated after reqScreenSize — 5th gen: single screen)
        self._screen_size_event = threading.Event()
        self.screen_width = 0
        self.screen_height = 0

        # Version info
        self.agent_version = ""
        self.set_main_version = ""
        self.set_sub_version = ""

        # Resource info (reqResourceInfo 응답)
        self.cpu_core_num = 0
        self.cpu_info: dict = {}
        self.mem_info: dict = {}
        self.app_mem_info: dict = {}
        self.app_cpu_info: dict = {}

        # Resource monitor file handle (resourceMonitor)
        self._resource_file = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the HKMC 5th agent and start receive thread."""
        if self._socket:
            logger.warning("Already connected to %s:%d", self.host, self.port)
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._socket.settimeout(timeout)
            self._socket.connect((self.host, self.port))
        except Exception as e:
            logger.error("Failed to connect to %s:%d: %s", self.host, self.port, e)
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
            self._socket = None
            return False

        # Handshake: 13 byte 핸드셰이크 패킷 대기
        deadline = time.time() + timeout
        self._connected = False
        while not self._connected and time.time() < deadline:
            try:
                remaining = max(0.1, deadline - time.time())
                self._socket.settimeout(remaining)
                raw = self._socket.recv(13)
                if not raw:
                    logger.error("HKMC5thWide handshake: peer closed (%s:%d)", self.host, self.port)
                    break
                hex_val = raw.hex()
                # 6th gen과 동일한 핸드셰이크 패턴 사용 (IVILGECommonAgentClient 참조)
                if hex_val in ("6161000000035e002185fd6f6f", "6161000000035e0000df856f6f"):
                    self._connected = True
                    logger.info("HKMC5thWide agent connected: %s:%d", self.host, self.port)
                else:
                    logger.warning("Invalid HKMC5thWide handshake: %s", hex_val)
                    break
            except socket.timeout:
                logger.error("HKMC5thWide handshake timeout (%s:%d)", self.host, self.port)
                break
            except socket.error as e:
                logger.error("HKMC5thWide socket error during handshake: %s", e)
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
                return False

        if not self._connected:
            logger.error("HKMC5thWide handshake failed for %s:%d", self.host, self.port)
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
            return False

        # 핸드셰이크 완료 → blocking 모드
        try:
            self._socket.settimeout(None)
        except Exception:
            pass

        # Start receive thread
        self._exit_flag = False
        self._recv_thread = threading.Thread(
            target=self._receive_thread,
            name=f"hkmc5th-recv-{self.device_id}",
            daemon=True,
        )
        self._recv_thread.start()

        # 초기화 시퀀스 (IVILGECommonAgentClient 참조)
        self._req_ats_agent_version()
        time.sleep(0.3)
        self._req_target_main_version()
        time.sleep(0.3)
        self._req_target_sub_version()
        time.sleep(0.3)
        self._req_screen_size()
        if self.screen_height == 0:
            logger.warning("HKMC5thWide screen size not received, retrying…")
            time.sleep(1)
            self._req_screen_size()

        return True

    def disconnect(self) -> None:
        """Close connection and stop receive thread."""
        self._exit_flag = True
        if self._resource_file is not None:
            try:
                self._resource_file.close()
            except Exception:
                pass
            self._resource_file = None
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
        logger.info("HKMC5thWide disconnected: %s:%d", self.host, self.port)

    @property
    def is_connected(self) -> bool:
        return self._connected and self._socket is not None

    # ------------------------------------------------------------------
    # Packet send
    # ------------------------------------------------------------------

    def _send_raw(self, packet: list[int]) -> None:
        if not self._socket:
            raise ConnectionError("Not connected to HKMC5thWide agent")
        msg = bytearray(packet)
        try:
            self._socket.send(msg)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            logger.warning("HKMC5thWide connection lost (device=%s): %s", self.device_id, e)
            self.disconnect()
            raise ConnectionError(f"HKMC5thWide connection lost: {e}")

    def _make_send_packet(self, cmd: int, sub_cmd: int, resp: int, data: list[int]) -> None:
        """Build and send a framed CRC16 packet."""
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

        logger.debug("[HKMC5thWide SEND] cmd=0x%02X sub=0x%02X resp=0x%02X data_len=%d",
                     cmd, sub_cmd, resp, len(data))
        self._send_raw(packet)

    # ------------------------------------------------------------------
    # Receive thread
    # ------------------------------------------------------------------

    def _receive_thread(self) -> None:
        logger.info("HKMC5thWide receive thread started for %s:%d", self.host, self.port)
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
                        logger.warning("HKMC5thWide bad packet header")
                        self._recv_complete = True
                        self._recv_data = ""
                else:
                    remaining = self._recv_packet_len + 4
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
                        logger.warning("HKMC5thWide packet length mismatch")
                        self._recv_complete = True

            except (socket.error, OSError):
                if not self._exit_flag:
                    logger.error("HKMC5thWide receive thread socket error")
                    self._connected = False
                break
            except Exception as e:
                if not self._exit_flag:
                    logger.error("HKMC5thWide receive thread error: %s", e)
                    self._connected = False
                break

        logger.info("HKMC5thWide receive thread ended for %s:%d", self.host, self.port)

    def _decode_response(self) -> None:
        """Decode received packets from queue."""
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
            data_str = msg[9:9 + packet_len - 3]
            data_len = len(data_str)
            data = [ord(c) for c in data_str]

            if cmd == NOTI_CONNECTED:
                self._connected = True
                logger.info("HKMC5thWide: connected notification received")

            elif cmd == CMD_ATSA_GETVERSION:
                self.agent_version = data_str if data_len > 0 else ""
                logger.info("HKMC5thWide agent version: %s", self.agent_version)

            elif cmd == CMD_ATSA_GETSETMAINVERSION:
                self.set_main_version = data_str if data_len > 0 else ""
                logger.info("HKMC5thWide main version: %s", self.set_main_version)

            elif cmd == CMD_ATSA_GETSETSUBVERSION:
                self.set_sub_version = data_str if data_len > 0 else ""
                logger.info("HKMC5thWide sub version: %s", self.set_sub_version)

            elif cmd == CMD_ATSA_GETSCREENWIDTHHEIGHT:
                if len(data) >= 8:
                    self.screen_width  = _parse_int32(data, 0)
                    self.screen_height = _parse_int32(data, 4)
                    logger.info("HKMC5thWide screen size: %dx%d",
                                self.screen_width, self.screen_height)
                self._screen_size_event.set()

            elif cmd == CMD_GETIMG:
                raw_bytes = data_str.encode("iso-8859-1")
                self._img_buffer = raw_bytes
                if self._img_filename:
                    with open(self._img_filename, "wb") as f:
                        f.write(raw_bytes)
                self._img_made = True
                self._img_event.set()
                logger.debug("HKMC5thWide image received: %d bytes", len(raw_bytes))

            elif cmd == CMD_GETFILE:
                if self.path_pc:
                    dir_name = os.path.dirname(self.path_pc)
                    if dir_name and not os.path.isdir(dir_name):
                        os.makedirs(dir_name, exist_ok=True)
                    text = "".join(chr(b) for b in data)
                    with open(self.path_pc, "w", encoding="utf-8", errors="replace") as f:
                        f.write(text)
                    logger.info("HKMC5thWide getFile: wrote %d bytes → %s", len(text), self.path_pc)
                    self.bGetFile = True
                    self._file_event.set()
                    self._file_event.clear()

            elif cmd == CMD_ATSA_GETRESOURCEINFO:
                self._decode_resource_info(data)

    def _decode_resource_info(self, data: list[int]) -> None:
        """Decode resource info response (CPU/MEM/App)."""
        if not data:
            return
        self.cpu_core_num = data[0]
        idx = 0
        for i in range(self.cpu_core_num):
            key = f"CPU{i + 1}"
            self.cpu_info[key] = _parse_int32(data, (i * 4) + 1)
            idx = (i * 4) + 4

        if len(data) > idx + 8:
            self.mem_info["TotalMem"] = _parse_int32(data, idx + 1)
            idx += 4
            self.mem_info["FreeMem"] = _parse_int32(data, idx + 1)
            idx += 4
            self.mem_info["UsedMem"] = (
                self.mem_info["TotalMem"] - self.mem_info["FreeMem"]
            )

        app_cnt = _parse_int32(data, idx + 1) if len(data) > idx + 4 else 0

        if app_cnt > 0 and len(data) > idx + 4:
            idx += 4
            app_data = data[idx + 1:]
            app_info_size = 44  # 32 name + 4 mem + 8 cpu
            for _ in range(app_cnt):
                if len(app_data) < app_info_size:
                    break
                app_name = ""
                for i in range(32):
                    if app_data[i] != 0:
                        app_name += chr(app_data[i])
                app_mem = _parse_int32(app_data, 32)
                if app_mem:
                    self.app_mem_info[app_name] = app_mem
                app_cpu = "".join(chr(app_data[i]) for i in range(36, app_info_size))
                if app_cpu.strip():
                    self.app_cpu_info[app_name] = app_cpu
                app_data = app_data[app_info_size:]

        # 리소스 파일에 기록
        if self._resource_file is not None:
            self._write_resource_record()

    def _write_resource_record(self) -> None:
        """Write a resource record line to the open resource file."""
        try:
            from time import strftime, localtime
            f = self._resource_file
            f.write(strftime("%Y-%m-%d %H:%M:%S\t", localtime()))
            for i in range(self.cpu_core_num):
                f.write(f"{self.cpu_info.get(f'CPU{i + 1}', 0)}%\t")
            total = self.mem_info.get("TotalMem", 1)
            used  = self.mem_info.get("UsedMem", 0)
            f.write(f"{int(round(used / total, 2) * 100)}%\t")
            for app_name in HKMC_5TH_WIDE_APP_LIST:
                written = False
                for k, v in self.app_mem_info.items():
                    if str(k).split("/")[-1] == app_name:
                        f.write(f"{v}\t")
                        written = True
                        break
                if not written:
                    f.write("0\t")
            for app_name in HKMC_5TH_WIDE_APP_LIST:
                written = False
                for k, v in self.app_cpu_info.items():
                    if str(k).split("/")[-1] == app_name:
                        f.write(f"{v}\t")
                        written = True
                        break
                if not written:
                    f.write("0\t")
            f.write("\n")
            f.flush()
        except Exception as e:
            logger.warning("HKMC5thWide resource write error: %s", e)

    # ------------------------------------------------------------------
    # Info requests
    # ------------------------------------------------------------------

    def _req_ats_agent_version(self) -> None:
        self._make_send_packet(CMD_ATSA_GETVERSION, 0, 0, [])

    def _req_target_main_version(self) -> None:
        self._make_send_packet(CMD_ATSA_GETSETMAINVERSION, 0, 0, [])

    def _req_target_sub_version(self) -> None:
        self._make_send_packet(CMD_ATSA_GETSETSUBVERSION, 0, 0, [])

    def _req_screen_size(self) -> None:
        self._screen_size_event.clear()
        self._make_send_packet(CMD_ATSA_GETSCREENWIDTHHEIGHT, 0, 0, [])
        self._screen_size_event.wait(timeout=5)

    def req_resource_info(self, b_start: bool, n_interval: int) -> None:
        """Start or stop resource info collection.

        Args:
            b_start: True=시작, False=중지
            n_interval: 수집 간격(ms)
        """
        cmd = CMD_RESOURCEINFO_START if b_start else CMD_RESOURCEINFO_STOP
        data = [
            (n_interval >> 24) & 0xFF,
            (n_interval >> 16) & 0xFF,
            (n_interval >> 8) & 0xFF,
            n_interval & 0xFF,
        ]
        with self._send_lock:
            self._make_send_packet(cmd, 0, 0, data)

    def resource_monitor(self, b_start: bool, n_interval: int, filepath: str) -> None:
        """Start/stop resource monitoring with file output.

        Args:
            b_start: True=시작, False=중지
            n_interval: 수집 간격(ms)
            filepath: 출력 파일 경로
        """
        if b_start:
            self._resource_file = open(filepath, "a", encoding="utf-8")
            # 헤더 행: 앱 이름 목록
            for app_name in HKMC_5TH_WIDE_APP_LIST:
                self._resource_file.write(f"{app_name}\t")
            self._resource_file.write("\n")
            self.req_resource_info(True, n_interval)
        else:
            self.req_resource_info(False, n_interval)
            if self._resource_file is not None:
                self._resource_file.close()
                self._resource_file = None

    def get_file(self, path_pc: str, path_target: str, timeout: float = 10.0) -> bool:
        """Request a file from the target device and save it to path_pc.

        Args:
            path_pc: 저장할 PC 측 파일 경로
            path_target: 디바이스 측 파일 경로
            timeout: 응답 대기 최대 시간(초)

        Returns:
            True=성공, False=실패
        """
        self.path_pc = path_pc
        self.bGetFile = False
        self._file_event.clear()

        # 원본 getFile()과 동일: 4바이트 길이 + 경로 바이트
        length = len(path_target)
        data: list[int] = [
            (length >> 24) & 0xFF,
            (length >> 16) & 0xFF,
            (length >> 8) & 0xFF,
            length & 0xFF,
        ] + [ord(c) for c in path_target]
        with self._send_lock:
            self._make_send_packet(CMD_GETFILE, 0, 0, data)

        self._file_event.wait(timeout=timeout)

        if self.bGetFile:
            return True
        logger.error("HKMC5thWide get_file timeout or error: %s", path_target)
        return False

    async def async_get_file(self, path_pc: str, path_target: str,
                              timeout: float = 10.0) -> bool:
        """Async wrapper for get_file."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.get_file, path_pc, path_target, timeout)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    _DEFAULT_SCREEN_SIZE = (1920, 720)

    def get_screen_size(self, screen_type: str = "front_center") -> tuple[int, int]:
        """Return (width, height). Falls back to default if 0."""
        # 5th gen은 front_center 단일 스크린
        w, h = self.screen_width, self.screen_height
        if w == 0 or h == 0:
            dw, dh = self._DEFAULT_SCREEN_SIZE
            if not getattr(self, "_screen_default_logged", False):
                logger.info("HKMC5thWide screen size 0, using default %dx%d", dw, dh)
                self._screen_default_logged = True
            return dw, dh
        return w, h

    def _request_img(self, left: int, top: int, right: int, bottom: int,
                     filename: str, screen_type_bits: Optional[int] = None) -> None:
        self._img_made = False
        self._img_event.clear()
        self._img_filename = filename

        data = []
        data.append((left  >> 8) & 0xFF); data.append(left  & 0xFF)
        data.append((top   >> 8) & 0xFF); data.append(top   & 0xFF)
        data.append((right >> 8) & 0xFF); data.append(right & 0xFF)
        data.append((bottom >> 8) & 0xFF); data.append(bottom & 0xFF)
        if screen_type_bits is not None:
            data.append((screen_type_bits >> 8) & 0xFF)
            data.append(screen_type_bits & 0xFF)
        with self._send_lock:
            self._make_send_packet(CMD_GETIMG, 0, 0, data)

    def screencap(self, output_path: str, screen_type: str = "front_center",
                  timeout: float = 10.0) -> str:
        """Capture a screenshot and save to output_path (BMP).

        Returns the output path on success, raises on failure.
        """
        w, h = self.get_screen_size(screen_type)
        self._request_img(0, 0, w, h, output_path)

        if not self._img_event.wait(timeout=timeout):
            raise TimeoutError(f"HKMC5thWide screenshot timeout ({timeout}s)")
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"HKMC5thWide screenshot file not created: {output_path}")
        return output_path

    def screencap_bytes(self, screen_type: str = "front_center",
                        fmt: str = "png", timeout: float = 10.0) -> bytes:
        """Capture screenshot and return as PNG/JPEG bytes (BMP → convert)."""
        with self._capture_lock:
            w, h = self.get_screen_size(screen_type)
            self._img_buffer = b""
            self._img_made = False
            self._img_event.clear()
            self._img_filename = ""

            img_data = [0, 0, 0, 0]
            img_data.append((w >> 8) & 0xFF); img_data.append(w & 0xFF)
            img_data.append((h >> 8) & 0xFF); img_data.append(h & 0xFF)
            with self._send_lock:
                self._make_send_packet(CMD_GETIMG, 0, 0, img_data)

            if not self._img_event.wait(timeout=timeout):
                raise TimeoutError(f"HKMC5thWide screenshot timeout ({timeout}s)")
            bmp_bytes = self._img_buffer
            if not bmp_bytes:
                raise ValueError("HKMC5thWide empty image buffer")

        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(bmp_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                if fmt == "jpeg":
                    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
                else:
                    _, buf = cv2.imencode(".png", img)
                return buf.tobytes()
        except Exception:
            pass

        try:
            from PIL import Image
            pil_img = Image.open(io.BytesIO(bmp_bytes))
            bio = io.BytesIO()
            pil_img.save(bio, format="PNG" if fmt == "png" else "JPEG", quality=60)
            return bio.getvalue()
        except Exception:
            pass

        return bmp_bytes

    # ------------------------------------------------------------------
    # Touch input
    # ------------------------------------------------------------------

    def tap(self, x: int, y: int, screen_type: str = "front_center") -> None:
        """Tap at (x, y)."""
        x, y = int(x), int(y)
        with self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                self._lcd_touch(x, y)
                logger.info("[HKMC5thWide TAP] (%d,%d)", x, y)
            time.sleep(0.05)

    def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100,
                   screen_type: str = "front_center") -> None:
        x, y = int(x), int(y)
        interval_sec = interval_ms / 1000.0
        with self._capture_lock:
            with self._send_lock:
                for i in range(count):
                    self._lcd_touch(x, y)
                    if i < count - 1 and interval_sec > 0:
                        time.sleep(interval_sec)
                logger.info("[HKMC5thWide REPEAT_TAP] (%d,%d) ×%d", x, y, count)
            time.sleep(0.05)

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "front_center") -> None:
        """Long press using PRESS_KEY + delay + RELEASE_KEY sub_cmd."""
        x, y = int(x), int(y)
        data = [(x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF]
        with self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                self._make_send_packet(CMD_LCDTOUCH, PRESS_KEY, 0, list(data))
                logger.info("[HKMC5thWide LONG_PRESS] PRESS (%d,%d)", x, y)
                time.sleep(duration_ms / 1000.0)
                self._make_send_packet(CMD_LCDTOUCH, RELEASE_KEY, 0, list(data))
                logger.info("[HKMC5thWide LONG_PRESS] RELEASE (%d,%d) %dms", x, y, duration_ms)
            time.sleep(0.05)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "front_center", duration_ms: int = 0) -> None:
        """Swipe (drag) from (x1, y1) to (x2, y2)."""
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        with self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                self._lcd_drag(x1, y1, x2, y2)
                logger.info("[HKMC5thWide SWIPE] (%d,%d)->(%d,%d)", x1, y1, x2, y2)
            time.sleep(0.05)

    def _lcd_touch(self, x: int, y: int, screen_type: Optional[int] = None) -> None:
        data = [(x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF]
        if screen_type is not None:
            data.append((screen_type >> 8) & 0xFF)
            data.append(screen_type & 0xFF)
        self._make_send_packet(CMD_LCDTOUCH, 0, 0, data)

    def _lcd_drag(self, sx: int, sy: int, ex: int, ey: int,
                  screen_type: Optional[int] = None) -> None:
        data = []
        for v in (sx, sy, ex, ey):
            data.append((v >> 8) & 0xFF)
            data.append(v & 0xFF)
        if screen_type is not None:
            data.append((screen_type >> 8) & 0xFF)
            data.append(screen_type & 0xFF)
        self._make_send_packet(CMD_LCDTOUCH_DRAG, 0, 0, data)

    def lcd_touch_ext(self, pos_fing_idx: list[int], data_list: list[int],
                      screen_type: Optional[int] = None) -> None:
        """Multi-finger touch (원본 lcdTouchExt 포팅).

        Args:
            pos_fing_idx: 손가락 인덱스 리스트
            data_list: 손가락별 [fingIdx, action, xPos, yPos] 반복 (4 * N 개)
            screen_type: 스크린 타입 (None=생략)
        """
        fing_num = len(pos_fing_idx)
        data: list[int] = [fing_num]
        for i in range(fing_num):
            fing_idx = i
            data.append(fing_idx)
            action = data_list[(i * 4) + 1]
            x_pos  = data_list[(i * 4) + 2]
            y_pos  = data_list[(i * 4) + 3]
            data.append((x_pos >> 8) & 0xFF)
            data.append(x_pos & 0xFF)
            data.append((y_pos >> 8) & 0xFF)
            data.append(y_pos & 0xFF)
            data.append(action)
        if screen_type is not None:
            data.append((screen_type >> 8) & 0xFF)
            data.append(screen_type & 0xFF)
        logger.debug("[HKMC5thWide LCD_TOUCH_EXT] fing_num=%d", fing_num)
        with self._send_lock:
            self._make_send_packet(CMD_LCDTOUCHEXT, 0, 0, data)

    async def async_lcd_touch_ext(self, pos_fing_idx: list[int], data_list: list[int],
                                   screen_type: Optional[int] = None) -> None:
        """Async wrapper for lcd_touch_ext."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.lcd_touch_ext, pos_fing_idx, data_list, screen_type)

    # ------------------------------------------------------------------
    # Hardware keys
    # ------------------------------------------------------------------

    def send_key(self, cmd: int, sub_cmd: int, key_data: int,
                 monitor: int = 0x00, direction: Optional[int] = None) -> None:
        """Send a hardware key event (keyExt int/list 방식).

        Args:
            cmd: 키 카테고리 커맨드 (CMD_HKEY, CMD_SWC, CMD_CCP, CMD_RRC)
            sub_cmd: 동작 (SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, DIAL_ACTION)
            key_data: 키 코드 (int)
            monitor: 미사용 시그니처 호환 파라미터 (원본 keyExt에 없음 — 패킷에 포함 안 함)
            direction: 다이얼 방향 (CLOCK=0x00, ANTI_CLOCK=0x01) — ListType[1] 에 해당
        """
        resp = 0xFE
        # 원본 keyExt: IntType → 4바이트, ListType → 4바이트 + 1바이트(direction)
        data = [
            (key_data >> 24) & 0xFF,
            (key_data >> 16) & 0xFF,
            (key_data >> 8) & 0xFF,
            key_data & 0xFF,
        ]
        if direction is not None:
            data.append(direction & 0xFF)

        logger.debug("[HKMC5thWide KEY] cmd=0x%02X sub=0x%02X key=0x%02X dir=%s",
                     cmd, sub_cmd, key_data, direction)
        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, resp, data)

    def send_key_message(self, cmd: int, sub_cmd: int = SHORT_KEY) -> None:
        """Send a message-type key (cmd+subCmd, no data payload).

        RADIO, MAP, NAVI 등 개별 커맨드 바이트를 사용하는 키에 사용.
        """
        logger.debug("[HKMC5thWide KEY_MSG] cmd=0x%02X sub=0x%02X", cmd, sub_cmd)
        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, 0, [])

    def resolve_key(self, key_name: str) -> Optional[dict]:
        """spec default + device override 병합된 키 정보 반환."""
        base = HKMC5TH_WIDE_KEYS.get(key_name, {})
        ov = self._key_overrides.get(key_name, {})
        merged = dict(base)
        for k in ("cmd", "key", "dial", "direction", "msg"):
            if k in ov:
                merged[k] = ov[k]
        if "cmd" not in merged:
            return None
        return merged

    def set_key_overrides(self, overrides: Optional[dict[str, dict]]) -> None:
        """디바이스 키 오버라이드 일괄 갱신."""
        self._key_overrides = dict(overrides or {})

    def get_key_overrides(self) -> dict[str, dict]:
        return dict(self._key_overrides)

    def send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                         monitor: int = 0x00, direction: Optional[int] = None,
                         screen_type: Optional[str] = None,
                         key_source: Optional[int] = None) -> None:
        """Send a hardware key by its name (e.g. 'RADIO', 'HKEY_FMAM', 'SWC_PTT').

        Args:
            key_name: 키 이름 (HKMC5TH_WIDE_KEYS)
            sub_cmd: SHORT_KEY / LONG_KEY / PRESS_KEY / RELEASE_KEY / DIAL_ACTION
            monitor: 미사용 (5th gen 단일 스크린)
            direction: 다이얼 방향 (None=키 정의 기본값 사용)
            screen_type: 미사용 (5th gen 단일 스크린, 시그니처 호환용)
            key_source: 미사용 (5th gen, 시그니처 호환용)
        """
        key_info = self.resolve_key(key_name)
        if not key_info:
            raise ValueError(f"Unknown HKMC5thWide key: {key_name}")

        cmd = key_info["cmd"]
        is_msg = bool(key_info.get("msg"))
        is_dial = bool(key_info.get("dial"))

        with self._capture_lock:
            if is_msg:
                # 메시지 키: 데이터 없이 cmd+subCmd만 전송
                self.send_key_message(cmd, sub_cmd)

            elif is_dial:
                dir_val = direction if direction is not None else key_info.get("direction", CLOCK)
                key_data = key_info["key"]
                self.send_key(cmd, DIAL_ACTION, key_data, monitor, dir_val)

            elif sub_cmd == SHORT_KEY:
                # SHORT_KEY 자체가 디바이스 측에서 press+500ms+release를 자동 처리하는 통합 명령
                # (ref IVIHKMC6thProtocol: "SHORT_KEY = 0x43 # press + 500msec + release").
                # PRESS+SHORT+RELEASE 3중 시퀀스를 보내면 5th_wide 디바이스가 protocol error로 판단해
                # socket을 끊는 회귀 발견 → 단일 SHORT 패킷만 송신.
                key_data = key_info["key"]
                self.send_key(cmd, SHORT_KEY, key_data, monitor, direction)

            elif sub_cmd == LONG_KEY:
                # LONG_KEY도 동일하게 통합 명령으로 처리.
                key_data = key_info["key"]
                self.send_key(cmd, LONG_KEY, key_data, monitor, direction)

            else:
                # PRESS_KEY/RELEASE_KEY 등 호출자가 명시한 sub_cmd 그대로 송신.
                key_data = key_info["key"]
                self.send_key(cmd, sub_cmd, key_data, monitor, direction)

            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Async wrappers (FastAPI / asyncio context)
    # ------------------------------------------------------------------

    async def async_connect(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.connect, timeout)

    async def async_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    async def async_screencap(self, output_path: str, screen_type: str = "front_center",
                               timeout: float = 10.0) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap, output_path, screen_type, timeout)

    async def async_screencap_bytes(self, screen_type: str = "front_center",
                                     fmt: str = "png", timeout: float = 10.0) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap_bytes, screen_type, fmt, timeout)

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
                          screen_type: str = "front_center", duration_ms: int = 300) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.swipe, x1, y1, x2, y2, screen_type, duration_ms)

    async def async_send_key(self, cmd: int, sub_cmd: int, key_data: int,
                              monitor: int = 0x00, direction: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.send_key, cmd, sub_cmd, key_data, monitor, direction)

    async def async_send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                                      monitor: int = 0x00, direction: Optional[int] = None,
                                      screen_type: Optional[str] = None,
                                      key_source: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.send_key_by_name, key_name, sub_cmd, monitor, direction,
            screen_type, key_source,
        )

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def get_info(self) -> dict:
        """Return device info dict."""
        return {
            "host": self.host,
            "port": self.port,
            "connected": self.is_connected,
            "agent_version": self.agent_version,
            "set_main_version": self.set_main_version,
            "set_sub_version": self.set_sub_version,
            "screens": {
                "front_center": {
                    "width": self.screen_width,
                    "height": self.screen_height,
                },
            },
        }
