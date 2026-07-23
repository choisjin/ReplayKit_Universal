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

# activity 전이 중 INFO 로 남길 값 — idle/in_use 는 창 포커스만 따라 수시로 뒤집히므로 제외.
_MEANINGFUL_ACTIVITY = {"playing", "recording"}

# 전송 사이 activity 폴링 간격(초) — 상태 전이를 이 해상도로 감지해 즉시 재전송한다.
# 값이 작을수록 짧은 이벤트를 잘 잡지만 폴링이 잦아진다(경량 함수라 0.5s 면 부하 무시).
_TRANSITION_POLL = 0.5

# 재생 중 전송 주기(초) — 테스트 PC 가 가장 바쁜 구간이라 보고 빈도를 낮춰 간섭을 줄인다.
# 평상시(_status_interval=2s)보다 길지만 매니저의 오프라인 판정(45s)보다는 충분히 짧다.
_STATUS_INTERVAL_PLAYING = 10.0


def get_machine_uid() -> str:
    """이 PC 를 식별하는, **재부팅해도 변하지 않는 고정 UID** 를 반환한다.

    관제 서버가 PC 를 구분하는 키다. 값이 한 번이라도 달라지면 같은 PC 가 새 PC 로 잡혀
    관제 목록과 함수 통계가 오염되므로, **실행 환경(관리자 권한 여부, subprocess 성공 여부)에
    좌우되지 않는 소스를 최우선**으로 쓴다.

    우선순위:
      - Windows: 레지스트리 MachineGuid (일반 권한으로 읽힘, subprocess 불필요 → 항상 동일)
                 → SMBIOS 제품 UUID (PowerShell, 실패 가능성 있어 후순위)
      - Linux:   /etc/machine-id (world-readable → 항상 동일)
                 → /var/lib/dbus/machine-id
                 → /sys/class/dmi/id/product_uuid (root 필요 — 권한 따라 값이 흔들려 후순위)
      - 모두 실패 시: machine_uid.txt 에 랜덤 UUID 를 1회 생성·보존해 이후 재사용.

    OS 를 재설치하면 값이 바뀐다(새 PC 로 잡힘). 그 외 재부팅/재시작/권한 변화에는 불변.
    한 번 계산하면 프로세스 내에서 캐시한다.
    """
    global _MACHINE_UID_CACHE
    if _MACHINE_UID_CACHE:
        return _MACHINE_UID_CACHE

    uid = ""
    source = ""
    system = platform.system()
    try:
        if system == "Windows":
            # 1순위: 레지스트리 MachineGuid — 일반 권한으로 읽히고 subprocess 가 없어
            # 실행 환경에 관계없이 **항상 같은 값**이 나온다.
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"SOFTWARE\Microsoft\Cryptography") as k:
                    uid = str(winreg.QueryValueEx(k, "MachineGuid")[0]).strip()
                    source = "registry:MachineGuid"
            except Exception:
                uid = ""
            if not uid:
                # 폴백: SMBIOS 제품 UUID. PowerShell 호출이라 타임아웃/차단 시 실패할 수 있어
                # 후순위 (1순위로 두면 실패한 부팅에서만 값이 달라져 PC 가 중복 등록된다).
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
                    if uid:
                        source = "smbios:ComputerSystemProduct.UUID"
                except Exception:
                    uid = ""
        else:
            # Linux: /etc/machine-id 는 world-readable 이라 권한과 무관하게 항상 같은 값.
            # /sys/class/dmi/id/product_uuid 는 보통 root 여야 읽혀서, 권한이 바뀌면 값도
            # 바뀌어 같은 PC 가 둘로 잡힌다 → 후순위로 둔다.
            for p, label in (
                ("/etc/machine-id", "linux:/etc/machine-id"),
                ("/var/lib/dbus/machine-id", "linux:dbus-machine-id"),
                ("/sys/class/dmi/id/product_uuid", "linux:dmi-product_uuid"),
            ):
                try:
                    v = Path(p).read_text().strip()
                    if v:
                        uid = v
                        source = label
                        break
                except Exception:
                    continue
    except Exception as e:
        logger.warning("머신 UID 조회 실패: %s", e)

    if not uid:
        # 최종 폴백: 영속 파일에 랜덤 UUID 를 1회 만들어 두고 이후 계속 재사용.
        # (파일에 남겨야 재부팅해도 같은 값 — 매번 새로 만들면 PC 가 계속 중복 등록된다)
        fallback = Path(__file__).resolve().parent.parent.parent / "machine_uid.txt"
        try:
            if fallback.exists():
                uid = fallback.read_text().strip()
                source = "file:machine_uid.txt"
            if not uid:
                uid = str(uuid.uuid4())
                fallback.write_text(uid)
                source = "file:machine_uid.txt(new)"
        except Exception as e:
            # 파일조차 못 쓰면 매 기동마다 값이 달라져 관제가 오염된다 — 반드시 눈에 띄게 경고.
            uid = uid or str(uuid.uuid4())
            source = "random(NOT PERSISTED)"
            logger.warning(
                "머신 UID 를 파일(%s)에 보존하지 못했습니다: %s — "
                "재시작마다 다른 PC 로 잡혀 관제/통계가 오염될 수 있습니다.",
                fallback, e,
            )

    _MACHINE_UID_CACHE = uid.strip().lower().replace("{", "").replace("}", "")
    logger.info("머신 UID 결정: %s (source=%s)", _MACHINE_UID_CACHE, source or "unknown")
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
        # 경량 activity 판별 콜백 — 전송 사이 폴링으로 상태 전이를 즉시 감지(짧은 이벤트 포착).
        self._get_activity_fn: Optional[Callable[[], str]] = None
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

    def set_activity_callback(self, fn: Callable[[], str]):
        """경량 activity 판별 콜백 등록. 전송 사이 폴링으로 상태 전이를 감지하는 데 쓴다.

        디바이스 조회 없이 즉시 반환해야 한다(자주 호출됨). 없으면 폴링 없이 기존 주기만 쓴다.
        """
        self._get_activity_fn = fn

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
          - 그 외에는 _STATUS_LOG_EVERY 회마다 하트비트 DEBUG 1줄 (평상시 로그에 안 보임)
          - **실패는 절대 조용히 삼키지 않는다** — 첫 실패는 traceback 과 함께 WARNING,
            이후엔 _STATUS_FAIL_LOG_EVERY 회마다 WARNING.
        (과거 dev.is_connected AttributeError 가 debug 로 묻혀 관제가 죽은 걸 몰랐던 사고 재발 방지)
        """
        sent = 0
        fail_count = 0
        last_activity: Optional[str] = None
        # 이 연결에서 마지막으로 실제 전송한 usage_stats 의 generated_at.
        # 재연결 시 새 태스크로 시작하므로 None 으로 리셋 → 첫 전송에 반드시 포함된다
        # (매니저가 재시작해 상태를 잃었어도 복구됨).
        last_usage_gen: Optional[str] = None
        interval = self._status_interval
        try:
            while self._running:
                if self._get_status_fn:
                    try:
                        status = await self._get_status_fn()
                        # usage_stats 는 60초 주기로만 갱신된다 — 값이 그대로면 키를 통째로
                        # 빼서 2초 페이로드를 줄인다. 매니저는 키가 없으면 마지막 값을 유지한다.
                        # (아직 계산 전이라 None 일 때도 보내지 않는다 — 마지막 값을 덮어쓰지 않도록)
                        us = status.get("usage_stats")
                        gen = us.get("generated_at") if isinstance(us, dict) else None
                        if us is None or (gen and gen == last_usage_gen):
                            status.pop("usage_stats", None)
                        elif gen:
                            last_usage_gen = gen
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
                        # 재생 중에는 주기를 늘려(10초) 테스트 PC 부하/간섭을 줄인다.
                        interval = _STATUS_INTERVAL_PLAYING if act == "playing" else self._status_interval
                        if act != last_activity:
                            pb = status.get("playback") or {}
                            extra = ""
                            if pb:
                                extra = (f" scenario={pb.get('scenario_name')}"
                                         f" cycle={pb.get('current_cycle')}/{pb.get('total_cycles')}")
                            # idle↔in_use 는 사용자가 창을 오갈 때마다 뒤집혀 로그를 도배한다 —
                            # 실제로 알 가치가 있는 재생/녹화 전이만 INFO, 나머지는 DEBUG.
                            _log = logger.info if _MEANINGFUL_ACTIVITY & {act, last_activity} else logger.debug
                            _log(
                                "관제 상태 전송: activity=%s%s (주기 %.0fs, 누적 %d회)",
                                act, extra, interval, sent,
                            )
                            last_activity = act
                        elif sent % _STATUS_LOG_EVERY == 0:
                            # 하트비트는 DEBUG — 상태가 그대로면 로그를 남기지 않는다(백엔드 로그 스팸).
                            # 이상 징후(activity 변화/전송 실패)는 아래·위에서 INFO/WARNING 으로 남는다.
                            logger.debug("관제 상태 전송 중: activity=%s (누적 %d회)", act, sent)
                    except Exception as e:
                        interval = self._status_interval  # 실패 시엔 기본 주기로 재시도
                        fail_count += 1
                        if fail_count == 1 or fail_count % _STATUS_FAIL_LOG_EVERY == 0:
                            logger.warning(
                                "관제 상태 전송 실패 (%d회째) — 관제 대시보드가 '대기/오프라인'으로 굳습니다: %r",
                                fail_count, e, exc_info=(fail_count == 1),
                            )
                # 다음 전송까지 대기 — 그 사이 activity 가 바뀌면 즉시 깨어나 재전송한다.
                # (관제가 상태 전이를 늦어도 _TRANSITION_POLL 이내에 받아 구간 경계가 정확해진다.
                #  activity 판별은 디바이스 조회 없는 경량 함수라 폴링 부하는 무시할 수준)
                await self._wait_or_transition(interval, last_activity)
        except Exception:
            pass

    async def _wait_or_transition(self, interval: float, baseline: Optional[str]):
        """interval 초 동안 대기하되, activity 가 baseline 에서 바뀌면 즉시 반환한다."""
        if not self._get_activity_fn:
            await asyncio.sleep(interval)
            return
        waited = 0.0
        while self._running and waited < interval:
            step = min(_TRANSITION_POLL, interval - waited)
            await asyncio.sleep(step)
            waited += step
            try:
                if self._get_activity_fn() != baseline:
                    return  # 전이 감지 → 즉시 전체 status 재전송
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
