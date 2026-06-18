"""동봉 adb 바이너리 경로 해석.

전 PC에서 **동일한 adb 바이너리(버전)** 를 쓰도록 보장한다. 시스템 PATH에 잡힌
제각각 버전의 adb 대신 번들된 ``tools/platform-tools/adb`` 를 우선 사용한다.

배경: 특정 PC에서만 `exec-out screencap` raw 바이너리가 깨져(구버전/충돌 adb)
"Cannot decode screenshot" 가 났다. 미러링(base64 스트리머)은 멀쩡한데 단발 캡처만
실패하는 비대칭의 근본 원인이 PC별 adb 편차였다. → 번들 adb로 버전 통일해서 해결.

※ adb **서버 포트는 기본 5037을 그대로 공유**한다. 전용 포트(예: 15037)로 격리하면
별도 adb 서버가 떠서 USB 디바이스를 시스템 5037 서버와 경합 → 앱이 디바이스를
하나도 못 보는 회귀가 발생했다(USB는 한 서버만 인터페이스를 claim). 번들 adb가
시스템 adb와 같은 버전이면 5037 공유가 안정적이라 격리가 불필요하다.

경로 탐색은 ``capture.scrcpy_server.detect_scrcpy_server`` 와 동일한 우선순위를 따른다.
adb_service / scrcpy_server / logcat_service 3개 모듈이 공통으로 import 한다.
(scrcpy_server ← adb_service 의존이 있어 순환을 피하려고 별도 모듈로 분리)
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path


def _project_root() -> Path:
    """이 파일은 <root>/backend/app/services/adb_path.py → parents[3] 이 <root>."""
    return Path(__file__).resolve().parents[3]


def _install_root_candidates() -> list[Path]:
    if sys.platform == "win32":
        return [Path(r"C:\ReplayKit")]
    return [Path("/opt/ReplayKit"), Path.home() / ".local" / "share" / "ReplayKit"]


@functools.lru_cache(maxsize=1)
def resolve_adb_path() -> str:
    """사용할 adb 바이너리 경로를 반환.

    우선순위:
      1. ADB_PATH 환경변수 (파일로 존재하거나, "adb" 가 아닌 명시 지정일 때)
      2. <repo>/tools/platform-tools/adb(.exe)
      3. ./tools/platform-tools/adb(.exe)  (CWD)
      4. 배포 설치 경로/tools/platform-tools/adb(.exe)
      5. 최후 폴백: "adb" (시스템 PATH) — 번들 미배치 시 graceful degradation
    """
    env_path = os.environ.get("ADB_PATH")
    if env_path:
        if os.path.isfile(env_path):
            return env_path
        # 디렉토리/명령 형태로 명시 지정한 경우도 존중 (기본 'adb' 폴백은 제외)
        if env_path != "adb":
            return env_path

    exe = "adb.exe" if sys.platform == "win32" else "adb"
    rel = Path("tools") / "platform-tools" / exe
    candidates: list[Path] = [
        _project_root() / rel,
        Path.cwd() / rel,
    ]
    for root in _install_root_candidates():
        candidates.append(root / rel)

    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return "adb"
