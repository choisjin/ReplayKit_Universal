"""THSignal 검증 — trigger 감지 즉시 콜백, timeout 처리, e2e_ms 측정.

client.py 대신 더미 bash 스크립트를 실행해 stdout 에 토큰을 흘려보낸다.
THSignal 이 호출하는 명령은 [python_bin, 'client.py', ...] 형태인데 우리는
python_bin='bash' 와 client_dir 트릭으로 우회한다 — 더미 client.py 를 만들어
인자를 무시하고 trigger 만 찍게 한다.
"""

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="TH plugin is Linux-only",
)


def _import():
    from backend.app.plugins.linux.common.th_signal import THSignal  # type: ignore
    return THSignal


@pytest.fixture
def dummy_client_dir(tmp_path: Path) -> Path:
    """더미 client.py 가 들어있는 디렉터리.

    인자를 무시하고 0.1s 후 'GEAR_LEVER_ACCEPTED_T_REVERSE' 한 줄을 stdout 으로 흘린다.
    """
    client = tmp_path / "client.py"
    client.write_text(textwrap.dedent("""
        import sys, time
        time.sleep(0.1)
        sys.stdout.write('GEAR_LEVER_ACCEPTED_T_REVERSE\\n')
        sys.stdout.flush()
        time.sleep(0.1)
    """).lstrip())
    return tmp_path


def test_send_detects_trigger_and_measures_e2e(dummy_client_dir: Path):
    THSignal = _import()
    th = THSignal(client_dir=str(dummy_client_dir), th_addr="127.0.0.1", python_bin=sys.executable)

    hits: list[float] = []
    sr = th.send(
        topic_name="dummy_topic",
        json_path="/dev/null",
        trigger=b"GEAR_LEVER_ACCEPTED_T_REVERSE",
        on_trigger=lambda ts: hits.append(ts),
        timeout=3.0,
    )

    assert sr.trigger_hit == b"GEAR_LEVER_ACCEPTED_T_REVERSE"
    assert len(hits) == 1
    assert sr.e2e_ms is not None
    # 더미가 0.1s sleep 후 토큰을 찍으므로 100ms 이상 + 1초 이내
    assert 80.0 < sr.e2e_ms < 1500.0
    assert not sr.timed_out


def test_send_timeout_returns_no_trigger(dummy_client_dir: Path, tmp_path: Path):
    THSignal = _import()
    # trigger 가 들어있지 않은 client
    silent_dir = tmp_path / "silent"
    silent_dir.mkdir()
    (silent_dir / "client.py").write_text("import time; time.sleep(5)\n")

    th = THSignal(client_dir=str(silent_dir), th_addr="127.0.0.1", python_bin=sys.executable)
    sr = th.send(
        topic_name="dummy",
        json_path="/dev/null",
        trigger=b"NEVER_APPEARS",
        on_trigger=lambda ts: None,
        timeout=0.3,
    )
    assert sr.timed_out
    assert sr.trigger_hit is None


def test_send_fire_and_forget_no_trigger(dummy_client_dir: Path):
    THSignal = _import()
    th = THSignal(client_dir=str(dummy_client_dir), th_addr="127.0.0.1", python_bin=sys.executable)
    sr = th.send(
        topic_name="dummy",
        json_path="/dev/null",
        trigger=None,
        on_trigger=None,
        timeout=3.0,
    )
    assert sr.rc == 0
    assert b"GEAR_LEVER_ACCEPTED_T_REVERSE" in sr.stdout
    assert sr.trigger_hit is None
