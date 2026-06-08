"""Android logcat 캡처·저장·키워드 합부 판정 서비스.

SerialLogging과 동일한 사용 감각을 Android(adb) 디바이스에 제공한다.
  - StartLogging / StopLogging 으로 `adb logcat` 캡처 시작/종료(파일 저장)
  - Monitor_pass_on_keyword / Monitor_fail_on_keyword 로 (이미 출력된 버퍼 + 미래
    time초) 로그에서 키워드를 판독 — adb로 아무 명령도 push하지 않음.

핵심 설계(시리얼에서 얻은 교훈 반영):
  - **serial(=device serial)별 독립 세션**: 한 벤치에 여러 Android 디바이스가 붙어도
    버퍼/프로세스가 섞이지 않도록 serial을 키로 세션을 분리한다.
    (cf. SerialLogging 멀티포트 싱글톤 키 충돌 회귀)
  - **자가복구 리더**: 디바이스 reboot/usb 드롭으로 `adb logcat`이 종료(EOF)돼도
    리더 스레드를 죽이지 않고 logcat을 재기동하여 **같은 버퍼에 이어서** 캡처한다.
  - **과거 버퍼 포함 판독**: 키워드 검사는 include_past=True면 현재까지 캡처된
    버퍼 전체를 먼저 스캔한 뒤 미래 time초를 폴링한다.
"""

from __future__ import annotations

import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# adb 바이너리 — adb_service와 동일 규약(ADB_PATH 환경변수 → 'adb').
ADB_PATH = os.environ.get("ADB_PATH", "adb")
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


# ==========================================================================
# 뷰어용 Pub/Sub 허브 — SERIAL_HUB와 동일 패턴 (세션 키 = device serial).
# ==========================================================================

class _LogcatHub:
    """logcat 세션 + 로그 스트림 구독자 관리 (thread-safe)."""

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
                self._sessions[sid] = {k: v for k, v in event.items() if k != "type"}
            elif etype == "session_stopped" and sid:
                self._sessions.pop(sid, None)
            subs = list(self._lifecycle_subs)
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


LOGCAT_HUB = _LogcatHub()


def _get_run_output_dir() -> Optional[Path]:
    try:
        from .playback_service import get_run_output_dir
        return get_run_output_dir()
    except Exception:
        return None


