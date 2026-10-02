"""재생/스텝 테스트 중단 신호 — 모듈 스레드까지 전파되는 협조적 abort 플래그.

중단 요청 시 '진행 중인 동작'은 끝까지 수행하되, 그 안의 대기(wait/sleep)와
키워드 감시·폴링 같은 루프는 현재 회차에서 즉시 끝내기 위해 쓴다.

- 재생 중단: PlaybackService._should_stop 세터가 함께 set (다음 스텝 진행도 막음).
- 스텝 테스트 중단: 이 신호만 set — 액션은 실행하고 wait 만 건너뛴다.
- 플러그인(스레드에서 실행되는 모듈 함수)은 time.sleep 대신 abort_sleep() 을,
  deadline 폴링 루프 조건에 is_abort_requested() 를 사용한다.

플러그인은 ``backend.app.services.run_abort`` 로, 앱은 상대 import 로 불러오므로
같은 모듈이 서로 다른 이름으로 두 번 로드될 수 있다. 상태(Event)는 sys.modules 의
고정 키에 한 번만 만들어 공유해 어느 경로로 import 해도 동일 신호를 보게 한다.
"""

from __future__ import annotations

import sys
import threading
import types

_STATE_KEY = "_replaykit_run_abort_state"

_state = sys.modules.get(_STATE_KEY)
if _state is None:
    _state = types.ModuleType(_STATE_KEY)
    _state.event = threading.Event()
    sys.modules[_STATE_KEY] = _state

_event: threading.Event = _state.event

# 중단 시 모듈 함수가 반환하는 결과 접두어 — 재생이 FAIL 대신 '중단'으로 기록한다.
STOPPED_PREFIX = "STOPPED:"


def request_abort() -> None:
    """중단 요청 — 진행 중인 wait/폴링 루프가 다음 체크에서 빠져나온다."""
    _event.set()


def clear_abort() -> None:
    """새 재생/스텝 테스트 시작 시 이전 중단 신호를 해제."""
    _event.clear()


def is_abort_requested() -> bool:
    return _event.is_set()


def abort_sleep(seconds: float) -> bool:
    """중단 가능한 sleep. 중단 요청이 오면 즉시 깨어나 True 를 반환한다."""
    if seconds <= 0:
        return _event.is_set()
    return _event.wait(seconds)
