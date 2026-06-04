# -*- coding: utf-8 -*-
"""CCIC 우현벤치 UDP control plugin — 전원(IGN/ACC/BATTERY) + CAN FD 송신 통합.

UDP 패킷 형식: [0x55, 0xAA, sender(100), seq(0), cmd1, cmd2, len_hi, len_lo, ...data]
Reference: WoohyunBench_LIBRARY.py, CCIC_DEFINITION_LIBRARY.py (legacy)

벤치 기본값: BENCH_IP = 192.168.1.101, BENCH_PORT = 25000

CAN FD 기능은 공용 UDP_CANFD 라이브러리(backend/app/lib/UDP_CANFD.py)를 composition
방식으로 내부 보관하며, 단일 UDP 소켓을 공유해 동작한다(원본 라이브러리는 수정하지 않음).
신호 정의 파일(signal_file, 선택)이 주어지면 Connect 시 자동 로드된다.
"""

from __future__ import annotations

import socket
import logging
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

START_1 = 0x55
START_2 = 0xAA
SENDER_ID = 100
DEFAULT_UDP_PORT = 25000

# CAN FD 송신 패킷 헤더
# cmd1 채널 번호: 0x02=CAN B, 0x03=CAN D(AVN/EXT), 0x04=CAN C(Cluster/FD)
CANFD_SEND_PACKET_HEADER_AVN     = [START_1, START_2, SENDER_ID, 0x00, 0x03, 0x30]  # CAN D — AVN(EXT)
CANFD_SEND_PACKET_HEADER_CLUSTER = [START_1, START_2, SENDER_ID, 0x00, 0x04, 0x30]  # CAN C — Cluster(FD)
CANFD_SEND_PACKET_HEADER         = CANFD_SEND_PACKET_HEADER_CLUSTER  # 기존 기본값 유지 (STA/FD)
# CAN FD INIT(OPEN write) 패킷 헤더. cmd=0x04 0x10 — 원본 UDP_CANFD_INIT()와 동일.
CANFD_INIT_PACKET_HEADER = [START_1, START_2, SENDER_ID, 0x00, 0x04, 0x10]


def _payload_size_to_dlc(payload_size: int) -> int:
    """CAN FD payload 크기 → DLC. 매핑에 없으면 8."""
    _map = {
        0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7,
        8: 8, 12: 9, 16: 10, 20: 11, 24: 12, 32: 13, 48: 14, 64: 15,
    }
    return _map.get(payload_size, 8)