def _auto_save_path(serial: str, prefix: str = "logcat") -> str:
    """컨텍스트별 자동 저장 경로 (SerialLogging과 동일 규약).

    - 재생 중: {run_dir}/logs/{prefix}_{serial}_{ts}.log
    - 스텝 테스트: backend/results/Temp_logs/{prefix}_{serial}_{ts}.log
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_serial = "".join(c if c.isalnum() or c in "-._" else "_" for c in serial)[:40]
    run_dir = _get_run_output_dir()
    if run_dir:
        log_dir = run_dir / "logs"
    else:
        try:
            from .playback_service import RESULTS_DIR
            log_dir = Path(RESULTS_DIR) / "Temp_logs"
        except Exception:
            log_dir = Path(__file__).resolve().parent.parent.parent / "results" / "Temp_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return str(log_dir / f"{prefix}_{safe_serial}_{ts}.log")


def _current_step_context() -> tuple[Optional[int], int]:
    try:
        from .playback_service import get_current_step_context
        return get_current_step_context()
    except Exception:
        return None, 1


def _is_scenario_playback() -> bool:
    try:
        from .playback_service import is_playback_active
        return is_playback_active()
    except Exception:
        return False


class _LogcatSession:
    """단일 Android 디바이스(serial)의 logcat 캡처 세션."""

    def __init__(self, serial: str):
        self._serial = serial
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._capturing = False
        self._lock = threading.Lock()
        self._logs: list[str] = []
        self._log_capture_ts: list[float] = []
        self._line_counter = 0
        self._logcat_args = "-v time"

    # ------------------------------------------------------------------
    # logcat 프로세스 제어
    # ------------------------------------------------------------------

    def _spawn_logcat(self) -> Optional[subprocess.Popen]:
        cmd = [ADB_PATH, "-s", self._serial, "logcat", *self._logcat_args.split()]
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
                bufsize=1,
                universal_newlines=False,  # 바이트로 받아 직접 디코드 (깨짐 방지)
            )
            logger.info("[Logcat] spawned: %s", " ".join(cmd))
            return proc
        except Exception as e:
            logger.debug("[Logcat] spawn failed for %s: %s", self._serial, e)
            return None

    def clear(self) -> str:
        """`adb logcat -c` 로 디바이스 logcat 링버퍼를 비운다."""
        try:
            subprocess.run(
                [ADB_PATH, "-s", self._serial, "logcat", "-c"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=10, creationflags=_NO_WINDOW,
            )
            return f"Logcat cleared: {self._serial}"
        except Exception as e:
            return f"ERROR: logcat clear 실패 — {e}"

    def start(self, clear: bool = True) -> str:
        """캡처 시작. 이미 캡처 중이면 새로 시작하지 않고 그대로 둔다."""
        if self._capturing:
            return f"Logcat already capturing: {self._serial}"
        if clear:
            self.clear()
        with self._lock:
            self._logs.clear()
            self._log_capture_ts.clear()
        self._line_counter = 0
        self._capturing = True
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"Logcat-{self._serial}", daemon=True
        )
        self._reader_thread.start()
        LOGCAT_HUB.emit_lifecycle({
            "type": "session_started",
            "session_id": self._serial,
            "serial": self._serial,
            "started_at": time.time(),
            "scenario_playback": _is_scenario_playback(),
        })
        return f"Logcat logging started: {self._serial}"

    def stop(self, save_path: str = "") -> str:
        """메모리 버퍼를 파일로 저장하고 캡처를 종료한다."""
        with self._lock:
            logs_snapshot = list(self._logs)

        saved_path = ""
        save_error = ""
        try:
            if not save_path:
                save_path = _auto_save_path(self._serial)
            elif not os.path.dirname(save_path):
                base_dir = Path(_auto_save_path(self._serial)).parent
                save_path = str(base_dir / save_path)
            try:
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(logs_snapshot))
                    if logs_snapshot:
                        f.write("\n")
                saved_path = save_path
                logger.info("[Logcat] Saved %d lines to %s", len(logs_snapshot), save_path)
            except Exception as e:
                logger.error("[Logcat] Save failed: %s", e)
                save_error = str(e)
        except Exception as e:
            logger.error("[Logcat] stop path resolution failed: %s", e)
            save_error = save_error or str(e)
        finally:
            self._stop_capture()
            with self._lock:
                self._logs.clear()
                self._log_capture_ts.clear()
            self._line_counter = 0
            try:
                LOGCAT_HUB.emit_lifecycle({
                    "type": "session_stopped",
                    "session_id": self._serial,
                    "save_path": saved_path,
                    "stopped_at": time.time(),
                })
            except Exception:
                pass

        if save_error:
            return f"ERROR: 저장 실패 — {save_error}"
        return f"Logcat saved ({len(logs_snapshot)} lines) to: {saved_path}"

    def _stop_capture(self):
        self._capturing = False
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.terminate()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._reader_thread:
            self._reader_thread.join(timeout=3)
            self._reader_thread = None

    def is_capturing(self) -> bool:
        return self._capturing

    # ------------------------------------------------------------------
    # 자가복구 리더 스레드
    # ------------------------------------------------------------------

    def _interruptible_sleep(self, seconds: float) -> None:
        end = time.time() + max(0.0, seconds)
        while self._capturing and time.time() < end:
            time.sleep(0.1)

    def _emit_marker(self, text: str) -> None:
        cap_ts = time.time()
        ts = time.strftime("%H:%M:%S", time.localtime(cap_ts))
        stamped = f"[{ts}] --- {text} ---"
        with self._lock:
            self._logs.append(stamped)
            self._log_capture_ts.append(cap_ts)
            self._line_counter += 1
        try:
            LOGCAT_HUB.emit_log(self._serial, stamped)
        except Exception:
            pass

    def _reader_loop(self):
        """logcat stdout을 줄 단위로 수신. 디바이스 reboot/끊김으로 logcat이 종료돼도
        리더를 죽이지 않고 재기동하여 **같은 버퍼에 이어서** 캡처한다."""
        backoff = 0.5
        while self._capturing:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                proc = self._spawn_logcat()
                if proc is None:
                    self._interruptible_sleep(backoff)
                    backoff = min(backoff * 2, 5.0)
                    continue
                self._proc = proc
                backoff = 0.5

            stream = proc.stdout
            if stream is None:
                self._interruptible_sleep(backoff)
                continue
            try:
                raw = stream.readline()
            except Exception as e:
                if self._capturing:
                    logger.warning("[Logcat] read failed (%s) — restarting for %s", e, self._serial)
                    self._emit_marker("logcat disconnected — reconnecting")
                self._kill_proc()
                continue

            if not raw:
                # EOF — logcat 종료(디바이스 reboot/끊김). 재기동 루프로.
                if self._capturing:
                    self._emit_marker("logcat ended — reconnecting")
                self._kill_proc()
                self._interruptible_sleep(0.5)
                continue

            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            cap_ts = time.time()
            ts = time.strftime("%H:%M:%S", time.localtime(cap_ts))
            stamped = f"[{ts}] {line}"
            with self._lock:
                self._logs.append(stamped)
                self._log_capture_ts.append(cap_ts)
                self._line_counter += 1
            try:
                LOGCAT_HUB.emit_log(self._serial, stamped)
            except Exception:
                pass

        logger.info("[Logcat] reader loop ended for %s (logs=%d)", self._serial, len(self._logs))

    def _kill_proc(self):
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 키워드 판독 (명령 push 없음)
    # ------------------------------------------------------------------

    def monitor_pass(self, keyword: str, time_s: float = 5, include_past: bool = True) -> str:
        import time as _t
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"
        if not self._capturing:
            return "ERROR: logcat 캡처가 시작되지 않았습니다. StartLogging() 먼저 호출하세요."

        with self._lock:
            check_idx = 0 if include_past else len(self._logs)
            snapshot = self._logs[check_idx:]
        for ln in snapshot:
            if keyword in ln:
                return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"
        check_idx += len(snapshot)

        deadline = _t.time() + max(0.0, float(time_s))
        while _t.time() < deadline:
            with self._lock:
                snapshot = self._logs[check_idx:]
            check_idx += len(snapshot)
            for ln in snapshot:
                if keyword in ln:
                    return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"
            _t.sleep(0.1)

        with self._lock:
            tail = self._logs[check_idx:]
        for ln in tail:
            if keyword in ln:
                return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"

        parent_step_id, parent_repeat_index = _current_step_context()
        try:
            from .playback_service import report_runtime_fail
            report_runtime_fail(
                "Android", keyword, _t.time(),
                f"(timeout: '{keyword}' not found in logcat)", reason="missing",
                repeat_index=parent_repeat_index, parent_step_id=parent_step_id,
            )
        except Exception:
            pass
        return f"FAIL: keyword '{keyword}' not detected within {float(time_s):g}s"

    def monitor_fail(self, keyword: str, time_s: float = 5, include_past: bool = True) -> str:
        import time as _t
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"
        if not self._capturing:
            return "ERROR: logcat 캡처가 시작되지 않았습니다. StartLogging() 먼저 호출하세요."

        parent_step_id, parent_repeat_index = _current_step_context()
        hits: list[tuple[float, str]] = []

        with self._lock:
            check_idx = 0 if include_past else len(self._logs)
            snapshot = self._logs[check_idx:]
            snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot)]
        for ln, ts in zip(snapshot, snapshot_ts):
            if keyword in ln:
                hits.append((ts, ln))
        check_idx += len(snapshot)

        deadline = _t.time() + max(0.0, float(time_s))
        while _t.time() < deadline:
            with self._lock:
                snapshot = self._logs[check_idx:]
                snapshot_ts = self._log_capture_ts[check_idx:check_idx + len(snapshot)]
            check_idx += len(snapshot)
            for ln, ts in zip(snapshot, snapshot_ts):
                if keyword in ln:
                    hits.append((ts, ln))
            _t.sleep(0.1)

        with self._lock:
            tail = self._logs[check_idx:]
            tail_ts = self._log_capture_ts[check_idx:check_idx + len(tail)]
        for ln, ts in zip(tail, tail_ts):
            if keyword in ln:
                hits.append((ts, ln))

        if hits:
            try:
                from .playback_service import report_runtime_fail
                for ts_b, ln in hits:
                    report_runtime_fail(
                        "Android", keyword, ts_b, ln, reason="matched",
                        repeat_index=parent_repeat_index, parent_step_id=parent_step_id,
                    )
            except Exception:
                pass
            return (f"FAIL: keyword '{keyword}' detected {len(hits)} time(s) — "
                    f"{hits[0][1].strip()[:120]}")
        return f"PASS: keyword '{keyword}' not detected within {float(time_s):g}s"

    def get_recent(self, limit: int = 1000) -> list[str]:
        with self._lock:
            return list(self._logs[-int(limit):]) if self._logs else []


class LogcatService:
    """serial별 _LogcatSession을 관리하는 싱글톤."""

    def __init__(self):
        self._sessions: dict[str, _LogcatSession] = {}
        self._lock = threading.Lock()

    def _session(self, serial: str) -> _LogcatSession:
        with self._lock:
            sess = self._sessions.get(serial)
            if sess is None:
                sess = _LogcatSession(serial)
                self._sessions[serial] = sess
            return sess

    def start(self, serial: str, clear: bool = True) -> str:
        return self._session(serial).start(clear=clear)

    def stop(self, serial: str, save_path: str = "") -> str:
        return self._session(serial).stop(save_path=save_path)

    def clear(self, serial: str) -> str:
        return self._session(serial).clear()

    def monitor_pass(self, serial: str, keyword: str, time_s: float = 5,
                     include_past: bool = True) -> str:
        return self._session(serial).monitor_pass(keyword, time_s, include_past)

    def monitor_fail(self, serial: str, keyword: str, time_s: float = 5,
                     include_past: bool = True) -> str:
        return self._session(serial).monitor_fail(keyword, time_s, include_past)

    def get_recent(self, serial: str, limit: int = 1000) -> list[str]:
        sess = self._sessions.get(serial)
        return sess.get_recent(limit) if sess else []

    def session_snapshot(self, serial: str, limit: int = 1000) -> Optional[dict]:
        """뷰어 백필용: 세션의 최근 로그 + 총 라인 수. 세션 없으면 None."""
        sess = self._sessions.get(serial)
        if sess is None:
            return None
        return {
            "logs": sess.get_recent(limit),
            "total": sess._line_counter,
            "capturing": sess.is_capturing(),
        }

    def stop_all(self) -> None:
        """재생 종료/cleanup 시 진행 중인 모든 logcat 세션을 저장·정리한다."""
        with self._lock:
            sessions = list(self._sessions.values())
        for sess in sessions:
            if sess.is_capturing():
                try:
                    sess.stop()
                except Exception as e:
                    logger.warning("[Logcat] stop_all: %s", e)


_logcat_service: Optional[LogcatService] = None


def get_logcat_service() -> LogcatService:
    global _logcat_service
    if _logcat_service is None:
        _logcat_service = LogcatService()
    return _logcat_service
