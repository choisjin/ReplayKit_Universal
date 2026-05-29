"""SCAR 준비 상태 머신 (원본 'Ensure SCAR Is Ready').

흐름:
  while retry < max_retry:
    if force_docker_mode: return DOCKER
    if api.is_alive():    return API
    if docker.is_running(): return DOCKER
    reconnect(); force_docker_mode = True; sleep(reconnect_wait_s)
  return NONE

force_docker_mode 는 인스턴스 속성으로 유지 — 한 번 reconnect 가 일어났다면
이후 호출은 API 를 다시 시도하지 않고 바로 DOCKER 로 직행 (원본과 동일).
"""

from __future__ import annotations

import logging
import time
from typing import Literal, Optional

from .scar_api import SCARApi
from .scar_docker import SCARDocker


logger = logging.getLogger(__name__)

Mode = Literal["API", "DOCKER", "NONE"]


class SCARHealth:
    def __init__(
        self,
        api: SCARApi,
        docker: SCARDocker,
        reconnect_script: Optional[str] = None,
        reconnect_args: Optional[list[str]] = None,
        reconnect_cwd: Optional[str] = None,
        reconnect_wait_s: float = 20.0,
    ):
        self.api = api
        self.docker = docker
        self.reconnect_script = reconnect_script
        self.reconnect_args = reconnect_args
        self.reconnect_cwd = reconnect_cwd
        self.reconnect_wait_s = reconnect_wait_s
        self.force_docker_mode = False

    def ensure_ready(self, max_retry: int = 3) -> Mode:
        for attempt in range(max_retry):
            logger.info("SCAR readiness check (attempt %d/%d)", attempt + 1, max_retry)

            if self.force_docker_mode:
                logger.info("Force DOCKER mode (reconnect already happened)")
                return "DOCKER"

            if self.api.is_alive():
                logger.info("SCAR API is alive")
                return "API"

            logger.warning("SCAR API is not alive. Check docker...")
            if self.docker.is_running():
                logger.info("SCAR container is running (API not ready)")
                return "DOCKER"

            logger.warning("SCAR API and docker are both down. Try reconnect...")
            self._reconnect()

        logger.warning("SCAR is not ready after %d retries. Continue without SCAR.", max_retry)
        return "NONE"

    def _reconnect(self) -> None:
        if not self.reconnect_script:
            logger.warning("SCAR reconnect_script not configured; skipping reconnect")
            self.force_docker_mode = True
            return
        ok = self.docker.start_via_script(
            self.reconnect_script,
            self.reconnect_args,
            self.reconnect_cwd,
        )
        self.force_docker_mode = True
        logger.info("SCAR reconnect started ok=%s; waiting %.1fs", ok, self.reconnect_wait_s)
        time.sleep(self.reconnect_wait_s)
