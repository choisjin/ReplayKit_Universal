import datetime
import os
import glob
import time
import json
import ast
import threading

from can import Message, Logger, Notifier, broadcastmanager
from can.interfaces.vector import VectorBus
from robot.api.deco import keyword
from isotp import Address, NotifierBasedCanStack, AddressingMode, BlockingSendFailure

broadcastmanager.USE_WINDOWS_EVENTS = False  # For Periodic msg

# 벡터(CANoe_Ctrl) 연결의 기본 비트레이트. 사용자가 연결 시 콤보박스로
# bitrate/data_bitrate 를 선택할 수 있으며, 미지정 시 이 기본값을 사용한다.
DEFAULT_NOM_BITRATE = 500_000
DEFAULT_DATA_BITRATE = 2_000_000

# CAN FD 타이밍 프로필 — Vector 하드웨어에 정확한 tseg/sjw 값을 전달한다.
# from_sample_point 로 계산하면 Vector 드라이버가 내부 f_clock(40MHz)로 변환하면서
# tseg 값이 달라져(예: 500k/2M → tseg1Dbr=15, tseg2Dbr=4) 사용자가 원하는
# 정확한 타이밍(FD_M: tseg1Dbr=14, tseg2Dbr=5 / FD_E: tseg1Dbr=13, tseg2Dbr=6)과
# 어긋난다. 프로필을 선택하면 이 정확한 값을 그대로 VectorBus 에 전달한다.
# f_clock=80MHz 기준 brp 계산:
#   FD_M nom: 80M/(500k*80) = 2, data: 80M/(1M*20) = 4
#   FD_E nom: 80M/(500k*10) = 16, data: 80M/(2M*20) = 2
CANOE_TIMING_PROFILES = {
    "FD_M": {
        "label": "FD_M (500k/1M)",
        "bitrate": 500_000,
        "data_bitrate": 1_000_000,
        "timing": dict(
            f_clock=80_000_000,
            nom_brp=2, nom_tseg1=63, nom_tseg2=16, nom_sjw=16,
            data_brp=4, data_tseg1=14, data_tseg2=5, data_sjw=4,
        ),
    },
    "FD_E": {
        "label": "FD_E (500k/2M)",
        "bitrate": 500_000,
        "data_bitrate": 2_000_000,
        "timing": dict(
            f_clock=80_000_000,
            nom_brp=16, nom_tseg1=7, nom_tseg2=2, nom_sjw=2,
            data_brp=2, data_tseg1=13, data_tseg2=6, data_sjw=1,
        ),
    },
}


def _coerce_value(v):
    """UI는 모든 값을 string으로 보냄 → 적절한 Python 타입으로 변환.

    - "True"/"False" → bool
    - "None"/"" → None
    - 숫자 문자열 → int
    - 그 외 → 원본 유지
    """
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "" or s.lower() == "none" or s.lower() == "null":
        return None
    if s.lower() == "true":
        return True
    if s.lower() == "false":
        return False
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return v


def _normalize_device_info(device_info):
    """device_info를 list[dict] 형태로 정규화.

    허용되는 입력 형식:
      1. list[dict] — 그대로 사용
      2. JSON 문자열 — json.loads
      3. Python literal 문자열 (단일 따옴표 포함) — ast.literal_eval
      4. dict 단일 — list로 wrap
    각 dict의 값은 _coerce_value로 타입 변환.
    """
    if device_info is None or device_info == "":
        raise ValueError("device_info is empty")

    parsed = device_info
    if isinstance(parsed, str):
        s = parsed.strip()
        try:
            parsed = json.loads(s)
        except (ValueError, TypeError):
            parsed = ast.literal_eval(s)

    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        raise ValueError(f"device_info must be list[dict], got {type(parsed).__name__}")

    result = []
    for item in parsed:
        if not isinstance(item, dict):
            raise ValueError(f"device_info item must be dict, got {type(item).__name__}")
        result.append({k: _coerce_value(v) for k, v in item.items()})
    return result


