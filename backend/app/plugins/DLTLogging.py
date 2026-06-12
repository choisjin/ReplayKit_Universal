"""DLTLogging — DLT 데몬 TCP 연결로 로그 캡처·저장·키워드 판정 모듈.

DLT Viewer GUI 없이 시나리오 스텝 내에서:
  - DLT 데몬에 TCP 직접 연결하여 실시간 로그 수신
  - 로그를 파일로 저장 (시작/중단)
  - 키워드 검색으로 PASS/FAIL 판정
  - 스텝 인덱스 구간 지정 검색

저장 구조 (스트리밍 정본):
  - StartSave/StartLogging 모두 캡처 시작과 동시에 모든 라인을 텍스트 파일에
    즉시 기록한다 — 파일이 로그 정본.
  - 메모리는 최근 _RING_MAX줄 링버퍼만 유지 (장시간 로깅 메모리 증가 방지).
  - 전체/구간 검색(SearchAll/SearchRange 등)은 링버퍼에 없는 과거분을
    스트림 파일에서 읽는다 (절대 인덱스 = 파일 라인 번호).
  - StopLogging(save_path)은 일괄 덤프 대신 스트림 파일 이동만 수행.

사용 예 (시나리오 스텝):
  DLTLogging.StartSave("C:/logs/test.log")      # 연결 + 캡처 + 파일 저장 시작
  DLTLogging.MarkStep(1)                        # 스텝 1 경계 표시
  ... (다른 스텝들) ...
  DLTLogging.MarkStep(5)                        # 스텝 5 경계 표시
  DLTLogging.SearchAll("BootComplete")          # 전체 로그에서 키워드 판정
  DLTLogging.SearchRange("ERROR", 1, 5)         # 스텝 1~5 구간 키워드 판정
  DLTLogging.StopSave()                         # 파일 저장 중단 + 연결 해제
"""

import logging
import os
import queue
import shutil
import socket
import struct
import threading
import time
from collections import deque
from pathlib import Path
from typing import Iterator, Optional

logger = logging.getLogger(__name__)

# 메모리 링버퍼 최대 라인 수 — 전체 로그의 정본은 스트림 파일이며, 메모리는
# 최근 N줄만 유지한다 (장시간 로깅 시 무한 메모리 증가 → 전체 성능 저하 방지).
_RING_MAX = 100_000


# ==========================================================================
# DLT 뷰어용 Pub/Sub 허브 — 기존 StartSave/StopSave/SearchRange는 그대로 두고,
# 뷰어 연동은 신규 StartLogging/StopLogging/SearchSection 경로만 사용한다.
# ==========================================================================

class _DLTHub:
    """DLT 로깅 세션 + 로그 스트림 구독자 관리 (thread-safe)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}            # session_id -> session info
        self._lifecycle_subs: list[queue.Queue] = []    # 전역 lifecycle 구독
        self._log_subs: dict[str, list[queue.Queue]] = {}  # session_id -> [Queue]

    # --- Session registry ---------------------------------------------------

    def list_sessions(self) -> list[dict]:
        with self._lock:
            return [{"session_id": sid, **info} for sid, info in self._sessions.items()]

    def emit_lifecycle(self, event: dict) -> None:
        """session_started / session_stopped 이벤트를 전파."""
        sid = event.get("session_id", "")
        etype = event.get("type", "")
        with self._lock:
            if etype == "session_started" and sid:
                self._sessions[sid] = {k: v for k, v in event.items() if k not in ("type",)}
            elif etype == "session_stopped" and sid:
                self._sessions.pop(sid, None)
            subs = list(self._lifecycle_subs)
        logger.info("[DLT_HUB] emit_lifecycle type=%s sid=%s subscribers=%d",
                    etype, sid, len(subs))
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    # --- Lifecycle subscription (for /ws/dlt-lifecycle) ---------------------

    def register_lifecycle(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=1000)
        with self._lock:
            self._lifecycle_subs.append(q)
            # 현재 활성 세션을 backfill로 즉시 전달
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

    # --- Log stream subscription (for /ws/dlt/{session_id}) -----------------

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
                # 구독자가 소비를 못 따라오면 최신 라인을 버림 — 스트리밍 끊김 방지
                pass


DLT_HUB = _DLTHub()


def get_active_session(session_id: str) -> Optional["DLTLogging"]:
    """session_id(host:port)에 대응하는 현재 활성 DLTLogging 인스턴스를 반환.
    module_service 싱글톤을 역참조하여 찾는다.
    """
    try:
        from backend.app.services.module_service import _instances
    except Exception:
        return None
    inst = _instances.get("DLTLogging")
    if not inst:
        return None
    if f"{getattr(inst, '_host', '')}:{getattr(inst, '_port', 0)}" == session_id:
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


def _auto_save_path(prefix: str = "dlt") -> str:
    """컨텍스트별 자동 저장 경로 생성.

    - 시나리오 재생 중: {run_output_dir}/logs/{prefix}_{timestamp}.log
    - 스텝 테스트 (재생 중 아님): backend/results/Temp_logs/{prefix}_{timestamp}.log
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


# DLT 프로토콜 상수
_MSG_TYPE = {0: "LOG", 1: "TRACE", 2: "NW", 3: "CTRL"}
_LOG_LEVEL = {0: "", 1: "FATAL", 2: "ERROR", 3: "WARN", 4: "INFO", 5: "DEBUG", 6: "VERBOSE"}
_TYLE_BYTES = {1: 1, 2: 2, 3: 4, 4: 8, 5: 16}


