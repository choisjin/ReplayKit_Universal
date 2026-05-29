"""Test Harness 신호 송신.

원본 (Reference/Renault_CDC_Plugin/TH_Lib.py 와 RVC_Performance.txt) 의
`Send Signal To Test Harness`, `Send Signal And Update Panel` 두 키워드를
공용 Python API 로 합친다.

핵심 차이:
  - subprocess.Popen + readline() 대신 proc.scan_until 로 raw byte 스캔
  - trigger 가 같은 줄에 들어와도 줄바꿈 대기 없이 즉시 콜백
  - SignalResult 로 rc/stdout/trigger 시점/E2E ms 를 한 번에 회수
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import proc


@dataclass
class SignalResult:
    rc: Optional[int]
    stdout: bytes
    trigger_hit: Optional[bytes]
    trigger_ts: Optional[float]
    e2e_ms: Optional[float]
    timed_out: bool


class THSignal:
    """Test Harness 의 client.py 를 한 번 호출하는 sender.

    원본 Robot 키워드와 호환되는 인자:
      client_dir: client.py 가 있는 디렉터리
      th_addr:    --ip_address 로 전달할 TH 브로커 주소
      python_bin: client.py 실행에 쓸 python 바이너리 (기본 python3)
    """

    def __init__(
        self,
        client_dir: str,
        th_addr: str,
        python_bin: str = "python3",
        log_level: str = "DEBUG",
    ):
        self.client_dir = client_dir
        self.th_addr = th_addr
        self.python_bin = python_bin
        self.log_level = log_level

    def _build_cmd(self, topic_name: str, json_path: str) -> list[str]:
        return [
            self.python_bin,
            "client.py",
            "--pub_topic_name", topic_name,
            "--json_path", json_path,
            "--ip_address", self.th_addr,
            "--log_level", self.log_level,
        ]

    def send(
        self,
        topic_name: str,
        json_path: str,
        trigger: Optional[bytes] = None,
        on_trigger: Optional[Callable[[float], None]] = None,
        timeout: float = 10.0,
    ) -> SignalResult:
        """client.py 를 한 번 띄워 신호 전송.

        trigger=None 이면 단순 fire-and-forget (stdout 은 끝까지 수집).
        trigger 가 주어지면 byte 매칭 즉시 on_trigger(monotonic_ts) 호출.
        """
        cmd = self._build_cmd(topic_name, json_path)
        spawn_ts = time.monotonic()
        p = proc.spawn(cmd, cwd=self.client_dir)

        if trigger:
            def _cb(tok: bytes, ts: float) -> None:
                if on_trigger is not None:
                    on_trigger(ts)

            sr = proc.scan_until(
                p,
                tokens=[trigger],
                on_match=_cb,
                timeout=timeout,
                spawn_ts=spawn_ts,
            )
        else:
            sr = proc.scan_until(
                p,
                tokens=[],
                on_match=None,
                timeout=timeout,
                spawn_ts=spawn_ts,
            )

        return SignalResult(
            rc=sr.rc,
            stdout=sr.stdout,
            trigger_hit=sr.trigger_hit,
            trigger_ts=sr.trigger_ts,
            e2e_ms=sr.e2e_ms,
            timed_out=sr.timed_out,
        )