class E2E_Helper:
    """Vector E2E CRC16 테이블 빌드 및 알고리즘 헬퍼 클래스"""
    CRC16table = []

    @classmethod
    def init_crc_table(cls):
        if cls.CRC16table:
            return
        # Standard CRC16-CCITT polynomial: 0x1021
        polynomial = 0x1021
        for i in range(256):
            crc = i << 8
            for _ in range(8):
                if crc & 0x8000:
                    crc = (crc << 1) ^ polynomial
                else:
                    crc = crc << 1
                crc &= 0xFFFF
            cls.CRC16table.append(crc)

    @staticmethod
    def normalize_msg_id(msg_id_str):
        """메시지 ID 형식을 표준화 (예: '0x0A0' -> 'A0', 'a0' -> 'A0')"""
        if not isinstance(msg_id_str, str):
            msg_id_str = f"{msg_id_str:X}"
        temp = msg_id_str.upper().strip()
        if temp.startswith("0X"):
            temp = temp[2:]
        return temp.lstrip("0") if temp.lstrip("0") else "0"

    @staticmethod
    def safe_int(value):
        """10진수 숫자형 문자열 혹은 '0xF800' 형식의 16진수 문자열을 안전하게 int형으로 변환"""
        if isinstance(value, str):
            if value.lower().startswith("0x"):
                return int(value, 16)
            return int(value)
        return int(value)


class E2E_PeriodicTask:
    """JSON 설정을 기반으로 실시간 Alive Counter 및 CRC16을 동적 계산하여 주기 송신하는 고정밀 스레드"""

    def __init__(self, bus, message, period, msg_id, config):
        self.bus = bus
        self.message = message
        self.period = period
        self.msg_id = msg_id
        self.config = config

        # JSON 사양 동적 로딩 및 변환
        self.counter_idx = E2E_Helper.safe_int(config.get("counter_index", 2))
        self.counter_mask = E2E_Helper.safe_int(config.get("counter_mask", 0xFF))
        self.crc_start_idx = E2E_Helper.safe_int(config.get("crc_start_index", 2))
        self.crc_low_idx = E2E_Helper.safe_int(config.get("crc_low_index", 0))
        self.crc_high_idx = E2E_Helper.safe_int(config.get("crc_high_index", 1))
        self.data_id_offset = E2E_Helper.safe_int(config.get("data_id_offset", 0xF800))

        data_list = list(message.data)
        self.alive_counter = data_list[self.counter_idx] if len(data_list) > self.counter_idx else 0
        self.running = False
        self.thread = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join(timeout=0.5)

    def _run(self):
        CRC16table = E2E_Helper.CRC16table
        u16msgID = self.msg_id

        # Windows 주기 편차 제거를 위한 고정밀 타이머 초기화
        next_tx_time = time.perf_counter()

        while self.running:
            data_list = list(self.message.data)
            dlc = len(data_list)

            # 설정된 인덱스 범위 초과 에러 방지 안전장치
            max_index_required = max(self.counter_idx, self.crc_start_idx, self.crc_low_idx, self.crc_high_idx)
            if dlc > max_index_required:
                # 1. Alive Counter 업데이트 (동적 마스크 크기 지원)
                self.alive_counter = (self.alive_counter + 1) & self.counter_mask
                data_list[self.counter_idx] = self.alive_counter

                # 2. 동적 영역 CRC16 계산
                param_crc = 0xFFFF
                for crcIndex in range(self.crc_start_idx, dlc):
                    param_crc = (param_crc << 8) ^ CRC16table[(param_crc >> 8) ^ data_list[crcIndex]]
                    param_crc &= 0xFFFF

                # 2-1. ID Low Byte 반영
                param_crc = (param_crc << 8) ^ CRC16table[(param_crc >> 8) ^ (u16msgID & 0xFF)]
                param_crc &= 0xFFFF

                # 2-2. ID + DATA_ID_INIT High Byte 반영
                high_byte = ((u16msgID + self.data_id_offset) >> 8) & 0xFF
                param_crc = (param_crc << 8) ^ CRC16table[(param_crc >> 8) ^ high_byte]
                param_crc &= 0xFFFF

                # 3. 계산된 CRC 값을 동적 위치에 분할 저장
                data_list[self.crc_low_idx] = param_crc & 0xFF
                data_list[self.crc_high_idx] = (param_crc >> 8) & 0xFF

                self.message.data = bytes(data_list)

            # CAN 메시지 송신
            try:
                self.bus.send(self.message)
            except Exception as e:
                print(f"E2E Send Error: {e}")

            # [고정밀 튜닝] Windows OS 오차 방지 하이브리드 슬립 알고리즘 적용 (10ms 정밀 유지)
            next_tx_time += self.period
            while True:
                current_time = time.perf_counter()
                remaining = next_tx_time - current_time
                if remaining <= 0:
                    break
                if remaining > 0.002:
                    time.sleep(0.001)