class DLTLogging:
    """DLT 로그 캡처·저장·키워드 판정 모듈.

    생성자:
        host: DLT 데몬 IP 주소
        port: DLT 데몬 TCP 포트 (기본 3490)
    """

    def __init__(self, host: str = "", port: int = 3490):
        self._host = host
        self._port = int(port)
        self._socket: Optional[socket.socket] = None
        self._capture_thread: Optional[threading.Thread] = None
        self._capturing = False
        self._lock = threading.Lock()
        self._recv_buffer = bytearray()

        # 로그 링버퍼 (최근 _RING_MAX줄) + 라인별 capture timestamp (epoch float).
        # 전체 로그의 정본은 스트림 파일(_stream_path) — 캡처 시작과 동시에 모든
        # 라인을 파일에 즉시 기록하고, 메모리는 최근분만 유지한다.
        # 절대 인덱스 규약: 파일의 i번째 라인 = 절대 인덱스 i.
        #   링버퍼의 시작 절대 인덱스 = _total_count - len(_logs)
        self._logs: deque[str] = deque(maxlen=_RING_MAX)
        self._log_capture_ts: deque[float] = deque(maxlen=_RING_MAX)
        self._msg_counter = 0
        self._total_count = 0   # 캡처 시작 이후 누적 라인 수 (= 스트림 파일 라인 수)
        self._clear_base = 0    # ClearLogs 이후 논리적 시작 절대 인덱스

        # 파일 저장 (스트리밍 정본)
        self._save_file = None
        self._save_path: Optional[str] = None
        self._stream_path: Optional[str] = None  # 정본 파일 경로 (close/move 후에도 유지)

        # 스텝 마킹: {step_index: log_buffer_index}
        self._step_marks: dict[int, int] = {}

        # 키워드 실시간 카운터 — SerialLogging과 동일 패턴
        # {name: {"keyword", "count", "timestamps", "started_at"}}
        self._counters: dict[str, dict] = {}
        self._counter_lock = threading.Lock()

        # 키워드 단언(assert): 미일치 라인 fail 누적 보고용
        self._asserts: dict[str, dict] = {}

        # fail_on_keyword: keyword가 라인에 포함되면 fail 보고 (assert의 반대)
        self._fail_keywords: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # 연결 관리 (내부)
    # ------------------------------------------------------------------

    def _connect(self) -> str:
        """DLT 데몬에 TCP 연결 후 로그 캡처를 시작."""
        if not self._host:
            return "ERROR: host가 설정되지 않았습니다"
        if self._socket:
            return ""  # 이미 연결됨 — 정상

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((self._host, self._port))
            sock.settimeout(1)
            self._socket = sock
            self._recv_buffer.clear()
            self._logs.clear()
            self._log_capture_ts.clear()
            self._msg_counter = 0
            self._total_count = 0
            self._clear_base = 0
            self._stream_path = None
            self._step_marks.clear()
            with self._counter_lock:
                self._counters.clear()  # 새 세션마다 키워드 카운터 자동 리셋
                self._asserts.clear()
                self._fail_keywords.clear()
            self._start_capture()
            logger.info("[DLTLogging] Connected to %s:%d", self._host, self._port)
            return ""
        except Exception as e:
            self._socket = None
            logger.error("[DLTLogging] Connection failed: %s", e)
            return f"ERROR: 연결 실패 — {e}"

    def _disconnect(self):
        """DLT 데몬 연결 해제. cleanup 경로에서 호출되므로 어떤 단계도 raise 하지 않는다."""
        try:
            self._stop_capture()
        except Exception as e:
            logger.warning("[DLTLogging] stop_capture raised: %s", e)
        if self._socket is not None:
            try:
                self._socket.close()
            except Exception as e:
                logger.warning("[DLTLogging] socket.close raised: %s", e)
            self._socket = None
        logger.info("[DLTLogging] Disconnected")

    def IsConnected(self) -> bool:
        """연결 상태 확인. StartSave 전에도 모듈은 사용 가능 (지연 연결)."""
        return True

    # ------------------------------------------------------------------
    # 스트림 파일 (로그 정본) 헬퍼
    # ------------------------------------------------------------------

    def _open_stream_file(self, save_path: str) -> str:
        """스트림 파일을 연다. 성공 시 "", 실패 시 에러 메시지."""
        try:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            self._save_file = open(save_path, "w", encoding="utf-8")
            self._save_path = save_path
            self._stream_path = save_path
            return ""
        except Exception as e:
            return f"ERROR: 파일 열기 실패 — {e}"

    def _iter_abs_range(self, start_abs: int, end_abs: int) -> Iterator[str]:
        """절대 인덱스 [start_abs, end_abs) 라인을 순서대로 yield.

        링버퍼에 남아 있는 구간은 메모리에서, 밀려난 과거 구간은 스트림 파일
        (정본)에서 읽는다. 파일이 없으면 링버퍼 가용분만 반환한다.
        검색류 함수가 전체 로그를 메모리에 복사하지 않고 스트리밍 소비하기 위한 통로.
        """
        with self._lock:
            total = self._total_count
            base = total - len(self._logs)
            end_abs = min(int(end_abs), total)
            start_abs = max(0, int(start_abs))
            if start_abs >= end_abs:
                return
            if start_abs >= base:
                ring_snap = list(self._logs)[start_abs - base:end_abs - base]
                file_range = None
            else:
                ring_snap = list(self._logs)[:max(0, end_abs - base)]
                file_range = (start_abs, min(end_abs, base))
            path = self._stream_path
        if file_range:
            fstart, fend = file_range
            if path and os.path.exists(path):
                try:
                    with open(path, "r", encoding="utf-8", errors="replace") as f:
                        for i, raw in enumerate(f):
                            if i < fstart:
                                continue
                            if i >= fend:
                                break
                            yield raw.rstrip("\n")
                except Exception as e:
                    logger.warning("[DLTLogging] stream file read failed (%s): %s", path, e)
            else:
                logger.warning("[DLTLogging] stream file missing for range %d~%d", fstart, fend)
        for line in ring_snap:
            yield line

    # ------------------------------------------------------------------
    # 로그 저장 시작/중단 (연결 포함)
    # ------------------------------------------------------------------

    def StartSave(self, save_path: str = "") -> str:
        """DLT 데몬에 연결하고 로그 캡처 + 파일 저장을 시작합니다.

        Args:
            save_path: 저장 파일 경로. 빈 값이면 자동 생성 (backend/logs/dlt_YYYYMMDD_HHMMSS.log)

        Returns:
            결과 메시지

        StartLogging과 동일하게 DLT_HUB에 session_started를 emit하여 뷰어가 자동 오픈된다.
        """
        if self._save_file:
            return f"ERROR: 이미 저장 중입니다 ({self._save_path}). StopSave() 먼저 호출하세요."

        # 연결이 안 되어 있으면 자동 연결
        err = self._connect()
        if err:
            return err

        if not save_path:
            # 재생 중: run_dir/logs, 스텝 테스트: backend/results/Temp_logs
            save_path = _auto_save_path("dlt")

        err = self._open_stream_file(save_path)
        if err:
            return err
        logger.info("[DLTLogging] Save started: %s", save_path)

        # 뷰어 동기화: StartLogging과 동일한 lifecycle emit
        try:
            DLT_HUB.emit_lifecycle({
                "type": "session_started",
                "session_id": self._session_id(),
                "host": self._host,
                "port": self._port,
                "save_path": save_path,
                "started_at": time.time(),
                "scenario_playback": _is_scenario_playback(),
            })
        except Exception as e:
            logger.warning("[DLTLogging] StartSave lifecycle emit failed: %s", e)

        return f"Save started: {save_path}"

    def StopSave(self) -> str:
        """로그 파일 저장을 중단하고 DLT 연결을 해제합니다.

        Returns:
            저장된 파일 경로
        """
        if not self._save_file:
            return "저장 중이 아닙니다."

        path = self._save_path
        sid = self._session_id()
        self._close_save_file()
        self._disconnect()
        logger.info("[DLTLogging] Save stopped + disconnected: %s", path)

        # 뷰어 동기화: StopLogging과 동일한 lifecycle emit
        try:
            DLT_HUB.emit_lifecycle({
                "type": "session_stopped",
                "session_id": sid,
                "save_path": path or "",
                "stopped_at": time.time(),
            })
        except Exception as e:
            logger.warning("[DLTLogging] StopSave lifecycle emit failed: %s", e)

        return f"Save stopped: {path}"

    # ------------------------------------------------------------------
    # 뷰어 연동 신규 API — StartLogging / StopLogging / SearchSection
    # 기존 StartSave/StopSave/SearchRange와 동일 동작 + DLT_HUB에 이벤트 전파.
    # ------------------------------------------------------------------

    def _session_id(self) -> str:
        return f"{self._host}:{self._port}"

    def StartLogging(self) -> str:
        """뷰어 연동용: DLT 연결 + 로그 캡처 시작 (파일 즉시 스트리밍 저장).

        모든 라인을 수신 즉시 텍스트 파일(자동 경로)에 기록한다 — 파일이 로그
        정본이며, 메모리는 최근 {_RING_MAX}줄 링버퍼만 유지해 장시간 로깅에도
        느려지지 않는다. StopLogging(save_path)은 일괄 덤프 대신 스트림 파일을
        지정 경로로 이동만 한다.
        DLT_HUB에 session_started 이벤트를 emit하여 뷰어가 자동 오픈된다.

        Returns:
            결과 메시지
        """
        err = self._connect()
        if err:
            return err
        # 스트림 파일 즉시 오픈 (StartSave로 이미 저장 중이면 그 파일을 그대로 사용)
        if not self._save_file:
            err = self._open_stream_file(_auto_save_path("dlt"))
            if err:
                return err
            logger.info("[DLTLogging] StartLogging streaming to: %s", self._save_path)
        DLT_HUB.emit_lifecycle({
            "type": "session_started",
            "session_id": self._session_id(),
            "host": self._host,
            "port": self._port,
            "save_path": self._save_path or "",
            "started_at": time.time(),
            "scenario_playback": _is_scenario_playback(),
        })
        return f"Logging started: {self._host}:{self._port} (streaming to {self._save_path})"

    def StopLogging(self, save_path: str = "") -> str:
        """뷰어 연동용: DLT 연결 종료. 로그는 이미 스트림 파일에 실시간 기록됨.

        메모리 일괄 덤프 대신, 캡처 중 스트리밍 기록된 파일을 닫고 save_path가
        지정되면 그 위치로 이동(move)만 한다 — 장시간 로깅에서도 종료가 즉시 끝난다.

        Args:
            save_path: 저장할 파일 경로. 빈 값이면 스트림 파일(자동 경로) 그대로 유지:
                - 시나리오 재생 중: {run_dir}/logs/dlt_{timestamp}.log
                - 스텝 테스트:     backend/results/Temp_logs/dlt_{timestamp}.log

        파일 처리 단계의 어떤 예외(경로 해석/mkdir/move)가 발생해도 finally에서
        _close_save_file + _disconnect를 무조건 실행하여 소켓 leak을 방지한다.
        cleanup_active_instances가 재생 중단 시 자동 호출하는 진입점이기도 하다.

        Returns:
            결과 메시지 (저장 경로 포함)
        """
        sid = self._session_id()
        with self._lock:
            total_lines = self._total_count

        saved_path = ""
        save_error = ""
        try:
            # 캡처/파일을 먼저 정리해야 move 가능 (Windows: 열린 파일 이동 불가)
            self._stop_capture()
            self._close_save_file()
            stream_path = self._stream_path

            if not stream_path or not os.path.exists(stream_path):
                # 스트림 파일이 없는 비정상 케이스 — 링버퍼 잔여분이라도 덤프
                with self._lock:
                    logs_snapshot = list(self._logs)
                if not save_path:
                    save_path = _auto_save_path("dlt")
                elif not os.path.dirname(save_path):
                    save_path = str(Path(_auto_save_path("dlt")).parent / save_path)
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(logs_snapshot))
                    if logs_snapshot:
                        f.write("\n")
                saved_path = save_path
                total_lines = len(logs_snapshot)
                logger.info("[DLTLogging] Saved %d lines (ring fallback) to %s",
                            total_lines, save_path)
            elif save_path:
                # save_path 해석: 파일명만 → 자동 디렉토리 하위
                if not os.path.dirname(save_path):
                    save_path = str(Path(_auto_save_path("dlt")).parent / save_path)
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                if os.path.abspath(save_path) != os.path.abspath(stream_path):
                    shutil.move(stream_path, save_path)
                self._stream_path = save_path
                saved_path = save_path
                logger.info("[DLTLogging] Stream file moved: %s → %s (%d lines)",
                            stream_path, save_path, total_lines)
            else:
                saved_path = stream_path
                logger.info("[DLTLogging] Stream file kept: %s (%d lines)",
                            stream_path, total_lines)
        except Exception as e:
            logger.error("[DLTLogging] StopLogging save handling failed: %s", e)
            save_error = str(e)
        finally:
            # StartSave로 시작했더라도 _save_file 누수 방지 + 캡처/소켓 무조건 정리
            self._close_save_file()
            self._disconnect()
            try:
                DLT_HUB.emit_lifecycle({
                    "type": "session_stopped",
                    "session_id": sid,
                    "save_path": saved_path,
                    "stopped_at": time.time(),
                })
            except Exception:
                pass

        if save_error:
            return f"ERROR: 저장 실패 — {save_error}"
        return f"Logging stopped. Saved {total_lines} lines to: {saved_path}"

    def SearchSection(self, keyword: str, from_step: int, to_step: int, count: int = 5) -> str:
        """뷰어 연동용: MarkStep 구간 검색. SearchRange의 alias."""
        return self.SearchRange(keyword, from_step, to_step, count)

    def WatchAndStop(self, keyword: str, save_path: str = "",
                     interval_ms: int = 500,
                     max_checks: int = 0,
                     timeout_sec: int = 0) -> str:
        """StartLogging 중에 키워드를 주기적으로 감시하다 발견 시:
        - 해당 키워드가 나타난 라인까지의 로그를 save_path에 저장
        - DLT 연결 종료 + 뷰어에 session_stopped emit
        블로킹 함수. 시나리오 스텝에서 호출하면 발견 또는 한도 도달까지 대기.

        Args:
            keyword: 감시할 키워드 (공백=AND)
            save_path: 저장 파일 경로. 빈 값이면 매칭 시에도 저장 없이 종료만.
            interval_ms: 체크 주기 (ms). 기본 500ms.
            max_checks: 최대 체크 횟수. 0이면 제한 없음.
            timeout_sec: 총 대기 시간 (초). 0이면 제한 없음.
                (둘 다 0이면 기본 300초 타임아웃 적용 — 무한 블록 방지)

        Returns:
            "PASS: ..." 발견 시
            "FAIL: ..." 한도 도달 시 (현재까지의 로그는 저장)
        """
        if not self._capturing:
            return "ERROR: StartLogging이 실행 중이 아닙니다."

        keywords = keyword.split() if keyword else []
        if not keywords:
            return "ERROR: keyword가 비어있습니다."

        if max_checks <= 0 and timeout_sec <= 0:
            timeout_sec = 300  # 안전장치: 최대 5분

        interval = max(0.05, float(interval_ms) / 1000.0)
        start_time = time.time()
        check_count = 0
        scan_pos = self._clear_base  # 다음 검사 시 시작할 절대 인덱스

        while True:
            check_count += 1

            # 새로 추가된 라인만 검사 — O(delta). 폴링 주기 내 신규분은 링버퍼에 있음.
            with self._lock:
                total_len = self._total_count

            matched_idx = -1
            for i, line in enumerate(self._iter_abs_range(scan_pos, total_len)):
                if all(k in line for k in keywords):
                    matched_idx = scan_pos + i
                    break

            # 이 회차에서 본 범위 업데이트
            scan_pos = total_len

            if matched_idx >= 0:
                logger.info("[DLTLogging] WatchAndStop MATCH '%s' at idx=%d (check %d)",
                            keyword, matched_idx, check_count)
                return self._watch_save_and_stop(
                    matched_idx + 1, save_path, keyword, check_count, matched=True,
                )

            elapsed = time.time() - start_time
            if timeout_sec > 0 and elapsed >= timeout_sec:
                logger.info("[DLTLogging] WatchAndStop TIMEOUT '%s' after %.1fs (check %d)",
                            keyword, elapsed, check_count)
                return self._watch_save_and_stop(
                    total_len, save_path, keyword, check_count,
                    matched=False, reason="timeout",
                )
            if max_checks > 0 and check_count >= max_checks:
                logger.info("[DLTLogging] WatchAndStop EXHAUSTED '%s' after %d checks",
                            keyword, check_count)
                return self._watch_save_and_stop(
                    total_len, save_path, keyword, check_count,
                    matched=False, reason="max_checks",
                )

            time.sleep(interval)

    def _watch_save_and_stop(self, end_abs: int, save_path: str, keyword: str,
                             check_count: int, matched: bool, reason: str = "") -> str:
        """WatchAndStop 종료 처리 — 정본(스트림 파일/링버퍼)에서 end_abs까지 저장
        + 연결 해제 + lifecycle emit.

        save_path 해석:
          - 빈 값:           컨텍스트별 자동 경로 + 자동 파일명
            (재생: {run_dir}/logs/dlt_watch_{ts}.log, 스텝 테스트: backend/results/Temp_logs/...)
          - 파일명만 지정:   자동 디렉토리 하위에 해당 파일명으로 저장
          - 절대/상대 경로:  지정 경로 그대로 사용
        """
        sid = self._session_id()
        if not save_path:
            save_path = _auto_save_path("dlt_watch")
        elif not os.path.dirname(save_path):
            # 파일명만 주어진 경우 → 컨텍스트별 자동 디렉토리 하위로 라우팅
            base_dir = Path(_auto_save_path("dlt_watch")).parent
            save_path = str(base_dir / save_path)
        saved = ""
        lines = 0
        start_abs = self._clear_base
        try:
            os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
            with open(save_path, "w", encoding="utf-8") as f:
                for line in self._iter_abs_range(start_abs, end_abs):
                    f.write(line + "\n")
                    lines += 1
            saved = save_path
        except Exception as e:
            logger.error("[DLTLogging] WatchAndStop save failed: %s", e)
            lines = max(0, end_abs - start_abs)

        self._close_save_file()
        self._disconnect()
        DLT_HUB.emit_lifecycle({
            "type": "session_stopped",
            "session_id": sid,
            "save_path": saved,
            "stopped_at": time.time(),
        })

        tail = f"saved {lines} lines to {saved}" if saved else f"{lines} lines (save failed)"
        if matched:
            return f"PASS: '{keyword}' found after {check_count} checks — {tail}"
        if reason == "timeout":
            return f"FAIL: '{keyword}' not found within timeout ({check_count} checks) — {tail}"
        return f"FAIL: '{keyword}' not found after {check_count} checks — {tail}"

    def GetRecentLogs(self, limit: int = 1000) -> list[str]:
        """뷰어 backfill용 — 현재까지 캡처된 로그의 마지막 N줄 (링버퍼 내)."""
        with self._lock:
            logs = list(self._logs)
        if limit <= 0:
            return logs
        return logs[-int(limit):]

    def GetStepMarks(self) -> dict[int, int]:
        """뷰어 UI용 — 현재 스텝 마킹 위치 dict."""
        return dict(self._step_marks)

    def SearchAllDetailed(self, keyword: str, max_results: int = 500) -> list[str]:
        """뷰어 검색바용 — 매칭된 로그 라인 목록을 반환 (정본=스트림 파일 기준)."""
        keywords = keyword.split() if keyword else []
        if not keywords:
            return []
        with self._lock:
            total = self._total_count
        out: list[str] = []
        for line in self._iter_abs_range(self._clear_base, total):
            if all(k in line for k in keywords):
                out.append(line)
                if len(out) >= int(max_results):
                    break
        return out

    def SearchSectionDetailed(self, keyword: str, from_step: int,
                              to_step: int, max_results: int = 500) -> list[str]:
        """뷰어 검색바용 — 스텝 구간 내 매칭 로그 라인 목록 반환 (정본 기준)."""
        keywords = keyword.split() if keyword else []
        if not keywords:
            return []
        from_step = int(from_step)
        to_step = int(to_step)
        if from_step not in self._step_marks:
            return []
        start_idx = self._step_marks[from_step]
        if to_step in self._step_marks:
            end_idx = self._step_marks[to_step]
        else:
            with self._lock:
                end_idx = self._total_count
        out: list[str] = []
        for line in self._iter_abs_range(start_idx, end_idx):
            if all(k in line for k in keywords):
                out.append(line)
                if len(out) >= int(max_results):
                    break
        return out

    def _close_save_file(self):
        if self._save_file:
            try:
                self._save_file.close()
            except Exception:
                pass
            self._save_file = None
            self._save_path = None

    # ------------------------------------------------------------------
    # 스텝 마킹
    # ------------------------------------------------------------------

    def MarkStep(self, step_index: int) -> str:
        """현재 로그 버퍼 위치에 스텝 경계를 표시합니다.

        SearchRange에서 구간 검색할 때 사용됩니다.

        Args:
            step_index: 스텝 인덱스 번호

        Returns:
            결과 메시지
        """
        with self._lock:
            pos = self._total_count  # 절대 인덱스 (= 스트림 파일 라인 위치)
        self._step_marks[int(step_index)] = pos
        logger.info("[DLTLogging] MarkStep %d at log index %d", step_index, pos)
        return f"Step {step_index} marked at log index {pos}"

    # ------------------------------------------------------------------
    # 실시간 키워드 카운터 — 호출 시점부터 활성, 매 라인 검사
    # ------------------------------------------------------------------

    def count_keyword(self, keyword: str, name: str = "") -> str:
        """캡처 로그에서 keyword가 등장할 때마다 카운트하고 발생 시간을 누적합니다.

        - 같은 name(또는 keyword)으로 첫 호출: 카운터 시작 (count=0)
        - 같은 name 재호출: 현재까지의 결과 반환
        SerialLogging.count_keyword와 동일 인터페이스.
        """
        key = name.strip() if name else keyword
        with self._counter_lock:
            existing = self._counters.get(key)
            if existing is None:
                self._counters[key] = {
                    "keyword": keyword,
                    "count": 0,
                    "timestamps": [],
                    "started_at": time.time(),
                }
                logger.info("[DLTLogging] count_keyword started: name='%s' keyword='%s'", key, keyword)
                return f"Started counting '{keyword}' (name='{key}')"
            cnt = existing["count"]
            ts_list = list(existing["timestamps"])
            started_at = existing["started_at"]
            kw = existing["keyword"]

        def _fmt(t: float) -> str:
            return time.strftime("%H:%M:%S", time.localtime(t))

        if cnt == 0:
            return f"COUNT '{kw}' (name='{key}'): 0 occurrences (since {_fmt(started_at)})"
        first_s = _fmt(ts_list[0])
        last_s = _fmt(ts_list[-1])
        logger.info("[DLTLogging] count_keyword query: name='%s' count=%d", key, cnt)
        return f"COUNT '{kw}' (name='{key}'): {cnt} occurrences | first: {first_s} | last: {last_s}"

    def reset_count_keyword(self, name: str = "") -> str:
        """키워드 카운터를 리셋합니다. name 빈 값이면 모든 카운터 제거."""
        with self._counter_lock:
            if not name:
                n = len(self._counters)
                self._counters.clear()
                return f"Reset all counters ({n})"
            existing = self._counters.get(name)
            if existing is None:
                return f"Counter '{name}' not found"
            existing["count"] = 0
            existing["timestamps"].clear()
            existing["started_at"] = time.time()
            return f"Reset counter '{name}'"

    def get_count_details(self, name: str = "") -> dict:
        """raw 카운터 dict 반환. timestamps는 epoch float."""
        with self._counter_lock:
            if not name:
                return {k: {**v, "timestamps": list(v["timestamps"])} for k, v in self._counters.items()}
            v = self._counters.get(name)
            if v is None:
                return {}
            return {**v, "timestamps": list(v["timestamps"])}

    # ------------------------------------------------------------------
    # 키워드 단언(assert) — 일치하지 않는 라인을 시나리오 fail로 누적 보고
    # ------------------------------------------------------------------

    def assert_keyword(self, keyword: str, name: str = "") -> str:
        """캡처되는 모든 라인이 keyword를 포함해야 함을 단언.
        keyword 미포함 라인이 들어오면 시나리오 재생 중일 때 한해 fail step row 자동 추가.
        SerialLogging.assert_keyword와 동일 인터페이스.
        """
        key = name.strip() if name else f"assert_{keyword}"
        with self._counter_lock:
            existing = self._asserts.get(key)
            if existing is None:
                self._asserts[key] = {
                    "keyword": keyword,
                    "miss_count": 0,
                    "miss_timestamps": [],
                    "started_at": time.time(),
                }
                logger.info("[DLTLogging] assert_keyword started: name='%s' keyword='%s'", key, keyword)
                return f"Asserting all lines contain '{keyword}' (name='{key}')"
            cnt = existing["miss_count"]
            ts_list = list(existing["miss_timestamps"])
            started_at = existing["started_at"]
            kw = existing["keyword"]

        def _fmt(t: float) -> str:
            return time.strftime("%H:%M:%S", time.localtime(t))

        if cnt == 0:
            return f"ASSERT '{kw}' (name='{key}'): 0 misses (since {_fmt(started_at)})"
        return f"ASSERT '{kw}' (name='{key}'): {cnt} miss lines | first: {_fmt(ts_list[0])} | last: {_fmt(ts_list[-1])}"

    def reset_assert_keyword(self, name: str = "") -> str:
        """assert 카운터 리셋. name 빈 값이면 모든 단언 제거."""
        with self._counter_lock:
            if not name:
                n = len(self._asserts)
                self._asserts.clear()
                return f"Reset all assertions ({n})"
            existing = self._asserts.get(name)
            if existing is None:
                return f"Assertion '{name}' not found"
            existing["miss_count"] = 0
            existing["miss_timestamps"].clear()
            existing["started_at"] = time.time()
            return f"Reset assertion '{name}'"

    # ------------------------------------------------------------------
    # 키워드 검출 모드 — 키워드가 들어오면 fail로 보고
    # ------------------------------------------------------------------

    def fail_on_keyword(self, keyword: str, time: float = 0, name: str = "") -> str:
        """캡처되는 라인에 keyword가 **포함되면** 시나리오 결과에 fail row 자동 누적.
        SerialLogging.fail_on_keyword와 동일 인터페이스. 'ERROR'/'crash' 등 검출용.
        첫 호출 시 backfill 스캔 — 이미 누적된 로그 라인의 매칭도 정확한 timestamp로 보고.

        모드:
          - **time > 0 (sync)**: 등록 + backfill → 해당 시간 동안 모니터링 → 자동 unregister.
            검출 fail은 이 스텝의 인라인 결과로 표시 (Fail_Count_N).
          - **time == 0 (legacy)**: 백그라운드 누적, 시나리오 종료까지 동작.
        """
        import time as _time_mod
        sync_duration = float(time) if time else 0.0
        parent_step_id: Optional[int] = None
        parent_repeat_index = 1
        if sync_duration > 0:
            try:
                from backend.app.services.playback_service import get_current_step_context
                parent_step_id, parent_repeat_index = get_current_step_context()
            except Exception:
                pass

        key = name.strip() if name else f"fail_{keyword}"
        backfill_reports: list[tuple[float, str]] = []
        is_new = False
        with self._counter_lock:
            existing = self._fail_keywords.get(key)
            if existing is None:
                is_new = True
                new_entry = {
                    "keyword": keyword,
                    "hit_count": 0,
                    "hit_timestamps": [],
                    "started_at": _time_mod.time(),
                    "parent_step_id": parent_step_id,
                    "parent_repeat_index": parent_repeat_index,
                }
                self._fail_keywords[key] = new_entry
                with self._lock:
                    logs_snapshot = list(self._logs)
                    ts_snapshot = list(self._log_capture_ts)
                for i, ln in enumerate(logs_snapshot):
                    if keyword in ln:
                        ts_b = ts_snapshot[i] if i < len(ts_snapshot) else _time_mod.time()
                        new_entry["hit_count"] += 1
                        new_entry["hit_timestamps"].append(ts_b)
                        backfill_reports.append((ts_b, ln))
                logger.info("[DLTLogging] fail_on_keyword started: name='%s' keyword='%s' backfill=%d sync=%.1fs parent=%s",
                            key, keyword, len(backfill_reports), sync_duration, parent_step_id)
            else:
                cnt = existing["hit_count"]
                ts_list = list(existing["hit_timestamps"])
                started_at = existing["started_at"]
                kw = existing["keyword"]
                if sync_duration > 0:
                    existing["parent_step_id"] = parent_step_id
                    existing["parent_repeat_index"] = parent_repeat_index

        if is_new and backfill_reports:
            try:
                from backend.app.services.playback_service import report_runtime_fail
                for ts_b, ln in backfill_reports:
                    report_runtime_fail(
                        "DLTLogging", keyword, ts_b, ln, reason="matched",
                        repeat_index=parent_repeat_index,
                        parent_step_id=parent_step_id,
                    )
            except Exception:
                pass

        if sync_duration > 0:
            _time_mod.sleep(sync_duration)
            with self._counter_lock:
                final_entry = self._fail_keywords.pop(key, None)
            final_cnt = final_entry["hit_count"] if final_entry else 0
            backfill_n = len(backfill_reports) if is_new else 0
            window_n = max(0, final_cnt - backfill_n)
            return (
                f"FAIL_ON '{keyword}' (name='{key}', time={sync_duration:g}s): "
                f"{final_cnt} hits (backfill={backfill_n}, window={window_n})"
            )

        if is_new:
            return (f"Failing on keyword '{keyword}' (name='{key}')"
                    + (f" — backfill matched {len(backfill_reports)} lines" if backfill_reports else ""))

        def _fmt(t: float) -> str:
            return _time_mod.strftime("%H:%M:%S", _time_mod.localtime(t))

        if cnt == 0:
            return f"FAIL_ON '{kw}' (name='{key}'): 0 hits (since {_fmt(started_at)})"
        return f"FAIL_ON '{kw}' (name='{key}'): {cnt} hit lines | first: {_fmt(ts_list[0])} | last: {_fmt(ts_list[-1])}"

    def reset_fail_on_keyword(self, name: str = "") -> str:
        """fail_on_keyword 검출 리셋."""
        with self._counter_lock:
            if not name:
                n = len(self._fail_keywords)
                self._fail_keywords.clear()
                return f"Reset all fail-on detectors ({n})"
            existing = self._fail_keywords.get(name)
            if existing is None:
                return f"Detector '{name}' not found"
            existing["hit_count"] = 0
            existing["hit_timestamps"].clear()
            existing["started_at"] = time.time()
            return f"Reset detector '{name}'"

    # ------------------------------------------------------------------
    # 키워드 검색 — PASS/FAIL 판정
    # ------------------------------------------------------------------

    def SearchAll(self, keyword: str, count: int = 5) -> str:
        """전체 로그에서 키워드를 검색하여 PASS/FAIL 판정합니다.

        처음부터 현재까지 캡처된 모든 로그를 대상으로 검색합니다.

        Args:
            keyword: 검색 키워드 (공백 구분 시 AND 조건)
            count: 최대 매칭 결과 수 (기본 5)

        Returns:
            "PASS: N건 발견 — (첫 매칭 로그)" 또는 "FAIL: keyword not found"
        """
        keywords = keyword.split()
        with self._lock:
            total = self._total_count

        matches = []
        for line in self._iter_abs_range(self._clear_base, total):
            if all(k in line for k in keywords):
                matches.append(line.strip())
                if len(matches) >= int(count):
                    break

        if matches:
            summary = matches[0][:120]
            logger.info("[DLTLogging] SearchAll PASS: '%s' → %d건", keyword, len(matches))
            return f"PASS: {len(matches)}건 발견 — {summary}"
        else:
            logger.info("[DLTLogging] SearchAll FAIL: '%s'", keyword)
            return f"FAIL: keyword '{keyword}' not found"

    def SearchRange(self, keyword: str, from_step: int, to_step: int, count: int = 5) -> str:
        """스텝 구간 내 로그에서 키워드를 검색하여 PASS/FAIL 판정합니다.

        MarkStep으로 표시된 구간만 대상으로 검색합니다.

        Args:
            keyword: 검색 키워드 (공백 구분 시 AND 조건)
            from_step: 시작 스텝 인덱스 (이 스텝 이후 로그부터)
            to_step: 종료 스텝 인덱스 (이 스텝까지의 로그)
            count: 최대 매칭 결과 수 (기본 5)

        Returns:
            "PASS: N건 발견 — (첫 매칭 로그)" 또는 "FAIL: keyword not found in step range"
        """
        from_step = int(from_step)
        to_step = int(to_step)

        if from_step not in self._step_marks:
            return f"ERROR: step {from_step}이 마킹되지 않았습니다. MarkStep({from_step})을 먼저 호출하세요."
        if to_step not in self._step_marks:
            # to_step이 마킹 안 되었으면 현재 끝까지
            with self._lock:
                end_idx = self._total_count
        else:
            end_idx = self._step_marks[to_step]

        start_idx = self._step_marks[from_step]
        keywords = keyword.split()

        matches = []
        for line in self._iter_abs_range(start_idx, end_idx):
            if all(k in line for k in keywords):
                matches.append(line.strip())
                if len(matches) >= int(count):
                    break

        if matches:
            summary = matches[0][:120]
            logger.info("[DLTLogging] SearchRange PASS: '%s' step %d~%d → %d건",
                        keyword, from_step, to_step, len(matches))
            return f"PASS: {len(matches)}건 발견 (step {from_step}~{to_step}) — {summary}"
        else:
            logger.info("[DLTLogging] SearchRange FAIL: '%s' step %d~%d", keyword, from_step, to_step)
            return f"FAIL: keyword '{keyword}' not found in step {from_step}~{to_step}"

    def WaitLog(self, keyword: str, timeout: int = 30) -> str:
        """키워드가 포함된 로그가 나타날 때까지 대기합니다 (블로킹).

        Args:
            keyword: 검색 키워드 (공백 구분 시 AND 조건)
            timeout: 최대 대기 시간 (초, 기본 30)

        Returns:
            "PASS: (매칭된 로그)" 또는 "FAIL: keyword not found within {timeout}s"
        """
        if not self._capturing:
            return "ERROR: 캡처가 실행 중이 아닙니다. Connect() 먼저 호출하세요."

        keywords = keyword.split()
        timeout_sec = float(timeout)
        start = time.time()
        check_abs = self._clear_base  # 절대 인덱스 — 새로 추가된 라인만 검사

        while time.time() - start < timeout_sec:
            with self._lock:
                total = self._total_count

            for line in self._iter_abs_range(check_abs, total):
                if all(k in line for k in keywords):
                    line = line.strip()
                    logger.info("[DLTLogging] WaitLog PASS: %s", line)
                    return f"PASS: {line}"

            check_abs = total
            time.sleep(0.3)

        logger.info("[DLTLogging] WaitLog FAIL: '%s' not found in %ds", keyword, timeout_sec)
        return f"FAIL: keyword '{keyword}' not found within {int(timeout_sec)}s"

    def ExpectFound(self, keyword: str, timeout: int = 60, max_retries: int = 5) -> str:
        """키워드가 나타날 때까지 전체 로그를 주기적으로 검색합니다.

        먼저 현재 버퍼를 즉시 검색하고, 없으면 (timeout / max_retries) 간격으로
        최대 max_retries회 재시도합니다.

        Args:
            keyword: 검색 키워드 (공백 구분 시 AND 조건)
            timeout: 총 대기 시간 (초, 기본 60)
            max_retries: 최대 재시도 횟수 (기본 5)

        Returns:
            "PASS: 발견 (N회차) — (매칭 로그)" 또는 "FAIL: keyword not found after N retries"
        """
        keywords = keyword.split()
        timeout_sec = float(timeout)
        max_retries = max(1, int(max_retries))
        interval = timeout_sec / max_retries

        for attempt in range(1, max_retries + 1):
            with self._lock:
                total = self._total_count

            for line in self._iter_abs_range(self._clear_base, total):
                if all(k in line for k in keywords):
                    summary = line.strip()[:120]
                    logger.info("[DLTLogging] ExpectFound PASS: '%s' → attempt %d/%d — %s",
                                keyword, attempt, max_retries, summary)
                    return f"PASS: 발견 ({attempt}회차) — {summary}"

            if attempt < max_retries:
                logger.info("[DLTLogging] ExpectFound: '%s' not found, retry %d/%d (next in %.1fs)",
                            keyword, attempt, max_retries, interval)
                time.sleep(interval)

        logger.info("[DLTLogging] ExpectFound FAIL: '%s' not found after %d retries (%.0fs)",
                    keyword, max_retries, timeout_sec)
        return f"FAIL: keyword '{keyword}' not found after {max_retries} retries ({int(timeout_sec)}s)"

    def ExpectNotFound(self, keyword: str, timeout: int = 60, max_retries: int = 5) -> str:
        """키워드가 끝까지 없는지 전체 로그를 주기적으로 확인합니다.

        먼저 현재 버퍼를 즉시 검색하고, 발견되면 즉시 FAIL.
        없으면 (timeout / max_retries) 간격으로 최대 max_retries회 재확인합니다.
        끝까지 없으면 PASS.

        Args:
            keyword: 검색 키워드 (공백 구분 시 AND 조건)
            timeout: 총 확인 시간 (초, 기본 60)
            max_retries: 최대 확인 횟수 (기본 5)

        Returns:
            "PASS: keyword not found after N checks" 또는 "FAIL: 발견 (N회차) — (매칭 로그)"
        """
        keywords = keyword.split()
        timeout_sec = float(timeout)
        max_retries = max(1, int(max_retries))
        interval = timeout_sec / max_retries

        for attempt in range(1, max_retries + 1):
            with self._lock:
                total = self._total_count

            for line in self._iter_abs_range(self._clear_base, total):
                if all(k in line for k in keywords):
                    summary = line.strip()[:120]
                    logger.info("[DLTLogging] ExpectNotFound FAIL: '%s' → found at attempt %d/%d — %s",
                                keyword, attempt, max_retries, summary)
                    return f"FAIL: 발견 ({attempt}회차) — {summary}"

            if attempt < max_retries:
                logger.info("[DLTLogging] ExpectNotFound: '%s' absent, check %d/%d (next in %.1fs)",
                            keyword, attempt, max_retries, interval)
                time.sleep(interval)

        logger.info("[DLTLogging] ExpectNotFound PASS: '%s' not found after %d checks (%.0fs)",
                    keyword, max_retries, timeout_sec)
        return f"PASS: keyword '{keyword}' not found after {max_retries} checks ({int(timeout_sec)}s)"

    # ------------------------------------------------------------------
    # 상태 조회
    # ------------------------------------------------------------------

    def GetStatus(self) -> str:
        """현재 모듈 상태를 조회합니다.

        Returns:
            상태 문자열
        """
        connected = self._socket is not None
        with self._lock:
            log_count = self._total_count - self._clear_base
        saving = self._save_path or self._stream_path or "N/A"
        marks = ", ".join(f"{k}:{v}" for k, v in sorted(self._step_marks.items()))

        parts = [
            f"Host: {self._host}:{self._port}",
            f"Connected: {connected}",
            f"Capturing: {self._capturing}",
            f"Logs: {log_count} (total: {self._msg_counter})",
            f"Saving: {saving}",
            f"StepMarks: {marks or 'none'}",
        ]
        return " | ".join(parts)

    def ClearLogs(self) -> str:
        """로그 버퍼와 스텝 마킹을 초기화합니다.

        Returns:
            결과 메시지
        """
        with self._lock:
            self._logs.clear()
            self._log_capture_ts.clear()
            # 절대 인덱스는 유지 (스트림 파일 라인 위치와 1:1) — 논리적 시작점만 이동
            self._clear_base = self._total_count
        self._msg_counter = 0
        self._step_marks.clear()
        return "Logs and step marks cleared"

    # ------------------------------------------------------------------
    # 로그 캡처 (백그라운드 스레드)
    # ------------------------------------------------------------------

    def _start_capture(self):
        if self._capturing:
            return
        self._capturing = True
        self._capture_thread = threading.Thread(
            target=self._capture_loop, name="DLTLogging-Capture", daemon=True
        )
        self._capture_thread.start()

    def _stop_capture(self):
        self._capturing = False
        if self._capture_thread:
            self._capture_thread.join(timeout=3)
            self._capture_thread = None

    def _capture_loop(self):
        """백그라운드 스레드: DLT 메시지 수신 및 파싱."""
        while self._capturing and self._socket:
            try:
                data = self._socket.recv(65536)
                if not data:
                    logger.warning("[DLTLogging] Connection closed by remote")
                    break
                self._recv_buffer.extend(data)
                self._process_buffer()
            except socket.timeout:
                continue
            except OSError:
                break
            except Exception as e:
                logger.error("[DLTLogging] Capture error: %s", e)
                break

        self._capturing = False
        logger.info("[DLTLogging] Capture loop ended (logs=%d)", len(self._logs))

    def _process_buffer(self):
        """수신 버퍼에서 완전한 DLT 메시지를 파싱."""
        while len(self._recv_buffer) >= 4:
            htyp = self._recv_buffer[0]
            version = (htyp >> 5) & 0x07

            if version != 1:
                del self._recv_buffer[0]
                continue

            msg_len = struct.unpack(">H", self._recv_buffer[2:4])[0]
            if msg_len < 4 or msg_len > 65535:
                del self._recv_buffer[0]
                continue

            if len(self._recv_buffer) < msg_len:
                break

            msg_data = bytes(self._recv_buffer[:msg_len])
            del self._recv_buffer[:msg_len]

            line = self._parse_message(msg_data)
            if line:
                cap_ts = time.time()
                # 스트림 파일(정본)에 즉시 기록 — 절대 인덱스(_total_count)와
                # 파일 라인 위치가 1:1이 되도록 append와 같은 lock 구간에서 처리.
                with self._lock:
                    self._logs.append(line)
                    self._log_capture_ts.append(cap_ts)
                    self._msg_counter += 1
                    self._total_count += 1
                    if self._save_file:
                        try:
                            self._save_file.write(line + "\n")
                            self._save_file.flush()
                        except Exception:
                            pass

                # 키워드 카운터/단언/검출 검사
                if self._counters or self._asserts or self._fail_keywords:
                    now_ts = time.time()
                    fail_reports: list[tuple[str, str, str, Optional[int], int]] = []
                    with self._counter_lock:
                        for c in self._counters.values():
                            if c["keyword"] in line:
                                c["count"] += 1
                                c["timestamps"].append(now_ts)
                        for a in self._asserts.values():
                            if a["keyword"] not in line:
                                a["miss_count"] += 1
                                a["miss_timestamps"].append(now_ts)
                                fail_reports.append((a["keyword"], line, "missing", None, 1))
                        for f in self._fail_keywords.values():
                            if f["keyword"] in line:
                                f["hit_count"] += 1
                                f["hit_timestamps"].append(now_ts)
                                fail_reports.append((
                                    f["keyword"], line, "matched",
                                    f.get("parent_step_id"),
                                    f.get("parent_repeat_index", 1),
                                ))
                    if fail_reports:
                        try:
                            from backend.app.services.playback_service import report_runtime_fail
                            for kw, ln, reason, p_sid, p_rep in fail_reports:
                                report_runtime_fail(
                                    "DLTLogging", kw, now_ts, ln, reason=reason,
                                    repeat_index=p_rep,
                                    parent_step_id=p_sid,
                                )
                        except Exception:
                            pass

                # 뷰어 구독자에게 스트리밍 (Hub에 세션 등록된 경우만 비용 발생)
                DLT_HUB.emit_log(self._session_id(), line)

    # ------------------------------------------------------------------
    # DLT 메시지 파싱 (DLTViewer와 동일 로직)
    # ------------------------------------------------------------------

    def _parse_message(self, data: bytes) -> Optional[str]:
        """DLT 메시지 1개를 파싱하여 텍스트 한 줄로 변환."""
        if len(data) < 4:
            return None

        htyp = data[0]
        msg_len = struct.unpack(">H", data[2:4])[0]
        pos = 4

        ecu_id = ""
        timestamp = 0

        if htyp & 0x04:  # WEID
            if pos + 4 > msg_len:
                return None
            ecu_id = data[pos:pos + 4].decode("ascii", errors="replace").rstrip("\x00")
            pos += 4

        if htyp & 0x08:  # WSID
            pos += 4

        if htyp & 0x10:  # WTMS
            if pos + 4 <= msg_len:
                timestamp = struct.unpack(">I", data[pos:pos + 4])[0]
            pos += 4

        apid = ""
        ctid = ""
        msg_type_str = ""
        verbose = False
        noar = 0

        if htyp & 0x01:  # UEH
            if pos + 10 > msg_len:
                return None
            msin = data[pos]
            noar = data[pos + 1]
            apid = data[pos + 2:pos + 6].decode("ascii", errors="replace").rstrip("\x00")
            ctid = data[pos + 6:pos + 10].decode("ascii", errors="replace").rstrip("\x00")
            pos += 10

            verbose = bool(msin & 0x01)
            mtype = (msin >> 1) & 0x07
            msub = (msin >> 4) & 0x0F

            mtype_name = _MSG_TYPE.get(mtype, str(mtype))
            if mtype == 0:
                msub_name = _LOG_LEVEL.get(msub, str(msub))
            else:
                msub_name = str(msub)
            msg_type_str = f"{mtype_name} {msub_name}".strip()

        payload_data = data[pos:msg_len]
        payload_text = ""

        if verbose and noar > 0 and len(payload_data) > 0:
            payload_text = self._parse_verbose_payload(payload_data, noar)
        elif len(payload_data) > 0:
            payload_text = self._extract_printable(payload_data)

        if not payload_text.strip():
            return None

        ts_sec = timestamp / 10000.0
        ts_str = f"{ts_sec:>12.4f}"

        return f"{ts_str} {ecu_id:<4s} {apid:<4s} {ctid:<4s} {msg_type_str:<12s} {payload_text}"

    def _parse_verbose_payload(self, data: bytes, noar: int) -> str:
        """Verbose 모드 DLT payload 파싱."""
        parts = []
        pos = 0

        for _ in range(noar):
            if pos + 4 > len(data):
                break

            type_info = struct.unpack("<I", data[pos:pos + 4])[0]
            pos += 4

            tyle = type_info & 0x0F
            is_bool = bool(type_info & 0x10)
            is_sint = bool(type_info & 0x20)
            is_uint = bool(type_info & 0x40)
            is_float = bool(type_info & 0x80)
            is_string = bool(type_info & 0x200)
            is_raw = bool(type_info & 0x400)
            has_vari = bool(type_info & 0x800)

            if has_vari:
                if pos + 2 > len(data):
                    break
                name_len = struct.unpack("<H", data[pos:pos + 2])[0]
                pos += 2
                if pos + name_len > len(data):
                    break
                pos += name_len

            if is_string:
                if pos + 2 > len(data):
                    break
                str_len = struct.unpack("<H", data[pos:pos + 2])[0]
                pos += 2
                if pos + str_len > len(data):
                    break
                s = data[pos:pos + str_len].decode("utf-8", errors="replace").rstrip("\x00")
                parts.append(s)
                pos += str_len
            elif is_raw:
                if pos + 2 > len(data):
                    break
                raw_len = struct.unpack("<H", data[pos:pos + 2])[0]
                pos += 2
                if pos + raw_len > len(data):
                    break
                parts.append(data[pos:pos + raw_len].hex())
                pos += raw_len
            elif is_bool:
                byte_len = _TYLE_BYTES.get(tyle, 1)
                if pos + byte_len > len(data):
                    break
                parts.append(str(bool(data[pos])))
                pos += byte_len
            elif is_uint:
                byte_len = _TYLE_BYTES.get(tyle, 4)
                if pos + byte_len > len(data):
                    break
                val = int.from_bytes(data[pos:pos + byte_len], "little", signed=False)
                parts.append(str(val))
                pos += byte_len
            elif is_sint:
                byte_len = _TYLE_BYTES.get(tyle, 4)
                if pos + byte_len > len(data):
                    break
                val = int.from_bytes(data[pos:pos + byte_len], "little", signed=True)
                parts.append(str(val))
                pos += byte_len
            elif is_float:
                byte_len = 4 if tyle <= 3 else 8
                if pos + byte_len > len(data):
                    break
                if byte_len == 4:
                    val = struct.unpack("<f", data[pos:pos + byte_len])[0]
                else:
                    val = struct.unpack("<d", data[pos:pos + byte_len])[0]
                parts.append(f"{val:.6f}")
                pos += byte_len
            else:
                parts.append(self._extract_printable(data[pos:]))
                break

        return " ".join(parts)

    @staticmethod
    def _extract_printable(data: bytes) -> str:
        """바이트에서 출력 가능한 텍스트 추출."""
        text = data.decode("utf-8", errors="replace")
        return "".join(c if c.isprintable() or c in "\n\t " else "" for c in text).strip()
