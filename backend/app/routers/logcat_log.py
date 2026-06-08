"""Android logcat 뷰어 REST + WebSocket 라우터 (serial_log.py와 동일 패턴).

엔드포인트:
  GET  /api/logcat-log/sessions                  — 활성 logcat 세션 목록
  GET  /api/logcat-log/{session_id}/logs         — 백필용 최근 로그 조회
  WS   /ws/logcat-log/{session_id}               — 실시간 로그 스트리밍
  WS   /ws/logcat-lifecycle                      — 세션 시작/종료 이벤트

session_id = device serial.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import queue
import urllib.parse

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from ..services.logcat_service import LOGCAT_HUB, get_logcat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/logcat-log", tags=["logcat-log"])


def _decode_session(session_id: str) -> str:
    return urllib.parse.unquote(session_id)


@router.get("/sessions")
async def list_sessions():
    return {"sessions": LOGCAT_HUB.list_sessions()}


@router.get("/{session_id}/logs")
async def get_recent_logs(session_id: str, limit: int = 1000):
    sid = _decode_session(session_id)
    snap = get_logcat_service().session_snapshot(sid, limit)
    if snap is None:
        raise HTTPException(404, f"Logcat session '{sid}' not active")
    return {
        "session_id": sid,
        "logs": snap["logs"],
        "total": snap["total"],
    }


# ── WebSocket: 실시간 로그 스트림 ─────────────────────────────────────

async def ws_logcat_stream(websocket: WebSocket, session_id: str):
    """실시간 logcat 로그 스트리밍. 접속 시 backfill 후 새 로그 push."""
    await websocket.accept()
    sid = _decode_session(session_id)
    q: queue.Queue = LOGCAT_HUB.register_log(sid)

    snap = get_logcat_service().session_snapshot(sid, 2000)
    if snap is not None:
        try:
            await websocket.send_json({
                "type": "backfill",
                "session_id": sid,
                "logs": snap["logs"],
            })
        except Exception:
            pass

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                line = await loop.run_in_executor(None, functools.partial(q.get, True, 1.0))
            except queue.Empty:
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
        logger.warning("Logcat stream WS error (sid=%s): %s", sid, e)
    finally:
        LOGCAT_HUB.unregister_log(sid, q)
        try:
            await websocket.close()
        except Exception:
            pass


async def ws_logcat_lifecycle(websocket: WebSocket):
    """세션 시작/종료 이벤트 스트림. recv 루프 동시 실행으로 close 즉시 감지."""
    await websocket.accept()
    q: queue.Queue = LOGCAT_HUB.register_lifecycle()
    client = f"{websocket.client.host}:{websocket.client.port}" if websocket.client else "?"
    logger.info("[Logcat WS] lifecycle subscriber connected: %s", client)
    loop = asyncio.get_event_loop()

    async def _recv_drain():
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
        logger.warning("[Logcat WS] lifecycle error: %s", e)
        disconnect_reason = f"exception: {e}"
    finally:
        recv_task.cancel()
        try:
            await recv_task
        except (asyncio.CancelledError, Exception):
            pass
        LOGCAT_HUB.unregister_lifecycle(q)
        logger.info("[Logcat WS] lifecycle subscriber disconnected: %s (reason=%s)", client, disconnect_reason)
        try:
            await websocket.close()
        except Exception:
            pass
