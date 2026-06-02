"""SCAR REST API 래퍼.

원본 (Reference/Renault_CDC_Plugin/SCAR&TH.txt) 의 키워드:
  - Check SCAR API Is Alive — GET /
  - Send request in SCAR by using API — POST <url> json=<data> headers=<headers>

requests 의 Session 을 한 번만 만들어 keep-alive 로 재사용한다.
원본은 매 호출마다 Create Session 을 호출했지만 비용일 뿐 의미는 없음.
"""

from __future__ import annotations

import logging
from typing import Optional

import requests  # type: ignore


logger = logging.getLogger(__name__)


class SCARApi:
    """http://localhost:8081 (기본) 의 SCAR REST 인터페이스."""

    def __init__(
        self,
        base_url: str = "http://localhost:8081",
        verify: bool = False,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.verify = verify
        self.timeout = timeout
        self._session = requests.Session()
        self._session.verify = verify

    # ─── readiness ────────────────────────────────────
    def is_alive(self) -> bool:
        """GET / 가 '응답'을 주면(상태코드 무관) alive — 연결거부/타임아웃만 down.

        8081 은 SCAR UI(정적 http-server)라 `/` 에 404 를 줄 수 있다. 2xx~3xx 로
        좁히면 살아있는 서버를 down 으로 오판함(README: "200/404 등 응답"이면 up).
        따라서 reachable(응답 도착) = alive 로 본다.
        """
        try:
            self._session.get(
                self.base_url + "/",
                timeout=min(self.timeout, 3.0),
            )
        except requests.RequestException as e:
            logger.debug("SCAR API not alive: %s", e)
            return False
        return True

    # ─── POST ─────────────────────────────────────────
    def post(
        self,
        url: str,
        headers: Optional[dict] = None,
        data: Optional[dict] = None,
    ) -> Optional[requests.Response]:
        """원본 'Send request in SCAR by using API' 등가.

        실패해도 예외를 던지지 않는다(원본 Robot 키워드와 동일 — 호출자가 결정).
        status != 200 이면 WARN 로깅만.
        """
        try:
            resp = self._session.post(
                url,
                json=data or {},
                headers=headers or {},
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            logger.warning("SCAR API POST failed (%s): %s", url, e)
            return None

        if resp.status_code != 200:
            logger.warning(
                "SCAR API returned non-200 status: %s body=%s",
                resp.status_code,
                resp.text[:512],
            )
        return resp

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass
