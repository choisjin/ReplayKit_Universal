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
    """업데이트 버튼: git pull → 서버 재시작.

    Windows: .restart 플래그를 server.py(Tkinter 런처)가 watch 해서 처리. git pull 자체는
        런처가 ReplayKit.bat 의 git pull 단계에서 수행.
    Linux:
      - 소스 clone (.git 존재): in-place fetch + reset.
      - .deb 설치본 (.git 없음, REPLAYKIT_INSTALLED=1): cache repo 에 pull → $USER_DATA 로 sync.
        프론트엔드 boot_id 감지로 자동 reload.

    Linux 의 git pull 결과를 응답에 포함시켜 UI 가 "updated/no-change/fetch-failed" 를 구분.
    응답을 보낸 직후 별도 스레드에서 os.execv 로 재시작.
    """
    logger.info("Update requested")

    pull_result: dict = {"performed": False, "mode": "none"}

    if sys.platform != "win32":
        # 1) 동기 git pull — 결과를 응답에 포함하기 위해 background 가 아닌 인라인 수행.
        # subprocess 가 blocking 이므로 to_thread 로 감싸 event loop 미차단.
        def _do_pull() -> dict:
            candidates = [_PROJECT_ROOT]
            app_dir = os.environ.get("REPLAYKIT_APP_DIR")
            if app_dir:
                candidates.append(Path(app_dir))
            candidates.append(Path("/opt/ReplayKit"))
            git_root: Optional[Path] = None
            for c in candidates:
                if (c / ".git").exists():
                    git_root = c
                    break

            if git_root is not None:
                env = os.environ.copy()
                env.pop("GIT_ASKPASS", None)
                env.pop("SSH_ASKPASS", None)
                env["GIT_TERMINAL_PROMPT"] = "0"
                _run(["git", "config", "--global", "--add", "safe.directory", str(git_root)])
                rc, _, err = _run(
                    ["git", "-c", "core.askPass=", "fetch", "origin", "main"],
                    cwd=str(git_root), timeout=60, env=env,
                )
                if rc == 0:
                    rc2, _, err2 = _run(["git", "reset", "--hard", "origin/main"],
                                        cwd=str(git_root), timeout=30)
                    return {
                        "performed": rc2 == 0,
                        "mode": "in-place",
                        "git_root": str(git_root),
                        "error": err2.strip()[:300] if rc2 != 0 else None,
                    }
                return {"performed": False, "mode": "in-place", "error": f"fetch: {err.strip()[:300]}"}
            elif os.environ.get("REPLAYKIT_INSTALLED") == "1":
                ok, msg = _deb_self_update(_PROJECT_ROOT)
                return {"performed": ok, "mode": "deb-sync", "detail": msg}
            else:
                return {"performed": False, "mode": "no-git", "detail": ".git 없음 + .deb 아님"}

        try:
            pull_result = await asyncio.to_thread(_do_pull)
        except Exception as e:
            logger.exception("[update-restart] inline pull 실패")
            pull_result = {"performed": False, "mode": "error", "error": f"{type(e).__name__}: {e}"}

        # 2) 재시작 트리거 — pull 성공/실패 무관하게 (사용자가 다시 시도 가능하도록).
        _RESTART_FLAG.write_text("restart", encoding="utf-8")
        import threading
        threading.Thread(target=_linux_post_pull_restart, daemon=True).start()
    else:
        # Windows: 기존 동작 유지 — 플래그 작성 후 런처가 처리.
        _RESTART_FLAG.write_text("restart", encoding="utf-8")

    return {"status": "restarting", "pull": pull_result}


