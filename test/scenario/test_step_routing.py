"""재생 분기(조건부이동 + 구간반복) 라우팅 규칙 회귀 테스트.

이 테스트들은 step 참조를 uid 기준으로 옮기는 작업(2b/2c)의 **기준선**이다.
전환 전후로 동일하게 통과해야 하며, 통과하지 못하면 동작이 바뀐 것이다.

대상: PlaybackService._resolve_next_index / _build_loop_map
      (스트리밍/비스트리밍 두 실행 경로가 공유하는 순수 로직)
"""

import pytest

from backend.app.models.scenario import GOTO_END, LoopRange, Scenario, Step, StepType
from backend.app.services.playback_service import PlaybackService


def mk_step(step_id: int, on_pass=None, on_fail=None) -> Step:
    """on_pass/on_fail 은 대상 step.uid 또는 GOTO_END."""
    return Step(id=step_id, type=StepType.WAIT, params={},
                on_pass_goto=on_pass, on_fail_goto=on_fail)


def link(src: Step, *, on_pass: Step = None, on_fail: Step = None) -> Step:
    """src 의 조건부이동을 대상 스텝의 uid 로 연결한다."""
    if on_pass is not None:
        src.on_pass_goto = on_pass.uid
    if on_fail is not None:
        src.on_fail_goto = on_fail.uid
    return src


def mk_scenario(steps, loops=None) -> Scenario:
    return Scenario(name="T", steps=steps, loops=loops or [])


def step_index_map(scenario: Scenario) -> dict[str, int]:
    return {s.uid: i for i, s in enumerate(scenario.steps)}


def run_route(scenario: Scenario, statuses: dict[int, str], max_steps: int = 50) -> list[int]:
    """시나리오를 모의 실행하고 방문한 step.id 순서를 반환.

    statuses: step.id -> "pass" | "fail" | "error" (미지정은 pass)
    """
    by_uid = step_index_map(scenario)
    loop_map = PlaybackService._build_loop_map(scenario, {s.id: i for i, s in enumerate(scenario.steps)})
    loop_remaining: dict[int, int] = {}

    visited: list[int] = []
    idx = 0
    while 0 <= idx < len(scenario.steps) and len(visited) < max_steps:
        step = scenario.steps[idx]
        visited.append(step.id)
        status = statuses.get(step.id, "pass")
        idx, stop = PlaybackService._resolve_next_index(
            idx, step, status, by_uid, loop_map, loop_remaining
        )
        if stop:
            break
    return visited


# ----------------------------------------------------------------------
# 조건부이동 (goto)
# ----------------------------------------------------------------------

def test_natural_progression_without_goto():
    sc = mk_scenario([mk_step(1), mk_step(2), mk_step(3)])
    assert run_route(sc, {}) == [1, 2, 3]


def test_on_pass_goto_jumps_forward():
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    link(s1, on_pass=s3)
    assert run_route(mk_scenario([s1, s2, s3]), {}) == [1, 3]


def test_on_fail_goto_taken_only_on_failure():
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    link(s1, on_fail=s3)
    sc = mk_scenario([s1, s2, s3])
    assert run_route(sc, {}) == [1, 2, 3]            # pass → 자연 진행
    assert run_route(sc, {1: "fail"}) == [1, 3]      # fail → 점프


def test_error_status_follows_on_fail_goto():
    """error 는 fail 과 동일하게 on_fail_goto 를 따른다."""
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    link(s1, on_fail=s3)
    assert run_route(mk_scenario([s1, s2, s3]), {1: "error"}) == [1, 3]


def test_goto_end_sentinel_stops_playback():
    sc = mk_scenario([mk_step(1, on_pass=GOTO_END), mk_step(2), mk_step(3)])
    assert run_route(sc, {}) == [1]


def test_goto_backward_revisits_step():
    """뒤로 점프하면 같은 스텝을 다시 실행한다(무한루프는 max_steps 로 차단)."""
    s1, s2 = mk_step(1), mk_step(2)
    link(s2, on_pass=s1)
    visited = run_route(mk_scenario([s1, s2]), {}, max_steps=5)
    assert visited == [1, 2, 1, 2, 1]


def test_unresolvable_goto_target_falls_through():
    """대상 스텝이 삭제되어 uid 를 못 찾으면 점프를 무시하고 자연 진행한다."""
    sc = mk_scenario([mk_step(1, on_pass="deadbeef"), mk_step(2)])
    assert run_route(sc, {}) == [1, 2]


# ----------------------------------------------------------------------
# 구간반복 (loops)
# ----------------------------------------------------------------------

def test_loop_repeats_section_count_times():
    """count 는 '총 실행 횟수' — 2 면 구간을 두 번 실행."""
    sc = mk_scenario([mk_step(1), mk_step(2), mk_step(3), mk_step(4)],
                     loops=[LoopRange(start=2, end=3, count=2)])
    assert run_route(sc, {}) == [1, 2, 3, 2, 3, 4]


def test_loop_count_three():
    sc = mk_scenario([mk_step(1), mk_step(2), mk_step(3)],
                     loops=[LoopRange(start=1, end=2, count=3)])
    assert run_route(sc, {}) == [1, 2, 1, 2, 1, 2, 3]


def test_loop_count_one_or_less_is_ignored():
    sc = mk_scenario([mk_step(1), mk_step(2), mk_step(3)],
                     loops=[LoopRange(start=1, end=2, count=1)])
    assert run_route(sc, {}) == [1, 2, 3]


def test_loop_with_missing_boundary_is_ignored():
    """경계 스텝이 없으면 해당 구간반복은 무시된다."""
    sc = mk_scenario([mk_step(1), mk_step(2)],
                     loops=[LoopRange(start=1, end=99, count=3)])
    assert run_route(sc, {}) == [1, 2]


def test_loop_inverted_boundaries_are_swapped():
    """start > end 로 저장돼도 스왑해서 정상 구간으로 처리한다."""
    sc = mk_scenario([mk_step(1), mk_step(2), mk_step(3)],
                     loops=[LoopRange(start=2, end=1, count=2)])
    assert run_route(sc, {}) == [1, 2, 1, 2, 3]


# ----------------------------------------------------------------------
# goto 와 loop 의 우선순위 — 회귀 시 가장 티가 안 나는 규칙
# ----------------------------------------------------------------------

def test_goto_takes_precedence_over_loop():
    """구간 끝 스텝에 goto 가 걸려 있으면 반복하지 않고 점프한다."""
    steps = [mk_step(i) for i in range(1, 6)]
    link(steps[2], on_pass=steps[4])
    sc = mk_scenario(steps, loops=[LoopRange(start=2, end=3, count=3)])
    assert run_route(sc, {}) == [1, 2, 3, 5]


def test_loop_applies_when_goto_not_triggered():
    """on_fail_goto 가 있어도 pass 면 자연 진행이므로 반복이 적용된다."""
    steps = [mk_step(i) for i in range(1, 6)]
    link(steps[2], on_fail=steps[4])
    sc = mk_scenario(steps, loops=[LoopRange(start=2, end=3, count=2)])
    assert run_route(sc, {}) == [1, 2, 3, 2, 3, 4, 5]
