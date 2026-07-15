"""HKMC 6th protocol service — TCP 소켓 기반 IVI 디바이스 통신.

IVIHKMC6thClient.py에서 프로토콜 로직을 추출, ATS 프레임워크 의존성 제거.
ADBService와 병렬 구조로 스크린샷 캡처, 터치, 키 입력 등을 지원.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import socket
import struct
import tempfile
import threading
import time
import queue
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Protocol constants (from IVIHKMC6thProtocol.py)
# ---------------------------------------------------------------------------
START_BIT = 0x61
END_BIT = 0x6F

CMD_GETIMG = 0x6A
CMD_ATSA_GETVERSION = 0xA0
CMD_ATSA_GETSCREENWIDTHHEIGHT = 0xA3

CMD_LCDTOUCH = 0x69
CMD_LCDTOUCH_DRAG = 0xD6
CMD_LCDTOUCHEXT = 0xB0

# 진단용: 이 집합에 든 cmd의 송신 패킷은 INFO 레벨로 hex 덤프된다.
# 길게터치 동작이 펌웨어에서 받아들여지는지 확인하기 위한 임시 조치 — 검증 후 비울 것.
_DEBUG_LOG_CMDS = {CMD_LCDTOUCH, CMD_LCDTOUCH_DRAG, CMD_LCDTOUCHEXT}

NOTI_CONNECTED = 0x5E

# Key commands
CMD_MKBD = 0x60
CMD_SWC = 0x70       # SWRC (기본)
CMD_SWRC2 = 0x71     # hkccic SWRC2 (CCIC 전용)
CMD_CCP = 0x80
CMD_RRC = 0x90
CMD_MIRROR = 0x92
CMD_CCRC = 0x93      # CCRC (Rear seat remote + rear monitor direct, 2 monitor 전용)

# Sub commands
RELEASE_KEY = 0x41
PRESS_KEY = 0x42
SHORT_KEY = 0x43
LONG_KEY = 0x44
MOVE_KEY = 0x45
DIAL_ACTION = 0x80

# CCRC key action codes (CMD_CCRC=0x93 전용, 일반 KEY와 별도 값 사용)
CCRC_RELEASE = 0x00
CCRC_PRESS = 0x01
CCRC_SHORT = 0x02
CCRC_LONG = 0x03

# CCRC key_source (data[0]) — 어떤 입력 장치에서 온 키인지.
# 값은 IVI 펌웨어 KeySourceType enum 의 ordinal 과 1:1 대응한다.
#   MKBD=0, CCP=1, RRC=2, SWRC=3, MIRROR=4, BT_REAR_LEFT=5, BT_REAR_RIGHT=6,
#   BRRC=7, BRPC=8, BRGP_LEFT=9, BRGP_RIGHT=10, RL_MONITOR=11, RR_MONITOR=12,
#   CCRC_RESET=13, SOFTKEY=14, CTS=15, ATS=16
CCRC_SRC_RRC = 0x02             # 유선 RRC
CCRC_SRC_BRRC = 0x07            # Bluetooth Rear Remote Control (기본)
CCRC_SRC_REAR_LEFT_MONITOR = 0x0B   # RL_MONITOR (Rear Left Monitor)
CCRC_SRC_REAR_RIGHT_MONITOR = 0x0C  # RR_MONITOR (Rear Right Monitor)
CCRC_SRC_RESET = 0x0D           # CCRC_RESET — ccRC Reset Flag Check 용
CCRC_SRC_SOFTKEY = 0x0E         # SOFTKEY — SW Remote Controller (구 SW_VT_RC 개명)
CCRC_SRC_CTS = 0x0F             # CTS — Console Touch Screen (ccRC Slim)
CCRC_SRC_ATS = 0x10             # ATS — Armrest Touch Screen (ccRC Slim)

# CCRC monitor 필드 (data[3]) — 대상 모니터
CCRC_MONITOR_LEFT = 0x01
CCRC_MONITOR_RIGHT = 0x02

# RRC/L_RRC/R_RRC 모니터 별칭
LEFT_MONITOR  = CCRC_MONITOR_LEFT   # 0x01
RIGHT_MONITOR = CCRC_MONITOR_RIGHT  # 0x02

# 다이얼 방향
CLOCK      = 0x00
ANTI_CLOCK = 0x01

# RRC HKEY (버튼) 코드
RRC_HKEY_ENTER        = 0x08
RRC_HKEY_UP           = 0x00
RRC_HKEY_DOWN         = 0x01
RRC_HKEY_LEFT         = 0x03
RRC_HKEY_RIGHT        = 0x06
RRC_HKEY_BACK         = 0x09
RRC_HKEY_MENU         = 0x0A
RRC_HKEY_HOME         = 0x14
RRC_HKEY_POWER_LEFT   = 0x1A
RRC_HKEY_POWER_RIGHT  = 0x1B
RRC_HKEY_VOLUME_LEFT  = 0x17
RRC_HKEY_VOLUME_RIGHT = 0x18

# RRC HKNOB (다이얼) 코드
RRC_HKNOB_JOGDIAL      = 0x00
RRC_HKNOB_VOLUME_LEFT  = 0x02
RRC_HKNOB_VOLUME_RIGHT = 0x03

# Ethernet 전송 커맨드 (legacy IVIHKMC6thProtocol.py / ISAP_Protocol.py 기준)
# 주의: 0x95/0x96은 SWRC_NEXT_KEY_REQ/SWRC_PREV_KEY_REQ로 다른 명령이라 사용 금지.
CMD_ETHERNET         = 0x94
CMD_ETHERNETSIGNAL   = 0xD0  # EthernetSignal (신호 ID + Payload 전송)
CMD_EXECUTESHELLCMD  = 0xE4  # ExecuteShell (쉘 커맨드 문자열 전송)

# Response codes (legacy 기준)
RESPONSE_FAIL = 0x20   # 응답 실패 / 응답 불필요

# Screen type mapping for touch — CCRC_MONITOR_LEFT/RIGHT 값과 정렬.
# (레거시 에이전트 호환을 위해 rear_right=1, rear_left=2 였으나, 최신 HKMC Agent
# 에서는 monitor 바이트가 키와 터치 공통으로 LEFT=1, RIGHT=2 로 해석되어
# 반대 값일 경우 좌우가 뒤섞인다.)
SCREEN_TOUCH_MAP = {
    "front_center": 0,
    "rear_left":  CCRC_MONITOR_LEFT,   # 1
    "rear_right": CCRC_MONITOR_RIGHT,  # 2
}

# Screen type mapping for image capture (bitmask)
SCREEN_CAPTURE_MAP = {
    "cluster": 1,       # 1 << 0
    "front_center": 8,  # 1 << 3
    "rear_left": 32,    # 1 << 5
    "rear_right": 128,  # 1 << 7
}

# Key definitions — HKMC 6th Connected Wide + hkccic.
# 각 디바이스는 info["hkmc_keys"]로 cmd/key/visible/dial을 개별 오버라이드할 수 있다.
HKMC_KEYS = {
    # ---------- MKBD (CMD_MKBD=0x60) — Navi/Non-Navi 공통 ----------
    "MKBD_MAP":         {"cmd": CMD_MKBD, "key": 0x0B},
    "MKBD_NAV":         {"cmd": CMD_MKBD, "key": 0x0C},
    "MKBD_RADIO":       {"cmd": CMD_MKBD, "key": 0x0D},
    "MKBD_MEDIA":       {"cmd": CMD_MKBD, "key": 0x0E},
    "MKBD_CUSTOM":      {"cmd": CMD_MKBD, "key": 0x11},
    "MKBD_SETUP":       {"cmd": CMD_MKBD, "key": 0x12},
    "MKBD_HOME":        {"cmd": CMD_MKBD, "key": 0x14, "variant": "non_navi"},
    "MKBD_PHONE":       {"cmd": CMD_MKBD, "key": 0x29, "variant": "non_navi"},

    # ---------- CCP (CMD_CCP=0x80) ----------
    "CCP_ENTER":        {"cmd": CMD_CCP, "key": 0x08},
    "CCP_UP":           {"cmd": CMD_CCP, "key": 0x00},
    "CCP_DOWN":         {"cmd": CMD_CCP, "key": 0x01},
    "CCP_LEFT":         {"cmd": CMD_CCP, "key": 0x03},
    "CCP_RIGHT":        {"cmd": CMD_CCP, "key": 0x06},
    "CCP_BACK":         {"cmd": CMD_CCP, "key": 0x09},
    "CCP_MENU":         {"cmd": CMD_CCP, "key": 0x0A},
    "CCP_HOME":         {"cmd": CMD_CCP, "key": 0x14},
    "CCP_POWER":        {"cmd": CMD_CCP, "key": 0x19},
    "CCP_TUNE_PUSH":    {"cmd": CMD_CCP, "key": 0x1E},
    "CCP_JOGDIAL_CLOCK_Right": {"cmd": CMD_CCP, "key": 0x00, "dial": True, "direction": 0x00},
    "CCP_JOGDIAL_CLOCK_Left":  {"cmd": CMD_CCP, "key": 0x00, "dial": True, "direction": 0x01},
    "CCP_VOLUME_UP":    {"cmd": CMD_CCP, "key": 0x01, "dial": True, "direction": 0x00},
    "CCP_VOLUME_DOWN":  {"cmd": CMD_CCP, "key": 0x01, "dial": True, "direction": 0x01},
    "CCP_TUNE_UP":      {"cmd": CMD_CCP, "key": 0x04, "dial": True, "direction": 0x00},
    "CCP_TUNE_DOWN":    {"cmd": CMD_CCP, "key": 0x04, "dial": True, "direction": 0x01},

    # ---------- RRC (CMD_RRC=0x90) ----------
    # 참조 스펙은 Navi/Non-Navi 로 구분하지만 실제로는 RRC Type A / Type B 의
    # 하드웨어 타입 차이가 더 지배적이며 Navi 여부와 무관하다 (Gen6 Premium 같은
    # Navi 모델에 Type B RRC 가 탑재되어 RADIO/MEDIA 가 정상 존재할 수 있음).
    # 일괄 필터는 오히려 유효 키를 숨기는 부작용이 있어 variant 태그 없이 전부
    # 노출하고, 사용하지 않는 키는 기존 per-device hkmc_keys override 로 숨긴다.
    "RRC_ENTER":        {"cmd": CMD_RRC, "key": 0x08},
    "RRC_UP":           {"cmd": CMD_RRC, "key": 0x00},
    "RRC_DOWN":         {"cmd": CMD_RRC, "key": 0x01},
    "RRC_LEFT":         {"cmd": CMD_RRC, "key": 0x03},
    "RRC_RIGHT":        {"cmd": CMD_RRC, "key": 0x06},
    "RRC_BACK":         {"cmd": CMD_RRC, "key": 0x09},
    "RRC_MENU":         {"cmd": CMD_RRC, "key": 0x0A},
    "RRC_HOME":         {"cmd": CMD_RRC, "key": 0x14},
    "RRC_POWER_LEFT":   {"cmd": CMD_RRC, "key": 0x1A},
    "RRC_POWER_RIGHT":  {"cmd": CMD_RRC, "key": 0x1B},
    "RRC_VOLUME_LEFT":  {"cmd": CMD_RRC, "key": 0x17},
    "RRC_VOLUME_RIGHT": {"cmd": CMD_RRC, "key": 0x18},
    "RRC_JOGDIAL":                  {"cmd": CMD_RRC, "key": 0x00, "dial": True},
    "RRC_VOLUME_LEFT_DIAL":         {"cmd": CMD_RRC, "key": 0x02, "dial": True},
    "RRC_VOLUME_RIGHT_DIAL":        {"cmd": CMD_RRC, "key": 0x03, "dial": True},
    # RRC Type B 전용(추정) — 단독 소스 선택 / 단일 POWER·VOLUME 키 세트.
    # Type A 장비에선 정의되지 않아 무반응일 수 있음. 필요 시 사용자가 hkmc_keys
    # override 로 개별 숨김 처리.
    "RRC_RADIO":        {"cmd": CMD_RRC, "key": 0x0D},
    "RRC_MEDIA":        {"cmd": CMD_RRC, "key": 0x0E},
    "RRC_MUTE":         {"cmd": CMD_RRC, "key": 0x24},
    "RRC_SEEK_UP":      {"cmd": CMD_RRC, "key": 0x0F},
    "RRC_SEEK_DOWN":    {"cmd": CMD_RRC, "key": 0x10},
    "RRC_PRESET_UP":    {"cmd": CMD_RRC, "key": 0x20},
    "RRC_PRESET_DOWN":  {"cmd": CMD_RRC, "key": 0x21},
    "RRC_POWER":        {"cmd": CMD_RRC, "key": 0x19},
    "RRC_VOLUME":       {"cmd": CMD_RRC, "key": 0x01, "dial": True},

    # ---------- SWRC (CMD_SWC=0x70) ----------
    "SWRC_PTT":         {"cmd": CMD_SWC, "key": 0x22},
    "SWRC_MODE":        {"cmd": CMD_SWC, "key": 0x23},
    "SWRC_MUTE":        {"cmd": CMD_SWC, "key": 0x24},
    "SWRC_SEEK_UP":     {"cmd": CMD_SWC, "key": 0x0F},
    "SWRC_SEEK_DOWN":   {"cmd": CMD_SWC, "key": 0x10},
    "SWRC_SEND":        {"cmd": CMD_SWC, "key": 0x25},
    "SWRC_END":         {"cmd": CMD_SWC, "key": 0x26},
    "SWRC_CUSTOM":      {"cmd": CMD_SWC, "key": 0x11},
    "SWRC_VOLUME_UP":   {"cmd": CMD_SWC, "key": 0x01, "dial": True, "direction": 0x00},
    "SWRC_VOLUME_DOWN": {"cmd": CMD_SWC, "key": 0x01, "dial": True, "direction": 0x01},

    # ---------- MIRROR (CMD_MIRROR=0x92) ----------
    "MIRROR_SOS":                   {"cmd": CMD_MIRROR, "key": 0x27},
    "MIRROR_CONCIERGE":              {"cmd": CMD_MIRROR, "key": 0x2A},
    "MIRROR_CONCIERGE_POI":          {"cmd": CMD_MIRROR, "key": 0x2B},
    "MIRROR_VOICE_LOCAL_SEARCH":     {"cmd": CMD_MIRROR, "key": 0x2C},
    "MIRROR_ROADSIDE_ASSISTANT":     {"cmd": CMD_MIRROR, "key": 0x2D},

    # ---------- CCRC (CMD_CCRC=0x93) — BRRC(Bluetooth Rear Remote Control) 하드키
    # data format: [key_source, key_type, key_status, monitor]
    # key_type 은 IVI 펌웨어 KeyType enum ordinal (아래 주석은 매핑되는 KeyType).
    # ccrc=True: send_key_by_name에서 PRESS/SHORT/LONG/RELEASE 값이 CCRC_* 로 치환된다.
    # 2024 최신 ccRC 키 스펙(BT Key event value 표) 기준 20키 반영.
    "CCRC_UP":           {"cmd": CMD_CCRC, "key": 0x00, "ccrc": True, "source": CCRC_SRC_BRRC},  # UP
    "CCRC_DOWN":         {"cmd": CMD_CCRC, "key": 0x01, "ccrc": True, "source": CCRC_SRC_BRRC},  # DOWN
    "CCRC_LEFT":         {"cmd": CMD_CCRC, "key": 0x03, "ccrc": True, "source": CCRC_SRC_BRRC},  # LEFT
    "CCRC_RIGHT":        {"cmd": CMD_CCRC, "key": 0x06, "ccrc": True, "source": CCRC_SRC_BRRC},  # RIGHT
    "CCRC_ENTER":        {"cmd": CMD_CCRC, "key": 0x08, "ccrc": True, "source": CCRC_SRC_BRRC},  # ENTER (Select/Play·Pause)
    "CCRC_BACK":         {"cmd": CMD_CCRC, "key": 0x09, "ccrc": True, "source": CCRC_SRC_BRRC},  # BACK
    "CCRC_HOME":         {"cmd": CMD_CCRC, "key": 0x14, "ccrc": True, "source": CCRC_SRC_BRRC},  # HOME
    "CCRC_VOLUME_UP":    {"cmd": CMD_CCRC, "key": 0x15, "ccrc": True, "source": CCRC_SRC_BRRC},  # VOLUME_UP
    "CCRC_VOLUME_DOWN":  {"cmd": CMD_CCRC, "key": 0x16, "ccrc": True, "source": CCRC_SRC_BRRC},  # VOLUME_DOWN
    "CCRC_POWER":        {"cmd": CMD_CCRC, "key": 0x19, "ccrc": True, "source": CCRC_SRC_BRRC},  # POWER (On/Off)
    "CCRC_TOGGLE":       {"cmd": CMD_CCRC, "key": 0x37, "ccrc": True, "source": CCRC_SRC_BRRC},  # TOGGLE (L/R Toggle)
    "CCRC_CUSTOM":       {"cmd": CMD_CCRC, "key": 0x11, "ccrc": True, "source": CCRC_SRC_BRRC},  # CUSTOM
    "CCRC_MIC":          {"cmd": CMD_CCRC, "key": 0x3A, "ccrc": True, "source": CCRC_SRC_BRRC},  # VOICE_COMMAND (MIC)
    "CCRC_YOUTUBE":      {"cmd": CMD_CCRC, "key": 0x38, "ccrc": True, "source": CCRC_SRC_BRRC},  # HOTKEY_1 (YouTube)
    "CCRC_NETFLIX":      {"cmd": CMD_CCRC, "key": 0x39, "ccrc": True, "source": CCRC_SRC_BRRC},  # HOTKEY_2 (NETFLIX)
    "CCRC_MUTE":         {"cmd": CMD_CCRC, "key": 0x24, "ccrc": True, "source": CCRC_SRC_BRRC},  # MUTE
    "CCRC_DISPLAY":      {"cmd": CMD_CCRC, "key": 0x2F, "ccrc": True, "source": CCRC_SRC_BRRC},  # DISPLAY (Display On/Off)
    "CCRC_FOLD_UP":      {"cmd": CMD_CCRC, "key": 0x3B, "ccrc": True, "source": CCRC_SRC_BRRC},  # FOLD_UP (Folding up)
    "CCRC_FOLD_DOWN":    {"cmd": CMD_CCRC, "key": 0x3C, "ccrc": True, "source": CCRC_SRC_BRRC},  # FOLD_DOWN (Folding down)
    "CCRC_STAR_LIGHT":   {"cmd": CMD_CCRC, "key": 0x3D, "ccrc": True, "source": CCRC_SRC_BRRC},  # STAR_LIGHT (Star Light On/Off)
    # 구 rear 듀얼모니터 리모컨 전용(단일 BRRC 스펙엔 없음) — 기존 시나리오 호환 위해 유지.
    "CCRC_VOLUME_LEFT":  {"cmd": CMD_CCRC, "key": 0x17, "ccrc": True, "source": CCRC_SRC_BRRC},  # VOLUME_LEFT
    "CCRC_VOLUME_RIGHT": {"cmd": CMD_CCRC, "key": 0x18, "ccrc": True, "source": CCRC_SRC_BRRC},  # VOLUME_RIGHT
    "CCRC_POWER_LEFT":   {"cmd": CMD_CCRC, "key": 0x1A, "ccrc": True, "source": CCRC_SRC_BRRC},  # POWER_LEFT
    "CCRC_POWER_RIGHT":  {"cmd": CMD_CCRC, "key": 0x1B, "ccrc": True, "source": CCRC_SRC_BRRC},  # POWER_RIGHT

    # ---------- hkccic SWRC2 (CMD_SWRC2=0x71) ----------
    "SWRC2_BACK":       {"cmd": CMD_SWRC2, "key": 0x01},
    "SWRC2_UP":         {"cmd": CMD_SWRC2, "key": 0x02},
    "SWRC2_DOWN":       {"cmd": CMD_SWRC2, "key": 0x03},
    "SWRC2_OK":         {"cmd": CMD_SWRC2, "key": 0x04},
    "SWRC2_ENTER":      {"cmd": CMD_SWRC2, "key": 0x05},
    # SWRC2 Optical mouse events (hkccic)
    "SWRC2_SWIPE_UP":            {"cmd": CMD_SWRC2, "key": 0x06},
    "SWRC2_SWIPE_DOWN":          {"cmd": CMD_SWRC2, "key": 0x07},
    "SWRC2_SWIPE_LEFT":          {"cmd": CMD_SWRC2, "key": 0x08},
    "SWRC2_SWIPE_RIGHT":         {"cmd": CMD_SWRC2, "key": 0x09},
    "SWRC2_SWIPE_FAST_UP":       {"cmd": CMD_SWRC2, "key": 0x0A},
    "SWRC2_SWIPE_FAST_DOWN":     {"cmd": CMD_SWRC2, "key": 0x0B},
    "SWRC2_SWIPE_FAST_LEFT":     {"cmd": CMD_SWRC2, "key": 0x0C},
    "SWRC2_SWIPE_FAST_RIGHT":    {"cmd": CMD_SWRC2, "key": 0x0D},
    "SWRC2_DRAG_UP":             {"cmd": CMD_SWRC2, "key": 0x0E},
    "SWRC2_DRAG_DOWN":           {"cmd": CMD_SWRC2, "key": 0x0F},
    "SWRC2_DRAG_LEFT":           {"cmd": CMD_SWRC2, "key": 0x10},
    "SWRC2_DRAG_RIGHT":          {"cmd": CMD_SWRC2, "key": 0x11},
    "SWRC2_TOUCH":               {"cmd": CMD_SWRC2, "key": 0x12},
    "SWRC2_DOUBLE_TOUCH":        {"cmd": CMD_SWRC2, "key": 0x13},

    # ---------- L_RRC / R_RRC (CMD_RRC=0x90) — 좌/우 모니터 지정 RRC ----------
    "L_RRC_ENTER":                    {"cmd": CMD_RRC, "key": RRC_HKEY_ENTER,        "monitor": LEFT_MONITOR},
    "L_RRC_UP":                       {"cmd": CMD_RRC, "key": RRC_HKEY_UP,           "monitor": LEFT_MONITOR},
    "L_RRC_DOWN":                     {"cmd": CMD_RRC, "key": RRC_HKEY_DOWN,         "monitor": LEFT_MONITOR},
    "L_RRC_LEFT":                     {"cmd": CMD_RRC, "key": RRC_HKEY_LEFT,         "monitor": LEFT_MONITOR},
    "L_RRC_RIGHT":                    {"cmd": CMD_RRC, "key": RRC_HKEY_RIGHT,        "monitor": LEFT_MONITOR},
    "L_RRC_BACK":                     {"cmd": CMD_RRC, "key": RRC_HKEY_BACK,         "monitor": LEFT_MONITOR},
    "L_RRC_MENU":                     {"cmd": CMD_RRC, "key": RRC_HKEY_MENU,         "monitor": LEFT_MONITOR},
    "L_RRC_HOME":                     {"cmd": CMD_RRC, "key": RRC_HKEY_HOME,         "monitor": LEFT_MONITOR},
    "L_RRC_POWER_LEFT":               {"cmd": CMD_RRC, "key": RRC_HKEY_POWER_LEFT,   "monitor": LEFT_MONITOR},
    "L_RRC_POWER_RIGHT":              {"cmd": CMD_RRC, "key": RRC_HKEY_POWER_RIGHT,  "monitor": LEFT_MONITOR},
    "L_RRC_VOLUME_LEFT":              {"cmd": CMD_RRC, "key": RRC_HKEY_VOLUME_LEFT,  "monitor": LEFT_MONITOR},
    "L_RRC_VOLUME_RIGHT":             {"cmd": CMD_RRC, "key": RRC_HKEY_VOLUME_RIGHT, "monitor": LEFT_MONITOR},
    "R_RRC_ENTER":                    {"cmd": CMD_RRC, "key": RRC_HKEY_ENTER,        "monitor": RIGHT_MONITOR},
    "R_RRC_UP":                       {"cmd": CMD_RRC, "key": RRC_HKEY_UP,           "monitor": RIGHT_MONITOR},
    "R_RRC_DOWN":                     {"cmd": CMD_RRC, "key": RRC_HKEY_DOWN,         "monitor": RIGHT_MONITOR},
    "R_RRC_LEFT":                     {"cmd": CMD_RRC, "key": RRC_HKEY_LEFT,         "monitor": RIGHT_MONITOR},
    "R_RRC_RIGHT":                    {"cmd": CMD_RRC, "key": RRC_HKEY_RIGHT,        "monitor": RIGHT_MONITOR},
    "R_RRC_BACK":                     {"cmd": CMD_RRC, "key": RRC_HKEY_BACK,         "monitor": RIGHT_MONITOR},
    "R_RRC_MENU":                     {"cmd": CMD_RRC, "key": RRC_HKEY_MENU,         "monitor": RIGHT_MONITOR},
    "R_RRC_HOME":                     {"cmd": CMD_RRC, "key": RRC_HKEY_HOME,         "monitor": RIGHT_MONITOR},
    "R_RRC_POWER_LEFT":               {"cmd": CMD_RRC, "key": RRC_HKEY_POWER_LEFT,   "monitor": RIGHT_MONITOR},
    "R_RRC_POWER_RIGHT":              {"cmd": CMD_RRC, "key": RRC_HKEY_POWER_RIGHT,  "monitor": RIGHT_MONITOR},
    "R_RRC_VOLUME_LEFT":              {"cmd": CMD_RRC, "key": RRC_HKEY_VOLUME_LEFT,  "monitor": RIGHT_MONITOR},
    "R_RRC_VOLUME_RIGHT":             {"cmd": CMD_RRC, "key": RRC_HKEY_VOLUME_RIGHT, "monitor": RIGHT_MONITOR},
    "L_RRC_JOGDIAL_CLOCK":            {"cmd": CMD_RRC, "key": RRC_HKNOB_JOGDIAL,      "monitor": LEFT_MONITOR,  "dial": True, "direction": CLOCK},
    "L_RRC_JOGDIAL_ANTI_CLOCK":       {"cmd": CMD_RRC, "key": RRC_HKNOB_JOGDIAL,      "monitor": LEFT_MONITOR,  "dial": True, "direction": ANTI_CLOCK},
    "L_RRC_VOLUME_LEFT_CLOCK":        {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_LEFT,  "monitor": LEFT_MONITOR,  "dial": True, "direction": CLOCK},
    "L_RRC_VOLUME_LEFT_ANTI_CLOCK":   {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_LEFT,  "monitor": LEFT_MONITOR,  "dial": True, "direction": ANTI_CLOCK},
    "L_RRC_VOLUME_RIGHT_CLOCK":       {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_RIGHT, "monitor": LEFT_MONITOR,  "dial": True, "direction": CLOCK},
    "L_RRC_VOLUME_RIGHT_ANTI_CLOCK":  {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_RIGHT, "monitor": LEFT_MONITOR,  "dial": True, "direction": ANTI_CLOCK},
    "R_RRC_JOGDIAL_CLOCK":            {"cmd": CMD_RRC, "key": RRC_HKNOB_JOGDIAL,      "monitor": RIGHT_MONITOR, "dial": True, "direction": CLOCK},
    "R_RRC_JOGDIAL_ANTI_CLOCK":       {"cmd": CMD_RRC, "key": RRC_HKNOB_JOGDIAL,      "monitor": RIGHT_MONITOR, "dial": True, "direction": ANTI_CLOCK},
    "R_RRC_VOLUME_LEFT_CLOCK":        {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_LEFT,  "monitor": RIGHT_MONITOR, "dial": True, "direction": CLOCK},
    "R_RRC_VOLUME_LEFT_ANTI_CLOCK":   {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_LEFT,  "monitor": RIGHT_MONITOR, "dial": True, "direction": ANTI_CLOCK},
    "R_RRC_VOLUME_RIGHT_CLOCK":       {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_RIGHT, "monitor": RIGHT_MONITOR, "dial": True, "direction": CLOCK},
    "R_RRC_VOLUME_RIGHT_ANTI_CLOCK":  {"cmd": CMD_RRC, "key": RRC_HKNOB_VOLUME_RIGHT, "monitor": RIGHT_MONITOR, "dial": True, "direction": ANTI_CLOCK},
}


def _enable_tcp_keepalive(sock: socket.socket,
                          idle: int = 5, interval: int = 2, count: int = 3) -> None:
    """소켓에 TCP keepalive 를 켜서 half-open 연결(피어 전원 OFF 등)을 OS 가 감지하게 한다.

    HKMC 에이전트 디바이스를 전원 OFF 하면 FIN/RST 없이 연결이 half-open 으로 남는다.
    recv 스레드는 1s 타임아웃을 무시(continue)하므로 죽은 소켓을 영원히 감지 못 하고
    `_connected` 가 거짓 True 로 유지 → 자동 재연결이 발동하지 않는다.
    keepalive 를 켜면 idle + interval*count(기본 ~11s) 후 recv() 가 OSError 를 던져
    recv 스레드가 `_connected=False` 로 내리고, 다음 스텝에서 재연결이 정상 동작한다.

    Windows 는 per-socket idle/interval 을 SIO_KEEPALIVE_VALS(ioctl)로만 설정 가능하고,
    Linux 는 TCP_KEEPIDLE/INTVL/CNT 소켓옵션을 쓴다. 어느 쪽도 실패해도 무해하게 무시.
    """
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    except Exception:
        return
    try:
        if hasattr(socket, "SIO_KEEPALIVE_VALS"):  # Windows
            # (onoff, keepalivetime_ms, keepaliveinterval_ms)
            sock.ioctl(socket.SIO_KEEPALIVE_VALS, (1, idle * 1000, interval * 1000))
        else:  # Linux / 기타 POSIX
            if hasattr(socket, "TCP_KEEPIDLE"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, count)
    except Exception:
        pass


def _calc_crc16(data: list[int]) -> int:
    """CRC16 with 0xC659 polynomial (from IVIHKMC6thClient)."""
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
    """Parse a big-endian 32-bit integer from a byte list."""
    return ((data[offset] << 24) | (data[offset + 1] << 16) |
            (data[offset + 2] << 8) | data[offset + 3])


# HKMC 6th 디바이스 모델 → Navi / Non-Navi 분류
# 참조 스펙: RRC_RADIO/MEDIA/MUTE/SEEK_*/PRESET_*/POWER(단일)/VOLUME(단일) 은
# Non-Navi 전용 키 코드이며, Navi 모델에서는 IVI 가 미정의 키로 처리하여 rear 모니터
# routing 이 실패하고 front_center 로 fallback 된다.
_NAVI_MODELS = {
    "Gen6 Premium",   # HKMC Gen6 Premium — Navi 포함
    "ccIC",
    "ccIC27",
    "CCU2",
    "Connect Wide",
}
_NON_NAVI_MODELS: set[str] = set()  # 아직 알려진 것 없음


def resolve_device_variant(device_model: str) -> str:
    """디바이스 모델명을 'navi' / 'non_navi' / 'unknown' 로 분류.

    미상 모델은 'navi' 기본 — 현행 Premium IVI 들은 대부분 Navi 탑재.
    """
    if not device_model:
        return "navi"
    if device_model in _NAVI_MODELS:
        return "navi"
    if device_model in _NON_NAVI_MODELS:
        return "non_navi"
    return "navi"


def _parse_bgr(value, default=(0, 0, 0)):
    """"B,G,R" 문자열을 (B,G,R) int 튜플로 파싱. 실패 시 default 반환."""
    try:
        parts = [int(p.strip()) for p in str(value).split(",")]
        if len(parts) == 3:
            return tuple(max(0, min(255, p)) for p in parts)
    except (TypeError, ValueError):
        pass
    return default


def _parse_crop(value):
    """"x1,y1,x2,y2" 문자열을 (x1,y1,x2,y2) int 튜플로 파싱. 빈값/실패 시 None.
    x2/y2<=0 이면 이미지 끝까지를 의미(crop 적용 시 해석)."""
    if not value:
        return None
    try:
        parts = [int(p.strip()) for p in str(value).split(",")]
        if len(parts) == 4:
            return tuple(parts)
    except (TypeError, ValueError):
        pass
    return None


def _composite_layers_bgr(bg, ov, mode="alpha", key_color=(0, 0, 0), threshold=24):
    """배경 bg(BGR ndarray) 위에 오버레이 ov(decoded ndarray: BGR/BGRA/gray)를 합성.

    HKMC ccIC27 계기판은 두 소스의 합성이다:
      - 배경 = Linux 측 cluster surface (TCP CMD_GETIMG cluster, 테마/맵)
      - 정보 = QNX 측 `screenshot -display=2` (게이지/경고, 검은 배경)
    검은 배경 위 정보라 chroma(검정 키 제거) 합성이 적합하다.

    mode:
      "alpha"  - ov가 RGBA 4채널이면 alpha-over, 아니면 chroma 폴백.
      "chroma" - ov가 key_color(B,G,R)와 threshold 이상 차이나는 픽셀만 덮음.
    ov가 None이면 bg 단독 반환. 크기가 다르면 ov를 bg 크기로 resize.
    """
    import cv2
    import numpy as np

    if bg is None:
        return None
    if ov is None:
        return bg
    bh, bw = bg.shape[:2]

    # alpha-over 경로 (오버레이가 RGBA 4채널일 때).
    if mode == "alpha" and ov.ndim == 3 and ov.shape[2] == 4:
        if ov.shape[0] != bh or ov.shape[1] != bw:
            ov = cv2.resize(ov, (bw, bh), interpolation=cv2.INTER_AREA)
        ov_bgr = ov[:, :, :3].astype(np.float32)
        alpha = (ov[:, :, 3].astype(np.float32) / 255.0)[:, :, None]
        out = bg.astype(np.float32) * (1.0 - alpha) + ov_bgr * alpha
        return out.astype(np.uint8)

    # chroma 경로 (mode=="chroma" 이거나 alpha 채널이 없는 경우 폴백).
    if ov.ndim == 2:
        ov = cv2.cvtColor(ov, cv2.COLOR_GRAY2BGR)
    elif ov.shape[2] == 4:
        ov = ov[:, :, :3]
    if ov.shape[0] != bh or ov.shape[1] != bw:
        ov = cv2.resize(ov, (bw, bh), interpolation=cv2.INTER_AREA)
    key = np.array(key_color, dtype=np.int16)  # (B,G,R) — cv2와 동일 채널 순서
    diff = np.abs(ov.astype(np.int16) - key).max(axis=2)
    mask = diff > int(threshold)
    out = bg.copy()
    out[mask] = ov[mask]
    return out


def composite_cluster_layers(bg_png: bytes, ov_png: bytes,
                             mode: str = "alpha",
                             key_color=(0, 0, 0),
                             threshold: int = 24):
    """(PNG bytes 입력 버전) 배경 PNG 위에 오버레이 PNG를 합성해 BGR ndarray 반환.

    배경 디코딩 실패 → None. 오버레이 빈값/디코딩 실패 → 배경 단독 반환.
    실제 합성 로직은 _composite_layers_bgr 참고.
    """
    import cv2
    import numpy as np

    bg = cv2.imdecode(np.frombuffer(bg_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bg is None:
        return None
    if not ov_png:
        return bg
    read_flag = cv2.IMREAD_UNCHANGED if mode == "alpha" else cv2.IMREAD_COLOR
    ov = cv2.imdecode(np.frombuffer(ov_png, dtype=np.uint8), read_flag)
    if ov is None:
        return bg
    return _composite_layers_bgr(bg, ov, mode, key_color, threshold)


class HKMC6thService:
    """TCP socket client for HKMC 6th generation IVI devices.

    Provides screenshot capture, touch/swipe input, and hardware key control.
    Each instance manages one TCP connection to one target device.
    """

    def __init__(self, host: str, port: int, device_id: str = "",
                 key_overrides: Optional[dict[str, dict]] = None,
                 device_model: str = "",
                 ssh_username: str = "root", ssh_password: str = "",
                 ssh_port: int = 10022, cluster_resolution: str = "2720x720",
                 cluster_display: str = "1",
                 cluster_overlay_display: str = "",
                 cluster_composite_mode: str = "off",
                 cluster_overlay_key_color: str = "0,0,0",
                 cluster_overlay_threshold: int = 24,
                 cluster_composite_live: bool = True,
                 cluster_crop: str = ""):
        """
        Args:
            key_overrides: 디바이스별 키 오버라이드.
                {name: {"cmd": int, "key": int, "dial": bool, "visible": bool}}
                visible=False면 UI 표시 제외. cmd/key/dial은 spec default를 덮어쓴다.
                차종별로 키 값이 다를 때 사용 (Non-Navi/Navi 차이 등).
            device_model: 디바이스 모델명. 'ccRC'인 경우 monitor 매핑이 일반 HKMC 6th와
                반대(legacy ccIC_Agent: monitor=1=RIGHT, 2=LEFT)이므로 SCREEN_TOUCH_MAP을
                swap한 결과로 라우팅한다.
            ssh_username/ssh_password/ssh_port: 클러스터 캡처용 QNX SSH 자격증명.
                기본값은 legacy CLU_IMG_GET(QNX_INIT)와 동일한 `root` / 빈 패스워드 / 10022.
                (QNX 클러스터 SSH는 dropbear 포트 10022 — 일반 22 아님.)
                cluster screen_type 캡처 시 `screenshot -size=WxH -display=N`을
                SSH로 실행하고 SCP로 BMP를 가져온다. SSH 실패 시 TCP CMD_GETIMG 자동 폴백.
            cluster_resolution: "WxH" 형식 (legacy CLU_IMG_GET 기본 2720x720).
            cluster_display: QNX `screenshot -display=N`의 N (legacy default 1).
                = 배경 플레인(게이지/기본 UI) 디스플레이 인덱스.
            cluster_overlay_display: 오버레이 플레인(알람/경고/정보) 디스플레이 인덱스.
                빈 문자열이면 합성 비활성 → 단일 플레인(기존 동작) 유지.
            cluster_composite_mode: "off"|"alpha"|"chroma".
                - "off"(기본): 오버레이 무시, 배경 단일 플레인만 캡처.
                - "alpha": 오버레이를 RGBA로 받아 alpha-over (알람 없는 곳이 투명한 경우).
                - "chroma": 오버레이가 key_color와 threshold 이상 차이나는 픽셀만 덮음
                  (알람 없는 곳이 단색(예: 검정)으로 채워지는 경우).
                실디바이스 동작이 미지수이므로 두 모드를 모두 지원한다. "alpha"부터
                시도하고 알람이 검은 박스로 나오면 "chroma"+키=검정으로 전환한다.
            cluster_overlay_key_color: chroma 모드의 키 색 "B,G,R" (기본 "0,0,0"=검정).
            cluster_overlay_threshold: chroma 모드 키 색 허용 오차 (기본 24).
            cluster_composite_live: 라이브 미러링에서도 합성할지 여부 (기본 True).
                False면 라이브 경로는 배경 단일 플레인만 캡처(프레임당 SSH 왕복 절약),
                결과/비교 캡처는 계속 합성한다.
        """
        self.host = host
        self.port = port
        self.device_id = device_id
        self.device_model = device_model
        # CCRC 디바이스는 ccIC_Agent legacy 매핑 사용 (REAR_R=1, REAR_L=2).
        # device_model이 비어있거나 케이스가 다를 수 있어(예: device_id "CCRC_1"),
        # device_model·device_id 양쪽을 대소문자 무시 substring으로 판별한다.
        # (device_manager Gen5 자동 마이그레이션과 동일한 패턴 — line ~2525)
        _ccrc_dm = (device_model or "").lower()
        _ccrc_did = (device_id or "").lower()
        self._is_ccrc_legacy_monitor = "ccrc" in _ccrc_dm or "ccrc" in _ccrc_did
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})

        # 클러스터 SSH 캡처 설정 (legacy CLU_IMG_GET 호환). 기본 root/빈 패스워드(ICAS QNX 패턴).
        self.ssh_username = ssh_username if ssh_username is not None else "root"
        if not self.ssh_username:
            self.ssh_username = "root"
        self.ssh_password = ssh_password if ssh_password is not None else ""
        self.ssh_port = int(ssh_port) if ssh_port else 10022
        try:
            cw_s, ch_s = str(cluster_resolution).lower().split("x")
            self.cluster_width = int(cw_s)
            self.cluster_height = int(ch_s)
        except Exception:
            self.cluster_width, self.cluster_height = 2720, 720
        self.cluster_display = str(cluster_display) if cluster_display is not None else "1"
        # 클러스터 2-레이어 합성 (배경 플레인 + 알람/정보 오버레이 플레인).
        self.cluster_overlay_display = str(cluster_overlay_display or "").strip()
        # off=TCP 배경 단독(front 방식), chroma/alpha=배경+정보 합성, ssh=정보 단독(legacy).
        _mode = str(cluster_composite_mode or "off").strip().lower()
        if _mode not in ("off", "alpha", "chroma", "ssh"):
            _mode = "off"
        self.cluster_composite_mode = _mode
        self.cluster_overlay_key_color = _parse_bgr(cluster_overlay_key_color, (0, 0, 0))
        try:
            self.cluster_overlay_threshold = int(cluster_overlay_threshold)
        except (TypeError, ValueError):
            self.cluster_overlay_threshold = 24
        self.cluster_composite_live = bool(cluster_composite_live)
        # cluster crop "x1,y1,x2,y2" (x2/y2<=0 = 끝까지). 빈값=crop 없음.
        # ccIC27 QNX display=2는 좌측에만 내용이 있고 우측이 비어 우측을 잘라낸다.
        self.cluster_crop = _parse_crop(cluster_crop)
        # cluster 합성: 배경(TCP CMD_GETIMG cluster, Linux) + 정보(QNX SSH screenshot
        # display=cluster_display). mode가 alpha/chroma이고 SSH 자격증명이 있으면 활성화.
        self._composite_enabled = (
            self.cluster_composite_mode in ("alpha", "chroma")
            and bool(self.ssh_username)
        )
        self._cluster_ssh = None  # paramiko.SSHClient (lazy)
        self._cluster_ssh_lock = threading.Lock()
        # SSH 캡처 인증/연결 실패 시 매 프레임 재시도 폭주를 막는 cooldown 데드라인(monotonic).
        # 이 시각 전에는 SSH를 건너뛰고 바로 TCP CMD_GETIMG로 캡처한다.
        self._cluster_ssh_fail_until = 0.0

        self._socket: Optional[socket.socket] = None
        self._connected = False
        self._recv_thread: Optional[threading.Thread] = None
        self._exit_flag = False
        self._send_lock = threading.Lock()  # 송신 시퀀스 보호 (press-release 등)
        self._capture_lock = threading.Lock()  # 스크린샷 캡처 직렬화
        # 입력(키/탭/스와이프) 진행 카운터 — screencap이 이 동안 lock 획득을 양보.
        # GIL 보장으로 int read/write 는 atomic 이라 별도 lock 불필요.
        self._input_pending = 0

        # Receive state
        self._recv_queue: queue.Queue = queue.Queue()
        self._recv_complete = True
        self._recv_packet_len = 0
        self._recv_data = ""

        # Image capture state
        self._img_event = threading.Event()
        self._img_filename = ""
        self._img_made = False
        self._img_buffer: bytes = b""  # 인메모리 BMP 데이터

        # Screen sizes (populated after reqScreenSize)
        self._screen_size_event = threading.Event()
        self.screen_width_front = 0
        self.screen_height_front = 0
        self.screen_width_rear_l = 0
        self.screen_height_rear_l = 0
        self.screen_width_rear_r = 0
        self.screen_height_rear_r = 0
        self.screen_width_cluster = 1920
        self.screen_height_cluster = 720

        # Version info
        self.agent_version = ""

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> bool:
        """Connect to the HKMC agent and start receive thread."""
        if self._socket:
            logger.warning("Already connected to %s:%d", self.host, self.port)
            return True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # half-open 감지: 디바이스 전원 OFF 시 OS 가 ~11s 내 죽은 소켓을 감지하게 한다.
            _enable_tcp_keepalive(self._socket)
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

        # Wait for handshake (13 bytes) — 소켓 타임아웃으로 무한 블록 방지
        deadline = time.time() + timeout
        self._connected = False
        while not self._connected and time.time() < deadline:
            try:
                remaining = max(0.1, deadline - time.time())
                self._socket.settimeout(remaining)
                raw = self._socket.recv(13)
                if not raw:
                    logger.error("HKMC handshake: peer closed before handshake (%s:%d)", self.host, self.port)
                    break
                hex_val = raw.hex()
                if hex_val in ("6161000000035e002185fd6f6f", "6161000000035e0000df856f6f"):
                    self._connected = True
                    logger.info("HKMC agent connected: %s:%d", self.host, self.port)
                else:
                    logger.warning("Invalid handshake: %s", hex_val)
                    break
            except socket.timeout:
                logger.error("HKMC handshake recv timeout (%s:%d)", self.host, self.port)
                break
            except socket.error as e:
                logger.error("Socket error during handshake: %s", e)
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None
                return False

        if not self._connected:
            logger.error("Handshake failed for %s:%d", self.host, self.port)
            try:
                self._socket.close()
            except Exception:
                pass
            self._socket = None
            return False

        # 핸드셰이크 완료 — receive thread는 블로킹 모드로 동작
        try:
            self._socket.settimeout(None)
        except Exception:
            pass

        # Start receive thread
        self._exit_flag = False
        self._recv_thread = threading.Thread(
            target=self._receive_thread, name=f"hkmc6th-recv-{self.device_id}", daemon=True
        )
        self._recv_thread.start()

        # 초기화 시퀀스 (레거시와 동일: version → 대기 → screen size → 대기)
        self._req_ats_agent_version()
        time.sleep(0.5)
        self._req_screen_size()
        # screen size 수신 대기 (Agent가 키 명령을 받으려면 초기화 완료 필요)
        if self.screen_height_front == 0:
            logger.warning("Screen size not received, retrying...")
            time.sleep(1)
            self._req_screen_size()

        return True

    def disconnect(self) -> None:
        """Close connection and stop receive thread."""
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
        # 클러스터 SSH 세션도 함께 종료
        with self._cluster_ssh_lock:
            if self._cluster_ssh is not None:
                try:
                    self._cluster_ssh.close()
                except Exception:
                    pass
                self._cluster_ssh = None
        logger.info("HKMC disconnected: %s:%d", self.host, self.port)

    @property
    def is_connected(self) -> bool:
        return self._connected and self._socket is not None

    # ------------------------------------------------------------------
    # Packet send
    # ------------------------------------------------------------------

    @contextmanager
    def _input_priority(self):
        """입력(키/탭/스와이프) 진행 표시 컨텍스트.

        이 컨텍스트가 활성화된 동안 `screencap_bytes` 는 새 캡처 lock 획득을
        잠시 양보(최대 0.5초)하여 입력 요청이 빠르게 lock 을 잡도록 한다.
        라이브 미러링이 _capture_lock 을 점유한 상태에서 키/탭이 2~3초씩
        지연되던 문제 완화용.
        """
        self._input_pending += 1
        try:
            yield
        finally:
            if self._input_pending > 0:
                self._input_pending -= 1

    def _send_raw(self, packet: list[int]) -> None:
        """Send raw packet bytes to socket."""
        if not self._socket:
            raise ConnectionError("Not connected to HKMC agent")
        msg = bytearray(packet)
        try:
            self._socket.send(msg)
        except (ConnectionResetError, ConnectionAbortedError, OSError) as e:
            # WinError 10054 등: 원격 호스트 연결 끊김 → 자동 disconnect
            logger.warning("HKMC connection lost (device=%s): %s", self.device_id, e)
            self.disconnect()
            raise ConnectionError(f"HKMC connection lost: {e}")

    def _make_send_packet(self, cmd: int, sub_cmd: int, resp: int, data: list[int]) -> None:
        """Build and send a framed packet with CRC16."""
        agent_cmd = [cmd, sub_cmd, resp] + data
        crc = _calc_crc16(agent_cmd)
        logger.debug("[HKMC SEND] cmd=0x%02X sub=0x%02X resp=0x%02X data_len=%d", cmd, sub_cmd, resp, len(data))
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

        # TODO: 패킷 hex 진단 종료 후 _DEBUG_LOG_CMDS 비우거나 줄을 삭제할 것
        if cmd in _DEBUG_LOG_CMDS:
            logger.info("[HKMC PACKET] cmd=0x%02X %s", cmd, ' '.join(f'{b:02X}' for b in packet))
        else:
            logger.debug("[HKMC PACKET] %s", ' '.join(f'{b:02X}' for b in packet))
        self._send_raw(packet)

    # ------------------------------------------------------------------
    # Receive thread
    # ------------------------------------------------------------------

    def _receive_thread(self) -> None:
        """Background thread that receives and decodes packets."""
        logger.info("Receive thread started for %s:%d", self.host, self.port)
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
                        logger.warning("Bad packet header")
                        self._recv_complete = True
                        self._recv_data = ""
                else:
                    remaining = self._recv_packet_len + 4  # cmd+crc+end
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
                        logger.warning("Packet length mismatch")
                        self._recv_complete = True

            except (socket.error, OSError):
                if not self._exit_flag:
                    logger.error("Receive thread socket error")
                    # stale state 방지: 다음 _send_raw 가 호출되기 전이라도
                    # is_connected 가 정확히 False 를 반환하도록 flag 만 내림.
                    # socket close 는 _send_raw 의 ConnectionError 핸들러
                    # 또는 외부 reconnect 경로가 책임진다 — 여기서 close 하면
                    # 동시 송신 스레드와의 race 가능.
                    self._connected = False
                break
            except Exception as e:
                if not self._exit_flag:
                    logger.error("Receive thread error: %s", e)
                    self._connected = False
                break

        logger.info("Receive thread ended for %s:%d", self.host, self.port)

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

            if cmd == NOTI_CONNECTED:
                self._connected = True
                logger.info("Agent connection notification received")

            elif cmd == CMD_ATSA_GETVERSION:
                self.agent_version = data_str if data_len > 0 else ""
                logger.info("Agent version: %s", self.agent_version)

            elif cmd == CMD_ATSA_GETSCREENWIDTHHEIGHT:
                data = [ord(c) for c in data_str]
                if len(data) >= 8:
                    self.screen_width_front = _parse_int32(data, 0)
                    self.screen_height_front = _parse_int32(data, 4)
                if len(data) >= 24:
                    self.screen_width_rear_l = _parse_int32(data, 8)
                    self.screen_height_rear_l = _parse_int32(data, 12)
                    self.screen_width_rear_r = _parse_int32(data, 16)
                    self.screen_height_rear_r = _parse_int32(data, 20)
                logger.info(
                    "Screen sizes: front=%dx%d, rear_l=%dx%d, rear_r=%dx%d, cluster=%dx%d",
                    self.screen_width_front, self.screen_height_front,
                    self.screen_width_rear_l, self.screen_height_rear_l,
                    self.screen_width_rear_r, self.screen_height_rear_r,
                    self.screen_width_cluster, self.screen_height_cluster,
                )
                self._screen_size_event.set()

            elif cmd == CMD_GETIMG:
                # Image data received — store in memory buffer
                raw_bytes = data_str.encode("iso-8859-1")
                self._img_buffer = raw_bytes
                if self._img_filename:
                    with open(self._img_filename, "wb") as f:
                        f.write(raw_bytes)
                self._img_made = True
                self._img_event.set()
                logger.debug("Image received: %d bytes", len(raw_bytes))

    # ------------------------------------------------------------------
    # Info requests
    # ------------------------------------------------------------------

    def _req_ats_agent_version(self) -> None:
        self._make_send_packet(CMD_ATSA_GETVERSION, 0, 0, [])

    def _req_screen_size(self) -> None:
        self._screen_size_event.clear()
        self._make_send_packet(CMD_ATSA_GETSCREENWIDTHHEIGHT, 0, 0, [])
        self._screen_size_event.wait(timeout=5)

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    # 화면 크기를 응답하지 않는 에이전트용 기본값
    _DEFAULT_SCREEN_SIZES = {
        "front_center": (1920, 720),
        "rear_left":    (1920, 720),
        "rear_right":   (1920, 720),
        "cluster":      (1920, 720),
    }

    def get_screen_size(self, screen_type: str = "front_center") -> tuple[int, int]:
        """Return (width, height) for the given screen type. Falls back to defaults if 0."""
        if screen_type == "front_center":
            w, h = self.screen_width_front, self.screen_height_front
        elif screen_type == "rear_left":
            w, h = self.screen_width_rear_l, self.screen_height_rear_l
        elif screen_type == "rear_right":
            w, h = self.screen_width_rear_r, self.screen_height_rear_r
        elif screen_type == "cluster":
            w, h = self.screen_width_cluster, self.screen_height_cluster
        else:
            w, h = self.screen_width_front, self.screen_height_front
        # 0이면 기본값 사용 (최초 1회만 로그)
        if w == 0 or h == 0:
            dw, dh = self._DEFAULT_SCREEN_SIZES.get(screen_type, (1920, 720))
            if not getattr(self, '_screen_default_logged', False):
                logger.info("Screen size 0 for %s, using default %dx%d", screen_type, dw, dh)
                self._screen_default_logged = True
            return dw, dh
        return w, h

    def _request_img(self, left: int, top: int, right: int, bottom: int,
                     filename: str, screen_type_bits: Optional[int] = None) -> None:
        """Send image capture request to agent."""
        self._img_made = False
        self._img_event.clear()
        self._img_filename = filename

        data = []
        data.append((left >> 8) & 0xFF)
        data.append(left & 0xFF)
        data.append((top >> 8) & 0xFF)
        data.append(top & 0xFF)
        data.append((right >> 8) & 0xFF)
        data.append(right & 0xFF)
        data.append((bottom >> 8) & 0xFF)
        data.append(bottom & 0xFF)
        if screen_type_bits is not None:
            data.append((screen_type_bits >> 8) & 0xFF)
            data.append(screen_type_bits & 0xFF)

        with self._send_lock:
            self._make_send_packet(CMD_GETIMG, 0, 0, data)

    def screencap(self, output_path: str, screen_type: str = "front_center",
                  timeout: float = 10.0, composite: bool = True) -> str:
        """Capture a screenshot and save to output_path.

        cluster + SSH 자격증명이 있으면 SSH 경로로 캡처 후 파일에 저장.
        그 외는 TCP CMD_GETIMG (BMP).

        composite: cluster 합성(배경 TCP + 정보 SSH) 적용 여부 (설정 시에만 유효).

        Returns the output path on success, raises on failure.
        """
        if screen_type == "cluster":
            # cluster는 합성/SSH/TCP 분기가 screencap_bytes에 일원화되어 있으므로 위임.
            ext = os.path.splitext(output_path)[1].lower().lstrip(".") or "png"
            fmt = "jpeg" if ext in ("jpg", "jpeg") else "png"
            data = self.screencap_bytes(screen_type="cluster", fmt=fmt,
                                        timeout=timeout, composite=composite)
            with open(output_path, "wb") as f:
                f.write(data)
            return output_path

        w, h = self.get_screen_size(screen_type)
        screen_bits = SCREEN_CAPTURE_MAP.get(screen_type)

        self._request_img(0, 0, w, h, output_path, screen_bits)

        # Wait for image
        if not self._img_event.wait(timeout=timeout):
            raise TimeoutError(f"Screenshot timeout ({timeout}s) for {screen_type}")

        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Screenshot file not created: {output_path}")

        return output_path

    def screencap_bytes(self, screen_type: str = "front_center",
                        fmt: str = "png", timeout: float = 10.0,
                        composite: bool = True) -> bytes:
        """Capture screenshot and return as PNG/JPEG bytes.

        cluster screen_type + SSH 자격증명이 설정되어 있으면 legacy CLU_IMG_GET와
        동일한 SSH+screenshot+SCP 경로로 캡처. 그 외는 TCP CMD_GETIMG.
        The agent sends BMP format. We convert to the requested format.
        _capture_lock으로 동시 호출을 직렬화하여 _img_event 경쟁 방지.
        """
        # 입력 진행 중이면 캡처 우선순위 양보 (최대 0.5초).
        # 라이브 미러링이 매 프레임 _capture_lock 을 점유하므로 키/탭이 lock 을
        # 기다리며 2~3초 지연되는 회귀를 완화한다. cluster SSH 경로도 동일하게
        # 입력을 양보하도록 cluster 분기 앞에 둔다.
        _yield_deadline = time.monotonic() + 0.5
        while self._input_pending > 0 and time.monotonic() < _yield_deadline:
            time.sleep(0.02)

        # cluster 캡처 전략 (cluster_composite_mode):
        #   - "chroma"/"alpha" : 배경(TCP CMD_GETIMG cluster) + 정보(QNX SSH display) 합성
        #   - "ssh"            : 정보만 (QNX SSH 단독, legacy ccIC 등)
        #   - "off"(기본)      : 배경 단독 (TCP CMD_GETIMG, front_center와 동일 경로)
        # composite=False(라이브 토글) 또는 cooldown 중이면 SSH/합성을 건너뛴다.
        if screen_type == "cluster" and composite and not self._cluster_ssh_in_cooldown():
            if self._composite_enabled:
                try:
                    data = self._capture_cluster_composite_bytes(fmt, timeout)
                    self._cluster_ssh_fail_until = 0.0  # 성공 → cooldown 해제
                    return data
                except Exception as e:
                    self._note_cluster_ssh_failure(e)
                    logger.warning("HKMC cluster composite failed, falling back to TCP bg: %s", e)
            elif self.cluster_composite_mode == "ssh" and self.ssh_username:
                try:
                    data = self._screencap_cluster_via_ssh(fmt=fmt, timeout=timeout)
                    self._cluster_ssh_fail_until = 0.0
                    return data
                except Exception as e:
                    self._note_cluster_ssh_failure(e)
                    logger.warning("HKMC cluster SSH capture failed, falling back to TCP: %s", e)

        # TCP 경로 (cluster 배경 단독 = front_center와 동일 방식, 또는 front_center/rear_*)
        bmp_bytes = self._capture_tcp_raw(screen_type, timeout)
        return self._encode_tcp_bmp(bmp_bytes, fmt)

    def _capture_tcp_raw(self, screen_type: str, timeout: float) -> bytes:
        """TCP CMD_GETIMG로 해당 screen을 캡처해 raw BMP bytes 반환.
        front_center/rear_*/cluster 모두 동일 — 에이전트(Linux)가 컴포지터 surface를 떠서 BMP로 응답.
        cluster의 경우 이 결과 = Linux 측 cluster 배경(테마/맵)."""
        with self._capture_lock:
            w, h = self.get_screen_size(screen_type)
            screen_bits = SCREEN_CAPTURE_MAP.get(screen_type)

            self._img_buffer = b""
            self._img_made = False
            self._img_event.clear()
            self._img_filename = ""

            img_data = [0, 0, 0, 0]  # left=0, top=0
            img_data.append((w >> 8) & 0xFF)
            img_data.append(w & 0xFF)
            img_data.append((h >> 8) & 0xFF)
            img_data.append(h & 0xFF)
            if screen_bits is not None:
                img_data.append((screen_bits >> 8) & 0xFF)
                img_data.append(screen_bits & 0xFF)

            with self._send_lock:
                self._make_send_packet(CMD_GETIMG, 0, 0, img_data)

            if not self._img_event.wait(timeout=timeout):
                raise TimeoutError(f"Screenshot timeout ({timeout}s) for {screen_type}")

            bmp_bytes = self._img_buffer
            if not bmp_bytes:
                raise ValueError("Empty image buffer")
        return bmp_bytes

    @staticmethod
    def _encode_tcp_bmp(bmp_bytes: bytes, fmt: str) -> bytes:
        """TCP 캡처 BMP → 요청 fmt. cv2 실패 시 PIL, 그래도 실패면 raw. (jpeg q=30, 라이브 대역폭)"""
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(bmp_bytes, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                if fmt == "jpeg":
                    _, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 30])
                else:
                    _, buf = cv2.imencode(".png", img)
                return buf.tobytes()
        except Exception:
            pass
        try:
            from PIL import Image
            pil_img = Image.open(io.BytesIO(bmp_bytes))
            bio = io.BytesIO()
            pil_img.save(bio, format="PNG" if fmt == "png" else "JPEG", quality=30)
            return bio.getvalue()
        except Exception:
            pass
        return bmp_bytes

    def _capture_cluster_composite_bytes(self, fmt: str, timeout: float) -> bytes:
        """완성 cluster = 배경(TCP CMD_GETIMG cluster) + 정보(QNX SSH screenshot display) 합성.

        ccIC27: Linux가 배경(테마/맵)을, QNX가 정보(게이지/경고, 검은 배경)를 렌더 →
        chroma(검정 키 제거)로 정보를 배경 위에 올린다.
        """
        import cv2
        import numpy as np

        # 합성은 캡처 2회(TCP 배경 + SSH 정보)라 라이브의 빡빡한 timeout(3s)으론 부족.
        # 디바이스 캡처가 프레임당 ~1초대라 각 소스에 넉넉한 하한을 둔다.
        cap_timeout = max(timeout, 8.0)

        # 1) 배경 = TCP CMD_GETIMG cluster (Linux surface)
        bg_bmp = self._capture_tcp_raw("cluster", cap_timeout)
        bg = cv2.imdecode(np.frombuffer(bg_bmp, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bg is None:
            raise RuntimeError("cluster TCP background decode failed")

        # 2) 정보 = QNX SSH screenshot display=cluster_display (검은 배경 위 정보)
        info_png = self._capture_one_plane(self.cluster_display, cap_timeout)
        read_flag = cv2.IMREAD_UNCHANGED if self.cluster_composite_mode == "alpha" else cv2.IMREAD_COLOR
        ov = cv2.imdecode(np.frombuffer(info_png, dtype=np.uint8), read_flag) if info_png else None

        # 3) 합성 (정보 크기가 다르면 배경 크기로 resize)
        img = _composite_layers_bgr(bg, ov, self.cluster_composite_mode,
                                    self.cluster_overlay_key_color,
                                    self.cluster_overlay_threshold)
        return self._encode_bgr(self._apply_cluster_crop(img), fmt)

    # ------------------------------------------------------------------
    # Cluster screenshot via SSH+SCP (legacy CLU_IMG_GET 호환)
    # ------------------------------------------------------------------
    def _cluster_ssh_in_cooldown(self) -> bool:
        """SSH 캡처 실패 cooldown 중이면 True (이 동안 SSH 건너뛰고 TCP 사용)."""
        return time.monotonic() < self._cluster_ssh_fail_until

    def _note_cluster_ssh_failure(self, exc) -> None:
        """SSH 캡처 실패를 기록하고 cooldown을 설정해 매 프레임 재시도 폭주를 방지.

        - 인증 실패(자격증명 문제): 비밀번호를 고쳐 재연결하기 전엔 회복 불가 →
          긴 cooldown(120s). 재연결 시 새 인스턴스가 생성되어 상태가 초기화된다.
        - 그 외(타임아웃/네트워크): 짧은 cooldown(10s)으로 일시 장애에 빠르게 회복.
        """
        msg = str(exc)
        is_auth = ("Authentication" in msg) or ("authentication" in msg.lower())
        cooldown = 120.0 if is_auth else 10.0
        self._cluster_ssh_fail_until = time.monotonic() + cooldown
        if is_auth:
            logger.warning(
                "HKMC cluster SSH 인증 실패 — %.0fs 동안 SSH를 건너뛰고 TCP로 캡처합니다. "
                "SSH 자격증명(사용자/비밀번호)을 확인하고 재연결하세요.", cooldown)

    def _get_cluster_ssh(self):
        """Lazy paramiko SSHClient — keepalive로 재인증 방지. 죽었으면 재연결."""
        import paramiko

        with self._cluster_ssh_lock:
            ssh = self._cluster_ssh
            if ssh is not None:
                try:
                    t = ssh.get_transport()
                    if t is not None and t.is_active():
                        return ssh
                except Exception:
                    pass
                try:
                    ssh.close()
                except Exception:
                    pass
                self._cluster_ssh = None

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            logger.info("HKMC cluster SSH connecting: %s@%s:%d (pw=%s)",
                        self.ssh_username, self.host, self.ssh_port,
                        "set" if self.ssh_password else "EMPTY")
            ssh.connect(
                hostname=self.host,
                port=self.ssh_port,
                username=self.ssh_username,
                password=self.ssh_password,
                timeout=10,
                allow_agent=False,
                look_for_keys=False,
            )
            try:
                t = ssh.get_transport()
                if t is not None:
                    t.set_keepalive(30)
            except Exception:
                pass
            self._cluster_ssh = ssh
            logger.info("HKMC cluster SSH connected: %s@%s:%d",
                        self.ssh_username, self.host, self.ssh_port)
            return ssh

    @staticmethod
    def _exec_and_log(ssh, cmd: str, timeout: float) -> None:
        """SSH 명령 실행 후 exit status가 0이 아니면 stderr를 경고 로깅."""
        stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
        try:
            stdin.close()
        except Exception:
            pass
        try:
            exit_status = stdout.channel.recv_exit_status()
        except Exception:
            exit_status = -1
        if exit_status != 0:
            err = ""
            try:
                err = stderr.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            logger.warning("HKMC cluster screenshot exit=%d stderr=%r",
                           exit_status, err)

    @staticmethod
    def _scp_get_bytes(transport, remote_path: str, timeout: float) -> bytes:
        """SCP로 원격 파일을 메모리(bytes)로 받는다. 일부 scp 버전은 file-like
        local_path를 지원하지 않으므로 임시 파일 폴백."""
        from scp import SCPClient
        buf = io.BytesIO()
        try:
            with SCPClient(transport, socket_timeout=timeout) as scp:
                scp.get(remote_path, local_path=buf)
            return buf.getvalue()
        except TypeError:
            import tempfile, os
            tmp_dir = tempfile.mkdtemp(prefix="hkmc_cluster_")
            local = os.path.join(tmp_dir, os.path.basename(remote_path) or "CLU_IMAGE.png")
            try:
                with SCPClient(transport, socket_timeout=timeout) as scp:
                    scp.get(remote_path, local)
                with open(local, "rb") as f:
                    return f.read()
            finally:
                try:
                    os.remove(local)
                except Exception:
                    pass
                try:
                    os.rmdir(tmp_dir)
                except Exception:
                    pass

    def _capture_one_plane(self, display, timeout: float) -> bytes:
        """단일 디스플레이 플레인을 캡처해 raw(PNG) bytes 반환."""
        remote_path = "/CLU_IMAGE.png"
        with self._capture_lock:
            ssh = self._get_cluster_ssh()
            cmd = (
                "mount -o remount -rw / 2>/dev/null; "
                f"rm -f {remote_path}; "
                f"screenshot -size={self.cluster_width}x{self.cluster_height} "
                f"-display={display} -file={remote_path}"
            )
            self._exec_and_log(ssh, cmd, timeout)
            data = self._scp_get_bytes(ssh.get_transport(), remote_path, timeout)
            try:
                ssh.exec_command(f"rm -f {remote_path}")
            except Exception:
                pass
        return data

    def _apply_cluster_crop(self, img):
        """self.cluster_crop=(x1,y1,x2,y2)로 BGR 이미지를 잘라낸다. x2/y2<=0=끝까지.
        crop 미설정/이미지 None이면 원본 반환."""
        if img is None or not self.cluster_crop:
            return img
        h, w = img.shape[:2]
        x1, y1, x2, y2 = self.cluster_crop
        x1 = max(0, min(x1, w))
        y1 = max(0, min(y1, h))
        x2 = w if (x2 <= 0 or x2 > w) else x2
        y2 = h if (y2 <= 0 or y2 > h) else y2
        if x2 <= x1 or y2 <= y1:
            return img
        return img[y1:y2, x1:x2]

    @staticmethod
    def _encode_bgr(img, fmt: str) -> bytes:
        """BGR ndarray → 요청 fmt(jpeg/png) bytes."""
        import cv2
        if fmt == "jpeg":
            _, b = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        else:
            _, b = cv2.imencode(".png", img)
        return b.tobytes()

    @staticmethod
    def _encode_cluster_raw(raw: bytes, fmt: str) -> bytes:
        """캡처 raw(PNG/BMP) bytes → 요청 fmt로 변환. cv2 실패 시 PIL, 그래도 실패면 raw."""
        try:
            import cv2
            import numpy as np
            arr = np.frombuffer(raw, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is not None:
                return HKMC6thService._encode_bgr(img, fmt)
        except Exception:
            pass
        try:
            from PIL import Image
            pil = Image.open(io.BytesIO(raw))
            bio = io.BytesIO()
            pil.save(bio, format="PNG" if fmt == "png" else "JPEG", quality=60)
            return bio.getvalue()
        except Exception:
            pass
        return raw

    def _screencap_cluster_via_ssh(self, fmt: str = "png", timeout: float = 10.0) -> bytes:
        """QNX SSH로 cluster 정보 플레인(`screenshot -display=cluster_display`)을 캡처.
        legacy CLU_IMG_GET 흐름: mount rw → screenshot → SCP → rm → fmt 변환.
        ccIC27에선 이 결과 = 검은 배경 위 게이지/경고 '정보 레이어'.
        (배경+정보 합성은 _capture_cluster_composite_bytes에서 TCP 배경과 결합한다.)
        """
        try:
            from scp import SCPClient  # noqa: F401  (가용성 확인)
        except ImportError as e:
            raise RuntimeError("scp module required: pip install scp") from e

        # QNX screenshot + SCP(수MB)는 라이브의 빡빡한 timeout(3s)으론 부족 → 하한 8s.
        raw = self._capture_one_plane(self.cluster_display, max(timeout, 8.0))
        if not raw:
            raise RuntimeError("Empty cluster screenshot from QNX")
        # crop이 설정돼 있으면 디코딩 후 잘라서 인코딩, 아니면 기존 raw 변환 경로.
        if self.cluster_crop:
            try:
                import cv2
                import numpy as np
                img = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
                if img is not None:
                    return self._encode_bgr(self._apply_cluster_crop(img), fmt)
            except Exception:
                pass
        return self._encode_cluster_raw(raw, fmt)

    # ------------------------------------------------------------------
    # Touch input
    # ------------------------------------------------------------------

    def _touch_screen_bits(self, screen_type: str) -> Optional[int]:
        """rear_left/rear_right일 때만 LCD 패킷에 monitor 바이트 포함.
        front_center는 None 반환 → 레거시 agent와의 호환성 유지.

        cluster/hud 등 터치 미지원 화면은 ValueError — monitor 바이트가 없는
        LCD 패킷은 에이전트가 front_center 터치로 해석하므로, 조용히
        front_center 로 오라우팅하지 말고 차단한다.

        ccRC(legacy ccIC_Agent) 펌웨어는 monitor 바이트를 REAR_L=2 / REAR_R=1 로
        해석한다 — CMD_LCDTOUCH(0x69) 터치와 CMD_CCRC(0x93) 하드키가 **동일** 규약.
        따라서 ccRC 디바이스는 터치도 좌우를 swap해야 화면(SCREEN_CAPTURE_MAP)·
        하드키(send_key_by_name)와 라우팅이 일치한다.

        주의(회귀 이력): 과거 "터치만 화면 매핑(LEFT=1/RIGHT=2)에 맞추고 swap 제거"한
        커밋이 있었으나, ccRC 실기에서 Rear Left 터치가 Rear Right로 입력되는 좌우
        반전이 재발 → swap을 복원한다. 터치/하드키 monitor 해석은 같은 펌웨어상
        동일하므로 ccRC면 둘 다 swap이 맞다 (일반 HKMC 6th 디바이스는 swap 없음).
        """
        if screen_type in (None, "", "front_center"):
            return None
        if screen_type not in ("rear_left", "rear_right"):
            raise ValueError(
                f"HKMC 6th touch input is not supported on '{screen_type}' screen "
                "(front_center/rear_left/rear_right only)"
            )
        if self._is_ccrc_legacy_monitor:
            # REAR_L↔REAR_R swap (legacy: byte 1=RIGHT, 2=LEFT)
            screen_type = "rear_right" if screen_type == "rear_left" else "rear_left"
        return SCREEN_TOUCH_MAP.get(screen_type)


    def tap(self, x: int, y: int, screen_type: str = "front_center") -> None:
        """Tap at (x, y) using lcdTouch."""
        x, y = int(x), int(y)
        st = self._touch_screen_bits(screen_type)
        # _capture_lock: 탭 동안 스크린샷 CMD_GETIMG 차단.
        # _input_priority: 라이브 미러링이 lock 점유 중일 때 다음 캡처를 양보시켜
        # 입력 응답 지연(2~3초)을 1초 이하로 줄임.
        with self._input_priority(), self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                self._lcd_touch(x, y, st)
                logger.info("[TAP] (%d,%d) screen=%s", x, y, screen_type)
            time.sleep(0.05)

    def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100,
                   screen_type: str = "front_center") -> None:
        """연속 터치 — lock/sleep 오버헤드를 최소화하여 빠르게 실행."""
        x, y = int(x), int(y)
        st = self._touch_screen_bits(screen_type)
        interval_sec = interval_ms / 1000.0
        with self._input_priority(), self._capture_lock:
            with self._send_lock:
                for i in range(count):
                    self._lcd_touch(x, y, st)
                    if i < count - 1 and interval_sec > 0:
                        time.sleep(interval_sec)
                logger.info("[REPEAT_TAP] (%d,%d) ×%d @%dms screen=%s", x, y, count, interval_ms, screen_type)
            time.sleep(0.05)

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "front_center") -> None:
        """Long press at (x, y) — CMD_LCDTOUCH(0x69) sub_cmd 분리 방식.

        과거 시도 (모두 실패 확인):
          - CMD_LCDTOUCH × 2 with sub_cmd=0: 0x69 + sub=0 은 press+release 묶음
            'tap'이라 두 번 보내면 탭 2회로 인식.
          - CMD_LCDTOUCHEXT(0xB0) PRESS/RELEASE: 펌웨어가 패킷 자체를 무시
            (cad6c2c에서 폐기).
          - CMD_LCDTOUCH_DRAG(0xD6) + DraggingTime 4바이트: 페이로드 길이 불일치로
            펌웨어가 패킷 거부 (스와이프까지 함께 깨짐).

        이번 시도: CMD_LCDTOUCH(0x69)의 **sub_cmd**를 변경. sub_cmd=0이 tap(SHORT_KEY와
        유사) 이라면 sub_cmd=PRESS_KEY(0x42)는 press-only, RELEASE_KEY(0x41)는
        release-only일 가능성이 높다. 페이로드 길이는 기존 tap과 동일하므로 패킷
        파싱 단계에서 거부될 가능성이 가장 낮은 시도이다.
        """
        x, y = int(x), int(y)
        st = self._touch_screen_bits(screen_type)
        data = [(x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF]
        if st is not None:
            data.append((st >> 8) & 0xFF)
            data.append(st & 0xFF)
        with self._input_priority(), self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                # 1) PRESS only
                self._make_send_packet(CMD_LCDTOUCH, PRESS_KEY, 0, list(data))
                logger.info("[LONG_PRESS] PRESS (%d,%d) screen=%s", x, y, screen_type)
                # 2) Hold
                time.sleep(duration_ms / 1000.0)
                # 3) RELEASE only
                self._make_send_packet(CMD_LCDTOUCH, RELEASE_KEY, 0, list(data))
                logger.info("[LONG_PRESS] RELEASE (%d,%d) %dms", x, y, duration_ms)
            time.sleep(0.05)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "front_center", duration_ms: int = 0,
              hold_ms: int = 0) -> None:
        """Swipe (drag) from (x1, y1) to (x2, y2) using lcdDrag.

        duration_ms 인자는 시그니처 호환을 위해 유지하나 현재 펌웨어가
        DraggingTime 페이로드를 거부하므로 무시한다 (빠른 fling으로 동작).

        hold_ms>0이면 드래그앤드롭(앱카드 이동) 베스트-에포트: long_press와 동일한
        PRESS_KEY로 시작점을 눌러 유지한 뒤, 중간 좌표를 PRESS_KEY sub_cmd로 보내
        "누른 채 이동"을 시도하고 RELEASE_KEY로 뗀다. 펌웨어가 press-only 좌표 갱신을
        이동으로 해석하는지는 미검증(실기 확인 필요) — 안 되면 _lcd_drag fling으로 폴백.
        """
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        st = self._touch_screen_bits(screen_type)
        if hold_ms and hold_ms > 0:
            def _touch_data(x: int, y: int) -> list[int]:
                d = [(x >> 8) & 0xFF, x & 0xFF, (y >> 8) & 0xFF, y & 0xFF]
                if st is not None:
                    d.append((st >> 8) & 0xFF)
                    d.append(st & 0xFF)
                return d
            steps = max(3, min(12, max(1, int(max(duration_ms, 200)) // 30)))
            dx = (x2 - x1) / steps
            dy = (y2 - y1) / steps
            with self._input_priority(), self._capture_lock:
                time.sleep(0.3)
                with self._send_lock:
                    # 1) PRESS (집어 올리기)
                    self._make_send_packet(CMD_LCDTOUCH, PRESS_KEY, 0, _touch_data(x1, y1))
                    logger.info("[DRAG_DROP] PRESS (%d,%d) screen=%s", x1, y1, screen_type)
                    time.sleep(hold_ms / 1000.0)
                    # 2) MOVE — press-only 좌표 갱신으로 누른 채 이동 시도
                    for i in range(1, steps):
                        ix = int(round(x1 + dx * i))
                        iy = int(round(y1 + dy * i))
                        self._make_send_packet(CMD_LCDTOUCH, PRESS_KEY, 0, _touch_data(ix, iy))
                        time.sleep(0.03)
                    # 3) RELEASE (놓기)
                    self._make_send_packet(CMD_LCDTOUCH, RELEASE_KEY, 0, _touch_data(x2, y2))
                    logger.info("[DRAG_DROP] RELEASE (%d,%d) hold=%dms", x2, y2, hold_ms)
                time.sleep(0.05)
            return
        with self._input_priority(), self._capture_lock:
            time.sleep(0.3)
            with self._send_lock:
                self._lcd_drag(x1, y1, x2, y2, st)
                logger.info("[SWIPE] (%d,%d)->(%d,%d) screen=%s",
                            x1, y1, x2, y2, screen_type)
            time.sleep(0.05)

    def _lcd_touch_ext_6th(self, events: list[list[int]]) -> None:
        """Send extended touch event for 6th gen (with screen type per finger).

        Each event is [x, y, action, screenType].
        """
        data = []
        num_fingers = len(events)
        data.append(num_fingers)
        for idx, ev in enumerate(events):
            x, y, action, st = ev
            data.append(idx)  # finger index
            data.append((x >> 8) & 0xFF)
            data.append(x & 0xFF)
            data.append((y >> 8) & 0xFF)
            data.append(y & 0xFF)
            data.append(action)
            data.append((st >> 8) & 0xFF)
            data.append(st & 0xFF)

        self._make_send_packet(CMD_LCDTOUCHEXT, 0, 0, data)

    def _lcd_touch(self, x: int, y: int, screen_type: Optional[int] = None) -> None:
        """Simple LCD touch (legacy)."""
        data = []
        data.append((x >> 8) & 0xFF)
        data.append(x & 0xFF)
        data.append((y >> 8) & 0xFF)
        data.append(y & 0xFF)
        if screen_type is not None:
            data.append((screen_type >> 8) & 0xFF)
            data.append(screen_type & 0xFF)
        self._make_send_packet(CMD_LCDTOUCH, 0, 0, data)

    def _lcd_drag(self, sx: int, sy: int, ex: int, ey: int,
                  screen_type: Optional[int] = None) -> None:
        """LCD drag (swipe). Payload: StartX(2) StartY(2) EndX(2) EndY(2) [Monitor(2)].

        펌웨어가 페이로드 길이가 다른 drag 패킷(DraggingTime 추가본 등)을 거부하므로
        원본 포맷 유지. duration 제어는 다른 경로로 구현해야 한다.
        """
        data = []
        for v in (sx, sy, ex, ey):
            data.append((v >> 8) & 0xFF)
            data.append(v & 0xFF)
        if screen_type is not None:
            data.append((screen_type >> 8) & 0xFF)
            data.append(screen_type & 0xFF)
        self._make_send_packet(CMD_LCDTOUCH_DRAG, 0, 0, data)

    # ------------------------------------------------------------------
    # Hardware keys
    # ------------------------------------------------------------------

    def _send_ccrc_key(self, source: int, key_type: int, status: int, monitor: int) -> None:
        """CCRC 전용 패킷 송신. data = [source, key_type, status, monitor], cmd=0x93 sub=0x00 resp=0x00."""
        data = [source & 0xFF, key_type & 0xFF, status & 0xFF, monitor & 0xFF]
        logger.debug("[HKMC CCRC] src=0x%02X key=0x%02X status=0x%02X mon=0x%02X",
                     source, key_type, status, monitor)
        with self._send_lock:
            self._make_send_packet(CMD_CCRC, 0, 0, data)

    def send_key(self, cmd: int, sub_cmd: int, key_data: int,
                 monitor: int = 0x00, direction: Optional[int] = None) -> None:
        """Send a hardware key event (6th gen keyExt6th).

        Args:
            cmd: Key category command (CMD_MKBD, CMD_CCP, CMD_RRC, CMD_SWC, CMD_MIRROR)
            sub_cmd: Sub command (SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, DIAL_ACTION)
            key_data: Key code
            monitor: Target monitor (0x00=NONE, 0x01=LEFT, 0x02=RIGHT)
            direction: Optional direction byte for dial/knob events
        """
        resp = 0xFE
        data = []
        data.append((key_data >> 24) & 0xFF)
        data.append((key_data >> 16) & 0xFF)
        data.append((key_data >> 8) & 0xFF)
        data.append(key_data & 0xFF)
        if direction is not None:
            data.append(direction)
        data.append(monitor)

        logger.debug("[HKMC KEY] cmd=0x%02X sub=0x%02X key=0x%02X monitor=0x%02X dir=%s",
                     cmd, sub_cmd, key_data, monitor, direction)

        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, resp, data)

    def resolve_key(self, key_name: str) -> Optional[dict]:
        """spec default + device override 병합된 키 정보 반환.

        cmd/key/dial/direction 개별 필드만 덮어쓴다 (visible은 UI 전용).
        """
        base = HKMC_KEYS.get(key_name, {})
        ov = self._key_overrides.get(key_name, {})
        merged = dict(base)
        for k in ("cmd", "key", "dial", "direction", "ccrc", "source"):
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
                         monitor: int = 0x00, direction: Optional[int] = None,
                         screen_type: Optional[str] = None,
                         key_source: Optional[int] = None,
                         hold_ms: int = 0) -> None:
        """Send a hardware key by its name (e.g. 'CCP_ENTER', 'MKBD_MAP').

        Args:
            key_name: Key name from HKMC_KEYS
            sub_cmd: SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, DIAL_ACTION
            monitor: Target monitor (LEFT=0x01, RIGHT=0x02)
            direction: Direction for dial events
            hold_ms: >0이면 '키 누름 유지(press-and-hold)' — PRESS만 보내고 hold_ms 동안
                누른 상태를 유지한 뒤 RELEASE를 보낸다. 영상/음악 재생 중 >>/Enter 등을
                꾹 눌러 배속·연속 동작을 유발하는 용도. 0이면 기존 SHORT/LONG 단발 동작.
            screen_type: CCRC 전용 — monitor 미지정 시 'rear_left'/'rear_right'에서
                CCRC monitor 값 자동 유도.
            key_source: rear-source 강제값 (CCRC_SRC_RRC=0x02 / CCRC_SRC_BRRC=0x07 /
                CCRC_SRC_REAR_LEFT_MONITOR=0x0B / CCRC_SRC_REAR_RIGHT_MONITOR=0x0C).
                지정 시:
                - CCRC 키(ccrc=true): hardcoded BRRC 대신 이 값을 source로 사용
                - RRC 키(cmd=CMD_RRC): 같은 keycode를 CMD_CCRC(0x93)로 라우팅 + 이 source 적용
                  (RRC 명령(0x90)은 source 필드가 없는 프로토콜이므로 source가 의미 있는
                  CCRC 명령으로 자동 전환)
                None이면 기본 동작 — RRC는 0x90 그대로, CCRC는 정의된 source(보통 BRRC).
        """
        key_info = self.resolve_key(key_name)
        if not key_info:
            raise ValueError(f"Unknown HKMC key: {key_name}")

        cmd = key_info["cmd"]
        key_data = key_info["key"]
        is_ccrc = bool(key_info.get("ccrc"))

        # key_source가 명시되었고 RRC 명령이면 CCRC 명령(0x93) 경로로 자동 전환.
        # RRC keycode들과 CCRC keycode들은 핵심 키(UP/DOWN/LEFT/RIGHT/ENTER/BACK/HOME/
        # VOLUME_*/POWER_*)가 모두 동일한 hex 값이라 같은 keycode를 그대로 재사용 가능.
        # 단, dial 키(JOGDIAL/VOLUME_*_DIAL)는 CCRC 프로토콜에 dial action 정의가 없어
        # 라우팅하면 무반응이 되므로 제외하고 CMD_RRC(0x90) 그대로 유지한다.
        if key_source is not None and cmd == CMD_RRC and not key_info.get("dial"):
            cmd = CMD_CCRC
            is_ccrc = True

        # 키 정의에 monitor 필드가 있으면 (L_RRC/R_RRC 계열) 우선 적용.
        # 호출자가 monitor를 명시하지 않은 경우에만 — 명시된 값이 항상 우선.
        if monitor not in (CCRC_MONITOR_LEFT, CCRC_MONITOR_RIGHT) and "monitor" in key_info:
            monitor = key_info["monitor"]

        # monitor 미지정(0x00 NONE) + rear_left/rear_right 화면이면 자동으로 LEFT/RIGHT 유도.
        # CCRC·일반 하드키 공통 — 일반 키도 monitor 필드가 rear 모니터 라우팅에 사용됨.
        # rear_left→LEFT(0x01), rear_right→RIGHT(0x02). L_RRC/R_RRC(monitor 필드 직접
        # 사용, swap 없음)와 동일 규약 — ccRC도 동일. (과거 ccRC swap을 걸었으나 실기에서
        # RRC/CCRC 그룹 키가 반대 화면으로 입력되어 제거. L/R 그룹은 정상이므로 그에 일치.)
        if monitor not in (CCRC_MONITOR_LEFT, CCRC_MONITOR_RIGHT):
            if screen_type == "rear_left":
                monitor = CCRC_MONITOR_LEFT
            elif screen_type == "rear_right":
                monitor = CCRC_MONITOR_RIGHT

        # _capture_lock: 키 시퀀스 중 스크린샷 CMD_GETIMG 차단
        # _input_priority: 미러링이 lock 점유 중일 때 다음 캡처를 양보시켜 키 입력 응답 지연 완화
        with self._input_priority(), self._capture_lock:
            # Agent가 이전 이미지 응답 전송을 마칠 시간 확보
            time.sleep(0.3)
            if is_ccrc:
                # CCRC: cmd=0x93, data=[source, key, status, monitor]
                # PRESS/SHORT/LONG/RELEASE 값이 일반 KEY와 다름 (0x01~0x03, 0x00).
                # 호출자 override(key_source) > 키 정의 default > BRRC fallback
                if key_source is not None:
                    source = key_source
                else:
                    source = key_info.get("source", CCRC_SRC_BRRC)
                # monitor 0x00(NONE)이면 Right 모니터 기본값으로 보정 (레거시 CCRC_HK 기본).
                mon = monitor if monitor in (CCRC_MONITOR_LEFT, CCRC_MONITOR_RIGHT) else CCRC_MONITOR_RIGHT
                if hold_ms and hold_ms > 0:
                    # 누름 유지(연속/배속): key-down(CCRC_PRESS)을 hold_ms 동안 일정 간격으로
                    # 반복 송신(auto-repeat)하다가 마지막에 RELEASE. 단일 PRESS 유지만으론
                    # IVI 가 연속 동작을 하지 않으므로 반복 전송으로 구동.
                    repeat_interval = 0.12
                    end = time.monotonic() + hold_ms / 1000.0
                    self._send_ccrc_key(source, key_data, CCRC_PRESS, mon)
                    while time.monotonic() < end:
                        time.sleep(repeat_interval)
                        self._send_ccrc_key(source, key_data, CCRC_PRESS, mon)
                    self._send_ccrc_key(source, key_data, CCRC_RELEASE, mon)
                elif sub_cmd == LONG_KEY:
                    self._send_ccrc_key(source, key_data, CCRC_PRESS, mon)
                    time.sleep(0.1)
                    self._send_ccrc_key(source, key_data, CCRC_LONG, mon)
                    time.sleep(0.1)
                    self._send_ccrc_key(source, key_data, CCRC_RELEASE, mon)
                else:
                    # SHORT (기본) — PRESS → SHORT → RELEASE
                    self._send_ccrc_key(source, key_data, CCRC_PRESS, mon)
                    time.sleep(0.1)
                    self._send_ccrc_key(source, key_data, CCRC_SHORT, mon)
                    time.sleep(0.1)
                    self._send_ccrc_key(source, key_data, CCRC_RELEASE, mon)
                time.sleep(0.05)
                return
            if key_info.get("dial"):
                dir_val = direction if direction is not None else key_info.get("direction")
                self.send_key(cmd, DIAL_ACTION, key_data, monitor, dir_val)
            elif hold_ms and hold_ms > 0:
                # 누름 유지(연속/배속): 키-다운(PRESS, 0x42)으로 키를 누른 뒤 hold_ms 동안
                # 그대로 유지하다가 RELEASE 한다. 중간에 별도 이벤트를 끼우지 않아 '키가
                # 계속 눌려 있음'으로 인식되게 한다. (PRESS → 유지 → RELEASE)
                # try/finally 로 송신 중 예외가 나도 RELEASE 를 보장 — 키가 눌린 채 멈추지 않게.
                self.send_key(cmd, PRESS_KEY, key_data, monitor, direction)
                try:
                    time.sleep(hold_ms / 1000.0)
                finally:
                    self.send_key(cmd, RELEASE_KEY, key_data, monitor, direction)
                logger.info("[KEY HOLD] %s hold=%dms (press-hold-release)", key_name, hold_ms)
            elif sub_cmd == SHORT_KEY:
                # 일반 키: PRESS → SHORT → RELEASE 3단계 시퀀스
                self.send_key(cmd, PRESS_KEY, key_data, monitor, direction)
                time.sleep(0.1)
                self.send_key(cmd, SHORT_KEY, key_data, monitor, direction)
                time.sleep(0.1)
                self.send_key(cmd, RELEASE_KEY, key_data, monitor, direction)
            elif sub_cmd == LONG_KEY:
                # 롱프레스: PRESS → LONG → RELEASE
                # SHORT와 동일하게 0.1초 간격으로 보낸다. PRESS~LONG 사이를 길게(1초)
                # 두면 일부 IVI가 리어 모니터 포커스를 잃고 front_center 로 전환되어
                # LONG 이 엉뚱한 화면에서 처리되는 현상이 있음 — sub_cmd=LONG_KEY(0x44)
                # 자체로 IVI가 롱키 의미를 인식하므로 실제 홀드 시간은 불필요.
                self.send_key(cmd, PRESS_KEY, key_data, monitor, direction)
                time.sleep(0.1)
                self.send_key(cmd, LONG_KEY, key_data, monitor, direction)
                time.sleep(0.1)
                self.send_key(cmd, RELEASE_KEY, key_data, monitor, direction)
            else:
                self.send_key(cmd, sub_cmd, key_data, monitor, direction)
            # Agent 처리 시간 확보 (CMD_GETIMG 즉시 진입 방지)
            time.sleep(0.05)

    # ------------------------------------------------------------------
    # Ethernet signal / Shell command execution
    # ------------------------------------------------------------------

    def ethernet_signal(self, n_signal_payload: int, n_signal_id: int) -> int:
        """Send an Ethernet signal with payload and signal ID.

        Args:
            n_signal_payload: Signal payload value (1 byte if ≤0xFF, 2 bytes otherwise)
            n_signal_id:      Signal ID (4 bytes, big-endian)

        Returns:
            0 on success.
        """
        cmd     = CMD_ETHERNETSIGNAL
        sub_cmd = 0
        resp    = 0

        data: list[int] = []
        data.append(0x00)
        data.append(0x00)

        len_payload = 1 if n_signal_payload <= 0xFF else 2
        data.append((len_payload >> 8) & 0xFF)
        data.append(len_payload & 0xFF)

        if len_payload == 1:
            data.append(n_signal_payload & 0xFF)
        else:
            data.append((n_signal_payload >> 8) & 0xFF)
            data.append(n_signal_payload & 0xFF)

        data.append((n_signal_id >> 24) & 0xFF)
        data.append((n_signal_id >> 16) & 0xFF)
        data.append((n_signal_id >> 8) & 0xFF)
        data.append(n_signal_id & 0xFF)

        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, resp, data)
        return 0  # AT_SUCCESS

    def execute_shell(self, cmd: int, sub_cmd: int, resp: int, str_data: str) -> int:
        """Send a shell command string to the agent.

        Args:
            cmd:      Command byte (e.g. CMD_EXECUTESHELLCMD)
            sub_cmd:  Sub-command byte (e.g. MOVE_KEY=0x45)
            resp:     Response code byte (e.g. RESPONSE_FAIL)
            str_data: Shell command string to send

        Returns:
            0 on success.
        """
        data: list[int] = []
        length = len(str_data)
        data.append((length >> 24) & 0xFF)
        data.append((length >> 16) & 0xFF)
        data.append((length >> 8) & 0xFF)
        data.append(length & 0xFF)
        for ch in str_data:
            data.append(ord(ch))
        with self._send_lock:
            self._make_send_packet(cmd, sub_cmd, resp, data)
        return 0  # AT_SUCCESS

    def send_shell_cmd(self, str_data: str) -> int:
        """Convenience wrapper: send shell command using fixed protocol constants.

        Equivalent to execute_shell(CMD_EXECUTESHELLCMD, 0x45, RESPONSE_FAIL, str_data).
        """
        return self.execute_shell(CMD_EXECUTESHELLCMD, MOVE_KEY, RESPONSE_FAIL, str_data)

    async def async_ethernet_signal(self, n_signal_payload: int, n_signal_id: int) -> int:
        """Async wrapper for ethernet_signal()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.ethernet_signal, n_signal_payload, n_signal_id)

    async def async_execute_shell(self, cmd: int, sub_cmd: int, resp: int,
                                  str_data: str) -> int:
        """Async wrapper for execute_shell()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.execute_shell, cmd, sub_cmd, resp, str_data)

    async def async_send_shell_cmd(self, str_data: str) -> int:
        """Async wrapper for send_shell_cmd()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send_shell_cmd, str_data)

    # ------------------------------------------------------------------
    # Async wrappers (for use from FastAPI/asyncio context)
    # ------------------------------------------------------------------

    async def async_connect(self, timeout: float = 10.0) -> bool:
        """Async wrapper for connect()."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.connect, timeout)

    async def async_disconnect(self) -> None:
        """Async wrapper for disconnect()."""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    async def async_screencap(self, output_path: str, screen_type: str = "front_center",
                              timeout: float = 10.0, composite: bool = True) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap, output_path, screen_type,
                                          timeout, composite)

    async def async_screencap_bytes(self, screen_type: str = "front_center",
                                    fmt: str = "png", timeout: float = 10.0,
                                    composite: bool = True) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.screencap_bytes, screen_type, fmt,
                                          timeout, composite)

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
                          screen_type: str = "front_center",
                          duration_ms: int = 300, hold_ms: int = 0) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.swipe, x1, y1, x2, y2, screen_type, duration_ms, hold_ms)

    async def async_send_key(self, cmd: int, sub_cmd: int, key_data: int,
                             monitor: int = 0x00, direction: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.send_key, cmd, sub_cmd, key_data, monitor, direction)

    async def async_send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                                     monitor: int = 0x00, direction: Optional[int] = None,
                                     screen_type: Optional[str] = None,
                                     key_source: Optional[int] = None,
                                     hold_ms: int = 0) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.send_key_by_name, key_name, sub_cmd, monitor, direction, screen_type, key_source, hold_ms
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
            "screens": {
                "front_center": {"width": self.screen_width_front, "height": self.screen_height_front},
                "rear_left": {"width": self.screen_width_rear_l, "height": self.screen_height_rear_l},
                "rear_right": {"width": self.screen_width_rear_r, "height": self.screen_height_rear_r},
                "cluster": {"width": self.screen_width_cluster, "height": self.screen_height_cluster},
            },
        }