def _linux_post_pull_restart() -> None:
    """Linux 재시작 — backend 가 직접 os.execv 하지 않고 자식 정리 후 SIGTERM.

    os.execv 는 uvicorn 의 listening socket FD 가 자동으로 닫히지 않아 새 프로세스가
    EADDRINUSE 로 bind 실패. 대신 .restart 플래그를 유지한 채 SIGTERM 으로 자기 종료 →
    부모 (replaykit-gui.py) 의 _check_restart_flag 가 감지해서 새 자식 spawn.

    REPLAYKIT_INSTALLED=1 (= .deb GUI launcher 환경) 만 이 경로. 그 외 (헤드리스/서비스/
    server.py 런처) 는 기존대로 os.execv 시도.
    """
    import time as _t
    _t.sleep(1)
    try:
        if os.environ.get("REPLAYKIT_INSTALLED") == "1":
            # .restart 플래그는 유지 — 부모 launcher 가 이 플래그를 보고 재시작 트리거.
            logger.info("[update-restart] (.deb) sending SIGTERM to self for parent to respawn (pid=%d)", os.getpid())
            try:
                import signal as _sig
                os.kill(os.getpid(), _sig.SIGTERM)
            except Exception:
                # SIGTERM 실패 시 직접 종료 — 부모가 자식 사망 감지
                os._exit(0)
            return

        # 그 외 환경 (예: 시스템 서비스, server.py 런처) — 기존 os.execv 사용.
        # 단 .restart 플래그는 처리됐다는 표시로 제거.
        try:
            _RESTART_FLAG.unlink(missing_ok=True)
        except Exception:
            pass
        logger.info("[update-restart] os.execv → new process (sys.executable=%s argv=%s)",
                    sys.executable, sys.argv)
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.exception("[update-restart] 재시작 실패: %s", e)


def _run(cmd: list[str], cwd: Optional[str] = None, timeout: int = 30,
         env: Optional[dict] = None) -> tuple[int, str, str]:
    """subprocess.run 짧은 wrapper — Linux update flow 에서 반복 사용."""
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, timeout=timeout,
                       encoding="utf-8", errors="replace", env=env)
    return r.returncode, r.stdout or "", r.stderr or ""


# .deb 환경에서 git pull 시 LGE 내부 원격 (배포본 트리). source clone 환경에선
# 기존 origin 을 그대로 쓰므로 이 상수는 .deb 자가 업데이트용.
_DEPLOY_REMOTE_URL = "http://mod.lge.com/hub/dqa_replay_kit/replay_kit_linux.git"

# .deb 자가 업데이트가 git pull 한 결과물을 staging 하는 사용자 캐시 경로.
# $USER_DATA 자체에 git init 하면 launcher 가 만들어둔 symlink (python/, frontend/, tools/)
# 를 통해 /opt/ReplayKit (root-owned, read-only) 에 쓰려다 실패하므로 분리된 cache 에 받음.
def _update_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "replaykit-update"
    return Path.home() / ".cache" / "replaykit-update"


# launcher (replaykit-launcher.sh) 가 /opt/ReplayKit → $USER_DATA 로 복사하는 파일 set 과
# 일치해야 함. 업데이트 시 .update-cache 의 새 버전으로 동일 set 을 덮어쓴다.
# Note: python/, tools/ 등은 symlink 로 유지되어 .deb (apt) 가 관리.
# frontend/dist 는 자가 업데이트로 갱신 가능 — symlink 인 경우 처음 한 번 풀어서 실제
# 디렉토리로 교체. 그 이후엔 .deb apt upgrade 가 더 새로운 frontend 를 가져와도 user 의
# frontend/dist 가 우선 (자가 업데이트가 새 release 추적).
_DEB_USER_SYNC_FILES = ("server.py", "_launcher.py", "requirements.txt", "version.txt", "changelog.json")
_DEB_USER_SYNC_DIRS = ("backend", "frontend/dist")


