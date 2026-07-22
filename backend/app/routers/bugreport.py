"""버그 리포트 번들 생성 API.

사용자가 최근 동작(스텝 테스트 연속 실행 / 시나리오 재생 결과의 스텝 구간)을
선택하면, 그 시간 범위를 앵커로 backend.log 슬라이스 + 판정결과 + 스크린샷을
ZIP 으로 취합한다. 업로드(Manager 전송)는 프론트가 담당하고, 여기서는 번들만
만든다 — 그래서 업로드 실패 시 같은 blob 을 로컬 저장하는 폴백이 공짜다.

잡 구조는 results.py 의 export-bundle 패턴(백그라운드 스레드 + 진행률 폴링 +
다운로드 후 임시파일 삭제)을 따른다. ZIP 조립은 디스크→디스크 스트리밍이라
대형 결과에서도 메모리에 전체를 올리지 않는다.
"""

import json
import logging
import os
import platform
import re
import socket
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from ..dependencies import device_manager as dm
from ..services import step_test_history
from ..services.playback_service import RESULTS_DIR, STEPS_NDJSON_NAME
from .results import _content_disposition, _list_results_sync, _resolve_image_path
from .settings import _BOOT_ID, _PROJECT_ROOT

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bugreport", tags=["bugreport"])

LOG_DIR = _PROJECT_ROOT / "logs"

# 용량 캡 (계획서 §Part B)
_LOG_SLICE_CAP = 10 * 1024 * 1024      # 구간별 backend.log 슬라이스
_BACKEND_TAIL_CAP = 5 * 1024 * 1024    # 구간 미선택 시 backend.log tail
_LAUNCHER_TAIL_CAP = 2 * 1024 * 1024   # 런처 로그 tail
_IMAGES_PER_RANGE = 30                 # 재생 구간당 이미지 수
_IMAGES_BYTES_PER_RANGE = 30 * 1024 * 1024
_RESULT_JSON_CAP = 30 * 1024 * 1024    # ndjson 없을 때 result.json 파싱 허용 상한
_WINDOW_PAD = timedelta(seconds=30)


# ------------------------------------------------------------------
# 요청 모델
# ------------------------------------------------------------------

class StepTestRange(BaseModel):
    from_ts: str
    to_ts: str


class PlaybackRange(BaseModel):
    run_folder: str
    step_from: int  # step_results 배열 기준 0-based 인덱스 (경계 포함)
    step_to: int


class BuildInclude(BaseModel):
    backend_log: bool = True
    launcher_log: bool = True
    step_test_range: Optional[StepTestRange] = None
    playback_ranges: list[PlaybackRange] = Field(default_factory=list)


class BuildRequest(BaseModel):
    title: str
    description: str = ""
    reporter: str = ""
    include: BuildInclude = Field(default_factory=BuildInclude)
    client: Optional[dict] = None  # {user_agent, app_url} 등 프론트 부가정보


# ------------------------------------------------------------------
# 환경정보 / context
# ------------------------------------------------------------------

def _read_version() -> str:
    candidates = [
        _PROJECT_ROOT / "version.txt",
        Path(__file__).resolve().parent.parent.parent.parent / "version.txt",
    ]
    for vf in candidates:
        if vf.exists():
            try:
                return vf.read_text(encoding="utf-8").strip()
            except OSError:
                pass
    return ""


def _device_summary() -> list[dict]:
    out = []
    try:
        for dev in list(dm.list_primary()) + list(dm.list_auxiliary()):
            d = dev.to_dict() if hasattr(dev, "to_dict") else dict(dev)
            info = d.get("info") or {}
            out.append({
                "id": d.get("id", ""),
                "type": str(d.get("type", "")),
                "name": d.get("name", ""),
                "model": info.get("device_model", "") or d.get("model", ""),
                "status": str(d.get("status", "")),
            })
    except Exception:
        logger.warning("bugreport: device summary failed", exc_info=True)
    return out


def _env_info() -> dict:
    return {
        "version": _read_version(),
        "boot_id": _BOOT_ID,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
        "created_at": datetime.now().astimezone().isoformat(),
    }


