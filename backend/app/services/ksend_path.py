"""VW 계열(MIB/ICAS) 에이전트의 ksend 바이너리 경로 분기.

시료 빌드에 따라 ksend 가 놓이는 위치가 다르다.
  - debug     : /lge/app_ro/bin/ksend  — 디버그 빌드에 기본 포함된 정식 경로
  - 0-version : /tmp/ksend             — 0-version(양산) 빌드는 app_ro 가 read-only/미포함이라
                                         ksend 를 /tmp 에 올려두고 쓴다

터치뿐 아니라 하드키·POWER 추가 커맨드·주소 진단(-v 더미 송신)까지 ksend 를 쓰는 모든
경로가 같은 값을 참조해야 하므로, 서비스는 하드코딩 대신 self.ksend_bin 을 쓴다.
"""

from typing import Optional

KSEND_VARIANT_DEBUG = "debug"
KSEND_VARIANT_ZERO = "0-version"

KSEND_PATHS: dict[str, str] = {
    KSEND_VARIANT_DEBUG: "/lge/app_ro/bin/ksend",
    KSEND_VARIANT_ZERO: "/tmp/ksend",
}

DEFAULT_KSEND_VARIANT = KSEND_VARIANT_DEBUG


def normalize_variant(variant: Optional[str]) -> str:
    """프론트/저장값을 정규 variant 키로. 알 수 없는 값은 debug."""
    v = (variant or "").strip().lower()
    if v in ("0-version", "0version", "zero", "0", "tmp"):
        return KSEND_VARIANT_ZERO
    if v in ("debug", "dbg", "app_ro", ""):
        return KSEND_VARIANT_DEBUG
    return DEFAULT_KSEND_VARIANT


def resolve_ksend_path(variant: Optional[str] = None, path_override: str = "") -> str:
    """variant → ksend 절대경로. path_override 가 있으면 그 값을 그대로 쓴다."""
    if path_override and str(path_override).strip():
        return str(path_override).strip()
    return KSEND_PATHS[normalize_variant(variant)]


def other_ksend_path(path: str) -> str:
    """현재 경로의 반대편 후보 경로 (미존재 시 폴백 진단용). 커스텀 경로면 debug 경로."""
    for v, p in KSEND_PATHS.items():
        if p == path:
            return (KSEND_PATHS[KSEND_VARIANT_ZERO] if v == KSEND_VARIANT_DEBUG
                    else KSEND_PATHS[KSEND_VARIANT_DEBUG])
    return KSEND_PATHS[KSEND_VARIANT_DEBUG]


def variant_of_path(path: str) -> str:
    """경로 → variant 키 (커스텀 경로면 'custom')."""
    for v, p in KSEND_PATHS.items():
        if p == path:
            return v
    return "custom"
