"""pytest 공용 설정 — 프로젝트 루트를 sys.path 에 추가해
`backend.app.plugins.linux.common.*` 같은 정식 패키지 경로 import 가 가능하게 한다.
"""

import sys
from pathlib import Path


_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
