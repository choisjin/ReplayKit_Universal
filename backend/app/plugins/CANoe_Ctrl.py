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

# 벡터(CANoe_Ctrl) 연결은 항상 이 비트레이트로 고정 오픈한다.
# nominal 500kbps / data 2Mbps CAN FD. (다른 옵션은 현재 미사용)
FIXED_NOM_BITRATE = 500_000
FIXED_DATA_BITRATE = 2_000_000


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


class CANoe_Ctrl:
    def __init__(self, device_info):
        self.bus = []
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

            # 벡터 연결은 무조건 CAN FD, nominal 500kbps / data 2Mbps 고정으로 연다.
            # (현재 다른 비트레이트 옵션은 미사용 — device_info 의 bitrate/data_bitrate/is_fd 값은 무시)
            # data_bitrate 미지정 시 nominal 로 폴백돼 FD 데이터 페이즈가 어긋나던 문제
            # (DUT 미-ACK → Vector TX 큐 XL_ERR_QUEUE_IS_FULL → 주기 송신 스레드 사망) 원천 차단.
            from can import BitTimingFd
            _timing = BitTimingFd.from_sample_point(
                f_clock=80_000_000,
                nom_bitrate=FIXED_NOM_BITRATE, nom_sample_point=80.0,
                data_bitrate=FIXED_DATA_BITRATE, data_sample_point=80.0,
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
    def canoe_send_message(self, message_id, cycle_time, can_message, bus_channel, message_type='FD'):
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

        if _cycle_time > 0:
            self.bus[_bus_ch].send_periodic(
                Message(arbitration_id=_message_id,
                        data=_can_message,
                        is_fd=_is_fd,
                        dlc=len(_can_message),
                        is_extended_id=_is_extended_id,
                        is_rx=False), _cycle_time / 1000)
        else:
            self.bus[_bus_ch].send(
                Message(arbitration_id=_message_id,
                        data=_can_message,
                        is_fd=_is_fd,
                        dlc=len(_can_message),
                        is_extended_id=_is_extended_id,
                        is_rx=False))

    @keyword('CANoe Send Message All Stop')
    def canoe_send_msg_all_stop(self, bus_channel):
        _bus_ch = int(bus_channel)
        self.bus[_bus_ch].stop_all_periodic_tasks()

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