@router.get("/context")
async def get_context():
    """버그 리포트 모달 오픈 시 표시할 환경정보 + 최근 동작 목록."""
    import asyncio

    def _sync():
        step_tests = step_test_history.list_entries()
        results = _list_results_sync().get("results", [])
        # 런 폴더 결과만 (레거시 플랫 파일은 스텝 구간 추출 경로가 달라 제외)
        recent = [r for r in results if r.get("run_folder")][:10]
        return step_tests, recent

    step_tests, recent = await asyncio.to_thread(_sync)
    return {
        **_env_info(),
        "devices": _device_summary(),
        "step_tests": list(reversed(step_tests)),  # 최신순
        "recent_results": recent,
    }


# ------------------------------------------------------------------
# 시간 파싱 / 로그 슬라이스
# ------------------------------------------------------------------

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


def _to_local_naive(ts: str | None) -> datetime | None:
    """ISO 문자열(UTC aware 또는 naive) → backend.log 와 비교 가능한 로컬 naive."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt


def _backend_log_files(lo: datetime, hi: datetime) -> list[Path]:
    """시간창이 걸칠 수 있는 backend 로그 파일들 (오래된 날짜 → 현재 순)."""
    files: list[tuple[str, Path]] = []
    for f in LOG_DIR.glob("backend.log.*"):
        date_part = f.name.replace("backend.log.", "")
        try:
            d = datetime.strptime(date_part, "%Y-%m-%d").date()
        except ValueError:
            continue
        if lo.date() - timedelta(days=1) <= d <= hi.date() + timedelta(days=1):
            files.append((date_part, f))
    files.sort()
    ordered = [f for _, f in files]
    current = LOG_DIR / "backend.log"
    if current.exists():
        ordered.append(current)
    return ordered


def _slice_backend_log(lo: datetime, hi: datetime, cap: int = _LOG_SLICE_CAP) -> str:
    """[lo, hi] 로컬 시간창에 해당하는 backend.log 라인 추출.

    타임스탬프 없는 라인(traceback 등)은 직전 라인의 포함 여부를 따른다.
    """
    chunks: list[str] = []
    total = 0
    for path in _backend_log_files(lo, hi):
        include = False  # 파일 경계에서 포함 상태 이월 방지
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = _LOG_TS_RE.match(line)
                    if m:
                        try:
                            t = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                            t = t.replace(microsecond=int(m.group(2)) * 1000)
                            include = lo <= t <= hi
                        except ValueError:
                            pass  # 파싱 실패 시 직전 상태 유지
                    if include:
                        chunks.append(line)
                        total += len(line)
                        if total >= cap:
                            chunks.append("... [truncated: slice cap reached]\n")
                            return "".join(chunks)
        except OSError:
            continue
    return "".join(chunks)


def _tail_bytes(path: Path, cap: int) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as f:
        if size > cap:
            f.seek(size - cap)
        return f.read()


# ------------------------------------------------------------------
# 재생 결과 스텝 구간 추출
# ------------------------------------------------------------------

def _load_range_steps(run_folder: str, step_from: int, step_to: int) -> list[dict]:
    """results/{run}/의 스텝 레코드 중 [step_from, step_to] 인덱스 구간 추출.

    ndjson(줄 단위 스트리밍) 우선 — 대형 결과에서 result.json 전체 파싱 회피.
    """
    if step_from > step_to:
        step_from, step_to = step_to, step_from
    run_dir = RESULTS_DIR / run_folder
    ndjson = run_dir / STEPS_NDJSON_NAME
    records: list[dict] = []
    if ndjson.exists():
        with open(ndjson, encoding="utf-8", errors="replace") as f:
            for idx, line in enumerate(f):
                if idx > step_to:
                    break
                if idx < step_from:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return records

    rj = run_dir / "result.json"
    if not rj.exists():
        return []
    if rj.stat().st_size > _RESULT_JSON_CAP:
        logger.warning("bugreport: result.json too large to parse: %s", run_folder)
        return []
    try:
        data = json.loads(rj.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    steps = data.get("step_results", [])
    return steps[step_from:step_to + 1]


def _step_records_window(records: list[dict]) -> tuple[datetime, datetime] | None:
    """스텝 레코드들의 timestamp min~max(+실행시간) → 로컬 naive 시간창."""
    times: list[datetime] = []
    last_extra_ms = 0
    for r in records:
        t = _to_local_naive(r.get("timestamp"))
        if t:
            times.append(t)
            last_extra_ms = int(r.get("execution_time_ms") or 0) + int(r.get("delay_ms") or 0)
    if not times:
        return None
    return min(times), max(times) + timedelta(milliseconds=last_extra_ms)


# ------------------------------------------------------------------
# 잡 관리 (export-bundle 패턴)
# ------------------------------------------------------------------

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600


def _cleanup_jobs() -> None:
    now = time.monotonic()
    with _JOBS_LOCK:
        stale = [
            jid for jid, j in _JOBS.items()
            if j.get("status") in ("done", "error")
            and now - j.get("finished", now) > _JOB_TTL
        ]
        for jid in stale:
            j = _JOBS.pop(jid, None)
            if j and j.get("zip_path"):
                try:
                    os.unlink(j["zip_path"])
                except OSError:
                    pass


def _set_progress(job_id: str, pct: int, phase: str) -> None:
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if j:
            j["percent"] = max(j.get("percent", 0), min(99, int(pct)))
            j["phase"] = phase


# ------------------------------------------------------------------
# 번들 조립
# ------------------------------------------------------------------

def _add_range_images(zf: zipfile.ZipFile, base: str, records: list[dict]) -> int:
    """선택 구간 스텝들의 이미지 파일을 ZIP 에 복사 (구간당 개수/용량 캡)."""
    added = 0
    total_bytes = 0
    seen: set[str] = set()
    for r in records:
        for field in ("actual_image", "actual_annotated_image", "diff_image",
                      "expected_image", "expected_annotated_image"):
            rel = r.get(field)
            if not rel or rel in seen:
                continue
            seen.add(rel)
            src = _resolve_image_path(rel)
            if not src:
                continue
            try:
                sz = src.stat().st_size
            except OSError:
                continue
            if added >= _IMAGES_PER_RANGE or total_bytes + sz > _IMAGES_BYTES_PER_RANGE:
                return added
            arc = rel.replace("\\", "/").lstrip("/")
            zf.write(str(src), f"{base}/screenshots/{arc}")
            added += 1
            total_bytes += sz
    return added


def _build_bundle(job_id: str, req: BuildRequest) -> None:
    """백그라운드 스레드: ZIP 번들 조립."""
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"bugreport_{stamp}"
        fd, zip_path = tempfile.mkstemp(prefix="bugreport_", suffix=".zip")
        os.close(fd)

        windows: list[dict] = []  # report.json 기록용 {label, from, to}
        slice_windows: list[tuple[str, datetime, datetime]] = []

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # 1) 스텝 테스트 구간
            _set_progress(job_id, 10, "스텝 테스트 이력 수집 중")
            st_range = req.include.step_test_range
            if st_range:
                entries = step_test_history.entries_between(st_range.from_ts, st_range.to_ts)
                if entries:
                    zf.writestr(
                        f"{base}/step_tests/records.json",
                        json.dumps(entries, ensure_ascii=False, indent=2),
                    )
                    for e in entries:
                        for rel in (e.get("images") or {}).values():
                            src = step_test_history.HISTORY_DIR / rel
                            if src.exists():
                                ts_dir = Path(rel).parent.name
                                zf.write(str(src), f"{base}/step_tests/shots/{ts_dir}/{Path(rel).name}")
                    times = [t for e in entries if (t := _to_local_naive(e.get("ts")))]
                    if times:
                        lo, hi = min(times) - _WINDOW_PAD, max(times) + _WINDOW_PAD
                        slice_windows.append(("step_tests", lo, hi))

            # 2) 재생 결과 스텝 구간
            _set_progress(job_id, 30, "재생 결과 구간 수집 중")
            for pr in req.include.playback_ranges:
                # run_folder 는 사용자 선택값이지만 경로 조작 방지로 이름만 허용
                if "/" in pr.run_folder or "\\" in pr.run_folder or ".." in pr.run_folder:
                    continue
                records = _load_range_steps(pr.run_folder, pr.step_from, pr.step_to)
                if not records:
                    continue
                rbase = f"{base}/results/{pr.run_folder}"
                zf.writestr(
                    f"{rbase}/range_steps.json",
                    json.dumps(records, ensure_ascii=False, indent=2),
                )
                _add_range_images(zf, rbase, records)
                win = _step_records_window(records)
                if win:
                    slice_windows.append(
                        (f"playback:{pr.run_folder}", win[0] - _WINDOW_PAD, win[1] + _WINDOW_PAD)
                    )

            # 3) backend.log — 구간 선택 시 슬라이스, 미선택 시 tail 폴백
            _set_progress(job_id, 55, "로그 수집 중")
            if req.include.backend_log:
                if slice_windows:
                    for n, (label, lo, hi) in enumerate(slice_windows, start=1):
                        text = _slice_backend_log(lo, hi)
                        header = (
                            f"# window: {label}\n"
                            f"# {lo.isoformat()} ~ {hi.isoformat()} (local, ±30s pad)\n"
                        )
                        zf.writestr(f"{base}/logs/backend_slice_{n}.log", header + text)
                else:
                    bl = LOG_DIR / "backend.log"
                    if bl.exists():
                        zf.writestr(f"{base}/logs/backend.log", _tail_bytes(bl, _BACKEND_TAIL_CAP))

            # 4) 런처 로그 (오늘 + 어제 tail)
            if req.include.launcher_log:
                today = datetime.now().date()
                for d in (today, today - timedelta(days=1)):
                    lf = LOG_DIR / f"{d.strftime('%Y-%m-%d')}.log"
                    if lf.exists():
                        zf.writestr(
                            f"{base}/logs/launcher_{d.strftime('%Y-%m-%d')}.log",
                            _tail_bytes(lf, _LAUNCHER_TAIL_CAP),
                        )

            # 5) report.json (메타 + 환경 + 선택 구간 정의)
            _set_progress(job_id, 85, "리포트 작성 중")
            for label, lo, hi in slice_windows:
                windows.append({"label": label, "from": lo.isoformat(), "to": hi.isoformat()})
            report = {
                "title": req.title,
                "description": req.description,
                "reporter": req.reporter,
                "client": req.client or {},
                "env": _env_info(),
                "devices": _device_summary(),
                "include": req.include.model_dump(),
                "log_windows": windows,
            }
            zf.writestr(
                f"{base}/report.json",
                json.dumps(report, ensure_ascii=False, indent=2),
            )

        size = os.path.getsize(zip_path)
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j:
                j.update({
                    "status": "done", "percent": 100, "phase": "완료",
                    "zip_path": zip_path, "size": size, "name": f"{base}.zip",
                    "finished": time.monotonic(),
                })
    except Exception as e:
        logger.exception("bugreport build failed")
        with _JOBS_LOCK:
            j = _JOBS.get(job_id)
            if j:
                j["status"] = "error"
                j["error"] = str(e)
                j["finished"] = time.monotonic()


# ------------------------------------------------------------------
# 엔드포인트
# ------------------------------------------------------------------

@router.post("/build")
async def build_report(req: BuildRequest):
    """번들 생성 시작 — job_id 즉시 반환, 진행률은 GET /job/{id} 폴링."""
    if not req.title.strip():
        raise HTTPException(status_code=400, detail="title is required")
    _cleanup_jobs()
    with _JOBS_LOCK:
        if any(j.get("status") == "running" for j in _JOBS.values()):
            raise HTTPException(status_code=409, detail="Another bug report is being built")
        job_id = uuid.uuid4().hex
        _JOBS[job_id] = {
            "status": "running", "percent": 0, "phase": "준비 중",
            "error": None, "created": time.monotonic(),
        }
    threading.Thread(target=_build_bundle, args=(job_id, req), daemon=True).start()
    return {"job_id": job_id}


@router.get("/job/{job_id}")
async def job_status(job_id: str):
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
        if not j:
            raise HTTPException(status_code=404, detail="Job not found")
        return {
            "status": j["status"],
            "percent": j.get("percent", 0),
            "phase": j.get("phase", ""),
            "size": j.get("size"),
            "name": j.get("name"),
            "error": j.get("error"),
        }


@router.get("/job/{job_id}/download")
async def job_download(job_id: str):
    """완료된 번들 ZIP 다운로드. 전송 후 임시파일·잡 정리."""
    with _JOBS_LOCK:
        j = _JOBS.get(job_id)
    if not j or j.get("status") != "done":
        raise HTTPException(status_code=404, detail="Bundle not ready")
    zip_path = j.get("zip_path")
    if not zip_path or not Path(zip_path).exists():
        raise HTTPException(status_code=404, detail="Bundle file missing")
    name = j.get("name", "bugreport.zip")

    def _after():
        with _JOBS_LOCK:
            jj = _JOBS.pop(job_id, None)
        try:
            if jj and jj.get("zip_path"):
                os.unlink(jj["zip_path"])
        except OSError:
            pass

    return FileResponse(
        zip_path,
        media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(name)},
        background=BackgroundTask(_after),
    )
