"""CANAT RX 수신 메시지 확인 (CANAT.check_can_message / check_no_can_message 가상 함수).

레퍼런스 ATS IVICANatClient.FindCANMSG 의 개선 포팅:
- PRE/POST(수집 시작/중지)를 스텝 하나 안에서 자동 처리 — 사용자가 별도 준비 스텝 불필요
- 괄호 문자 옵션([]/{}/<>) 대신 명시적 match_mode 콤보 (startswith/exact/contains/bitmask)
- '*' 니블 와일드카드는 모든 위치 모드에서 사용 가능 ("1* ** 33")
- 폴링 1s → 0.2s (반응성), 대기시간 ms → 초 단위 (다른 모듈 timeout 규약과 통일)
- 옵션0 의 '짧은 쪽 길이로 자른 substring 매칭' 함정 제거 — 의미가 명확한 모드로 분리
- 실패 시 해당 ID 로 마지막에 수신된 데이터를 함께 반환해 디버깅 가능

CANAT .pyd 인스턴스가 들고 있는 CANat 전송 DLL 핸들(instance.hdll)의
ExtPreSaveCANDataAllList / ExtGetPointCANDataAllList / ExtStopSaveCANDataAllList 를 직접 호출한다.
DLL 축적 목록 형식: "MSGID:BYTE BYTE ...\r\n..." (레퍼런스와 동일, 채널 정보 없음).
"""

from __future__ import annotations

import ctypes
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.2


class CanRxError(RuntimeError):
    """CANAT RX 확인 자체가 불가능한 상황 (미연결/DLL 함수 부재 등)."""


def _get_hdll(instance):
    hdll = getattr(instance, "hdll", None)
    if hdll is None:
        raise CanRxError("CANAT 디바이스가 연결되어 있지 않습니다 (hdll 없음 — init 필요)")
    for fn in ("ExtPreSaveCANDataAllList", "ExtGetPointCANDataAllList", "ExtStopSaveCANDataAllList"):
        if not hasattr(hdll, fn):
            raise CanRxError(f"CANat DLL에 {fn} 함수가 없습니다 (DLL 버전 확인 필요)")
    # 64-bit 파이썬에서 포인터 반환값이 c_int(32-bit) 로 절단되지 않도록 restype 지정.
    # (레퍼런스 구현은 32-bit 전제로 c_char_p(int) 캐스팅을 썼음)
    hdll.ExtGetPointCANDataAllList.restype = ctypes.c_char_p
    return hdll


def _enable_can_monitor(hdll) -> None:
    """DLL 의 CAN 수신 모니터를 켠다 (축적 리스트 캡처의 필수 전제).

    CANat DLL 은 전역 싱글톤 하나로 동작하며, 수신 디스패처가 프레임을
    내부 축적 리스트(AllList)/콜백으로 넘기기 전에 '모니터 플래그'를 검사한다.
    이 플래그가 꺼져 있으면 들어온 CAN 프레임이 전부 버려진다.

    문제: lge.auto CANAT .pyd 의 init() 은 모니터를 끈 상태(ExtSetCANMonitor(False))로
    끝난다. 레퍼런스 canat.py 는 init 말미에 ExtSetCANMonitor(True) 로 다시 켜지만
    .pyd 는 그 단계를 생략했다. 그래서 우리 가상 함수가 ExtPreSaveCANDataAllList 로
    수집 플래그만 세워도 모니터가 꺼져 있으면 AllList 가 영원히 비어
    '5s 동안 수신 메시지 없음' 오탐이 난다. 캡처 직전에 반드시 켠다.

    모니터를 켠 채로 두는 것은 레퍼런스의 정상 수신 상태와 동일하므로 되돌리지 않는다
    (check_no_can_message 도 모니터가 켜져 있어야 '금지 메시지 수신'을 실제로 감지한다).
    """
    fn = getattr(hdll, "ExtSetCANMonitor", None)
    if fn is None:
        logger.warning("CANat DLL에 ExtSetCANMonitor 없음 — 모니터 강제 활성화 불가")
        return
    try:
        fn(True)
    except Exception as e:
        logger.warning("ExtSetCANMonitor(True) failed: %s", e)


