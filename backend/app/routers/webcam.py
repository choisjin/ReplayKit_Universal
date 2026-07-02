"""Webcam API — backend OpenCV 기반 캡처/녹화 제어.

Frontend MediaRecorder를 대체하여 WS 연결 상태와 무관하게 녹화가 유지된다.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from ..services.webcam_service import get_webcam_service
from ..dependencies import device_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webcam", tags=["webcam"])


def _get_primary_webcam_indices() -> set[int]:
    """주 디바이스로 **연결 중**인 웹캠 인덱스 집합 (PIP 목록에서 제외용).

    제외 목적은 하드웨어 경합 방지 — 주 디바이스의 WebcamDevice가 열어둔 캡처를
    PIP가 재오픈하면 기존 스트리밍이 끊긴다. 따라서 실제로 카메라를 점유 중인
    (status=connected) 디바이스만 제외한다. 등록만 되어 있고 연결 해제된 웹캠은
    아무것도 점유하지 않으므로 PIP에서 자유롭게 사용 가능해야 한다.
    (과거: 등록 여부만 보고 전부 제외 → 웹캠 2대를 모두 등록한 환경에서 하나를
    연결 해제해도 PIP 목록이 항상 비는 문제가 있었다.)
    """
    indices: set[int] = set()
    try:
        for d in device_manager.list_primary():
            if d.type == "webcam" and d.status == "connected":
                try:
                    indices.add(int(d.info.get("device_index", -1)))
                except (TypeError, ValueError):
                    pass
    except Exception:
        pass
    return indices


@router.get("/devices")
async def list_devices():
    """Enumerate detected webcam devices (주 디바이스로 등록된 인덱스는 제외).

    중요: 주 디바이스가 점유한 인덱스는 프로브 단계에서 skip해야 한다.
    DirectShow에서 재오픈 시도하면 기존 캡처가 끊어져 스트리밍이 멈춘다.
    """
    svc = get_webcam_service()
    excluded = _get_primary_webcam_indices()
    # excluded 를 함께 반환 — 목록이 비었을 때 "전부 주 디바이스 점유" vs "탐지 실패" 를
    # 프론트/사용자가 구분할 수 있게 한다.
    return {"devices": svc.list_devices(exclude=excluded), "excluded": sorted(excluded)}


@router.get("/resolutions/{device_index}")
async def probe_resolutions(device_index: int):
    """지원 해상도 목록.

    주 디바이스로 등록된 인덱스는 프로브 거부 — 재오픈 시 주 디바이스 캡처가 끊긴다.
    """
    if device_index in _get_primary_webcam_indices():
        # 빈 배열 반환 — 프론트는 fallback 해상도 사용
        return {"resolutions": []}
    svc = get_webcam_service()
    if svc.is_open() and svc._device_index == device_index:
        # 현재 열려 있는 장치는 재오픈 피함 — status에서 현재 해상도만 반환
        return {"resolutions": [f"{svc._width}x{svc._height}"]}
    return {"resolutions": svc.probe_resolutions(device_index)}


class OpenRequest(BaseModel):
    device_index: int = 0
    width: int = 640
    height: int = 480


@router.post("/open")
async def open_webcam(req: OpenRequest):
    """카메라 오픈 + 캡처 스레드 시작.
    주 디바이스로 등록된 인덱스는 거부 (하드웨어 경합 방지).
    """
    if req.device_index in _get_primary_webcam_indices():
        raise HTTPException(
            status_code=409,
            detail=f"Webcam index {req.device_index} is registered as a primary device",
        )
    svc = get_webcam_service()
    ok = svc.open(req.device_index, req.width, req.height)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Failed to open webcam device {req.device_index}")
    return svc.status()


@router.post("/close")
async def close_webcam():
    svc = get_webcam_service()
    svc.close()
    return {"ok": True}


@router.get("/status")
async def get_status():
    svc = get_webcam_service()
    return svc.status()


@router.get("/preview.jpg")
async def preview_jpeg():
    """최신 프레임을 JPEG로 반환 (프런트 PiP 폴링용)."""
    svc = get_webcam_service()
    data = svc.get_latest_jpeg()
    if data is None:
        # 카메라 닫힌 상태 → 404 (프런트가 폴링 중단)
        raise HTTPException(status_code=404, detail="No frame available")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache"},
    )


class RecordStartRequest(BaseModel):
    output_path: str  # absolute or relative to project


@router.post("/record/start")
async def start_record(req: RecordStartRequest):
    """녹화 시작."""
    svc = get_webcam_service()
    ok = svc.start_recording(req.output_path)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to start recording (webcam not open or already recording)")
    return svc.status()


@router.post("/record/stop")
async def stop_record():
    """녹화 정지 + 파일 경로 반환."""
    svc = get_webcam_service()
    path = svc.stop_recording()
    if path is None:
        raise HTTPException(status_code=400, detail="Not recording")
    return {"path": path}


@router.post("/record/pause")
async def pause_record():
    svc = get_webcam_service()
    svc.pause_recording()
    return {"ok": True}


@router.post("/record/resume")
async def resume_record():
    svc = get_webcam_service()
    svc.resume_recording()
    return {"ok": True}


class OverlayRequest(BaseModel):
    position: Optional[str] = None  # top-left | top-right | bottom-left | bottom-right | off
    color_hex: Optional[str] = None  # "#ffffff"
    font_scale: Optional[float] = None  # 0 = auto


@router.post("/overlay")
async def set_overlay(req: OverlayRequest):
    svc = get_webcam_service()
    svc.set_overlay(position=req.position, color_hex=req.color_hex, font_scale=req.font_scale)
    return svc.status()


@router.get("/exposure")
async def get_exposure():
    """현재 노출값/모드 + 카메라 지원 범위."""
    svc = get_webcam_service()
    return svc.get_exposure()


class ExposureRequest(BaseModel):
    value: Optional[float] = None  # manual 노출값 (DSHOW: 보통 -13 ~ 0)
    auto: Optional[bool] = None    # True면 자동 모드


@router.post("/exposure")
async def set_exposure(req: ExposureRequest):
    svc = get_webcam_service()
    ok = svc.set_exposure(value=req.value, auto=req.auto)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to set exposure (camera not open or unsupported)")
    return svc.get_exposure()
