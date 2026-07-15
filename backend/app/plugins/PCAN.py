"""PCAN — python-can 기반 CAN/CAN FD 송수신·로깅 플러그인 (멀티채널).

Reference/PCAN_python-can 프로토타입(app.py 의 PCAN 클래스)을 ReplayKit 모듈 규약으로 이식.

설계
----
- **인스턴스 1개 = 인터페이스 1개(여러 채널 bus 관리)**. 디바이스는 PCAN 하드웨어 하나당 1개만
  등록하고, **채널은 각 스텝 함수의 `channel` 인자로 선택**한다(예: 'PCAN_USBBUS1').
  Connect() 가 감지된 모든 채널의 bus 를 열어 self._buses[채널명] 에 담고, 없는 채널은
  스텝 실행 시 lazy open 한다. bitrate/fd 는 디바이스 공통 설정(모든 채널 동일).
- module_service 의 connect_type="can" 가 생성자에 interface/bitrate/fd 를 kwargs 로 전달한다
  (_build_connect_kwargs). __init__ 은 설정만 저장, _create_and_register 가 이어서 Connect() 를
  자동 호출한다(모든 인자 optional).
- **구조화 파라미터**. 참조의 세미콜론 문자열 파싱(`name;;0x621;..`)은 쓰지 않는다.
- **HW 미탐지여도 hard-fail 금지**. Connect 실패(감지 채널 0) 시 'ERROR' 반환 → 미연결로 남는다.
- **IsConnected()** = 열린 bus 존재 여부 (module_service._is_connected 규약: 끊긴 인스턴스 재생성).

노출 스텝(핵심 1차)
------------------
send_can, send_periodic_can, stop_all_tx, start_logging, stop_logging, can_all_stop.
전부 첫 인자 뒤에 `channel`(기본 'PCAN_USBBUS1') 을 받아 채널을 선택한다.
Connect/Disconnect/IsConnected 는 module_service.per_module_excluded 로 스텝 UI 에서 숨긴다.
ISO-TP tp_send / send_and_verify(응답검증) 는 2차.

결과 계약: 성공 "ok: ...", 실패 "FAIL: ..." (playback_service 가 "FAIL:" 접두로 스텝 실패 처리).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

import can

logger = logging.getLogger(__name__)

_DEFAULT_CHANNEL = "PCAN_USBBUS1"


# ──────────────────────────────────────────────────────────────────────────
# 파싱 헬퍼 — 스텝 파라미터는 문자열로 들어오므로 관대하게 캐스팅한다.
# ──────────────────────────────────────────────────────────────────────────
def _to_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y", "t")


def _parse_msg_id(s) -> int:
    """CAN 메시지 ID 를 int 로. 관례상 16진수('0x621' 또는 '621' 모두 0x621)."""
    s = str(s).strip()
    if not s:
        raise ValueError("msg_id 가 비어 있습니다")
    return int(s, 16)


def _parse_data(s) -> list[int]:
    """데이터 바이트 문자열('45 40 FF' 또는 '45,40,FF')을 int 리스트로."""
    s = str(s).strip()
    if not s:
        return []
    return [int(p, 16) for p in s.replace(",", " ").split()]


def _norm_channel(ch) -> str:
    return str(ch).strip() or _DEFAULT_CHANNEL


# ──────────────────────────────────────────────────────────────────────────
# CSV 로깅 리스너 — can.Notifier 에 물려 수신/송신 프레임을 파일로 기록.
#   포맷: Timestamp,Message_id,Dir,DLC,Data,channel (Reference/can_logging.py 와 동일)
# ──────────────────────────────────────────────────────────────────────────
class _CsvCanListener(can.Listener):
    _HEADER = "Timestamp,Message_id,Dir,DLC,Data,channel\n"

    def __init__(self, path: str) -> None:
        self._f = open(path, "a", encoding="utf-8")
        if self._f.tell() == 0:
            self._f.write(self._HEADER)
            self._f.flush()
        self._wlock = threading.Lock()

    def on_message_received(self, msg) -> None:
        try:
            direction = "Er" if msg.is_error_frame else ("Rx" if msg.is_rx else "Tx")
            data_str = " ".join(f"{b:02X}" for b in msg.data)
            channel = getattr(msg, "channel", "") or ""
            line = (
                f"{msg.timestamp:.6f},"
                f"0x{msg.arbitration_id:X},"
                f"{direction},"
                f"{msg.dlc},"
                f"{data_str},"
                f"{channel}\n"
            )
            with self._wlock:
                self._f.write(line)
        except Exception:  # 로깅은 best-effort — 한 프레임 실패로 노티파이어를 죽이지 않는다
            pass

    def stop(self) -> None:
        try:
            self._f.flush()
            self._f.close()
        except Exception:
            pass


class PCAN:
    def __init__(self, interface: str = "pcan", bitrate: str = "500000",
                 fd: str = "False", data_bitrate: str = "2000000",
                 channel: str = "") -> None:
        self.interface = (str(interface).strip() or "pcan")
        try:
            self.bitrate = int(str(bitrate).strip() or "500000")
        except ValueError:
            self.bitrate = 500000
        self.fd = _to_bool(fd)
        # FD 데이터 구간 bitrate (nominal=self.bitrate, data=this). classic 이면 무시.
        try:
            self.data_bitrate = int(str(data_bitrate).strip() or "2000000")
        except ValueError:
            self.data_bitrate = 2000000
        # channel 이 명시되면 그 채널만 연다(단일채널 모드). 비면 감지된 전 채널을 연다.
        self.channel = str(channel).strip()

        self._buses: dict[str, object] = {}          # channel_name -> can.Bus
        self._notifier: Optional[can.Notifier] = None
        self._log_listener: Optional[_CsvCanListener] = None
        self._log_path: Optional[str] = None
        self._periodic: dict[tuple, object] = {}      # (channel, arbitration_id) -> CyclicSendTask
        self._last_tx_error: Optional[str] = None     # 마지막 주기 송신 스레드 에러 (check_status 진단용)
        self._lock = threading.RLock()

    # ── 연결 수명주기 (스텝 UI 에서는 숨김) ─────────────────────────────
    def Connect(self):
        """감지된 PCAN 채널(또는 명시된 단일 채널)의 bus 를 연다. 최소 1개 성공 시 'OK'."""
        with self._lock:
            if self._buses:
                return "OK"
            if self.channel:
                targets = [self.channel]
            else:
                try:
                    cfgs = can.detect_available_configs(interfaces=self.interface)
                    targets = [str(c.get("channel")) for c in cfgs if c.get("channel")]
                except Exception as e:
                    logger.warning("PCAN detect_available_configs failed: %s", e)
                    targets = []
            opened = []
            for ch in targets:
                try:
                    self._buses[ch] = self._open_bus(ch)
                    opened.append(ch)
                except Exception as e:
                    logger.error("PCAN open channel %s failed: %s", ch, e)
            if not self._buses:
                logger.error("PCAN Connect: no channel opened (interface=%s)", self.interface)
                return "ERROR"
            logger.info("PCAN connected: interface=%s bitrate=%d fd=%s channels=%s",
                        self.interface, self.bitrate, self.fd, opened)
            return "OK"

    def IsConnected(self) -> bool:
        return bool(self._buses)

    def Disconnect(self):
        """주기 송신·로깅을 정리하고 모든 채널 bus 를 shutdown."""
        with self._lock:
            self._stop_all_tx_locked()
            self._stop_logging_locked()
            for bus in self._buses.values():
                try:
                    bus.shutdown()
                except Exception:
                    pass
            self._buses.clear()
            return "OK"

    # ── 송신 ────────────────────────────────────────────────────────────
    def send_can(self, msg_id, data, channel: str = _DEFAULT_CHANNEL,
                 is_extended: str = "False", is_fd: str = "False", brs: str = ""):
        """지정 채널로 단발 CAN/CAN FD 프레임 1개 송신.

        channel: 대상 채널명 (예: 'PCAN_USBBUS1', 'PCAN_USBBUS2').
        msg_id: 16진수 ID ('0x621' 또는 '621'). data: 공백/콤마 구분 16진수 바이트.
        brs: FD Bit Rate Switch. 빈값이면 FD 프레임에서 자동 켜짐(참조 pcan_fd_test_sender 와 동일 —
             500k/2M 자동차 버스는 BRS 프레임을 기대). classic 프레임에서는 항상 꺼짐.
        """
        with self._lock:
            ch = _norm_channel(channel)
            bus = self._get_bus(ch)
            if bus is None:
                return f"FAIL: PCAN channel not available: {ch}"
            try:
                mid = _parse_msg_id(msg_id)
                payload = _parse_data(data)
                fd_flag = _to_bool(is_fd) or self.fd
                brs_flag = fd_flag if str(brs).strip() == "" else (_to_bool(brs) and fd_flag)
                msg = can.Message(
                    arbitration_id=mid,
                    data=payload,
                    is_extended_id=_to_bool(is_extended),
                    is_fd=fd_flag,
                    bitrate_switch=brs_flag,
                )
                bus.send(msg)
                return f"ok: [{ch}] sent 0x{mid:X} [{' '.join(f'{b:02X}' for b in payload)}]"
            except Exception as e:
                return f"FAIL: send error — {e}"

    def send_periodic_can(self, msg_id, data, channel: str = _DEFAULT_CHANNEL,
                          period_ms: str = "1000", is_extended: str = "False",
                          is_fd: str = "False", brs: str = ""):
        """지정 채널에서 주기(cyclic) 송신 시작/갱신. 같은 채널·ID 재호출 시 데이터만 갱신.

        stop_all_tx / can_all_stop 로 정지. period_ms: 송신 주기(밀리초).
        brs: FD Bit Rate Switch (send_can 과 동일 규약 — 빈값이면 FD 프레임에서 자동 켜짐).
        """
        with self._lock:
            ch = _norm_channel(channel)
            bus = self._get_bus(ch)
            if bus is None:
                return f"FAIL: PCAN channel not available: {ch}"
            try:
                mid = _parse_msg_id(msg_id)
                payload = _parse_data(data)
                try:
                    period_s = max(1, int(float(str(period_ms).strip() or "1000"))) / 1000.0
                except ValueError:
                    period_s = 1.0
                fd_flag = _to_bool(is_fd) or self.fd
                brs_flag = fd_flag if str(brs).strip() == "" else (_to_bool(brs) and fd_flag)
                msg = can.Message(
                    arbitration_id=mid,
                    data=payload,
                    is_extended_id=_to_bool(is_extended),
                    is_fd=fd_flag,
                    bitrate_switch=brs_flag,
                )
                # ① 즉시 1회 동기 전송으로 전송 가능 여부를 검증한다(참조 pcan_fd_test_sender 와 동일).
                #    버스 문제(채널 점유·비트레이트/FD 불일치·버스오프·큐풀)면 여기서 예외 → FAIL 로 노출.
                #    이전 구조는 주기 태스크만 만들고 "ok" 를 즉시 반환했는데, 스레드가 첫 send 에서
                #    죽어도(on_error 부재) 스텝은 PASS 로 보였다 → 송신 안 되고 로그 빔 증상의 원인.
                bus.send(msg)
                key = (ch, mid)
                existing = self._periodic.get(key)
                if existing is not None:
                    existing.modify_data(msg)
                    return f"ok: [{ch}] periodic 0x{mid:X} updated (period={period_s * 1000:.0f}ms)"
                # ② 일시적 실패에도 죽지 않는 주기 태스크(웨이크업은 ECU 가 깰 때까지 계속 송신).
                task = self._start_resilient_periodic(bus, msg, period_s)
                self._periodic[key] = task
                return f"ok: [{ch}] periodic 0x{mid:X} started (period={period_s * 1000:.0f}ms)"
            except Exception as e:
                return f"FAIL: periodic send error — {e}"

    def stop_all_tx(self):
        """모든 채널의 주기(cyclic) 송신 작업을 정지."""
        with self._lock:
            if not self._buses:
                return "FAIL: PCAN not connected"
            n = self._stop_all_tx_locked()
            return f"ok: stopped {n} periodic task(s)"

    # ── 로깅 ────────────────────────────────────────────────────────────
    def start_logging(self, csv_file: str = ""):
        """열린 모든 채널의 CAN 트래픽을 하나의 CSV 로 로깅 시작 (channel 컬럼으로 구분).

        csv_file 이 비면 재생 런 폴더(logs/) 또는 results/PCAN_Log 아래로 자동 저장한다.
        """
        with self._lock:
            if not self._buses:
                return "FAIL: PCAN not connected"
            if self._notifier is not None:
                return f"ok: logging already running → {self._log_path}"
            path = str(csv_file).strip()
            if not path:
                base = Path(__file__).resolve().parent.parent.parent / "results" / "PCAN_Log"
                base.mkdir(parents=True, exist_ok=True)
                path = str(base / f"pcan_{datetime.now():%Y%m%d_%H%M%S}.csv")
            else:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            try:
                self._log_listener = _CsvCanListener(path)
                self._notifier = can.Notifier(list(self._buses.values()), [self._log_listener])
                self._log_path = path
                return f"ok: logging started ({len(self._buses)} ch) → {path}"
            except Exception as e:
                self._stop_logging_locked()
                return f"FAIL: logging start error — {e}"

    def stop_logging(self):
        """CSV 로깅 정지 (파일 flush + close)."""
        with self._lock:
            path = self._log_path
            if self._notifier is None:
                return "ok: logging not running"
            self._stop_logging_locked()
            return f"ok: logging stopped → {path}"

    def check_status(self, channel: str = _DEFAULT_CHANNEL):
        """진단: 지정 채널 bus 의 상태(정상/에러/버스오프)와 열린 채널·주기송신 개수를 보고.

        송신이 안 먹힐 때 원인 좁히기용. state 가 ACTIVE/status_ok=True 인데도 ECU 반응이
        없으면 bitrate·채널은 정상이고 ID/데이터/웨이크업(NM) 문제일 가능성이 크다.
        반대로 ERROR/PASSIVE/버스오프면 bitrate 불일치·종단저항·배선(ACK 없음)을 의심.
        """
        with self._lock:
            ch = _norm_channel(channel)
            bus = self._buses.get(ch)
            if bus is None:
                opened = ", ".join(self._buses) or "none"
                return f"FAIL: channel not open: {ch} (opened: {opened})"
            parts = [f"[{ch}]"]
            try:
                parts.append(f"state={getattr(bus, 'state', '?')}")
            except Exception as e:
                parts.append(f"state=err({e})")
            status_ok = getattr(bus, "status_is_ok", None)
            if callable(status_ok):
                try:
                    parts.append(f"status_ok={status_ok()}")
                except Exception as e:
                    parts.append(f"status_ok=err({e})")
            parts.append(f"bitrate={self.bitrate}")
            parts.append(f"fd={self.fd}")
            parts.append(f"opened={', '.join(self._buses)}")
            parts.append(f"periodic={len(self._periodic)}")
            if self._last_tx_error:
                parts.append(f"last_tx_error={self._last_tx_error}")
            return "ok: " + " ".join(str(p) for p in parts)

    def can_all_stop(self):
        """모든 채널의 주기 송신 + 로깅을 한 번에 정지 (bus 는 유지 → 연결 상태 보존)."""
        with self._lock:
            if not self._buses:
                return "ok: PCAN not connected"
            self._stop_all_tx_locked()
            self._stop_logging_locked()
            return "ok: all tx + logging stopped"

    # ── 내부 ───────────────────────────────────────────────────────────
    def _on_periodic_error(self, exc) -> bool:
        """주기 송신 스레드에서 send 예외 발생 시 콜백. True 반환 = 태스크 유지.

        웨이크업 시퀀스는 대상 ECU 가 깰 때까지 계속 프레임을 보내야 하므로, 일시적 전송
        실패(초기 미ACK 등)로 태스크를 죽이지 않는다(참조 sender 가 예외를 잡고 루프를
        계속하는 것과 동일). 마지막 에러는 기록해 check_status 로 진단한다.
        """
        self._last_tx_error = str(exc)
        logger.warning("PCAN periodic send error (task kept alive): %s", exc)
        return True

    def _start_resilient_periodic(self, bus, msg, period_s):
        """on_error 로 태스크가 죽지 않는 주기 송신을 시작한다.

        bus.send_periodic() 은 on_error 를 노출하지 않으므로 ThreadBasedCyclicSendTask 를
        직접 구성한다. python-can 버전차로 실패하면 표준 send_periodic 으로 폴백.
        """
        try:
            from can.broadcastmanager import ThreadBasedCyclicSendTask
            lock = getattr(bus, "_lock_send_periodic", None)
            if lock is None:
                lock = threading.Lock()
                try:
                    bus._lock_send_periodic = lock
                except Exception:
                    pass
            return ThreadBasedCyclicSendTask(
                bus, lock, msg, period_s, on_error=self._on_periodic_error,
            )
        except Exception as e:
            logger.warning("PCAN resilient periodic unavailable (%s) — falling back", e)
            return bus.send_periodic(msg, period_s)

    def _open_bus(self, channel: str):
        # receive_own_messages=True: 우리가 보낸 TX 프레임도 수신 루프백으로 돌아온다 →
        # start_logging CSV 에 Tx 로 찍혀 "실제로 송신됐는지" 눈으로 확인 가능 (진단 목적).
        common: dict = {
            "interface": self.interface,
            "channel": channel,
            "receive_own_messages": True,
        }
        if not self.fd:
            return can.Bus(bitrate=self.bitrate, **common)

        # CAN FD: 참조 pcan_fd_test_sender.py 의 PEAK 권장 프리셋을 그대로 사용한다.
        # (같은 벤치에서 CANAT FD 및 참조 sender 로 검증된 값 — from_sample_point 계산은
        #  data 구간 DBRP 가 달라져(예: 2M 을 brp=2 로) 데이터 구간 동기가 어긋날 수 있어 폐기.)
        # f_clock 80MHz 기준. 표에 없는 조합만 from_sample_point 로 폴백.
        _NOM_PRESETS = {   # nom_bitrate: (nom_brp, nom_tseg1, nom_tseg2, nom_sjw)
            1000000: (2, 31, 8, 8),
            500000:  (2, 63, 16, 16),
            250000:  (4, 63, 16, 16),
            125000:  (8, 63, 16, 16),
        }
        _DATA_PRESETS = {  # data_bitrate: (data_brp, data_tseg1, data_tseg2, data_sjw)
            12000000: (1, 4, 2, 2),
            8000000:  (1, 7, 2, 2),
            5000000:  (1, 12, 3, 3),
            4000000:  (1, 15, 4, 4),
            2000000:  (1, 31, 8, 8),
            1000000:  (2, 31, 8, 8),
        }
        if self.bitrate in _NOM_PRESETS and self.data_bitrate in _DATA_PRESETS:
            nb, nt1, nt2, nsjw = _NOM_PRESETS[self.bitrate]
            db, dt1, dt2, dsjw = _DATA_PRESETS[self.data_bitrate]
            # 참조와 동일한 레거시 kwargs 스타일(f_clock_mhz + nom_*/data_*). PcanBus 가 직접 지원.
            return can.Bus(
                fd=True, f_clock_mhz=80,
                nom_brp=nb, nom_tseg1=nt1, nom_tseg2=nt2, nom_sjw=nsjw,
                data_brp=db, data_tseg1=dt1, data_tseg2=dt2, data_sjw=dsjw,
                **common,
            )

        # 폴백: 프리셋에 없는 조합 → 계산(80% 샘플포인트), 유효 클럭 높은 것부터.
        from can import BitTimingFd
        last_err: Optional[Exception] = None
        for f_clock in (80_000_000, 60_000_000, 40_000_000, 30_000_000, 24_000_000, 20_000_000):
            try:
                timing = BitTimingFd.from_sample_point(
                    f_clock=f_clock,
                    nom_bitrate=self.bitrate, nom_sample_point=80.0,
                    data_bitrate=self.data_bitrate, data_sample_point=80.0,
                )
                return can.Bus(timing=timing, **common)
            except ValueError as e:
                last_err = e
                continue
        raise RuntimeError(
            f"FD 타이밍 계산 실패 (nominal={self.bitrate}, data={self.data_bitrate}): {last_err}"
        )

    def _get_bus(self, channel: str):
        """열린 bus 반환. 없으면 lazy open 시도(감지 못한 채널 명시 사용 대비). 실패 시 None."""
        bus = self._buses.get(channel)
        if bus is None:
            try:
                bus = self._open_bus(channel)
                self._buses[channel] = bus
            except Exception as e:
                logger.error("PCAN lazy open channel %s failed: %s", channel, e)
                return None
        return bus

    def _stop_all_tx_locked(self) -> int:
        n = len(self._periodic)
        for bus in self._buses.values():
            try:
                bus.stop_all_periodic_tasks()
            except Exception:
                pass
        self._periodic.clear()
        return n

    def _stop_logging_locked(self) -> None:
        if self._notifier is not None:
            try:
                self._notifier.stop()   # 내부적으로 listener.stop() 호출 → 파일 close
            except Exception:
                pass
            self._notifier = None
        self._log_listener = None
        self._log_path = None

    def __del__(self):
        try:
            self.Disconnect()
        except Exception:
            pass
