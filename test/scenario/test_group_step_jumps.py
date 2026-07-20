"""그룹 스텝점프(step_jumps) 키의 uid 전환 테스트.

step_jumps 는 시나리오 파일 **바깥**(groups.json)에 저장되면서 키가 step.id(위치)였다.
시나리오 스텝이 밀려도 리맵이 전혀 없어, 조용히 다른 스텝에 점프가 걸리는 구조였다.
uid 키로 전환하되, 레거시 정수 키는 안전하게 복원할 수 없으므로 폐기한다.
"""

import asyncio
import json

import pytest

import backend.app.services.recording_service as rs


@pytest.fixture
def svc(tmp_path, monkeypatch):
    monkeypatch.setattr(rs, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(rs, "GROUPS_FILE", tmp_path / "scenarios" / "groups.json")
    (tmp_path / "scenarios").mkdir()
    return rs.RecordingService.__new__(rs.RecordingService)


def write_groups(tmp_path, payload: dict) -> None:
    (tmp_path / "scenarios" / "groups.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def test_legacy_int_keyed_step_jumps_are_discarded(svc, tmp_path):
    write_groups(tmp_path, {
        "G": [{
            "name": "S1", "on_pass_goto": None, "on_fail_goto": None, "play_count": 1,
            "step_jumps": {
                "3": {"on_pass_goto": {"scenario": 1, "step": 0}, "on_fail_goto": None},
                "7": {"on_pass_goto": None, "on_fail_goto": {"scenario": 0, "step": 0}},
            },
        }],
    })
    groups = svc._load_groups()
    assert "step_jumps" not in groups["G"][0], "레거시 정수 키 점프는 폐기되어야 한다"


def test_discard_is_persisted(svc, tmp_path):
    write_groups(tmp_path, {
        "G": [{"name": "S1", "play_count": 1,
               "step_jumps": {"3": {"on_pass_goto": {"scenario": 1, "step": 0}}}}],
    })
    svc._load_groups()
    saved = json.loads((tmp_path / "scenarios" / "groups.json").read_text(encoding="utf-8"))
    assert "step_jumps" not in saved["G"][0]


def test_uid_keyed_step_jumps_are_kept(svc, tmp_path):
    """uid 키(8자리 hex)는 그대로 유지된다."""
    write_groups(tmp_path, {
        "G": [{"name": "S1", "play_count": 1,
               "step_jumps": {"a3f9c1d2": {"on_pass_goto": {"scenario": 1, "step": 0}}}}],
    })
    groups = svc._load_groups()
    assert "a3f9c1d2" in groups["G"][0]["step_jumps"]


def test_mixed_keys_keep_only_uid(svc, tmp_path):
    write_groups(tmp_path, {
        "G": [{"name": "S1", "play_count": 1, "step_jumps": {
            "5": {"on_pass_goto": {"scenario": 1, "step": 0}},
            "beef0001": {"on_fail_goto": {"scenario": 2, "step": 0}},
        }}],
    })
    groups = svc._load_groups()
    assert list(groups["G"][0]["step_jumps"]) == ["beef0001"]


def test_member_jumps_converted_to_member_uid(svc, tmp_path):
    """멤버 단위 점프의 '대상 시나리오'는 인덱스 → member_uid 로 변환된다.

    인덱스는 순서변경/삭제로 바뀌고, 같은 시나리오가 중복으로 담길 수 있어
    이름만으로도 특정할 수 없기 때문.
    """
    write_groups(tmp_path, {
        "G": [
            {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
            {"name": "B", "on_pass_goto": None, "play_count": 1},
        ],
    })
    groups = svc._load_groups()
    a, b = groups["G"]
    assert a["on_pass_goto"]["member_uid"] == b["uid"]
    assert a["on_pass_goto"]["scenario_name"] == "B"


def test_member_jump_step_index_is_reset(svc, tmp_path):
    """스텝 인덱스는 uid 로 복원할 수 없다(대상 시나리오가 편집됐을 수 있음).
    조용히 틀린 스텝을 가리키느니 '처음부터'로 초기화한다."""
    write_groups(tmp_path, {
        "G": [
            {"name": "A", "on_pass_goto": {"scenario": 1, "step": 7}, "play_count": 1},
            {"name": "B", "play_count": 1},
        ],
    })
    groups = svc._load_groups()
    assert groups["G"][0]["on_pass_goto"]["step_uid"] is None


def test_end_sentinel_converted(svc, tmp_path):
    write_groups(tmp_path, {
        "G": [{"name": "A", "on_fail_goto": {"scenario": -1, "step": 0}, "play_count": 1}],
    })
    groups = svc._load_groups()
    assert groups["G"][0]["on_fail_goto"]["member_uid"] == rs.GROUP_JUMP_END


def test_jump_to_missing_member_is_cleared(svc, tmp_path):
    """대상 인덱스가 그룹 범위를 벗어나면 점프를 해제한다."""
    write_groups(tmp_path, {
        "G": [{"name": "A", "on_pass_goto": {"scenario": 5, "step": 0}, "play_count": 1}],
    })
    groups = svc._load_groups()
    assert groups["G"][0]["on_pass_goto"] is None


# ----------------------------------------------------------------------
# 멤버 uid — 중복 시나리오 구분
# ----------------------------------------------------------------------

def test_members_get_unique_uids_even_when_duplicated(svc, tmp_path):
    """같은 시나리오를 중복으로 담아도 멤버끼리는 구분된다."""
    write_groups(tmp_path, {
        "G": [{"name": "A", "play_count": 1}, {"name": "A", "play_count": 1}],
    })
    groups = svc._load_groups()
    uids = [m["uid"] for m in groups["G"]]
    assert len(set(uids)) == 2, "중복 시나리오도 서로 다른 uid 를 가져야 한다"


def test_member_uids_persisted_and_stable(svc, tmp_path):
    write_groups(tmp_path, {"G": [{"name": "A", "play_count": 1}]})
    first = svc._load_groups()["G"][0]["uid"]

    saved = json.loads((tmp_path / "scenarios" / "groups.json").read_text(encoding="utf-8"))
    assert saved["G"][0]["uid"] == first

    assert svc._load_groups()["G"][0]["uid"] == first


def test_duplicate_member_uids_are_deduped(svc, tmp_path):
    write_groups(tmp_path, {
        "G": [{"uid": "dup00001", "name": "A"}, {"uid": "dup00001", "name": "B"}],
    })
    groups = svc._load_groups()
    assert groups["G"][0]["uid"] != groups["G"][1]["uid"]


def test_reorder_does_not_break_member_jump(svc, tmp_path):
    """순서를 바꿔도 점프는 원래 멤버를 계속 가리킨다 (인덱스 리맵 없이)."""
    write_groups(tmp_path, {
        "G": [
            {"name": "A", "on_pass_goto": {"scenario": 2, "step": 0}, "play_count": 1},
            {"name": "B", "play_count": 1},
            {"name": "C", "play_count": 1},
        ],
    })
    before = svc._load_groups()["G"]
    target_uid = before[0]["on_pass_goto"]["member_uid"]
    assert target_uid == before[2]["uid"]      # C 를 가리킴

    svc.reorder_group("G", [2, 1, 0])          # C, B, A 로 뒤집기
    after = svc._load_groups()["G"]
    still = next(m for m in after if m["uid"] == target_uid)
    assert still["name"] == "C", "순서를 바꿔도 대상은 여전히 C 여야 한다"


# ----------------------------------------------------------------------
# 재생 전 점프 대상 검증
# ----------------------------------------------------------------------

def _write_scenario(tmp_path, name: str, step_uids: list[str]) -> None:
    (tmp_path / "scenarios" / f"{name}.json").write_text(json.dumps({
        "schema_version": 2, "name": name,
        "steps": [{"id": i + 1, "uid": u, "type": "wait", "params": {}}
                  for i, u in enumerate(step_uids)],
    }), encoding="utf-8")


@pytest.fixture
def svc_full(tmp_path, monkeypatch):
    """시나리오 파일까지 읽어야 하는 검증 테스트용 (SCREENSHOTS_DIR 도 필요)."""
    monkeypatch.setattr(rs, "SCENARIOS_DIR", tmp_path / "scenarios")
    monkeypatch.setattr(rs, "SCREENSHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(rs, "GROUPS_FILE", tmp_path / "scenarios" / "groups.json")
    (tmp_path / "scenarios").mkdir()
    (tmp_path / "shots").mkdir()
    return rs.RecordingService.__new__(rs.RecordingService)


def test_valid_jumps_report_no_problems(svc_full, tmp_path):
    _write_scenario(tmp_path, "A", ["aaaa0001"])
    _write_scenario(tmp_path, "B", ["bbbb0001", "bbbb0002"])
    write_groups(tmp_path, {"G": [
        {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
        {"name": "B", "play_count": 1},
    ]})
    groups = svc_full._load_groups()
    # 마이그레이션은 step_uid 를 비우므로(처음부터) 명시적으로 유효한 값을 넣어 검증
    groups["G"][0]["on_pass_goto"]["step_uid"] = "bbbb0002"
    svc_full._save_groups(groups)

    assert asyncio.run(svc_full.validate_group_jumps("G")) == []


def test_deleted_target_step_is_reported(svc_full, tmp_path):
    _write_scenario(tmp_path, "A", ["aaaa0001"])
    _write_scenario(tmp_path, "B", ["bbbb0001"])
    write_groups(tmp_path, {"G": [
        {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
        {"name": "B", "play_count": 1},
    ]})
    groups = svc_full._load_groups()
    groups["G"][0]["on_pass_goto"]["step_uid"] = "gone0000"   # 삭제된 스텝
    svc_full._save_groups(groups)

    problems = asyncio.run(svc_full.validate_group_jumps("G"))
    assert len(problems) == 1
    assert "'B' 의 대상 스텝이 삭제" in problems[0]
    assert "Pass" in problems[0]


def test_deleted_target_member_is_reported(svc_full, tmp_path):
    _write_scenario(tmp_path, "A", ["aaaa0001"])
    write_groups(tmp_path, {"G": [
        {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
        {"name": "B", "play_count": 1},
    ]})
    groups = svc_full._load_groups()
    groups["G"].pop(1)                                        # 대상 멤버 삭제
    svc_full._save_groups(groups)

    problems = asyncio.run(svc_full.validate_group_jumps("G"))
    assert len(problems) == 1
    assert "그룹에서 삭제" in problems[0]
    assert "'B'" in problems[0]


def test_missing_target_scenario_file_is_reported(svc_full, tmp_path):
    _write_scenario(tmp_path, "A", ["aaaa0001"])
    write_groups(tmp_path, {"G": [
        {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
        {"name": "B", "play_count": 1},          # B.json 을 만들지 않음
    ]})
    groups = svc_full._load_groups()
    groups["G"][0]["on_pass_goto"]["step_uid"] = "bbbb0001"
    svc_full._save_groups(groups)

    problems = asyncio.run(svc_full.validate_group_jumps("G"))
    assert any("찾을 수 없습니다" in p for p in problems)


def test_jump_without_step_uid_needs_no_scenario_read(svc_full, tmp_path):
    """'처음부터' 점프는 대상 시나리오를 읽지 않아도 유효하다."""
    write_groups(tmp_path, {"G": [
        {"name": "A", "on_pass_goto": {"scenario": 1, "step": 0}, "play_count": 1},
        {"name": "B", "play_count": 1},
    ]})
    assert asyncio.run(svc_full.validate_group_jumps("G")) == []
