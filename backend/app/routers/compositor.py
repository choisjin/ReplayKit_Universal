"""Compositor API — 다중 캡처 소스(웹캠+윈도우)를 합성한 화면 녹화 제어.

- 소스 enum: /api/compositor/sources/webcams, /windows
- 프리셋 CRUD: /api/compositor/presets
- 캡처 lifecycle: /api/compositor/configure, /capture/start, /capture/stop
- 녹화: /api/compositor/record/start, /record/stop, /record/pause, /record/resume
- 상태/프리뷰: /api/compositor/status, /api/compositor/preview.jpg
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

import sys

from ..services.compositor_service import get_compositor_service
from ..services.webcam_service import get_webcam_service

# OS 분기 — Linux 에선 LinControlService 가 동일 API surface 로 동작.
if sys.platform.startswith("linux"):
    from ..services.lincontrol_service import LinControlService as _WindowControlService
else:
    from ..services.wincontrol_service import WinControlService as _WindowControlService

from ..dependencies import device_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/compositor", tags=["compositor"])

_PRESETS_FILE = Path(__file__).resolve().parent.parent.parent / "compositor_presets.json"


# ------------------------------------------------------------
# Preset persistence
# ------------------------------------------------------------
def _load_presets() -> dict:
    if not _PRESETS_FILE.exists():
        return {"active": "", "presets": {}}
    try:
        data = json.loads(_PRESETS_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"active": "", "presets": {}}
        data.setdefault("active", "")
        data.setdefault("presets", {})
        if not isinstance(data["presets"], dict):
            data["presets"] = {}
        return data
    except Exception as e:
        logger.warning("Failed to load compositor presets: %s", e)
        return {"active": "", "presets": {}}


def _save_presets(data: dict) -> None:
    try:
        _PRESETS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to save compositor presets: %s", e)


# ------------------------------------------------------------
# Source enumeration
# ------------------------------------------------------------
@router.get("/sources/webcams")
async def list_webcam_sources():
    """사용 가능한 웹캠 (주 디바이스로 등록된 인덱스 + 단일 webcam 서비스가 점유 중인 인덱스 제외).

    Compositor는 자체 cv2.VideoCapture를 열기 때문에 다른 곳에서 점유 중인 인덱스는
    동시에 열 수 없다 (DSHOW 단일 점유 제약).
    """
    excluded: set[int] = set()
    # 주 디바이스 등록 webcam
    try:
        for d in device_manager.list_primary():
            if d.type == "webcam":
                try:
                    excluded.add(int(d.info.get("device_index", -1)))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    # 단일 webcam 서비스가 열어둔 index
    try:
        ws = get_webcam_service()
        if ws.is_open():
            excluded.add(int(ws._device_index))  # type: ignore[attr-defined]
    except Exception:
        pass
    svc = get_compositor_service()
    # WebcamService.list_devices는 0..max_index를 probe — compositor도 동일 로직 활용
    return {"devices": get_webcam_service().list_devices(exclude=excluded)}


@router.get("/sources/windows")
async def list_window_sources():
    """현재 가시 최상위 윈도우 목록 (WindowControlService.list_processes 재사용).

    OS 에 따라 WinControlService(Win32) 또는 LinControlService(X11) 가 활성화.
    """
    helper = _WindowControlService()
    # is_available 는 인스턴스/클래스 양쪽 호환 — Linux 구현은 인스턴스에서 display 연결까지 검증.
    if not helper.is_available():
        return {"available": False, "windows": []}
    return {"available": True, "windows": helper.list_processes()}


# ------------------------------------------------------------
# Configure / capture / status
# ------------------------------------------------------------
class LayoutConfig(BaseModel):
    canvas: dict = Field(default_factory=dict)
    sources: list = Field(default_factory=list)


@router.post("/configure")
async def configure(layout: LayoutConfig):
    """레이아웃 적용 — capture 중에도 무중단으로 차분 업데이트.

    소스 추가/제거/캡처 파라미터 변경 시 해당 소스만 start/stop.
    레이아웃 변경(x/y/w/h/crop/opacity/z/label/캔버스/FPS)은 즉시 반영.
    """
    svc = get_compositor_service()
    diff = svc.configure(layout.model_dump() if hasattr(layout, "model_dump") else layout.dict())
    return {"layout": svc.get_layout(), "diff": diff}


@router.get("/layout")
async def get_layout():
    return get_compositor_service().get_layout()


@router.post("/capture/start")
async def start_capture():
    svc = get_compositor_service()
    return svc.start_capture()


@router.post("/capture/stop")
async def stop_capture():
    get_compositor_service().stop_capture()
    return {"ok": True}


@router.get("/status")
async def status():
    return get_compositor_service().status()


@router.get("/preview.jpg")
async def preview_jpg():
    svc = get_compositor_service()
    data = svc.get_latest_jpeg()
    if data is None:
        raise HTTPException(status_code=404, detail="No frame available")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


# ------------------------------------------------------------
# Recording
# ------------------------------------------------------------
class RecordStartRequest(BaseModel):
    output_path: str


@router.post("/record/start")
async def record_start(req: RecordStartRequest):
    svc = get_compositor_service()
    ok = svc.start_recording(req.output_path)
    if not ok:
        raise HTTPException(
            status_code=400,
            detail="Failed to start recording (compositor not capturing or already recording)",
        )
    return svc.status()


@router.post("/record/stop")
async def record_stop():
    svc = get_compositor_service()
    path = svc.stop_recording()
    if path is None:
        raise HTTPException(status_code=400, detail="Not recording")
    return {"path": path}


@router.post("/record/pause")
async def record_pause():
    get_compositor_service().pause_recording()
    return {"ok": True}


@router.post("/record/resume")
async def record_resume():
    get_compositor_service().resume_recording()
    return {"ok": True}


# ------------------------------------------------------------
# Presets
# ------------------------------------------------------------
@router.get("/presets")
async def list_presets():
    """전체 프리셋 목록 + 활성 프리셋 이름 + enabled 플래그."""
    data = _load_presets()
    return {
        "active": data.get("active") or "",
        "enabled": bool(data.get("enabled", False)),
        "presets": data.get("presets") or {},
    }


class SavePresetRequest(BaseModel):
    name: str
    layout: LayoutConfig


@router.post("/presets")
async def save_preset(req: SavePresetRequest):
    """프리셋 저장/덮어쓰기."""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Preset name is required")
    data = _load_presets()
    data["presets"][name] = req.layout.dict()
    _save_presets(data)
    return {"ok": True, "name": name}


@router.delete("/presets/{name}")
async def delete_preset(name: str):
    data = _load_presets()
    if name in data["presets"]:
        del data["presets"][name]
        if data.get("active") == name:
            data["active"] = ""
        _save_presets(data)
    return {"ok": True}


class ActivatePresetRequest(BaseModel):
    name: Optional[str] = ""
    enabled: Optional[bool] = None


@router.post("/presets/activate")
async def activate_preset(req: ActivatePresetRequest):
    """프리셋을 활성화 (재생 시 사용할 레이아웃 지정) + enabled 플래그 토글."""
    data = _load_presets()
    if req.name is not None:
        if req.name and req.name not in data["presets"]:
            raise HTTPException(status_code=404, detail=f"Preset not found: {req.name}")
        data["active"] = req.name
    if req.enabled is not None:
        data["enabled"] = bool(req.enabled)
    _save_presets(data)
    return {"active": data.get("active", ""), "enabled": bool(data.get("enabled", False))}


def get_active_layout() -> Optional[dict]:
    """재생 통합용 — 활성 프리셋의 layout dict 반환. 비활성/미설정이면 None."""
    data = _load_presets()
    if not data.get("enabled"):
        return None
    name = data.get("active") or ""
    if not name:
        return None
    layout = data.get("presets", {}).get(name)
    if not layout:
        return None
    return layout
