"""SCAR Docker 컨테이너 래퍼.

원본 키워드:
  - Check SCAR Container By Inspect — docker inspect -f {{.State.Running}} scar
  - Exec In SCAR Container — docker exec scar bash -c <cmd>
  - Reconnect SCAR — setsid ./scar.sh -t 2.2.0 --ui --arti tls </dev/null >/dev/null 2>&1

proc.spawn 을 재사용해 process group 단위 종료 가능.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from . import proc


logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    rc: Optional[int]
    stdout: bytes
    timed_out: bool


class SCARDocker:
    def __init__(self, container: str = "scar"):
        self.container = container

    # ─── inspect ────────────────────────────────────────
    def is_running(self, timeout: float = 5.0) -> bool:
        try:
            res = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", self.container],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.debug("docker inspect timed out (%s)", self.container)
            return False
        except FileNotFoundError:
            logger.warning("docker binary not found")
            return False
        return res.returncode == 0 and res.stdout.strip().lower() == "true"

    # ─── exec ───────────────────────────────────────────
    def exec(self, cmd: str, timeout: float = 300.0) -> ExecResult:
        """docker exec <container> bash -c <cmd>.

        원본 'Exec In SCAR Container' 와 동일 시맨틱. stdout+stderr 합쳐서 반환.
        """
        argv = ["docker", "exec", self.container, "bash", "-c", cmd]
        p = proc.spawn(argv)
        sr = proc.scan_until(p, tokens=[], on_match=None, timeout=timeout)
        return ExecResult(rc=sr.rc, stdout=sr.stdout, timed_out=sr.timed_out)

    # ─── reconnect ──────────────────────────────────────
    def start_via_script(
        self,
        script: str,
        args: Optional[list[str]] = None,
        cwd: Optional[str] = None,
    ) -> bool:
        """원본 'Reconnect SCAR' 의 setsid + nohup 패턴을 단순화한 fire-and-forget.

        호스트 셸이 죽어도 스크립트가 계속 떠있도록 start_new_session=True 로 분리.
        scar.sh 가 백그라운드에서 컨테이너를 띄우는데 보통 ~20초 걸린다 (원본 Wait 20).
        """
        cmd_str = " ".join(["setsid", script, *(args or [])]) + " </dev/null >/dev/null 2>&1 &"
        # cwd 가 빈 문자열('')이면 Popen 이 FileNotFoundError 를 던지므로 None 으로 정규화.
        # 미지정 시 scar.sh 가 있는 디렉터리에서 실행 (가이드: ~/scar/scar-master/scripts$ ./scar.sh).
        run_cwd = cwd or (os.path.dirname(script) or None)
        try:
            subprocess.Popen(
                ["bash", "-c", cmd_str],
                cwd=run_cwd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except (OSError, FileNotFoundError) as e:
            logger.warning("Failed to start SCAR reconnect script (cwd=%r): %s", run_cwd, e)
            return False
