"""Settings API routes."""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

_SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "settings.json"

_DEFAULTS = {
    "theme": "dark",
    "webcam_save_dir": "",
    "excel_export_dir": "",
    "scenario_export_dir": "",
    "language": "ko",
    "monitor_server_url": "",
    "admin_server_url": "",
    "threshold_full": 0.95,
    "threshold_single_crop": 0.90,
    "threshold_full_exclude": 0.93,
    "threshold_multi_crop": 0.85,
    "threshold_match_crop": 0.85,
}


def _load() -> dict:
    if _SETTINGS_FILE.exists():
        try:
            data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
            return {**_DEFAULTS, **data}
        except Exception:
            pass
    return dict(_DEFAULTS)


def _save(data: dict) -> None:
    _SETTINGS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@router.get("")
async def get_settings():
    return _load()


class UpdateSettingsRequest(BaseModel):
    theme: Optional[str] = None
    webcam_save_dir: Optional[str] = None
    excel_export_dir: Optional[str] = None
    scenario_export_dir: Optional[str] = None
    language: Optional[str] = None
    monitor_server_url: Optional[str] = None
    admin_server_url: Optional[str] = None
    threshold_full: Optional[float] = None
    threshold_single_crop: Optional[float] = None
    threshold_full_exclude: Optional[float] = None
    threshold_multi_crop: Optional[float] = None
    threshold_match_crop: Optional[float] = None


@router.post("")
async def update_settings(req: UpdateSettingsRequest):
    current = _load()
    if req.theme is not None:
        current["theme"] = req.theme
    if req.webcam_save_dir is not None:
        current["webcam_save_dir"] = req.webcam_save_dir
    if req.excel_export_dir is not None:
        current["excel_export_dir"] = req.excel_export_dir
    if req.scenario_export_dir is not None:
        current["scenario_export_dir"] = req.scenario_export_dir
    if req.language is not None:
        current["language"] = req.language
    if req.monitor_server_url is not None:
        current["monitor_server_url"] = req.monitor_server_url
    if req.admin_server_url is not None:
        current["admin_server_url"] = req.admin_server_url
    if req.threshold_full is not None:
        current["threshold_full"] = req.threshold_full
    if req.threshold_single_crop is not None:
        current["threshold_single_crop"] = req.threshold_single_crop
    if req.threshold_full_exclude is not None:
        current["threshold_full_exclude"] = req.threshold_full_exclude
    if req.threshold_multi_crop is not None:
        current["threshold_multi_crop"] = req.threshold_multi_crop
    if req.threshold_match_crop is not None:
        current["threshold_match_crop"] = req.threshold_match_crop
    _save(current)

    # 관제 서버 URL 변경 시 monitor_client 재연결
    if req.monitor_server_url is not None:
        try:
            from ..dependencies import monitor_client
            import asyncio
            asyncio.create_task(monitor_client.update_server_url(req.monitor_server_url))
        except Exception as e:
            logger.debug("Monitor client URL update: %s", e)

    return current


class BrowseFolderRequest(BaseModel):
    initial_dir: Optional[str] = None


def _open_folder_dialog(initial_dir: str = "") -> str:
    """Open a native folder picker dialog using tkinter (runs in main thread)."""
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    kwargs = {}
    if initial_dir and Path(initial_dir).is_dir():
        kwargs["initialdir"] = initial_dir
    folder = filedialog.askdirectory(**kwargs)
    root.destroy()
    return folder or ""


@router.post("/browse-folder")
async def browse_folder(req: BrowseFolderRequest):
    """Open native folder picker dialog and return the selected path."""
    loop = asyncio.get_event_loop()
    try:
        selected = await loop.run_in_executor(None, _open_folder_dialog, req.initial_dir or "")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"폴더 선택 실패: {e}")
    return {"path": selected}