class CANoe_Ctrl:
    def __init__(self, device_info):
        # CAN FD 타이밍은 각 채널(행)마다 개별적으로 설정한다.
        # Vector 장비 한 대가 여러 채널을 가지므로, 채널별 bitrate/data_bitrate 를
        # device_info 의 각 행에서 읽어 사용한다. 값이 없으면 기본값(500k/2M)을 사용한다.
        self.bus = []
        self.e2e_tasks = {}  # E2E 주기 태스크 관리 딕셔너리
        self.periodic_tasks = {}  # 일반 주기 태스크 관리 딕셔너리
        self.e2e_config = {}  # 동적 E2E 사양 딕셔너리

        E2E_Helper.init_crc_table()  # CRC Table 초기화
        self.load_e2e_config()  # 기본 e2e_config.json 파일 자동 로드 시도

        dev_dict = _normalize_device_info(device_info)
        for rp in range(0, len(dev_dict)):
            row = dev_dict[rp]
            # 스캔으로 선택된 채널이면 전역 channel_index 로 직접 오픈 (app_name/Hardware Config 무관).
            # 없으면 기존 방식(app_name + 채널 인덱스)으로 오픈.
            ci = row.get('channel_index', None)
            if ci is not None and ci != '':
                open_kwargs = dict(channel=0, channel_index=int(ci), app_name=None)
            else:
                open_kwargs = dict(channel=row['channel'], app_name=row.get('app_name', 'CANoe'))

            # 채널별 타이밍 설정: 각 행(row)의 bitrate/data_bitrate 를 사용한다.
            # 값이 없거나 비어있으면 기본값(DEFAULT_NOM_BITRATE/DEFAULT_DATA_BITRATE)을 사용한다.
            row_nom = row.get('bitrate')
            row_dat = row.get('data_bitrate')
            try:
                row_nom_bitrate = int(str(row_nom).strip()) if row_nom not in (None, '') else DEFAULT_NOM_BITRATE
            except (ValueError, TypeError):
                row_nom_bitrate = DEFAULT_NOM_BITRATE
            try:
                row_data_bitrate = int(str(row_dat).strip()) if row_dat not in (None, '') else DEFAULT_DATA_BITRATE
            except (ValueError, TypeError):
                row_data_bitrate = DEFAULT_DATA_BITRATE

            # Advanced CAN Timing: 사용자가 채널별로 sjw/tseg 값을 지정한 경우 정확한 값 사용.
            # 모든 6개 값(sjwAbr/sjwDbr/tseg1Abr/tseg1Dbr/tseg2Abr/tseg2Dbr)이 있으면
            # BitTimingFd 에 정확한 tseg/sjw/brp 를 전달한다. 없으면 from_sample_point(80%) 로 자동 계산.
            from can import BitTimingFd
            _adv_keys = ('sjwAbr', 'sjwDbr', 'tseg1Abr', 'tseg1Dbr', 'tseg2Abr', 'tseg2Dbr')
            _adv_vals = {}
            for _k in _adv_keys:
                _v = row.get(_k)
                try:
                    _adv_vals[_k] = int(str(_v).strip()) if _v not in (None, '') else None
                except (ValueError, TypeError):
                    _adv_vals[_k] = None
            if all(_adv_vals.get(k) is not None for k in _adv_keys):
                # brp = f_clock / (bitrate * (1 + tseg1 + tseg2))
                _nom_brp = int(round(80_000_000 / (row_nom_bitrate * (1 + _adv_vals['tseg1Abr'] + _adv_vals['tseg2Abr']))))
                _data_brp = int(round(80_000_000 / (row_data_bitrate * (1 + _adv_vals['tseg1Dbr'] + _adv_vals['tseg2Dbr']))))
                _timing = BitTimingFd(
                    f_clock=80_000_000,
                    nom_brp=_nom_brp, nom_tseg1=_adv_vals['tseg1Abr'], nom_tseg2=_adv_vals['tseg2Abr'], nom_sjw=_adv_vals['sjwAbr'],
                    data_brp=_data_brp, data_tseg1=_adv_vals['tseg1Dbr'], data_tseg2=_adv_vals['tseg2Dbr'], data_sjw=_adv_vals['sjwDbr'],
                )
            else:
                # 벡터 연결은 CAN FD 로 연다. nominal/data bitrate 는 채널별 설정값을 사용한다.
                # data_bitrate 미지정 시 nominal 로 폴백돼 FD 데이터 페이즈가 어긋나던 문제
                # (DUT 미-ACK → Vector TX 큐 XL_ERR_QUEUE_IS_FULL → 주기 송신 스레드 사망) 원천 차단.
                _timing = BitTimingFd.from_sample_point(
                    f_clock=80_000_000,
                    nom_bitrate=row_nom_bitrate, nom_sample_point=80.0,
                    data_bitrate=row_data_bitrate, data_sample_point=80.0,
                )
            self.bus.append(VectorBus(**open_kwargs, timing=_timing,
                                      rx_queue_size=2 ** 16, receive_own_messages=True))
        self.CANoe_logger = None
        self.CANoe_logger_full = None
        self.CANoe_recv = None
        self.addr = None
        self.stack = None

        self.diag_ch = 0
        self.nTx_id = 0
        self.nRx_id = 0

        self.tester_present_thread = None
        self.tester_present_running = False

    @keyword('CANoe Load E2E Config')
    def load_e2e_config(self, config_path=None):
        """E2E JSON 설정 파일을 동적으로 불러오는 키워드.

        config_path 미지정 시 이 모듈과 같은 폴더의 'e2e_config.json'을 기본 사용
        (백엔드 실행 cwd 와 무관하게 항상 찾도록).
        """
        if config_path is None or config_path == "":
            config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e_config.json")

        if os.path.exists(config_path):
            try:
                with open(config_path, 'r', encoding='utf-8') as f:
                    raw_config = json.load(f)
                    # ID를 대문자 표준 포맷으로 노멀라이징하여 저장
                    self.e2e_config = {E2E_Helper.normalize_msg_id(k): v for k, v in raw_config.items()}
                print(f"[E2E Config] Successfully loaded config from '{config_path}': {self.e2e_config}")
            except Exception as e:
                print(f"[E2E Config] Error loading config file: {e}")
        else:
            print(f"[E2E Config] Configuration file '{config_path}' not found.")

    @keyword('CANoe Full Log Save Start')
    def canoe_full_log_save_start(self, path, file_name):
        file_name_SD = file_name.split('.')
        print('canoe_full_log_save_start ',path)
        print('canoe_full_log_save_start ',file_name_SD[0])
        print('canoe_full_log_save_start ',file_name_SD[1])
        log_path = path + "/" + file_name_SD[0] + "_{0}.".format(datetime.datetime.now().strftime("%y%m%d_%H%M%S")) + file_name_SD[1]
        # log_path = path + file_name_SD[0] + "_{0}.".format(datetime.datetime.now().strftime("%y%m%d_%H%M%S")) + file_name_SD[1]
        print('canoe_full_log_save_start ',log_path)
        self.CANoe_logger_full = Logger(log_path)
        self.CANoe_recv = Notifier(self.bus, [self.CANoe_logger_full])
        return log_path

    @keyword('CANoe Full Log Save Stop')
    def canoe_full_log_save_stop(self):
        # Stop periodic tasks first
        for busNum in range(0, len(self.bus)):
            try:
                self.bus[busNum].stop_all_periodic_tasks()
            except:
                pass

        # 일반 주기 태스크 전체 정지 및 메모리 비우기
        for task in list(self.periodic_tasks.values()):
            try:
                task.stop()
            except:
                pass
        self.periodic_tasks.clear()

        # E2E 주기 태스크 전체 정지 및 메모리 비우기
        for task in list(self.e2e_tasks.values()):
            try:
                task.stop()
            except:
                pass
        self.e2e_tasks.clear()

        # Stop notifier BEFORE logger to prevent writing to closed file
        if self.CANoe_recv:
            try:
                self.CANoe_recv.stop()
            except:
                pass

        # Now safe to stop logger
        if self.CANoe_logger_full:
            try:
                self.CANoe_logger_full.stop()
                self.CANoe_logger_full = None
            except:
                pass

    @keyword('CANoe Log Save Start')
    def canoe_log_save_start(self, path, file_name):
        file_name_SD = file_name.split('.')
        log_path = path + file_name_SD[0] + "_{0}.".format(datetime.datetime.now().strftime("%y%m%d_%H%M%S")) + \
                   file_name_SD[1]
        self.CANoe_logger = Logger(log_path)
        self.CANoe_recv.add_listener(self.CANoe_logger)

    @keyword('CANoe Log Save Stop')
    def canoe_log_save_stop(self):
        if self.CANoe_logger:
            try:
                # Remove from notifier first
                if self.CANoe_recv:
                    self.CANoe_recv.remove_listener(self.CANoe_logger)
                time.sleep(0.1)
                # Then stop logger
                self.CANoe_logger.stop()
                self.CANoe_logger = None
            except:
                pass

    @keyword('CANoe Send Message')
    def canoe_send_message(self, message_id, cycle_time, can_message, bus_channel, message_type='FD', apply_e2e=False):
        """
        메시지 송신 키워드
        :param apply_e2e: True인 경우에만 E2E(Alive Counter + CRC16) 적용 주기 송신하며,
                          False이거나 지정하지 않은 경우 일반 전송 (기본값: False)
        """
        _message_id = int(message_id, 16)
        _bus_ch = int(bus_channel)

        if message_type == 'FD':
            _is_fd = True
        else:
            _is_fd = False

        if _message_id <= 0x7FF:
            _is_extended_id = False
        else:
            _is_extended_id = True

        _can_message = [int(n, 16) for n in can_message.split()]
        _cycle_time = int(cycle_time)

        # Robot Framework 문자열 호환 대응 ('True', 'YES', '1' 등 판별하여 Boolean화)
        if isinstance(apply_e2e, str):
            if apply_e2e.upper() in ["TRUE", "YES", "1"]:
                should_apply_e2e = True
            else:
                should_apply_e2e = False
        else:
            should_apply_e2e = bool(apply_e2e)

        task_key = (_bus_ch, _message_id)

        if _cycle_time > 0:
            if should_apply_e2e:
                # 기존 일반 주기 태스크가 돌고 있다면 정지 후 제거
                if task_key in self.periodic_tasks:
                    try:
                        self.periodic_tasks[task_key].stop()
                    except:
                        pass
                    del self.periodic_tasks[task_key]

                # E2E 설정 매핑 정보 추출 (만약 JSON에 설정을 깜빡했을 시 기본 템플릿 적용)
                norm_id = E2E_Helper.normalize_msg_id(message_id)
                config_data = self.e2e_config.get(norm_id, {
                    "counter_index": 2, "counter_mask": "0xFF", "crc_start_index": 2,
                    "crc_low_index": 0, "crc_high_index": 1, "data_id_offset": "0xF800"
                })

                print(f'periodic E2E Dynamic send ({message_id}) config : {config_data}')

                if task_key in self.e2e_tasks:
                    self.e2e_tasks[task_key].stop()

                msg = Message(arbitration_id=_message_id,
                              data=_can_message,
                              is_fd=_is_fd,
                              dlc=len(_can_message),
                              is_extended_id=_is_extended_id,
                              is_rx=False)

                task = E2E_PeriodicTask(self.bus[_bus_ch], msg, _cycle_time / 1000.0, _message_id, config_data)
                self.e2e_tasks[task_key] = task
                task.start()
            else:
                # 기존 E2E 태스크가 돌고 있다면 정지 후 제거
                if task_key in self.e2e_tasks:
                    try:
                        self.e2e_tasks[task_key].stop()
                    except:
                        pass
                    del self.e2e_tasks[task_key]

                # 기존 일반 주기 태스크가 이미 등록되어 있다면 정지
                if task_key in self.periodic_tasks:
                    try:
                        self.periodic_tasks[task_key].stop()
                    except:
                        pass

                # 일반 주기 메시지 송신 및 태스크 객체 저장 (E2E 미적용)
                msg = Message(arbitration_id=_message_id,
                              data=_can_message,
                              is_fd=_is_fd,
                              dlc=len(_can_message),
                              is_extended_id=_is_extended_id,
                              is_rx=False)

                # send_periodic은 정지 제어가 가능한 CyclicSendTask 객체를 리턴합니다.
                task = self.bus[_bus_ch].send_periodic(msg, _cycle_time / 1000.0)
                self.periodic_tasks[task_key] = task
        else:
            # 단일 샷(1회성) 전송
            self.bus[_bus_ch].send(
                Message(arbitration_id=_message_id,
                        data=_can_message,
                        is_fd=_is_fd,
                        dlc=len(_can_message),
                        is_extended_id=_is_extended_id,
                        is_rx=False))

    @keyword('CANoe Send Message Stop')
    def canoe_send_message_stop(self, message_id, bus_channel):
        """
        특정 단일 메시지의 주기적 송신을 중단하는 키워드 (E2E 및 일반 주기 메시지 모두 지원)
        :param message_id: 중단할 메시지 ID (예: '0xA0' 또는 '0x411')
        :param bus_channel: 버스 채널 인덱스 (예: '0' 또는 0)
        """
        _message_id = int(message_id, 16)
        _bus_ch = int(bus_channel)
        task_key = (_bus_ch, _message_id)
        stopped = False

        # 1. E2E 태스크 정지 시도
        if task_key in self.e2e_tasks:
            try:
                self.e2e_tasks[task_key].stop()
                del self.e2e_tasks[task_key]
                print(f"[Stop] Stopped E2E periodic message 0x{_message_id:X} on channel {_bus_ch}")
                stopped = True
            except Exception as e:
                print(f"[Stop] Error stopping E2E task for 0x{_message_id:X}: {e}")

        # 2. 일반 주기 태스크 정지 시도
        if task_key in self.periodic_tasks:
            try:
                self.periodic_tasks[task_key].stop()
                del self.periodic_tasks[task_key]
                print(f"[Stop] Stopped normal periodic message 0x{_message_id:X} on channel {_bus_ch}")
                stopped = True
            except Exception as e:
                print(f"[Stop] Error stopping normal task for 0x{_message_id:X}: {e}")

        if not stopped:
            print(f"[Stop] No active periodic message found for 0x{_message_id:X} on channel {_bus_ch}")

    @keyword('CANoe Send Message All Stop')
    def canoe_send_msg_all_stop(self, bus_channel):
        _bus_ch = int(bus_channel)
        self.bus[_bus_ch].stop_all_periodic_tasks()

        # 채널에 해당되는 일반 주기 태스크 정지 및 제거
        keys_to_remove_norm = []
        for key, task in self.periodic_tasks.items():
            if key[0] == _bus_ch:
                try:
                    task.stop()
                except:
                    pass
                keys_to_remove_norm.append(key)
        for key in keys_to_remove_norm:
            del self.periodic_tasks[key]

        # 채널에 해당되는 E2E 주기 태스크 정지 및 제거
        keys_to_remove_e2e = []
        for key, task in self.e2e_tasks.items():
            if key[0] == _bus_ch:
                try:
                    task.stop()
                except:
                    pass
                keys_to_remove_e2e.append(key)
        for key in keys_to_remove_e2e:
            del self.e2e_tasks[key]

    @keyword('Find CAN Data From Latest Log')
    def find_msg_from_latest_log(self, log_file, message_id, can_message):
        msg_id = message_id.replace("0x", "").upper()
        data_tokens = can_message.upper().split()

        with open(log_file, 'r', errors='ignore') as f:
            for line in f:
                line_u = line.upper()

                # ID 체크
                if f" {msg_id} " not in line_u:
                    continue

                # DATA 토큰 전부 포함되는지 확인
                if all(tok in line_u for tok in data_tokens):
                    return "pass", line.strip()

        return "fail", ""

    # @keyword('Find CAN Data From Latest Log')
    # def find_msg_from_latest_log(self, log_path, message_id, can_message):
    #     find_data = ""
    #     list_of_files = glob.glob(log_path + '*')
    #     latest_file = max(list_of_files, key=os.path.getmtime)
    #
    #     with open(latest_file) as log_file:
    #         datafile = log_file.readlines()
    #     for line in datafile:
    #         if message_id[2:] in line and can_message in line:
    #             find_data = line.replace('\n', '')
    #
    #     log_file.close()
    #
    #     if find_data != "":
    #         find_result = "pass"
    #     else:
    #         find_result = "fail"
    #
    #     return find_result, find_data

    @keyword('CANoe Set Diag Env')
    def canoe_set_diag_env(self, bus_ch, tx_id, rx_id, can_type='CC'):

        self.diag_ch = int(bus_ch)
        self.nTx_id = int(tx_id, 16)
        self.nRx_id = int(rx_id, 16)

        self.addr = Address(AddressingMode.Normal_29bits, txid=self.nTx_id, rxid=self.nRx_id)

        params = {
            'stmin': 0,
            'blocksize': 0,
            'rx_flowcontrol_timeout': 1000,
            'rx_consecutive_frame_timeout': 1000,
            'tx_padding': 0x55,
            'blocking_send': False
        }

        if can_type == 'FD':
            params['tx_data_length'] = 64
            params['can_fd'] = True
            params['bitrate_switch'] = True
        else:
            params['tx_data_length'] = 8
            params['can_fd'] = False
            params['bitrate_switch'] = False

        self.stack = NotifierBasedCanStack(
            bus=self.bus[self.diag_ch],
            notifier=self.CANoe_recv,
            address=self.addr,
            params=params
        )

        self.stack.start()

    @keyword('CANoe Start Tester Present')
    def canoe_start_tester_present(self, interval=2.0):
        """
        Start sending periodic Tester Present (3E 00) messages

        Args:
            interval: Interval in seconds between messages (default: 2.0)
        """
        if self.tester_present_running:
            print("Tester Present already running")
            return

        interval_sec = float(interval)
        self.tester_present_running = True

        def _send_tester_present():
            while self.tester_present_running:
                try:
                    self.canoe_send_diag_msg("3E 00")
                    time.sleep(interval_sec)
                except Exception as e:
                    print(f"Tester Present error: {e}")
                    break

        self.tester_present_thread = threading.Thread(target=_send_tester_present, daemon=True)
        self.tester_present_thread.start()
        print(f"Tester Present started (interval: {interval_sec}s)")

    @keyword('CANoe Stop Tester Present')
    def canoe_stop_tester_present(self):
        """
        Stop sending periodic Tester Present messages
        """
        if not self.tester_present_running:
            print("Tester Present not running")
            return

        self.tester_present_running = False
        if self.tester_present_thread:
            self.tester_present_thread.join(timeout=3)
        print("Tester Present stopped")

    @keyword('CANoe Send Diag Message')
    def canoe_send_diag_msg(self, can_message, timeout="5", wait_response=True):
        """
        Send TP (Transport Protocol) data using ISO-TP stack and check response

        Args:
            can_message: Data to send (hex string with spaces, e.g., '22 F1 90 01 02 03 ...')
            wait_response: Wait for response and check PRC/NRC (default: True)
            timeout: Timeout for waiting response in seconds (default: 5.0)

        Returns:
            dict with 'status': 'PRC'/'NRC'/'TIMEOUT'/'FAIL', 'response': hex string, 'service_id': hex
        """
        f_timeout = float(timeout)

        # Remove spaces and convert to bytes
        tp_data_clean = can_message.replace(' ', '')
        _can_message = bytes.fromhex(tp_data_clean)

        # Get service ID from request
        service_id = _can_message[0] if len(_can_message) > 0 else 0x00

        try:
            self.stack.send(_can_message, send_timeout=f_timeout)
            print(f"TX: {can_message} ({len(_can_message)} bytes)")

            if not wait_response:
                return {'status': 'SENT', 'response': '', 'service_id': f'{service_id:02X}'}

            # Wait for response
            start_time = time.time()
            pending_count = 0
            max_pending_time = f_timeout * 3  # Extend timeout for pending responses

            while time.time() - start_time < max_pending_time:
                try:
                    remaining_time = max_pending_time - (time.time() - start_time)
                    response = self.stack.recv(timeout=min(0.5, remaining_time))
                    if response:
                        response_hex = ' '.join(f'{b:02X}' for b in response)
                        print(f"RX: {response_hex}")

                        if len(response) > 0:
                            # Check if it's a Negative Response (7F)
                            if response[0] == 0x7F:
                                if len(response) >= 3:
                                    requested_sid = response[1]
                                    nrc_code = response[2]

                                    # Check for Response Pending (0x78)
                                    if nrc_code == 0x78:
                                        pending_count += 1
                                        print(f"NRC 0x78 (Response Pending) - waiting... (count: {pending_count})")
                                        # Continue waiting for final response
                                        continue
                                    else:
                                        print(f"NRC: Service 0x{requested_sid:02X}, Code 0x{nrc_code:02X}")
                                        return {
                                            'status': 'NRC',
                                            'response': response_hex,
                                            'service_id': f'{requested_sid:02X}',
                                            'nrc_code': f'{nrc_code:02X}',
                                            'pending_count': pending_count
                                        }
                            # Check if it's a Positive Response (Service ID + 0x40)
                            elif response[0] == (service_id + 0x40):
                                print(f"PRC: Service 0x{service_id:02X}")
                                result = {
                                    'status': 'PRC',
                                    'response': response_hex,
                                    'service_id': f'{service_id:02X}'
                                }
                                if pending_count > 0:
                                    result['pending_count'] = pending_count
                                    print(f"Received after {pending_count} pending response(s)")
                                return result
                            else:
                                print(f"Unexpected response: {response_hex}")
                                return {
                                    'status': 'UNEXPECTED',
                                    'response': response_hex,
                                    'service_id': f'{service_id:02X}',
                                    'pending_count': pending_count
                                }
                except Exception as e:
                    continue

            print(f"Response timeout ({max_pending_time}s, pending count: {pending_count})")
            return {
                'status': 'TIMEOUT',
                'response': '',
                'service_id': f'{service_id:02X}',
                'pending_count': pending_count
            }

        except BlockingSendFailure as e:
            print(f"TP transmission failed: {e}")
            return {
                'status': 'FAIL',
                'response': str(e),
                'service_id': f'{service_id:02X}'
            }
        except Exception as e:
            print(f"TP transmission error: {e}")
            return {
                'status': 'FAIL',
                'response': str(e),
                'service_id': f'{service_id:02X}'
            }

    def __del__(self):
        try:
            # Stop tester present thread first
            if self.tester_present_running:
                self.stop_tester_present()

            # Stop all periodic tasks
            for busNum in range(0, len(self.bus)):
                try:
                    self.bus[busNum].stop_all_periodic_tasks()
                except:
                    pass

            if hasattr(self, 'periodic_tasks'):
                for task in list(self.periodic_tasks.values()):
                    try:
                        task.stop()
                    except:
                        pass
                self.periodic_tasks.clear()

            if hasattr(self, 'e2e_tasks'):
                for task in list(self.e2e_tasks.values()):
                    try:
                        task.stop()
                    except:
                        pass
                self.e2e_tasks.clear()

            # Stop isotp stack
            if self.stack:
                try:
                    self.stack.stop()
                except:
                    pass

            # Stop notifier FIRST to stop message flow
            if self.CANoe_recv:
                try:
                    self.CANoe_recv.stop()
                except:
                    pass

            # Small delay to ensure no more messages
            time.sleep(0.1)

            # Now stop loggers safely
            if self.CANoe_logger:
                try:
                    self.CANoe_logger.stop()
                except:
                    pass

            if self.CANoe_logger_full:
                try:
                    self.CANoe_logger_full.stop()
                except:
                    pass

            # Finally shutdown buses
            for busNum in range(0, len(self.bus)):
                try:
                    self.bus[busNum].shutdown()
                except:
                    pass

        except Exception as e:
            pass  # Silently ignore cleanup errors


