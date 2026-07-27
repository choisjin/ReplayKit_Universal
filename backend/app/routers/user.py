# -*- coding: utf-8 -*-
"""로그인(사용자 식별) 라우터.

프론트가 앱 진입 시 /current 로 로그인 여부를 확인하고, 미로그인이면
로그인 모달에서 /config(프로젝트 목록) + /search(Jira 유저 검색)를 쓴다.
Jira 계정 자체는 백엔드(login_service)에만 있고 여기서는 노출하지 않는다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..services import login_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["user"])


@router.get("/current")
async def get_current_user():
    return {
        "user": login_service.get_login_user(),
        "temporary": login_service.is_temporary_login(),
    }


class UserSelectRequest(BaseModel):
    user_id: Optional[str] = ""
    name: str
    title: Optional[str] = ""
    team: Optional[str] = ""      # 부서/팀
    project: Optional[str] = ""   # 카탈로그 프로젝트 (HKMC / VW 등)
    model: Optional[str] = ""     # 카탈로그 모델 (ccIC27 / MIB 등, 선택)
    # 임시 로그인 — 이번 실행 동안만 유효, 다음 실행 시 로그인 창이 다시 뜬다
    temporary: Optional[bool] = False


@router.post("/current")
async def set_current_user(req: UserSelectRequest):
    # model_dump() 는 pydantic v2 전용 — 배포 PC 의 구버전(v1)에서도 돌도록 직접 구성
    payload = {k: getattr(req, k) for k in
               ("user_id", "name", "title", "team", "project", "model")}
    temporary = bool(req.temporary)
    user = login_service.set_login_user(payload, persist=not temporary)
    logger.info("로그인 사용자 설정%s: %s / %s / %s%s",
                " (임시)" if temporary else "",
                req.name, req.team or "-", req.project or "-",
                f" ({req.model})" if req.model else "")
    return {"user": user, "temporary": login_service.is_temporary_login()}


@router.delete("/current")
async def clear_current_user():
    login_service.set_login_user(None)
    return {"user": None}


@router.get("/config")
async def get_login_config():
    """로그인 모달 구성 — 프로젝트/모델 목록(주 디바이스 카탈로그 원본) +
    유저 검색 가능 여부.

    검색은 Manager 가 대행하므로 이 백엔드에 Jira 계정 자체가 없다 — jira_ready 는
    'Manager 가 검색해 줄 수 있는 상태인지'를 뜻한다(프론트 키 이름은 호환 유지).
    """
    cfg = await asyncio.to_thread(login_service.fetch_login_config)
    return {
        "projects": login_service.login_projects(),
        "jira_ready": bool(cfg["ready"] and cfg["search_url"]),
    }


@router.get("/search")
async def search_users(keyword: str = ""):
    """Jira 유저 검색 (이름/아이디/조직명). 동기 requests 는 스레드로 오프로드.

    ⚠️ 기본값에 fastapi.Query(...) 를 쓰지 않는다 — 배포 PC 의 FastAPI/pydantic
    버전에서 라우터 import 시점에 "TypeError: Expected str, got Query" 로
    서버 기동 자체가 실패했다 (2026-07-23). 검증은 함수 안에서 직접 한다.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        raise HTTPException(status_code=400, detail="검색어(keyword)를 입력하세요")
    try:
        users = await asyncio.to_thread(login_service.search_users, keyword)
    except RuntimeError as e:
        # 구성 미비/인증 실패 — 사용자에게 그대로 보여줄 메시지
        raise HTTPException(status_code=502, detail=str(e))
    except Exception as e:
        logger.warning("Jira 유저 검색 실패: %s", e)
        raise HTTPException(status_code=502, detail=f"Jira 검색 실패: {e}")
    return {"users": users}