@router.post("/upload-webcam")
async def upload_webcam_recording(file: UploadFile = File(...), filename: str = ""):
    """Save uploaded webcam recording to Results/Video/ directory."""
    dirpath = Path(__file__).resolve().parent.parent.parent.parent / "Results" / "Video"
    if not dirpath.exists():
        try:
            dirpath.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"디렉토리 생성 실패: {e}")

    final_name = filename or file.filename or "webcam_recording.webm"
    dest = dirpath / final_name
    try:
        with open(dest, "wb") as f:
            shutil.copyfileobj(file.file, f)
        return {"result": "ok", "path": str(dest)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"파일 저장 실패: {e}")


class SaveExcelRequest(BaseModel):
    result_filename: str


@router.post("/save-excel")
async def save_excel_to_dir(req: SaveExcelRequest):
    """Export Excel and save directly to the configured directory."""
    result_filename = req.result_filename
    print(f"[save-excel] result_filename={result_filename!r}, settings_file={_SETTINGS_FILE}")
    settings = _load()
    save_dir = settings.get("excel_export_dir", "")
    print(f"[save-excel] excel_export_dir={save_dir!r}")
    if not save_dir:
        raise HTTPException(status_code=400, detail="Excel 저장 경로가 설정되지 않았습니다. 설정 탭에서 경로를 지정하세요.")

    dirpath = Path(save_dir)
    if not dirpath.exists():
        try:
            dirpath.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"디렉토리 생성 실패: {e}")

    # Reuse the export logic from results router
    from .results import RESULTS_DIR
    filepath = RESULTS_DIR / result_filename
    if not filepath.exists():
        raise HTTPException(status_code=404, detail="Result not found")

    data = json.loads(filepath.read_text(encoding="utf-8"))

    from .results import _build_excel_workbook
    try:
        wb = _build_excel_workbook(data, filepath)
    except ImportError:
        raise HTTPException(status_code=500, detail="openpyxl not installed")

    excel_name = result_filename.replace('.json', '.xlsx')
    dest = dirpath / excel_name
    try:
        wb.save(str(dest))
        return {"result": "ok", "path": str(dest)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Excel 저장 실패: {e}")


class SaveExportZipRequest(BaseModel):
    scenarios: list[str] = []
    groups: list[str] = []
    include_all: bool = False


@router.post("/save-export-zip")
async def save_export_zip(req: SaveExportZipRequest):
    """Export scenarios/groups as ZIP and save to the configured directory."""
    settings = _load()
    save_dir = settings.get("scenario_export_dir", "")
    if not save_dir:
        raise HTTPException(status_code=400, detail="내보내기 저장 경로가 설정되지 않았습니다. 설정 탭에서 경로를 지정하세요.")

    dirpath = Path(save_dir)
    if not dirpath.exists():
        try:
            dirpath.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"디렉토리 생성 실패: {e}")

    from ..dependencies import recording_service as recording_svc
    scenario_names = req.scenarios
    group_names = req.groups

    if req.include_all:
        scenario_names = await recording_svc.list_scenarios()
        group_names = list(recording_svc.get_groups().keys())

    if not scenario_names and not group_names:
        raise HTTPException(status_code=400, detail="내보낼 항목이 없습니다.")

    zip_bytes = await recording_svc.export_zip(scenario_names, group_names)

    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    zip_name = f"replaykit_export_{ts}.zip"
    dest = dirpath / zip_name
    try:
        dest.write_bytes(zip_bytes)
        return {"result": "ok", "path": str(dest)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ZIP 저장 실패: {e}")


_PROJECT_ROOT = Path(os.environ.get("RECORDING_PROJECT_ROOT",
                     str(Path(__file__).resolve().parent.parent.parent.parent)))
_RESTART_FLAG = _PROJECT_ROOT / ".restart"


@router.post("/server-restart")
async def server_restart():
    """서버 재시작 요청. server.py(또는 exe)가 .restart 플래그를 감지하여 재시작."""
    logger.info("Server restart requested via API")
    _RESTART_FLAG.write_text("restart", encoding="utf-8")
    return {"status": "restarting"}


@router.get("/power-status")
async def power_status():
    """PC 절전 모드 설정 조회 (Windows 전용)."""
    result = {"ac_standby_seconds": None, "dc_standby_seconds": None, "warning": None}
    if sys.platform != "win32":
        result["warning"] = "Windows만 지원"
        return result
    try:
        import ctypes
        import ctypes.wintypes as wt
        powrprof = ctypes.windll.powrprof
        GUID = ctypes.c_byte * 16
        SLEEP_SUB = (GUID)(0x38, 0x83, 0x30, 0x23, 0x82, 0x15, 0xD2, 0x11, 0x9C, 0xE7, 0x00, 0x80, 0xC7, 0x3C, 0x88, 0x81)
        STANDBY = (GUID)(0x1D, 0xC1, 0xF6, 0x29, 0xEA, 0x42, 0x6D, 0x47, 0x82, 0x8A, 0x3B, 0x06, 0x42, 0x6B, 0xD1, 0xFD)
        val = wt.DWORD(0)
        powrprof.PowerReadACValueIndex(None, None, ctypes.byref(SLEEP_SUB), ctypes.byref(STANDBY), ctypes.byref(val))
        result["ac_standby_seconds"] = val.value
        powrprof.PowerReadDCValueIndex(None, None, ctypes.byref(SLEEP_SUB), ctypes.byref(STANDBY), ctypes.byref(val))
        result["dc_standby_seconds"] = val.value
        if val.value > 0 or result["ac_standby_seconds"] > 0:
            mins = min(result["ac_standby_seconds"] or 99999, result["dc_standby_seconds"] or 99999) // 60
            result["warning"] = f"절전 모드가 {mins}분으로 설정되어 있습니다. 장시간 재생 시 중단될 수 있습니다."
    except Exception as e:
        result["warning"] = f"절전 설정 조회 실패: {e}"
    return result


@router.get("/launcher-log")
async def get_launcher_log(lines: int = 200, date: str = "", source: str = ""):
    """런처/백엔드 로그 읽기 (날짜별 로그 파일).
    source: '' = 런처(날짜별), 'backend' = 백엔드(backend.log + 로테이션)
    """
    from datetime import datetime as _dt
    log_dir = _PROJECT_ROOT / "logs"
    if not log_dir.is_dir():
        return {"lines": [], "dates": [], "sources": ["launcher", "backend"]}

    if source == "backend":
        # 백엔드 로그: backend.log + backend.log.2026-04-09 등
        files = sorted(log_dir.glob("backend.log*"), reverse=True)
        dates = []
        for f in files:
            if f.name == "backend.log":
                dates.append("today")
            else:
                dates.append(f.name.replace("backend.log.", ""))
        target = date or "today"
        if target == "today":
            log_file = log_dir / "backend.log"
        else:
            log_file = log_dir / f"backend.log.{target}"
    else:
        # 런처 로그: 날짜별
        dates = sorted([f.stem for f in log_dir.glob("*.log") if not f.name.startswith("backend")], reverse=True)
        target = date or _dt.now().strftime("%Y-%m-%d")
        log_file = log_dir / f"{target}.log"

    if not log_file.exists():
        return {"lines": [], "dates": dates, "sources": ["launcher", "backend"]}
    try:
        content = log_file.read_text(encoding="utf-8", errors="replace")
        all_lines = content.strip().split("\n") if content.strip() else []
        return {"lines": all_lines[-lines:], "dates": dates, "sources": ["launcher", "backend"]}
    except Exception:
        return {"lines": [], "dates": dates, "sources": ["launcher", "backend"]}


@router.post("/update-and-restart")
async def update_and_restart():
    """서버 종료 → ReplayKit.bat이 git pull + 서버 재시작."""
    logger.info("Update requested — writing .restart flag")
    _RESTART_FLAG.write_text("restart", encoding="utf-8")
    return {"status": "restarting"}


@router.get("/disk-usage")
async def disk_usage():
    """연결된 모든 디스크 드라이브의 사용량 조회."""
    import platform
    drives: list[dict] = []
    if platform.system() == "Windows":
        # Windows: A~Z 드라이브 스캔
        for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
            dp = f"{letter}:\\"
            try:
                total, used, free = shutil.disk_usage(dp)
                if total > 0:
                    drives.append({
                        "drive": f"{letter}:",
                        "total_gb": round(total / (1024 ** 3), 1),
                        "used_gb": round(used / (1024 ** 3), 1),
                        "free_gb": round(free / (1024 ** 3), 1),
                        "used_percent": round(used / total * 100, 1),
                    })
            except (OSError, PermissionError):
                continue
    else:
        # Linux/Mac: 루트 드라이브
        drive = Path(_PROJECT_ROOT).anchor or "/"
        total, used, free = shutil.disk_usage(drive)
        drives.append({
            "drive": drive.rstrip("/") or "/",
            "total_gb": round(total / (1024 ** 3), 1),
            "used_gb": round(used / (1024 ** 3), 1),
            "free_gb": round(free / (1024 ** 3), 1),
            "used_percent": round(used / total * 100, 1),
        })
    return drives


def _find_git_root() -> Optional[Path]:
    """git working tree 위치 탐색.

    탐색 순서:
      1) _PROJECT_ROOT/.git           — 기본 (Mode A / Windows 원본)
      2) REPLAYKIT_APP_DIR/.git       — .deb 설치본 launcher 가 설정 (보통 /opt/ReplayKit)
      3) /opt/ReplayKit/.git          — Linux 하드코딩 폴백
    .git 이 없는 경우(예: .deb 만 설치 후 git_init 안 됨) None 반환.
    """
    candidates = [_PROJECT_ROOT]
    app_dir = os.environ.get("REPLAYKIT_APP_DIR")
    if app_dir:
        candidates.append(Path(app_dir))
    candidates.append(Path("/opt/ReplayKit"))
    for c in candidates:
        if (c / ".git").exists():
            return c
    return None


@router.get("/git-log")
async def git_log(limit: int = 100, fetch: bool = False):
    """Git 커밋 내역 조회. fetch=true면 원격에서 최신 커밋 가져온 후 조회.

    회복력 있게 처리:
      - .git 디렉토리가 없으면 빈 commits 반환 + branch="(no git)" — 200 OK 로 응답해
        프론트가 "loadFailed" toast 가 아닌 "git 미설정" 안내를 표시 가능.
      - fetch 실패해도 로컬 git log 는 계속 시도 (오프라인 환경 대응).
      - origin/main 이 없으면 HEAD / main 순서로 폴백.
    """
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    git_root = _find_git_root()
    if git_root is None:
        # .git 없음 — Mode B (.deb 설치, 첫 실행 전) 등. 에러가 아닌 정상 빈 응답.
        return {
            "branch": "(no git)",
            "tags": [],
            "commits": [],
            "note": "git 트래킹이 설정되지 않았습니다 (.deb 설치본은 apt upgrade 로 업데이트).",
        }

    cwd = str(git_root)
    fetch_error: Optional[str] = None

    try:
        if fetch:
            fetch_r = subprocess.run(
                ["git", "fetch", "origin", "main"],
                cwd=cwd, capture_output=True, timeout=15,
                encoding="utf-8", errors="replace", creationflags=no_window,
            )
            if fetch_r.returncode != 0:
                fetch_error = fetch_r.stderr.strip()
                # 에러로 끝내지 않고 로컬 log 는 계속 시도

        # origin/main → main → HEAD 순서로 시도 — origin 미설정 환경 (clone 직후, .deb 후 git_init 전) 대응.
        commits: list[dict] = []
        used_ref = ""
        for ref in ("origin/main", "main", "HEAD"):
            r = subprocess.run(
                ["git", "log", ref, f"-{limit}", "--pretty=format:%H||%h||%an||%ae||%aI||%s"],
                cwd=cwd,
                capture_output=True, timeout=10, encoding="utf-8", errors="replace",
                creationflags=no_window,
            )
            if r.returncode == 0 and r.stdout.strip():
                used_ref = ref
                for line in r.stdout.strip().split("\n"):
                    parts = line.split("||", 5)
                    if len(parts) < 6:
                        continue
                    commits.append({
                        "hash": parts[0],
                        "short_hash": parts[1],
                        "author": parts[2],
                        "email": parts[3],
                        "date": parts[4],
                        "message": parts[5],
                    })
                break  # 첫 성공 ref 사용

        # 현재 브랜치, 태그 정보
        branch_r = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, timeout=5, encoding="utf-8", errors="replace",
            creationflags=no_window,
        )
        branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "unknown"

        tag_r = subprocess.run(
            ["git", "tag", "--sort=-creatordate"],
            cwd=cwd, capture_output=True, timeout=5, encoding="utf-8", errors="replace",
            creationflags=no_window,
        )
        tags = [t for t in tag_r.stdout.strip().split("\n") if t] if tag_r.returncode == 0 else []

        result: dict = {
            "branch": branch,
            "tags": tags,
            "commits": commits,
            "git_root": cwd,
            "ref": used_ref,
        }
        if fetch_error:
            result["fetch_warning"] = fetch_error
        if not commits:
            result["note"] = "git log 결과 없음 (origin/main / main / HEAD 모두 빈 상태)."
        return result
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail="git command timed out")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="git not found")