def _parse_id(text: str) -> int:
    """'0x18DAF141' / '18DAF141' / '291' 등 hex 표기를 int 로 (0x 유무/대소문자 무관)."""
    s = str(text or "").strip()
    if not s:
        raise ValueError("message_id 가 비어 있습니다")
    return int(s, 16)


def _parse_expected(expected_data: str) -> list[tuple[int, int]]:
    """기대 데이터 문자열 → [(mask, value)] 바이트 목록.

    각 바이트는 2자리 hex, '*' 는 해당 니블 무시:
      "1A"  → (0xFF, 0x1A)   정확히 0x1A
      "1*"  → (0xF0, 0x10)   상위 니블만 1
      "**"  → (0x00, 0x00)   아무 값이나
    """
    out: list[tuple[int, int]] = []
    for tok in str(expected_data).split():
        t = tok.strip().upper()
        if len(t) == 1:
            t = "0" + t  # "5" → "05" 관용 허용
        if len(t) != 2:
            raise ValueError(f"기대 데이터 바이트 '{tok}' 형식 오류 — 2자리 hex 또는 '*' 니블 (예: 1A, 1*, **)")
        mask = 0
        value = 0
        for i, ch in enumerate(t):
            shift = 4 if i == 0 else 0
            if ch == "*":
                continue
            try:
                nib = int(ch, 16)
            except ValueError:
                raise ValueError(f"기대 데이터 바이트 '{tok}' 에 잘못된 문자 '{ch}'")
            mask |= 0xF << shift
            value |= nib << shift
        out.append((mask, value))
    return out


def _match_at(data: list[int], expected: list[tuple[int, int]], offset: int) -> bool:
    if offset + len(expected) > len(data):
        return False
    for i, (mask, value) in enumerate(expected):
        if data[offset + i] & mask != value:
            return False
    return True


def _matches(data: list[int], expected: list[tuple[int, int]], match_mode: str) -> bool:
    """수신 데이터 바이트가 기대 패턴과 매칭되는지 (match_mode 별 위치 규칙)."""
    if match_mode == "exact":
        return len(data) == len(expected) and _match_at(data, expected, 0)
    if match_mode == "contains":
        return any(_match_at(data, expected, off) for off in range(len(data) - len(expected) + 1)) \
            if len(data) >= len(expected) else False
    if match_mode == "bitmask":
        # 기대 바이트의 1비트가 수신 데이터에 모두 켜져 있는지 (앞에서부터 기대 길이만큼).
        # 레퍼런스 [..] 옵션과 동일 의미. '*' 는 사용 불가(비트 지정과 의미 충돌) — value 만 사용.
        if len(data) < len(expected):
            return False
        return all(data[i] & value == value for i, (_m, value) in enumerate(expected))
    # startswith (기본): 데이터 앞부분이 기대 패턴과 일치
    return _match_at(data, expected, 0)


def _snapshot(hdll) -> list[tuple[int, str]]:
    """DLL 축적 목록 스냅샷 → [(message_id_int, data_str)] (파싱 불가 라인은 skip)."""
    raw: Optional[bytes] = hdll.ExtGetPointCANDataAllList()
    if not raw:
        return []
    out: list[tuple[int, str]] = []
    for line in raw.decode("ascii", errors="replace").splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        id_part, data_part = line.split(":", 1)
        try:
            mid = int(id_part.strip(), 16)
        except ValueError:
            continue
        out.append((mid, data_part.strip()))
    return out


