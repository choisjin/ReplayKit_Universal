"""SHELL 모듈 — Linux/macOS bash 명령어 실행 플러그인.

Windows 의 CMD 모듈에 대응되는 Linux 측 모듈. API 표면은 동일:
  - Run(command): 블로킹 실행, stdout+stderr 반환
  - Check(command, expected, match_mode): 블로킹 실행 + 기대값 비교. 실패 시 "FAIL: ..." 반환
  - Check_Logic(command, keywords, logic): 다중 키워드 and/or 합부 판정
  - RunCapture(command): 비블로킹 실행, [BG_TASK:bg_x] placeholder 반환 (폴링으로 결과 회수)
  - CheckCapture(command, expected, match_mode): 비블로킹 + 기대값 비교 (서버 폴링 시 최종 판정)
  - RunBackground(command): 서브프로세스 fire-and-forget (PID 반환)
  - Kill(pid), ListBackground(): 백그라운드 프로세스 관리

CMD 와 차이점:
  - 명령은 항상 bash -c 로 실행. 시스템 기본 sh (Ubuntu 에선 dash) 와 다른 bash 확장 문법
    (process substitution `<(...)`, `[[ ]]`, arrays 등) 을 사용할 수 있음.
  - Windows 전용 플래그(CREATE_NO_WINDOW) 미사용.
  - bash 미설치 환경(드물지만 alpine 등)에선 실행 실패 — 호스트가 일반 Ubuntu/Debian/RHEL
    계열이면 문제 없음.
"""

import os
import shutil
import subprocess
import sys


# bash 경로 — PATH 에서 동적으로 찾되, 못 찾으면 /bin/bash 폴백.
# Windows 에서 모듈을 import 해도 NameError 가 없도록 모듈 로드 시에 한 번만 평가.
_BASH = shutil.which("bash") or "/bin/bash"


