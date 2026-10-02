"""CMD 모듈 — 시스템 명령어 실행 플러그인.

실행 방식:
  - Run(command): 블로킹 실행, stdout+stderr 반환
  - Check(command, expected, match_mode): 블로킹 실행 + 기대값 비교. 실패 시 "FAIL: ..." 반환
  - RunCapture(command): 비블로킹 실행, [BG_TASK:bg_x] placeholder 반환 (폴링으로 결과 회수)
  - CheckCapture(command, expected, match_mode): 비블로킹 + 기대값 비교 (서버 폴링 시 최종 판정)
  - RunBackground(command): 서브프로세스 fire-and-forget (PID 반환, 결과 회수 불가)
  - Kill(pid), ListBackground(): 백그라운드 프로세스 관리
"""

import subprocess
import sys


class CMD:
    """시스템 명령어 실행 모듈."""

    def __init__(self):
        self._bg_processes: dict[int, subprocess.Popen] = {}

    def Run(self, command: str, timeout: int = 30) -> str:
        """명령어를 실행하고 완료될 때까지 대기.

        Args:
            command: 실행할 명령어 (예: "ping 127.0.0.1 -n 3")
            timeout: 최대 대기 시간 (초, 기본 30)

        Returns:
            stdout + stderr 출력 결과
        """
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            output = (result.stdout.strip() + "\n" + result.stderr.strip()).strip()
            return output or f"(exit code: {result.returncode})"
        except subprocess.TimeoutExpired:
            return f"TIMEOUT ({timeout}s)"
        except Exception as e:
            return f"ERROR: {e}"

    def Check(self, command: str, expected: str = "", match_mode: str = "contains", timeout: int = 30) -> str:
        """명령어를 실행하고 출력 결과를 기대값과 비교 (블로킹).

        Args:
            command: 실행할 명령어
            expected: 기대값 (출력에 포함되거나 완전히 일치해야 하는 문자열).
                      비어있으면 "리턴값이 없을 때만 pass"로 동작 (no-output 검증). 기본값: ""
            match_mode: "contains" (부분 일치) 또는 "exact" (완전 일치). 기본값: contains
            timeout: 최대 대기 시간 (초). 기본값: 30

        Returns:
            통과 시: stdout 원문 (출력이 없을 경우 "(no output)")
            실패 시: "FAIL: expected(<mode>): <expected>\\n---\\n<stdout>"
                    ("FAIL:" 접두사로 module_command가 자동으로 fail 처리)
        """
        output = self.Run(command, timeout)
        actual = output.strip()
        exp = (expected or "").strip()
        # exit code 라인만 있는 경우는 실질적으로 출력 없음으로 간주
        actual_for_empty = actual
        if actual_for_empty.startswith("(exit code:") and actual_for_empty.endswith(")"):
            actual_for_empty = ""
        if not exp:
            # expected가 비어있으면 "출력 없음"일 때만 pass
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
                    ("FAIL:" 접두사로 module_command가 자동으로 fail 처리)
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

        반환된 [BG_TASK:bg_x] placeholder를 통해 /api/scenarios/cmd-result/{task_id}로
        실제 결과를 폴링할 수 있다.

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

        폴링 엔드포인트가 완료 시 기대값 비교를 수행하여 최종 pass/fail을 판정한다.

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
            command: 실행할 명령어

        Returns:
            실행된 프로세스의 PID
        """
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            self._bg_processes[proc.pid] = proc
            return f"PID:{proc.pid}"
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
        # bg_processes에 없으면 시스템에서 직접 종료 시도
        try:
            import os
            os.kill(pid, 9)
            return f"Killed PID:{pid}"
        except Exception as e:
            return f"ERROR: {e}"

    def Disconnect(self) -> str:
        """RunBackground 로 띄운 프로세스(트리)를 모두 종료 — 재생 중단/오류 시 정리용.

        shell=True 라 PID 는 cmd.exe 이므로 자식까지 /T 로 함께 내린다. 먼저 정상 종료를
        요청하고(3초), 남아 있으면 강제 종료한다.
        """
        killed = []
        for pid, proc in list(self._bg_processes.items()):
            if proc.poll() is None:
                try:
                    subprocess.run(["taskkill", "/T", "/PID", str(pid)],
                                   capture_output=True, timeout=5,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                                       capture_output=True, timeout=5,
                                       creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                    killed.append(str(pid))
                except Exception:
                    try:
                        proc.kill()
                        killed.append(str(pid))
                    except Exception:
                        pass
            self._bg_processes.pop(pid, None)
        return f"ok: stopped background {', '.join(killed)}" if killed else "ok"

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
