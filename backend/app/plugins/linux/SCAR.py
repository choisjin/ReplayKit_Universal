"""SCAR 모듈 — Linux 전용 SDV(Software Defined Vehicle) 제어 플러그인.

원본:
  Reference/Renault_CDC_Plugin/SCAR&TH.txt (Robot 키워드)
  Reference/Renault_CDC_Plugin/RVC_Performance.txt (호출 시퀀스)

동작:
  매 호출마다 ensure_ready() 가 API / DOCKER / NONE 모드를 자동 판별.
    API:    POST http://localhost:8081/...
    DOCKER: docker exec scar bash -c <equivalent script>
    NONE:   "FAIL: SCAR not available"
  한 번이라도 reconnect 가 일어났다면 그 후로는 force_docker_mode 가 True 가 되어
  API 경로를 다시 시도하지 않음 (원본 'Ensure SCAR Is Ready' 와 동일).

시나리오 노출 메서드:
  - Ready(max_retry=3)               -> "API" / "DOCKER" / "NONE"
  - SendApi(url, headers, data)      -> 응답 요약 또는 "FAIL: ..."
  - Exec(cmd, timeout=300)           -> docker exec 결과
  - Reconnect()                      -> scar.sh 백그라운드 spawn + 20s 대기
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .common.scar_api import SCARApi
from .common.scar_docker import SCARDocker
from .common.scar_health import SCARHealth


logger = logging.getLogger(__name__)


class SCAR:
    """Linux SCAR 플러그인."""

    def __init__(
        self,
        api_base: str = "http://localhost:8081",
        container: str = "scar",
        reconnect_script: Optional[str] = None,
        reconnect_args: Optional[str] = None,   # 공백 구분 문자열 (시나리오 호환)
        reconnect_cwd: Optional[str] = None,
        reconnect_wait_s: float = 20.0,
    ):
        self._api = SCARApi(base_url=api_base)
        self._docker = SCARDocker(container=container)
        args_list = reconnect_args.split() if reconnect_args else None
        self._health = SCARHealth(
            api=self._api,
            docker=self._docker,
            reconnect_script=reconnect_script,
            reconnect_args=args_list,
            reconnect_cwd=reconnect_cwd,
            reconnect_wait_s=reconnect_wait_s,
        )

    # ── 시나리오 노출 ─────────────────────────────────
    def Ready(self, max_retry: int = 3) -> str:
        """현재 SCAR 가 사용 가능한 모드를 결정. 'API' / 'DOCKER' / 'NONE'."""
        return self._health.ensure_ready(int(max_retry))

    def SendApi(
        self,
        url: str,
        headers: str = "",
        data: str = "",
        max_retry: int = 3,
    ) -> str:
        """원본 'Send request in SCAR by using API'.

        headers/data 는 JSON 문자열로 전달 (Robot 키워드와 동일 호환).
        Ready 결과가 API 가 아니면 FAIL — Docker 모드에서 동등 동작이 필요한 호출은
        Exec() 로 명시적으로 부르는 것이 옳다.
        """
        mode = self.Ready(int(max_retry))
        if mode == "NONE":
            return "FAIL: SCAR not available"
        if mode == "DOCKER":
            return "FAIL: SCAR API down, use Exec() in docker mode"

        try:
            hdr = json.loads(headers) if headers else {}
            body = json.loads(data) if data else {}
        except json.JSONDecodeError as e:
            return f"FAIL: bad JSON in headers/data: {e}"

        resp = self._api.post(url, headers=hdr, data=body)
        if resp is None:
            return "FAIL: SCAR API POST request failed"
        snippet = resp.text[:512] if resp.text else ""
        return f"status={resp.status_code}\n{snippet}".strip()

    def Exec(self, cmd: str, timeout: int = 300, max_retry: int = 3) -> str:
        """원본 'Exec In SCAR Container'. docker exec scar bash -c <cmd>.

        Ready 결과가 NONE 이면 FAIL. API 든 DOCKER 든 컨테이너만 떠있으면 동작 가능.
        """
        mode = self.Ready(int(max_retry))
        if mode == "NONE":
            return "FAIL: SCAR not available"
        if not self._docker.is_running():
            return "FAIL: SCAR container not running"

        res = self._docker.exec(cmd, timeout=float(timeout))
        if res.timed_out:
            return f"FAIL: docker exec timeout ({timeout}s) rc={res.rc}"
        tail = res.stdout[-2048:].decode("utf-8", "replace").strip()
        header = f"rc={res.rc}"
        return f"{header}\n{tail}" if tail else header

    def Reconnect(self) -> str:
        """원본 'Reconnect SCAR'. setsid 로 scar.sh 백그라운드 spawn + 20s 대기."""
        if not self._health.reconnect_script:
            return "FAIL: reconnect_script not configured"
        # SCARHealth._reconnect 와 동일한 부수효과를 일으키기 위해 헬스 객체 경유
        self._health._reconnect()  # noqa: SLF001 — 의도적 호출
        return "DOCKER" if self._health.force_docker_mode else "NONE"