def _deb_self_update(user_data: Path) -> tuple[bool, str]:
    """.deb 환경 전용 자가 업데이트: cache 에 git pull → user_data 로 sync.

    Returns: (success, message). success=False 면 호출자가 로그를 보고 재시작 여부 판단.
    """
    cache = _update_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    env["GIT_TERMINAL_PROMPT"] = "0"

    # cache 초기화 (첫 호출) 또는 remote URL 보정
    if not (cache / ".git").exists():
        logger.info("[update-restart] init cache repo at %s", cache)
        rc, _, err = _run(["git", "init", "-b", "main"], cwd=str(cache))
        if rc != 0:
            return False, f"git init 실패: {err.strip()[:200]}"
        _run(["git", "remote", "add", "origin", _DEPLOY_REMOTE_URL], cwd=str(cache))
        _run(["git", "config", "--global", "--add", "safe.directory", str(cache)])
    else:
        _run(["git", "remote", "set-url", "origin", _DEPLOY_REMOTE_URL], cwd=str(cache))

    # fetch + reset
    rc, _, err = _run(
        ["git", "-c", "core.askPass=", "fetch", "--depth=1", "origin", "main"],
        cwd=str(cache), timeout=120, env=env,
    )
    if rc != 0:
        return False, f"git fetch 실패: {err.strip()[:300]}"

    rc, _, err = _run(
        ["git", "reset", "--hard", "origin/main"],
        cwd=str(cache), timeout=60,
    )
    if rc != 0:
        return False, f"git reset 실패: {err.strip()[:300]}"

    # 새 파일을 user_data 로 sync. 파일/디렉토리 모두 dest 가 symlink 였을 수 있으니
    # 먼저 제거 후 복사 (symlink target 인 /opt/ReplayKit 에 쓰기 금지).
    import shutil as _sh
    synced: list[str] = []
    for f in _DEB_USER_SYNC_FILES:
        src = cache / f
        if not src.exists():
            continue
        dst = user_data / f
        try:
            if dst.is_symlink() or dst.exists():
                dst.unlink()
        except Exception as e:
            logger.warning("[update-restart] %s unlink 실패: %s", dst, e)
        try:
            _sh.copy2(src, dst)
            synced.append(f)
        except Exception as e:
            logger.warning("[update-restart] %s copy 실패: %s", f, e)

    # backend/ 디렉토리는 절대 rmtree 하면 안 됨 — 안에 사용자 데이터 (scenarios/, results/,
    # settings.json, scan_settings.json 등) 가 있음. 대신 cache 의 파일을 dst 로 overlay 복사.
    # 이미 있는 사용자 데이터는 건드리지 않음. 새/변경된 코드 파일만 덮어쓰여짐.
    # frontend/dist 는 빌드 산출물 전체가 git 트래킹 → rmtree+copytree 안전.
    _OVERLAY_DIRS = {"backend"}  # 안에 사용자 데이터가 섞임 — 파일 단위 overlay
    for d in _DEB_USER_SYNC_DIRS:
        src = cache / d
        if not src.is_dir():
            continue
        dst = user_data / d

        # 부모 경로가 user_data 외부 (예: /opt/ReplayKit) 의 symlink 인 경우 그 부모를
        # 먼저 실제 디렉토리로 교체해야 함. 예: $USER_DATA/frontend 가 /opt/ReplayKit/frontend
        # symlink → $USER_DATA/frontend/dist 쓰기 시 /opt 로 가서 권한 거부.
        parent_path = dst.parent
        if parent_path != user_data and parent_path.is_symlink():
            try:
                target = parent_path.resolve()
                parent_path.unlink()
                parent_path.mkdir(parents=True, exist_ok=True)
                if target.is_dir():
                    for child in target.iterdir():
                        cp_dst = parent_path / child.name
                        if cp_dst.exists():
                            continue
                        try:
                            if child.is_dir():
                                _sh.copytree(child, cp_dst, symlinks=True)
                            else:
                                _sh.copy2(child, cp_dst)
                        except Exception as e:
                            logger.debug("[update-restart] %s 부모 backfill 실패: %s", child, e)
                logger.info("[update-restart] %s symlink → 실제 디렉토리로 교체", parent_path)
            except Exception as e:
                logger.warning("[update-restart] %s 부모 symlink 교체 실패: %s", parent_path, e)

        if d in _OVERLAY_DIRS:
            # 파일 단위 overlay: src 트리를 walk 하면서 각 파일을 dst 에 복사.
            # dst 에만 있는 파일 (= 사용자 데이터) 은 그대로 보존.
            try:
                if dst.is_symlink():
                    dst.unlink()
                dst.mkdir(parents=True, exist_ok=True)
                file_count = 0
                for src_path in src.rglob("*"):
                    rel = src_path.relative_to(src)
                    dst_path = dst / rel
                    if src_path.is_dir():
                        dst_path.mkdir(parents=True, exist_ok=True)
                        continue
                    if src_path.is_file() or src_path.is_symlink():
                        dst_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            if dst_path.is_symlink() or dst_path.exists():
                                dst_path.unlink()
                        except Exception:
                            pass
                        try:
                            if src_path.is_symlink():
                                _sh.copy(src_path, dst_path, follow_symlinks=False)
                            else:
                                _sh.copy2(src_path, dst_path)
                            file_count += 1
                        except Exception as e:
                            logger.debug("[update-restart] %s copy 실패: %s", rel, e)
                synced.append(f"{d}/ (overlay {file_count} files)")
            except Exception as e:
                return False, f"{d}/ overlay 실패: {e}"
        else:
            # 사용자 데이터가 없는 디렉토리 — 전체 교체 안전.
            try:
                if dst.is_symlink():
                    dst.unlink()
                elif dst.exists():
                    _sh.rmtree(dst)
            except Exception as e:
                logger.warning("[update-restart] %s 제거 실패: %s", dst, e)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                _sh.copytree(src, dst, symlinks=True)
                synced.append(d + "/")
            except Exception as e:
                return False, f"{d}/ copy 실패: {e}"

    if not synced:
        return False, "동기화할 파일이 없음 (cache 가 비었거나 build 산출물 누락)"

    logger.info("[update-restart] synced from cache → %s: %s", user_data, synced)
    return True, f"updated: {', '.join(synced)}"


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


