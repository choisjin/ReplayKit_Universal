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

# scar.sh 백그라운드 기동 출력을 남길 로그 (왜 컨테이너가 안 뜨는지 진단용).
# 기존엔 /dev/null 로 버려서 실패 원인이 안 보였다.
SCAR_LAUNCH_LOG = "/tmp/replaykit-scar-launch.log"


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
        # 잘못된 경로(폴더/없는 파일/실행권한 없음)면 setsid 가 'Permission denied' 로만 찍혀
        # 원인 파악이 어렵다. 미리 검사해 분명한 사유를 남긴다.
        if not os.path.isfile(script):
            reason = ("디렉터리임 (scar.sh 파일 경로 필요)" if os.path.isdir(script)
                      else "파일 없음")
            logger.warning("SCAR launch aborted: %r — %s", script, reason)
            try:
                with open(SCAR_LAUNCH_LOG, "w") as f:
                    f.write(f"launch aborted: {script!r} — {reason}\n"
                            f"scar.sh 의 전체 파일 경로를 지정하세요 "
                            f"(예: <scar-master>/scripts/scar.sh)\n")
            except OSError:
                pass
            return False
        if not os.access(script, os.X_OK):
            logger.warning("SCAR launch aborted: %r — 실행권한 없음 (chmod +x 필요)", script)
            try:
                with open(SCAR_LAUNCH_LOG, "w") as f:
                    f.write(f"launch aborted: {script!r} — 실행권한 없음\nchmod +x {script}\n")
            except OSError:
                pass
            return False

        # scar.sh 출력을 /dev/null 대신 로그 파일로 남긴다 — 컨테이너 미기동 원인 진단용.
        cmd_str = (
            " ".join(["setsid", script, *(args or [])])
            + f" </dev/null >{SCAR_LAUNCH_LOG} 2>&1 &"
        )
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
            logger.info("SCAR launch: %s (cwd=%r) → output at %s", script, run_cwd, SCAR_LAUNCH_LOG)
            return True
        except (OSError, FileNotFoundError) as e:
            logger.warning("Failed to start SCAR reconnect script (cwd=%r): %s", run_cwd, e)
            return False