# ───────────── 메모리 사용량 모니터링 ─────────────
# Python-side peak 추적 (OS가 추적하는 peak_wset과 별개로 세션 단위 리셋 가능)
# - _peak_memory: 현재 살아있는 프로세스의 관측 peak (죽으면 제거)
# - _session_total_peak: 전체 합계(total RSS)의 세션 최대 스냅샷 — 이것이 사용자에게 보이는 "Session Peak"
_peak_memory: dict[int, int] = {}
_session_total_peak: int = 0


def _find_launcher_root():
    """현재 프로세스에서 부모로 거슬러 올라가 python 런처의 최상위 프로세스를 찾는다.
    server.py로 시작했다면 최상위 python 런처가 root가 되어 자식(백엔드+프론트엔드)을 모두 감쌈.
    직접 uvicorn으로 띄웠으면 자기 자신이 root.
    """
    import psutil
    try:
        me = psutil.Process()
        root = me
        while True:
            try:
                parent = root.parent()
                if not parent:
                    break
                name = parent.name().lower()
                if not (name.startswith("python") or name == "py.exe"):
                    break
                root = parent
            except psutil.Error:
                break
        return root
    except Exception:
        return None


def _classify_process(name: str, cmdline: str) -> str:
    n = name.lower()
    c = cmdline.lower()
    if "uvicorn" in c or "backend.app.main" in c:
        return "backend"
    if "server.py" in c and (n.startswith("python") or n == "py.exe"):
        return "launcher"
    if n in ("node.exe", "node") and ("vite" in c or "npm" in c):
        return "frontend"
    if n in ("node.exe", "node"):
        return "node"
    if n in ("adb.exe", "adb"):
        return "adb"
    if n in ("python.exe", "pythonw.exe", "python", "py.exe"):
        return "python"
    return n.replace(".exe", "") or "unknown"