if __name__ == '__main__':
    ch_dict = "[{'channel': 0, 'app_name': 'CANoe', 'bitrate': 500000, 'data_bitrate': None, 'is_fd': False}, {'channel': 1, 'app_name': 'CANoe', 'bitrate': 500000, 'data_bitrate': 2000000, 'is_fd': True}]"
    test = CANoe_Ctrl(ch_dict)
    file_path = r'D:\jh\workspace\canoe_test'
    log_path = test.canoe_full_log_save_start(file_path, "Auto.asc")
    time.sleep(5)
    #def canoe_send_message(self, message_id, cycle_time, can_message, bus_channel, message_type='FD'):
    print(test.canoe_send_message('0x7f2',100,'00 00 00 00 00 00 00 00',0,'noneFD'))
    time.sleep(3)
    print(test.canoe_send_message('0x288',100,'01 02 03 04 05 06 07 08',0,'noneFD'))
    time.sleep(5)
    test.canoe_full_log_save_stop()

    print(log_path)
    # def find_msg_from_latest_log(self, log_path, message_id, can_message):
    print(test.find_msg_from_latest_log(log_path, '0x7f2', '00 00 00 00 00 00 00 00'))

    # def find_msg_from_latest_log(self, log_path, message_id, can_message):
    print(test.find_msg_from_latest_log(log_path, '0x288', '01 02 03 04 05 06 07 08'))
