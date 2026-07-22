
### 📌 [시스템 아키텍처 및 핵심 구조]
'''
1. **Multi-MCU & Multi-Channel 지원**:
   - 장비는 최대 3개(`mcu1`, `mcu2`, `mcu3`), 각 장비는 최대 4개의 채널(`A`, `B`, `C`, `D`)을 가집니다.
   - 단일 소켓(`self._sock`)이 아닌 `self._connected_mcus` 딕셔너리로 각 MCU의 UDP Tx 소켓을 관리합니다.
   - Rx 수신 포트는 채널마다 다르며, `self._rx_sockets`와 `self._rx_threads`에 포트 번호를 Key로 하여 멀티스레드로 관리됩니다.

2. **명령어 전송 컨벤션 (`_send`)**:
   - 장비로 제어 명령을 보낼 때 사용하는 `self._send()` 함수는 반드시 첫 번째 인자로 대상 장비 이름(`mcu`)을 명시해야 합니다. (예: `self._send("mcu1", data)`)
   - 전원/상태 제어 함수(IGN1, ACC, BATTERY 등)들도 모두 `mcu` 파라미터를 받아 특정 장비에만 명령을 내릴 수 있도록 설계되어 있습니다.

3. **채널 속도 동적 할당 (`woohyunchannelconfig.txt`)**:
   - `Connect()` 시 외부 txt 파일에서 `mcu`, `channel` 별 Speed와 DataSpeed(Baudrate)를 읽어와 `_send_canfd_init()`에 개별 적용합니다.

4. **멀티스레드 기반 비동기 처리**:
   - 수신(Rx): 채널별 포트마다 데몬 스레드가 돌며 데이터를 감지하고 `_write_log`를 호출합니다.
   - 송신(Tx): `SendCan` 호출 시 주기 전송(Periodic)을 위해 개별 스레드가 생성됩니다. 
   - 로그 저장 시 스레드 충돌을 막기 위해 `self._log_lock`을 사용 중입니다.

5. **UDP 패킷 포맷**:
   - 패킷 전송 시 헤더(8 Byte) 구조를 항상 유지해야 합니다. 
   - `[START_1(0x55), START_2(0xAA), SENDER_ID, 0, cmd1, cmd2, data_len_hi, data_len_lo] + payload`

### ⚠️ [수정 시 주의사항 (Golden Rules)]
- 기존 레거시 코드의 **단일 장비 제어 구조(예: `self._sock`, 글로벌 전송 등)로 회귀시키는 코드를 작성하지 마세요.** 항상 `mcu` 변수를 식별자로 활용하세요.
- `IsConnected()`는 `len(self._connected_mcus) > 0` 로 다중 장비 상태를 확인합니다. 이 로직을 훼손하지 마세요.
- CAN 통신을 위해 `SendCan`이나 `CanSaveStart`를 건드릴 때, 스레드 플래그(`_rx_stop_event`, `stop_event`) 관리에 유의하여 메모리 누수나 데드락이 없도록 하세요.

'''
from __future__ import annotations

import os
import socket
import logging
import time
import datetime
import threading
import subprocess
import platform
import collections
from typing import Optional, Callable

# 📌 UDP_CANFD 모듈 임포트 유지
try:
    from ..lib.UDP_CANFD import UDP_CANFD
except ImportError:
    logging.warning("UDP_CANFD 모듈을 찾을 수 없습니다. Signal 관련 기능이 제한될 수 있습니다.")

logger = logging.getLogger(__name__)

START_1 = 0x55
START_2 = 0xAA
SENDER_ID = 100


def _payload_size_to_dlc(payload_size: int) -> int:
    _map = {
        0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6, 7: 7,
        8: 8, 12: 9, 16: 10, 20: 11, 24: 12, 32: 13, 48: 14, 64: 15,
    }
    return _map.get(payload_size, 8)