def find_can_message(
    instance,
    message_id: str,
    expected_data: str = "",
    match_mode: str = "startswith",
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """RX 축적 목록에서 message_id(+expected_data) 매칭 메시지를 timeout(초) 안에 찾는다.

    반환: (found, detail) — detail 은 매칭 메시지 또는 실패 사유(마지막 수신 데이터 포함).
    수집 시작/중지(PRE/POST)는 내부에서 자동 처리.
    """
    hdll = _get_hdll(instance)
    target_id = _parse_id(message_id)
    mode = str(match_mode or "startswith").strip().lower()
    if mode not in ("startswith", "exact", "contains", "bitmask"):
        raise ValueError(f"match_mode '{match_mode}' 는 지원하지 않습니다 (startswith/exact/contains/bitmask)")
    expected = _parse_expected(expected_data) if str(expected_data or "").strip() else []
    if mode == "bitmask" and any(m != 0xFF for m, _v in expected):
        raise ValueError("bitmask 모드에서는 '*' 와일드카드를 사용할 수 없습니다")

    timeout_s = max(0.0, float(timeout))
    deadline = time.monotonic() + timeout_s
    last_seen_data: Optional[str] = None  # 같은 ID 인데 데이터가 안 맞은 경우 디버깅용
    seen_total = 0

    # .pyd init 이 CAN 모니터를 꺼둔 채 끝나므로, 수집을 시작하기 전에 반드시 켠다.
    # (모니터가 꺼져 있으면 DLL 수신 디스패처가 프레임을 버려 AllList 가 비어 있게 됨)
    _enable_can_monitor(hdll)
    hdll.ExtPreSaveCANDataAllList()
    try:
        while True:
            messages = _snapshot(hdll)
            seen_total = len(messages)
            for mid, data_str in messages:
                if mid != target_id:
                    continue
                if not expected:
                    return True, f"0x{mid:X}: {data_str}"
                last_seen_data = data_str
                try:
                    data_bytes = [int(b, 16) for b in data_str.split()]
                except ValueError:
                    continue
                if _matches(data_bytes, expected, mode):
                    return True, f"0x{mid:X}: {data_str}"
            if time.monotonic() >= deadline:
                break
            time.sleep(_POLL_INTERVAL_S)
    finally:
        try:
            hdll.ExtStopSaveCANDataAllList()
        except Exception as e:
            logger.warning("ExtStopSaveCANDataAllList failed: %s", e)

    if last_seen_data is not None:
        detail = (f"0x{target_id:X} 수신은 있으나 데이터 불일치 "
                  f"(마지막 수신: {last_seen_data}, 기대[{mode}]: {expected_data})")
    elif seen_total > 0:
        detail = f"0x{target_id:X} 미수신 ({timeout_s:g}s 동안 다른 메시지 {seen_total}건 수신)"
    else:
        detail = f"0x{target_id:X} 미수신 ({timeout_s:g}s 동안 수신 메시지 없음 - 버스/채널/연결 확인)"
    return False, detail


def check_can_message(instance, message_id: str, expected_data: str = "",
                      match_mode: str = "startswith", timeout: float = 5.0) -> str:
    """수신되어야 PASS. 반환 문자열이 'FAIL:' 로 시작하면 재생 엔진이 스텝 실패 처리."""
    found, detail = find_can_message(instance, message_id, expected_data, match_mode, timeout)
    if found:
        return f"PASS: {detail}"
    return f"FAIL: {detail}"


def check_no_can_message(instance, message_id: str, expected_data: str = "",
                         match_mode: str = "startswith", timeout: float = 5.0) -> str:
    """timeout(초) 동안 한 번도 수신되지 않아야 PASS (레퍼런스 NO_CANMSG 대응)."""
    found, detail = find_can_message(instance, message_id, expected_data, match_mode, timeout)
    if found:
        return f"FAIL: 수신 금지 메시지가 수신됨 - {detail}"
    return f"PASS: {timeout:g}s 동안 미수신 확인 (0x{_parse_id(message_id):X})"
