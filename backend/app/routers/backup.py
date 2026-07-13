"""백업/복원 API routes."""

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from ..services import backup_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/backup", tags=["backup"])


@router.get("/list")
async def list_backups():
    """모든 백업 스냅샷을 최신순으로 반환."""
    import asyncio
    return await asyncio.to_thread(backup_service.list_backups)


class CreateBackupRequest(BaseModel):
    reason: str = "manual"


@router.post("/create")
async def create_backup(req: CreateBackupRequest):
    """지금 즉시 수동 백업 생성. (수동 백업은 항상 생성 — force)"""
    import asyncio
    try:
        return await asyncio.to_thread(backup_service.create_backup, req.reason or "manual", True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"백업 생성 실패: {e}")


@router.get("/{backup_id}")
async def get_backup(backup_id: str):
    detail = backup_service.get_backup_detail(backup_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="백업을 찾을 수 없습니다.")
    return detail


@router.get("/{backup_id}/preview")
async def preview_backup(backup_id: str):
    """복원 전 미리보기(담긴 시나리오 + 현재 충돌 여부)."""
    import asyncio
    preview = await asyncio.to_thread(backup_service.preview_restore, backup_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="백업을 찾을 수 없습니다.")
    return preview


@router.get("/{backup_id}/download")
async def download_backup(backup_id: str):
    data = backup_service.read_backup_bytes(backup_id)
    if data is None:
        raise HTTPException(status_code=404, detail="백업을 찾을 수 없습니다.")
    return Response(
        content=data,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{backup_id}.zip"'},
    )


class RestoreFullRequest(BaseModel):
    mode: str = "merge"  # "merge" | "replace"


@router.post("/{backup_id}/restore")
async def restore_full(backup_id: str, req: RestoreFullRequest):
    """스냅샷 전체 복원(merge/replace). 복원 직전 현재 상태를 자동 백업."""
    import asyncio
    mode = req.mode if req.mode in ("merge", "replace") else "merge"
    try:
        return await asyncio.to_thread(backup_service.restore_full, backup_id, mode)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"복원 실패: {e}")


class RestoreScenarioRequest(BaseModel):
    scenario: str


@router.post("/{backup_id}/restore-scenario")
async def restore_scenario(backup_id: str, req: RestoreScenarioRequest):
    """백업에서 특정 시나리오 하나만 복원."""
    import asyncio
    if not req.scenario:
        raise HTTPException(status_code=400, detail="시나리오 이름이 필요합니다.")
    try:
        return await asyncio.to_thread(backup_service.restore_scenario, backup_id, req.scenario)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"복원 실패: {e}")


@router.delete("/{backup_id}")
async def delete_backup(backup_id: str):
    ok = backup_service.delete_backup(backup_id)
    if not ok:
        raise HTTPException(status_code=404, detail="백업을 찾을 수 없습니다.")
    return {"status": "deleted", "id": backup_id}
