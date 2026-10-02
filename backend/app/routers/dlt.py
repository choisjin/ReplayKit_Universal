"""DLT 뷰어 REST + WebSocket 라우터.

엔드포인트:
  POST /api/dlt/{session_id}/search-all   — 전체 로그 검색
  POST /api/dlt/{session_id}/search-section — 스텝 구간 검색
  WS   /ws/dlt/{session_id}               — 실시간 로그 스트리밍
  WS   /ws/dlt-lifecycle                  — 세션 시작/종료 이벤트
"""

from __future__ import annotations

import asyncio
import functools
import logging
import queue
import urllib.parse
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..plugins.DLTLogging import DLT_HUB, get_active_session

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dlt", tags=["dlt"])


def _decode_session(session_id: str) -> str:
    """URL 경로로 올라온 session_id(host:port)에서 %3A 디코딩."""
    return urllib.parse.unquote(session_id)


class SearchAllRequest(BaseModel):
    keyword: str
    max_results: int = 500


@router.post("/{session_id}/search-all")
async def search_all(session_id: str, req: SearchAllRequest):
    sid = _decode_session(session_id)
    inst = get_active_session(sid)
    if not inst:
        raise HTTPException(404, f"DLT session '{sid}' not active")
    matches = inst.SearchAllDetailed(req.keyword, req.max_results)
    return {
        "session_id": sid,
        "keyword": req.keyword,
        "count": len(matches),
        "matches": matches,
    }


class SearchSectionRequest(BaseModel):
    keyword: str
    from_step: int
    to_step: int
    max_results: int = 500


@router.post("/{session_id}/search-section")
async def search_section(session_id: str, req: SearchSectionRequest):
    sid = _decode_session(session_id)
    inst = get_active_session(sid)
    if not inst:
        raise HTTPException(404, f"DLT session '{sid}' not active")
    if req.from_step not in inst._step_marks:
        return {
            "session_id": sid,
            "keyword": req.keyword,
            "error": f"step {req.from_step} not marked",
            "matches": [],
            "count": 0,
        }
    matches = inst.SearchSectionDetailed(req.keyword, req.from_step, req.to_step, req.max_results)
    return {
        "session_id": sid,
        "keyword": req.keyword,
        "from_step": req.from_step,
        "to_step": req.to_step,
        "count": len(matches),
        "matches": matches,
    }


# ── WebSocket: 실시간 로그 스트림 ─────────────────────────────────────

async def ws_dlt_stream(websocket: WebSocket, session_id: str):
    """실시간 DLT 로그 스트리밍. 접속 시 backfill 후 새 로그 push."""
    await websocket.accept()
    sid = _decode_session(session_id)
    q: queue.Queue = DLT_HUB.register_log(sid)

    # 초기 backfill
    inst = get_active_session(sid)
    if inst:
        try:
            await websocket.send_json({
                "type": "backfill",
                "session_id": sid,
                "logs": inst.GetRecentLogs(2000),
            })
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                # thread-safe queue에서 대기 — 1초 타임아웃으로 disconnect 체크
                line = await loop.run_in_executor(None, functools.partial(q.get, True, 1.0))
            except queue.Empty:
                # Keepalive / disconnect 감지용
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
                continue
            try:
                await websocket.send_json({"type": "log", "line": line})
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("DLT stream WS error (sid=%s): %s", sid, e)
    finally:
        DLT_HUB.unregister_log(sid, q)
        try:
            await websocket.close()
        except Exception:
            pass


async def ws_dlt_lifecycle(websocket: WebSocket):
    """세션 시작/종료 이벤트 스트림. 접속 시 현재 활성 세션들을 즉시 전달.

    recv 루프를 함께 돌려 클라이언트 close 프레임을 즉시 감지한다 (없으면 최대 ping 주기만큼 지연).
    """
    await websocket.accept()
    q: queue.Queue = DLT_HUB.register_lifecycle()
    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    logger.info("[DLT WS] lifecycle subscriber connected: %s (subs now ≥ 1)", client)
    loop = asyncio.get_event_loop()

    async def _recv_drain():
        """클라이언트 close 프레임을 감지하기 위한 recv 루프. 보낼 수 있는 메시지는 없음."""
        try:
            while True:
                await websocket.receive_text()
        except WebSocketDisconnect:
            return
        except Exception:
            return

    recv_task = asyncio.create_task(_recv_drain())
    disconnect_reason = "normal"
    try:
        while True:
            if recv_task.done():
                disconnect_reason = "client_close_detected"
                break
            try:
                event = await loop.run_in_executor(None, functools.partial(q.get, True, 2.0))
            except queue.Empty:
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception as e:
                    disconnect_reason = f"ping_send_fail: {e}"
                    break
                continue
            try:
                await websocket.send_json(event)
            except Exception as e:
                disconnect_reason = f"event_send_fail: {e}"
                break
    except WebSocketDisconnect:
        disconnect_reason = "WebSocketDisconnect"
    except Exception as e:
        logger.warning("[DLT WS] lifecycle error: %s", e)
        disconnect_reason = f"exception: {e}"
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except (asyncio.CancelledError, Exception):
            pass
        DLT_HUB.unregister_lifecycle(q)
        logger.info("[DLT WS] lifecycle subscriber disconnected: %s (reason=%s)", client, disconnect_reason)
        try:
            await websocket.close()
        except Exception:
            pass
