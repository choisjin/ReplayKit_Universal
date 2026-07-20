"""그룹 스텝점프(step_jumps) 키의 uid 전환 테스트.

step_jumps 는 시나리오 파일 **바깥**(groups.json)에 저장되면서 키가 step.id(위치)였다.
시나리오 스텝이 밀려도 리맵이 전혀 없어, 조용히 다른 스텝에 점프가 걸리는 구조였다.
uid 키로 전환하되, 레거시 정수 키는 안전하게 복원할 수 없으므로 폐기한다.
"""

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


def test_scenario_level_jumps_are_untouched(svc, tmp_path):
    """멤버 단위 점프(on_pass_goto/on_fail_goto)는 시나리오 인덱스 참조라 건드리지 않는다."""
    write_groups(tmp_path, {
        "G": [{"name": "S1", "on_pass_goto": {"scenario": 2, "step": 0},
               "on_fail_goto": None, "play_count": 1,
               "step_jumps": {"3": {"on_pass_goto": None}}}],
    })
    groups = svc._load_groups()
    assert groups["G"][0]["on_pass_goto"] == {"scenario": 2, "step": 0}
