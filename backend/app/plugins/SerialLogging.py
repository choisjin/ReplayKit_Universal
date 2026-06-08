"""SerialLogging — 시리얼 포트 로그 캡처·저장·키워드 합부 판정 모듈.

시나리오 스텝 내에서:
  - StartLogging / StopLogging 으로 시리얼 캡처 시작/종료
  - SendCommand 로 명령 전송
  - SendCommand_fail_on_keyword / SendCommand_pass_on_keyword 로
    명령 전송 + 응답 캡처를 한 호출로 묶어 합부 판정

연결 수명 주기:
  - 시리얼 포트의 open/close는 Device 탭의 Connect/Disconnect가 관리.
  - StartLogging은 (이미 열려 있으면) 포트를 재open하지 않고 캡처 세션만 시작.
  - StopLogging은 메모리 로그를 파일로 일괄 저장하고 캡처를 멈추되 포트는 유지.
    → 일부 장비는 connect 시 모든 설정이 초기화되므로, 캡처를 멈춰도 포트는
      살아 있어야 후속 SendCommand가 디바이스를 리셋시키지 않는다.

사용 예 (시나리오 스텝):
  SerialLogging.StartLogging()                                          # 캡처 시작 (포트는 Device 탭 Connect로 이미 열림)
  SerialLogging.SendCommand_pass_on_keyword("ping", "OK", time=3)       # 응답 OK 검사
  SerialLogging.SendCommand_fail_on_keyword("self_test", "ERROR", 10)   # ERROR 검출 모니터링
  SerialLogging.StopLogging()                                           # 캡처 종료 + 파일 저장 (포트는 유지)
"""

import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ==========================================================================
# Serial 뷰어용 Pub/Sub 허브 — DLT_HUB와 동일 패턴.
# ==========================================================================

class _SerialHub:
    """Serial 로깅 세션 + 로그 스트림 구독자 관리 (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}
        self._lifecycle_subs: list[queue.Queue] = []
        self._log_subs: dict[str, list[queue.Queue]] = {}

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [{"session_id": sid, **info} for sid, info in self._sessions.items()]

    def emit_lifecycle(self, event: dict) -> None:
        sid = event.get("session_id", "")
        etype = event.get("type", "")
        with self._lock:
            if etype == "session_started" and sid:
                self._sessions[sid] = {k: v for k, v in event.items() if k not in ("type",)}
            elif etype == "session_stopped" and sid:
                self._sessions.pop(sid, None)
            subs = list(self._lifecycle_subs)
        logger.info("[SERIAL_HUB] emit_lifecycle type=%s sid=%s subscribers=%d",
                    etype, sid, len(subs))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def register_lifecycle(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._lifecycle_subs.append(q)
            for sid, info in self._sessions.items():
                try:
                    q.put_nowait({"type": "session_started", "session_id": sid, **info})
                except queue.Full:
                    break
        return q

    def unregister_lifecycle(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._lifecycle_subs:
                self._lifecycle_subs.remove(q)

    def register_log(self, session_id: str) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=10000)
        with self._lock:
            self._log_subs.setdefault(session_id, []).append(q)
        return q

    def unregister_log(self, session_id: str, q: queue.Queue) -> None:
        with self._lock:
            lst = self._log_subs.get(session_id, [])
            if q in lst:
                lst.remove(q)

    def emit_log(self, session_id: str, line: str) -> None:
        with self._lock:
            subs = list(self._log_subs.get(session_id, []))
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass


SERIAL_HUB = _SerialHub()


def get_active_session(session_id: str) -> Optional["SerialLogging"]:
    """session_id(port@bps)에 대응하는 현재 활성 SerialLogging 인스턴스 반환."""
    try:
        from backend.app.services.module_service import _instances
    except Exception:
        return None
    inst = _instances.get("SerialLogging")
    if not inst:
        return None
    if f"{getattr(inst, '_port', '')}@{getattr(inst, '_bps', 0)}" == session_id:
        return inst
    return None


def _get_run_output_dir() -> Optional[Path]:
    """현재 재생 런의 출력 디렉토리. 재생 중이 아니면 None."""
    try:
        from backend.app.services.playback_service import get_run_output_dir
        return get_run_output_dir()
    except Exception:
        return None


def _is_scenario_playback() -> bool:
    """시나리오 재생 active 여부. lifecycle 이벤트에 컨텍스트 플래그로 부착되어
    프론트엔드(RecordPage) 모달 자동 오픈을 막는다 — ScenarioPage가 이미 좌측 카드로 표시함."""
    try:
        from backend.app.services.playback_service import is_playback_active
        return is_playback_active()
    except Exception:
        return False


def _auto_save_path(prefix: str = "serial") -> str:
    """컨텍스트별 자동 저장 경로.

    - 재생 중: {run_dir}/logs/{prefix}_{ts}.log
    - 스텝 테스트: backend/results/Temp_logs/{prefix}_{ts}.log
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    run_dir = _get_run_output_dir()
    if run_dir:
        log_dir = run_dir / "logs"
    else:
        try:
            from backend.app.services.playback_service import RESULTS_DIR
            log_dir = Path(RESULTS_DIR) / "Temp_logs"
        except Exception:
            log_dir = Path(__file__).resolve().parent.parent.parent / "results" / "Temp_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / f"{prefix}_{ts}.log")


