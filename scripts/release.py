#!/usr/bin/env python3
"""Deprecated wrapper — build_dist.py 로 통합됨.

이 스크립트는 호환성 유지를 위해 build_dist.py 를 호출하는 thin wrapper.
실제 기능 (OS 선택 + 버전 + PySide6 GUI + 빌드 + 배포 push) 은 모두 build_dist.py 가 처리.

기존 사용자 호출 호환:
  ./scripts/release.py                       → python build_dist.py        (GUI)
  ./scripts/release.py --os linux --version 1.2.0 -y
                                             → python build_dist.py --os linux --version 1.2.0
  ./scripts/release.py --skip-build/--skip-push 도 그대로 전달

권장: 향후 build_dist.py 직접 사용.
"""

from __future__ import annotations

import sys
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    # --yes / -y 는 GUI 통합 시 의미 없음 (CLI 자동 모드는 인자 그 자체로 결정). 제거 후 그대로 전달.
    fwd = [a for a in sys.argv[1:] if a not in ("-y", "--yes")]
    cmd = [sys.executable, str(ROOT / "build_dist.py")] + fwd
    print(f"[release.py] delegating → {' '.join(cmd)}")
    sys.exit(subprocess.call(cmd, cwd=ROOT))


if __name__ == "__main__":
    main()