# 업데이트 내역 조회용 고정 원격 — LGE Linux 배포 git.
# 로컬 .git (실행 환경의 working tree) 와 무관하게 항상 이 URL 의 main 브랜치 commit 을 보여줌.
# 즉 .deb 설치본이든 source clone 이든 같은 changelog 가 노출됨.
_CHANGELOG_REMOTE_URL = "http://mod.lge.com/hub/dqa_replay_kit/replay_kit_linux.git"


def _changelog_cache_dir() -> Path:
    """endpoint 가 changelog 조회용으로 사용하는 사용자별 캐시 디렉토리.
    XDG_CACHE_HOME → ~/.cache 우선. 없으면 임시 위치.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "replaykit-changelog"
    return Path.home() / ".cache" / "replaykit-changelog"


def _load_bundled_changelog() -> list[dict]:
    """빌드 시점에 생성된 changelog.json 을 읽어 반환.

    경로 후보: _PROJECT_ROOT/changelog.json (.deb 의 /opt/ReplayKit/changelog.json 도 동일 경로).
    파일이 없거나 형식 오류면 빈 리스트.
    """
    path = _PROJECT_ROOT / "changelog.json"
    if not path.exists():
        return []
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [c for c in data if isinstance(c, dict) and "hash" in c]
    except Exception as e:
        logger.warning("[git-log] bundled changelog.json parse failed: %s", e)
    return []


def _ensure_changelog_cache() -> Path:
    """캐시 디렉토리를 git 트래킹 가능한 상태로 보장. 없으면 init + remote 추가."""
    cache = _changelog_cache_dir()
    cache.mkdir(parents=True, exist_ok=True)
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    if not (cache / ".git").exists():
        subprocess.run(["git", "init", "-b", "main"], cwd=str(cache),
                       capture_output=True, timeout=10, creationflags=no_window)
        subprocess.run(["git", "remote", "add", "origin", _CHANGELOG_REMOTE_URL], cwd=str(cache),
                       capture_output=True, timeout=10, creationflags=no_window)
        # safe.directory 등록 (root/user 다른 케이스 회피)
        subprocess.run(["git", "config", "--global", "--add", "safe.directory", str(cache)],
                       capture_output=True, timeout=10, creationflags=no_window)
    else:
        # remote URL 갱신 — 정책 변경 (URL 수정) 시 자동 반영.
        subprocess.run(["git", "remote", "set-url", "origin", _CHANGELOG_REMOTE_URL], cwd=str(cache),
                       capture_output=True, timeout=10, creationflags=no_window)
    return cache


def _find_git_root() -> Optional[Path]:
    """Windows 용 로컬 git working tree 탐색.

    탐색 순서: _PROJECT_ROOT → REPLAYKIT_APP_DIR → /opt/ReplayKit.
    Linux 는 _CHANGELOG_REMOTE_URL 고정 캐시 방식을 사용하므로 호출되지 않음.
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
    """Git 커밋 내역 조회.

    OS 분기:
      - Linux: _CHANGELOG_REMOTE_URL (replay_kit_linux.git) 고정 캐시. 어떤 환경 (.deb,
        source clone) 에서도 동일한 changelog 노출.
      - Windows: 로컬 working tree (.git) 의 origin/main → main → HEAD 순 폴백.

    fetch=true 면 강제 갱신 (네트워크 필요), false 면 캐시/로컬만 조회.

    예외 처리 정책: 어떤 실패에서도 HTTP 500 을 던지지 않고 200 OK + 빈 commits +
    note/fetch_warning 으로 응답. 프론트는 빈 commits 자체를 정상 케이스로 보고
    안내 메시지만 표시 (catch 블록의 message.error popup 안 뜸).
    """
    no_window = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    fetch_error: Optional[str] = None

    def _empty(note: str, **extra) -> dict:
        return {"branch": "(unknown)", "tags": [], "commits": [], "note": note, **extra}

    try:
        # ── Windows: 로컬 working tree 기준 ──
        if sys.platform == "win32":
            git_root = _find_git_root()
            if git_root is None:
                return _empty("git 트래킹이 설정되지 않았습니다.")
            cwd = str(git_root)
            if fetch:
                fetch_r = subprocess.run(
                    ["git", "fetch", "origin", "main"],
                    cwd=cwd, capture_output=True, timeout=15,
                    encoding="utf-8", errors="replace", creationflags=no_window,
                )
                if fetch_r.returncode != 0:
                    fetch_error = fetch_r.stderr.strip()

            commits: list[dict] = []
            used_ref = ""
            for ref in ("origin/main", "main", "HEAD"):
                r = subprocess.run(
                    ["git", "log", ref, f"-{limit}", "--pretty=format:%H||%h||%an||%ae||%aI||%s"],
                    cwd=cwd, capture_output=True, timeout=10,
                    encoding="utf-8", errors="replace", creationflags=no_window,
                )
                if r.returncode == 0 and r.stdout.strip():
                    used_ref = ref
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split("||", 5)
                        if len(parts) < 6:
                            continue
                        commits.append({
                            "hash": parts[0], "short_hash": parts[1],
                            "author": parts[2], "email": parts[3],
                            "date": parts[4], "message": parts[5],
                        })
                    break

            branch_r = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=cwd, capture_output=True, timeout=5,
                encoding="utf-8", errors="replace", creationflags=no_window,
            )
            branch = branch_r.stdout.strip() if branch_r.returncode == 0 else "unknown"
            tag_r = subprocess.run(
                ["git", "tag", "--sort=-creatordate"],
                cwd=cwd, capture_output=True, timeout=5,
                encoding="utf-8", errors="replace", creationflags=no_window,
            )
            tags = [t for t in tag_r.stdout.strip().split("\n") if t] if tag_r.returncode == 0 else []
            result: dict = {
                "branch": branch, "tags": tags, "commits": commits,
                "git_root": cwd, "ref": used_ref,
            }
            if fetch_error:
                result["fetch_warning"] = fetch_error
            if not commits:
                result["note"] = "git log 결과 없음."
            return result

        # ── Linux: 고정 URL 캐시 ──
        cache = _ensure_changelog_cache()
        cwd = str(cache)

        env = os.environ.copy()
        env.pop("GIT_ASKPASS", None)
        env.pop("SSH_ASKPASS", None)
        env["GIT_TERMINAL_PROMPT"] = "0"

        cache_empty = not (cache / ".git" / "refs" / "remotes" / "origin").exists()
        if fetch or cache_empty:
            fetch_r = subprocess.run(
                ["git", "-c", "core.askPass=", "fetch", "--depth=300", "origin", "main"],
                cwd=cwd, capture_output=True, timeout=30,
                encoding="utf-8", errors="replace", creationflags=no_window, env=env,
            )
            if fetch_r.returncode != 0:
                fetch_error = (fetch_r.stderr or fetch_r.stdout or "").strip()
                logger.warning("[git-log] fetch failed: %s", fetch_error[:300])

        r = subprocess.run(
            ["git", "log", "origin/main", f"-{limit}", "--pretty=format:%H||%h||%an||%ae||%aI||%s"],
            cwd=cwd, capture_output=True, timeout=10,
            encoding="utf-8", errors="replace", creationflags=no_window,
        )
        commits = []
        if r.returncode == 0 and r.stdout.strip():
            for line in r.stdout.strip().split("\n"):
                parts = line.split("||", 5)
                if len(parts) < 6:
                    continue
                commits.append({
                    "hash": parts[0], "short_hash": parts[1],
                    "author": parts[2], "email": parts[3],
                    "date": parts[4], "message": parts[5],
                })

        tag_r = subprocess.run(
            ["git", "tag", "--sort=-creatordate"],
            cwd=cwd, capture_output=True, timeout=5, encoding="utf-8", errors="replace",
            creationflags=no_window,
        )
        tags = [t for t in tag_r.stdout.strip().split("\n") if t] if tag_r.returncode == 0 else []

        # 원격 fetch/log 가 실패해서 commits 가 비었을 때 — 빌드 시점에 번들된
        # changelog.json 으로 폴백. .deb 사용자가 LGE 내부 git (mod.lge.com) 에
        # 접근 불가한 환경에서도 적어도 설치 시점의 changelog 는 보이게.
        source = "remote"
        if not commits:
            bundled = _load_bundled_changelog()
            if bundled:
                commits = bundled[:limit]
                source = "bundled"
                logger.info("[git-log] using bundled changelog.json (%d commits)", len(commits))

        result = {
            "branch": "main",
            "tags": tags,
            "commits": commits,
            "remote_url": _CHANGELOG_REMOTE_URL,
            "source": source,
        }
        if fetch_error:
            result["fetch_warning"] = fetch_error
        if not commits:
            result["note"] = (
                "원격에서 commit 을 가져오지 못했고 번들된 changelog.json 도 없습니다. "
                "네트워크/인증 확인 필요 (mod.lge.com 접근 가능 여부)."
            )
        elif source == "bundled":
            # 사용자에게 "오프라인 스냅샷을 보고 있음" 안내. 짧지만 정보성.
            result["note"] = (
                "오프라인 changelog (빌드 시점 스냅샷) 를 표시 중입니다. "
                "최신 commit 을 보려면 mod.lge.com 접근이 필요합니다."
            )
        return result

    except subprocess.TimeoutExpired:
        return _empty("git 명령 timeout — 네트워크 응답 지연.")
    except FileNotFoundError:
        return _empty("git 명령을 찾을 수 없습니다. apt install git 필요.")
    except Exception as e:
        # 예상 못한 모든 예외 — 500 대신 200 + 진단으로 응답해 popup 안 뜨게.
        logger.exception("[git-log] unexpected error: %s", e)
        return _empty(f"내부 오류: {type(e).__name__}: {e}")


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


# 프로세스 boot id — 모듈 import 시점(=백엔드 프로세스 시작 시점) 한 번만 평가.
# version.txt 가 안 바뀐 채로 git pull 만 한 경우 (예: bugfix 인데 버전 bump 안 함) 에도
# 백엔드가 재시작되면 이 값이 바뀌므로 프론트가 그걸로 reload 트리거.
import time as _time
import uuid as _uuid
_BOOT_ID = f"{int(_time.time())}-{_uuid.uuid4().hex[:8]}"


@router.get("/version")
async def get_version():
    """프로젝트 버전 + 백엔드 프로세스 boot_id 조회.

    프론트는 두 값 모두 감시:
      - version 바뀌면 → 빌드 변경됨 → reload
      - boot_id 바뀌면 → 서버 재시작됨 → reload (version 변경 없는 패치도 커버)
    """
    candidates = [
        _PROJECT_ROOT / "version.txt",
        Path(__file__).resolve().parent.parent.parent.parent / "version.txt",
    ]
    version = ""
    for vf in candidates:
        if vf.exists():
            try:
                version = vf.read_text(encoding="utf-8").strip()
                break
            except Exception:
                pass
    return {"version": version, "boot_id": _BOOT_ID}