class SHELL:
    """Linux/macOS bash 명령어 실행 모듈."""

    def __init__(self):
        self._bg_processes: dict[int, subprocess.Popen] = {}

    def _bash_argv(self, command: str) -> list[str]:
        """bash -c 형태 argv 생성 — login shell 옵션 없이 단순 실행."""
        return [_BASH, "-c", command]

    def Run(self, command: str, timeout: int = 30) -> str:
        """명령어를 bash 로 실행하고 완료될 때까지 대기.

        Args:
            command: 실행할 명령어. 예: "ls -la /tmp", "ps -ef | grep python"
            timeout: 최대 대기 시간 (초, 기본 30)

        Returns:
            stdout + stderr 출력 결과. 출력이 없으면 "(exit code: N)" 형식.
        """
        try:
            result = subprocess.run(
                self._bash_argv(command),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
            return output or f"(exit code: {result.returncode})"
        except subprocess.TimeoutExpired:
            return f"TIMEOUT ({timeout}s)"
        except FileNotFoundError:
            return f"ERROR: bash not found at '{_BASH}'"
        except Exception as e:
            return f"ERROR: {e}"

    def Check(self, command: str, expected: str = "", match_mode: str = "contains", timeout: int = 30) -> str:
        """명령어를 bash 로 실행하고 출력 결과를 기대값과 비교 (블로킹).

        Args:
            command: 실행할 명령어
            expected: 기대값 (출력에 포함되거나 완전히 일치해야 하는 문자열).
                      비어있으면 "리턴값이 없을 때만 pass"로 동작 (no-output 검증). 기본값: ""
            match_mode: "contains" (부분 일치) 또는 "exact" (완전 일치). 기본값: contains
            timeout: 최대 대기 시간 (초). 기본값: 30

        Returns:
            통과 시: stdout 원문 (출력이 없을 경우 "(no output)")
            실패 시: "FAIL: expected(<mode>): <expected>\\n---\\n<stdout>"
                    ("FAIL:" 접두사로 module_command 가 자동으로 fail 처리)
        """
        output = self.Run(command, timeout)
        actual = output.strip()
        exp = (expected or "").strip()
        actual_for_empty = actual
        if actual_for_empty.startswith("(exit code:") and actual_for_empty.endswith(")"):
            actual_for_empty = ""
        if not exp:
            passed = actual_for_empty == ""
            if passed:
                return "(no output)"
            return f"FAIL: expected({match_mode}): (no output)\n---\n{output}"
        if match_mode == "exact":
            passed = actual == exp
        else:
            passed = exp in actual
        if passed:
            return output
        return f"FAIL: expected({match_mode}): {expected}\n---\n{output}"

    def Check_Logic(self, command: str, keywords: str, logic: str = "and", timeout: int = 30) -> str:
        """명령어를 실행하고 두 개 이상의 키워드를 and/or 로직으로 합부 판정 (블로킹).

        Args:
            command: 실행할 명령어
            keywords: 키워드 목록. "," 로 구분 (예: "OK,ready,done")
            logic: "and" (모든 키워드 포함 시 pass) 또는 "or" (하나 이상 포함 시 pass).
                   기본값: and
            timeout: 최대 대기 시간 (초). 기본값: 30

        Returns:
            통과 시: stdout 원문
            실패 시: "FAIL: logic(<mode>): <keywords>\\n---\\n<stdout>"
        """
        output = self.Run(command, timeout)
        actual = output.strip()
        kw_list = [k.strip() for k in (keywords or "").split(",") if k.strip()]
        if not kw_list:
            return f"FAIL: logic({logic}): no keywords provided\n---\n{output}"
        mode = (logic or "and").strip().lower()
        if mode not in ("and", "or"):
            return f"FAIL: logic: unknown mode '{logic}' (use 'and' or 'or')\n---\n{output}"
        if mode == "and":
            passed = all(k in actual for k in kw_list)
        else:
            passed = any(k in actual for k in kw_list)
        if passed:
            return output
        return f"FAIL: logic({mode}): {keywords}\n---\n{output}"

    def RunCapture(self, command: str) -> str:
        """명령어를 백그라운드로 실행 (비블로킹). 결과 회수 가능.

        반환된 [BG_TASK:bg_x] placeholder 를 통해 /api/scenarios/cmd-result/{task_id} 로
        실제 결과를 폴링할 수 있다. bg_task_store 가 내부적으로 shell=True 로 실행하므로,
        CMD 모듈과 동일한 처리 경로를 공유 — 단, 호출 측에서 bash 전용 문법이 필요하면
        Run/Check 처럼 명시적으로 bash 가 보장되지는 않음 (시스템 기본 sh 사용 가능).
        bash 전용 문법이 필요한 케이스에선 command 를 'bash -c "..."' 로 직접 감싸서 호출.

        Args:
            command: 실행할 명령어

        Returns:
            "[BG_TASK:bg_x]" 형태의 placeholder
        """
        from backend.app.services import bg_task_store
        task_id = bg_task_store.start_task(command)
        return f"[BG_TASK:{task_id}]"

    def CheckCapture(self, command: str, expected: str, match_mode: str = "contains") -> str:
        """명령어를 백그라운드로 실행 + 기대값 비교 (비블로킹).

        Args:
            command: 실행할 명령어
            expected: 기대값
            match_mode: "contains" 또는 "exact"

        Returns:
            "[BG_TASK:bg_x]" 형태의 placeholder
        """
        from backend.app.services import bg_task_store
        task_id = bg_task_store.start_task(command, expected=expected, match_mode=match_mode)
        return f"[BG_TASK:{task_id}]"

    def RunBackground(self, command: str) -> str:
        """명령어를 서브프로세스로 실행 (백그라운드, non-blocking).

        Args:
            command: 실행할 명령어. 예: 'sleep 60', 'python3 script.py'

        Returns:
            실행된 프로세스의 PID. 예: "PID:1234"
        """
        try:
            proc = subprocess.Popen(
                self._bash_argv(command),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                # 부모 종료와 무관하게 살아남도록 새 세션 분리.
                # CMD 모듈은 Windows DETACHED_PROCESS 가 아닌 기본 동작이지만, Linux 에선
                # 호출자가 종료되면 자식도 SIGHUP 받을 수 있어 setsid 로 분리.
                start_new_session=True,
            )
            self._bg_processes[proc.pid] = proc
            return f"PID:{proc.pid}"
        except FileNotFoundError:
            return f"ERROR: bash not found at '{_BASH}'"
        except Exception as e:
            return f"ERROR: {e}"

    def Kill(self, pid: int) -> str:
        """백그라운드 프로세스를 종료.

        Args:
            pid: 종료할 프로세스 PID

        Returns:
            결과 메시지
        """
        proc = self._bg_processes.pop(pid, None)
        if proc:
            try:
                proc.kill()
                return f"Killed PID:{pid}"
            except Exception as e:
                return f"ERROR: {e}"
        # bg_processes 에 없으면 시스템에서 직접 종료 시도
        try:
            os.kill(pid, 9)
            return f"Killed PID:{pid}"
        except Exception as e:
            return f"ERROR: {e}"

    def ListBackground(self) -> str:
        """실행 중인 백그라운드 프로세스 목록.

        Returns:
            PID 목록 (alive 상태만)
        """
        alive = []
        dead = []
        for pid, proc in list(self._bg_processes.items()):
            if proc.poll() is None:
                alive.append(str(pid))
            else:
                dead.append(pid)
        for pid in dead:
            self._bg_processes.pop(pid, None)
        return ", ".join(alive) if alive else "(none)"
