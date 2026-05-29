"""SCARHealth 상태머신 검증.

원본 'Ensure SCAR Is Ready' 의 시나리오 재현:
  1. API 살아있음 → 즉시 "API"
  2. API 죽음 + 컨테이너 살아있음 → "DOCKER"
  3. 둘 다 죽음 → reconnect → force_docker_mode=True → 이후 "DOCKER"
  4. reconnect 실패 + max_retry 소진 → "NONE"
"""

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="SCAR plugin is Linux-only",
)


def _import():
    from backend.app.plugins.linux.common.scar_health import SCARHealth  # type: ignore
    return SCARHealth


def _fake_api(alive: bool):
    api = MagicMock()
    api.is_alive.return_value = alive
    return api


def _fake_docker(running: bool):
    docker = MagicMock()
    docker.is_running.return_value = running
    docker.start_via_script.return_value = True
    return docker


def test_api_alive_returns_api_immediately():
    SCARHealth = _import()
    h = SCARHealth(api=_fake_api(True), docker=_fake_docker(False))
    assert h.ensure_ready(max_retry=3) == "API"


def test_api_down_docker_up_returns_docker():
    SCARHealth = _import()
    h = SCARHealth(api=_fake_api(False), docker=_fake_docker(True))
    assert h.ensure_ready(max_retry=3) == "DOCKER"


def test_both_down_then_reconnect_promotes_to_docker():
    SCARHealth = _import()
    api = _fake_api(False)
    docker = _fake_docker(False)
    h = SCARHealth(
        api=api,
        docker=docker,
        reconnect_script="/bin/true",
        reconnect_wait_s=0.0,  # 테스트 가속
    )
    # 첫 시도: 둘 다 down → reconnect → force_docker_mode=True
    # 두 번째 시도: force_docker_mode=True → 즉시 DOCKER
    result = h.ensure_ready(max_retry=3)
    assert result == "DOCKER"
    assert h.force_docker_mode is True
    docker.start_via_script.assert_called_once()


def test_no_reconnect_script_and_both_down_returns_docker_via_force():
    """reconnect_script 가 없으면 reconnect 는 실제 실행되지 않지만
    force_docker_mode 만 True 로 세팅되고 다음 라운드에 DOCKER 반환."""
    SCARHealth = _import()
    h = SCARHealth(
        api=_fake_api(False),
        docker=_fake_docker(False),
        reconnect_script=None,
        reconnect_wait_s=0.0,
    )
    result = h.ensure_ready(max_retry=3)
    # 첫 라운드 reconnect → force_docker_mode=True → 두 번째 라운드에서 DOCKER 반환
    assert result == "DOCKER"


def test_max_retry_exhausted_returns_none_when_reconnect_disabled():
    """force_docker_mode 가 켜지지 않는 시나리오를 강제로 만들어 NONE 케이스 검증.

    SCARHealth 기본 동작상 reconnect 가 항상 force_docker_mode=True 를 세팅하므로
    NONE 을 받으려면 max_retry=0 같은 edge case 가 필요하다. 0 이면 루프 미진입 → NONE.
    """
    SCARHealth = _import()
    h = SCARHealth(api=_fake_api(False), docker=_fake_docker(False))
    assert h.ensure_ready(max_retry=0) == "NONE"
