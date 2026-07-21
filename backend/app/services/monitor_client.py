"""Monitor Client — 관제 서버에 상태를 보고하고 원격 명령을 수신하는 클라이언트."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# websockets 라이브러리가 없으면 aiohttp 또는 기본 라이브러리로 폴백
try:
    import websockets
    from websockets.client import connect as ws_connect
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False


_MACHINE_UID_CACHE: Optional[str] = None

# 상태 전송 로그 스로틀 (전송 주기 2초 기준)
_STATUS_LOG_EVERY = 30       # 성공 하트비트 — 30회 = 약 60초마다 1줄
_STATUS_FAIL_LOG_EVERY = 15  # 연속 실패 — 15회 = 약 30초마다 1줄


def get_machine_uid() -> str:
    """하드웨어 기반의 **안정적** 머신 UID 를 반환한다.

    관제 서버가 각 테스트 PC 를 식별하는 키. IP 는 바뀔 수 있으므로 쓰지 않고,
    메인보드 펌웨어에 각인된 SMBIOS 제품 UUID(부품 교체 전까지 불변)를 우선 사용한다.
    우선순위: SMBIOS/제품 UUID → (Windows) 레지스트리 MachineGuid → (Linux) machine-id
             → 최종 폴백으로 영속 파일에 랜덤 UUID 저장.
    한 번 계산하면 프로세스 내에서 캐시한다.
    """
    global _MACHINE_UID_CACHE
    if _MACHINE_UID_CACHE:
        return _MACHINE_UID_CACHE

    uid = ""
    system = platform.system()
    try:
        if system == "Windows":
            # SMBIOS 제품 UUID (메인보드 펌웨어에 각인 — 부품 교체 전까지 불변)
            try:
                out = subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID"],
                    capture_output=True, text=True, timeout=10,
                )
                cand = (out.stdout or "").strip()
                # 일부 메인보드는 UUID 를 채우지 않아 all-F / all-0 을 반환 → 무효 처리
                if cand and not cand.lower().replace("-", "").strip("f0"):
                    cand = ""
                uid = cand
            except Exception:
                uid = ""
            if not uid:
                # 폴백: 레지스트리 MachineGuid (OS 설치 단위 고유)
                try:
                    import winreg
                    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                        r"SOFTWARE\Microsoft\Cryptography") as k:
                        uid = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
                except Exception:
                    uid = ""
        else:
            # Linux: product_uuid(root 필요할 수 있음) → machine-id
            for p in ("/sys/class/dmi/id/product_uuid", "/etc/machine-id",
                      "/var/lib/dbus/machine-id"):
                try:
                    v = Path(p).read_text().strip()
                    if v:
                        uid = v
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.warning("머신 UID 조회 실패: %s", e)

    if not uid:
        # 최종 폴백: 영속 파일에 랜덤 UUID 저장 (재부팅해도 유지되지만 OS 재설치 시 변경)
        try:
            fallback = Path(__file__).resolve().parent.parent.parent / "machine_uid.txt"
            if fallback.exists():
                uid = fallback.read_text().strip()
            if not uid:
                uid = str(uuid.uuid4())
                fallback.write_text(uid)
        except Exception:
            uid = str(uuid.uuid4())

    _MACHINE_UID_CACHE = uid.strip().lower().replace("{", "").replace("}", "")
    return _MACHINE_UID_CACHE


class MonitorClient:
    """관제 서버에 WebSocket으로 연결하여 상태를 주기적으로 push하는 클라이언트.

    원격 명령 수신 시 콜백을 호출하여 시나리오 재생/중지 등을 처리.
    """

    def __init__(self):
        self._server_url: str = ""
        # 관제 서버 식별 키 — 하드웨어 기반 안정적 머신 UID (IP 대신 사용, 부품 교체 전 불변).
        # 무거운 조회를 피하려 start() 시점에 lazy 계산한다.
        self._client_id: str = ""
        self._client_name: str = platform.node()  # 호스트명
        self._ws: Any = None
        self._task: Optional[asyncio.Task] = None
        self._status_interval: float = 2.0  # 상태 전송 간격 (초)
        self._running = False

        # 최초 연결 가드 — 서버가 접근 불가하면 무한 재시도로 두드리지 않고 중단한다.
        #  - 한 번도 연결에 성공하지 못한 상태에서 _max_initial_attempts 회 실패하면 루프 종료.
        #  - 한 번이라도 연결에 성공(_ever_connected)한 뒤의 끊김은 기존대로 무한 재연결(서버 재시작/일시 단절 복구).
        #  - URL 재설정/재저장(start)으로 다시 시도할 수 있다.
        self._ever_connected = False
        self._initial_attempts = 0
        self._max_initial_attempts = 3

        # 상태 수집 콜백
        self._get_status_fn: Optional[Callable[[], Coroutine[Any, Any, dict]]] = None
        # 원격 명령 수신 콜백
        self._on_command_fn: Optional[Callable[[dict], Coroutine[Any, Any, dict | None]]] = None

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._running

    @property
    def server_url(self) -> str:
        return self._server_url

    def set_status_callback(self, fn: Callable[[], Coroutine[Any, Any, dict]]):
        """상태 수집 콜백 등록. 주기적으로 호출되어 현재 상태를 반환해야 함."""
        self._get_status_fn = fn

    def set_command_callback(self, fn: Callable[[dict], Coroutine[Any, Any, dict | None]]):
        """원격 명령 수신 콜백 등록. 명령을 처리하고 결과를 반환."""
        self._on_command_fn = fn

    async def start(self, server_url: str):
        """관제 서버에 연결 시작."""
        if not HAS_WEBSOCKETS:
            logger.warning("websockets 패키지 미설치 — 관제 서버 연결 불가 (pip install websockets)")
            return

        if not server_url:
            logger.debug("관제 서버 URL 미설정 — 연결하지 않음")
            return

        # 기존 연결 정리
        await self.stop()

        # 머신 UID lazy 계산 (subprocess 호출 — 이벤트 루프 블록 방지 위해 스레드로)
        if not self._client_id:
            try:
                self._client_id = await asyncio.to_thread(get_machine_uid)
            except Exception:
                self._client_id = str(uuid.uuid4())
            logger.info("Monitor client machine_uid=%s", self._client_id)

        # 새로 시작할 때마다 최초 연결 가드 리셋 — URL 재설정/재저장은 '다시 시도' 의도.
        self._ever_connected = False
        self._initial_attempts = 0

        self._server_url = server_url.rstrip("/")
        self._running = True
        self._task = asyncio.create_task(self._connection_loop())
        logger.info("Monitor client 시작: %s", self._server_url)

    async def stop(self):
        """연결 종료."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        logger.info("Monitor client 중지")

    async def _connection_loop(self):
        """자동 재연결 루프."""
        ws_url = self._server_url.replace("http://", "ws://").replace("https://", "wss://")
        if not ws_url.endswith("/ws/client"):
            ws_url = ws_url.rstrip("/") + "/ws/client"

        while self._running:
            try:
                # ping_interval/timeout 을 넉넉히 — 재생 중 이벤트 루프가 잠깐 바빠져도
                # keepalive pong 지연으로 WS 가 끊겨 관제가 오프라인으로 보이는 것을 방지.
                # (최대 ~50초 무응답까지 버팀. 그보다 긴 스톨은 loop_watchdog 로그로 원인 특정)
                async with ws_connect(ws_url, ping_interval=30, ping_timeout=20) as ws:
                    self._ws = ws
                    # 연결 성공 — 이후 끊김은 무한 재연결 대상(최초 연결 가드 해제).
                    self._ever_connected = True
                    self._initial_attempts = 0
                    logger.info("관제 서버 연결 성공: %s", ws_url)

                    # 등록 메시지 전송
                    await ws.send(json.dumps({
                        "type": "register",
                        "client_id": self._client_id,
                        "name": self._client_name,
                        "version": "0.1.0",
                    }))

                    # 등록 확인 대기
                    resp = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    resp_data = json.loads(resp)
                    if resp_data.get("type") == "registered":
                        logger.info("관제 서버 등록 완료: client_id=%s", self._client_id)

                    # 수신 태스크 + 상태 전송 태스크 병렬 실행
                    recv_task = asyncio.create_task(self._receive_loop(ws))
                    send_task = asyncio.create_task(self._send_status_loop(ws))

                    # 하나가 끝나면 나머지도 종료
                    done, pending = await asyncio.wait(
                        [recv_task, send_task],
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for t in pending:
                        t.cancel()
                        try:
                            await t
                        except (asyncio.CancelledError, Exception):
                            pass

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("관제 서버 연결 실패: %s", e)
            finally:
                self._ws = None

            if not self._running:
                break

            # 최초 연결 가드 — 한 번도 성공하지 못한 채 _max_initial_attempts 회 실패하면
            # 접근 불가 서버로 보고 루프를 중단한다(무한 재시도로 두드리지 않음).
            # 관제 서버 URL 재설정/재저장 시 start()가 가드를 리셋하고 다시 시도한다.
            if not self._ever_connected:
                self._initial_attempts += 1
                if self._initial_attempts >= self._max_initial_attempts:
                    logger.warning(
                        "관제 서버(%s) 최초 연결 실패 %d회 — 접근 불가로 판단해 이후 보고를 중단합니다. "
                        "(관제 서버 URL 을 다시 저장하면 재시도)",
                        self._server_url, self._initial_attempts,
                    )
                    self._running = False
                    break
                logger.debug(
                    "관제 서버 최초 연결 실패 %d/%d — 5초 후 재시도",
                    self._initial_attempts, self._max_initial_attempts,
                )

            await asyncio.sleep(5)

    async def _receive_loop(self, ws):
        """서버에서 수신한 메시지 처리 (원격 명령)."""
        try:
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                msg_type = data.get("type")
                if msg_type == "command" and self._on_command_fn:
                    action = data.get("action", "")
                    logger.info("원격 명령 수신: %s", action)
                    try:
                        result = await self._on_command_fn(data)
                        if result is not None:
                            await ws.send(json.dumps({
                                "type": "command_result",
                                "result": result,
                            }))
                    except Exception as e:
                        logger.error("원격 명령 처리 오류: %s", e)
                        await ws.send(json.dumps({
                            "type": "command_result",
                            "result": {"error": str(e)},
                        }))
        except Exception:
            pass  # 연결 종료 시 루프 탈출

    async def _send_status_loop(self, ws):
        """주기적으로 상태를 관제 서버에 전송.

        관측성 규칙 — 2초마다 성공 로그를 남기면 스팸이므로:
          - **activity 변화 시**(대기→재생중 등) INFO 1줄
          - 그 외에는 _STATUS_LOG_EVERY 회마다 하트비트 INFO 1줄
          - **실패는 절대 조용히 삼키지 않는다** — 첫 실패는 traceback 과 함께 WARNING,
            이후엔 _STATUS_FAIL_LOG_EVERY 회마다 WARNING.
        (과거 dev.is_connected AttributeError 가 debug 로 묻혀 관제가 죽은 걸 몰랐던 사고 재발 방지)
        """
        sent = 0
        fail_count = 0
        last_activity: Optional[str] = None
        try:
            while self._running:
                if self._get_status_fn:
                    try:
                        status = await self._get_status_fn()
                        status["type"] = "status_update"
                        status["client_id"] = self._client_id
                        status["name"] = self._client_name
                        status["version"] = "0.1.0"
                        status["timestamp"] = datetime.now(timezone.utc).isoformat()
                        await ws.send(json.dumps(status, default=str))
                        sent += 1
                        if fail_count:
                            logger.info("관제 상태 전송 복구 (이전 실패 %d회)", fail_count)
                            fail_count = 0
                        act = status.get("activity")
                        if act != last_activity:
                            pb = status.get("playback") or {}
                            extra = ""
                            if pb:
                                extra = (f" scenario={pb.get('scenario_name')}"
                                         f" cycle={pb.get('current_cycle')}/{pb.get('total_cycles')}")
                            logger.info("관제 상태 전송: activity=%s%s (누적 %d회)", act, extra, sent)
                            last_activity = act
                        elif sent % _STATUS_LOG_EVERY == 0:
                            logger.info("관제 상태 전송 중: activity=%s (누적 %d회)", act, sent)
                    except Exception as e:
                        fail_count += 1
                        if fail_count == 1 or fail_count % _STATUS_FAIL_LOG_EVERY == 0:
                            logger.warning(
                                "관제 상태 전송 실패 (%d회째) — 관제 대시보드가 '대기/오프라인'으로 굳습니다: %r",
                                fail_count, e, exc_info=(fail_count == 1),
                            )
                await asyncio.sleep(self._status_interval)
        except Exception:
            pass

    async def update_server_url(self, new_url: str):
        """관제 서버 URL 변경 시 재연결.

        같은 URL 을 다시 저장한 경우라도 현재 연결돼 있지 않으면(최초 연결 실패로 중단된 상태 포함)
        재시도한다 — #admin 에서 '저장'을 다시 누르는 것이 '재시도' 의도이기 때문.
        """
        if new_url == self._server_url and self.is_connected:
            return
        if new_url:
            await self.start(new_url)
        else:
            await self.stop()
