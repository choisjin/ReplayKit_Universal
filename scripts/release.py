#!/usr/bin/env python3
"""ReplayKit 통합 릴리스 TUI — OS + 버전 선택 → 빌드 + 배포 git push.

흐름:
  1. OS 선택 (Windows / Linux)
  2. 버전 입력 (기본값 = version.txt 의 현재 값)
  3. 선택한 OS 의 빌드 스크립트 호출
       - Windows: build_dist.py  (Cython + Inno Setup → .exe installer)
       - Linux:   scripts/build_deb.sh  (embedded Python + dpkg-deb → .deb)
  4. OS 별 배포 git 으로 force push (DEPLOY_REMOTES 의 URL)

OS 별 배포 git URL 은 한 곳 (DEPLOY_REMOTES 상수) 에서 관리.
빌드는 호스트 OS 에 맞는 toolchain 이 필요 — Windows 빌드는 Windows 머신/MSVC,
Linux 빌드는 Linux 머신/dpkg-dev. 잘못된 OS 조합은 즉시 거부.

사용:
  ./scripts/release.py                 # 인터랙티브 (OS + 버전 prompt)
  ./scripts/release.py --os win        # OS 선택 자동 (prompt 생략)
  ./scripts/release.py --os linux --version 1.2.0  # 모두 자동 (CI)
  ./scripts/release.py --skip-push     # 빌드만, 배포 git push 안 함
  ./scripts/release.py --skip-build    # push 만 (빌드 산출물 이미 있다고 가정)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 프로젝트 루트 (scripts/ 의 부모)
ROOT = Path(__file__).resolve().parent.parent

# ── OS 별 배포 git URL — single source of truth ──
DEPLOY_REMOTES = {
    "win":   "http://mod.lge.com/hub/dqa_replay_kit/replay_kit.git",
    "linux": "http://mod.lge.com/hub/dqa_dcv_auto/rnavn_project.git",
}

# OS 별 사람이 읽는 이름
OS_LABELS = {"win": "Windows", "linux": "Linux"}


def _color(s: str, code: str) -> str:
    """ANSI color — TTY 에서만 활성화."""
    if not sys.stdout.isatty():
        return s
    return f"\033[{code}m{s}\033[0m"


def bold(s: str) -> str:
    return _color(s, "1")


def green(s: str) -> str:
    return _color(s, "32")


def red(s: str) -> str:
    return _color(s, "31")


def yellow(s: str) -> str:
    return _color(s, "33")


def cyan(s: str) -> str:
    return _color(s, "36")


def read_current_version() -> str:
    vf = ROOT / "version.txt"
    if not vf.is_file():
        return ""
    return vf.read_text(encoding="utf-8").strip()


def write_version(ver: str) -> None:
    (ROOT / "version.txt").write_text(ver + "\n", encoding="utf-8")


def prompt_os() -> str:
    print()
    print(bold("=== ReplayKit Release ==="))
    print()
    print("빌드할 대상 OS 를 선택하세요:")
    print(f"  {cyan('1')}) Windows  (.exe installer, build_dist.py + Inno Setup)")
    print(f"  {cyan('2')}) Linux    (.deb package, scripts/build_deb.sh)")
    while True:
        choice = input(bold("선택 [1/2]: ")).strip().lower()
        if choice in ("1", "w", "win", "windows"):
            return "win"
        if choice in ("2", "l", "lin", "linux"):
            return "linux"
        print(red("  잘못된 선택. 1 또는 2 입력."))


def prompt_version(current: str) -> str:
    print()
    if current:
        print(f"현재 버전: {bold(current)}")
        prompt = bold(f"새 버전 [엔터=유지 / 1.2.0 / patch / minor / major]: ")
    else:
        prompt = bold("버전 (예: 1.1.0): ")
    while True:
        ans = input(prompt).strip()
        if not ans and current:
            return current
        if ans in ("patch", "minor", "major"):
            return bump_semver(current, ans)
        # 입력 정규화 — 'v' prefix 허용
        if ans.startswith("v") or ans.startswith("V"):
            ans = ans[1:]
        # 단순 형식 검증 — N.N.N
        parts = ans.split(".")
        if len(parts) == 3 and all(p.isdigit() for p in parts):
            return f"v{ans}"
        print(red(f"  잘못된 형식 ({ans!r}). N.N.N 또는 patch/minor/major."))


def bump_semver(current: str, kind: str) -> str:
    """current=v1.1.0, kind=patch → v1.1.1.  major/minor 도 동일."""
    base = current.lstrip("vV") if current else "0.0.0"
    parts = base.split(".")
    while len(parts) < 3:
        parts.append("0")
    try:
        ma, mi, pa = (int(parts[0]), int(parts[1]), int(parts[2]))
    except ValueError:
        ma, mi, pa = (0, 0, 0)
    if kind == "major":
        ma += 1; mi = 0; pa = 0
    elif kind == "minor":
        mi += 1; pa = 0
    else:
        pa += 1
    return f"v{ma}.{mi}.{pa}"


def confirm(msg: str) -> bool:
    while True:
        ans = input(bold(f"{msg} [y/N]: ")).strip().lower()
        if ans in ("y", "yes"):
            return True
        if ans in ("", "n", "no"):
            return False


def host_os() -> str:
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def check_host_compat(target: str) -> None:
    """대상 OS 빌드를 현재 호스트에서 실행 가능한지 검증."""
    host = host_os()
    if target == "win" and host != "win":
        print(red(f"[ERROR] Windows 빌드는 Windows 호스트에서만 가능 (current: {host})"))
        print("        WSL/Wine 미지원 — Cython MSVC + Inno Setup 필요.")
        sys.exit(2)
    if target == "linux" and host != "linux":
        print(red(f"[ERROR] Linux 빌드는 Linux 호스트에서만 가능 (current: {host})"))
        print("        dpkg-dev + embedded Python + apt 의존성 필요.")
        sys.exit(2)


def run_build(target: str) -> int:
    """대상 OS 의 빌드 스크립트 호출. exit code 반환."""
    if target == "win":
        # build_dist.py 는 자체 버전 prompt 가 있는데 우리가 이미 version.txt 를
        # 갱신했으니 그대로 사용 — 단 인터랙티브 prompt 가 다시 뜨므로 사용자가
        # 엔터로 '유지' 선택. 향후 build_dist.py 에 --version 인자 추가 필요.
        cmd = [sys.executable, str(ROOT / "build_dist.py")]
        print(cyan(f"\n[BUILD] $ {' '.join(cmd)}\n"))
        return subprocess.call(cmd, cwd=ROOT)
    if target == "linux":
        sh = ROOT / "scripts" / "build_deb.sh"
        if not sh.is_file():
            print(red(f"[ERROR] {sh} not found"))
            return 1
        # build_deb.sh 의 자체 LGE PUSH 는 release.py 와 충돌 — SKIP_LGE_PUSH=1 로 끄고
        # release.py 가 push 까지 통합 관리.
        env = os.environ.copy()
        env["SKIP_LGE_PUSH"] = "1"
        cmd = ["bash", str(sh)]
        print(cyan(f"\n[BUILD] $ SKIP_LGE_PUSH=1 {' '.join(cmd)}\n"))
        return subprocess.call(cmd, cwd=ROOT, env=env)
    print(red(f"[ERROR] unknown OS: {target}"))
    return 1


def deploy_push(target: str) -> int:
    """OS 별 배포 git 으로 force push.

    Windows: build_dist.py 가 dist/.git 별도 repo 를 사용 (Cython 결과만 push).
    Linux:   source main 을 force push (build_deb.sh 가 .deb 생성 후 source 동기화).

    구현 단순화 — 두 케이스 모두 ROOT 의 main 을 OS 별 remote 로 force push.
    Windows 의 dist/.git 별도 운영은 향후 release.py 확장으로 분리 가능.
    """
    url = DEPLOY_REMOTES.get(target)
    if not url:
        print(red(f"[ERROR] no deploy URL for OS={target}"))
        return 1
    remote_name = f"lge-{target}"
    print(cyan(f"\n[PUSH] {OS_LABELS[target]} → {url}"))

    # remote 등록/갱신
    cur = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=ROOT, capture_output=True, text=True,
    )
    if cur.returncode != 0:
        subprocess.run(["git", "remote", "add", remote_name, url], cwd=ROOT, check=True)
        print(f"       remote '{remote_name}' 추가됨")
    elif cur.stdout.strip() != url:
        subprocess.run(["git", "remote", "set-url", remote_name, url], cwd=ROOT, check=True)
        print(f"       remote '{remote_name}' URL 갱신")

    # force push main
    r = subprocess.run(
        ["git", "push", "--force", remote_name, "main"],
        cwd=ROOT,
    )
    if r.returncode == 0:
        print(green(f"[PUSH] OK — {OS_LABELS[target]} 배포 git 갱신 완료."))
        return 0
    print(red(f"[PUSH] FAILED — 네트워크/인증 확인 후 수동:"))
    print(f"          git push --force {remote_name} main")
    return r.returncode


def main():
    parser = argparse.ArgumentParser(description="ReplayKit unified release TUI")
    parser.add_argument("--os", choices=["win", "linux"], help="대상 OS (생략 시 prompt)")
    parser.add_argument("--version", help="릴리스 버전 (예: 1.2.0; 생략 시 prompt)")
    parser.add_argument("--skip-build", action="store_true", help="빌드 단계 스킵 (push 만)")
    parser.add_argument("--skip-push", action="store_true", help="배포 push 스킵 (빌드만)")
    parser.add_argument("--yes", "-y", action="store_true", help="확인 prompt 자동 yes")
    args = parser.parse_args()

    # 1) OS 결정
    target = args.os or prompt_os()
    print(green(f"\n→ 대상 OS: {OS_LABELS[target]}"))

    # 2) 호스트 호환성 검증 (빌드 안 할 거면 skip)
    if not args.skip_build:
        check_host_compat(target)

    # 3) 버전 결정 + version.txt 갱신
    current = read_current_version()
    new_ver = args.version
    if new_ver is None:
        new_ver = prompt_version(current)
    elif not new_ver.startswith("v"):
        new_ver = f"v{new_ver}"
    if new_ver != current:
        print(green(f"→ 버전: {current} → {new_ver}"))
        if args.yes or confirm(f"version.txt 를 {new_ver} 로 갱신?"):
            write_version(new_ver)
        else:
            print(yellow("→ 버전 갱신 취소. 현재 버전으로 진행."))
            new_ver = current
    else:
        print(green(f"→ 버전: {new_ver} (변경 없음)"))

    # 4) 빌드
    if not args.skip_build:
        rc = run_build(target)
        if rc != 0:
            print(red(f"\n[ABORT] 빌드 실패 (exit {rc}) — push 건너뜀."))
            sys.exit(rc)
    else:
        print(yellow("[BUILD] skipped (--skip-build)"))

    # 5) 배포 push
    if not args.skip_push:
        rc = deploy_push(target)
        if rc != 0:
            sys.exit(rc)
    else:
        print(yellow("[PUSH] skipped (--skip-push)"))

    print(green(f"\n=== Release 완료: {OS_LABELS[target]} {new_ver} ==="))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(red("\n[ABORT] 사용자 취소 (Ctrl+C)"))
        sys.exit(130)
