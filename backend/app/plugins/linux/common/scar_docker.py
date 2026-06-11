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

        # 이미 떠 있는 컨테이너 안의 UI(start_ui.sh) 직접 재기동 — start_via_script 와 별개 경로.
        # (해당 메서드는 클래스 본문 아래쪽 restart_ui_in_container 로 정의)

        # 이전 기동 로그를 '스폰 전에' 동기적으로 비운다 — 아래 bash 의 > truncate 는 백그라운드
        # 프로세스가 뜬 뒤에야 일어나므로, 폴링(_launch_hit_tty_trap)이 그보다 먼저 읽으면 직전
        # 실행의 TTY 에러를 현재 실행 것으로 오인해 폴링을 0초 만에 포기한다(2026-06-11 실측).
        try:
            open(SCAR_LAUNCH_LOG, "w").close()
        except OSError:
            pass

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

    # ─── stop (정리) ────────────────────────────────────
    def stop_via_script(
        self,
        script: str,
        tag: str = "2.2.0",
        cwd: Optional[str] = None,
        clear_logs: bool = True,
        timeout: float = 120.0,
    ) -> tuple[bool, str]:
        """`./scar.sh -t <tag> -c` 로 scar 도커 컨테이너 정지 (설치 가이드 'To stop Scar').

        -c 는 'Do you want to clear logs? [y/n] (default=y)' 프롬프트를 띄우므로 stdin 으로
        답을 먹인다. start_via_script(기동, 백그라운드)와 달리 동기 실행 — 컨테이너가 실제로
        내려갔는지 is_running() 으로 확인 후 반환.
        """
        if not script or not os.path.isfile(script):
            return False, f"scar.sh 파일 없음: {script!r}"
        run_cwd = cwd or (os.path.dirname(script) or None)
        answer = ("y" if clear_logs else "n") + "\n"
        try:
            res = subprocess.run(
                ["bash", script, "-t", tag, "-c"],
                cwd=run_cwd,
                input=answer,
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"scar.sh -c 타임아웃 ({timeout}s)"
        except (OSError, FileNotFoundError) as e:
            return False, f"scar.sh -c 실행 실패: {e}"
        tail = ((res.stdout or "") + (res.stderr or "")).strip()[-512:]
        if not self.is_running():
            return True, f"scar docker stopped\n{tail}"
        return False, f"scar.sh -c rc={res.returncode}, 컨테이너 여전히 running\n{tail}"

    def restart_ui_in_container(
        self,
        ui_dir: str = "/home/scar/ui",
        home_scar: str = "/home/scar",
        timeout: float = 60.0,
    ) -> tuple[bool, str]:
        """이미 떠 있는 컨테이너 안에서 UI(start_ui.sh) 직접 재기동.

        ★ 핵심: host `scar.sh --ui` 는 컨테이너가 이미 running 이면 `docker exec -it`(TTY) 로
        들어가 UI 를 띄운다. 우리는 setsid </dev/null (무TTY) 로 실행하므로
        'cannot attach stdin to a TTY-enabled container because stdin is not a terminal' 로
        실패한다 → UI 가 안 뜬다. 그래서 여기서 TTY 없이 docker exec 로 start_ui.sh 를 직접 돌린다.

        start_ui.sh 는 backend_ui(node scar-server.js:3000) + frontend_ui(http-server:8081) 를
        `screen -dm`(detached)로 기동하고, 죽은 backend_ui/frontend_ui screen 은 스스로 quit+wipe.
        screen -dm 는 TTY 불필요라 무TTY exec 로도 정상 동작한다.
        """
        # 죽은 screen 소켓 정리(서비스 포함) 후 start_ui.sh. -lc 로 로그인 env(HOME_SCAR 등) 로드,
        # 안전하게 HOME_SCAR 도 명시적으로 덮어쓴다 (root 로 exec 시 미설정일 수 있음).
        inner = (
            f"screen -wipe || true; "
            f"cd {ui_dir} && HOME_SCAR={home_scar} ./start_ui.sh"
        )
        argv = ["docker", "exec", self.container, "bash", "-lc", inner]
        try:
            res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # start_ui.sh 가 screen -dm 로 백그라운드 기동하므로 보통 빨리 끝난다. 타임아웃이면
            # 비정상이지만 screen 은 이미 떴을 수 있으니 호출자가 8081 폴링으로 확정한다.
            return False, f"docker exec timeout ({timeout}s) — 8081 폴링으로 확인"
        except (OSError, FileNotFoundError) as e:
            return False, f"docker exec spawn failed: {e}"
        tail = ((res.stdout or "") + (res.stderr or "")).strip()[-512:]
        if res.returncode != 0:
            return False, f"start_ui.sh rc={res.returncode}\n{tail}"
        return True, f"start_ui.sh launched in {self.container}:{ui_dir}\n{tail}"
