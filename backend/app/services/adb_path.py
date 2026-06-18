"""동봉 adb 바이너리 경로 해석 + adb 서버 포트 격리.

전 PC에서 **동일한 adb 클라이언트/서버**를 쓰도록 보장한다. 시스템 PATH에 잡힌
제각각의 adb(버전 상이) 대신 번들된 ``tools/platform-tools/adb`` 를 우선 사용하고,
전용 ``ANDROID_ADB_SERVER_PORT`` 를 강제해 그 PC의 시스템 adb 서버(기본 5037)나
에뮬레이터(5554-5585)와 완전히 분리한다.

배경: 특정 PC에서만 `exec-out screencap` raw 바이너리가 깨져(구버전/충돌 adb)
"Cannot decode screenshot" 가 났다. 미러링(base64 스트리머)은 멀쩡한데 단발 캡처만
실패하는 비대칭의 근본 원인이 PC별 adb 편차였다.

경로 탐색은 ``capture.scrcpy_server.detect_scrcpy_server`` 와 동일한 우선순위를 따른다.
adb_service / scrcpy_server / logcat_service 3개 모듈이 공통으로 import 한다.
(scrcpy_server ← adb_service 의존이 있어 순환을 피하려고 별도 모듈로 분리)
"""

from __future__ import annotations

import functools
import os
import sys
from pathlib import Path

# 전용 adb 서버 포트 — adb 기본(5037)·에뮬레이터 예약대역(5554-5585)을 모두 피한다.
# 이미 외부에서 ANDROID_ADB_SERVER_PORT 를 지정했다면 그 값을 존중(setdefault).
# 이 모듈은 adb를 쓰는 backend 서비스들이 import 하므로, 실제 adb 명령이 실행되기
# 전에 환경변수가 세팅되어 모든 adb 자식 프로세스가 이 포트의 서버를 사용하게 된다.
ADB_SERVER_PORT = "15037"
os.environ.setdefault("ANDROID_ADB_SERVER_PORT", ADB_SERVER_PORT)


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
