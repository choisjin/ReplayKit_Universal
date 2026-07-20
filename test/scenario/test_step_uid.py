"""step.uid 부여/영속화/유일성 및 편집 내성 테스트.

배경: step.id 는 스텝 삽입/삭제/순서변경 때마다 배열 위치(i+1)로 재부여되는
'표시용 순번'이다. 영속 참조(기대이미지 파일명, 조건부이동, 구간반복)를 id 로
잡으면 편집 시 조용히 다른 스텝을 가리키게 된다.
실제 사고: 20번 크롭 재추가가 21번의 _step_020_crop_00.png 를 덮어써 오비교 발생.
"""

import asyncio
import json

import pytest

from backend.app.models.scenario import LoopRange, Scenario, Step, StepType
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


@pytest.mark.xfail(
    reason="2b 미완료: on_pass_goto 가 step.id(위치) 기준이라 삽입 시 대상이 밀린다",
    strict=True,
)
def test_goto_survives_step_insertion():
    """1번이 3번을 가리키는 상태에서 맨 앞에 스텝을 삽입해도
    여전히 '원래 그 스텝'을 가리켜야 한다."""
    s1, s2, s3 = mk_step(1, on_pass_goto=3), mk_step(2), mk_step(3)
    target_uid = s3.uid
    steps = [s1, s2, s3]

    steps.insert(0, mk_step(0))
    reindex(steps)

    # id 기준이면 on_pass_goto=3 이 이제 s2(새 id 3)를 가리킨다 → 오작동
    resolved = next(s for s in steps if s.id == s1.on_pass_goto)
    assert resolved.uid == target_uid


@pytest.mark.xfail(
    reason="2c 미완료: LoopRange.start/end 가 step.id(위치) 기준이라 삽입 시 구간이 밀린다",
    strict=True,
)
def test_loop_range_survives_step_insertion():
    """2~3 구간반복 상태에서 맨 앞에 스텝을 삽입해도 같은 스텝들을 감싸야 한다."""
    steps = [mk_step(1), mk_step(2), mk_step(3), mk_step(4)]
    loop = LoopRange(start=2, end=3, count=2)
    start_uid = steps[1].uid
    end_uid = steps[2].uid

    steps.insert(0, mk_step(0))
    reindex(steps)

    assert next(s for s in steps if s.id == loop.start).uid == start_uid
    assert next(s for s in steps if s.id == loop.end).uid == end_uid
