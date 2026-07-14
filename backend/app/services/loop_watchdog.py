"""Event-loop stall watchdog — "서버 연결 중..." 원인 코드 런타임 특정용 진단 도구.

메인 asyncio 이벤트 루프가 동기 작업(무거운 파일 I/O·pydantic·순수 파이썬 루프·
블로킹 소켓 등)에 의해 임계(_STALL_THRESHOLD_S) 이상 블록되면, 그 시점의 메인 스레드
스택을 로그로 덤프한다. 루프가 굶으면 /api/health 가 응답하지 못해 프론트가
"서버 연결 중..." 배너를 띄우므로, 어떤 코드가 루프를 막는지 정확히 잡아낸다.

동작 원리(별도 감시 스레드):
  - 루프에서 도는 heartbeat 태스크가 주기적으로 monotonic 시각을 갱신한다.
  - 데몬 스레드가 이를 감시한다. 루프가 블록되면 heartbeat 가 멈추므로, 감시 스레드가
    임계 초과(lag)를 감지해 sys._current_frames()로 메인 스레드 스택을 덤프한다.
    (블록 중인 코드가 아직 콜스택에 살아있는 동안 캡처 — 사후가 아니라 현행범 포착.)

감시를 루프가 아닌 별도 스레드에 두는 이유: 루프가 막힌 바로 그 순간을 관찰해야 하는데,
루프 자신은 막혀 있으므로 관찰할 수 없다. 순수 파이썬 CPU 루프라도 GIL 은 수 ms 마다
전환되므로(sys.getswitchinterval), 감시 스레드는 CPU 를 받아 스택을 읽을 수 있다.

오버헤드는 무시할 수준(0.25s 슬립 태스크 1개 + 0.5s 슬립 데몬 스레드 1개).
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from typing import Optional

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


# 이 이상 루프가 멈추면 스택 덤프. health 타임아웃(5s)·연속 3회 임계보다 훨씬 낮게 잡아,
# 배너가 뜨기 전 단계의 짧은 스톨까지 선제적으로 포착한다.
_STALL_THRESHOLD_S = _env_float("REPLAYKIT_LOOP_STALL_THRESHOLD", 1.0)
_BEAT_INTERVAL_S = 0.25       # heartbeat 갱신 주기
_CHECK_INTERVAL_S = 0.5       # 감시 스레드 점검 주기
_DUMP_COOLDOWN_S = 5.0        # 동일 스톨에 대한 중복 덤프 억제

_last_beat: float = 0.0
_main_thread_id: Optional[int] = None
_watch_thread: Optional[threading.Thread] = None
_heartbeat_handle: Optional[asyncio.Task] = None
_running = False


async def _heartbeat_task() -> None:
    global _last_beat
    while _running:
        _last_beat = time.monotonic()
        try:
            await asyncio.sleep(_BEAT_INTERVAL_S)
        except asyncio.CancelledError:
            break


def _watch_loop() -> None:
    last_dump = 0.0
    while _running:
        time.sleep(_CHECK_INTERVAL_S)
        if not _running:
            break
        now = time.monotonic()
        lag = now - _last_beat
        if lag < _STALL_THRESHOLD_S:
            continue
        if now - last_dump < _DUMP_COOLDOWN_S:
            continue
        last_dump = now
        frame = sys._current_frames().get(_main_thread_id) if _main_thread_id else None
        if frame is None:
            logger.warning(
                "[loop-watchdog] event loop blocked %.2fs — /api/health 굶김 (메인 스택 캡처 실패)",
                lag,
            )
            continue
        stack = "".join(traceback.format_stack(frame))
        logger.warning(
            "[loop-watchdog] event loop blocked %.2fs — /api/health 굶김 (\"서버 연결 중...\" 원인). "
            "메인 스레드 블록 지점:\n%s",
            lag, stack,
        )


def start() -> None:
    """이벤트 루프 안에서 호출(예: FastAPI lifespan startup)."""
    global _running, _main_thread_id, _watch_thread, _heartbeat_handle, _last_beat
    if _running:
        return
    if _STALL_THRESHOLD_S <= 0:
        logger.info("[loop-watchdog] disabled (threshold<=0)")
        return
    _running = True
    _last_beat = time.monotonic()
    _main_thread_id = threading.main_thread().ident
    _watch_thread = threading.Thread(target=_watch_loop, name="loop-watchdog", daemon=True)
    _watch_thread.start()
    _heartbeat_handle = asyncio.create_task(_heartbeat_task())
    logger.info(
        "[loop-watchdog] started (threshold=%.1fs) — 루프 스톨 시 메인 스택 덤프",
        _STALL_THRESHOLD_S,
    )


def stop() -> None:
    global _running, _heartbeat_handle
    _running = False
    if _heartbeat_handle is not None and not _heartbeat_handle.done():
        _heartbeat_handle.cancel()
    _heartbeat_handle = None