class WoohyunBench:
    """CCIC 우현벤치 UDP 제어 플러그인 (전원 + CAN FD)."""

    def __init__(self, host: str = "", udp_port: int = DEFAULT_UDP_PORT,
                 signal_file: str = "", signal_file_AVN: str = ""):
        self._host = host
        self._udp_port = int(udp_port) if udp_port else DEFAULT_UDP_PORT
        self._signal_file = (signal_file or "").strip()
        self._signal_file_AVN = (signal_file_AVN or "").strip()
        self._sock = None
        # CAN FD 위임 객체. Connect 시 생성 + 이 플러그인의 _sock을 공유 바인딩.
        self._canfd = None
        # AVN용 CAN FD 인스턴스 (UDP_CANFD_AVN, signal list 방식, can_type 선택 가능)
        self._canfd_AVN = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """UDP 소켓 연결 + CAN 버스 OPEN(INIT) + (선택) 신호 정의 로드.

        원본 흐름과 동일하게 항상 CAN FD INIT 패킷(0x04 0x10)을 송신해 bench의
        CAN 버스를 연다. signal_file이 지정된 경우 추가로 신호 정의를 로드한다.
        """
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        if not self._host:
            raise RuntimeError("Host not set")
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # 레거시 코드와 동일: connect()로 기본 목적지 설정 (타임아웃 미설정)
        self._sock.connect((self._host, self._udp_port))
        logger.info("WoohyunBench connected to %s:%d", self._host, self._udp_port)

        # CAN 버스 OPEN — signal_file 유무와 무관하게 항상 송신
        # (원본 UDP_CANFD_INIT()와 정확히 같은 패킷 구조: data 6바이트, length=6)
        try:
            self._send_canfd_init()
        except Exception as e:
            logger.warning("WoohyunBench CAN FD INIT 실패 (CAN 송신 불가능할 수 있음): %s", e)

        # CAN FD 신호 정의용 lib (signal-name 기반 송신 기능에서만 필요)
        try:
            from ..lib.UDP_CANFD import UDP_CANFD
        except Exception as e:
            logger.warning("WoohyunBench: UDP_CANFD 라이브러리 로드 실패 — 신호 이름 기반 기능 비활성 (%s)", e)
            self._canfd = None
        else:
            cf = UDP_CANFD()
            cf.sock = self._sock          # 동일 소켓 공유 (별도 자원 없음)
            cf.udp_ip = self._host
            cf.udp_port = self._udp_port
            self._canfd = cf
            if self._signal_file:
                try:
                    self._load_signals_into(cf, self._signal_file)
                    logger.info("WoohyunBench CAN FD signals loaded (count=%d from %s)",
                                len(cf.signal_defs), self._signal_file)
                except Exception as e:
                    logger.warning("WoohyunBench 신호 정의 로드 실패 (비치명): %s", e)

        return f"Connected to {self._host}:{self._udp_port}"

    def _send_canfd_init(self, baudrate: int = 0x1F4, databit_time: int = 0x7D0) -> None:
        """원본 UDP_CANFD_INIT()와 동일한 CAN 버스 OPEN 패킷 송신.

        패킷 구조 (총 14B, length 필드=6):
          55 AA 64 00 04 10 [00 06] 00 00 baud_h baud_l dbt_h dbt_l
        """
        if not self._sock:
            raise RuntimeError("Not connected")
        data = [
            0x00, 0x00,                                  # can_type 1, 2
            (baudrate >> 8) & 0xFF, baudrate & 0xFF,
            (databit_time >> 8) & 0xFF, databit_time & 0xFF,
        ]
        length_bytes = [(len(data) >> 8) & 0xFF, len(data) & 0xFF]
        packet = bytearray(CANFD_INIT_PACKET_HEADER + length_bytes + data)
        self._sock.sendto(packet, (self._host, self._udp_port))
        logger.info("WoohyunBench CANFD INIT TX (baud=0x%X, dbt=0x%X): [%s]",
                    baudrate, databit_time,
                    ", ".join(hex(b) for b in packet))

    def Disconnect(self) -> str:
        """UDP 소켓 해제. CAN FD 서브시스템도 함께 정리."""
        # UDP_CANFD.UDP_DEINIT()가 내부적으로 sock.close() 후 None 처리. 소켓이 공유이므로
        # 이 경로로 닫으면 self._sock도 같은 객체가 닫힌 상태가 된다.
        if self._canfd is not None:
            try:
                self._canfd.UDP_DEINIT()
            except Exception:
                pass
            self._canfd = None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        return "Disconnected"

    def IsConnected(self) -> bool:
        """연결 상태 확인."""
        return self._sock is not None

    # ------------------------------------------------------------------
    # CAN FD — 공용 UDP_CANFD 라이브러리 위임
    # ------------------------------------------------------------------

    @staticmethod
    def _load_signals_into(canfd_impl, file_path: str) -> None:
        """파일 확장자에 따라 UDP_CANFD에 신호 정의를 로드."""
        p = Path(file_path)
        if not p.is_file():
            raise FileNotFoundError(f"signal file not found: {file_path}")
        if file_path.lower().endswith('.can'):
            canfd_impl.load_signal_definitions_from_xml(file_path)
        else:
            canfd_impl.load_signal_definitions_from_excel(file_path)

    def LoadSignals(self, file_path: str) -> str:
        """런타임에 CAN FD 신호 정의를 Excel/XML에서 다시 로드."""
        if self._canfd is None:
            return "FAIL: CAN FD 비활성 (UDP_CANFD 라이브러리 미설치 또는 연결 전)"
        try:
            self._load_signals_into(self._canfd, file_path)
            try:
                self._canfd.UDP_CANFD_INIT_MESSAGE()
            except Exception as e:
                logger.warning("WoohyunBench CAN FD INIT 재전송 실패: %s", e)
            return f"OK: {len(self._canfd.signal_defs)} signals loaded from {file_path}"
        except Exception as e:
            logger.error("WoohyunBench LoadSignals failed: %s", e)
            return f"FAIL: LoadSignals: {e}"

    def SendSignal(self, signal_name: str, physical_value) -> str:
        """이름으로 지정한 CAN 신호를 physical_value로 전송 (200ms × 5회 반복)."""
        if self._canfd is None:
            return "FAIL: CAN FD 비활성"
        if not self._canfd.signal_defs:
            return "FAIL: 신호 정의 미로드 — signal_file 설정 또는 LoadSignals 호출 필요"
        ok = self._canfd.SEND_CANEthernetData(signal_name, physical_value)
        return f"{'OK' if ok else 'FAIL'}: SendSignal {signal_name}={physical_value}"

    def DoorTest(self) -> str:
        """운전석 도어 스위치 신호(Warn_DrvDrSwSta)를 ON/OFF 반복 송신."""
        if self._canfd is None:
            return "FAIL: CAN FD 비활성"
        ok = self._canfd.door_test()
        return f"{'OK' if ok else 'FAIL'}: DoorTest"

    def TestAllSignals(self) -> str:
        """로드된 모든 신호를 중간값으로 순차 송신 (부하/연결 확인용)."""
        if self._canfd is None:
            return "FAIL: CAN FD 비활성"
        ok = self._canfd.test_all_canfd_signals()
        return f"{'OK' if ok else 'FAIL'}: TestAllSignals"

    def CheckSignals(self) -> str:
        """로드된 CAN 신호 정의를 로그로 덤프."""
        if self._canfd is None:
            return "FAIL: CAN FD 비활성"
        self._canfd.CHECK_CAN_SIGNAL()
        return f"OK: {len(self._canfd.signal_defs)} signals"

    def SendCanFd(self, can_id, payload_hex: str = "", fd_mode: bool = False,
                  can_type: str = "STA") -> str:
        """Raw CAN FD 프레임 직접 송신 (신호 정의 불필요).

        Args:
            can_id: int 또는 문자열("0x448"/"1096" 모두 허용).
            payload_hex: 다음 형식 모두 허용:
              - "0 0 0 0 0 0 1 0"  (공백 구분)
              - "0,0,0,0,0,0,1,0"  (콤마 구분)
              - "[0, 0, 0, 0, 0, 0, 1, 0]" (대괄호 리스트)
              - "0000000100"       (붙여쓴 hex 문자열)
            fd_mode: 레거시 호환. True면 can_type="FD"와 동일.
            can_type: "STA"(기본) / "EXT"(확장 프레임, AVN) / "FD"(CAN FD, Cluster).
                      fd_mode=True이면 자동으로 "FD"로 처리됨.
        """
        if not self._sock:
            return "FAIL: 연결 안 됨 — Connect() 먼저 호출"
        try:
            cid = self._parse_can_id(can_id)
            payload = self._parse_payload(payload_hex)
            if isinstance(fd_mode, str):
                fd_mode = fd_mode.strip().lower() in ("1", "true", "yes", "on")
            ct = str(can_type).upper() if can_type else "STA"
            if fd_mode and ct == "STA":
                ct = "FD"
            self._send_canfd_raw(cid, payload, can_type=ct)
            return (f"OK: SendCanFd ID=0x{cid:X} ({len(payload)}B, type={ct}, x5@200ms)")
        except Exception as e:
            logger.error("WoohyunBench SendCanFd failed: %s", e)
            return f"FAIL: SendCanFd: {e}"

    def SendAvnCan(self, msg_id, type, payload_hex: str = "") -> str:
        """AVN 채널 Raw CAN 프레임 송신 (send_can.py TARGET="AVN" 동일 동작).

        목적지를 항상 AVN 채널(0x03 헤더)로 고정하고, 프레임 타입은 type 인자로
        독립 지정한다(표준 0x33E와 확장 1004001을 같은 AVN 채널로 송신 가능).
        msg_id는 '0x' 접두 유무와 무관하게 항상 hex로 파싱.

        Args:
            msg_id: CAN ID. 정수 또는 문자열("0x414", "414", "1004001" 모두 hex로 해석).
            type: 프레임 타입 — "STA"(표준) / "EXT"(확장 프레임) / "FD"(CAN FD).
            payload_hex: 페이로드. 다음 형식 모두 허용:
              - 쉼표 구분 hex: "00,00,00,00,00,00,00,00"
              - 공백 구분:     "00 00 00 00 00 00 00 00"
              - 대괄호 리스트: "[0, 0, 0, 0, 0, 0, 0, 0]"
        """
        if not self._sock:
            return "FAIL: 연결 안 됨 — Connect() 먼저 호출"
        try:
            cid = self._parse_can_id_hex(msg_id)
            payload = self._parse_payload(payload_hex)
            self._send_canfd_raw(cid, payload, can_type=type, target_ecu="AVN")
            return f"OK: SendAvnCan ID=0x{cid:X} ({len(payload)}B, {type}, x5@200ms)"
        except Exception as e:
            logger.error("WoohyunBench SendAvnCan failed: %s", e)
            return f"FAIL: SendAvnCan: {e}"

    def SendClusterCan(self, msg_id, payload_hex: str = "") -> str:
        """Cluster 채널 Raw CAN FD 프레임 송신 (CANTYPE.FD, send_can.py 동일 동작).

        send_can.py의 TARGET="CLUSTER" 과 완전히 동일한 패킷 포맷.
        frame_byte = 0x80 | DLC  (FD Frame Flag)
        msg_id는 '0x' 접두 유무와 무관하게 항상 hex로 파싱.

        Args:
            msg_id: CAN ID. 정수 또는 문자열("0x65", "65" 모두 0x65로 해석).
            payload_hex: 페이로드. 다음 형식 모두 허용:
              - 쉼표 구분 hex: "00,00,00,00,00,00,00,00"
              - 공백 구분:     "00 00 00 00 00 00 00 00"
              - 대괄호 리스트: "[0, 0, 0, 0, 0, 0, 0, 0]"
        """
        if not self._sock:
            return "FAIL: 연결 안 됨 — Connect() 먼저 호출"
        try:
            cid = self._parse_can_id_hex(msg_id)
            payload = self._parse_payload(payload_hex)
            self._send_canfd_raw(cid, payload, can_type="FD")
            return f"OK: SendClusterCan ID=0x{cid:X} ({len(payload)}B, FD, x5@200ms)"
        except Exception as e:
            logger.error("WoohyunBench SendClusterCan failed: %s", e)
            return f"FAIL: SendClusterCan: {e}"

    def _send_canfd_raw(self, can_id: int, payload: bytearray, fd_mode: bool = False,
                        repeat: int = 5, interval_sec: float = 0.2,
                        can_type: str = "STA", target_ecu: str = "CLUSTER") -> None:
        """벤치 UDP_CANFD_SEND과 동일한 포맷으로 raw CAN FD 프레임 송신.

        포맷: HEADER(6) + LEN(2) + CAN_ID(4) + frame_byte(1) + reserved(0x00,1) + payload
        주기 송신: 벤치/ECU가 CAN 신호를 안정적으로 잡도록 5회 × 200ms 반복.

        목적지 채널(target_ecu)과 프레임 타입(can_type)을 완전히 분리한다.
        과거에는 프레임 타입이 채널을 강제로 결정해(STA→Cluster) AVN으로 보내야 할
        표준 프레임(0x33E)이 Cluster로 잘못 라우팅되는 버그가 있었다.

        target_ecu: 목적지 채널 헤더 선택.
          "AVN"     → 0x03 헤더 (CAN D)
          "CLUSTER" → 0x04 헤더 (CAN C, 기본값)

        can_type: frame_byte(프레임 타입)만 결정 (채널과 무관).
          "FD"  → frame_byte 0x80 | DLC  (CAN FD)
          "EXT" → frame_byte 0x20 | DLC  (확장 프레임)
          "STA" → frame_byte 0x00 | len  (표준 프레임, 기본값)
        """
        if not self._sock:
            raise RuntimeError("Not connected")

        ct = can_type.upper() if isinstance(can_type, str) else "STA"
        # fd_mode=True 레거시 호환: can_type이 기본값 STA인 경우에만 FD로 승격
        if fd_mode and ct == "STA":
            ct = "FD"

        # 목적지 채널(Header) — 프레임 타입과 독립적으로 결정
        if str(target_ecu).upper() == "AVN":
            send_header = CANFD_SEND_PACKET_HEADER_AVN      # 0x03 — CAN D (AVN)
        else:
            send_header = CANFD_SEND_PACKET_HEADER_CLUSTER  # 0x04 — CAN C (Cluster)

        # 프레임 타입(frame_byte) — 채널과 독립적으로 결정
        if ct == "FD":
            dlc = _payload_size_to_dlc(len(payload))
            can_frame = 0x80 | (dlc & 0x7F)
        elif ct == "EXT":
            dlc = _payload_size_to_dlc(len(payload))
            can_frame = 0x20 | (dlc & 0x7F)
        else:  # STA
            can_frame = len(payload) & 0x7F

        can_id_bytes = [
            (can_id >> 24) & 0xFF,
            (can_id >> 16) & 0xFF,
            (can_id >> 8)  & 0xFF,
             can_id        & 0xFF,
        ]
        data = can_id_bytes + [can_frame, 0x00] + list(payload)
        length_bytes = [(len(data) >> 8) & 0xFF, len(data) & 0xFF]
        packet = bytearray(send_header + length_bytes + data)
        hex_str = ", ".join(hex(b) for b in packet)

        for i in range(max(1, repeat)):
            self._sock.sendto(packet, (self._host, self._udp_port))
            logger.info("WoohyunBench CANFD TX [%d/%d] (ID=0x%X, payload=%dB): [%s]",
                        i + 1, repeat, can_id, len(payload), hex_str)
            if i + 1 < repeat:
                time.sleep(interval_sec)

    @staticmethod
    def _parse_can_id(can_id) -> int:
        """can_id를 int로 정규화. '0x448', '1096', 1096 모두 허용."""
        if isinstance(can_id, int):
            return can_id
        if isinstance(can_id, str):
            s = can_id.strip()
            if not s:
                raise ValueError("can_id is empty")
            # 0x 접두 또는 hex 문자가 섞여 있으면 hex로 해석
            if s.lower().startswith("0x"):
                return int(s, 16)
            try:
                return int(s)
            except ValueError:
                # 마지막 폴백: hex 시도
                return int(s, 16)
        return int(can_id)

    @staticmethod
    def _parse_can_id_hex(can_id) -> int:
        """can_id를 항상 hex로 파싱. '0x' 접두 유무 무관.

        SendAvnCan / SendClusterCan 전용. send_can.py와 동일한 동작 보장.
        예) '1004001' → 0x1004001,  '0x414' → 0x414,  1044 → 1044
        """
        if isinstance(can_id, int):
            return can_id
        if isinstance(can_id, str):
            s = can_id.strip()
            if not s:
                raise ValueError("can_id is empty")
            s_clean = s[2:] if s.lower().startswith("0x") else s
            return int(s_clean, 16)
        return int(can_id)

    @staticmethod
    def _parse_payload(payload_hex: str) -> bytearray:
        """payload 문자열을 bytearray로 정규화. 여러 형식 허용.

        우선순위:
          1) 대괄호 리스트  "[a, b, c]"          → 각 토큰 1byte
          2) 구분자(공백/콤마/세미콜론) 포함     → 각 토큰 1byte
             - 토큰이 "0x" 접두면 hex, 아니면 decimal로 해석 (0~255)
          3) 그 외 (붙여쓴 문자열)               → hex 문자열로 디코드
        """
        if not payload_hex:
            return bytearray()
        s = payload_hex.strip()
        if not s:
            return bytearray()

        # 1) 대괄호 리스트
        if s.startswith("[") and s.endswith("]"):
            return WoohyunBench._tokens_to_bytes(s[1:-1])

        # 2) 구분자가 있으면 토큰별 byte로 해석
        if any(ch in s for ch in (" ", ",", ";")):
            return WoohyunBench._tokens_to_bytes(s)

        # 3) 단일 hex 문자열
        cleaned = s
        if cleaned.lower().startswith("0x"):
            cleaned = cleaned[2:]
        if len(cleaned) % 2 != 0:
            cleaned = "0" + cleaned
        return bytearray.fromhex(cleaned)

    @staticmethod
    def _tokens_to_bytes(text: str) -> bytearray:
        """공백/콤마/세미콜론으로 나뉜 토큰들을 byte 배열로 변환.

        토큰이 "0x" 접두면 hex, 아니면 decimal로 해석. 각 값은 0~255 범위.
        """
        # 모든 구분자를 공백으로 통일 후 split
        normalized = text.replace(",", " ").replace(";", " ")
        tokens = [t.strip() for t in normalized.split() if t.strip()]
        out = bytearray()
        for t in tokens:
            v = int(t, 16) if t.lower().startswith("0x") else int(t)
            if not (0 <= v <= 0xFF):
                raise ValueError(f"payload byte out of range: {t}")
            out.append(v)
        return out

    def ReinitCanFd(self, baudrate=0x1F4, databit_time=0x7D0) -> str:
        """CAN FD 버스 재초기화 (기본 500k/2M). 원본과 동일한 INIT 패킷 송신."""
        if not self._sock:
            return "FAIL: 연결 안 됨 — Connect() 먼저 호출"
        try:
            br = self._parse_int_arg(baudrate, default=0x1F4)
            dbt = self._parse_int_arg(databit_time, default=0x7D0)
            self._send_canfd_init(br, dbt)
            return f"OK: ReinitCanFd baudrate=0x{br:X} databit=0x{dbt:X}"
        except Exception as e:
            logger.error("WoohyunBench ReinitCanFd failed: %s", e)
            return f"FAIL: ReinitCanFd: {e}"

    @staticmethod
    def _parse_int_arg(val, default: int) -> int:
        """UI에서 들어온 인자(int 또는 문자열)를 int로 정규화. '0x1F4'/'500'/500 모두 허용."""
        if isinstance(val, int):
            return val
        if isinstance(val, str):
            s = val.strip()
            if not s:
                return default
            return int(s, 16) if s.lower().startswith("0x") else int(s)
        return int(val) if val is not None else default

    # ------------------------------------------------------------------
    # Internal — 레거시 UDP_SEND()와 동일한 로직
    # ------------------------------------------------------------------

    def _drain_rx(self) -> int:
        """수신 버퍼에 남아있는 이전 응답들을 모두 비워 현재 요청 응답과 섞이지 않게 한다.

        Returns: drop된 패킷 수 (디버깅용).
        """
        if not self._sock:
            return 0
        dropped = 0
        orig_timeout = self._sock.gettimeout()
        try:
            self._sock.setblocking(False)
            while True:
                try:
                    data = self._sock.recv(64)
                    if not data:
                        break
                    dropped += 1
                    if dropped > 32:
                        break  # 안전장치
                except BlockingIOError:
                    break
                except Exception:
                    break
        finally:
            try:
                self._sock.settimeout(orig_timeout)
            except Exception:
                pass
        if dropped:
            logger.debug("WoohyunBench drained %d stale packet(s) from rx buffer", dropped)
        return dropped

    def _send(self, data: list, recv: bool = True, recv_timeout: float = 60.0) -> list | bool:
        """UDP 패킷 전송 및 응답 수신.

        레거시 UDP_SEND() 함수와 동일한 패킷 구조 및 응답 검증 로직.
        단, 요청 전에 수신 버퍼를 비워 이전 응답이 섞이지 않도록 한다.
        """
        if not self._sock:
            raise RuntimeError("Not connected — call Connect() first")

        # 이전 명령 응답이 버퍼에 남아있으면 매칭 루프에서 오래 소모됨 → 보내기 전 비움
        self._drain_rx()

        data_len = len(data) - 2
        packet = [START_1, START_2, SENDER_ID, 0,
                  data[0], data[1],
                  (data_len >> 8) & 0xFF, data_len & 0xFF]
        for i in range(data_len):
            packet.append(data[2 + i])

        encoded = bytearray(packet)
        hex_str = " ".join(f"0x{b:02X}" for b in packet)

        try:
            self._sock.sendto(encoded, (self._host, self._udp_port))
        except Exception as e:
            logger.error("WoohyunBench send failed: %s", e)
            return False

        logger.info("WoohyunBench TX: %s", hex_str)

        if not recv:
            return True

        # 레거시와 동일: recv_timeout(60초) 내에서 1초 타임아웃으로 반복 수신
        current_time = time.time()
        while (time.time() - current_time) < recv_timeout:
            self._sock.settimeout(1)
            try:
                recv_data = self._sock.recv(16)
            except socket.timeout:
                continue
            except Exception as e:
                logger.error("WoohyunBench recv error: %s", e)
                return False
            finally:
                self._sock.settimeout(None)  # 레거시와 동일: recv 후 blocking 복원

            recv_list = [int(c) for c in recv_data]

            # 레거시와 동일: 송신 패킷과 응답 패킷의 [0],[1],[3],[4],[5] 비교
            res = True
            for idx, packet_value in enumerate(packet):
                if idx == 2:
                    continue
                elif idx >= len(recv_list) or packet_value != recv_list[idx]:
                    res = False
                    break
                if idx == 5:
                    res = True
                    break

            if res:
                recv_hex = " ".join(f"0x{b:02X}" for b in recv_list)
                logger.info("WoohyunBench RX: %s", recv_hex)
                return recv_list

        logger.warning("WoohyunBench: no matching response within %ds", recv_timeout)
        return True

    # ------------------------------------------------------------------
    # Power Control — 레거시 WOOHYUN_* 함수 동일
    # ------------------------------------------------------------------

    def IGN1(self, on_off: int = 1) -> str:
        """IGN1 제어 (0=OFF, 1=ON). 레거시 WOOHYUN_IGN1()."""
        data = [0x24, 0x22, on_off]
        res = self._send(data)
        status = "ON" if on_off else "OFF"
        return f"IGN1 {status}: {'OK' if res else 'FAIL'}"

    def IGN1_Read(self) -> int:
        """IGN1 상태 읽기. 응답이 3초 내 오지 않으면 -1."""
        res = self._send([0x24, 0x32], recv_timeout=3.0)
        # 응답 packet은 헤더 8바이트 + 1바이트 상태 = 9바이트 이상이어야 유효
        if isinstance(res, list) and len(res) >= 9:
            return res[-1]
        return -1

    def IGN2(self, on_off: int = 1) -> str:
        """IGN2 제어 (0=OFF, 1=ON). 레거시 WOOHYUN_IGN2()."""
        data = [0x24, 0x28, on_off]
        res = self._send(data)
        status = "ON" if on_off else "OFF"
        return f"IGN2 {status}: {'OK' if res else 'FAIL'}"

    def IGN2_Read(self) -> int:
        """IGN2 상태 읽기. 응답이 3초 내 오지 않으면 -1."""
        res = self._send([0x24, 0x38], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 9:
            return res[-1]
        return -1

    def ACC(self, on_off: int = 1) -> str:
        """ACC 제어 (0=OFF, 1=ON). 레거시 WOOHYUN_ACC()."""
        data = [0x24, 0x21, on_off]
        res = self._send(data)
        status = "ON" if on_off else "OFF"
        return f"ACC {status}: {'OK' if res else 'FAIL'}"

    def ACC_Read(self) -> int:
        """ACC 상태 읽기. 응답이 3초 내 오지 않으면 -1."""
        res = self._send([0x24, 0x31], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 9:
            return res[-1]
        return -1

    def BATTERY(self, on_off: int = 1) -> str:
        """Battery relay 제어 (0=OFF, 1=ON). 레거시 WOOHYUN_BATTERY()."""
        data = [0x24, 0x23, on_off]
        res = self._send(data)
        status = "ON" if on_off else "OFF"
        return f"BATTERY {status}: {'OK' if res else 'FAIL'}"

    def BATTERY_Read(self) -> int:
        """Battery relay 상태 읽기. 응답이 3초 내 오지 않으면 -1.

        장비가 echo만 반환(상태 payload 없음)하면 len(res)==8이어서 -1로 처리.
        정상 응답은 헤더 8 + 상태 1 = 최소 9바이트.
        """
        res = self._send([0x24, 0x33], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 9:
            return res[-1]
        return -1

    def BatterySet(self, voltage: float = 14.4) -> str:
        """배터리 전압 설정 (V). 레거시 BATTERY_SET()."""
        data = [0x20, 0x01, int(voltage * 10)]
        res = self._send(data)
        return f"Battery set to {voltage}V: {'OK' if res else 'FAIL'}"

    def BatteryCheck(self) -> float:
        """배터리 전압 읽기 (V). 레거시 BATTERY_CHECK()."""
        res = self._send([0x20, 0x02], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 9:
            return float(res[-1]) / 10
        return -1.0

    def AmpereCheck(self) -> float:
        """전류 읽기 (A). 레거시 AMPERE_CHECK()."""
        res = self._send([0x20, 0x03], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 10:
            raw = (res[-1] << 8) | res[-2]
            return float(raw) / 1000
        return -1.0

    # ------------------------------------------------------------------
    # Generic
    # ------------------------------------------------------------------

    def SendCommand(self, cmd1: int, cmd2: int, data_hex: str = "") -> str:
        """범용 UDP 명령 전송. data_hex: 공백 구분 hex (예: 'FF 01')."""
        cmd = [cmd1, cmd2]
        if data_hex:
            cmd.extend(int(b, 16) for b in data_hex.split())
        res = self._send(cmd)
        if isinstance(res, list):
            return " ".join(f"0x{b:02X}" for b in res)
        return str(res)

    def GetInfo(self) -> str:
        """연결 정보 (host/port + CAN FD 신호 수 포함)."""
        sig_count = len(self._canfd.signal_defs) if self._canfd is not None else 0
        return (f"host={self._host}, port={self._udp_port}, "
                f"signal_file={self._signal_file}, signals={sig_count}, "
                f"canfd={'on' if self._canfd is not None else 'off'}, "
                f"connected={self.IsConnected()}")