# ReplayKit 프로세스 트리에 자식으로 등록되지만 실제 프로그램 메모리와 무관한 프로세스.
# (런처가 webbrowser.open() 으로 브라우저를 띄우면 브라우저가 자식으로 묶임 — Chromium은 멀티프로세스라 수십 개)
_EXCLUDED_PROC_NAMES = {
    "msedge.exe", "msedgewebview2.exe",
    "chrome.exe", "chromedriver.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "iexplore.exe",
    "safari.exe",
}


@router.get("/memory-usage")
async def memory_usage():
    """백엔드 프로세스 + 자손(프론트엔드 dev, ADB 등) 메모리 사용량 조회.

    Peak는 두 가지로 제공:
      - peak_mb: Python이 세션 동안 관측한 최대 RSS (리셋 가능)
      - os_peak_mb: Windows OS가 추적하는 프로세스 수명 peak_wset (있을 때만, 참고용)
    """
    import psutil

    root = _find_launcher_root()
    procs: list = []
    if root is not None:
        procs.append(root)
        try:
            procs.extend(root.children(recursive=True))
        except psutil.Error:
            pass

    is_windows = sys.platform == "win32"
    out: list[dict] = []
    total_rss = 0
    alive_pids: set[int] = set()

    for p in procs:
        try:
            try:
                name = p.name()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                name = "?"
            # 브라우저 등 ReplayKit 프로그램 메모리와 무관한 프로세스는 스킵.
            # (런처가 webbrowser.open()으로 띄운 Edge/Chrome이 자식으로 묶이는 문제)
            if name.lower() in _EXCLUDED_PROC_NAMES:
                continue

            mi = p.memory_info()
            rss = int(mi.rss)
            try:
                cmdline = " ".join(p.cmdline())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                cmdline = ""

            alive_pids.add(p.pid)
            prev = _peak_memory.get(p.pid, 0)
            peak = max(prev, rss)
            _peak_memory[p.pid] = peak

            os_peak = None
            if is_windows and hasattr(mi, "peak_wset"):
                os_peak = int(mi.peak_wset)

            total_rss += rss

            out.append({
                "pid": p.pid,
                "name": name,
                "role": _classify_process(name, cmdline),
                "cmdline": cmdline[:200],
                "rss_mb": round(rss / 1024 / 1024, 1),
                "peak_mb": round(peak / 1024 / 1024, 1),
                "os_peak_mb": round(os_peak / 1024 / 1024, 1) if os_peak else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # 죽은 PID 제거 (ADB 단명 자식 프로세스가 계속 dict에 쌓이는 것 방지)
    for pid in list(_peak_memory.keys()):
        if pid not in alive_pids:
            _peak_memory.pop(pid, None)

    # 전체 합계 기준 Session Peak 갱신 (단일 스냅샷 최대치)
    global _session_total_peak
    if total_rss > _session_total_peak:
        _session_total_peak = total_rss

    vm = psutil.virtual_memory()

    return {
        "processes": out,
        "total": {
            "rss_mb": round(total_rss / 1024 / 1024, 1),
            "peak_mb": round(_session_total_peak / 1024 / 1024, 1),
        },
        "system": {
            "total_mb": round(vm.total / 1024 / 1024, 0),
            "available_mb": round(vm.available / 1024 / 1024, 0),
            "used_percent": vm.percent,
        },
    }


@router.post("/memory-usage/reset-peak")
async def reset_memory_peak():
    """Peak 메모리 추적값 리셋 (Python-side만; Windows OS peak_wset은 프로세스 재시작 전엔 리셋 불가)."""
    global _session_total_peak
    _peak_memory.clear()
    _session_total_peak = 0
    return {"status": "ok"}


@router.post("/open-results-folder")
async def open_results_folder():
    """Results 폴더를 파일 탐색기로 열기 (backend/results)."""
    results_dir = _PROJECT_ROOT / "backend" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(results_dir))
    else:
        subprocess.Popen(["xdg-open", str(results_dir)])
    return {"status": "ok", "path": str(results_dir)}


@router.get("/version")
async def get_version():
    """프로젝트 버전 조회 (version.txt). 빌드된 배포본에서는 dist 루트에 위치."""
    candidates = [
        _PROJECT_ROOT / "version.txt",
        Path(__file__).resolve().parent.parent.parent.parent / "version.txt",
    ]
    for vf in candidates:
        if vf.exists():
            try:
                return {"version": vf.read_text(encoding="utf-8").strip()}
            except Exception:
                pass
    return {"version": ""}
