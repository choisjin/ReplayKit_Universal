"""모듈 연결 진행 단계 레지스트리.

SCAR/TH 처럼 Connect/Setup 이 수십 초~분 단위로 걸리는 모듈이 현재 단계를 보고하면,
/device/list 가 읽어 디바이스 카드에 표시한다 ("아무것도 안 하는 것처럼 보이다가
혼자 연결됨" 체감 해소). 키는 모듈 이름("SCAR"/"TH") — 디바이스 info["module"] 과 동일.

플러그인 쪽에서는 실패해도 연결 자체에 영향이 없도록 try/except 로 감싸 호출할 것.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_progress: dict[str, str] = {}


def set_progress(module_name: str, text: str) -> None:
    if not module_name:
        return
    with _lock:
        _progress[module_name] = text


def get_progress(module_name: str) -> str:
    with _lock:
        return _progress.get(module_name, "")


def clear_progress(module_name: str) -> None:
    with _lock:
        _progress.pop(module_name, None)
