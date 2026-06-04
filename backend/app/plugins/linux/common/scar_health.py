"""SCAR 준비 상태 머신 (원본 'Ensure SCAR Is Ready').

흐름:
  while retry < max_retry:
    if force_docker_mode: return DOCKER
    if api.is_alive():    return API
    if docker.is_running(): return DOCKER
    reconnect(); force_docker_mode = True; sleep(reconnect_wait_s)
  return NONE

force_docker_mode 는 reconnect 시 True 가 되지만, 우리 배포에선 scar.sh --ui 가 8081 을
되살리므로 무조건 DOCKER 직행이 아니라 8081 이 살아나면 자동으로 래치를 풀고 API 로 복귀한다
(원본은 영구 DOCKER 였음 — 그래서 "첫 연결 실패, 재연결로만 됨" 증상이 났다).
reconnect 후 대기는 고정 sleep 이 아니라 8081 폴링(reconnect_wait_s 상한, 뜨면 즉시 복귀).
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
                # 원본은 무조건 DOCKER 직행이지만, 우리 배포에선 scar.sh --ui 가 8081/3000 을
                # 되살린다. 재기동 직후엔 아직 안 떠서 래치가 걸렸어도, 이후 호출 시 8081 이
                # 올라왔으면 API 로 자동 복귀시킨다 (안 그러면 인스턴스 수명 내내 DOCKER 고정 →
                # "첫 연결 실패, 재연결로만 됨" 증상). 8081 이 여전히 down 이면 종전대로 DOCKER.
                if self.api.is_alive():
                    logger.info("SCAR API recovered after reconnect; clearing force_docker_mode → API")
                    self.force_docker_mode = False
                    return "API"
                logger.info("Force DOCKER mode (reconnect already happened, API still down)")
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

    def _reconnect(self) -> bool:
        """scar.sh 기동 시도. 실제 spawn 성공 여부(ok)를 반환.

        spawn 실패(잘못된 경로/권한 등)면 20s 대기를 건너뛴다 — 어차피 안 뜬다.
        """
        if not self.reconnect_script:
            logger.warning("SCAR reconnect_script not configured; skipping reconnect")
            self.force_docker_mode = True
            return False
        ok = self.docker.start_via_script(
            self.reconnect_script,
            self.reconnect_args,
            self.reconnect_cwd,
        )
        self.force_docker_mode = True
        logger.info("SCAR reconnect started ok=%s; polling 8081 up to %.1fs", ok, self.reconnect_wait_s)
        if ok:
            # 고정 sleep 대신 8081 폴링 — scar.sh --ui 는 20s 보다 오래 걸릴 수 있고(노드+http-server
            # +서비스 init), 8081 이 뜨는 즉시 래치를 풀어 API 모드로 복귀시킨다. reconnect_wait_s=0
            # (테스트) 이면 루프 미진입 → 종전대로 force_docker_mode=True 유지.
            deadline = time.time() + self.reconnect_wait_s
            while time.time() < deadline:
                if self.api.is_alive():
                    self.force_docker_mode = False
                    logger.info("SCAR API came up during reconnect wait; force_docker_mode cleared")
                    break
                time.sleep(min(2.0, self.reconnect_wait_s))
        return ok
