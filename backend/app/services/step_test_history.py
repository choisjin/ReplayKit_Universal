"""스텝 테스트(단일 스텝 실행) 이력 저장.

test-step 결과는 원래 HTTP 응답으로만 반환되고 스크린샷은 결과 모달을 닫는 순간
clean-test-screenshots 가 삭제해 휘발된다. 버그 리포트에서 "최근 스텝 테스트
구간 선택 → 판정/스크린샷/로그 취합"을 지원하려면 실행 시점에 별도 사본을
남겨야 하므로, 여기서 판정 레코드(NDJSON)와 이미지 사본을 보존한다.

보존 정책: 최근 MAX_ENTRIES 건 + RETENTION_DAYS 일 (초과분은 append 시 prune).
이 저장은 부수 기능이므로 어떤 실패도 test-step 응답에 영향을 주면 안 된다
(호출부에서 try/except 격리).
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(
    os.environ.get("RECORDING_PROJECT_ROOT",
                   str(Path(__file__).resolve().parent.parent.parent.parent))
)
HISTORY_DIR = _PROJECT_ROOT / "logs" / "step_tests"
HISTORY_FILE = HISTORY_DIR / "history.ndjson"
SHOTS_DIR = HISTORY_DIR / "shots"

MAX_ENTRIES = 50
RETENTION_DAYS = 7

_lock = threading.Lock()

# test-step actual 이미지는 SCREENSHOTS_DIR 기준 상대경로로 반환된다.
from .recording_service import SCREENSHOTS_DIR

_IMAGE_FIELDS = (
    "expected_image", "expected_annotated_image",
    "actual_image", "actual_annotated_image", "diff_image",
)


def _resolve_image(rel_path: str | None) -> Path | None:
    if not rel_path:
        return None
    p = rel_path.replace("\\", "/")
    try:
        ap = Path(p)
        if ap.is_absolute() and ap.exists():
            return ap
    except OSError:
        return None
    cand = SCREENSHOTS_DIR / p
    return cand if cand.exists() else None


def record(scenario_name: str, step_index: int, step, result) -> None:
    """test-step 1회 실행 결과를 이력에 append (동기 — to_thread 로 호출할 것)."""
    exec_ts = datetime.now(timezone.utc)
    dir_name = exec_ts.strftime("%Y%m%d_%H%M%S_%f")[:-3]

    images: dict[str, str] = {}
    shot_dir = SHOTS_DIR / dir_name
    for field in _IMAGE_FIELDS:
        src = _resolve_image(getattr(result, field, None))
        if not src:
            continue
        try:
            shot_dir.mkdir(parents=True, exist_ok=True)
            dst = shot_dir / f"{field}{src.suffix or '.png'}"
            shutil.copy2(str(src), str(dst))
            images[field] = f"shots/{dir_name}/{dst.name}"
        except OSError:
            logger.warning("step-test history: image copy failed: %s", src)

    entry = {
        "ts": exec_ts.isoformat(),
        "scenario": scenario_name,
        "step_index": step_index,
        "step_id": getattr(step, "id", None),
        "step_uid": getattr(step, "uid", ""),
        "step_type": getattr(getattr(step, "type", ""), "value", str(getattr(step, "type", ""))),
        "status": result.status,
        "similarity_score": result.similarity_score,
        "command": result.command,
        "message": result.message,
        "execution_time_ms": result.execution_time_ms,
        "device_id": result.device_id,
        "images": images,
    }

    with _lock:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _prune_locked()


def _prune_locked() -> None:
    entries = _read_all()
    cutoff = datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)
    kept = [
        e for e in entries
        if (_parse_ts(e.get("ts")) or cutoff) >= cutoff
    ][-MAX_ENTRIES:]
    if len(kept) != len(entries):
        tmp = HISTORY_FILE.with_suffix(".ndjson.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in kept:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        tmp.replace(HISTORY_FILE)
    # 이력에서 빠진 항목의 shots 디렉토리 제거
    if SHOTS_DIR.is_dir():
        referenced = set()
        for e in kept:
            for rel in (e.get("images") or {}).values():
                parts = rel.replace("\\", "/").split("/")
                if len(parts) >= 2 and parts[0] == "shots":
                    referenced.add(parts[1])
        for d in SHOTS_DIR.iterdir():
            if d.is_dir() and d.name not in referenced:
                shutil.rmtree(str(d), ignore_errors=True)


def _read_all() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    entries = []
    with open(HISTORY_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive → 로컬 시각으로 간주
    return dt


def list_entries() -> list[dict]:
    """이력 전체(오래된 순). 버그 리포트 context 용."""
    with _lock:
        return _read_all()


def entries_between(from_ts: str, to_ts: str) -> list[dict]:
    """[from_ts, to_ts] 구간(경계 포함)의 이력 레코드."""
    lo = _parse_ts(from_ts)
    hi = _parse_ts(to_ts)
    if not lo or not hi:
        return []
    if lo > hi:
        lo, hi = hi, lo
    out = []
    for e in list_entries():
        t = _parse_ts(e.get("ts"))
        if t and lo <= t <= hi:
            out.append(e)
    return out
