"""proc.spawn + proc.scan_until 의 latency / 정합성 검증.

Linux 전용. trigger 가 같은 줄에 들어와도 (no newline) 감지되는지 확인.
"""

import sys
import time

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="proc helpers are Linux-only",
)


def _import():
    from backend.app.plugins.linux.common import proc  # type: ignore
    return proc


def test_scan_until_finds_token_without_newline():
    proc = _import()
    # printf 는 자동 줄바꿈 없음 — 토큰을 줄바꿈 없이 흘려보내고 잠시 sleep
    p = proc.spawn(["bash", "-c", "printf 'HELLO_TRIGGER_X'; sleep 0.5"])
    hits = []

    def cb(tok: bytes, ts: float) -> None:
        hits.append((tok, ts))

    spawn_ts = time.monotonic()
    sr = proc.scan_until(
        p,
        tokens=[b"HELLO_TRIGGER"],
        on_match=cb,
        timeout=3.0,
        spawn_ts=spawn_ts,
    )

    assert sr.trigger_hit == b"HELLO_TRIGGER"
    assert len(hits) == 1
    assert sr.e2e_ms is not None and sr.e2e_ms < 1000.0  # 1초 이내 감지
    assert b"HELLO_TRIGGER_X" in sr.stdout
    assert not sr.timed_out


def test_scan_until_timeout_when_no_trigger():
    proc = _import()
    p = proc.spawn(["bash", "-c", "echo waiting; sleep 2"])
    sr = proc.scan_until(
        p,
        tokens=[b"WILL_NOT_APPEAR"],
        on_match=lambda t, ts: None,
        timeout=0.4,
    )
    assert sr.timed_out
    assert sr.trigger_hit is None


def test_scan_until_no_tokens_collects_stdout():
    proc = _import()
    p = proc.spawn(["bash", "-c", "echo line1; echo line2"])
    sr = proc.scan_until(p, tokens=[], on_match=None, timeout=3.0)
    assert sr.rc == 0
    assert b"line1" in sr.stdout and b"line2" in sr.stdout


def test_terminate_kills_process_group():
    proc = _import()
    # 자식 sleep 을 새 세션으로 띄움 (proc.spawn 이 start_new_session=True)
    p = proc.spawn(["bash", "-c", "sleep 30"])
    assert p.poll() is None
    proc.terminate(p, grace=0.5)
    assert p.poll() is not None