class SerialLogging:
    """시리얼 로그 캡처·저장·키워드 판정 모듈.

    생성자:
        port: 시리얼 포트 (예: COM3)
        bps: 보드레이트 (기본 115200)
    """

    def __init__(self, port: str = "", bps: int = 115200):
        self._port = port
        self._bps = int(bps)
        self._serial = None  # serial.Serial (lazy import)
        self._capture_thread: Optional[threading.Thread] = None
        self._capturing = False
        self._lock = threading.Lock()

        # 로그 버퍼 + 라인별 capture timestamp (epoch float)
        # _log_capture_ts와 _logs는 같은 길이 유지 — backfill 스캔 시 정확한 발생 시각 사용
        self._logs: list[str] = []
        self._log_capture_ts: list[float] = []
        self._line_counter = 0

        # 파일 저장
        self._save_file = None
        self._save_path: Optional[str] = None

    # ------------------------------------------------------------------
    # 연결 관리 (내부)
    # ------------------------------------------------------------------

    def _connect(self, settle_ms: int = 500) -> str:
        """시리얼 포트 연결.

        Args:
            settle_ms: open 직후 드라이버/디바이스 안정화 대기(ms).
                       USB-Serial 어댑터(FTDI/CP210x/CH340 등)는 open 시 DTR/RTS 펄스가
                       발생하여 디바이스가 짧게 리셋되는 경우가 있고, OS도 buffer 설정 적용에
                       수십~수백 ms를 쓴다. 이 시간 안에 SendCommand가 들어오면 씹힘 — settle 후에
                       reset_input/output_buffer로 가비지를 비우고 capture loop를 시작한다.
        """
        if not self._port:
            return "ERROR: port가 설정되지 않았습니다"
        if self._serial and self._serial.is_open:
            return ""  # 이미 연결됨 — 정상

        try:
            import serial as pyserial
            self._serial = pyserial.Serial(self._port, self._bps, timeout=1)
            # 1) 드라이버/디바이스 안정화 — capture loop 시작 전에 처리 (가비지 라인 캡처 방지)
            if settle_ms and settle_ms > 0:
                time.sleep(settle_ms / 1000.0)
            # 2) open 동안 들어온 가비지 / 송신 잔여 비우기
            try:
                self._serial.reset_input_buffer()
                self._serial.reset_output_buffer()
            except Exception as _be:
                logger.debug("[SerialLogging] buffer reset skipped: %s", _be)
            self._logs.clear()
            self._log_capture_ts.clear()
            self._line_counter = 0
            # 3) capture loop 시작 후, 스레드가 실제 readline에 진입할 시간을 짧게 보장
            self._start_capture()
            time.sleep(0.05)  # capture thread가 첫 read 루프에 진입할 충분한 시간
            logger.info("[SerialLogging] Connected to %s @ %d (settle=%dms)",
                        self._port, self._bps, settle_ms)
            return ""
        except Exception as e:
            self._serial = None
            logger.error("[SerialLogging] Connection failed: %s", e)
            return f"ERROR: 연결 실패 — {e}"

    def _disconnect(self):
        """시리얼 포트 연결 해제. cleanup 경로에서 호출되므로 어떤 단계도 raise 하지 않는다."""
        try:
            self._stop_capture()
        except Exception as e:
            logger.warning("[SerialLogging] stop_capture raised: %s", e)
        if self._serial is not None:
            try:
                if getattr(self._serial, "is_open", False):
                    self._serial.close()
            except Exception as e:
                logger.warning("[SerialLogging] serial.close raised: %s", e)
        self._serial = None
        logger.info("[SerialLogging] Disconnected")

    def IsConnected(self) -> bool:
        """포트가 열려 있거나, 캡처 세션이 살아 있으면(일시적 USB 드롭 + 자동 재연결 중) True.

        module_service._is_connected가 이 메서드를 우선 호출하여 디바이스 status를
        결정한다. 그리고 _get_instance는 _is_connected가 False면 **인스턴스를 폐기하고
        새로 생성**한다 — 이 경우 캡처 버퍼(self._logs)가 통째로 유실되어, 뷰어에는
        (hub 세션 공유로) 로그가 계속 보여도 StopLogging이 저장하는 버퍼는 비게 된다.

        따라서 캡처 세션이 진행 중(self._capturing=True)이면 포트가 잠시 닫혀
        재연결 중이더라도 '연결됨'으로 보고하여 인스턴스(=버퍼)가 보존되도록 한다.
        세션이 없을 때(StopLogging 이후 등)는 실제 포트 상태를 그대로 반영해
        보조 디바이스 '연결' 직후 Send_Packet 등의 사용 가능 여부를 UI에 올바로 알린다.
        """
        if self._serial and getattr(self._serial, "is_open", False):
            return True
        return bool(self._capturing)

    def Connect(self) -> str:
        """모듈 표준 연결 인터페이스 — 보조 디바이스 '연결' 클릭 시 자동 호출됨.

        module_service._get_instance가 인자 없는 Connect()를 발견하면 인스턴스
        생성 직후 자동으로 호출한다. 포트 open + capture 스레드 시작만 수행하고
        SERIAL_HUB lifecycle은 emit하지 않는다 — 뷰어 모달 자동 오픈은 StartLogging()
        호출(시나리오 스텝)에만 한정. 디바이스 탭의 단순 연결로 모달이 튀어나오지
        않도록 하기 위함.

        이후 Send_Packet/SendCommand는 즉시 사용 가능. 사용자가 로그 뷰어를 보고
        싶다면 시나리오에서 StartLogging()을 호출하면 된다 (_connect는 idempotent).
        """
        err = self._connect()
        if err:
            return err
        return f"Connected: {self._port} @ {self._bps}"

    def Disconnect(self) -> str:
        """모듈 표준 연결 해제 인터페이스 — 보조 디바이스 '연결 해제' / cleanup 경로에서 자동 호출됨.

        capture 스레드 중단 + 시리얼 포트 close. lifecycle session_stopped emit은
        Connect 시점에 session_started를 emit하지 않았으므로 대칭으로 생략.
        StartLogging→StopLogging 사이클로 만든 세션은 StopLogging이 자체적으로
        session_stopped를 emit하므로 영향 없음.

        진행 중인 로깅 세션(capture 활성 + 미저장 버퍼)이 있으면 포트 close 전에
        자동 저장 — 시나리오 비정상 종료(cleanup_active_instances) 시 로그 유실 방지.
        """
        if not self._serial or not self._serial.is_open:
            return "Already disconnected"
        # 진행 중인 로깅 세션이 있으면 먼저 저장 (cleanup 안전성)
        if self._capturing and self._logs:
            try:
                self.StopLogging()
            except Exception as e:
                logger.warning("[SerialLogging] auto-save during Disconnect failed: %s", e)
        self._disconnect()
        return f"Disconnected: {self._port}"

    def _session_id(self) -> str:
        return f"{self._port}@{self._bps}"

    # ------------------------------------------------------------------
    # 뷰어 연동: StartLogging / StopLogging (DLTLogging과 유사 시그니처)
    # ------------------------------------------------------------------

    def StartLogging(self, settle_ms: int = 500) -> str:
        """뷰어 연동용: 시리얼 연결 + 로그 캡처 시작 (메모리만, 파일 저장 없음).

        Args:
            settle_ms: 포트 open 후 안정화 대기 시간(ms). 기본 500ms — USB-Serial
                       드라이버 reset/buffer settle 동안 다음 스텝의 SendCommand가 씹히지
                       않도록 보장. 이미 포트가 열려 있으면 이 대기는 스킵됨(재연결 안 함).
                       Arduino처럼 DTR-reset되는 보드는 1500~2000으로 늘릴 수 있다.

        리턴 시점에는 포트가 열리고, capture 스레드가 첫 readline 루프에 진입한 상태이므로
        다음 스텝에서 즉시 SendCommand해도 안전하다. SERIAL_HUB에 session_started 이벤트를
        emit하여 뷰어가 자동 오픈된다.

        포트가 이미 열려 있으면(_connect의 idempotent 가드) 재연결/안정화 대기 없이 캡처
        세션만 새로 시작 — 일부 장비가 connect 시 설정이 초기화되는 문제를 피한다.
        StopLogging 후 재호출 시 capture 스레드와 로그 버퍼는 새로 초기화된다.
        """
        err = self._connect(settle_ms=settle_ms)
        if err:
            return err
        # _connect의 idempotent 분기로 빠진 경우 capture가 중단된 상태일 수 있음 (StopLogging 후).
        # 새 로깅 세션 시작 — 버퍼 초기화 + capture 스레드 재기동.
        if not self._capturing:
            with self._lock:
                self._logs.clear()
                self._log_capture_ts.clear()
            self._line_counter = 0
            self._start_capture()
            time.sleep(0.05)
        SERIAL_HUB.emit_lifecycle({
            "type": "session_started",
            "session_id": self._session_id(),
            "port": self._port,
            "bps": self._bps,
            "save_path": "",
            "started_at": time.time(),
            "scenario_playback": _is_scenario_playback(),
        })
        return f"Logging started: {self._port} @ {self._bps} (settle={settle_ms}ms)"

    def StopLogging(self, save_path: str = "") -> str:
        """뷰어 연동용: 메모리 버퍼를 파일로 일괄 저장하고 캡처 세션을 종료한다.
        **시리얼 포트는 그대로 유지** — Device 탭에서 명시적으로 Disconnect 하기 전까지
        연결이 살아 있어 후속 SendCommand 등이 디바이스를 재초기화하지 않는다.

        Args:
            save_path: 저장할 파일 경로. 빈 값이면 컨텍스트별 자동 저장:
                - 재생 중: {run_dir}/logs/serial_{timestamp}.log
                - 스텝 테스트: backend/results/Temp_logs/serial_{timestamp}.log

        파일 저장 단계의 어떤 예외(경로 해석/mkdir/open)가 발생해도 finally에서 캡처
        스레드는 무조건 정지되어 리소스 누수를 막는다. 포트 close가 필요하면 별도로
        Disconnect를 호출하거나 Device 탭에서 연결 해제하면 된다.

        cleanup_active_instances가 재생 중단 시 호출하는 Disconnect 내부에서도 진행
        중인 로깅이 있으면 이 메서드가 먼저 호출되어 로그 유실을 방지한다.
        """
        sid = self._session_id()
        with self._lock:
            logs_snapshot = list(self._logs)

        saved_path = ""
        save_error = ""
        try:
            if not save_path:
                save_path = _auto_save_path("serial")
            elif not os.path.dirname(save_path):
                base_dir = Path(_auto_save_path("serial")).parent
                save_path = str(base_dir / save_path)
            try:
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(logs_snapshot))
                    if logs_snapshot:
                        f.write("\n")
                saved_path = save_path
                logger.info("[SerialLogging] Saved %d lines to %s", len(logs_snapshot), save_path)
            except Exception as e:
                logger.error("[SerialLogging] Save failed: %s", e)
                save_error = str(e)
        except Exception as e:
            # _auto_save_path 등 경로 해석이 실패해도 finally에서 capture는 무조건 정리
            logger.error("[SerialLogging] StopLogging path resolution failed: %s", e)
            save_error = save_error or str(e)
        finally:
            # 캡처 세션은 종료 — 포트는 유지 (연결 끊지 않음)
            self._close_save_file()
            try:
                self._stop_capture()
            except Exception as e:
                logger.warning("[SerialLogging] stop_capture during StopLogging raised: %s", e)
            # 다음 StartLogging이 새 세션으로 시작될 수 있도록 버퍼 초기화
            with self._lock:
                self._logs.clear()
                self._log_capture_ts.clear()
            self._line_counter = 0
            try:
                SERIAL_HUB.emit_lifecycle({
                    "type": "session_stopped",
                    "session_id": sid,
                    "save_path": saved_path,
                    "stopped_at": time.time(),
                })
            except Exception:
                pass

        if save_error:
            return f"ERROR: 저장 실패 — {save_error}"
        return f"Logging saved ({len(logs_snapshot)} lines) to: {saved_path} (port kept open)"

    # ------------------------------------------------------------------
    # 뷰어용 조회 (DLT와 동일 인터페이스)
    # ------------------------------------------------------------------

    def _GetRecentLogs(self, limit: int = 1000) -> list[str]:
        with self._lock:
            return list(self._logs[-int(limit):]) if self._logs else []

    def _close_save_file(self):
        if self._save_file:
            try:
                self._save_file.close()
            except Exception:
                pass
            self._save_file = None
            self._save_path = None

    # ------------------------------------------------------------------
    # 명령어 전송
    # ------------------------------------------------------------------

    def SendCommand(self, command: str, encoding: str = "utf-8", append_newline: bool = True) -> str:
        """시리얼 포트로 문자열 명령어를 전송합니다.

        Args:
            command: 전송할 명령어
            encoding: 인코딩 (기본 utf-8)
            append_newline: 개행 문자 자동 추가 (기본 True)

        Returns:
            결과 메시지
        """
        if not self._serial or not self._serial.is_open:
            return "ERROR: 시리얼 포트가 연결되어 있지 않습니다. StartLogging() 먼저 호출하세요."
        data = command
        if append_newline and not data.endswith("\n"):
            data += "\n"
        self._serial.write(data.encode(encoding))
        logger.info("[SerialLogging] SendCommand: %s", command.strip())
        return "OK"

    def Send_Packet(self, data: str) -> str:
        """raw hex 바이트 패킷을 시리얼 포트로 전송합니다.

        공백으로 구분된 hex 토큰 문자열을 받아 각 토큰을 바이트로 변환 후 송신.
        토큰별 파싱이라 `"00 77 42"`, `"0x79 0x6D"`, `"7 6D F2"` 같이 자릿수가
        다양해도 처리됩니다. write 후 `flush()` 호출로 OS 출력 버퍼까지 비워
        실제 회선 도달을 보장합니다.

        Args:
            data: 공백 구분 hex 문자열 (예: "00 77 42 37 02 F2 00 FE 00 FE 00")

        Returns:
            "OK: Sent N bytes (HH HH HH ...)" 또는 "ERROR: ..."

        예:
            SerialLogging.Send_Packet("79 6D F2 0F")
            SerialLogging.Send_Packet("00 77 42 37 02 F2 00 FE 00 FE 00")
        """
        if not self._serial or not self._serial.is_open:
            return "ERROR: 시리얼 포트가 연결되어 있지 않습니다. StartLogging() 먼저 호출하세요."
        if not data or not data.strip():
            return "ERROR: data가 비어 있습니다"
        try:
            # 공백 분리 → 각 토큰 hex 정수 변환 (1자리/2자리/0x prefix 모두 허용)
            tokens = data.split()
            byte_list: list[int] = []
            for tok in tokens:
                val = int(tok, 16)
                if val < 0 or val > 0xFF:
                    return f"ERROR: hex 값이 1바이트 범위(0~0xFF)를 벗어남 — '{tok}' → {val}"
                byte_list.append(val)
            raw = bytes(byte_list)
        except ValueError as e:
            return f"ERROR: hex 파싱 실패 — {e}"

        try:
            self._serial.write(raw)
            self._serial.flush()  # OS 출력 버퍼 비워서 wire 도달 보장
        except Exception as e:
            return f"ERROR: 송신 실패 — {e}"

        hex_str = " ".join(f"{b:02X}" for b in raw)
        logger.info("[SerialLogging] Send_Packet (%d bytes): %s", len(raw), hex_str)
        return f"OK: Sent {len(raw)} bytes ({hex_str})"

    # ------------------------------------------------------------------
    # 명령어 전송 + 키워드 합부 판정 (응답 라인을 즉시 캐치)
    # ------------------------------------------------------------------

    def SendCommand_fail_on_keyword(self, command: str, keyword: str, time: float = 5,
                                      encoding: str = "utf-8",
                                      append_newline: bool = True) -> str:
        """명령어 전송 후 응답에 keyword가 **포함되면 FAIL 판정**.

        'ERROR'/'Fail'/'crash' 등 비정상 키워드 검출용. 명령 전송 직후 캡처되는
        라인을 'time' 초간 모니터링하여, keyword 매칭 라인이 발견되면 모두
        fail row로 누적되며 결과 표에 인라인(Fail_Count_N) 표시됨.

        동작:
          1) SendCommand로 명령 전송 (실패 시 즉시 ERROR 반환)
          2) 전송 직전의 로그 인덱스를 잡아 그 이후 라인만 검사 — 과거 로그 무시
          3) time 초간 새로 들어오는 라인을 폴링하며 keyword 검사
          4) 매칭된 모든 라인을 fail row로 누적 후 PASS/FAIL 메시지 반환

        Args:
            command: 전송할 명령어
            keyword: FAIL을 일으킬 검출 키워드 (substring match)
            time: 응답 모니터링 시간(초). 기본 5초
            encoding: 인코딩 (기본 utf-8)
            append_newline: 개행 문자 자동 추가 (기본 True)
        """
        import time as _time_mod
        if not self._serial or not self._serial.is_open:
            return "ERROR: 시리얼 포트가 연결되어 있지 않습니다. StartLogging() 먼저 호출하세요."
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"

        # 응답 매칭 시작점 — 전송 직전의 로그 인덱스. capture_loop와의 race를 lock으로 차단
        with self._lock:
            start_idx = len(self._logs)

        data = command if (not append_newline or command.endswith("\n")) else command + "\n"
        try:
            self._serial.write(data.encode(encoding))
        except Exception as e:
            return f"ERROR: 명령 전송 실패 — {e}"
        logger.info("[SerialLogging] SendCommand_fail_on_keyword: cmd='%s' kw='%s' time=%.1fs",
                    command.strip(), keyword, float(time))

        # parent step 컨텍스트 (인라인 결과 표시용)
        parent_step_id: Optional[int] = None
        parent_repeat_index = 1
        try:
            from backend.app.services.playback_service import get_current_step_context
            parent_step_id, parent_repeat_index = get_current_step_context()
        except Exception:
            pass

        deadline = _time_mod.time() + float(time)
        hits: list[tuple[float, str]] = []
        check_idx = start_idx
        while _time_mod.time() < deadline:
            with self._lock:
                snapshot_logs = self._logs[check_idx:]
                snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot_logs)]
            check_idx += len(snapshot_logs)
            for ln, ts in zip(snapshot_logs, snapshot_ts):
                if keyword in ln:
                    hits.append((ts, ln))
            _time_mod.sleep(0.1)

        # 마지막 한 번 더 확인 — deadline 직전 도착한 라인 누락 방지
        with self._lock:
            tail_logs = self._logs[check_idx:]
            tail_ts = self._log_capture_ts[check_idx:check_idx + len(tail_logs)]
        for ln, ts in zip(tail_logs, tail_ts):
            if keyword in ln:
                hits.append((ts, ln))

        if hits:
            try:
                from backend.app.services.playback_service import report_runtime_fail
                for ts_b, ln in hits:
                    report_runtime_fail(
                        "SerialLogging", keyword, ts_b, ln, reason="matched",
                        repeat_index=parent_repeat_index,
                        parent_step_id=parent_step_id,
                    )
            except Exception:
                pass
            first = hits[0][1].strip()[:120]
            return (f"FAIL: keyword '{keyword}' detected {len(hits)} time(s) "
                    f"after command — {first}")
        return f"PASS: keyword '{keyword}' not detected within {float(time):g}s after command"

    def SendCommand_pass_on_keyword(self, command: str, keyword: str, time: float = 5,
                                      encoding: str = "utf-8",
                                      append_newline: bool = True) -> str:
        """명령어 전송 후 응답에 keyword가 **포함되면 PASS 판정**.

        'OK'/'Pass'/'BootComplete' 등 정상 응답 키워드 검출용. 명령 전송 직후
        캡처되는 라인을 모니터링하여 keyword를 발견하면 즉시 PASS 반환.
        time 초 안에 발견되지 않으면 fail row 누적 후 FAIL 반환.

        동작:
          1) SendCommand로 명령 전송 (실패 시 즉시 ERROR 반환)
          2) 전송 직전의 로그 인덱스를 잡아 그 이후 라인만 검사
          3) 새 라인 폴링하며 keyword 검사 — 발견 즉시 PASS 반환 (조기 종료)
          4) 타임아웃이면 fail row 1건 누적 후 FAIL 반환

        Args:
            command: 전송할 명령어
            keyword: PASS를 만족할 키워드 (substring match)
            time: 응답 대기 시간(초). 기본 5초
            encoding: 인코딩 (기본 utf-8)
            append_newline: 개행 문자 자동 추가 (기본 True)
        """
        import time as _time_mod
        if not self._serial or not self._serial.is_open:
            return "ERROR: 시리얼 포트가 연결되어 있지 않습니다. StartLogging() 먼저 호출하세요."
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"

        with self._lock:
            start_idx = len(self._logs)

        data = command if (not append_newline or command.endswith("\n")) else command + "\n"
        try:
            self._serial.write(data.encode(encoding))
        except Exception as e:
            return f"ERROR: 명령 전송 실패 — {e}"
        logger.info("[SerialLogging] SendCommand_pass_on_keyword: cmd='%s' kw='%s' time=%.1fs",
                    command.strip(), keyword, float(time))

        parent_step_id: Optional[int] = None
        parent_repeat_index = 1
        try:
            from backend.app.services.playback_service import get_current_step_context
            parent_step_id, parent_repeat_index = get_current_step_context()
        except Exception:
            pass

        deadline = _time_mod.time() + float(time)
        check_idx = start_idx
        while _time_mod.time() < deadline:
            with self._lock:
                snapshot_logs = self._logs[check_idx:]
                snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot_logs)]
            check_idx += len(snapshot_logs)
            for ln, ts in zip(snapshot_logs, snapshot_ts):
                if keyword in ln:
                    summary = ln.strip()[:120]
                    return f"PASS: keyword '{keyword}' detected — {summary}"
            _time_mod.sleep(0.1)

        # 최종 확인
        with self._lock:
            tail_logs = self._logs[check_idx:]
            tail_ts = self._log_capture_ts[check_idx:check_idx + len(tail_logs)]
        for ln, ts in zip(tail_logs, tail_ts):
            if keyword in ln:
                summary = ln.strip()[:120]
                return f"PASS: keyword '{keyword}' detected — {summary}"

        # 타임아웃 — fail row 1건 보고
        fail_ts = _time_mod.time()
        fail_line = f"(timeout: '{keyword}' not found after command '{command.strip()}')"
        try:
            from backend.app.services.playback_service import report_runtime_fail
            report_runtime_fail(
                "SerialLogging", keyword, fail_ts, fail_line, reason="missing",
                repeat_index=parent_repeat_index,
                parent_step_id=parent_step_id,
            )
        except Exception:
            pass
        return f"FAIL: keyword '{keyword}' not detected within {float(time):g}s after command"

    # ------------------------------------------------------------------
    # 명령 전송 없이 로그만 판독 (수동 모니터링)
    #   - 시리얼로 아무것도 쓰지 않으므로(write 없음) 디바이스 리셋/USB 전류 스파이크가
    #     없어, 같은 USB 허브에 물린 웹캠 녹화 등 다른 장치에 영향을 주지 않는다.
    #   - 이미 캡처된 버퍼(과거) + 앞으로 time초 동안 들어오는 라인을 함께 검사한다.
    # ------------------------------------------------------------------

    @staticmethod
    def _current_step_context() -> tuple[Optional[int], int]:
        """현재 재생 스텝 컨텍스트(parent_step_id, repeat_index). 재생 외이면 (None, 1)."""
        try:
            from backend.app.services.playback_service import get_current_step_context
            return get_current_step_context()
        except Exception:
            return None, 1

    def Monitor_pass_on_keyword(self, keyword: str, time: float = 5,
                                 include_past: bool = True) -> str:
        """**명령을 전송하지 않고** 로그에서 keyword를 찾으면 PASS 판정.

        SendCommand_pass_on_keyword와 달리 시리얼 포트로 아무것도 쓰지 않는다
        (write로 인한 디바이스 리셋/USB 전류 스파이크가 없어 같은 허브의 웹캠 녹화 등
        다른 장치에 영향을 주지 않음). 캡처 세션은 StartLogging 또는 디바이스 Connect로
        이미 돌고 있어야 한다.

        검사 범위:
          1) include_past=True(기본): **현재까지 캡처된 버퍼 전체**를 먼저 스캔 —
             이미 출력된 로그에 keyword가 있으면 즉시 PASS.
          2) 이후 time초 동안 새로 들어오는 라인을 폴링하며 검사 — 발견 즉시 PASS.
          3) time초 안에 못 찾으면 fail row 1건 누적 후 FAIL.
             (time=0 이면 과거 버퍼만 1회 검사하고 끝.)

        Args:
            keyword: PASS를 만족할 키워드 (substring match)
            time: 미래 로그 대기 시간(초). 기본 5. 0이면 과거 버퍼만 검사.
            include_past: 현재까지 캡처된 로그(과거)도 검사할지 (기본 True)
        """
        import time as _time_mod
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"

        # 1) 과거 버퍼 스캔(옵션) + 미래 검사 시작 인덱스 결정
        with self._lock:
            check_idx = 0 if include_past else len(self._logs)
            snapshot_logs = self._logs[check_idx:]
        for ln in snapshot_logs:
            if keyword in ln:
                return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"
        check_idx += len(snapshot_logs)

        logger.info("[SerialLogging] Monitor_pass_on_keyword: kw='%s' time=%.1fs include_past=%s",
                    keyword, float(time), include_past)

        # 2) 미래 라인 폴링
        deadline = _time_mod.time() + max(0.0, float(time))
        while _time_mod.time() < deadline:
            with self._lock:
                snapshot_logs = self._logs[check_idx:]
            check_idx += len(snapshot_logs)
            for ln in snapshot_logs:
                if keyword in ln:
                    return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"
            _time_mod.sleep(0.1)

        # 3) 최종 확인 — deadline 직전 도착 라인 누락 방지
        with self._lock:
            tail_logs = self._logs[check_idx:]
        for ln in tail_logs:
            if keyword in ln:
                return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"

        # 타임아웃 — fail row 1건 보고
        parent_step_id, parent_repeat_index = self._current_step_context()
        fail_ts = _time_mod.time()
        fail_line = f"(timeout: '{keyword}' not found in serial log)"
        try:
            from backend.app.services.playback_service import report_runtime_fail
            report_runtime_fail(
                "SerialLogging", keyword, fail_ts, fail_line, reason="missing",
                repeat_index=parent_repeat_index, parent_step_id=parent_step_id,
            )
        except Exception:
            pass
        return f"FAIL: keyword '{keyword}' not detected within {float(time):g}s"

    def Monitor_fail_on_keyword(self, keyword: str, time: float = 5,
                                 include_past: bool = True) -> str:
        """**명령을 전송하지 않고** 로그에서 keyword가 발견되면 FAIL 판정.

        'ERROR'/'crash' 등 비정상 키워드를 write 없이 모니터링. include_past=True면
        현재까지의 버퍼도 검사하고, time초 동안 들어오는 라인도 검사하여, 매칭된 모든
        라인을 fail row로 누적한다(결과 표에 인라인 표시).

        Args:
            keyword: FAIL을 일으킬 검출 키워드 (substring match)
            time: 미래 로그 모니터링 시간(초). 기본 5. 0이면 과거 버퍼만 검사.
            include_past: 현재까지 캡처된 로그(과거)도 검사할지 (기본 True)
        """
        import time as _time_mod
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"

        parent_step_id, parent_repeat_index = self._current_step_context()
        hits: list[tuple[float, str]] = []

        # 1) 과거 버퍼 스캔(옵션)
        with self._lock:
            check_idx = 0 if include_past else len(self._logs)
            snapshot_logs = self._logs[check_idx:]
            snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot_logs)]
        for ln, ts in zip(snapshot_logs, snapshot_ts):
            if keyword in ln:
                hits.append((ts, ln))
        check_idx += len(snapshot_logs)

        logger.info("[SerialLogging] Monitor_fail_on_keyword: kw='%s' time=%.1fs include_past=%s",
                    keyword, float(time), include_past)

        # 2) 미래 라인 폴링
        deadline = _time_mod.time() + max(0.0, float(time))
        while _time_mod.time() < deadline:
            with self._lock:
                snapshot_logs = self._logs[check_idx:]
                snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot_logs)]
            check_idx += len(snapshot_logs)
            for ln, ts in zip(snapshot_logs, snapshot_ts):
                if keyword in ln:
                    hits.append((ts, ln))
            _time_mod.sleep(0.1)

        # 3) 최종 확인
        with self._lock:
            tail_logs = self._logs[check_idx:]
            tail_ts = self._log_capture_ts[check_idx:check_idx + len(tail_logs)]
        for ln, ts in zip(tail_logs, tail_ts):
            if keyword in ln:
                hits.append((ts, ln))

        if hits:
            try:
                from backend.app.services.playback_service import report_runtime_fail
                for ts_b, ln in hits:
                    report_runtime_fail(
                        "SerialLogging", keyword, ts_b, ln, reason="matched",
                        repeat_index=parent_repeat_index, parent_step_id=parent_step_id,
                    )
            except Exception:
                pass
            return (f"FAIL: keyword '{keyword}' detected {len(hits)} time(s) — "
                    f"{hits[0][1].strip()[:120]}")
        return f"PASS: keyword '{keyword}' not detected within {float(time):g}s"

    # ------------------------------------------------------------------
    # 상태 조회 (내부)
    # ------------------------------------------------------------------

    def _GetStatus(self) -> str:
        """현재 모듈 상태를 조회합니다.

        Returns:
            상태 문자열
        """
        connected = self.IsConnected()
        with self._lock:
            log_count = len(self._logs)
        saving = self._save_path or "N/A"

        parts = [
            f"Port: {self._port} @ {self._bps}",
            f"Connected: {connected}",
            f"Capturing: {self._capturing}",
            f"Logs: {log_count} (total: {self._line_counter})",
            f"Saving: {saving}",
        ]
        return " | ".join(parts)

    def _ClearLogs(self) -> str:
        """로그 버퍼를 초기화합니다.

        Returns:
            결과 메시지
        """
        with self._lock:
            self._logs.clear()
            self._log_capture_ts.clear()
        self._line_counter = 0
        return "Logs cleared"

    # ------------------------------------------------------------------
    # 로그 캡처 (백그라운드 스레드)
    # ------------------------------------------------------------------

    def _start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="SerialLogging-Capture", daemon=True
        )
        self._capture_thread.start()

    def _stop_capture(self):
        self._capturing = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None

    def _capture_loop(self):
        """백그라운드 스레드: 시리얼 데이터를 줄 단위로 수신.

        **장치가 껐다 켜지거나 USB가 재열거되어 read가 실패해도 스레드를 종료하지
        않는다.** 끊긴 핸들을 정리하고 self._port로 재open을 반복 시도하여, 장치가
        다시 올라오면 **같은 버퍼(self._logs)에 로그를 이어서 계속 캡처**한다.
        이전 구현은 read 예외 한 번에 break → 스레드가 영구 종료되어 이후 라인이
        한 줄도 안 잡히고(부분 저장) pass_on_keyword가 응답을 못 받아 오판하던 문제를
        해결한다.

        종료는 StopLogging/Disconnect가 self._capturing을 False로 만들 때만 일어난다.
        끊김/재연결 구간은 마커 라인으로 로그에 가시화하여 결측 구간을 알 수 있게 한다.

        주의(Linux): USB 재열거 시 디바이스 노드 번호가 바뀌면(ttyUSB0→ttyUSB1) 원래
        경로 재open이 실패할 수 있다. 안정적인 by-id 심볼릭 경로(/dev/serial/by-id/...)
        사용을 권장한다.
        """
        backoff = 0.5  # 재연결 실패 시 지수 backoff (최대 5초)
        while self._capturing:
            ser = self._serial
            # 핸들이 없거나 닫혀 있으면 재연결 시도 (버퍼는 유지 → 이어서 기록)
            if ser is None or not getattr(ser, "is_open", False):
                if self._reconnect_serial():
                    backoff = 0.5
                    ser = self._serial
                else:
                    self._interruptible_sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue

            try:
                raw = ser.readline()
            except Exception as e:
                # 장치 분리/리셋 — 스레드를 죽이지 않고 핸들만 정리 후 재연결 루프로 진입
                if self._capturing:
                    logger.warning("[SerialLogging] read failed (%s) — reconnecting to %s",
                                   e, self._port)
                    self._emit_marker("serial disconnected — reconnecting")
                self._safe_close_serial()
                continue

            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            cap_ts = time.time()
            ts = time.strftime("%H:%M:%S", time.localtime(cap_ts))
            stamped = f"[{ts}] {line}"

            with self._lock:
                self._logs.append(stamped)
                self._log_capture_ts.append(cap_ts)
                self._line_counter += 1

            # 파일 저장 중이면 기록
            if self._save_file:
                try:
                    self._save_file.write(stamped + "\n")
                    self._save_file.flush()
                except Exception:
                    pass

            # 뷰어용 실시간 스트림으로 emit
            try:
                SERIAL_HUB.emit_log(self._session_id(), stamped)
            except Exception:
                pass

        self._capturing = False
        logger.info("[SerialLogging] Capture loop ended (logs=%d)", len(self._logs))

    # ------------------------------------------------------------------
    # 캡처 자가복구 헬퍼 (장치 off/on 시 재연결)
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float) -> None:
        """self._capturing이 False가 되면 즉시 깨어나는 분할 sleep.

        _stop_capture()의 join(timeout=3)이 재연결 backoff 대기 때문에 지연되지
        않도록, 0.1초 단위로 쪼개 종료 플래그를 확인한다.
        """
        end = time.time() + max(0.0, seconds)
        while self._capturing and time.time() < end:
            time.sleep(0.1)

    def _safe_close_serial(self) -> None:
        """끊긴 시리얼 핸들을 조용히 닫고 self._serial을 비운다 (raise 안 함)."""
        ser = self._serial
        self._serial = None
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass

    def _reconnect_serial(self) -> bool:
        """self._port를 다시 open. 성공 시 self._serial 설정 후 True.

        **캡처 버퍼(self._logs)는 비우지 않는다** — 장치가 다시 올라오면 기존 로그에
        이어서 기록하기 위함. 입력 버퍼만 비워 off 구간의 가비지를 버린다.
        """
        if not self._port:
            return False
        try:
            import serial as pyserial
            ser = pyserial.Serial(self._port, self._bps, timeout=1)
        except Exception as e:
            logger.debug("[SerialLogging] reconnect to %s failed: %s", self._port, e)
            return False
        try:
            ser.reset_input_buffer()
            ser.reset_output_buffer()
        except Exception:
            pass
        self._serial = ser
        logger.info("[SerialLogging] Reconnected to %s @ %d — resuming capture",
                    self._port, self._bps)
        self._emit_marker(f"serial reconnected to {self._port}")
        return True

    def _emit_marker(self, text: str) -> None:
        """끊김/재연결 구간을 버퍼·파일·뷰어 스트림에 마커 라인 1건으로 남긴다."""
        cap_ts = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(cap_ts))
        stamped = f"[{ts}] --- {text} ---"
        with self._lock:
            self._logs.append(stamped)
            self._log_capture_ts.append(cap_ts)
            self._line_counter += 1
        if self._save_file:
            try:
                self._save_file.write(stamped + "\n")
                self._save_file.flush()
            except Exception:
                pass
        try:
            SERIAL_HUB.emit_log(self._session_id(), stamped)
        except Exception:
            pass
