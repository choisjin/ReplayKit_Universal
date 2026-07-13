"""PCAN — python-can 기반 CAN/CAN FD 송수신·로깅 플러그인.

Reference/PCAN_python-can 프로토타입(app.py 의 PCAN 클래스)을 ReplayKit 모듈 규약으로 이식.

설계
----
- **인스턴스 1개 = 채널 1개(bus)**. module_service 의 connect_type="can" 가 생성자에
  interface/channel/bitrate/fd 를 kwargs 로 전달한다(_build_connect_kwargs). __init__ 은
  설정만 저장하고, _create_and_register 가 이어서 Connect() 를 자동 호출해 bus 를 연다
  (모든 인자가 optional 이라 자동 연결 대상이 된다).
- **구조화 파라미터**. 스텝은 msg_id/data/... 를 개별 인자로 받는다. 참조의 세미콜론
  문자열 파싱(`name;;0x621;0;8;45 40..;1;500;1`)은 쓰지 않는다.
- **HW 미탐지여도 hard-fail 금지**. Vector 처럼 채널을 수동 지정한다. Connect 실패 시
  'ERROR' 를 반환해 인스턴스가 미연결로 남게 한다(module_service 자동 연결 규약).
- **IsConnected()** 는 module_service._is_connected 규약에 쓰인다(끊긴 인스턴스 재생성).

노출 스텝(핵심 1차)
------------------
send_can, send_periodic_can, stop_all_tx, start_logging, stop_logging, can_all_stop.
Connect/Disconnect/IsConnected 는 module_service.per_module_excluded 로 스텝 UI 에서 숨긴다.
ISO-TP tp_send / send_and_verify(응답검증) 는 2차.

결과 계약: 성공은 "ok: ..." 문자열, 실패는 "FAIL: ..." 로 시작하는 문자열을 반환한다
(playback_service 가 not-connected/이미지비교 없을 때 "FAIL:" 접두로 스텝을 실패 처리).
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
    def __init__(self, interface: str = "pcan", channel: str = "PCAN_USBBUS1",
                 bitrate: str = "500000", fd: str = "False") -> None:
        self.interface = (str(interface).strip() or "pcan")
        self.channel = str(channel).strip()
        try:
            self.bitrate = int(str(bitrate).strip() or "500000")
        except ValueError:
            self.bitrate = 500000
        self.fd = _to_bool(fd)

        self._bus = None
        self._notifier: Optional[can.Notifier] = None
        self._log_listener: Optional[_CsvCanListener] = None
        self._log_path: Optional[str] = None
        self._periodic: dict[int, object] = {}   # arbitration_id -> CyclicSendTask
        self._lock = threading.RLock()

    # ── 연결 수명주기 (스텝 UI 에서는 숨김) ─────────────────────────────
    def Connect(self):
        """설정된 채널로 bus 를 연다. 성공 'OK', 실패 'ERROR'."""
        with self._lock:
            if self._bus is not None:
                return "OK"
            try:
                kwargs: dict = {"interface": self.interface, "bitrate": self.bitrate}
                if self.channel:
                    kwargs["channel"] = self.channel
                if self.fd:
                    kwargs["fd"] = True
                self._bus = can.Bus(**kwargs)
                logger.info("PCAN connected: interface=%s channel=%s bitrate=%d fd=%s",
                            self.interface, self.channel, self.bitrate, self.fd)
                return "OK"
            except Exception as e:
                logger.error("PCAN Connect failed (interface=%s channel=%s): %s",
                             self.interface, self.channel, e)
                self._bus = None
                return "ERROR"

    def IsConnected(self) -> bool:
        return self._bus is not None

    def Disconnect(self):
        """주기 송신·로깅을 정리하고 bus 를 shutdown."""
        with self._lock:
            self._stop_all_tx_locked()
            self._stop_logging_locked()
            if self._bus is not None:
                try:
                    self._bus.shutdown()
                except Exception:
                    pass
                self._bus = None
            return "OK"

    # ── 송신 ────────────────────────────────────────────────────────────
    def send_can(self, msg_id, data, is_extended: str = "False", is_fd: str = "False"):
        """단발 CAN/CAN FD 프레임 1개 송신.

        msg_id: 16진수 ID ('0x621' 또는 '621'). data: 공백/콤마 구분 16진수 바이트.
        """
        with self._lock:
            if self._bus is None:
                return "FAIL: PCAN not connected"
            try:
                mid = _parse_msg_id(msg_id)
                payload = _parse_data(data)
                msg = can.Message(
                    arbitration_id=mid,
                    data=payload,
                    is_extended_id=_to_bool(is_extended),
                    is_fd=_to_bool(is_fd) or self.fd,
                )
                self._bus.send(msg)
                return f"ok: sent 0x{mid:X} [{' '.join(f'{b:02X}' for b in payload)}]"
            except Exception as e:
                return f"FAIL: send error — {e}"

    def send_periodic_can(self, msg_id, data, period_ms: str = "1000",
                          is_extended: str = "False", is_fd: str = "False"):
        """주기(cyclic) 송신 시작/갱신. 같은 ID 재호출 시 데이터만 갱신한다.

        stop_all_tx / can_all_stop 로 정지. period_ms: 송신 주기(밀리초).
        """
        with self._lock:
            if self._bus is None:
                return "FAIL: PCAN not connected"
            try:
                mid = _parse_msg_id(msg_id)
                payload = _parse_data(data)
                try:
                    period_s = max(1, int(float(str(period_ms).strip() or "1000"))) / 1000.0
                except ValueError:
                    period_s = 1.0
                msg = can.Message(
                    arbitration_id=mid,
                    data=payload,
                    is_extended_id=_to_bool(is_extended),
                    is_fd=_to_bool(is_fd) or self.fd,
                )
                existing = self._periodic.get(mid)
                if existing is not None:
                    existing.modify_data(msg)
                    return f"ok: periodic 0x{mid:X} updated (period={period_s * 1000:.0f}ms)"
                task = self._bus.send_periodic(msg, period_s)
                self._periodic[mid] = task
                return f"ok: periodic 0x{mid:X} started (period={period_s * 1000:.0f}ms)"
            except Exception as e:
                return f"FAIL: periodic send error — {e}"

    def stop_all_tx(self):
        """모든 주기 송신 작업을 정지."""
        with self._lock:
            if self._bus is None:
                return "FAIL: PCAN not connected"
            n = self._stop_all_tx_locked()
            return f"ok: stopped {n} periodic task(s)"

    # ── 로깅 ────────────────────────────────────────────────────────────
    def start_logging(self, csv_file: str = ""):
        """CAN 트래픽 CSV 로깅 시작. csv_file 이 비면 재생 런 폴더(logs/) 또는
        results/PCAN_Log 아래로 자동 저장한다.
        """
        with self._lock:
            if self._bus is None:
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
                self._notifier = can.Notifier(self._bus, [self._log_listener])
                self._log_path = path
                return f"ok: logging started → {path}"
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

    def can_all_stop(self):
        """주기 송신 + 로깅을 한 번에 정지 (bus 는 유지 → 연결 상태 보존)."""
        with self._lock:
            if self._bus is None:
                return "ok: PCAN not connected"
            self._stop_all_tx_locked()
            self._stop_logging_locked()
            return "ok: all tx + logging stopped"

    # ── 내부 (락 보유 상태에서 호출) ───────────────────────────────────
    def _stop_all_tx_locked(self) -> int:
        n = len(self._periodic)
        if self._bus is not None:
            try:
                self._bus.stop_all_periodic_tasks()
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
