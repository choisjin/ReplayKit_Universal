"""step.uid 부여/영속화/유일성 및 편집 내성 테스트.

배경: step.id 는 스텝 삽입/삭제/순서변경 때마다 배열 위치(i+1)로 재부여되는
'표시용 순번'이다. 영속 참조(기대이미지 파일명, 조건부이동, 구간반복)를 id 로
잡으면 편집 시 조용히 다른 스텝을 가리키게 된다.
실제 사고: 20번 크롭 재추가가 21번의 _step_020_crop_00.png 를 덮어써 오비교 발생.
"""

import asyncio
import json

import pytest

from backend.app.models.scenario import GOTO_END, LoopRange, Scenario, Step, StepType
import backend.app.services.recording_service as rs


def mk_step(step_id: int, **kw) -> Step:
    return Step(id=step_id, type=StepType.WAIT, params={}, **kw)


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """SCENARIOS_DIR / SCREENSHOTS_DIR 를 임시 폴더로 격리한 RecordingService."""
    monkeypatch.setattr(rs, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(rs, "SCREENSHOTS_DIR", tmp_path / "shots")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "shots").mkdir()
    return rs.RecordingService.__new__(rs.RecordingService)


def write_scenario_json(tmp_path, name: str, payload: dict) -> None:
    (tmp_path / "scenarios" / f"{name}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ----------------------------------------------------------------------
# uid 기본 성질
# ----------------------------------------------------------------------

def test_uid_auto_assigned_and_unique():
    a, b = mk_step(1), mk_step(2)
    assert a.uid and b.uid
    assert a.uid != b.uid


def test_uid_is_filename_safe():
    """파일명에 그대로 들어가므로 경로 구분자/공백이 없어야 한다."""
    uid = mk_step(1).uid
    assert uid.isalnum()
    assert len(uid) == 8


def test_uid_survives_serialization_roundtrip():
    s = mk_step(1)
    restored = Step(**s.model_dump())
    assert restored.uid == s.uid


# ----------------------------------------------------------------------
# 레거시 마이그레이션 — uid 는 반드시 '1회 부여 후 영속'되어야 한다
# ----------------------------------------------------------------------

LEGACY = {
    "name": "X",
    "steps": [
        {"id": 20, "type": "wait", "params": {},
         "expected_image": "X_step_020_525093.png",
         "compare_mode": "multi_crop",
         "expected_images": [{"image": "X_step_020_crop_00.png", "label": "", "roi": None}]},
        {"id": 21, "type": "wait", "params": {}, "expected_image": "X_step_021_111.png"},
    ],
}


def test_legacy_scenario_gets_uids_persisted(svc, tmp_path):
    write_scenario_json(tmp_path, "X", LEGACY)
    for n in ("X_step_020_525093.png", "X_step_020_crop_00.png", "X_step_021_111.png"):
        (tmp_path / "shots" / "X").mkdir(exist_ok=True)
        (tmp_path / "shots" / "X" / n).write_bytes(b"x")

    sc = asyncio.run(svc.load_scenario("X"))
    assert all(s.uid for s in sc.steps)

    saved = json.loads((tmp_path / "scenarios" / "X.json").read_text(encoding="utf-8"))
    assert all("uid" in s for s in saved["steps"]), "uid 가 디스크에 영속되지 않았다"


def test_uid_stable_across_reloads(svc, tmp_path):
    """uid 가 로드마다 바뀌면 uid 기반 파일명이 매번 고아가 된다 — 가장 중요한 성질."""
    write_scenario_json(tmp_path, "X", LEGACY)
    (tmp_path / "shots" / "X").mkdir(exist_ok=True)

    first = [s.uid for s in asyncio.run(svc.load_scenario("X")).steps]
    second = [s.uid for s in asyncio.run(svc.load_scenario("X")).steps]
    third = [s.uid for s in asyncio.run(svc.load_scenario("X")).steps]
    assert first == second == third


def test_legacy_image_refs_are_not_renamed(svc, tmp_path):
    """기존 파일명은 건드리지 않는다 — 과거 결과물의 이미지 링크 보존."""
    write_scenario_json(tmp_path, "X", LEGACY)
    (tmp_path / "shots" / "X").mkdir(exist_ok=True)
    for n in ("X_step_020_525093.png", "X_step_020_crop_00.png", "X_step_021_111.png"):
        (tmp_path / "shots" / "X" / n).write_bytes(b"x")

    sc = asyncio.run(svc.load_scenario("X"))
    assert sc.steps[0].expected_images[0].image == "X_step_020_crop_00.png"


# ----------------------------------------------------------------------
# 레거시 goto(정수 step.id) → uid 변환
# ----------------------------------------------------------------------

LEGACY_GOTO = {
    "name": "G",
    "steps": [
        {"id": 1, "type": "wait", "params": {}, "on_pass_goto": 3},
        {"id": 2, "type": "wait", "params": {}},
        {"id": 3, "type": "wait", "params": {}, "on_fail_goto": -1},
        {"id": 4, "type": "wait", "params": {}, "on_pass_goto": 99},
    ],
}


def test_legacy_int_goto_converted_to_uid(svc, tmp_path):
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    sc = asyncio.run(svc.load_scenario("G"))

    # 1번의 goto 는 '3번 스텝'을 가리켰으므로 그 스텝의 uid 여야 한다
    assert sc.steps[0].on_pass_goto == sc.steps[2].uid


def test_legacy_end_sentinel_converted(svc, tmp_path):
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    sc = asyncio.run(svc.load_scenario("G"))
    assert sc.steps[2].on_fail_goto == GOTO_END


def test_legacy_dangling_goto_becomes_none(svc, tmp_path):
    """존재하지 않는 id 를 가리키던 goto 는 비운다."""
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    sc = asyncio.run(svc.load_scenario("G"))
    assert sc.steps[3].on_pass_goto is None


def test_goto_migration_is_persisted_and_idempotent(svc, tmp_path):
    """변환 결과가 저장되고, 재로드해도 uid 가 그대로여야 한다."""
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    first = asyncio.run(svc.load_scenario("G"))
    target = first.steps[0].on_pass_goto

    saved = json.loads((tmp_path / "scenarios" / "G.json").read_text(encoding="utf-8"))
    assert saved["steps"][0]["on_pass_goto"] == target

    second = asyncio.run(svc.load_scenario("G"))
    assert second.steps[0].on_pass_goto == target
    assert second.steps[0].on_pass_goto == second.steps[2].uid


# ----------------------------------------------------------------------
# uid 중복 — 스텝 복사/붙여넣기가 uid 까지 복제하는 경우
# ----------------------------------------------------------------------

def test_duplicate_uids_are_deduped():
    sc = Scenario(name="T", steps=[mk_step(1), mk_step(2)])
    sc.steps[1].uid = sc.steps[0].uid

    changed = rs._dedupe_step_uids(sc)
    assert changed
    assert sc.steps[0].uid != sc.steps[1].uid


def test_dedupe_is_noop_when_already_unique():
    sc = Scenario(name="T", steps=[mk_step(1), mk_step(2)])
    assert rs._dedupe_step_uids(sc) is False


def test_dedupe_fills_empty_uid():
    sc = Scenario(name="T", steps=[mk_step(1)])
    sc.steps[0].uid = ""
    assert rs._dedupe_step_uids(sc) is True
    assert sc.steps[0].uid


# ----------------------------------------------------------------------
# 편집 내성 — 2b/2c 가 고쳐야 할 대상
# ----------------------------------------------------------------------

def reindex(steps: list[Step]) -> list[Step]:
    """프론트(RecordPage)의 `id: i + 1` 재부여를 그대로 흉내낸다."""
    for i, s in enumerate(steps):
        s.id = i + 1
    return steps


def test_uid_unaffected_by_reindexing():
    steps = [mk_step(1), mk_step(2), mk_step(3)]
    uids = [s.uid for s in steps]

    steps.insert(0, mk_step(0))          # 맨 앞에 삽입
    reindex(steps)

    assert [s.uid for s in steps[1:]] == uids, "재부여가 uid 를 건드렸다"
    assert [s.id for s in steps] == [1, 2, 3, 4]


def test_goto_survives_step_insertion():
    """1번이 3번을 가리키는 상태에서 맨 앞에 스텝을 삽입해도
    여전히 '원래 그 스텝'을 가리켜야 한다. (2b 의 존재 이유)"""
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    s1.on_pass_goto = s3.uid
    steps = [s1, s2, s3]

    steps.insert(0, mk_step(0))
    reindex(steps)   # s3 의 id 는 3 → 4 로 밀림

    resolved = next(s for s in steps if s.uid == s1.on_pass_goto)
    assert resolved is s3
    assert resolved.id == 4, "삽입으로 위치는 밀렸지만 참조는 그대로여야 한다"


def test_goto_survives_step_deletion_of_unrelated_step():
    """관계없는 앞 스텝을 지워도 대상은 유지된다."""
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    s1.on_fail_goto = s3.uid
    steps = [s1, s2, s3]

    steps.remove(s2)
    reindex(steps)

    assert next(s for s in steps if s.uid == s1.on_fail_goto) is s3


def test_goto_survives_reorder():
    s1, s2, s3 = mk_step(1), mk_step(2), mk_step(3)
    s1.on_pass_goto = s3.uid
    steps = [s1, s3, s2]          # 순서 변경
    reindex(steps)

    assert next(s for s in steps if s.uid == s1.on_pass_goto) is s3


def test_goto_to_deleted_step_becomes_unresolvable():
    """대상 스텝이 삭제되면 참조는 해결 불가 상태가 되고,
    재생 시 _resolve_next_index 가 자연 진행으로 폴백한다."""
    s1, s2 = mk_step(1), mk_step(2)
    s1.on_pass_goto = s2.uid
    steps = [s1]                  # s2 삭제
    reindex(steps)

    assert not any(s.uid == s1.on_pass_goto for s in steps)


def test_loop_range_survives_step_insertion():
    """2~3 구간반복 상태에서 맨 앞에 스텝을 삽입해도 같은 스텝들을 감싸야 한다."""
    steps = [mk_step(1), mk_step(2), mk_step(3), mk_step(4)]
    loop = LoopRange(start_uid=steps[1].uid, end_uid=steps[2].uid, count=2)
    boundary = (steps[1], steps[2])

    steps.insert(0, mk_step(0))
    reindex(steps)   # 경계 스텝의 id 는 2,3 → 3,4 로 밀림

    assert next(s for s in steps if s.uid == loop.start_uid) is boundary[0]
    assert next(s for s in steps if s.uid == loop.end_uid) is boundary[1]
    assert [boundary[0].id, boundary[1].id] == [3, 4]


# ----------------------------------------------------------------------
# 레거시 loops(정수 위치) → 경계 uid 변환
# ----------------------------------------------------------------------

LEGACY_LOOPS = {
    "name": "L",
    "steps": [{"id": i, "type": "wait", "params": {}} for i in range(1, 5)],
    "loops": [
        {"start": 2, "end": 3, "count": 3},
        {"start": 1, "end": 99, "count": 2},   # 경계 없음 → 폐기 대상
    ],
}


def test_legacy_loops_converted_to_uids(svc, tmp_path):
    write_scenario_json(tmp_path, "L", LEGACY_LOOPS)
    sc = asyncio.run(svc.load_scenario("L"))

    assert len(sc.loops) == 1, "경계를 못 찾은 구간은 폐기되어야 한다"
    lp = sc.loops[0]
    assert lp.start_uid == sc.steps[1].uid
    assert lp.end_uid == sc.steps[2].uid
    assert lp.count == 3


def test_legacy_loops_migration_persisted_and_idempotent(svc, tmp_path):
    write_scenario_json(tmp_path, "L", LEGACY_LOOPS)
    first = asyncio.run(svc.load_scenario("L"))
    expected = (first.loops[0].start_uid, first.loops[0].end_uid)

    saved = json.loads((tmp_path / "scenarios" / "L.json").read_text(encoding="utf-8"))
    assert saved["loops"][0]["start_uid"] == expected[0]
    assert "start" not in saved["loops"][0], "레거시 정수 필드가 남아 있으면 안 된다"

    second = asyncio.run(svc.load_scenario("L"))
    assert (second.loops[0].start_uid, second.loops[0].end_uid) == expected


# ----------------------------------------------------------------------
# 스키마 버전
# ----------------------------------------------------------------------

def test_legacy_scenario_gets_current_schema_version(svc, tmp_path):
    """구형 파일(버전 필드 없음)은 마이그레이션 후 현재 버전으로 기록된다."""
    # ⚠️ 파일명은 scenario.name 과 일치해야 한다 — save_scenario 가 name 기준으로 쓰므로
    #    엉뚱한 파일명을 쓰면 원본이 그대로 남아 테스트가 거짓 통과한다.
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    sc = asyncio.run(svc.load_scenario("G"))
    assert sc.schema_version == rs.SCENARIO_SCHEMA_VERSION

    saved = json.loads((tmp_path / "scenarios" / "G.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == rs.SCENARIO_SCHEMA_VERSION


def test_future_schema_version_is_rejected(svc, tmp_path):
    """아는 것보다 새로운 스키마는 조용히 오해석하지 않고 거부한다."""
    payload = dict(LEGACY_GOTO)
    payload["schema_version"] = rs.SCENARIO_SCHEMA_VERSION + 1
    write_scenario_json(tmp_path, "G", payload)

    with pytest.raises(ValueError, match="최신 형식"):
        asyncio.run(svc.load_scenario("G"))


def test_current_version_file_is_not_rewritten(svc, tmp_path):
    """이미 최신 형식이면 로드만으로 다시 저장하지 않는다(불필요한 쓰기 방지)."""
    write_scenario_json(tmp_path, "G", LEGACY_GOTO)
    asyncio.run(svc.load_scenario("G"))          # 1회차: 마이그레이션 + 저장
    path = tmp_path / "scenarios" / "G.json"
    before = path.read_bytes()
    assert b"schema_version" in before, "1회차에서 실제로 저장되었는지 먼저 확인"

    asyncio.run(svc.load_scenario("G"))          # 2회차: 변경 없어야 함
    assert path.read_bytes() == before
