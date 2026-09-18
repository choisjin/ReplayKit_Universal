#!/usr/bin/env python3
"""
RSD tunnel / HID 세션 진단용 스크립트.

다른 PC에서 RSD 터널이 열리지 않을 때, pymobiledevice3 CLI 명령어의
실제 출력/에러/종료 코드를 파일로 남깁니다.

사용법 (Python 3.10 권장):
    py -3.10 diagnose_tunnel.py

    # 고급: 다른 Python 인터프리터를 강제하려면 PYMD_PYTHON 환경변수 사용
    $env:PYMD_PYTHON = "C:\Python310\python.exe"
    C:\Python310\python.exe diagnose_tunnel.py

출력:
    tunnel_diag.log   (사람이 읽기 쉬운 텍스트)
    tunnel_diag.json  (원시 결과 JSON)
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from typing import Any

PYTHON = os.environ.get("PYMD_PYTHON", sys.executable)
LOG_TXT = "tunnel_diag.log"
LOG_JSON = "tunnel_diag.json"
TIMEOUT_SEC = 20


def get_pymd_version() -> str:
    """pymobiledevice3 패키지 버전을 가져옵니다."""
    try:
        import pymobiledevice3
        v = getattr(pymobiledevice3, "__version__", None)
        if v:
            return v
    except Exception:
        pass
    try:
        from importlib.metadata import version
        return version("pymobiledevice3")
    except Exception as e:
        return f"unknown ({type(e).__name__}: {e})"


def run_with_timeout(
    description: str,
    args: list[str],
    timeout: int = TIMEOUT_SEC,
) -> dict[str, Any]:
    """
    지정한 pymobiledevice3 하위 명령을 실행하고 timeout 초 후 강제 종료한 뒤
    stdout, stderr, exit_code, timed_out 플래그를 반환합니다.
    """
    cmd = [PYTHON, "-m", "pymobiledevice3"] + args
    print(f"\n[{description}]")
    print(f"  cmd: {' '.join(cmd)}")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        print(f"  -> {timeout}s 초과로 프로세스 종료")
        proc.kill()
        try:
            stdout, stderr = proc.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""

    result = {
        "description": description,
        "command": cmd,
        "python": PYTHON,
        "exit_code": proc.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
    }

    print(f"  exit_code: {result['exit_code']}, timed_out: {timed_out}")
    if stdout.strip():
        print("  stdout preview:", stdout[:500].replace("\n", " | "))
    if stderr.strip():
        print("  stderr preview:", stderr[:500].replace("\n", " | "))

    return result


def main() -> int:
    print("=== iPhone HID tunnel diagnostic ===")
    print(f"datetime: {datetime.now().isoformat()}")
    print(f"PYTHON: {PYTHON}")

    pymd_version = get_pymd_version()
    print(f"pymobiledevice3 version: {pymd_version}")

    results: list[dict[str, Any]] = [
        {
            "description": "pymobiledevice3 version",
            "python": PYTHON,
            "version": pymd_version,
        }
    ]

    # 1. USB 연결된 기기 목록
    results.append(run_with_timeout(
        "usbmux list",
        ["usbmux", "list"],
        timeout=15,
    ))

    # 2. userspace RSD 터널
    results.append(run_with_timeout(
        "start-tunnel --userspace",
        ["lockdown", "start-tunnel", "--script-mode", "--userspace"],
        timeout=TIMEOUT_SEC,
    ))

    # 3. kernel RSD 터널 (Windows에선 보통 관리자 권한 필요)
    results.append(run_with_timeout(
        "start-tunnel kernel",
        ["lockdown", "start-tunnel", "--script-mode"],
        timeout=TIMEOUT_SEC,
    ))

    # 4. remote tunneld 띄우기 (uvicorn 자식 프로세스가 있어 자동 종료가 복잡하므로 별도 안내)
    #    필요 시 수동으로 실행:
    #    C:\Python311\python.exe -m pymobiledevice3 remote tunneld --host 127.0.0.1 --port 5555

    # JSON 저장
    with open(LOG_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "datetime": datetime.now().isoformat(),
            "python": PYTHON,
            "results": results,
        }, f, ensure_ascii=False, indent=2)

    # 텍스트 저장
    lines: list[str] = []
    lines.append(f"Generated: {datetime.now().isoformat()}")
    lines.append(f"PYTHON: {PYTHON}")
    lines.append(f"pymobiledevice3 version: {pymd_version}")
    lines.append("")

    for r in results:
        lines.append(f"---- {r['description']} ----")
        if "command" in r:
            lines.append(f"Command: {' '.join(r['command'])}")
        if "exit_code" in r:
            lines.append(f"ExitCode: {r['exit_code']}")
        if "timed_out" in r:
            lines.append(f"TimedOut: {r['timed_out']}")
        if "stdout" in r:
            lines.append("STDOUT:")
            lines.append(r["stdout"] if r["stdout"].strip() else "(empty)")
        if "stderr" in r:
            lines.append("STDERR:")
            lines.append(r["stderr"] if r["stderr"].strip() else "(empty)")
        lines.append("")

    with open(LOG_TXT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"\nSaved: {LOG_TXT}, {LOG_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
