"""Linux 전용 플러그인 패키지.

이 패키지 아래의 플러그인(TH, SCAR 등)은 Linux 환경에서만 동작한다.
Windows 빌드에서 import 시 명확한 ImportError 를 던져 module_service 의
플러그인 발견 로직이 깔끔히 스킵하도록 한다 — lincontrol_service.py 와 동일 패턴.
"""

import sys

if not sys.platform.startswith("linux"):
    raise ImportError(
        "backend.app.plugins.linux is Linux-only "
        f"(current platform: {sys.platform})"
    )
