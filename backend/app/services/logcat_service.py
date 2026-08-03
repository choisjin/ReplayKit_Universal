"""Android logcat 캡처·저장·키워드 합부 판정 서비스.

SerialLogging과 동일한 사용 감각을 Android(adb) 디바이스에 제공한다.
  - StartLogging / StopLogging 으로 `adb logcat` 캡처 시작/종료(파일 저장)
  - Monitor_pass_on_keyword / Monitor_fail_on_keyword 로 (이미 출력된 로그 + 미래
    time초) 로그에서 키워드를 판독 — adb로 아무 명령도 push하지 않음.

핵심 설계(시리얼에서 얻은 교훈 반영):
  - **serial(=device serial)별 독립 세션**: 한 벤치에 여러 Android 디바이스가 붙어도
    버퍼/프로세스가 섞이지 않도록 serial을 키로 세션을 분리한다.
    (cf. SerialLogging 멀티포트 싱글톤 키 충돌 회귀)
  - **자가복구 리더**: 디바이스 reboot/usb 드롭으로 `adb logcat`이 종료(EOF)돼도
    리더 스레드를 죽이지 않고 logcat을 재기동하여 **같은 세션에 이어서** 캡처한다.
  - **스트리밍 저장 (legacy RDF start_logcat 동일 컨셉)**: 캡처 라인은 StartLogging
    시점에 연 파일로 즉시 기록(주기 flush)한다 — 백엔드가 죽어도 그 시점까지 디스크에
    남고, 장시간 캡처에도 메모리가 늘지 않는다. StopLogging(save_path)는 그 파일을
    요청 경로로 이동만 한다. (이전: 메모리 누적 → Stop 일괄 저장 = 크래시 전체 유실)
  - **과거 포함 판독**: include_past=True 키워드 검사는 스트리밍 **파일을 정본**으로
    먼저 스캔(메모리 링버퍼는 최근 라인만 보관)한 뒤, 파일에서 읽은 라인 수를 절대
    인덱스 삼아 링버퍼로 이어받아 미래 time초를 폴링한다 — flush 지연분 누락 없음.
"""

from __future__ import annotations

import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from itertools import islice
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# adb 바이너리 — adb_service와 동일 규약(번들 adb 우선, PATH 'adb' 폴백).
from .adb_path import resolve_adb_path
ADB_PATH = resolve_adb_path()
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 메모리 링버퍼 상한 — 뷰어 백필/키워드 '미래' 폴링용 최근 라인만 보관 (~15-20MB/디바이스).
# 전체 이력은 스트리밍 파일이 정본이므로 메모리에 다 들고 있을 필요가 없다.
LOGCAT_RING_MAX = 100_000
# 스트리밍 파일 flush 정책 — N줄마다 또는 N초 경과 시. 미flush 백로그는 링버퍼 상한보다
# 항상 훨씬 작아야 한다 (Monitor 의 파일→링버퍼 이어받기 무결성 전제).
_FLUSH_EVERY_LINES = 100
_FLUSH_INTERVAL_S = 0.5