class WoohyunBench:
    logger.info('class WoohyunBench:')
    def __init__(self, signal_file: str = "", signal_file_AVN: str = ""):
        self._signal_file = (signal_file or "").strip()
        self._signal_file_AVN = (signal_file_AVN or "").strip()

        # 📌 UDP_CANFD 객체 초기화
        self._canfd = None
        try:
            # `from ..lib.UDP_CANFD import UDP_CANFD` 는 클래스를 직접 import 하므로
            # UDP_CANFD() 로 인스턴스화한다 (UDP_CANFD.UDP_CANFD() 는 AttributeError).
            if 'UDP_CANFD' in globals():
                self._canfd = UDP_CANFD()
        except Exception as e:
            logger.warning(f"UDP_CANFD 인스턴스 초기화 실패: {e}")

        self._mcu_configs = {
            "mcu1": {
                "ip": "192.168.1.101", "port": 25000,
                "channels": {
                    "A": {"cmd1": 0x02, "cmd2": 0x10, "rx_port": 25001},
                    "B": {"cmd1": 0x03, "cmd2": 0x11, "rx_port": 25002},
                    "C": {"cmd1": 0x04, "cmd2": 0x12, "rx_port": 25003},
                    "D": {"cmd1": 0x05, "cmd2": 0x13, "rx_port": 25004},
                }
            },
            "mcu2": {
                "ip": "192.168.1.102", "port": 25005,
                "channels": {
                    "A": {"cmd1": 0x06, "cmd2": 0x10, "rx_port": 25006},
                    "B": {"cmd1": 0x07, "cmd2": 0x11, "rx_port": 25007},
                    "C": {"cmd1": 0x08, "cmd2": 0x12, "rx_port": 25008},
                    "D": {"cmd1": 0x09, "cmd2": 0x13, "rx_port": 25009},
                }
            },
            "mcu3": {
                "ip": "192.168.1.103", "port": 25010,
                "channels": {
                    "A": {"cmd1": 0x0A, "cmd2": 0x10, "rx_port": 25011},
                    "B": {"cmd1": 0x0B, "cmd2": 0x11, "rx_port": 25012},
                    "C": {"cmd1": 0x0C, "cmd2": 0x12, "rx_port": 25013},
                    "D": {"cmd1": 0x0D, "cmd2": 0x13, "rx_port": 25014},
                }
            }
        }

        self._channel_speeds = self._load_channel_config()

        self._connected_mcus = {}
        self._rx_sockets = {}
        self._rx_threads = {}
        self._rx_stop_event = threading.Event()
        self._rx_callbacks = {}

        self._log_file = None
        self._log_lock = threading.Lock()
        self._flush_counter = 0
        self._stop_events = {}

        # 📌 [신규 추가] CAN 메시지 사전 캡처(Capture) 용 변수
        # 📌 [수정] 리스트([]) 대신 deque를 사용하여 FIFO 링 버퍼 구현
        self._capture_buffer = collections.deque(maxlen=100000)
        self._capture_lock = threading.Lock()
        self._is_capturing = False

    def _load_channel_config(self) -> dict:
        logger.info('_load_channel_config')
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "woohyunchannelconfig.txt")
        config = {}

        # 1. 파일이 없을 경우 요청하신 내용으로 기본 파일 자동 생성
        if not os.path.exists(config_path):
            default_content = """# 우현 장비 채널별 속도 설정 파일
            # [형식] 장비이름, 채널, Speed, DataSpeed
            # Speed 기본값: 500 / DataSpeed 기본값: 2000
            mcu1, A, 500, 2000
            mcu1, B, 500, 2000
            mcu1, C, 500, 2000
            mcu1, D, 500, 1000
            mcu2, A, 500, 1000
            mcu2, B, 500, 2000
            mcu2, C, 500, 2000
            mcu2, D, 500, 2000
            mcu3, A, 500, 2000
            mcu3, B, 500, 2000
            mcu3, C, 500, 2000
            mcu3, D, 500, 2000
            """
            try:
                with open(config_path, "w", encoding="utf-8") as f:
                    f.write(default_content)
                logger.info(f"[*] 채널 설정 파일이 없어 기본값으로 생성했습니다: {config_path}")
            except Exception as e:
                logger.warning(f"채널 설정 파일 생성 실패: {e}")

        # 2. 파일이 존재할 경우 설정 읽어오기
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = [p.strip() for p in line.split(",")]
                        if len(parts) >= 4:
                            mcu, ch = parts[0], parts[1].upper()
                            baud = int(parts[2])
                            dbaud = int(parts[3])
                            if mcu not in config:
                                config[mcu] = {}
                            config[mcu][ch] = (baud, dbaud)
            except Exception as e:
                logger.warning(f"설정 파일 읽기 오류 (기본값 500/2000 사용): {e}")

        return config

    @staticmethod
    def SendTimeSync():
        logger.info('SendTimeSync')
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            packet = bytearray([0x55, 0xAA, 0x00])
            sock.sendto(packet, ("255.255.255.255", 25100))
            sock.close()
        except:
            pass

    def _is_reachable(self, ip: str) -> bool:
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        timeout_param = '-w' if platform.system().lower() == 'windows' else '-W'
        timeout_val = '1000' if platform.system().lower() == 'windows' else '1'
        command = ['ping', param, '1', timeout_param, timeout_val, ip]
        try:
            result = subprocess.call(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return result == 0
        except Exception:
            return False

    def Connect(self, rx_callback: Optional[Callable] = None) -> str:
        logger.info('Connect')
        self.Disconnect()
        self._rx_stop_event.clear()
        self.SendTimeSync()

        mcus_to_check = ["mcu1", "mcu2", "mcu3"]
        connected_count = 0

        for mcu in mcus_to_check:
            conf = self._mcu_configs.get(mcu)
            if not conf: continue

            ip = conf["ip"]

            # 📌 1. mcu1은 Ping 테스트를 완전히 생략하고 무조건 통신 연결 시도!
            if mcu == "mcu1":
                logger.info(f"[{mcu}] 필수 장비이므로 Ping 체크를 생략하고 강제 연결을 시도합니다 ({ip})...")
            else:
                # 📌 2. mcu2, mcu3는 기존처럼 Ping 테스트 진행
                logger.info(f"Checking connection to {mcu} ({ip})...")
                if not self._is_reachable(ip):
                    logger.info(f"[{mcu}] No ping response. Skipping...")
                    continue

            # 소켓 연결 (mcu1은 무조건 여기로 내려옴)
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.connect((ip, conf["port"]))
            self._connected_mcus[mcu] = sock
            logger.info(f"Connected to {mcu} ({ip}:{conf['port']})")

            try:
                self._send(mcu, [0x1F, 0x00], recv=False)
                time.sleep(0.1)

                for ch_key, ch_data in conf["channels"].items():
                    ct = 2 if ch_key == "C" else 0
                    baud, dbaud = self._channel_speeds.get(mcu, {}).get(ch_key, (500, 2000))
                    logger.debug(f"[{mcu}-{ch_key}] Init - Baudrate: {baud}, DataBaudrate: {dbaud}")

                    self._send_canfd_init(mcu, channel=ch_key, baudrate=baud, data_baudrate=dbaud, line_select=0,
                                          can_type=ct)

                    rx_port = ch_data["rx_port"]
                    self._start_rx_listener(mcu, rx_port, channel_name=ch_key, callback=rx_callback)
            except Exception as e:
                logger.warning(f"연결 초기화 실패 [{mcu}]: {e}")

            connected_count += 1

        if connected_count == 0:
            return "FAIL: Ping 테스트 결과 연결 가능한 MCU가 없습니다."

        # Signal-based 전송(LoadSignals/SendSignal/DoorTest/TestAllSignals)이 사용하는
        # UDP_CANFD 위임 객체에 mcu1 의 live 소켓을 공유 바인딩한다. 바인딩하지 않으면
        # UDP_CANFD_SEND 가 self.sock=None 으로 조용히 no-op 된다.
        if self._canfd is not None and "mcu1" in self._connected_mcus:
            try:
                self._canfd.sock = self._connected_mcus["mcu1"]
                self._canfd.udp_ip = self._mcu_configs["mcu1"]["ip"]
                self._canfd.udp_port = self._mcu_configs["mcu1"]["port"]
                # 연결 시 signal_file 이 지정되어 있고 아직 로드 전이면 자동 로드
                if self._signal_file and not getattr(self._canfd, "signal_defs", None):
                    self.LoadSignals(self._signal_file)
            except Exception as e:
                logger.warning(f"UDP_CANFD 소켓 바인딩 실패 (signal 기능 제한): {e}")

        return f"OK: Connected to {connected_count} MCU(s) (mcu1 forced)"

    def _send_canfd_init(self, mcu: str, channel: str = "A", baudrate: int = 500, data_baudrate: int = 2000,
                         line_select: int = 0, can_type: int = 0) -> None:
        logger.info('_send_canfd_init')
        sock = self._connected_mcus.get(mcu)
        if not sock: return

        conf = self._mcu_configs.get(mcu)
        ch_conf = conf["channels"].get(channel.upper())
        if not ch_conf: return

        cmd1, line_cmd2 = ch_conf["cmd1"], ch_conf["cmd2"]
        host, port = conf["ip"], conf["port"]

        line_packet = bytearray([START_1, START_2, SENDER_ID, 0x00, 0x30, line_cmd2, 0x00, 0x01, line_select])
        sock.sendto(line_packet, (host, port))
        time.sleep(0.05)

        baud_hi, baud_lo = (baudrate >> 8) & 0xFF, baudrate & 0xFF
        data_hi, data_lo = (data_baudrate >> 8) & 0xFF, data_baudrate & 0xFF

        open_packet = bytearray([
            START_1, START_2, SENDER_ID, 0x00, cmd1, 0x10, 0x00, 0x06,
            can_type, 0x00, baud_hi, baud_lo, data_hi, data_lo
        ])
        sock.sendto(open_packet, (host, port))
        time.sleep(0.05)

    def SendCan(self, mcu: str = "mcu1", msg_id: str = "", type: str = "STA", payload_hex: str = "",
                   channel: str = "A", repeat: int = 0, cycle_ms: int = 200) -> str:
        logger.info('SendCan')
        if mcu not in self._connected_mcus:
            return f"FAIL: {mcu} 장비가 연결되어 있지 않습니다."
        try:
            cid = int(msg_id.replace("0x", ""), 16) if isinstance(msg_id, str) else int(msg_id)
            clean_hex = payload_hex.replace(" ", "").replace(",", "").replace("0x", "")
            if len(clean_hex) % 2 != 0: clean_hex = "0" + clean_hex
            payload = bytearray.fromhex(clean_hex) if clean_hex else bytearray([0x00])

            is_periodic = (int(repeat) == 1)
            interval_sec = max(0.001, int(cycle_ms) / 1000.0)

            stop_key = f"{mcu}_{cid}"
            if stop_key in self._stop_events: self._stop_events[stop_key].set()

            stop_event = threading.Event()
            self._stop_events[stop_key] = stop_event

            t = threading.Thread(
                target=self._send_canfd_raw,
                args=(mcu, cid, payload),
                kwargs={"can_type": type, "channel": channel, "is_periodic": is_periodic, "interval_sec": interval_sec,
                        "stop_event": stop_event},
                daemon=True
            )
            t.start()
            mode_str = "주기 전송" if is_periodic else "단발 전송"
            return f"OK: SendCan ID=0x{cid:X} ({len(payload)}B, 장비 {mcu}, 채널 {channel.upper()}) [{mode_str} 시작]"
        except Exception as e:
            return f"FAIL: SendCan Error: {e}"

    def _send_canfd_raw(self, mcu: str, can_id: int, payload: bytearray, can_type: str = "STA",
                        channel: str = "A", is_periodic: bool = False, interval_sec: float = 0.2,
                        stop_event: Optional[threading.Event] = None) -> None:
        logger.info('_send_canfd_raw')
        sock = self._connected_mcus.get(mcu)
        if not sock: return
        conf = self._mcu_configs[mcu]
        ch_key = channel.upper()
        ch_conf = conf["channels"].get(ch_key)
        if not ch_conf: return

        cmd1 = ch_conf["cmd1"]
        host, port = conf["ip"], conf["port"]

        can_id_bytes = [(can_id >> 24) & 0xFF, (can_id >> 16) & 0xFF, (can_id >> 8) & 0xFF, can_id & 0xFF]
        dlc = _payload_size_to_dlc(len(payload))
        ct = str(can_type).upper()

        if ct == "FD":
            can_frame = 0x80 | (dlc & 0x7F)
        elif ct == "EXT":
            can_frame = 0x20 | (dlc & 0x7F)
        else:
            can_frame = dlc & 0x7F

        padded_payload = list(payload)
        if len(padded_payload) < 8: padded_payload += [0x00] * (8 - len(padded_payload))

        data_body = can_id_bytes + [can_frame, 0x00] + padded_payload
        data_len = len(data_body)
        header = [START_1, START_2, SENDER_ID, 0x00, cmd1, 0x30, (data_len >> 8) & 0xFF, data_len & 0xFF]
        packet = bytearray(header + data_body)

        if is_periodic:
            while (mcu in self._connected_mcus) and not self._rx_stop_event.is_set():
                if stop_event and stop_event.is_set(): break
                try:
                    sock.sendto(packet, (host, port))
                    self._write_log(mcu, ch_key, "Tx", can_id, padded_payload)
                except:
                    break
                if stop_event and stop_event.wait(interval_sec): break
        else:
            if (mcu in self._connected_mcus) and not (stop_event and stop_event.is_set()):
                try:
                    sock.sendto(packet, (host, port))
                    self._write_log(mcu, ch_key, "Tx", can_id, padded_payload)
                except:
                    pass

    def SendStopCan(self, msg_id: str, mcu: str = "mcu1") -> str:
        """
        지정된 장비(mcu)와 msg_id의 CAN 메시지 전송(주기 송신 등)을 중단합니다.
        """
        logger.info('SendStopCan')
        if not hasattr(self, '_stop_events') or not self._stop_events:
            return "OK: 현재 주기 송신 중인 메시지가 없습니다."

        try:
            # hex string이나 int 형태의 msg_id를 정수형(cid)으로 파싱
            cid = int(str(msg_id).replace("0x", ""), 16) if isinstance(msg_id, str) else int(msg_id)

            # 📌 Multi-MCU 아키텍처에 맞춘 Key 검색 (예: mcu1_1143)
            stop_key = f"{mcu}_{cid}"

            if stop_key in self._stop_events:
                # 해당 메시지 송신 스레드에 정지(Event) 전달
                self._stop_events[stop_key].set()
                # 관리 딕셔너리에서 제거
                del self._stop_events[stop_key]
                return f"OK: SendStopCan - [장비: {mcu}] ID=0x{cid:X} 메시지 전송 중단 완료"
            else:
                return f"OK: SendStopCan - [장비: {mcu}] ID=0x{cid:X} 메시지는 현재 송신 중이 아닙니다."
        except Exception as e:
            logger.error(f"WoohyunBench SendStopCan failed: {e}")
            return f"FAIL: SendStopCan Error - {e}"

    def SendAllStopCan(self) -> str:
        """
        현재 전체 장비(MCU)에서 주기 송신 중인 모든 메시지 전송을 일괄 중단합니다.
        """
        logger.info('SendAllStopCan')
        if not hasattr(self, '_stop_events') or not self._stop_events:
            return "OK: 현재 주기 송신 중인 메시지가 없습니다."

        try:
            count = 0
            # 등록된 모든 주기 송신 스레드의 stop_event를 작동시켜 일괄 종료
            for stop_key, event in self._stop_events.items():
                event.set()
                count += 1

            # 관리 딕셔너리 비우기
            self._stop_events.clear()

            return f"OK: SendAllStopCan - 전체 송신 메시지({count}개) 전송 일괄 중단 완료"
        except Exception as e:
            logger.error(f"WoohyunBench SendAllStopCan failed: {e}")
            return f"FAIL: SendAllStopCan Error - {e}"

    def CanSaveStart(self, save_dir: str = "") -> str:
        logger.info('CanSaveStart')
        if not self._connected_mcus:
            return "CanSaveStart FAIL: 연결된 장비가 없습니다.."
        try:
            abs_save_dir = os.path.abspath(save_dir) if save_dir else os.path.abspath("./canlog")
            os.makedirs(abs_save_dir, exist_ok=True)

            timestamp_str = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
            filename = f"CAN_LOG_{timestamp_str}.csv"
            abs_save_path = os.path.join(abs_save_dir, filename)

            self.CanSaveStop()

            self._log_file = open(abs_save_path, "a", encoding="utf-8")
            self._flush_counter = 0

            if os.path.getsize(abs_save_path) == 0:
                self._log_file.write("Date/Time,MCU,CH,Dir,MessageID,payload_hex\n")
            self._log_file.flush()

            # 📌 요청하신 장비 접속 로그 출력
            logger.info(f"[*] 연결된 MCU 목록 : {self._connected_mcus}")

            activated_channels = 0
            for mcu in list(self._connected_mcus.keys()):
                for ch_key, ch_data in self._mcu_configs[mcu]["channels"].items():
                    ct = 2 if ch_key == "C" else 0
                    baud, dbaud = self._channel_speeds.get(mcu, {}).get(ch_key, (500, 2000))

                    # 📌 요청하신 채널별 속도 로그 출력
                    logger.info(f"mcu, ch_key, baud, dbaud  {mcu} {ch_key} {baud} {dbaud}")

                    self._send_canfd_init(mcu, ch_key, baudrate=baud, data_baudrate=dbaud, line_select=0, can_type=ct)

                    rx_port = ch_data["rx_port"]
                    if rx_port not in self._rx_threads or not self._rx_threads[rx_port].is_alive():
                        self._start_rx_listener(mcu, rx_port, channel_name=ch_key)
                    else:
                        logger.info(f"[*] {mcu}-{ch_key} 채널 로깅 준비 완료 (Port: {rx_port})")
                    activated_channels += 1

            return f"OK: 총 {activated_channels}개 채널 로깅 통합 시작 (저장경로: {abs_save_path})"
        except Exception as e:
            return f"FAIL: 통합 채널 로깅 시작 실패 - {e}"

    def CanSaveStop(self) -> str:
        logger.info('CanSaveStop')
        try:
            with self._log_lock:
                if self._log_file is not None:
                    self._log_file.flush()
                    self._log_file.close()
                    self._log_file = None
                    return "OK: 모든 채널 통합 로그 저장 완전 종료"
                return "OK: 현재 로깅 중인 파일이 없습니다."
        except Exception as e:
            return f"FAIL: 통합 로그 저장 종료 실패 - {e}"

    def _write_log(self, mcu: str, ch_key: str, direction: str, can_id: int, payload_bytes) -> None:
        if not self._log_file: return
        try:
            dt_str = datetime.datetime.now().strftime('%Y-%m-%d_%H:%M:%S.%f')
            payload_hex = ",".join(f"{b:02X}" for b in payload_bytes)
            log_line = f'{dt_str},{mcu},{ch_key},{direction},0x{can_id:X},"{payload_hex}"\n'

            with self._log_lock:
                if self._log_file:
                    self._log_file.write(log_line)
                    self._flush_counter += 1
                    if self._flush_counter >= 5:
                        self._log_file.flush()
                        self._flush_counter = 0
        except:
            pass

    def _start_rx_listener(self, mcu: str, port: int, channel_name: str, callback: Optional[Callable] = None) -> None:
        logger.info('_start_rx_listener')
        try:
            # 스레드가 죽어서 소켓을 다시 열어야 할 때, 기존 소켓 닫아주기 (좀비 블랙홀 방지)
            if port in self._rx_sockets:
                try:
                    self._rx_sockets[port].close()
                except:
                    pass

            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", port))
            sock.settimeout(1.0)
            self._rx_sockets[port] = sock

            # [핵심 수정] 외부에서 문자열(str)을 던져도 에러가 나지 않도록, 진짜 함수(callable)일 때만 등록!
            if callback and callable(callback):
                self._rx_callbacks[port] = callback

            t = threading.Thread(target=self._rx_task, args=(sock, port, mcu, channel_name), daemon=True)
            t.start()
            self._rx_threads[port] = t

            baud, dbaud = self._channel_speeds.get(mcu, {}).get(channel_name.upper(), (500, 2000))
            logger.info(f"[*] {mcu}-{channel_name} 리스너 오픈 (Port: {port} | Speed: {baud}, {dbaud})")
        except Exception as e:
            logger.error(f"Rx listener start failed for port {port}: {e}")

    def _rx_task(self, sock: socket.socket, port: int, mcu: str, channel_name: str) -> None:
        ch_key = channel_name.upper()
        cmd1 = self._mcu_configs[mcu]["channels"].get(ch_key, {}).get("cmd1")

        while not self._rx_stop_event.is_set():
            try:
                data, addr = sock.recvfrom(4096)
                if not data: continue

                if cmd1 is not None and len(data) >= 18 and data[0] == 0x55 and data[1] == 0xAA and data[4] == cmd1 and \
                        data[5] == 0x41:
                    can_id = (data[12] << 24) | (data[13] << 16) | (data[14] << 8) | data[15]
                    payload = data[18:]
                    self._write_log(mcu, ch_key, "Rx", can_id, payload)

                    # 📌 [신규 추가] canmsg_start가 호출되어 캡처 모드인 경우 실시간 적재, deque를 사용하므로 길이 체크 없이 append (자동으로 오래된 것 밀어냄)
                    if self._is_capturing:
                        recv_time = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                        with self._capture_lock:
                            self._capture_buffer.append((mcu, ch_key, can_id, payload, recv_time))

                else:
                    self._write_log(mcu, ch_key, "Rx", port, data)

                # [핵심 수정] 콜백 실행 시 진짜 함수인지(callable) 한 번 더 확인!
                if port in self._rx_callbacks and callable(self._rx_callbacks[port]):
                    try:
                        self._rx_callbacks[port](f"{mcu}_{ch_key}", data)
                    except Exception as cb_err:
                        logger.error(f"Callback Error: {cb_err}")  # 콜백 에러가 나도 스레드가 죽지 않게 방어
            except socket.timeout:
                continue
            except OSError as e:
                win_err = getattr(e, 'winerror', None)
                # 1. Windows UDP 타겟 무응답(10054) 에러 방어
                if win_err == 10054:
                    continue
                # 📌 [수정] 플래그 체크 없이 10038(소켓 파괴) 에러면 무조건 스레드 조용히 종료
                if win_err == 10038:
                    break
                logger.error(f"[{mcu}-{ch_key}] Rx OSError: {e}")
                break
            except Exception as e:
                logger.error(f"[{mcu}-{ch_key}] Rx Exception: {e}")
                break

    def Disconnect(self) -> str:
        logger.info('Disconnect')
        self._rx_stop_event.set()
        self.CanSaveStop()

        for sock in self._rx_sockets.values():
            try:
                sock.close()
            except:
                pass
        self._rx_sockets.clear()

        for sock in self._connected_mcus.values():
            try:
                sock.close()
            except:
                pass
        self._connected_mcus.clear()

        return "Disconnected"

    def _drain_rx(self, sock: socket.socket) -> int:
        logger.info('_drain_rx')
        dropped = 0
        orig_timeout = sock.gettimeout()
        try:
            sock.setblocking(False)
            while True:
                try:
                    data = sock.recv(64)
                    if not data: break
                    dropped += 1
                    if dropped > 32: break
                except:
                    break
        finally:
            try:
                sock.settimeout(orig_timeout)
            except:
                pass
        return dropped

    def _send(self, mcu: str, data: list, recv: bool = True, recv_timeout: float = 3.0) -> list | bool:
        logger.info('_send')
        sock = self._connected_mcus.get(mcu)
        if not sock: return False

        conf = self._mcu_configs[mcu]
        host, port = conf["ip"], conf["port"]
        self._drain_rx(sock)
        data_len = len(data) - 2
        packet = [START_1, START_2, SENDER_ID, 0, data[0], data[1], (data_len >> 8) & 0xFF, data_len & 0xFF] + data[2:]

        try:
            sock.sendto(bytearray(packet), (host, port))
        except Exception as e:
            logger.error(f"[{mcu}] _send Tx Error: {e}")
            return False

        if not recv: return True
        current_time = time.time()
        while (time.time() - current_time) < recv_timeout:
            sock.settimeout(1)
            try:
                recv_data = sock.recv(16)
            except socket.timeout:
                continue
            except OSError as e:
                # [핵심] UDP WinError 10054 발생 시 통신을 끊지 않고 다음 수신 대기
                if getattr(e, 'winerror', None) == 10054:
                    continue
                logger.error(f"[{mcu}] _send Rx OSError: {e}")
                return False
            except Exception as e:
                logger.error(f"[{mcu}] _send Rx Error: {e}")
                return False
            finally:
                sock.settimeout(None)

            recv_list = [int(c) for c in recv_data]
            res = True
            for idx, packet_value in enumerate(packet):
                if idx == 2:
                    continue
                elif idx >= len(recv_list) or packet_value != recv_list[idx]:
                    res = False
                    break
                if idx == 5: res = True; break
            if res: return recv_list
        return True

    # ------------------------------------------------------------------
    # Power & Status Control
    # ------------------------------------------------------------------

    def IsConnected(self) -> bool:
        return len(self._connected_mcus) > 0

    def _ensure_mcu1_connected(self) -> bool:
        """
        [수정: 조건부 재연결]
        (무조건 Connect()를 호출하면 진행중인 로그/캡처가 다 끊기므로,
        실제 끊어졌거나 유효하지 않을 때만 재연결하도록 수정)
        """
        mcu = "mcu1"
        is_dead = True

        if mcu in self._connected_mcus:
            try:
                # 소켓이 살아있고 유효한지 파일 디스크립터 검사
                if self._connected_mcus[mcu].fileno() != -1:
                    is_dead = False
            except:
                pass

        if is_dead:
            logger.info("👉 장비 소켓이 끊어져 있어 Connect()를 재실행합니다.")
            self.Connect()

        return mcu in self._connected_mcus

    def _safe_val(self, on_off) -> int:
        """ReplayKit UI에서 빈칸("")이나 None을 던져도 기본값(1, ON)으로 켜지도록 보정"""
        val_str = str(on_off).strip().lower()
        if val_str in ['', 'none']:
            return 1
        return 1 if val_str in ['1', 'on', 'true', 'y', 'yes'] else 0

    def IGN1(self, on_off: int = 1, mcu: str = "mcu1") -> str:
        val = self._safe_val(on_off)
        logger.info(f"👉 [IGN1] 제어값: {val} (1=ON, 0=OFF)")
        if not self._ensure_mcu1_connected(): return "FAIL: 연결 에러"

        self._send("mcu1", [0x24, 0x22, val], recv=False)
        time.sleep(0.3)
        return f"[mcu1] IGN1 {'ON' if val else 'OFF'}: OK"

    def IGN1_Read(self, mcu: str = "mcu1") -> int:
        logger.info('IGN1_Read')
        if not self._ensure_mcu1_connected(): return -1
        res = self._send("mcu1", [0x24, 0x32], recv_timeout=3.0)
        return res[-1] if isinstance(res, list) and len(res) >= 9 else -1

    def IGN2(self, on_off: int = 1, mcu: str = "mcu1") -> str:
        val = self._safe_val(on_off)
        logger.info(f"👉 [IGN2] 제어값: {val} (1=ON, 0=OFF)")
        if not self._ensure_mcu1_connected(): return "FAIL: 연결 에러"

        self._send("mcu1", [0x24, 0x28, val], recv=False)
        time.sleep(0.3)
        return f"[mcu1] IGN2 {'ON' if val else 'OFF'}: OK"

    def IGN2_Read(self, mcu: str = "mcu1") -> int:
        logger.info('IGN2_Read')
        if not self._ensure_mcu1_connected(): return -1
        res = self._send("mcu1", [0x24, 0x38], recv_timeout=3.0)
        return res[-1] if isinstance(res, list) and len(res) >= 9 else -1

    def ACC(self, on_off: int = 1, mcu: str = "mcu1") -> str:
        val = self._safe_val(on_off)
        logger.info(f"👉 [ACC] 제어값: {val} (1=ON, 0=OFF)")
        if not self._ensure_mcu1_connected(): return "FAIL: 연결 에러"

        self._send("mcu1", [0x24, 0x21, val], recv=False)
        time.sleep(0.3)
        return f"[mcu1] ACC {'ON' if val else 'OFF'}: OK"

    def ACC_Read(self, mcu: str = "mcu1") -> int:
        logger.info('ACC_Read')
        if not self._ensure_mcu1_connected(): return -1
        res = self._send("mcu1", [0x24, 0x31], recv_timeout=3.0)
        return res[-1] if isinstance(res, list) and len(res) >= 9 else -1

    def BATTERY(self, on_off: int = 1, mcu: str = "mcu1") -> str:
        val = self._safe_val(on_off)
        logger.info(f"👉 [BATTERY] 제어값: {val} (1=ON, 0=OFF)")
        if not self._ensure_mcu1_connected(): return "FAIL: 연결 에러"

        self._send("mcu1", [0x24, 0x23, val], recv=False)
        time.sleep(0.3)
        return f"[mcu1] BATTERY {'ON' if val else 'OFF'}: OK"

    def BATTERY_Read(self, mcu: str = "mcu1") -> int:
        logger.info('BATTERY_Read')
        if not self._ensure_mcu1_connected(): return -1
        res = self._send("mcu1", [0x24, 0x33], recv_timeout=3.0)
        return res[-1] if isinstance(res, list) and len(res) >= 9 else -1

    def BatterySet(self, voltage: float = 14.4, mcu: str = "mcu1") -> str:
        logger.info(f"👉 [BatterySet] 입력 전압값: '{voltage}'")
        if not self._ensure_mcu1_connected(): return "FAIL: 연결 에러"

        try:
            vol_str = str(voltage).replace('V', '').replace('v', '').strip()
            vol_val = 14.4 if vol_str in ['', 'none'] else float(vol_str)
        except:
            vol_val = 14.4

        self._send("mcu1", [0x20, 0x01, int(vol_val * 10)], recv=False)
        time.sleep(0.3)
        return f"[mcu1] Battery set to {vol_val}V: OK"

    def BatteryCheck(self, mcu: str = "mcu1") -> float:
        logger.info('BatteryCheck')
        if not self._ensure_mcu1_connected(): return -1.0
        res = self._send("mcu1", [0x20, 0x02], recv_timeout=3.0)
        return float(res[-1]) / 10 if isinstance(res, list) and len(res) >= 9 else -1.0

    def AmpereCheck(self, mcu: str = "mcu1") -> float:
        logger.info('AmpereCheck')
        if not self._ensure_mcu1_connected(): return -1.0
        res = self._send("mcu1", [0x20, 0x03], recv_timeout=3.0)
        if isinstance(res, list) and len(res) >= 10:
            raw = (res[-1] << 8) | res[-2]
            return float(raw) / 1000
        return -1.0

    def GetInfo(self) -> str:
        connected_list = list(self._connected_mcus.keys())
        return (f"connected_mcus={connected_list}, "
                f"signal_file={self._signal_file}, "
                f"is_connected={self.IsConnected()}")

    # ------------------------------------------------------------------
    # 📌 Signal-based 데이터 전송 (UDP_CANFD 연동)
    # ------------------------------------------------------------------

    def LoadSignals(self, file_path: str) -> str:
        """
        CAN 신호 정의 파일(Excel/XML)을 로드하고 장비의 버스를 일괄 초기화합니다.
        """
        if not self._canfd:
            return "FAIL: UDP_CANFD 모듈이 초기화되지 않았습니다."
        try:
            ext = os.path.splitext(file_path)[-1].lower()
            if ext in ['.xls', '.xlsx']:
                self._canfd.load_signal_definitions_from_excel(file_path)
            elif ext == '.can':
                self._canfd.load_signal_definitions_from_xml(file_path)
            else:
                return f"FAIL: 지원하지 않는 파일 확장자입니다 ({ext})"
            
            # 📌 기존의 단일 UDP_CANFD_INIT_MESSAGE() 대신 Multi-MCU 환경에 맞게
            # 현재 연결된 모든 MCU 및 채널에 대해 버스를 일괄 재동기화 합니다.
            for mcu in list(self._connected_mcus.keys()):
                for ch_key in self._mcu_configs[mcu]["channels"].keys():
                    baud, dbaud = self._channel_speeds.get(mcu, {}).get(ch_key, (500, 2000))
                    ct = 2 if ch_key == "C" else 0
                    self._send_canfd_init(mcu, channel=ch_key, baudrate=baud, data_baudrate=dbaud, line_select=0, can_type=ct)

            self._signal_file = file_path
            return f"OK: {file_path} 파일 로드 및 연결된 장비 버스 초기화 완료."
        except Exception as e:
            return f"FAIL: LoadSignals Error - {e}"

    def SendSignal(self, signal_name: str, physical_value) -> str:
        """
        신호명(Signal Name)과 물리값을 입력받아 CAN/CAN-FD 버스로 패킷을 조립 후 전송합니다.
        """
        if not self._canfd:
            return "FAIL: UDP_CANFD 모듈을 찾을 수 없습니다."
        try:
            # 변환 및 전송을 기존 모듈(UDP_CANFD)로 위임합니다.
            self._canfd.SEND_CANEthernetData(signal_name, physical_value)
            return f"OK: SendSignal [{signal_name} = {physical_value}] 전송 완료."
        except Exception as e:
            return f"FAIL: SendSignal Error - {e}"

    def DoorTest(self) -> str:
        """
        운전석 도어 스위치 신호 매크로 테스트를 실행합니다.
        """
        if not self._canfd:
            return "FAIL: UDP_CANFD 모듈을 찾을 수 없습니다."
        try:
            self._canfd.door_test()
            return "OK: DoorTest 매크로 동작 완료."
        except Exception as e:
            return f"FAIL: DoorTest Error - {e}"

    def TestAllSignals(self) -> str:
        """
        로드된 모든 신호를 순회하며 중간값을 계산해 통신 상태를 전체 테스트합니다.
        """
        if not self._canfd:
            return "FAIL: UDP_CANFD 모듈을 찾을 수 없습니다."
        try:
            self._canfd.test_all_canfd_signals()
            return "OK: TestAllSignals 부하 테스트 실행 완료."
        except Exception as e:
            return f"FAIL: TestAllSignals Error - {e}"

    # ------------------------------------------------------------------
    # 📌 신규 추가 기능: CAN 메시지 사전 수집 및 사후 검증 기능
    # ------------------------------------------------------------------

    def canmsg_start(self) -> str:
        """
        단발성 이벤트로 발생하는 CAN 메시지 유실을 막기 위해
        모든 채널의 수신 패킷을 백그라운드 버퍼에 미리 기록하기 시작합니다.
        (파라미터 없음)
        """
        logger.info("canmsg_start: 백그라운드 CAN 메시지 캡처를 시작합니다.")
        with self._capture_lock:
            self._capture_buffer.clear()
            self._is_capturing = True
        return "OK: CAN Capture Started"

    def canmsg_stop(self, mcu: str = "mcu1", channel: str = "A", msg_id: str | int = "",
                    data: str = "") -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        수집을 중단하고, 버퍼에 쌓인 내용 중 타겟 MCU, Channel, msg_id, data 패턴이 일치하는지 검증합니다.
        (기존 time 파라미터 제외됨)
        """
        logger.info(f"canmsg_stop 호출: mcu={mcu}, channel={channel}, msg_id={msg_id}, data='{data}'")
        logger.info(f"canmsg_stop captured buffer 수 : {len(self._capture_buffer)}")
        # 1. 캡처 중단 및 버퍼 복사 (멀티스레드 충돌 방지)
        with self._capture_lock:
            self._is_capturing = False
            captured_copy = list(self._capture_buffer)
            self._capture_buffer.clear()

        ch_key = str(channel).upper()

        # 2. msg_id 정수화 (0x 접두어 방어)
        try:
            target_id = int(str(msg_id).replace("0x", "").replace("0X", ""), 16) if isinstance(msg_id, str) else int(
                msg_id)
        except Exception as e:
            logger.error(f"canmsg_stop 실패: msg_id={msg_id} 파싱 오류 - {e}")
            return False, None, None, None

        # 3. 데이터 패턴 파싱 ("** ** 05" -> [None, None, 5])
        data_cleaned = str(data).replace(",", " ").strip()
        pattern = []
        for token in data_cleaned.split():
            if '*' in token:
                pattern.append(None)
            else:
                try:
                    pattern.append(int(token, 16))
                except ValueError:
                    pattern.append(None)

        # 4. 캡처된 버퍼에서 매칭 검사
        matched_details = None

        for c_mcu, c_ch, cid, payload, recv_time in captured_copy:
            # 타겟 장비, 채널, 아이디가 다르면 스킵
            if c_mcu != mcu or c_ch != ch_key or cid != target_id:
                continue

            # 수신된 데이터 길이가 패턴 길이보다 짧으면 스킵
            if len(payload) < len(pattern):
                continue

            # 바이트 단위 매칭 (와일드카드는 무시)
            is_current_match = True
            for i, pat_val in enumerate(pattern):
                if pat_val is None:
                    continue
                if payload[i] != pat_val:
                    is_current_match = False
                    break

            if is_current_match:
                matched_details = (cid, payload, recv_time)
                break  # 📌 첫 번째(가장 빨리 발생한) 일치건 발견 시 즉시 종료

        if matched_details:
            cid, payload_bytes, recv_time = matched_details
            payload_hex = " ".join(f"{b:02X}" for b in payload_bytes)
            logger.info(f"🎉 canmsg_stop 성공: 일치 메시지 발견! ID=0x{cid:X}, 시간={recv_time}, Payload=[{payload_hex}]")
            return True, recv_time, f"0x{cid:X}", payload_hex

        logger.info(f"❌ canmsg_stop 실패: 버퍼에 {len(captured_copy)}개의 패킷이 캡처되었으나 일치하는 패턴이 없습니다.")
        return False, None, None, None

    def canmsg(self, mcu: str = "mcu1", channel: str = "A", time: int = 1000, msg_id: str | int = "",
               data: str = "") -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
        """
        [Deprecated — 하위호환 유지용] 기존 저장된 시나리오의 canmsg 스텝을 위해 남겨둔 래퍼입니다.
        신규 시나리오는 canmsg_start(제어 명령 전) → canmsg_stop(검증) 분리 사용을 권장합니다.

        내부적으로 신규 캡처 버퍼(canmsg_start/stop)를 사용하므로 호출 시점에 진행 중이던
        별도 canmsg_start 캡처 세션은 초기화됩니다.
        """
        logger.info(f"canmsg(호환) 호출됨: mcu={mcu}, channel={channel}, time={time}ms, msg_id={msg_id}, data='{data}'")

        # 1. 대상 MCU 및 채널 존재 검증
        conf = self._mcu_configs.get(mcu)
        if not conf:
            logger.error(f"canmsg 실패: 정의되지 않은 MCU [{mcu}]")
            return False, None, None, None

        ch_key = str(channel).upper()
        ch_conf = conf["channels"].get(ch_key)
        if not ch_conf:
            logger.error(f"canmsg 실패: 정의되지 않은 채널 [{channel}] (MCU: {mcu})")
            return False, None, None, None

        # 2. 연결 및 수신 포트 오픈 확인 (예외 방어)
        if mcu not in self._connected_mcus:
            logger.info(f"[{mcu}] 장비가 연결 상태가 아닙니다. 강제 자동 연결을 시작합니다.")
            self.Connect()
            if mcu not in self._connected_mcus:
                logger.error(f"canmsg 실패: [{mcu}] 장비 연결에 실패했습니다.")
                return False, None, None, None

        port = ch_conf["rx_port"]
        if port not in self._rx_sockets:
            logger.info(f"[{mcu}-{ch_key}] 수신 스레드가 미작동 상태입니다. 리스너를 실행합니다 (Port: {port})")
            self._start_rx_listener(mcu, port, channel_name=ch_key)

        # 3. 신규 캡처 구조로 지정 시간(ms) 동안 수집 후 검증
        self.canmsg_start()
        import time as _time  # 파라미터 time과의 충돌 및 Shadowing 해결
        _time.sleep(max(0.0, float(time) / 1000.0))
        return self.canmsg_stop(mcu=mcu, channel=ch_key, msg_id=msg_id, data=data)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)

    tool = WoohyunBench()

    print("\n[Step 1] 장비 접속")
    print(tool.Connect())
    time.sleep(5)

    print('장비 정보 출력')
    print(tool.GetInfo())
    # time.sleep(1)
    # print(tool.BATTERY(0))
    # time.sleep(1)
    # print(tool.ACC(0))
    # time.sleep(1)
    # print(tool.IGN1(0))
    # time.sleep(5)
    # print(tool.BATTERY(1))
    # time.sleep(1)
    # print(tool.ACC(1))
    # time.sleep(1)
    # print(tool.IGN1(1))
    # print('장비 연결 확인')
    print(tool.IsConnected())
    volts = tool.BatteryCheck("mcu1")
    amps = tool.AmpereCheck("mcu1")
    ign1_status = tool.IGN1_Read("mcu1")

    print(f"배터리: {volts}V, 전류: {amps}A, IGN1 상태: {ign1_status}")
    time.sleep(5)

    print("\n[Step 2] 로깅 시작 (단일 파일)")
    print(tool.CanSaveStart(save_dir=r"C:\ReplayKit\canlog"))
    time.sleep(1)
    #
    # print("\n[Step 3] CAN 데이터 전송")
    #
    # print(tool.SendCan(msg_id="477", type="STA", payload_hex="00,00,00,00,00,C0,00,00",channel="A",  repeat=1, cycle_ms=100))
    # print(tool.SendCan(msg_id="47A", type="STA", payload_hex="00,00,70,00,00,00,00,00", channel="A", repeat=1, cycle_ms=100))
    # print(tool.SendCan(msg_id="479", type="STA", payload_hex="00,00,00,00,00,02,00,00", channel="A", repeat=1, cycle_ms=100))

    time.sleep(5)
    print("\n[Step 4] 전체 로깅 중지 및 연결 해제")
    print(tool.CanSaveStop())
    print(tool.Disconnect())
    print("✅ 장비 연결 해제 완료")