# 좀비 스트림 감시 — suspend→wake 에서 USB 재열거로 transport 가 바뀌면, suspend 중에
# 재기동된 logcat 이 옛(얼어붙은) transport 에 붙은 채 EOF 없이 영원히 무음이 된다
# (2026-06-11 실측: respawn 1회 후 2분간 무수신 → sleep 직전 로그가 파일에 안 들어옴).
# 'offline 을 겪은 적 있고 + 지금 디바이스 online + N초 무수신' 이면 강제 재기동해
# 새 transport 로 링버퍼 재덤프를 받는다. (안정 연결에서 조용한 디바이스는 offline 전적이
# 없어 재기동하지 않음 — 재덤프 중복 루프 방지)
_STALL_RESPAWN_S = 20.0


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

    - 재생 중: {run_dir}/logs/logcat/c{cycle}_{prefix}_{serial}_{ts}.log
      (로그 종류별 하위 폴더 + 사이클 접두사 — 장시간 반복 시 회차 구분용)
    - 스텝 테스트: backend/results/Temp_logs/{prefix}_{serial}_{ts}.log
    """
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe_serial = "".join(c if c.isalnum() or c in "-._" else "_" for c in serial)[:40]
    run_dir = _get_run_output_dir()
    cyc = ""
    if run_dir:
        log_dir = run_dir / "logs" / "logcat"
        cyc = f"c{_current_step_context()[1]:03d}_"
    else:
        try:
            from .playback_service import RESULTS_DIR
            log_dir = Path(RESULTS_DIR) / "Temp_logs"
        except Exception:
            log_dir = Path(__file__).resolve().parent.parent.parent / "results" / "Temp_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stem = f"{cyc}{prefix}_{safe_serial}_{ts}"
    path = log_dir / f"{stem}.log"
    n = 2
    while path.exists():  # 같은 초에 중복 저장 시 덮어쓰기 방지
        path = log_dir / f"{stem}_{n}.log"
        n += 1
    return str(path)


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
        # 메모리는 최근 라인 링버퍼만 — 오래된 라인은 자동 폐기(전체 이력은 파일이 정본).
        # 절대 라인 번호는 _line_counter 로 추적: 링버퍼 시작 = counter - len(logs).
        self._logs: deque[str] = deque(maxlen=LOGCAT_RING_MAX)
        self._log_capture_ts: deque[float] = deque(maxlen=LOGCAT_RING_MAX)
        self._line_counter = 0
        self._logcat_args = "-v time"
        # 스트리밍 저장 파일 — start() 에서 열고 라인마다 기록(주기 flush).
        # 쓰기/flush 는 리더 스레드 단일 작성자 전제(마커 포함) — 락 불필요.
        self._file = None
        self._file_path = ""
        self._unflushed = 0
        self._last_flush = 0.0
        # 좀비 스트림 감시 상태 (suspend→wake transport 재열거 대응)
        self._watchdog_thread: Optional[threading.Thread] = None
        self._last_data_ts = 0.0    # 마지막 '실제 디바이스 라인' 수신 시각
        self._saw_offline = False   # 캡처 중 링크 단절/offline 을 겪었는지

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
        """캡처 시작. 이미 캡처 중이면 새로 시작하지 않고 그대로 둔다.

        legacy RDF `start_logcat` 처럼 저장 파일을 여기서 열어 즉시 스트리밍 기록한다.
        파일을 못 열면 캡처를 시작하지 않는다 (메모리 전용 폴백 없음 — 유실 방지 목적).
        """
        if self._capturing:
            return f"Logcat already capturing: {self._serial}"
        if clear:
            self.clear()
        path = _auto_save_path(self._serial)
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            f = open(path, "w", encoding="utf-8")
        except Exception as e:
            return f"ERROR: 로그 파일 열기 실패 — {path}: {e}"
        with self._lock:
            self._logs.clear()
            self._log_capture_ts.clear()
            self._line_counter = 0
        self._file = f
        self._file_path = path
        self._unflushed = 0
        self._last_flush = time.time()
        self._capturing = True
        self._last_data_ts = time.time()
        self._saw_offline = False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"Logcat-{self._serial}", daemon=True
        )
        self._reader_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, name=f"LogcatWD-{self._serial}", daemon=True
        )
        self._watchdog_thread.start()
        LOGCAT_HUB.emit_lifecycle({
            "type": "session_started",
            "session_id": self._serial,
            "serial": self._serial,
            "started_at": time.time(),
            "save_path": path,
            "scenario_playback": _is_scenario_playback(),
        })
        return f"Logcat logging started: {self._serial} → {path}"

    def stop(self, save_path: str = "") -> str:
        """캡처를 종료하고 스트리밍 파일을 닫는다. save_path 지정 시 그 경로로 이동.

        라인은 캡처 중 이미 파일에 기록돼 있으므로(스트리밍) 여기서 버퍼를 쓰지 않는다.
        """
        if self._file is None and not self._capturing:
            return f"ERROR: logcat 캡처 중이 아닙니다: {self._serial}"

        self._stop_capture()  # 리더 스레드 join 후에 파일을 닫아야 마지막 라인 보존
        total = self._line_counter
        f = self._file
        stream_path = self._file_path
        self._file = None
        self._file_path = ""

        save_error = ""
        if f is not None:
            try:
                f.flush()
                f.close()
            except Exception as e:
                logger.error("[Logcat] stream file close failed: %s", e)
                save_error = str(e)

        # save_path 지정 시 스트리밍 파일 이동 (파일명만 오면 자동 저장 디렉터리 기준)
        saved_path = stream_path
        if save_path and stream_path:
            if not os.path.dirname(save_path):
                save_path = str(Path(stream_path).parent / save_path)
            try:
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                shutil.move(stream_path, save_path)
                saved_path = save_path
            except Exception as e:
                logger.error("[Logcat] move to save_path failed: %s", e)
                save_error = f"이동 실패({e}) — 스트리밍 경로에 보존: {stream_path}"

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
        logger.info("[Logcat] Saved %d lines to %s (streamed)", total, saved_path)
        return f"Logcat saved ({total} lines) to: {saved_path}"

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
        if self._watchdog_thread:
            self._watchdog_thread.join(timeout=3)
            self._watchdog_thread = None

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
        self._append_line(f"[{ts}] --- {text} ---", cap_ts)

    def _append_line(self, stamped: str, cap_ts: float) -> None:
        """캡처 라인 1줄 처리 — 링버퍼 적재 + 파일 즉시 기록(주기 flush) + 뷰어 발행.

        파일 쓰기는 리더 스레드 단일 작성자 전제(마커 포함). 라인별로 링버퍼와 파일에
        같은 순서로 들어가므로, Monitor 는 '파일에서 읽은 라인 수 = 절대 인덱스' 로
        링버퍼에 이어붙일 수 있다.
        """
        with self._lock:
            self._logs.append(stamped)        # maxlen 도달 시 가장 오래된 라인 자동 폐기
            self._log_capture_ts.append(cap_ts)
            self._line_counter += 1
        f = self._file
        if f is not None:
            try:
                f.write(stamped + "\n")
                self._unflushed += 1
                if (self._unflushed >= _FLUSH_EVERY_LINES
                        or cap_ts - self._last_flush >= _FLUSH_INTERVAL_S):
                    f.flush()
                    self._unflushed = 0
                    self._last_flush = cap_ts
            except Exception as e:
                logger.warning("[Logcat] stream write failed (%s): %s", self._file_path, e)
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
                self._saw_offline = True
                self._kill_proc()
                continue

            if not raw:
                # EOF — logcat 종료(디바이스 reboot/끊김). 재기동 루프로.
                if self._capturing:
                    self._emit_marker("logcat ended — reconnecting")
                self._saw_offline = True
                self._kill_proc()
                self._interruptible_sleep(0.5)
                continue

            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            cap_ts = time.time()
            self._last_data_ts = cap_ts
            ts = time.strftime("%H:%M:%S", time.localtime(cap_ts))
            self._append_line(f"[{ts}] {line}", cap_ts)

        logger.info("[Logcat] reader loop ended for %s (lines=%d)", self._serial, self._line_counter)

    def _kill_proc(self):
        proc = self._proc
        self._proc = None
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 좀비 스트림 감시 (suspend→wake USB 재열거 대응)
    # ------------------------------------------------------------------

    def _adb_online(self) -> bool:
        """`adb get-state` 가 'device' 면 online. offline/미등록/타임아웃은 False."""
        try:
            res = subprocess.run(
                [ADB_PATH, "-s", self._serial, "get-state"],
                capture_output=True, text=True, timeout=5,
                creationflags=_NO_WINDOW,
            )
            return res.returncode == 0 and res.stdout.strip() == "device"
        except Exception:
            return False

    def _watchdog_loop(self):
        """suspend 중 재기동된 logcat 이 옛 transport 에 붙어 EOF 없이 무한 무음(좀비)이
        되는 케이스 감시. readline 이 블로킹이라 리더 스스로는 탈출 못 함 → 여기서
        proc.kill() 로 끊어주면 리더 루프가 새 transport 로 재기동한다(링버퍼 재덤프 →
        suspend 직전 라인이 그제야 파일/버퍼에 들어옴).

        오탐 방지: 'offline 을 겪은 적'(_saw_offline) 이 있을 때만 발동 — 안정 연결의
        조용한 디바이스를 주기적으로 재기동해 링버퍼 중복 덤프를 쌓는 것을 막는다.
        """
        while self._capturing:
            time.sleep(2.0)
            if not self._capturing:
                break
            proc = self._proc
            if proc is None or proc.poll() is not None:
                continue  # 리더가 이미 재기동 사이클 중
            quiet = time.time() - self._last_data_ts
            if quiet < _STALL_RESPAWN_S or not self._saw_offline:
                continue
            if not self._adb_online():
                continue  # 디바이스 자체가 아직 offline/suspend — 재기동해도 같음
            logger.warning(
                "[Logcat] stream stalled %.0fs with device online — force respawn (%s)",
                quiet, self._serial,
            )
            self._emit_marker("logcat stalled — force respawn (transport renewed)")
            self._saw_offline = False
            self._last_data_ts = time.time()  # 재기동 후 다시 유예
            self._kill_proc()  # readline EOF/EIO → 리더 루프가 즉시 재기동

    # ------------------------------------------------------------------
    # 키워드 판독 (명령 push 없음)
    # ------------------------------------------------------------------
    # 과거(include_past)는 스트리밍 파일이 정본 — 링버퍼는 최근 LOGCAT_RING_MAX 줄만
    # 보관하므로 장시간 세션의 앞부분은 메모리에 없다. 파일에서 읽은 라인 수를 절대
    # 인덱스로 삼아 링버퍼로 이어받으면 flush 지연분(≤ 수백 줄 << 링 상한)도 누락 없다.

    def _lines_since(self, abs_idx: int) -> tuple[list[str], list[float], int]:
        """절대 라인 번호 abs_idx 이후의 링버퍼 스냅샷. 반환 (lines, ts, next_abs)."""
        with self._lock:
            buf_start = self._line_counter - len(self._logs)
            start = max(0, abs_idx - buf_start)
            lines = list(islice(self._logs, start, None))
            ts = list(islice(self._log_capture_ts, start, None))
            return lines, ts, self._line_counter

    def _scan_file_first(self, keyword: str) -> tuple[int, Optional[str]]:
        """스트리밍 파일에서 keyword 첫 매치 검색. 반환 (읽은 라인 수, 매치 라인|None).

        매치 발견 시 즉시 중단(라인 수는 호출자가 안 씀). flush 된 데까지만 보이지만
        이후 구간은 반환한 라인 수부터 링버퍼가 커버한다.
        """
        path = self._file_path
        if not path:
            return 0, None
        n = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    n += 1
                    if keyword in ln:
                        return n, ln.rstrip("\n")
        except FileNotFoundError:
            return 0, None
        except Exception as e:
            logger.warning("[Logcat] past file scan failed (%s): %s", path, e)
            return 0, None
        return n, None

    def _scan_file_all(self, keyword: str) -> tuple[int, list[tuple[float, str]]]:
        """스트리밍 파일에서 keyword 전체 매치 수집. 반환 (읽은 라인 수, [(ts, line)])."""
        path = self._file_path
        if not path:
            return 0, []
        n = 0
        hits: list[tuple[float, str]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for ln in f:
                    n += 1
                    if keyword in ln:
                        ln = ln.rstrip("\n")
                        hits.append((self._parse_line_ts(ln), ln))
        except FileNotFoundError:
            return 0, []
        except Exception as e:
            logger.warning("[Logcat] past file scan failed (%s): %s", path, e)
            return 0, []
        return n, hits

    @staticmethod
    def _parse_line_ts(line: str) -> float:
        """라인 prefix '[HH:MM:SS]' → 오늘 기준 epoch (리포트 시간축 근사). 실패 시 now."""
        now = time.time()
        m = re.match(r"\[(\d{2}):(\d{2}):(\d{2})\]", line)
        if not m:
            return now
        lt = time.localtime(now)
        try:
            ts = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                              int(m.group(1)), int(m.group(2)), int(m.group(3)), 0, 0, -1))
        except (ValueError, OverflowError):
            return now
        if ts > now + 60:  # 자정 직후 어제 기록을 읽은 경우
            ts -= 86400.0
        return ts

    def monitor_pass(self, keyword: str, time_s: float = 5, include_past: bool = True) -> str:
        import time as _t
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"
        if not self._capturing:
            return "ERROR: logcat 캡처가 시작되지 않았습니다. StartLogging() 먼저 호출하세요."

        # 과거분 — 파일(세션 시작부터 전부)을 스캔하고 그 라인 수에서 링버퍼로 이어받는다.
        if include_past:
            check_abs, hit = self._scan_file_first(keyword)
            if hit is not None:
                return f"PASS: keyword '{keyword}' detected — {hit.strip()[:120]}"
        else:
            with self._lock:
                check_abs = self._line_counter

        deadline = _t.time() + max(0.0, float(time_s))
        while _t.time() < deadline:
            lines, _ts, check_abs = self._lines_since(check_abs)
            for ln in lines:
                if keyword in ln:
                    return f"PASS: keyword '{keyword}' detected — {ln.strip()[:120]}"
            _t.sleep(0.1)

        lines, _ts, check_abs = self._lines_since(check_abs)
        for ln in lines:
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
        return (f"FAIL: keyword '{keyword}' not detected within {float(time_s):g}s"
                + self._link_note())

    def monitor_fail(self, keyword: str, time_s: float = 5, include_past: bool = True) -> str:
        import time as _t
        if not keyword:
            return "ERROR: keyword가 비어 있습니다"
        if not self._capturing:
            return "ERROR: logcat 캡처가 시작되지 않았습니다. StartLogging() 먼저 호출하세요."

        parent_step_id, parent_repeat_index = _current_step_context()
        hits: list[tuple[float, str]] = []

        # 과거분 — 파일 전체 매치 수집(타임스탬프는 라인 prefix에서 복원).
        if include_past:
            check_abs, hits = self._scan_file_all(keyword)
        else:
            with self._lock:
                check_abs = self._line_counter

        deadline = _t.time() + max(0.0, float(time_s))
        while _t.time() < deadline:
            lines, lines_ts, check_abs = self._lines_since(check_abs)
            for ln, ts in zip(lines, lines_ts):
                if keyword in ln:
                    hits.append((ts, ln))
            _t.sleep(0.1)

        lines, lines_ts, check_abs = self._lines_since(check_abs)
        for ln, ts in zip(lines, lines_ts):
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
        return (f"PASS: keyword '{keyword}' not detected within {float(time_s):g}s"
                + self._link_note())

    def _link_note(self) -> str:
        """미검출 결과에 붙일 logcat 링크 상태 힌트.

        디바이스 suspend/offline 중에는 logcat 링크가 죽어 라인이 호스트에 도착하지
        않는다 — suspend 시점 로그(예: 'suspend entry')는 wake 후 adb 복구 시 링버퍼
        재덤프로 일괄 도착하므로, 모니터를 wake 뒤에 배치해야 한다는 진단 단서.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return (" (주의: logcat 링크 끊김 — 디바이스 offline/suspend 중에는 로그가 "
                    "도착하지 않으며 wake 후 일괄 도착. 모니터를 wake 뒤로 배치 권장)")
        return ""

    def get_recent(self, limit: int = 1000) -> list[str]:
        with self._lock:
            if not self._logs:
                return []
            return list(self._logs)[-int(limit):]


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

    def _capture_error(self, serial: str) -> str:
        """모니터 호출 시 해당 serial 이 캡처 중이 아닐 때의 진단 메시지.

        serial 자리에 엉뚱한 값(인자 밀림 등)이 들어온 경우를 바로 알 수 있게,
        요청된 serial 과 현재 캡처 중인 세션 목록을 함께 보여준다.
        """
        with self._lock:
            active = [s for s, ss in self._sessions.items() if ss.is_capturing()]
        hint = f" (현재 캡처 중: {active})" if active else ""
        return (f"ERROR: logcat 캡처가 시작되지 않았습니다 — serial='{serial}'. "
                f"StartLogging() 먼저 호출하거나 serial 인자를 확인하세요.{hint}")

    def monitor_pass(self, serial: str, keyword: str, time_s: float = 5,
                     include_past: bool = True) -> str:
        sess = self._session(serial)
        if not sess.is_capturing():
            return self._capture_error(serial)
        return sess.monitor_pass(keyword, time_s, include_past)

    def monitor_fail(self, serial: str, keyword: str, time_s: float = 5,
                     include_past: bool = True) -> str:
        sess = self._session(serial)
        if not sess.is_capturing():
            return self._capture_error(serial)
        return sess.monitor_fail(keyword, time_s, include_past)

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
