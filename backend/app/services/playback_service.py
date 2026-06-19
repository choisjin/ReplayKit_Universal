"""Playback & Verification service — 시나리오 재생 및 검증."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncGenerator, Optional

from ..models.scenario import CompareMode, Scenario, ScenarioResult, Step, StepResult, StepType, SubResult


def _set_sleep_block(block: bool):
    """Windows 절전 모드 차단/해제. 비Windows에서는 무시."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ES_CONTINUOUS = 0x80000000
        if block:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | 0x00000001 | 0x00000002  # SYSTEM + DISPLAY
            )
        else:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    except Exception:
        pass
from .adb_service import ADBService
from .device_manager import DeviceManager
from .image_compare_service import ImageCompareService
from .module_service import execute_module_function

from ..utils.cv_io import safe_imread, safe_imwrite

logger = logging.getLogger(__name__)

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "screenshots"


# ================================================================
# Event Broadcaster — 재생 이벤트를 여러 WebSocket 구독자에게 fan-out
# ================================================================
# WebSocket이 끊어져도 백그라운드 재생은 계속 동작하고, 새로 연결된
# client가 "subscribe" 하면 버퍼링된 최근 이벤트를 재전송 받아 상태를 복구할 수 있다.

_EVENT_BUFFER_MAX = 500  # 재연결 시 replay용 — 그 이전 이벤트는 result.json에서 복원 가능
_event_subscribers: set["asyncio.Queue[dict]"] = set()
_event_buffer: list[dict] = []
_event_buffer_lock = asyncio.Lock()
# 버퍼에 저장할 때 step_result payload에서 제거할 heavy 필드 집합.
# 라이브 브로드캐스트에는 그대로 전달되고, 버퍼 replay 시에만 축약된 형태가 사용된다.
_BUFFER_STRIP_FIELDS = {"sub_results", "match_location", "roi"}

# 현재 실행 중인 백그라운드 재생 태스크 — stop() 이 완전 종료를 기다릴 수 있도록
# 외부(main.py _run_play_job 등)에서 등록한다.
_bg_playback_task: "asyncio.Task | None" = None


def set_bg_playback_task(task: "asyncio.Task | None") -> None:
    """백그라운드 재생 태스크 참조 등록. 새 재생 시작 시 호출."""
    global _bg_playback_task
    _bg_playback_task = task


async def await_bg_playback_task(timeout: float = 15.0) -> bool:
    """등록된 백그라운드 재생 태스크가 완전히 종료될 때까지 대기.

    Returns True if the task finished (or was None), False on timeout.
    재시작 로직이 stop 직후 즉시 새 재생을 시작할 수 있도록 동기화 목적으로 사용.
    """
    t = _bg_playback_task
    if t is None or t.done():
        return True
    try:
        await asyncio.wait_for(asyncio.shield(t), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning("Background playback task did not finish within %ss", timeout)
        return False
    except Exception:
        # 태스크 내부 예외는 무시 — 종료만 기다림
        return True
# 현재 재생이 실제로 진행 중인지 여부 — 중단/완료된 run의 버퍼를 새 subscriber에게 replay하지 않기 위해 사용.
_playback_active: bool = False


def mark_playback_active(active: bool) -> None:
    """새 재생 시작 시 True, 종료/중단 시 False. 버퍼 replay 여부를 결정한다."""
    global _playback_active
    _playback_active = active


def is_playback_active() -> bool:
    """현재 시나리오 재생이 진행 중인지 — DLT/Serial lifecycle 이벤트에 컨텍스트 부착용.
    재생 중이면 ScenarioPage가 좌측에 뷰어를 이미 표시하므로 RecordPage 모달 자동 오픈을 막는다."""
    return _playback_active


def subscribe_events() -> "asyncio.Queue[dict]":
    """재생 이벤트 구독 — 새 Queue를 생성한다.

    현재 재생이 진행 중일 때만 최근 버퍼를 replay한다 (재연결 시 상태 복구).
    재생이 끝났거나 중단된 상태에서는 이전 run의 이벤트가 새 subscriber에게
    유입되지 않도록 버퍼 replay를 건너뛴다.

    호출 측은 이벤트 처리 후 unsubscribe_events를 반드시 호출해야 함.
    """
    q: asyncio.Queue = asyncio.Queue(maxsize=5000)
    if _playback_active:
        for ev in list(_event_buffer):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                break
    _event_subscribers.add(q)
    logger.debug("Event subscriber added (total=%d, replay=%s)",
                 len(_event_subscribers), _playback_active)
    return q


def unsubscribe_events(q: "asyncio.Queue[dict]") -> None:
    """구독 해제."""
    _event_subscribers.discard(q)
    logger.debug("Event subscriber removed (total=%d)", len(_event_subscribers))


def _slim_for_buffer(event: dict) -> dict:
    """버퍼 저장용 경량 복사본. step_result의 heavy 필드를 제거하여 장시간 재생 시
    이벤트 버퍼 메모리 점유를 크게 낮춘다. 라이브 구독자에는 원본이 전달된다."""
    if event.get("type") != "step_result":
        return event
    data = event.get("data")
    if not isinstance(data, dict):
        return event
    slim_data = {k: v for k, v in data.items() if k not in _BUFFER_STRIP_FIELDS}
    return {**event, "data": slim_data}


def publish_event(event: dict) -> None:
    """이벤트를 모든 구독자에게 브로드캐스트 + 버퍼에 추가.
    버퍼에는 축약본을 저장해 장시간 재생 시 메모리 점유를 억제한다."""
    _event_buffer.append(_slim_for_buffer(event))
    if len(_event_buffer) > _EVENT_BUFFER_MAX:
        _event_buffer.pop(0)
    for q in list(_event_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            # 느린 subscriber → 이벤트 drop (재접속 시 버퍼에서 복구)
            pass


def clear_event_buffer() -> None:
    """새 재생 시작 시 버퍼 초기화 (구독자는 유지)."""
    _event_buffer.clear()


def _build_ctor_kwargs(dev) -> dict | None:
    """Build constructor kwargs from device info for module instantiation."""
    ct = dev.info.get("connect_type", "serial" if dev.type == "serial" else "none")
    if ct == "serial":
        return {"port": dev.address, "bps": dev.info.get("baudrate", 115200)}
    elif ct == "socket":
        kwargs = {"host": dev.address}
        for k, v in dev.info.items():
            if k not in ("module", "connect_type"):
                kwargs[k] = v
        return kwargs
    elif ct == "can":
        return {k: v for k, v in dev.info.items() if k not in ("module", "connect_type")}
    return None
RESULTS_DIR = Path(__file__).resolve().parent.parent.parent / "results"

# 현재 재생 런의 출력 디렉토리 (모듈에서 참조용)
_current_run_output_dir: Optional[Path] = None


def get_run_output_dir() -> Optional[Path]:
    """현재 재생 런의 출력 디렉토리 반환. 재생 중이 아니면 None."""
    return _current_run_output_dir


# ==========================================================================
# Runtime fail buffer — 모듈(SerialLogging/DLTLogging)이 캡처 중 발견한
# 비정상 라인을 step_result로 누적하기 위한 hook.
# 재생 중일 때만 buffer에 쌓이고, _run_play_job/group_job 종료 시
# consume_runtime_fails()로 흡수되어 ScenarioResult.step_results에 추가됨.
# ==========================================================================

import threading as _rt_threading
_runtime_fail_buf: list[StepResult] = []
_runtime_fail_lock = _rt_threading.Lock()
_runtime_fail_active = False
_runtime_fail_id_seq = 9000  # 일반 step_id와 충돌 방지용 시리즈
# 현재 실행 중인 step.id — playback_service가 스텝 시작/종료 시 갱신.
# fail_on_keyword(time>0)이 capture loop에서 검출 보고 시 자동으로 parent로 매칭됨.
_current_step_id: Optional[int] = None
_current_repeat_index: int = 1


def mark_runtime_fail_active(active: bool) -> None:
    """재생 시작/종료 시 호출. False면 보고가 무시됨."""
    global _runtime_fail_active
    _runtime_fail_active = active


def set_current_step_context(step_id: Optional[int], repeat_index: int = 1) -> None:
    """현재 실행 중인 스텝 컨텍스트 설정 (스텝 시작 직전에 호출).
    플러그인이 sync 모드 fail_on_keyword 등록 시 get_current_step_context()로 조회하여
    parent_step_id에 박는다."""
    global _current_step_id, _current_repeat_index
    _current_step_id = step_id
    _current_repeat_index = repeat_index


def get_current_step_context() -> tuple[Optional[int], int]:
    """현재 실행 중인 스텝의 (step_id, repeat_index) 반환."""
    return _current_step_id, _current_repeat_index


def report_runtime_fail(source: str, keyword: str, ts: float,
                        line: str = "", repeat_index: int = 1,
                        reason: str = "missing",
                        parent_step_id: Optional[int] = None) -> None:
    """모듈이 실시간 캡처 중 비정상 라인을 발견했을 때 호출.

    Args:
        reason: "missing" (assert_keyword 미일치) | "matched" (fail_on_keyword 검출)
        parent_step_id: 명시 시 이 스텝의 인라인 결과로 분류 (sync 모드).
                        None이면 시나리오 종료 시 tail-drain (legacy 모드).

    재생이 active일 때만 buffer에 쌓이며, parent_step_id 유무에 따라 인라인 또는 tail에 흡수.
    """
    if not _runtime_fail_active:
        return
    global _runtime_fail_id_seq
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    snippet = (line or "").strip()
    if len(snippet) > 200:
        snippet = snippet[:197] + "…"
    if reason == "matched":
        cmd = f"[{source}] fail_on '{keyword}'"
        desc = f"keyword '{keyword}' detected"
    else:
        cmd = f"[{source}] assert '{keyword}'"
        desc = f"keyword '{keyword}' missing"
    with _runtime_fail_lock:
        _runtime_fail_id_seq += 1
        sr = StepResult(
            step_id=_runtime_fail_id_seq,
            repeat_index=repeat_index,
            timestamp=iso,
            command=cmd,
            description=desc,
            status="fail",
            message=snippet,
            parent_step_id=parent_step_id,
        )
        _runtime_fail_buf.append(sr)


def consume_runtime_fails() -> list[StepResult]:
    """버퍼 전체를 비우고 누적된 fail step_results 반환. 재생 종료 시 호출."""
    with _runtime_fail_lock:
        out = list(_runtime_fail_buf)
        _runtime_fail_buf.clear()
    return out


def consume_runtime_fails_for(parent_step_id: int) -> list[StepResult]:
    """버퍼에서 특정 parent_step_id에 매칭되는 fail만 뽑아 반환.
    스텝 종료 직후 호출해 인라인 yield용으로 사용. fail_index도 1-based로 채워준다."""
    with _runtime_fail_lock:
        matched = [sr for sr in _runtime_fail_buf if sr.parent_step_id == parent_step_id]
        if not matched:
            return []
        # 버퍼에서 제거
        _runtime_fail_buf[:] = [sr for sr in _runtime_fail_buf if sr.parent_step_id != parent_step_id]
        # timestamp 순 정렬 (capture 순서가 보통 그대로지만 안전하게)
        matched.sort(key=lambda sr: sr.timestamp or "")
        for i, sr in enumerate(matched, start=1):
            sr.fail_index = i
        return matched


class PlaybackService:
    """Execute scenarios and verify results."""

    def __init__(self, adb: ADBService, image_compare: ImageCompareService, device_manager: DeviceManager):
        self.adb = adb
        self.image_compare = image_compare
        self.dm = device_manager
        self._running = False
        self._should_stop = False
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # 초기: 일시정지 아님
        self._device_map: dict[str, str] = {}  # alias -> real device id for current playback
        self._result_timestamp: str = ""  # 재생 세션별 고유 타임스탬프 (actual 이미지 폴더용)
        self._run_output_dir: Optional[Path] = None  # 런별 출력 디렉토리
        self._run_output_dir_owned = False  # 이 함수가 직접 output dir을 만들었는지
        self._group_scenario_index: int = 0  # 그룹 내 시나리오 순서 (1-based, 0=단일)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_paused(self) -> bool:
        return not self._pause_event.is_set()

    async def stop(self) -> None:
        """재생 중단 요청 + 백그라운드 태스크가 실제로 종료될 때까지 대기.

        호출이 리턴되면 이전 run이 완전히 정리된 상태이므로, 호출자는 바로
        다음 재생을 시작할 수 있다. 장시간 블록되는 액션(예: 모듈 커맨드)이
        걸려 있으면 최대 15초 대기 후 반환한다.
        """
        self._should_stop = True
        self._pause_event.set()  # 일시정지 중이면 풀어서 루프 종료 가능하게
        await await_bg_playback_task(timeout=15.0)

    async def pause(self) -> None:
        self._pause_event.clear()

    async def resume(self) -> None:
        self._pause_event.set()

    async def _wait_if_paused(self) -> bool:
        """일시정지 상태면 재개될 때까지 대기. 중단 시 True 반환."""
        await self._pause_event.wait()
        return self._should_stop

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """중단 가능한 sleep. _should_stop이면 즉시 반환. 중단 시 True 반환."""
        interval = 0.5
        remaining = seconds
        while remaining > 0:
            if self._should_stop:
                return True
            await asyncio.sleep(min(interval, remaining))
            remaining -= interval
        return False

    def _resolve_device_map(self, scenario: Scenario, override_map: Optional[dict[str, str]] = None) -> dict[str, str]:
        """Build alias -> real device ID mapping.

        If override_map is provided (from frontend), use it.
        Otherwise fall back to scenario.device_map.
        """
        if override_map:
            return override_map
        return dict(scenario.device_map) if scenario.device_map else {}

    def _resolve_alias(self, alias: Optional[str], device_map: dict[str, str]) -> Optional[str]:
        """Resolve a device alias to real device ID. If not in map, return as-is (backward compat)."""
        if not alias:
            return alias
        return device_map.get(alias, alias)

    async def preflight_check(self, scenario: Scenario, device_map_override: Optional[dict[str, str]] = None) -> list[str]:
        """Check that all devices referenced in scenario steps are connected.

        Returns a list of error messages. Empty list means all good.
        """
        errors: list[str] = []
        device_map = self._resolve_device_map(scenario, device_map_override)

        # Collect unique device aliases/IDs from steps
        aliases: set[str] = set()
        for step in scenario.steps:
            if step.device_id:
                aliases.add(step.device_id)

        if not aliases:
            return errors

        for alias in sorted(aliases):
            real_id = device_map.get(alias, alias)
            dev = self.dm.get_device(real_id)
            if not dev:
                if alias != real_id:
                    errors.append(f"'{alias}' → 디바이스 '{real_id}'을(를) 찾을 수 없습니다")
                else:
                    errors.append(f"디바이스 '{alias}'을(를) 찾을 수 없습니다 (매핑 없음)")
            elif dev.status not in ("device", "connected"):
                label = f"'{alias}' → " if alias != real_id else ""
                errors.append(f"{label}디바이스 '{dev.name or real_id}'이(가) 연결되어 있지 않습니다 (상태: {dev.status})")

        return errors

    async def execute_scenario(
        self,
        scenario: Scenario,
        verify: bool = True,
        device_map_override: Optional[dict[str, str]] = None,
    ) -> ScenarioResult:
        """Execute all steps in a scenario and optionally verify each step."""
        if self._running:
            raise RuntimeError("Playback already in progress")

        self._device_map = self._resolve_device_map(scenario, device_map_override)
        self._running = True
        _set_sleep_block(True)
        self._should_stop = False
        self._result_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._setup_run_output_dir(scenario.name)
        started_at = datetime.now(timezone.utc).isoformat()

        result = ScenarioResult(
            scenario_name=scenario.name,
            device_serial="multi-device",
            status="pass",
            total_steps=len(scenario.steps),
            started_at=started_at,
        )

        # Build step lookup by ID for conditional jumps
        step_by_id: dict[int, int] = {}
        for i, s in enumerate(scenario.steps):
            step_by_id[s.id] = i

        try:
            idx = 0
            while idx < len(scenario.steps):
                if self._should_stop:
                    logger.info("Playback stopped by user")
                    break

                step = scenario.steps[idx]
                step_result = await self._execute_step(step, scenario.name, verify)
                result.step_results.append(step_result)

                if step_result.status == "pass":
                    result.passed_steps += 1
                elif step_result.status == "fail":
                    result.failed_steps += 1
                else:
                    result.error_steps += 1

                # 이 스텝이 trigger한 sync 모드 fail_on_keyword 결과를 인라인 삽입
                inline_fails = consume_runtime_fails_for(step.id)
                for f_sr in inline_fails:
                    result.step_results.append(f_sr)
                    if f_sr.status == "fail":
                        result.failed_steps += 1
                    elif f_sr.status == "pass":
                        result.passed_steps += 1
                    else:
                        result.error_steps += 1

                # Conditional jump
                next_idx = idx + 1
                if step_result.status == "pass" and step.on_pass_goto is not None:
                    if step.on_pass_goto == -1:
                        break
                    target = step_by_id.get(step.on_pass_goto)
                    if target is not None:
                        next_idx = target
                elif step_result.status in ("fail", "error") and step.on_fail_goto is not None:
                    if step.on_fail_goto == -1:
                        break
                    target = step_by_id.get(step.on_fail_goto)
                    if target is not None:
                        next_idx = target
                idx = next_idx
        except Exception as e:
            logger.error("Playback error: %s", e)
            result.status = "error"
        finally:
            self._running = False
            _set_sleep_block(False)
            set_current_step_context(None, 1)
            self._cleanup_run_output_dir()
            result.finished_at = datetime.now(timezone.utc).isoformat()

        # Determine overall status
        if result.failed_steps > 0 or result.error_steps > 0:
            result.status = "fail"
        else:
            result.status = "pass"

        # Save result
        await self._save_result(result)
        return result

    async def execute_scenario_stream(
        self,
        scenario: Scenario,
        verify: bool = True,
        repeat_index: int = 1,
        start_step: int = 0,
        device_map_override: Optional[dict[str, str]] = None,
        group_scenario_index: int = 0,
    ) -> AsyncGenerator:
        """Execute scenario and yield step results one by one (for WebSocket streaming).

        Args:
            start_step: 0-based step index to start execution from (skip earlier steps).
        """
        self._device_map = self._resolve_device_map(scenario, device_map_override)
        self._group_scenario_index = group_scenario_index
        self._running = True
        _set_sleep_block(True)
        # 그룹 재생에서 호출 시 _should_stop을 리셋하면 안 됨 (이미 설정된 경우)
        if not self._result_timestamp:
            self._should_stop = False
        self._current_iteration = repeat_index - 1  # 0-based for cycle wait
        # ALL_RANDOM 등 일부 액션이 로그 기록에 scenario_name / repeat_index 를 필요로 함
        self._current_scenario_name = scenario.name
        self._current_repeat_index = repeat_index
        if not self._result_timestamp:
            self._result_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._setup_run_output_dir(scenario.name)
            self._run_output_dir_owned = True
        else:
            self._run_output_dir_owned = False

        # Build step lookup by ID for conditional jumps
        step_by_id: dict[int, int] = {}  # step.id -> index
        for i, s in enumerate(scenario.steps):
            step_by_id[s.id] = i

        # 그룹 재생 시 호출자(main.py)가 _result_timestamp를 미리 설정하므로
        # 여기서 cleanup하면 안 됨 — _is_group_member로 판별
        _is_group_member = self._result_timestamp != "" and not self._run_output_dir_owned
        try:
            idx = max(0, start_step)
            while idx < len(scenario.steps):
                if self._should_stop:
                    break
                step = scenario.steps[idx]

                # 스텝 시작 알림
                yield {
                    "_type": "step_start",
                    "step_id": step.id,
                    "repeat_index": repeat_index,
                    "device_id": step.device_id or "",
                    "command": self._format_command(step),
                    "description": step.description or "",
                    "delay_ms": step.delay_after_ms,
                }

                step_result = await self._execute_step(step, scenario.name, verify, repeat_index=repeat_index)
                yield step_result

                # 이 스텝이 trigger한 sync 모드 fail_on_keyword 결과를 인라인으로 yield
                # (parent_step_id == step.id로 매칭된 항목만)
                inline_fails = consume_runtime_fails_for(step.id)
                for f_sr in inline_fails:
                    yield f_sr

                # Determine next step based on conditional jump
                next_idx = idx + 1
                if step_result.status == "pass" and step.on_pass_goto is not None:
                    if step.on_pass_goto == -1:
                        break  # END
                    target = step_by_id.get(step.on_pass_goto)
                    if target is not None:
                        next_idx = target
                elif step_result.status in ("fail", "error") and step.on_fail_goto is not None:
                    if step.on_fail_goto == -1:
                        break  # END
                    target = step_by_id.get(step.on_fail_goto)
                    if target is not None:
                        next_idx = target
                idx = next_idx
        finally:
            self._running = False
            _set_sleep_block(False)
            set_current_step_context(None, 1)
            # 중단된 경우 run_dir 참조를 유지 — 호출자(_run_play_job.finally)가
            # cleanup_active_instances로 StopLogging(또는 SerialLogging의 경우 Disconnect가
            # 내부적으로 StopLogging 우선 호출)을 호출할 때 결과 폴더의 logs/에 시리얼/DLT
            # 로그가 저장되도록 하기 위함. 정상 완료에선 그대로 정리.
            if not _is_group_member and not self._should_stop:
                self._cleanup_run_output_dir()

    async def execute_single_step(self, step: Step, scenario_name: str, device_map: Optional[dict[str, str]] = None) -> StepResult:
        """Execute a single step with verification (for testing individual steps).

        매 호출마다 ``actual_<ms_timestamp>/`` 서브디렉토리에 캡처를 저장하여
        이전 테스트 이미지와의 경로 충돌을 원천 차단한다 (브라우저/antd Image
        컴포넌트의 preview 캐싱 우회 목적). cleanup은 clean-test-screenshots가
        ``actual*`` 패턴을 일괄 삭제한다.

        이전 full playback이 중간에 끊겨 _run_output_dir이 남아 있으면 스크린샷이
        results 폴더로 저장되어 프론트엔드(/screenshots/ 기준)에서 404가 나므로
        상태를 명시적으로 리셋한다.
        """
        self._should_stop = False  # 이전 재생 중단 플래그 초기화
        self._pause_event.set()    # 이전 재생이 pause 상태로 끝난 경우 풀어줌
        self._device_map = device_map or {}
        self._current_iteration = 0  # 단일 테스트는 항상 0번째
        self._current_scenario_name = scenario_name
        self._current_repeat_index = 1
        # 이전 run의 stale 상태 제거 — 프론트 이미지 URL 일관성 보장
        self._run_output_dir = None
        self._run_output_dir_owned = False
        # 매 호출마다 고유 ms timestamp → actual_<ms> 서브디렉토리 사용
        self._result_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        self._group_scenario_index = 0
        try:
            return await self._execute_step(step, scenario_name, verify=True)
        finally:
            self._result_timestamp = ""

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_step(
        self,
        step: Step,
        scenario_name: str,
        verify: bool,
        repeat_index: int = 1,
    ) -> StepResult:
        """Execute a single step, capture screenshot, and verify."""
        start_time = time.time()
        step_result = StepResult(
            step_id=step.id,
            repeat_index=repeat_index,
            status="pass",
            timestamp=datetime.now(timezone.utc).isoformat(),
            device_id=step.device_id or "",
            command=self._format_command(step),
            description=step.description or "",
            delay_ms=step.delay_after_ms,
        )

        # File prefix includes cycle number to avoid overwriting across repeats
        file_prefix = f"c{repeat_index}_step_{step.id:03d}"

        # 일시정지 상태면 재개될 때까지 대기
        if await self._wait_if_paused():
            step_result.status = "error"
            step_result.message = "Stopped by user"
            return step_result

        # fail_on_keyword(time>0) 등 백그라운드 캡처가 보고하는 runtime fail이
        # 이 스텝의 인라인 결과로 분류되도록 컨텍스트 설정.
        set_current_step_context(step.id, repeat_index)

        t0 = t1 = t2 = t3 = t4 = start_time
        try:
            # 1) 액션 실행 전: 해당 스텝의 디바이스 연결 확인
            if self._should_stop:
                step_result.status = "error"
                step_result.message = "Stopped by user"
                return step_result
            t0 = time.time()
            action_device_id = self._resolve_real_device_id(step)
            # MODULE_COMMAND의 경우 step.device_id가 엉뚱한 primary(HKMC/iSAP)를 가리킬 때가
            # 있어(레거시 녹화·수동 편집) 해당 디바이스 재연결에 오래 걸림. 모듈 자체는
            # module_service가 자체 인스턴스/연결을 관리하므로, 매칭되지 않으면 ensure 스킵.
            skip_ensure = False
            if step.type == StepType.MODULE_COMMAND:
                requested_module = (step.params or {}).get("module", "") if step.params else ""
                # logcat 버퍼만 판독/저장하는 Android 함수는 디바이스 연결과 무관하다 — StartLogging 이
                # 띄운 캡처 스레드의 버퍼를 읽을 뿐이다. 전원 사이클(suspend) 시나리오에서 기기가
                # 의도적으로 꺼져 있어도 즉시 판독해야 하므로 ensure(재연결 대기)를 건너뛴다.
                # (안 그러면 꺼진 adb 기기를 _ensure_device_connected 가 24×5s 재시도하며 ~2분 낭비)
                _buffn = (step.params or {}).get("function", "")
                if requested_module == "Android" and _buffn in (
                    "Monitor_pass_on_keyword", "Monitor_fail_on_keyword", "StopLogging",
                ):
                    skip_ensure = True
                if action_device_id:
                    dev_check = self.dm.get_device(action_device_id)
                    has_matching_module = bool(
                        dev_check and dev_check.info
                        and dev_check.info.get("module") == requested_module
                    )
                    # Android 가상 모듈은 ADB 기기에 붙으므로 타입 체크로 대체 허용
                    if not has_matching_module and requested_module == "Android":
                        has_matching_module = bool(dev_check and dev_check.type == "adb")
                    # HKMC6th 가상 모듈은 hkmc_agent 디바이스에 붙음
                    if not has_matching_module and requested_module == "HKMC6th":
                        has_matching_module = bool(dev_check and dev_check.type == "hkmc_agent")
                    if not has_matching_module:
                        logger.warning(
                            "Step %d MODULE_COMMAND device_id=%s mismatches module=%s (dev.type=%s, dev.module=%s) — skip reconnect",
                            step.id, action_device_id, requested_module,
                            dev_check.type if dev_check else None,
                            (dev_check.info or {}).get("module") if dev_check else None,
                        )
                        skip_ensure = True
            if action_device_id and not skip_ensure:
                # HKMC/iSAP 계열: 스텝 시작 시 이미 끊겨 있으면 1회만 재연결 시도하고
                # 스텝을 실행한다. (재생 중 끊김의 2분(20s×6) 장기 재연결은 액션 실행
                # 중 ConnectionError 경로가 담당 — 매 스텝마다 2분씩 기다리지 않게 분리.)
                if self._is_hkmc_device(action_device_id):
                    await self._ensure_device_connected(action_device_id, max_retries=1)
                else:
                    await self._ensure_device_connected(action_device_id)
            t1 = time.time()

            # Execute the action
            if self._should_stop:
                step_result.status = "error"
                step_result.message = "Stopped by user"
                return step_result
            await self._run_action(step)
            t2 = time.time()

            # Module command 결과 반영 (이미지 비교가 없을 때만 PASS/FAIL 판정)
            if step.type == StepType.MODULE_COMMAND and hasattr(self, '_last_module_result'):
                mod_result = str(self._last_module_result)
                del self._last_module_result
                step_result.message = mod_result
                has_expected = step.expected_image or (step.compare_mode == CompareMode.MULTI_CROP and step.expected_images)
                if not has_expected and mod_result.startswith("FAIL:"):
                    step_result.status = "fail"

            # Wait (중단 가능)
            if await self._interruptible_sleep(step.delay_after_ms / 1000.0):
                step_result.status = "error"
                step_result.message = "Stopped by user"
                return step_result
            t3 = time.time()

            # 2) 이미지 비교 전: 기대이미지가 있을 때만 스크린샷 캡처
            has_expected = step.expected_image or (step.compare_mode == CompareMode.MULTI_CROP and step.expected_images)
            ss_device = self._resolve_screenshot_device(step) if has_expected else None
            # screenshot_device_id 가 명시됐는데 해당 디바이스를 못 찾은 경우
            # (_resolve_screenshot_device 가 None 반환) — 임의 폴백 대신 명확한 에러 반환.
            if has_expected and ss_device is None and step.screenshot_device_id:
                step_result.status = "error"
                step_result.message = (
                    f"Screenshot device '{step.screenshot_device_id}' not found — "
                    f"cannot capture for comparison"
                )
                return step_result
            if ss_device:
                if self._is_hkmc_device(ss_device["id"]):
                    await self._ensure_device_connected(ss_device["id"], max_retries=1)
                else:
                    await self._ensure_device_connected(ss_device["id"])
            t4 = time.time()
            actual_path = None
            if ss_device:
                # 스크린샷을 results 런 폴더에 직접 저장
                if self._run_output_dir and self._run_output_dir.exists():
                    # 그룹 재생: {run_dir}/{scenario_name}/screenshots/
                    # 단일 재생: {run_dir}/screenshots/
                    if not self._run_output_dir_owned:
                        safe_sc = re.sub(r'[\\/:*?"<>|→]', '_', scenario_name).replace(" ", "_")
                        _gsi = self._group_scenario_index
                        prefix = f"{_gsi:02d}_" if _gsi > 0 else ""
                        actual_dir = self._run_output_dir / f"{prefix}{safe_sc}" / "screenshots"
                    else:
                        actual_dir = self._run_output_dir / "screenshots"
                else:
                    actual_subdir = f"actual_{self._result_timestamp}" if self._result_timestamp else "actual"
                    actual_dir = SCREENSHOTS_DIR / scenario_name / actual_subdir
                actual_dir.mkdir(parents=True, exist_ok=True)
                actual_path = str(actual_dir / f"{file_prefix}.png")

                if ss_device["type"] == "adb":
                    # screen_type이 숫자면 display_id로 사용
                    adb_did = None
                    _st = ss_device.get("screen_type")
                    if _st is not None:
                        try:
                            adb_did = int(_st)
                        except (ValueError, TypeError):
                            pass
                    # SF display ID 조회
                    sf_did = None
                    if adb_did is not None:
                        dev_obj = self.dm.get_device(ss_device["id"])
                        if dev_obj:
                            from .adb_service import resolve_sf_display_id
                            sf_did = resolve_sf_display_id(dev_obj.info, adb_did)
                    adb_serial = ss_device.get("serial") or ss_device["id"]
                    logger.debug("Screenshot capture: device=%s adb_did=%s sf_did=%s",
                                 ss_device["id"], adb_did, sf_did)
                    await self.adb.screencap(actual_path, serial=adb_serial, sf_display_id=sf_did)
                elif ss_device["type"] == "isap_agent":
                    isap_svc = self.dm.get_isap_service(ss_device["id"])
                    if isap_svc:
                        img_bytes = await isap_svc.async_screencap_bytes(
                            screen_type=ss_device.get("screen_type", "front_center"), fmt="png"
                        )
                        with open(actual_path, "wb") as f:
                            f.write(img_bytes)
                    else:
                        raise RuntimeError(f"iSAP device {ss_device['id']} not connected")
                elif ss_device["type"] == "hkmc_agent":
                    hkmc_svc = self.dm.get_hkmc_service(ss_device["id"])
                    if hkmc_svc:
                        img_bytes = await hkmc_svc.async_screencap_bytes(
                            screen_type=ss_device.get("screen_type", "front_center"), fmt="png"
                        )
                        Path(actual_path).write_bytes(img_bytes)
                    else:
                        raise RuntimeError(f"HKMC device {ss_device['id']} not connected")
                elif ss_device["type"] == "hkmc5th_wide_agent":
                    hkmc5_svc = self.dm.get_hkmc5th_wide_service(ss_device["id"])
                    if hkmc5_svc:
                        img_bytes = await hkmc5_svc.async_screencap_bytes(
                            screen_type=ss_device.get("screen_type", "front_center"), fmt="png"
                        )
                        Path(actual_path).write_bytes(img_bytes)
                    else:
                        raise RuntimeError(f"HKMC5thWide device {ss_device['id']} not connected")
                elif ss_device["type"] == "icas_agent":
                    icas_svc = self.dm.get_icas_service(ss_device["id"])
                    if icas_svc:
                        img_bytes = await icas_svc.async_screencap_bytes(
                            screen_type=ss_device.get("screen_type", "HU"), fmt="png"
                        )
                        Path(actual_path).write_bytes(img_bytes)
                    else:
                        raise RuntimeError(f"ICAS device {ss_device['id']} not connected")
                elif ss_device["type"] == "mib_agent":
                    mib_svc = self.dm.get_mib_service(ss_device["id"])
                    if mib_svc:
                        img_bytes = await mib_svc.async_screencap_bytes(
                            screen_type=ss_device.get("screen_type", "HU"), fmt="png"
                        )
                        Path(actual_path).write_bytes(img_bytes)
                    else:
                        raise RuntimeError(f"MIB device {ss_device['id']} not connected")
                elif ss_device["type"] == "vision_camera":
                    cam = self.dm.get_vision_camera(ss_device["id"])
                    if cam:
                        loop = asyncio.get_event_loop()
                        saved = await loop.run_in_executor(
                            None, cam.CaptureToFile, actual_path
                        )
                elif ss_device["type"] == "webcam":
                    cam = self.dm.get_webcam_device(ss_device["id"])
                    if cam:
                        loop = asyncio.get_event_loop()
                        await loop.run_in_executor(
                            None, cam.CaptureToFile, actual_path
                        )
                    else:
                        raise RuntimeError(f"Webcam device {ss_device['id']} not connected")
                elif ss_device["type"] == "wincontrol":
                    wc = self.dm.get_wincontrol_service()
                    proc_name = str(step.params.get("process_name", "") or "")
                    exe_path = str(step.params.get("exe_path", "") or "")
                    title_pattern = str(step.params.get("window_title", "") or "")
                    class_name = str(step.params.get("window_class", "") or "")
                    aumid = str(step.params.get("process_aumid", "") or "")
                    target_w = int(step.params.get("window_width", 0) or 0)
                    target_h = int(step.params.get("window_height", 0) or 0)
                    has_proc_info = bool(proc_name or exe_path or title_pattern or aumid)
                    loop = asyncio.get_event_loop()
                    if has_proc_info:
                        # 프로세스 정보 있으면: 임베드 상태 변경 없이 대상 프로세스 윈도우만 직접 캡처.
                        # 사용자가 다른 윈도우를 임베드/조작 중이어도 검증은 step 의 프로세스에서 수행.
                        try:
                            img_bytes = await loop.run_in_executor(
                                None,
                                functools.partial(
                                    wc.capture_window_by_match,
                                    process_name=proc_name, exe_path=exe_path,
                                    title_pattern=title_pattern, class_name=class_name,
                                    aumid=aumid,
                                    target_width=target_w, target_height=target_h,
                                    fmt="png",
                                    launch_if_missing=True,
                                    wait_seconds=float(step.params.get("launch_wait_seconds", 5.0) or 5.0),
                                ),
                            )
                        except Exception as e:
                            raise RuntimeError(f"WinControl screenshot (by_match): {e}")
                    else:
                        # 프로세스 정보 없는 legacy 스텝: 기존 동작 (현재 attached 윈도우 캡처)
                        if not wc.is_attached():
                            try:
                                await loop.run_in_executor(
                                    None,
                                    functools.partial(
                                        wc.ensure_attached,
                                        process_name=proc_name, exe_path=exe_path,
                                        title_pattern=title_pattern, class_name=class_name,
                                        aumid=aumid,
                                        launch_if_missing=True,
                                        wait_seconds=8.0,
                                        target_width=target_w, target_height=target_h,
                                    ),
                                )
                            except Exception as e:
                                raise RuntimeError(f"WinControl screenshot: {e}")
                        img_bytes = await loop.run_in_executor(
                            None, functools.partial(wc.capture_window, "png"),
                        )
                    Path(actual_path).write_bytes(img_bytes)

                step_result.actual_image = self._rel_path(actual_path, scenario_name)

                # Verify against expected image
                if verify and has_expected:
                    mode = step.compare_mode or CompareMode.FULL
                    step_result.compare_mode = mode.value if isinstance(mode, CompareMode) else mode

                    expected_path = str(SCREENSHOTS_DIR / scenario_name / step.expected_image) if step.expected_image else ""
                    if step.expected_image:
                        step_result.expected_image = f"{scenario_name}/{step.expected_image}"
                    step_result.roi = step.roi

                    # 모든 비교/주석 경로에서 재사용할 단일 ndarray를 먼저 로드.
                    # 실패 스텝당 기존 3~5회 imread → 1회로 축소.
                    act_img_ndarray = await asyncio.to_thread(safe_imread, actual_path)
                    exp_img_ndarray = None
                    if expected_path:
                        exp_img_ndarray = await asyncio.to_thread(safe_imread, expected_path)

                    if mode == CompareMode.MULTI_CROP:
                        # --- Multi-crop mode ---
                        crop_items = [
                            {
                                "image": str(SCREENSHOTS_DIR / scenario_name / ci.image),
                                "rel_path": f"{scenario_name}/{ci.image}",
                                "label": ci.label,
                                "roi": ci.roi.model_dump() if ci.roi else None,
                            }
                            for ci in step.expected_images
                        ]
                        judgement = await asyncio.to_thread(
                            self.image_compare.judge,
                            expected_path="",
                            actual_path=actual_path,
                            threshold_pass=step.similarity_threshold,
                            compare_mode="multi_crop",
                            crop_items=crop_items,
                            img_act=act_img_ndarray,
                        )
                        step_result.status = judgement["status"]
                        step_result.sub_results = [SubResult(**sr) for sr in judgement.get("sub_results", [])]

                        if judgement["status"] == "error":
                            step_result.message = judgement.get("message", "Multi-crop comparison error")
                        else:
                            # Generate annotated image with all match boxes
                            try:
                                annotated_path = str(actual_dir / f"{file_prefix}_annotated.png")
                                await asyncio.to_thread(
                                    self.image_compare.generate_multi_crop_annotated,
                                    actual_path, judgement.get("sub_results", []), annotated_path,
                                    act_img_ndarray,
                                )
                                step_result.actual_annotated_image = self._rel_path(annotated_path, scenario_name)
                            except Exception as e:
                                logger.warning("Failed to generate multi-crop annotated image: %s", e)

                            # Generate annotated expected image: only crop regions visible, rest darkened
                            if exp_img_ndarray is not None:
                                def _build_multi_crop_expected_annotated():
                                    import cv2
                                    dark = (exp_img_ndarray * 0.2).astype("uint8")
                                    for ci in step.expected_images:
                                        if ci.roi:
                                            r = ci.roi
                                            dark[r.y:r.y + r.height, r.x:r.x + r.width] = exp_img_ndarray[r.y:r.y + r.height, r.x:r.x + r.width]
                                            cv2.rectangle(dark, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 255, 0), 2)
                                    exp_ann_path = str(actual_dir / f"{file_prefix}_expected_annotated.png")
                                    safe_imwrite(exp_ann_path, dark)
                                    return exp_ann_path
                                try:
                                    built = await asyncio.to_thread(_build_multi_crop_expected_annotated)
                                    if built:
                                        step_result.expected_annotated_image = self._rel_path(built, scenario_name)
                                except Exception as e:
                                    logger.warning("Failed to generate multi-crop expected annotated: %s", e)

                            parts = [f"{sr.label or f'#{i+1}'}:{sr.status}({sr.score:.2f})" for i, sr in enumerate(step_result.sub_results)]
                            step_result.message = f"Multi-crop: {', '.join(parts)}"

                    elif mode == CompareMode.FULL_EXCLUDE:
                        # --- Full-exclude mode ---
                        exclude_rois_dicts = [r.model_dump() for r in step.exclude_rois]
                        judgement = await asyncio.to_thread(
                            self.image_compare.judge,
                            expected_path,
                            actual_path,
                            threshold_pass=step.similarity_threshold,
                            compare_mode="full_exclude",
                            exclude_rois=exclude_rois_dicts,
                            img_exp=exp_img_ndarray,
                            img_act=act_img_ndarray,
                        )
                        step_result.status = judgement["status"]
                        step_result.similarity_score = judgement["score"]
                        _diff_array = judgement.get("diff_array")  # 재사용용 SSIM diff

                        if judgement["status"] == "error":
                            step_result.message = judgement.get("message", "Exclude comparison error")
                        else:
                            def _build_exclude_actual_annotated():
                                import cv2
                                if act_img_ndarray is None:
                                    return False
                                img_annotated = act_img_ndarray.copy()
                                overlay = img_annotated.copy()
                                for r in step.exclude_rois:
                                    cv2.rectangle(overlay, (r.x, r.y), (r.x + r.width, r.y + r.height), (128, 128, 128), -1)
                                cv2.addWeighted(overlay, 0.5, img_annotated, 0.5, 0, img_annotated)
                                for r in step.exclude_rois:
                                    cv2.rectangle(img_annotated, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 0, 255), 2)
                                annotated_path = str(actual_dir / f"{file_prefix}_annotated.png")
                                safe_imwrite(annotated_path, img_annotated)
                                return True
                            try:
                                if await asyncio.to_thread(_build_exclude_actual_annotated):
                                    step_result.actual_annotated_image = self._rel_path(str(actual_dir / f"{file_prefix}_annotated.png"), scenario_name)
                            except Exception as e:
                                logger.warning("Failed to generate exclude annotated image: %s", e)

                            if step_result.status != "pass":
                                diff_path = str(actual_dir / f"diff_{file_prefix}.png")
                                diff_rel = self._rel_path(diff_path, scenario_name)
                                try:
                                    await asyncio.to_thread(
                                        self.image_compare.generate_diff_heatmap,
                                        expected_path, actual_path, diff_path,
                                        None, exclude_rois_dicts,
                                        exp_img_ndarray, act_img_ndarray, _diff_array,
                                    )
                                    step_result.diff_image = diff_rel
                                except Exception as e:
                                    logger.warning("Failed to generate diff: %s", e)

                            if exp_img_ndarray is not None:
                                def _build_exclude_expected_annotated():
                                    import cv2
                                    overlay = exp_img_ndarray.copy()
                                    for r in step.exclude_rois:
                                        cv2.rectangle(overlay, (r.x, r.y), (r.x + r.width, r.y + r.height), (128, 128, 128), -1)
                                    cv2.addWeighted(overlay, 0.5, exp_img_ndarray, 0.5, 0, overlay)
                                    for r in step.exclude_rois:
                                        cv2.rectangle(overlay, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 0, 255), 2)
                                    exp_ann_path = str(actual_dir / f"{file_prefix}_expected_annotated.png")
                                    safe_imwrite(exp_ann_path, overlay)
                                    return True
                                try:
                                    if await asyncio.to_thread(_build_exclude_expected_annotated):
                                        step_result.expected_annotated_image = self._rel_path(str(actual_dir / f"{file_prefix}_expected_annotated.png"), scenario_name)
                                except Exception as e:
                                    logger.warning("Failed to generate exclude expected annotated: %s", e)

                            step_result.message = f"Exclude {len(step.exclude_rois)} regions: {judgement['score']:.4f}"

                    elif mode == CompareMode.MATCH_CROP:
                        # --- Match-crop mode: 위치 무관 template matching ---
                        # expected_path 는 단일크롭과 동일 — 녹화 시 잘라 둔 작은 PNG.
                        # actual 전체에서 expected 를 찾아 score(confidence)와 match_location 반환.
                        judgement = await asyncio.to_thread(
                            self.image_compare.judge,
                            expected_path,
                            actual_path,
                            threshold_pass=step.similarity_threshold,
                            compare_mode="match_crop",
                            img_exp=exp_img_ndarray,
                            img_act=act_img_ndarray,
                        )
                        step_result.status = judgement["status"]
                        step_result.similarity_score = judgement["score"]
                        _diff_array = judgement.get("diff_array")

                        if judgement["status"] == "error":
                            step_result.message = judgement.get("message", "Match-crop comparison error")
                        else:
                            match_loc = judgement.get("match_location")
                            if match_loc:
                                step_result.match_location = match_loc
                                def _build_match_annotated():
                                    import cv2
                                    if act_img_ndarray is None:
                                        return False
                                    img_annotated = act_img_ndarray.copy()
                                    x, y = match_loc["x"], match_loc["y"]
                                    w, h = match_loc["width"], match_loc["height"]
                                    color = (0, 255, 0) if step_result.status == "pass" else (0, 0, 255)
                                    cv2.rectangle(img_annotated, (x, y), (x + w, y + h), color, 3)
                                    annotated_path = str(actual_dir / f"{file_prefix}_annotated.png")
                                    safe_imwrite(annotated_path, img_annotated)
                                    return True
                                try:
                                    if await asyncio.to_thread(_build_match_annotated):
                                        step_result.actual_annotated_image = self._rel_path(
                                            str(actual_dir / f"{file_prefix}_annotated.png"), scenario_name,
                                        )
                                except Exception as e:
                                    logger.warning("Failed to generate match-crop annotated image: %s", e)

                            ssim_score = judgement.get("ssim_score")
                            if ssim_score is not None:
                                step_result.message = (
                                    f"Match-crop: confidence={judgement['score']:.4f}, ssim={ssim_score:.4f}"
                                )
                            else:
                                step_result.message = f"Match-crop: confidence={judgement['score']:.4f}"

                    else:
                        # --- Full / Single-crop mode ---
                        compare_actual_path = actual_path
                        compare_actual_img = act_img_ndarray
                        if step.roi and act_img_ndarray is not None:
                            def _crop_actual_roi():
                                r = step.roi
                                ah, aw = act_img_ndarray.shape[:2]
                                # 진단: actual 풀 사이즈 + step.roi + 실제 잘린 shape +
                                # expected loaded shape 모두 한 줄로 노출. 좌표 misalignment 즉시 감지.
                                eshape = exp_img_ndarray.shape if exp_img_ndarray is not None else None
                                logger.info(
                                    "single_crop align: actual_full=%dx%d step.roi=(x=%d y=%d w=%d h=%d) "
                                    "expected_loaded_shape=%s",
                                    aw, ah, r.x, r.y, r.width, r.height, eshape,
                                )
                                if r.x < 0 or r.y < 0 or r.x + r.width > aw or r.y + r.height > ah:
                                    logger.warning(
                                        "single_crop ROI OUT OF BOUNDS: actual=%dx%d roi=(%d,%d,%dx%d)",
                                        aw, ah, r.x, r.y, r.width, r.height,
                                    )
                                cropped = act_img_ndarray[r.y:r.y + r.height, r.x:r.x + r.width]
                                if exp_img_ndarray is not None and cropped.shape != exp_img_ndarray.shape:
                                    logger.warning(
                                        "single_crop SHAPE MISMATCH: cropped_actual=%s expected=%s "
                                        "→ resize will distort comparison",
                                        cropped.shape, exp_img_ndarray.shape,
                                    )
                                cropped_path = str(actual_dir / f"{file_prefix}_roi.png")
                                safe_imwrite(cropped_path, cropped)
                                return cropped_path, cropped
                            cropped_ret = await asyncio.to_thread(_crop_actual_roi)
                            if cropped_ret:
                                compare_actual_path = cropped_ret[0]
                                compare_actual_img = cropped_ret[1]

                        judgement = await asyncio.to_thread(
                            self.image_compare.judge,
                            expected_path,
                            compare_actual_path,
                            threshold_pass=step.similarity_threshold,
                            img_exp=exp_img_ndarray,
                            img_act=compare_actual_img,
                        )
                        step_result.status = judgement["status"]
                        step_result.similarity_score = judgement["score"]
                        _diff_array = judgement.get("diff_array")

                        if judgement["status"] == "error":
                            step_result.message = judgement.get("message", "Image comparison error")
                        else:
                            match_loc = judgement.get("match_location")
                            if match_loc:
                                step_result.match_location = match_loc
                                def _build_match_annotated():
                                    import cv2
                                    if act_img_ndarray is None:
                                        return False
                                    img_annotated = act_img_ndarray.copy()
                                    x, y = match_loc["x"], match_loc["y"]
                                    w, h = match_loc["width"], match_loc["height"]
                                    cv2.rectangle(img_annotated, (x, y), (x + w, y + h), (0, 0, 255), 3)
                                    annotated_path = str(actual_dir / f"{file_prefix}_annotated.png")
                                    safe_imwrite(annotated_path, img_annotated)
                                    return True
                                try:
                                    if await asyncio.to_thread(_build_match_annotated):
                                        step_result.actual_annotated_image = self._rel_path(str(actual_dir / f"{file_prefix}_annotated.png"), scenario_name)
                                except Exception as e:
                                    logger.warning("Failed to generate annotated image: %s", e)
                            elif step.roi:
                                def _build_roi_annotated():
                                    import cv2
                                    if act_img_ndarray is None:
                                        return False
                                    img_annotated = act_img_ndarray.copy()
                                    r = step.roi
                                    cv2.rectangle(img_annotated, (r.x, r.y), (r.x + r.width, r.y + r.height), (0, 0, 255), 3)
                                    annotated_path = str(actual_dir / f"{file_prefix}_annotated.png")
                                    safe_imwrite(annotated_path, img_annotated)
                                    return True
                                try:
                                    if await asyncio.to_thread(_build_roi_annotated):
                                        step_result.actual_annotated_image = self._rel_path(str(actual_dir / f"{file_prefix}_annotated.png"), scenario_name)
                                        step_result.match_location = {"x": step.roi.x, "y": step.roi.y, "width": step.roi.width, "height": step.roi.height}
                                except Exception as e:
                                    logger.warning("Failed to generate annotated image: %s", e)

                            if step_result.status != "pass":
                                diff_path = str(actual_dir / f"diff_{file_prefix}.png")
                                diff_rel = self._rel_path(diff_path, scenario_name)
                                try:
                                    await asyncio.to_thread(
                                        self.image_compare.generate_diff_heatmap,
                                        expected_path, compare_actual_path, diff_path,
                                        None, None,
                                        exp_img_ndarray, compare_actual_img, _diff_array,
                                    )
                                    step_result.diff_image = diff_rel
                                except Exception as e:
                                    logger.warning("Failed to generate diff: %s", e)

                            sim_msg = f"Similarity: {judgement['score']:.4f}"
                            if step_result.message:
                                step_result.message = f"{step_result.message}\n{sim_msg}"
                            else:
                                step_result.message = sim_msg

                    # 이미지 ndarray 즉시 해제하여 GC가 곧바로 수거할 수 있게 한다.
                    act_img_ndarray = None
                    exp_img_ndarray = None
                else:
                    dev_label = ss_device["id"] if ss_device else step.device_id or "default"
                    if not step_result.message:
                        step_result.message = f"Executed on {dev_label} (기대 이미지 없음)"
            else:
                if not step_result.message:
                    step_result.message = f"Executed on {step.device_id or 'default'}"

        except Exception as e:
            step_result.status = "error"
            step_result.message = str(e)
            logger.error("Step %d execution error: %s", step.id, e)

        t_end = time.time()
        step_result.execution_time_ms = int((t_end - start_time) * 1000)
        logger.info(
            "Step %d timing: check1=%.1fs action=%.1fs delay=%.1fs check2=%.1fs rest=%.1fs total=%.1fs",
            step.id,
            t1 - t0, t2 - t1, t3 - t2, t4 - t3, t_end - t4, t_end - start_time,
        )
        # RAND 스텝은 random_log.txt에 별도 기록 + step 메시지 보강
        # (description이 "RAND " 로 시작하면 RAND 출처로 간주 — 프론트 randHK/SK/DRAG가 그렇게 라벨함)
        try:
            desc = (step.description or "")
            if desc.startswith("RAND "):
                self._log_random_step(scenario_name, step, step_result, repeat_index)
        except Exception as _e:
            logger.debug("random log write failed: %s", _e)
        return step_result

    def _format_random_action(self, step: Step) -> str:
        """RAND 스텝의 실제 동작을 사람이 읽기 쉬운 한 줄로 요약."""
        p = step.params or {}
        t = step.type
        if t in (StepType.HKMC_KEY, StepType.ICAS_KEY):
            kn = p.get("key_name", "")
            sub = p.get("sub_cmd", 0x43)
            sub_label = "LONG" if sub == 0x44 else "SHORT" if sub == 0x43 else f"sub=0x{int(sub):02X}"
            scr = p.get("screen_type", "")
            return f"HK key={kn} {sub_label} screen={scr}"
        if t in (StepType.HKMC_TOUCH, StepType.ICAS_TOUCH):
            return f"SK ({p.get('x',0)},{p.get('y',0)}) screen={p.get('screen_type','')}"
        if t in (StepType.HKMC_LONG_PRESS, StepType.ICAS_LONG_PRESS):
            return f"LP ({p.get('x',0)},{p.get('y',0)}) {p.get('duration_ms',3000)}ms screen={p.get('screen_type','')}"
        if t in (StepType.HKMC_SWIPE, StepType.ICAS_SWIPE):
            return f"DRAG ({p.get('x1',0)},{p.get('y1',0)})→({p.get('x2',0)},{p.get('y2',0)}) {p.get('duration_ms',0)}ms screen={p.get('screen_type','')}"
        # 일반 ADB 등도 혹시 RAND 라벨이 붙으면 동일 포맷으로
        return f"{t.value if hasattr(t, 'value') else t} {p}"

    def _log_random_step(self, scenario_name: str, step: Step,
                         step_result: StepResult, repeat_index: int) -> None:
        """RAND 출처 step의 실행 결과를 run_dir/random_log.txt 에 append.

        목적: 스트레스 테스트 시 어느 cycle/step에서 어떤 무작위 동작이
        실행됐고 결과가 무엇이었는지 추적하기 위함.
        """
        log_path = self._resolve_random_log_path(scenario_name)
        if log_path is None:
            return

        # 첫 줄에 헤더 1회 작성
        is_new = not log_path.exists()
        action_summary = self._format_random_action(step)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        line = (
            f"{ts}\tcycle={repeat_index}\tstep_id={step.id}\t"
            f"status={step_result.status}\tduration={step_result.execution_time_ms}ms\t"
            f"desc={(step.description or '').strip()}\t"
            f"action={action_summary}\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                if is_new:
                    f.write("# Random action log — timestamp / cycle / step_id / status / duration / desc / action\n")
                f.write(line)
        except Exception as e:
            logger.debug("random_log.txt write failed: %s", e)

        # step_result.message 에도 한 줄 요약 prepend (UI 결과 테이블에서 즉시 확인 가능)
        prefix = f"[RAND] {action_summary}"
        if step_result.message and step_result.message.strip():
            step_result.message = f"{prefix}\n{step_result.message}"
        else:
            step_result.message = prefix

    def _resolve_random_log_path(self, scenario_name: str):
        """run_dir 우선, 없으면 scenario_name 기반 results dir 하위의 random_log.txt 경로 반환."""
        run_dir = self._run_output_dir
        if run_dir is None or not run_dir.exists():
            run_dir = RESULTS_DIR / f"{scenario_name}_{self._result_timestamp or 'manual'}"
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                logger.debug("random_log run_dir create failed: %s", e)
                return None
        return run_dir / "random_log.txt"

    def _log_random_sub_action(self, scenario_name: str, step_id: int, repeat_index: int,
                               iteration: int, total: int,
                               action_summary: str, status: str, duration_ms: int,
                               error: str = "") -> None:
        """ALL_RANDOM step의 각 iteration별 실제 실행 동작을 random_log.txt 에 append.

        iteration: 1-based 인덱스, total: 총 반복 횟수.
        """
        log_path = self._resolve_random_log_path(scenario_name)
        if log_path is None:
            return
        is_new = not log_path.exists()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        err_part = f"\terror={error}" if error else ""
        line = (
            f"{ts}\tcycle={repeat_index}\tstep_id={step_id}\titer={iteration}/{total}\t"
            f"status={status}\tduration={duration_ms}ms\taction={action_summary}{err_part}\n"
        )
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                if is_new:
                    f.write("# Random action log — timestamp / cycle / step_id / iter / status / duration / action\n")
                f.write(line)
        except Exception as e:
            logger.debug("random_log.txt write failed: %s", e)

    @staticmethod
    def _format_command(step: Step) -> str:
        """Format a human-readable command description for the step."""
        p = step.params
        if step.type == StepType.TAP:
            return f"tap ({p.get('x', 0)}, {p.get('y', 0)})"
        elif step.type == StepType.IMAGE_TAP:
            tpl = p.get("template", "")
            sim = float(p.get("similarity", 0.85))
            if p.get("long_press"):
                return f"image_long_press [{tpl}] @sim≥{sim:.2f} {p.get('duration_ms', 3000)}ms"
            return f"image_tap [{tpl}] @sim≥{sim:.2f}"
        elif step.type == StepType.REPEAT_TAP:
            return f"repeat_tap ({p.get('x', 0)}, {p.get('y', 0)}) ×{p.get('count', 5)} @{p.get('interval_ms', 100)}ms"
        elif step.type == StepType.LONG_PRESS:
            return f"long_press ({p.get('x', 0)}, {p.get('y', 0)}) {p.get('duration_ms', 1000)}ms"
        elif step.type == StepType.SWIPE:
            pts = p.get("points") or []
            if isinstance(pts, list) and len(pts) >= 2:
                path = "→".join(f"({pt.get('x',0)},{pt.get('y',0)})" for pt in pts)
                return f"pattern_swipe {path} {p.get('duration_ms', 600)}ms"
            return f"swipe ({p.get('x1',0)},{p.get('y1',0)})→({p.get('x2',0)},{p.get('y2',0)})"
        elif step.type == StepType.INPUT_TEXT:
            return f"input_text \"{p.get('text', '')}\""
        elif step.type == StepType.KEY_EVENT:
            return f"key {p.get('keycode', '')}"
        elif step.type == StepType.WAIT:
            return f"wait {p.get('duration_ms', 1000)}ms"
        elif step.type == StepType.ADB_COMMAND:
            return f"adb {p.get('command', '')}"
        elif step.type == StepType.SERIAL_COMMAND:
            return f"serial \"{p.get('data', '')}\""
        elif step.type == StepType.MODULE_COMMAND:
            # 인자값을 함께 표시 — 어떤 명령을 실행했는지 한눈에 보이도록.
            # 예: CMD::Check(ipconfig), SSH::Send(reboot now), Plugin::Foo(a=1, b=2)
            mod = p.get('module', '')
            fn = p.get('function', '')
            args = p.get('args', {})
            if isinstance(args, dict) and args:
                # 단일 인자: 값만, 다인자: key=value (가독성)
                if len(args) == 1:
                    args_str = str(next(iter(args.values())))
                else:
                    args_str = ", ".join(f"{k}={v}" for k, v in args.items())
            elif isinstance(args, (list, tuple)) and args:
                args_str = ", ".join(str(v) for v in args)
            else:
                args_str = ""
            # 너무 긴 인자는 잘라서 표시 (출력값은 message에 따로 보존됨)
            if len(args_str) > 120:
                args_str = args_str[:117] + "..."
            return f"{mod}::{fn}({args_str})"
        elif step.type == StepType.HKMC_TOUCH:
            st = step.screen_type or p.get("screen_type", "")
            return f"hkmc_touch ({p.get('x', 0)}, {p.get('y', 0)}) [{st}]"
        elif step.type == StepType.HKMC_SWIPE:
            st = step.screen_type or p.get("screen_type", "")
            return f"hkmc_swipe ({p.get('x1',0)},{p.get('y1',0)})→({p.get('x2',0)},{p.get('y2',0)}) [{st}]"
        elif step.type == StepType.HKMC_KEY:
            key = p.get("key_name", f"0x{p.get('key_data', 0):02X}")
            return f"hkmc_key {key}"
        elif step.type == StepType.HKMC_LONG_PRESS:
            st = step.screen_type or p.get("screen_type", "")
            return f"hkmc_long_press ({p.get('x', 0)}, {p.get('y', 0)}) {p.get('duration_ms', 3000)}ms [{st}]"
        elif step.type == StepType.ICAS_TOUCH:
            st = step.screen_type or p.get("screen_type", "")
            return f"icas_touch ({p.get('x', 0)}, {p.get('y', 0)}) [{st}]"
        elif step.type == StepType.ICAS_SWIPE:
            st = step.screen_type or p.get("screen_type", "")
            return f"icas_swipe ({p.get('x1',0)},{p.get('y1',0)})→({p.get('x2',0)},{p.get('y2',0)}) [{st}]"
        elif step.type == StepType.ICAS_KEY:
            key = p.get("key_name", f"0x{p.get('key_data', 0):02X}")
            return f"icas_key {key}"
        elif step.type == StepType.ICAS_LONG_PRESS:
            st = step.screen_type or p.get("screen_type", "")
            return f"icas_long_press ({p.get('x', 0)}, {p.get('y', 0)}) {p.get('duration_ms', 3000)}ms [{st}]"
        elif step.type == StepType.ALL_RANDOM:
            rc = int(p.get("repeat_count", 1))
            iv = int(p.get("interval_ms", 0))
            return f"all_random ×{rc} @{iv}ms"
        elif step.type == StepType.WIN_TAP:
            tgt = p.get("process_name") or p.get("window_title") or ""
            btn = p.get("button", "left")
            btn_tag = "" if btn == "left" else f" [{btn}]"
            return f"win_tap{btn_tag} ({p.get('x', 0)}, {p.get('y', 0)}) [{tgt}]"
        elif step.type == StepType.WIN_DOUBLE_CLICK:
            tgt = p.get("process_name") or p.get("window_title") or ""
            return f"win_double_click ({p.get('x', 0)}, {p.get('y', 0)}) [{tgt}]"
        elif step.type == StepType.WIN_CLICK_SEQUENCE:
            tgt = p.get("process_name") or p.get("window_title") or ""
            pts = p.get("points") or []
            coords = " → ".join(f"({pt.get('x', 0)},{pt.get('y', 0)})" for pt in pts) or "(empty)"
            return f"win_click_sequence {coords} @{p.get('interval_ms', 150)}ms [{tgt}]"
        elif step.type == StepType.WIN_LONG_PRESS:
            tgt = p.get("process_name") or p.get("window_title") or ""
            btn = p.get("button", "left")
            btn_tag = "" if btn == "left" else f" [{btn}]"
            return f"win_long_press{btn_tag} ({p.get('x', 0)}, {p.get('y', 0)}) {p.get('duration_ms', 500)}ms [{tgt}]"
        elif step.type == StepType.WIN_SWIPE:
            tgt = p.get("process_name") or p.get("window_title") or ""
            return f"win_swipe ({p.get('x1', 0)},{p.get('y1', 0)})→({p.get('x2', 0)},{p.get('y2', 0)}) [{tgt}]"
        elif step.type == StepType.WIN_INPUT_TEXT:
            txt = p.get("text", "")
            return f"win_input_text \"{txt[:30]}{'...' if len(txt) > 30 else ''}\""
        elif step.type == StepType.WIN_KEY:
            return f"win_key {p.get('key', '')}"
        elif step.type == StepType.WIN_KEY_COMBO:
            seq = p.get("combo_seq")
            if isinstance(seq, list) and seq:
                keys_str = " → ".join(str(c) for c in seq)
            else:
                keys = p.get("keys") if "keys" in p else p.get("combo", "")
                if isinstance(keys, list):
                    keys_str = "+".join(str(k) for k in keys)
                else:
                    keys_str = str(keys)
            cfx = p.get("click_first_x")
            cfy = p.get("click_first_y")
            at = f" @({cfx},{cfy})" if cfx is not None and cfy is not None else ""
            return f"win_key_combo {keys_str}{at}"
        return step.type.value

    async def _force_reconnect_hkmc(self, device_id: str) -> bool:
        """HKMC/iSAP 디바이스를 강제 재연결 (stale is_connected 플래그 극복용).

        `_ensure_device_connected` 는 `is_connected == True` 면 재연결을 스킵하는데,
        TCP 소켓은 죽었지만 `_connected` 플래그만 True 로 남은 zombie 상태에서는
        스킵이 오히려 버그를 은폐한다. 여기서는 기존 서비스가 있든 없든 무조건
        disconnect → 새 서비스 → connect 시퀀스를 실행한다.

        Returns:
            True 면 재연결 성공, False 면 실패 (디바이스 미존재 / port 미설정 / connect 실패).
        """
        dev = self.dm.get_device(device_id)
        if not dev:
            logger.error("[HKMC RECONNECT] device %s not found in dm", device_id)
            return False
        port = dev.info.get("port", 0)
        if not port:
            logger.error("[HKMC RECONNECT] device %s has no port in dev.info", device_id)
            return False
        lock = self.dm.get_reconnect_lock(device_id)
        async with lock:
            existing = self.dm._hkmc_conns.get(dev.id) if dev.type == "hkmc_agent" else self.dm._isap_conns.get(dev.id)
            if existing:
                try:
                    await existing.async_disconnect()
                except Exception as e:
                    logger.debug("[HKMC RECONNECT] disconnect failed (ignored): %s", e)
            try:
                if dev.type == "hkmc_agent":
                    from .hkmc6th_service import HKMC6thService
                    svc = HKMC6thService(dev.address, port, device_id=dev.id,
                                          key_overrides=dev.info.get("hkmc_keys"),
                                          device_model=dev.info.get("device_model", ""),
                                          ssh_username=dev.info.get("ssh_username", ""),
                                          ssh_password=dev.info.get("ssh_password", ""),
                                          ssh_port=int(dev.info.get("ssh_port", 10022) or 10022),
                                          cluster_resolution=dev.info.get("cluster_resolution", "2720x720"),
                                          cluster_display=str(dev.info.get("cluster_display", "1") or "1"),
                                          cluster_overlay_display=str(dev.info.get("cluster_overlay_display", "") or ""),
                                          cluster_composite_mode=str(dev.info.get("cluster_composite_mode", "off") or "off"),
                                          cluster_overlay_key_color=str(dev.info.get("cluster_overlay_key_color", "0,0,0") or "0,0,0"),
                                          cluster_overlay_threshold=int(dev.info.get("cluster_overlay_threshold", 24) or 24),
                                          cluster_composite_live=bool(dev.info.get("cluster_composite_live", True)),
                                          cluster_crop=str(dev.info.get("cluster_crop", "") or ""))
                    ok = await svc.async_connect()
                    if ok:
                        self.dm._hkmc_conns[dev.id] = svc
                        dev.status = "connected"
                        dev.info["agent_version"] = svc.agent_version
                        try:
                            dev.info["screens"] = svc.get_info()["screens"]
                        except Exception:
                            pass
                        logger.info("[HKMC RECONNECT] reconnected %s (address=%s port=%s)",
                                    dev.id, dev.address, port)
                        return True
                elif dev.type == "isap_agent":
                    from .isap_agent_service import ISAPAgentService
                    svc = ISAPAgentService(dev.address, port, device_id=dev.id,
                                           key_overrides=dev.info.get("isap_keys"))
                    ok = await svc.async_connect()
                    if ok:
                        self.dm._isap_conns[dev.id] = svc
                        dev.status = "connected"
                        dev.info["agent_version"] = svc.agent_version
                        try:
                            dev.info["screens"] = svc.get_info()["screens"]
                        except Exception:
                            pass
                        logger.info("[HKMC RECONNECT] iSAP reconnected %s", dev.id)
                        return True
            except Exception as e:
                logger.error("[HKMC RECONNECT] connect failed for %s: %s", dev.id, e)
                return False
        logger.error("[HKMC RECONNECT] reconnect returned False for %s", dev.id)
        return False

    async def _ensure_device_connected(self, device_id: str, max_retries: int = 24, retry_interval: float = 5.0) -> None:
        """특정 디바이스의 연결 상태 확인 + 끊어진 경우 재연결 시도.

        Args:
            device_id: 실제 디바이스 ID (alias가 아닌 resolve된 ID)
        """
        if not device_id:
            return
        dev = self.dm.get_device(device_id)
        if not dev:
            return

        if dev.type == "hkmc_agent":
            hkmc = self.dm.get_hkmc_service(device_id)
            if hkmc and hkmc.is_connected:
                return
            port = dev.info.get("port", 0)
            if not port:
                return
            from .hkmc6th_service import HKMC6thService
            # device_manager와 동일한 재연결 락으로 직렬화 — race condition 제거.
            # 잠긴 동안 monitor 루프가 같은 디바이스를 건드리지 못함.
            lock = self.dm.get_reconnect_lock(device_id)
            async with lock:
                # 락 획득 후 재검사: 다른 경로가 이미 성공시켰을 수 있음
                hkmc = self.dm.get_hkmc_service(device_id)
                if hkmc and hkmc.is_connected:
                    return
                for attempt in range(1, max_retries + 1):
                    if self._should_stop:
                        return
                    logger.info("Playback: reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                    try:
                        hkmc = self.dm.get_hkmc_service(device_id)
                        if hkmc:
                            # disconnect()는 내부적으로 recv_thread.join(timeout=3)을 호출하는 blocking 호출.
                            # 직접 호출하면 event loop를 최대 3초 블록 → uvicorn WS ping 예산을 까먹음.
                            await hkmc.async_disconnect()
                        svc = HKMC6thService(dev.address, port, device_id=dev.id,
                                               key_overrides=dev.info.get("hkmc_keys"),
                                               device_model=dev.info.get("device_model", ""),
                                               ssh_username=dev.info.get("ssh_username", ""),
                                               ssh_password=dev.info.get("ssh_password", ""),
                                               ssh_port=int(dev.info.get("ssh_port", 10022) or 10022),
                                               cluster_resolution=dev.info.get("cluster_resolution", "2720x720"),
                                               cluster_display=str(dev.info.get("cluster_display", "1") or "1"),
                                               cluster_overlay_display=str(dev.info.get("cluster_overlay_display", "") or ""),
                                               cluster_composite_mode=str(dev.info.get("cluster_composite_mode", "off") or "off"),
                                               cluster_overlay_key_color=str(dev.info.get("cluster_overlay_key_color", "0,0,0") or "0,0,0"),
                                               cluster_overlay_threshold=int(dev.info.get("cluster_overlay_threshold", 24) or 24),
                                               cluster_composite_live=bool(dev.info.get("cluster_composite_live", True)),
                                               cluster_crop=str(dev.info.get("cluster_crop", "") or ""))
                        ok = await svc.async_connect()
                        if ok:
                            self.dm._hkmc_conns[dev.id] = svc
                            self.dm._hkmc_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            logger.info("Playback: reconnected %s", device_id)
                            return
                    except Exception as e:
                        logger.debug("Playback: reconnect %s failed: %s", device_id, e)
                    if attempt < max_retries:
                        if await self._interruptible_sleep(retry_interval):
                            return
                dev.status = "disconnected"

        elif dev.type == "hkmc5th_wide_agent":
            hkmc5 = self.dm.get_hkmc5th_wide_service(device_id)
            if hkmc5 and hkmc5.is_connected:
                return
            port = dev.info.get("port", 0)
            if not port:
                return
            from .hkmc5th_wide_service import HKMC5thWideService
            lock = self.dm.get_reconnect_lock(device_id)
            async with lock:
                hkmc5 = self.dm.get_hkmc5th_wide_service(device_id)
                if hkmc5 and hkmc5.is_connected:
                    return
                for attempt in range(1, max_retries + 1):
                    if self._should_stop:
                        return
                    logger.info("Playback: HKMC5thWide reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                    try:
                        hkmc5 = self.dm.get_hkmc5th_wide_service(device_id)
                        if hkmc5:
                            await hkmc5.async_disconnect()
                        svc = HKMC5thWideService(dev.address, port, device_id=dev.id,
                                                 key_overrides=dev.info.get("HKMC5TH_WIDE_KEYS"),
                                                 device_model=dev.info.get("device_model", ""))
                        ok = await svc.async_connect()
                        if ok:
                            self.dm._hkmc5th_wide_conns[dev.id] = svc
                            self.dm._hkmc5th_wide_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            logger.info("Playback: HKMC5thWide reconnected %s", device_id)
                            return
                    except Exception as e:
                        logger.debug("Playback: HKMC5thWide reconnect %s failed: %s", device_id, e)
                    if attempt < max_retries:
                        if await self._interruptible_sleep(retry_interval):
                            return
                dev.status = "disconnected"

        elif dev.type == "isap_agent":
            isap = self.dm.get_isap_service(device_id)
            if isap and isap.is_connected:
                return
            port = dev.info.get("port", 0)
            if not port:
                return
            from .isap_agent_service import ISAPAgentService
            lock = self.dm.get_reconnect_lock(device_id)
            async with lock:
                isap = self.dm.get_isap_service(device_id)
                if isap and isap.is_connected:
                    return
                for attempt in range(1, max_retries + 1):
                    if self._should_stop:
                        return
                    logger.info("Playback: iSAP reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                    try:
                        isap = self.dm.get_isap_service(device_id)
                        if isap:
                            await isap.async_disconnect()
                        svc = ISAPAgentService(dev.address, port, device_id=dev.id,
                                               key_overrides=dev.info.get("isap_keys"))
                        ok = await svc.async_connect()
                        if ok:
                            self.dm._isap_conns[dev.id] = svc
                            self.dm._isap_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            logger.info("Playback: iSAP reconnected %s", device_id)
                            return
                    except Exception as e:
                        logger.debug("Playback: iSAP reconnect %s failed: %s", device_id, e)
                    if attempt < max_retries:
                        if await self._interruptible_sleep(retry_interval):
                            return
                dev.status = "disconnected"

        elif dev.type == "icas_agent":
            icas = self.dm.get_icas_service(device_id)
            if icas and icas.is_connected:
                return
            from .icas_agent_service import ICASAgentService
            lock = self.dm.get_reconnect_lock(device_id)
            async with lock:
                icas = self.dm.get_icas_service(device_id)
                if icas and icas.is_connected:
                    return
                for attempt in range(1, max_retries + 1):
                    if self._should_stop:
                        return
                    logger.info("Playback: ICAS reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                    try:
                        _dm_upper = (dev.info.get("device_model") or "").upper()
                        _variant = "icas3" if "ICAS3" in _dm_upper else "icas"
                        svc = ICASAgentService(
                            dev.address,
                            port=int(dev.info.get("port", 22) or 22),
                            device_id=dev.id,
                            username=dev.info.get("username", "root") or "root",
                            password=dev.info.get("password", "") or "",
                            resolution=dev.info.get("resolution", "1560x700") or "1560x700",
                            private_server_ip=dev.info.get("private_server_ip", "192.168.0.2") or "192.168.0.2",
                            private_server_password=dev.info.get("private_server_password", "") or "",
                            iid_display=dev.info.get("iid_display", "10") or "10",
                            hud_display=dev.info.get("hud_display", "11") or "11",
                            variant=_variant,
                            key_overrides=dev.info.get("icas_keys"),
                        )
                        ok = await svc.async_connect()
                        if ok:
                            self.dm._icas_conns[dev.id] = svc
                            self.dm._icas_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            logger.info("Playback: ICAS reconnected %s", device_id)
                            return
                    except Exception as e:
                        logger.debug("Playback: ICAS reconnect %s failed: %s", device_id, e)
                    if attempt < max_retries:
                        if await self._interruptible_sleep(retry_interval):
                            return
                dev.status = "disconnected"

        elif dev.type == "mib_agent":
            mib = self.dm.get_mib_service(device_id)
            if mib and mib.is_connected:
                return
            # MIB 재연결은 device_manager.connect_device_by_id를 재사용 — 콜백/저장된 ksend_src/dst 등 모두 적용.
            lock = self.dm.get_reconnect_lock(device_id)
            async with lock:
                mib = self.dm.get_mib_service(device_id)
                if mib and mib.is_connected:
                    return
                for attempt in range(1, max_retries + 1):
                    if self._should_stop:
                        return
                    logger.info("Playback: MIB reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                    try:
                        msg = await self.dm.connect_device_by_id(device_id)
                        if "connected" in msg.lower() and "failed" not in msg.lower():
                            logger.info("Playback: MIB reconnected %s", device_id)
                            return
                    except Exception as e:
                        logger.debug("Playback: MIB reconnect %s failed: %s", device_id, e)
                    if attempt < max_retries:
                        if await self._interruptible_sleep(retry_interval):
                            return
                dev.status = "disconnected"

        elif dev.type == "adb":
            # 먼저 현재 상태 확인
            try:
                adb_devices = await self.adb.list_devices()
                found = next((d for d in adb_devices if d.serial == dev.address), None)
                if found and found.status == "device":
                    dev.status = "device"
                    self.dm.reset_reconnect_attempts(device_id)
                    return
            except Exception:
                pass

            # 연결 안 됨 → 재연결 대기 (백그라운드 루프가 상태 갱신 중)
            adb_serial = dev.address
            self.dm.reset_reconnect_attempts(device_id)
            for attempt in range(1, max_retries + 1):
                if self._should_stop:
                    return
                logger.info("Playback: ADB reconnect %s attempt %d/%d", device_id, attempt, max_retries)
                try:
                    adb_devices = await self.adb.list_devices()
                    found = next((d for d in adb_devices if d.serial == adb_serial), None)
                    if found and found.status == "device":
                        dev.status = "device"
                        self.dm.reset_reconnect_attempts(device_id)
                        logger.info("Playback: ADB reconnected %s", device_id)
                        return
                    # 'device' 상태가 아님(offline 또는 목록에 없음) → 해당 디바이스만 타겟 재연결.
                    # ⚠️ adb kill-server 금지: 호스트 adb 서버를 통째로 죽이면 같은 호스트에서
                    # 돌고 있는 다른 adb 연결까지 끊긴다. 특히 TH 로컬 CVD(0.0.0.0:6520) 위에서
                    # `adb shell` 로 떠 있는 grpc_*_gateway 프로세스가 트랜스포트를 잃고 죽어
                    # 브로커 토픽이 사라진다 → DUT 전원 OFF→ON 시나리오에서 이 경로(예: 디바이스가
                    # 꺼진 채 Android Monitor 스텝이 ensure 를 호출)가 트리거되어 이후 TH 신호가
                    # "Topic not found", RBVM 직접 adb 명령이 전부 실패하던 근본 원인.
                    # 원본 ensure-adb.sh 도 kill-server 대신 'adb connect' 만 사용.
                    if ":" in adb_serial:
                        # 네트워크 타겟(host:port): DUT 전원 OFF→ON 후 RBVM(예 192.168.140.1:5555)이
                        # 'offline' 스테일로 남는 경우가 많다 → 끊고 다시 connect 해야 'device' 로 복구.
                        logger.info("Playback: ADB net reconnect %s (%s, status=%s)",
                                    device_id, adb_serial, found.status if found else "absent")
                        try:
                            if found:
                                await self.adb.disconnect_device(adb_serial)
                            await self.adb.connect_device(adb_serial)
                        except Exception:
                            pass
                    elif found:
                        # USB 디바이스가 offline/unauthorized 등 → 글로벌 리셋 없이 대기(백그라운드 갱신).
                        logger.info("Playback: ADB %s status=%s, waiting...", device_id, found.status)
                    else:
                        # USB 디바이스가 목록에 없음 → offline 트랜스포트만 재연결 (서버 유지).
                        logger.info("Playback: ADB usb reconnect-offline for %s", device_id)
                        try:
                            await self.adb._run("reconnect offline")
                        except Exception:
                            pass
                except Exception as e:
                    logger.debug("Playback: ADB reconnect %s failed: %s", device_id, e)
                if attempt < max_retries:
                    if await self._interruptible_sleep(retry_interval):
                        return
            dev.status = "offline"

    def _resolve_real_device_id(self, step: Step) -> Optional[str]:
        """Resolve step's device_id alias to real device ID."""
        if not step.device_id:
            return None
        return self._resolve_alias(step.device_id, self._device_map)

    def _is_hkmc_device(self, device_id: Optional[str]) -> bool:
        """디바이스가 HKMC/iSAP 에이전트 타입인지 확인 (hkmc_* 스텝 라우팅용).

        ICAS는 완전 별도 프로젝트이므로 여기서 제외 — icas_* 전용 스텝으로 처리.
        """
        if not device_id:
            return False
        dev = self.dm.get_device(device_id)
        return dev is not None and dev.type in ("hkmc_agent", "isap_agent", "hkmc5th_wide_agent")

    def _is_icas_device(self, device_id: Optional[str]) -> bool:
        """디바이스가 ICAS 또는 MIB 에이전트 타입인지 확인 (icas_* 스텝 라우팅용).

        MIB은 ICAS와 동일한 ksend 메커니즘 + 호환 API라 같은 step type을 사용.
        """
        if not device_id:
            return False
        dev = self.dm.get_device(device_id)
        return dev is not None and dev.type in ("icas_agent", "mib_agent")

    def _get_agent_service(self, device_id: Optional[str]):
        """Return (svc, kind) where kind ∈ {"hkmc", "isap", "icas", None}.

        기존 호출부는 `svc, is_isap = ...` 형태인데 ICAS 지원을 위해 kind 문자열을
        반환하도록 확장. mib_agent는 MIBAgentService를 반환하면서 kind="icas"로 처리해
        ICAS step 디스패처를 그대로 재사용 (두 서비스 API 호환).
        """
        if not device_id:
            return None, None
        dev = self.dm.get_device(device_id)
        if not dev:
            return None, None
        if dev.type == "isap_agent":
            return self.dm.get_isap_service(device_id), "isap"
        if dev.type == "hkmc_agent":
            return self.dm.get_hkmc_service(device_id), "hkmc"
        if dev.type == "hkmc5th_wide_agent":
            return self.dm.get_hkmc5th_wide_service(device_id), "hkmc"
        if dev.type == "icas_agent":
            return self.dm.get_icas_service(device_id), "icas"
        if dev.type == "mib_agent":
            return self.dm.get_mib_service(device_id), "icas"
        return None, None

    # ── OCR 가상 모듈 헬퍼 ─────────────────────────────────────────────────

    def _find_ocr_device(self, step: Step) -> Optional[dict]:
        """OCR 스텝에서 스크린샷 대상 디바이스 정보 반환.
        step.screenshot_device_id 우선, 없으면 주 디바이스 첫 번째로 fallback."""
        device_id = step.screenshot_device_id
        if device_id:
            resolved = self._resolve_alias(device_id, self._device_map)
            dev = self.dm.get_device(resolved) or self.dm.get_device(device_id)
            if dev:
                screen_type = step.screen_type or "front_center"
                return {"type": dev.type, "id": dev.id, "address": dev.address, "screen_type": screen_type}
        # fallback: 주 디바이스 중 첫 번째
        for d in self.dm.list_primary():
            if d.type in ("adb", "hkmc_agent", "hkmc5th_wide_agent", "isap_agent", "icas_agent", "mib_agent", "vision_camera", "webcam"):
                return {"type": d.type, "id": d.id, "address": d.address, "screen_type": "front_center"}
        return None

    async def _screencap_bytes(self, dev_info: dict) -> Optional[bytes]:
        """디바이스 정보로부터 스크린샷 bytes 반환."""
        dev_type = dev_info["type"]
        dev_id = dev_info["id"]
        screen_type = dev_info.get("screen_type", "front_center")
        try:
            if dev_type == "adb":
                return await self.adb.screencap_bytes(serial=dev_info.get("address") or dev_id, fmt="png")
            elif dev_type == "hkmc_agent":
                svc = self.dm.get_hkmc_service(dev_id)
                if svc:
                    return await svc.async_screencap_bytes(screen_type=screen_type, fmt="png")
            elif dev_type == "hkmc5th_wide_agent":
                svc = self.dm.get_hkmc5th_wide_service(dev_id)
                if svc:
                    return await svc.async_screencap_bytes(screen_type=screen_type, fmt="png")
            elif dev_type == "isap_agent":
                svc = self.dm.get_isap_service(dev_id)
                if svc:
                    return await svc.async_screencap_bytes(screen_type=screen_type, fmt="png")
            elif dev_type == "icas_agent":
                svc = self.dm.get_icas_service(dev_id)
                if svc:
                    return await svc.async_screencap_bytes(screen_type=screen_type or "HU", fmt="png")
            elif dev_type == "mib_agent":
                svc = self.dm.get_mib_service(dev_id)
                if svc:
                    return await svc.async_screencap_bytes(screen_type=screen_type or "HU", fmt="png")
            elif dev_type in ("webcam", "vision_camera"):
                cam = (self.dm.get_webcam_device(dev_id) if dev_type == "webcam"
                       else self.dm.get_vision_camera(dev_id))
                if cam:
                    loop = asyncio.get_event_loop()
                    return await loop.run_in_executor(None, cam.CaptureBytes, "png")
        except Exception as e:
            logger.error("OCR screencap 실패 (device=%s type=%s): %s", dev_id, dev_type, e)
        return None

    async def _tap_ocr_device(self, dev_info: dict, x: int, y: int) -> None:
        """OCR이 찾은 좌표로 해당 디바이스에 탭 실행."""
        dev_type = dev_info["type"]
        dev_id = dev_info["id"]
        screen_type = dev_info.get("screen_type", "front_center")
        if dev_type == "adb":
            await self.adb.tap(x, y, serial=dev_info.get("address") or dev_id)
        elif dev_type == "hkmc_agent":
            svc = self.dm.get_hkmc_service(dev_id)
            if svc:
                await svc.async_tap(x, y, screen_type)
        elif dev_type == "hkmc5th_wide_agent":
            svc = self.dm.get_hkmc5th_wide_service(dev_id)
            if svc:
                await svc.async_tap(x, y, screen_type)
        elif dev_type == "isap_agent":
            svc = self.dm.get_isap_service(dev_id)
            if svc:
                await svc.async_tap(x, y, screen_type)
        elif dev_type == "icas_agent":
            svc = self.dm.get_icas_service(dev_id)
            if svc:
                await svc.async_tap(x, y, screen_type or "HU")
        elif dev_type == "mib_agent":
            svc = self.dm.get_mib_service(dev_id)
            if svc:
                await svc.async_tap(x, y, screen_type or "HU")
        else:
            logger.warning("OCR ClickText: 탭 미지원 디바이스 타입 %s", dev_type)

    async def _execute_ocr_step(self, step: Step, func_name: str, func_args: dict) -> str:
        """OCR 가상 모듈 스텝 실행."""
        from .ocr_service import (
            has_text, find_text_center, check_text_in_region, find_text_center_in_region,
            run_ocr, extract_region_items,
        )

        dev_info = self._find_ocr_device(step)
        if dev_info is None:
            return "FAIL: 스크린샷 디바이스를 찾을 수 없음"

        img_bytes = await self._screencap_bytes(dev_info)
        if img_bytes is None:
            return "FAIL: 스크린샷 캡처 실패"

        loop = asyncio.get_event_loop()
        # 모든 OCR 함수가 공유하는 language 인자 — 빈 값이면 ocr_service의 기본(korean) 사용
        language = str(func_args.get("language", "") or "").strip() or None

        def _parse_region(raw: str) -> tuple[int, int, int, int]:
            """region 문자열 'x,y,w,h' 파싱 — 토큰 부족/변환 실패 시 0으로 채움."""
            parts = [p.strip() for p in str(raw or "").split(",")]
            def _i(v: str) -> int:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
            return (
                _i(parts[0]) if len(parts) > 0 else 0,
                _i(parts[1]) if len(parts) > 1 else 0,
                _i(parts[2]) if len(parts) > 2 else 0,
                _i(parts[3]) if len(parts) > 3 else 0,
            )

        if func_name == "CheckText":
            # text는 쉼표 구분으로 여러 개 지정 가능 — 모두 존재해야 PASS (AND 조건).
            # 토큰별로 strip + 빈 토큰 제외. 입력 자체가 비어있으면 FAIL.
            raw_text = str(func_args.get("text", ""))
            targets = [t.strip() for t in raw_text.split(",") if t.strip()]
            if not targets:
                return "FAIL: text 파라미터가 비어 있습니다"
            threshold = float(func_args.get("threshold", "0.8") or 0.8)
            mode = str(func_args.get("mode", "Full Screen"))

            if mode == "Region":
                rx, ry, rw, rh = _parse_region(func_args.get("region", ""))
                missing: list[str] = []
                for target in targets:
                    ok = await loop.run_in_executor(
                        None, check_text_in_region, img_bytes, target, rx, ry, rw, rh, threshold, language
                    )
                    if not ok:
                        missing.append(target)
            else:
                missing = []
                for target in targets:
                    ok, _ = await loop.run_in_executor(None, has_text, img_bytes, target, threshold, language)
                    if not ok:
                        missing.append(target)

            if not missing:
                return f"PASS: 모든 텍스트 검출됨 ({len(targets)}개)" if len(targets) > 1 else "PASS"
            joined = ", ".join(f"'{m}'" for m in missing)
            return f"FAIL: {joined} 텍스트를 찾을 수 없음"

        elif func_name == "ClickText":
            target = str(func_args.get("text", ""))
            threshold = float(func_args.get("threshold", "0.8") or 0.8)
            mode = str(func_args.get("mode", "Full Screen"))
            if mode == "Region":
                rx, ry, rw, rh = _parse_region(func_args.get("region", ""))
                center = await loop.run_in_executor(
                    None, find_text_center_in_region, img_bytes, target, rx, ry, rw, rh, threshold, language
                )
            else:
                center = await loop.run_in_executor(None, find_text_center, img_bytes, target, threshold, language)
            if center is None:
                return f"FAIL: '{target}' 텍스트를 찾을 수 없음"
            x, y = center
            # HKMC 일체형 표시 보정 — image_tap과 동일. OCR은 front_center(AVN) 캡처의
            # 로컬 좌표를 찾으므로 실제 터치 좌표계(+x_offset)로 환산해야 한다.
            # 기본형/비-HKMC는 params에 x_offset이 없어 0 → 무영향.
            x_offset = int((step.params or {}).get("x_offset", 0) or 0)
            await self._tap_ocr_device(dev_info, x + x_offset, y)
            return f"PASS: '{target}' 클릭 완료 (x={x + x_offset}, y={y})"

        elif func_name == "ExtractAllText":
            # 디버깅/시나리오 작성용 — 화면(또는 영역)의 모든 텍스트를 결과로 반환.
            # 항상 PASS (텍스트 미검출도 정상 결과로 취급, OCR 엔진 자체 실패만 FAIL).
            mode = str(func_args.get("mode", "Full Screen"))
            # min_length: 1글자짜리 결과는 아이콘 오인식인 경우가 많아 기본 2자 이상만 표시.
            # 1로 두면 모든 결과 노출 (디버그용).
            try:
                min_length = int(func_args.get("min_length", "2") or 2)
            except (TypeError, ValueError):
                min_length = 2
            min_length = max(1, min_length)

            offset_x = 0
            offset_y = 0
            if mode == "Region":
                rx, ry, rw, rh = _parse_region(func_args.get("region", ""))
                items, offset_x, offset_y = await loop.run_in_executor(
                    None, extract_region_items, img_bytes, rx, ry, rw, rh, language
                )
                scope = f"Region({rx},{ry},{rw},{rh})"
            else:
                items = await loop.run_in_executor(None, run_ocr, img_bytes, language)
                scope = "Full Screen"

            total_raw = len(items)
            # 글자 수 필터 — strip 후 길이로 판단
            kept = [it for it in items if len(it.text.strip()) >= min_length]
            dropped = total_raw - len(kept)

            if not kept:
                tail = f" (필터로 {dropped}개 제외)" if dropped else ""
                return f"PASS: {scope} — 텍스트 미검출{tail}"

            header = f"PASS: {scope} — {len(kept)}개 텍스트 검출"
            if dropped:
                header += f" (min_length={min_length}로 {dropped}개 제외)"
            lines = [header]
            for i, it in enumerate(kept, 1):
                cx, cy = it.center
                # Region 모드면 크롭 좌표를 원본 이미지 좌표로 환산
                cx += offset_x
                cy += offset_y
                # 개행/탭은 공백으로 치환하여 한 줄에 표시
                clean = it.text.replace("\n", " ").replace("\t", " ").strip()
                lines.append(f"  [{i}] (x={cx}, y={cy}) score={it.score:.2f}: \"{clean}\"")
            return "\n".join(lines)

        return f"FAIL: 알 수 없는 OCR 함수 '{func_name}'"

    # ── /OCR 가상 모듈 헬퍼 ────────────────────────────────────────────────

    def _resolve_screenshot_device(self, step: Step) -> Optional[dict]:
        """Resolve which device to take screenshots from.

        Returns:
            {"type": "adb", "id": serial} or
            {"type": "hkmc_agent", "id": device_id, "screen_type": ...} or
            {"type": "vision_camera", "id": device_id} or
            None (no screenshot possible)
        """
        # 스크린샷 불필요한 경우: serial/module이면서 기대이미지 없음, wait이면서 기대이미지 없음
        if step.type in (StepType.SERIAL_COMMAND, StepType.MODULE_COMMAND) and not step.expected_image:
            return None
        if step.type == StepType.WAIT and not step.expected_image:
            return None
        # all_random은 스트레스성 스텝이라 기대이미지 없으면 비교 스킵 (화면이 예측 불가)
        if step.type == StepType.ALL_RANDOM and not step.expected_image:
            return None

        # WIN_* 스텝은 항상 WinControl에서 캡처 — step.device_id가 ADB 기기를 가리키더라도
        # (녹화 시 활성 primary가 ADB였으면 그렇게 저장됨) 액션은 WinControl에서 실행되므로
        # 검증 캡처도 같은 윈도우에서 떠야 함. 그렇지 않으면 ADB 화면이 actual로 잡혀 비교가 무의미.
        if step.type in (StepType.WIN_TAP, StepType.WIN_DOUBLE_CLICK, StepType.WIN_CLICK_SEQUENCE,
                          StepType.WIN_LONG_PRESS,
                          StepType.WIN_SWIPE, StepType.WIN_INPUT_TEXT, StepType.WIN_KEY,
                          StepType.WIN_KEY_COMBO):
            wc_dev = None
            for d in self.dm.list_all():
                if d.type == "wincontrol":
                    wc_dev = d
                    break
            if wc_dev is not None:
                logger.info(
                    "[SCREENSHOT RESOLVE] step=%s type=%s → forcing wincontrol device id=%s "
                    "(ignoring step.device_id=%s screenshot_device_id=%s)",
                    step.id, step.type.value, wc_dev.id,
                    step.device_id, step.screenshot_device_id,
                )
                return {"type": "wincontrol", "id": wc_dev.id}
            # WinControl 디바이스가 등록되지 않은 환경 — 명확히 에러 처리.
            logger.error(
                "[SCREENSHOT RESOLVE] step=%s type=%s requires WinControl but no wincontrol device registered",
                step.id, step.type.value,
            )
            return None

        # screenshot_device_id가 저장되어 있으면 해당 디바이스 우선 사용
        # device_map을 통해 실제 디바이스 ID로 매핑. 매핑 후 못 찾으면 원본 id 로도 재시도.
        if step.screenshot_device_id:
            resolved_ss_id = self._resolve_alias(step.screenshot_device_id, self._device_map)
            ss_dev = self.dm.get_device(resolved_ss_id)
            if not ss_dev and resolved_ss_id != step.screenshot_device_id:
                # device_map 이 오래되어 존재하지 않는 address 로 매핑된 경우, 원본 id 로 재조회
                ss_dev = self.dm.get_device(step.screenshot_device_id)
                if ss_dev:
                    logger.warning(
                        "[SCREENSHOT RESOLVE] step=%s device_map(%s)->%s stale; fell back to original id",
                        step.id, step.screenshot_device_id, resolved_ss_id,
                    )
            logger.info(
                "[SCREENSHOT RESOLVE] step=%s screenshot_device_id=%s resolved=%s found=%s type=%s",
                step.id, step.screenshot_device_id, resolved_ss_id,
                bool(ss_dev), ss_dev.type if ss_dev else None,
            )
            if ss_dev:
                if ss_dev.type == "hkmc_agent":
                    screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                    return {"type": "hkmc_agent", "id": ss_dev.id, "screen_type": screen_type}
                if ss_dev.type == "hkmc5th_wide_agent":
                    screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                    return {"type": "hkmc5th_wide_agent", "id": ss_dev.id, "screen_type": screen_type}
                if ss_dev.type == "isap_agent":
                    screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                    return {"type": "isap_agent", "id": ss_dev.id, "screen_type": screen_type}
                if ss_dev.type == "icas_agent":
                    screen_type = step.screen_type or step.params.get("screen_type", "HU")
                    return {"type": "icas_agent", "id": ss_dev.id, "screen_type": screen_type}
                if ss_dev.type == "mib_agent":
                    screen_type = step.screen_type or step.params.get("screen_type", "HU")
                    return {"type": "mib_agent", "id": ss_dev.id, "screen_type": screen_type}
                if ss_dev.type == "vision_camera":
                    return {"type": "vision_camera", "id": ss_dev.id}
                if ss_dev.type == "webcam":
                    return {"type": "webcam", "id": ss_dev.id}
                if ss_dev.type == "wincontrol":
                    return {"type": "wincontrol", "id": ss_dev.id}
                if ss_dev.type == "adb":
                    result = {"type": "adb", "id": ss_dev.id, "serial": ss_dev.address}
                    adb_screen = step.screen_type or step.params.get("screen_type")
                    if adb_screen:
                        result["screen_type"] = adb_screen
                    return result
            # 명시적 screenshot_device_id 가 주어졌는데 디바이스를 못 찾으면, primary[0] 으로
            # 임의 폴백하지 말고 명확히 실패시킨다 — WAIT 스텝 같이 device_id 가 없는 스텝이
            # 엉뚱한 디바이스(주로 인덱스 0 의 ADB)에서 캡처되어 비교가 틀어지는 버그 방지.
            logger.error(
                "[SCREENSHOT RESOLVE] step=%s screenshot_device_id=%s resolved=%s: device not found — aborting capture",
                step.id, step.screenshot_device_id, resolved_ss_id,
            )
            return None

        real_id = self._resolve_real_device_id(step)
        if real_id:
            dev = self.dm.get_device(real_id)
            if dev and dev.type == "wincontrol":
                # WinControl 은 auxiliary 지만 스크린샷 가능 — 임베드된 윈도우 캡처.
                return {"type": "wincontrol", "id": dev.id}
            if dev and dev.type in ("serial", "module"):
                # 보조 디바이스는 스크린샷 불가 → primary 디바이스로 폴백
                pass
            elif dev and dev.type == "hkmc_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "hkmc_agent", "id": dev.id, "screen_type": screen_type}
            elif dev and dev.type == "hkmc5th_wide_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "hkmc5th_wide_agent", "id": dev.id, "screen_type": screen_type}
            elif dev and dev.type == "isap_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "isap_agent", "id": dev.id, "screen_type": screen_type}
            elif dev and dev.type == "icas_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "HU")
                return {"type": "icas_agent", "id": dev.id, "screen_type": screen_type}
            elif dev and dev.type == "mib_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "HU")
                return {"type": "mib_agent", "id": dev.id, "screen_type": screen_type}
            elif dev and dev.type == "vision_camera":
                return {"type": "vision_camera", "id": dev.id}
            elif dev and dev.type == "webcam":
                return {"type": "webcam", "id": dev.id}
            elif dev:
                # ADB — dev.address가 실제 ADB 시리얼
                adb_screen = step.screen_type or step.params.get("screen_type")
                result = {"type": "adb", "id": dev.id, "serial": dev.address}
                if adb_screen is not None:
                    result["screen_type"] = adb_screen
                elif len(dev.info.get("displays", [])) > 1:
                    # 멀티 디스플레이: screen_type 미지정 시 display 0 기본값
                    result["screen_type"] = "0"
                return result
        # device_id 없거나, 보조 디바이스인 경우 → 첫 번째 primary 디바이스로 스크린샷
        # fallback이라도 step에 저장된 screen_type(rear_left/rear_right 등)은 반드시 존중해야
        # 한다. 이를 놓치면 스텝 테스트(override 적용)는 통과하지만 시나리오 재생은
        # front_center 로 캡처하여 SSIM이 틀리는 버그가 발생한다.
        primary = self.dm.list_primary()
        if primary:
            dev = primary[0]
            logger.warning(
                "[SCREENSHOT RESOLVE] step=%s no screenshot_device_id / device_id — falling back to primary[0]=%s (type=%s). "
                "기대이미지 비교가 의도와 다른 디바이스에서 수행될 수 있음.",
                step.id, dev.id, dev.type,
            )
            if dev.type == "hkmc_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "hkmc_agent", "id": dev.id, "screen_type": screen_type}
            if dev.type == "hkmc5th_wide_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "hkmc5th_wide_agent", "id": dev.id, "screen_type": screen_type}
            if dev.type == "isap_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "front_center")
                return {"type": "isap_agent", "id": dev.id, "screen_type": screen_type}
            if dev.type == "icas_agent":
                screen_type = step.screen_type or step.params.get("screen_type", "HU")
                return {"type": "icas_agent", "id": dev.id, "screen_type": screen_type}
            if dev.type == "vision_camera":
                return {"type": "vision_camera", "id": dev.id}
            if dev.type == "webcam":
                return {"type": "webcam", "id": dev.id}
            return {"type": "adb", "id": dev.id, "serial": dev.address}
        return None

    def _resolve_adb_serial(self, step: Step) -> Optional[str]:
        """Resolve the ADB serial for a step. Returns None for non-ADB steps.

        Backward-compatible wrapper around _resolve_screenshot_device.
        """
        info = self._resolve_screenshot_device(step)
        if info and info["type"] == "adb":
            return info["id"]
        return None

    async def _run_action(self, step: Step) -> None:
        """Execute step action on the appropriate device."""
        params = step.params
        real_id = self._resolve_real_device_id(step)

        # IMAGE_TAP — 좌표 비-종속 터치.
        # 실행 시점에 디바이스에서 현재 화면을 캡처해 template_match 를 돌려 중심 좌표를
        # 구한 뒤, 디바이스 종류에 따라 적절한 tap 으로 디스패치한다. 매칭 실패 시 RuntimeError.
        if step.type == StepType.IMAGE_TAP:
            await self._run_image_tap(step, real_id)
            return

        if step.type == StepType.MODULE_COMMAND:
            module_name = params.get("module", "")
            func_name = params.get("function", "")
            func_args = params.get("args", {})

            # OCR 가상 모듈 — 별도 처리 후 즉시 반환
            if module_name == "OCR":
                self._last_module_result = await self._execute_ocr_step(step, func_name, func_args)
                return

            # Pass device connection info as constructor kwargs
            ctor_kwargs = None
            shared_conn = None
            ssh_credentials = None
            adb_serial: Optional[str] = None
            dev = self.dm.get_device(real_id) if real_id else None

            # MODULE_COMMAND는 step.device_id가 과거 녹화·편집 과정에서 엉뚱한 디바이스를
            # 가리키는 경우가 있음(예: HKMC/DLT address로 resolve). 모듈 이름이 일반
            # 모듈(Android/CMD/SHELL 제외)이면 현재 시점에 유일하게 해당 모듈이 붙어있는
            # auxiliary 디바이스가 있는지 찾아, 있으면 그것으로 강제 교체한다.
            # CMD/SHELL 은 Common 디바이스 (OS 별로 다름) 에 묶여 있어 device 재탐색 불필요.
            if module_name and module_name not in ("Android", "CMD", "SHELL"):
                # HKMC6th는 hkmc_agent 타입 디바이스(primary)에서 찾는다.
                # 그 외 모듈은 auxiliary 디바이스의 info.module로 매칭.
                dev_module = (dev.info or {}).get("module") if dev else None
                if module_name == "HKMC6th":
                    candidates = [
                        d for d in self.dm.list_all() if d.type == "hkmc_agent"
                    ]
                    is_correct_dev = bool(dev and dev.type == "hkmc_agent")
                else:
                    candidates = [
                        d for d in self.dm.list_auxiliary()
                        if (d.info or {}).get("module") == module_name
                    ]
                    is_correct_dev = (dev_module == module_name)
                # dev가 이미 올바른 모듈 디바이스면 그대로 사용
                if not is_correct_dev:
                    if len(candidates) == 1:
                        chosen = candidates[0]
                        logger.info(
                            "MODULE_COMMAND: overriding step device %s (module=%s, type=%s) → %s (module=%s)",
                            real_id, dev_module,
                            dev.type if dev else None,
                            chosen.id, module_name,
                        )
                        dev = chosen
                        real_id = chosen.id
                    elif len(candidates) > 1:
                        # 여러 개 중에는 연결된 것을 우선, 그 다음 첫 번째
                        chosen = next(
                            (c for c in candidates if c.status in ("connected", "device")),
                            candidates[0],
                        )
                        logger.info(
                            "MODULE_COMMAND: multiple %s devices; using %s (status=%s) instead of %s",
                            module_name, chosen.id, chosen.status, real_id,
                        )
                        dev = chosen
                        real_id = chosen.id
                    else:
                        logger.warning(
                            "MODULE_COMMAND: module=%s but no auxiliary device registered for it; falling back to step device %s",
                            module_name, real_id,
                        )
            hkmc_svc = None
            if dev:
                ctor_kwargs = _build_ctor_kwargs(dev)
                shared_conn = self.dm.get_serial_conn(real_id)
                # SSH 디바이스: 저장된 자격증명을 SSHManager.create_ssh_client에 전달
                if dev.type == "ssh":
                    ssh_credentials = {
                        "host": dev.info.get("host", dev.address),
                        "port": int(dev.info.get("port", 22)),
                        "username": dev.info.get("username", ""),
                        "password": dev.info.get("password", ""),
                        "key_file_path": dev.info.get("key_file_path", ""),
                    }
                # ADB 디바이스: Android 모듈의 Send_adb_command가 사용
                if dev.type == "adb":
                    adb_serial = dev.address
                # HKMC6th 모듈: device_manager가 보유한 디바이스별 HKMC6thService 인스턴스 주입
                if module_name == "HKMC6th" and dev.type == "hkmc_agent":
                    hkmc_svc = self.dm.get_hkmc_service(real_id)
                    if hkmc_svc is None:
                        raise RuntimeError(
                            f"HKMC6th step requires connected hkmc_agent device, but {real_id} has no service"
                        )
                # HKMC5thWide 모듈: device_manager가 보유한 디바이스별 HKMC5thWideService 인스턴스 주입
                if module_name == "HKMC5thWide" and dev.type == "hkmc5th_wide_agent":
                    hkmc_svc = self.dm.get_hkmc5th_wide_service(real_id)
                    if hkmc_svc is None:
                        raise RuntimeError(
                            f"HKMC5thWide step requires connected hkmc5th_wide_agent device, but {real_id} has no service"
                        )
            logger.info("Module exec: %s.%s device=%s ctor=%s shared_conn=%s ssh=%s adb=%s hkmc=%s",
                        module_name, func_name, real_id, ctor_kwargs,
                        shared_conn is not None, ssh_credentials is not None, adb_serial,
                        hkmc_svc is not None)
            # HKMC6th 모듈은 직접 HKMC_TOUCH/SWIPE 스텝과 동일하게 stale socket
            # 또는 idle drop 으로 인한 ConnectionError 를 1회 retry — 같은 스텝이
            # false fail 로 끝나지 않고 force-reconnect 후 동일 함수를 재실행한다.
            if module_name == "HKMC6th" and dev and dev.type == "hkmc_agent":
                last_exc: Optional[BaseException] = None
                # 액션 시작 시점 연결 상태: 정상 연결 중 끊기면 2분(20s×6) 재연결,
                # 이미 끊겨 있던 경우엔 1회만(스텝 시작 게이트와 동일 정책 — 매 모듈
                # 스텝마다 2분씩 기다리는 것을 방지).
                _was_connected = bool(hkmc_svc and hkmc_svc.is_connected)
                for _hkmc_mod_attempt in range(2):
                    try:
                        result = await execute_module_function(
                            module_name, func_name, func_args, ctor_kwargs, shared_conn,
                            ssh_credentials, adb_serial,
                            hkmc_service=hkmc_svc,
                        )
                        last_exc = None
                        break
                    except (ConnectionError, OSError) as ce:
                        last_exc = ce
                        if _hkmc_mod_attempt == 0:
                            logger.warning(
                                "HKMC6th module action failed (connection lost), reconnecting: %s",
                                ce,
                            )
                            if _was_connected:
                                await self._ensure_device_connected(real_id, max_retries=6, retry_interval=20.0)
                            else:
                                await self._ensure_device_connected(real_id, max_retries=1)
                            # 재연결 후 새 서비스 인스턴스를 받아 다음 시도에 주입
                            hkmc_svc = self.dm.get_hkmc_service(real_id)
                            if hkmc_svc is None:
                                raise
                            continue
                        raise
                if last_exc is not None:
                    raise last_exc
            elif module_name == "HKMC5thWide" and dev and dev.type == "hkmc5th_wide_agent":
                last_exc = None
                # HKMC6th 와 동일 정책: 정상 연결 중 끊김=2분(20s×6), 이미 끊김=1회.
                _was_connected = bool(hkmc_svc and hkmc_svc.is_connected)
                for _hkmc_mod_attempt in range(2):
                    try:
                        result = await execute_module_function(
                            module_name, func_name, func_args, ctor_kwargs, shared_conn,
                            ssh_credentials, adb_serial,
                            hkmc_service=hkmc_svc,
                        )
                        last_exc = None
                        break
                    except (ConnectionError, OSError) as ce:
                        last_exc = ce
                        if _hkmc_mod_attempt == 0:
                            logger.warning(
                                "HKMC5thWide module action failed (connection lost), reconnecting: %s", ce,
                            )
                            if _was_connected:
                                await self._ensure_device_connected(real_id, max_retries=6, retry_interval=20.0)
                            else:
                                await self._ensure_device_connected(real_id, max_retries=1)
                            hkmc_svc = self.dm.get_hkmc5th_wide_service(real_id)
                            if hkmc_svc is None:
                                raise
                            continue
                        raise
                if last_exc is not None:
                    raise last_exc
            else:
                result = await execute_module_function(
                    module_name, func_name, func_args, ctor_kwargs, shared_conn,
                    ssh_credentials, adb_serial,
                    hkmc_service=hkmc_svc,
                )
            self._last_module_result = result
        elif step.type == StepType.SERIAL_COMMAND:
            if not real_id:
                raise ValueError("serial_command requires device_id")
            await self.dm.send_serial_command(
                real_id,
                params["data"],
                params.get("read_timeout", 1.0),
            )
        elif step.type in (StepType.HKMC_TOUCH, StepType.HKMC_SWIPE, StepType.HKMC_KEY, StepType.HKMC_LONG_PRESS) or (step.type == StepType.REPEAT_TAP and self._is_hkmc_device(real_id)):
            if not real_id:
                raise ValueError("HKMC/iSAP step requires device_id")
            # 액션 실행 중 연결 끊김 시 재연결 후 재시도 (최대 2회).
            # 사전 체크 단계에서 disconnected 인 경우에도 먼저 재연결을 시도하고,
            # 여전히 안 되면 에러. (이전에는 사전 체크 실패 시 즉시 raise 되어
            # 재연결 retry 가 사실상 동작하지 않았음 — HKMC 스텝 테스트 실패 원인.)
            for _hkmc_attempt in range(2):
                svc, kind = self._get_agent_service(real_id)
                if kind == "icas":
                    raise ValueError(
                        f"HKMC step on ICAS device {real_id}: ICAS는 icas_* 전용 스텝을 사용해야 함"
                    )
                is_isap = kind == "isap"
                if not svc or not svc.is_connected:
                    if _hkmc_attempt == 0:
                        # 진단 로그: 실제 디바이스/서비스 상태 출력
                        _dev = self.dm.get_device(real_id)
                        _svc_by_id = None
                        if _dev:
                            _svc_by_id = self.dm._hkmc_conns.get(_dev.id)
                        logger.warning(
                            "[HKMC RECONNECT] real_id=%s svc=%s is_connected=%s dev=%s dev.id=%s dev.address=%s dev.type=%s "
                            "svc_by_dev_id=%s _hkmc_conns_keys=%s port=%s",
                            real_id,
                            svc, (svc.is_connected if svc else None),
                            _dev, (_dev.id if _dev else None),
                            (_dev.address if _dev else None),
                            (_dev.type if _dev else None),
                            _svc_by_id,
                            list(self.dm._hkmc_conns.keys()),
                            (_dev.info.get("port") if _dev else None),
                        )
                        # 강제 재연결: stale flag(is_connected=True 인데 실제 socket 죽은 경우)도
                        # 극복하기 위해 직접 disconnect + 새 서비스 생성 + connect 수행.
                        await self._force_reconnect_hkmc(real_id)
                        continue  # 재시도
                    raise ValueError(f"HKMC/iSAP device {real_id} not connected")
                try:
                    screen_type = step.screen_type or params.get("screen_type", "front_center")
                    if step.type == StepType.REPEAT_TAP:
                        await svc.async_repeat_tap(params["x"], params["y"],
                                                   int(params.get("count", 5)),
                                                   int(params.get("interval_ms", 100)), screen_type)
                    elif step.type == StepType.HKMC_TOUCH:
                        await svc.async_tap(params["x"], params["y"], screen_type)
                    elif step.type == StepType.HKMC_LONG_PRESS:
                        await svc.async_long_press(params["x"], params["y"],
                                                   int(params.get("duration_ms", 3000)), screen_type)
                    elif step.type == StepType.HKMC_SWIPE:
                        if is_isap:
                            await svc.async_swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                                                  screen_type, int(params.get("duration_ms", 300)))
                        else:
                            await svc.async_swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                                                  screen_type, int(params.get("duration_ms", 0)))
                    elif step.type == StepType.HKMC_KEY:
                        key_name = params.get("key_name")
                        direction = params.get("direction")
                        # CCRC source override (UI 토글로 저장됨) — 정수 또는 None
                        key_source = params.get("key_source")
                        if key_name:
                            sub_cmd = params.get("sub_cmd", 0x43)
                            if is_isap:
                                await svc.async_send_key_by_name(key_name, sub_cmd, screen_type, direction,
                                                                  key_source=key_source)
                            else:
                                # screen_type 반드시 전달 — send_key_by_name 의 자동 monitor
                                # 보정(rear_left→CCRC_MONITOR_LEFT) 이 동작해야 리어 모니터로
                                # 키가 라우팅된다. 미전달 시 HKMC 스텝 테스트가 먹지 않는
                                # 회귀가 있었음. RRC_RADIO/MEDIA 는 IVI Type 제약이라 UI 에서
                                # rear 외 비활성화로 별도 차단.
                                monitor = params.get("monitor", 0x00)
                                await svc.async_send_key_by_name(key_name, sub_cmd, monitor, direction, screen_type,
                                                                  key_source=key_source)
                        else:
                            if is_isap:
                                await svc.async_send_key(
                                    params.get("cmd", 0), params["sub_cmd"], params["key_data"],
                                    screen_type, direction,
                                )
                            else:
                                await svc.async_send_key(
                                    params["cmd"], params["sub_cmd"], params["key_data"],
                                    params.get("monitor", 0x00), direction,
                                )
                    break
                except (ConnectionError, OSError) as ce:
                    if _hkmc_attempt == 0:
                        logger.warning("HKMC/iSAP action failed (connection lost), reconnecting: %s", ce)
                        # 재생 중 끊김(정상 연결 중 드롭): 20초에 한 번씩 2분간(6회) 재연결.
                        # 디바이스 전원 사이클(off→on) 직후 에이전트 부팅(수십 초)을 흡수.
                        # 2분 내 복구 실패 시 attempt 1 이 미연결을 확인하고 raise → 스텝 실패,
                        # 디바이스는 disconnected 로 남고 이후 스텝은 시작 시 1회만 재시도.
                        await self._ensure_device_connected(real_id, max_retries=6, retry_interval=20.0)
                    else:
                        raise
        elif step.type in (StepType.ICAS_TOUCH, StepType.ICAS_SWIPE, StepType.ICAS_KEY, StepType.ICAS_LONG_PRESS) or (step.type == StepType.REPEAT_TAP and self._is_icas_device(real_id)):
            if not real_id:
                raise ValueError("ICAS step requires device_id")
            for _icas_attempt in range(2):
                svc, kind = self._get_agent_service(real_id)
                if kind != "icas":
                    raise ValueError(
                        f"ICAS step on non-ICAS device {real_id} (kind={kind})"
                    )
                if not svc or not svc.is_connected:
                    if _icas_attempt == 0:
                        logger.warning(
                            "ICAS device %s not connected on pre-check, forcing reconnect",
                            real_id,
                        )
                        await self._ensure_device_connected(
                            real_id, max_retries=3, retry_interval=2.0,
                        )
                        continue
                    raise ValueError(f"ICAS device {real_id} not connected")
                try:
                    screen_type = step.screen_type or params.get("screen_type", "HU")
                    if step.type == StepType.REPEAT_TAP:
                        await svc.async_repeat_tap(params["x"], params["y"],
                                                   int(params.get("count", 5)),
                                                   int(params.get("interval_ms", 100)), screen_type)
                    elif step.type == StepType.ICAS_TOUCH:
                        await svc.async_tap(params["x"], params["y"], screen_type)
                    elif step.type == StepType.ICAS_LONG_PRESS:
                        await svc.async_long_press(params["x"], params["y"],
                                                   int(params.get("duration_ms", 3000)), screen_type)
                    elif step.type == StepType.ICAS_SWIPE:
                        await svc.async_swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                                              screen_type, int(params.get("duration_ms", 300)))
                    elif step.type == StepType.ICAS_KEY:
                        key_name = params.get("key_name")
                        direction = params.get("direction")
                        # LONG_KEY일 때만 의미 있음 — None이면 기본 1000ms (서비스 측 기본)
                        hold_ms_raw = params.get("hold_ms")
                        try:
                            hold_ms = int(hold_ms_raw) if hold_ms_raw is not None else None
                        except (TypeError, ValueError):
                            hold_ms = None
                        if key_name:
                            sub_cmd = params.get("sub_cmd", 0x43)
                            await svc.async_send_key_by_name(key_name, sub_cmd, screen_type, direction,
                                                             hold_ms=hold_ms)
                        else:
                            await svc.async_send_key(
                                params.get("cmd", 0), params["sub_cmd"], params["key_data"],
                                screen_type, direction, hold_ms=hold_ms,
                            )
                    break
                except (ConnectionError, OSError) as ce:
                    if _icas_attempt == 0:
                        logger.warning("ICAS action failed (connection lost), reconnecting: %s", ce)
                        await self._ensure_device_connected(real_id, max_retries=2, retry_interval=2.0)
                    else:
                        raise
        elif step.type == StepType.ALL_RANDOM:
            # 녹화 시 저장된 설정으로 랜덤 스트레스 재현
            # 가중치: HK 20% / SK 70% / DRAG 10% (참조 스크립트 CCIC RAND_ALL 기반)
            import random as _rnd
            if not real_id:
                raise ValueError("all_random step requires device_id")

            # 후보 조회 키: (1) real_id (device_map으로 resolve된 주소/ID),
            # (2) step.device_id (원본 alias) — device_map이 address로 해석되었을 때
            #     dm._devices / _hkmc_conns 가 alias를 키로 갖고 있어 address로 못 찾는 경우 대비
            candidate_ids = [real_id]
            if step.device_id and step.device_id != real_id:
                candidate_ids.append(step.device_id)

            svc = None
            kind = None
            used_id = real_id
            for cand in candidate_ids:
                svc, kind = self._get_agent_service(cand)
                if svc and svc.is_connected:
                    used_id = cand
                    break
            is_isap = kind == "isap"

            if not svc or not svc.is_connected:
                # 재연결 시도: 후보 중 하나라도 디바이스로 인식되면 ensure 호출
                for cand in candidate_ids:
                    if self.dm.get_device(cand):
                        logger.info("all_random: device %s not connected, trying reconnect", cand)
                        await self._ensure_device_connected(cand, max_retries=2, retry_interval=2.0)
                        svc, kind = self._get_agent_service(cand)
                        if svc and svc.is_connected:
                            used_id = cand
                            is_isap = kind == "isap"
                            break
                # 여전히 안 되면 진단성 있는 에러 메시지
                if not svc or not svc.is_connected:
                    dev = None
                    for cand in candidate_ids:
                        dev = self.dm.get_device(cand)
                        if dev:
                            break
                    if dev is None:
                        raise ValueError(
                            f"HKMC/iSAP device '{step.device_id}' (resolved='{real_id}') not found in device manager"
                        )
                    raise ValueError(
                        f"HKMC/iSAP device '{dev.id}' ({dev.address}) not connected "
                        f"(type={dev.type}, status={dev.status})"
                    )

            repeat_count = max(1, int(params.get("repeat_count", 1)))
            interval_ms = max(0, int(params.get("interval_ms", 0)))
            screen_type = step.screen_type or params.get("screen_type", "front_center")
            hk_keys = params.get("hk_keys") or []
            sk_region = params.get("sk_region") or None  # {x,y,width,height} or None
            drag_region = params.get("drag_region") or None
            weights = params.get("weights") or {}
            w_hk = float(weights.get("hk", 0.20))
            w_sk = float(weights.get("sk", 0.70))
            # HKMC 일체형 표시에서 AVN 영역 오프셋 (녹화 측에서 이미 적용된 경우 0)
            x_offset = int(params.get("x_offset", 0))
            # 대상 해상도 — 녹화 시점 값 사용 (디바이스 해상도와 맞지 않으면 범위 전체에서 추출)
            res_w = int(params.get("res_width", 1920))
            res_h = int(params.get("res_height", 720))

            def _pick_xy(region):
                if region:
                    x0 = max(0, int(region.get("x", 0)))
                    y0 = max(0, int(region.get("y", 0)))
                    x_max = min(res_w, x0 + int(region.get("width", res_w)))
                    y_max = min(res_h, y0 + int(region.get("height", res_h)))
                else:
                    x0, y0, x_max, y_max = 0, 0, res_w, res_h
                rw = max(1, x_max - x0)
                rh = max(1, y_max - y0)
                return _rnd.randrange(x0, x0 + rw), _rnd.randrange(y0, y0 + rh)

            # 로그 기록용 scenario_name / repeat_index (instance 상태에서 읽음)
            rand_scenario_name = getattr(self, "_current_scenario_name", "") or ""
            rand_repeat_idx = int(getattr(self, "_current_repeat_index", 1) or 1)
            for _i in range(repeat_count):
                if self._should_stop:
                    break
                roll = _rnd.random()
                action_summary = ""
                sub_status = "pass"
                sub_error = ""
                sub_t0 = time.time()
                try:
                    if roll < w_hk and hk_keys:
                        key_name = _rnd.choice(hk_keys)
                        is_long = _rnd.random() < 0.2
                        sub_cmd = 0x44 if is_long else 0x43
                        action_summary = f"HK key={key_name} {'LONG' if is_long else 'SHORT'} screen={screen_type}"
                        if is_isap:
                            await svc.async_send_key_by_name(key_name, sub_cmd, screen_type, None)
                        else:
                            await svc.async_send_key_by_name(key_name, sub_cmd, 0x00, None, screen_type)
                    elif roll < (w_hk + w_sk):
                        x, y = _pick_xy(sk_region)
                        x += x_offset
                        action_summary = f"SK ({x},{y}) screen={screen_type}"
                        await svc.async_tap(x, y, screen_type)
                    else:
                        x1, y1 = _pick_xy(drag_region)
                        x2, y2 = _pick_xy(drag_region)
                        x1 += x_offset; x2 += x_offset
                        action_summary = f"DRAG ({x1},{y1})→({x2},{y2}) 300ms screen={screen_type}"
                        if is_isap:
                            await svc.async_swipe(x1, y1, x2, y2, screen_type, 300)
                        else:
                            await svc.async_swipe(x1, y1, x2, y2, screen_type)
                except (ConnectionError, OSError) as ce:
                    sub_status = "error"
                    sub_error = str(ce)
                    logger.warning("all_random iteration %d failed: %s", _i + 1, ce)
                except Exception as ex:
                    sub_status = "error"
                    sub_error = str(ex)
                    logger.warning("all_random iteration %d error: %s", _i + 1, ex)
                finally:
                    sub_duration_ms = int((time.time() - sub_t0) * 1000)
                    try:
                        self._log_random_sub_action(
                            rand_scenario_name, step.id, rand_repeat_idx,
                            _i + 1, repeat_count,
                            action_summary or "(no-op)", sub_status, sub_duration_ms, sub_error,
                        )
                    except Exception as _le:
                        logger.debug("sub-action log write failed: %s", _le)
                    # 실시간 진행 상황을 WebSocket으로 전파 (프론트에서 진행률 표시 가능)
                    try:
                        publish_event({
                            "type": "random_action",
                            "step_id": step.id,
                            "iteration": _i + 1,
                            "total": repeat_count,
                            "status": sub_status,
                            "action": action_summary,
                            "duration_ms": sub_duration_ms,
                        })
                    except Exception:
                        pass
                if interval_ms > 0 and _i + 1 < repeat_count:
                    await self._interruptible_sleep(interval_ms / 1000.0)
        elif step.type == StepType.WAIT:
            wait_mode = params.get("wait_mode", "basic")
            if wait_mode == "cycle":
                start_ms = params.get("wait_start", 3000)
                interval_ms = params.get("wait_interval", 3000)
                cycle_idx = getattr(self, '_current_iteration', 0)
                actual_ms = start_ms + interval_ms * cycle_idx
                logger.info("Wait cycle: iteration=%d, wait=%dms (start=%d + interval=%d × %d)", cycle_idx, actual_ms, start_ms, interval_ms, cycle_idx)
            elif wait_mode == "random":
                import random
                wait_min = params.get("wait_min", 0)
                wait_max = params.get("wait_max", 10000)
                actual_ms = random.randint(wait_min, wait_max)
                logger.info("Wait random: %dms (range %d~%d)", actual_ms, wait_min, wait_max)
            else:
                actual_ms = params.get("duration_ms", 1000)
            # 긴 wait는 1초 chunk로 분할하며 주기적으로 wait_progress 이벤트 발행
            # → WebSocket idle 방지 + 프론트엔드 진행률 표시 가능
            total_s = actual_ms / 1000.0
            if total_s <= 2.0:
                await self._interruptible_sleep(total_s)
            else:
                PROGRESS_INTERVAL_S = 5.0
                CHUNK_S = 1.0
                elapsed = 0.0
                next_progress = PROGRESS_INTERVAL_S
                # 시작 시점 이벤트
                publish_event({
                    "type": "wait_progress",
                    "step_id": step.id,
                    "elapsed_ms": 0,
                    "total_ms": int(actual_ms),
                })
                while elapsed < total_s:
                    if self._should_stop:
                        return
                    sleep_s = min(CHUNK_S, total_s - elapsed)
                    await self._interruptible_sleep(sleep_s)
                    elapsed += sleep_s
                    if elapsed >= next_progress or elapsed >= total_s:
                        publish_event({
                            "type": "wait_progress",
                            "step_id": step.id,
                            "elapsed_ms": int(elapsed * 1000),
                            "total_ms": int(actual_ms),
                        })
                        next_progress = elapsed + PROGRESS_INTERVAL_S
        elif step.type in (StepType.WIN_TAP, StepType.WIN_DOUBLE_CLICK,
                           StepType.WIN_CLICK_SEQUENCE,
                           StepType.WIN_LONG_PRESS, StepType.WIN_SWIPE,
                           StepType.WIN_INPUT_TEXT, StepType.WIN_KEY,
                           StepType.WIN_KEY_COMBO):
            # 임베드 보장 — 저장된 process_name/exe_path 로 자동 attach 또는 launch.
            wc = self.dm.get_wincontrol_service()
            if not wc.is_available():
                # OS 별 라벨/누락 의존성 메시지 (Linux→LinControl/python-xlib, Win→WinControl/pywin32).
                from .device_manager import _WIN_CTRL_DISPLAY_NAME, _WIN_CTRL_IS_LINUX
                _missing = "python-xlib not installed" if _WIN_CTRL_IS_LINUX else "pywin32 not installed"
                raise ValueError(
                    f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {wc.import_error() or _missing}"
                )
            try:
                import asyncio
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None,
                    functools.partial(
                        wc.ensure_attached,
                        process_name=str(params.get("process_name", "") or ""),
                        exe_path=str(params.get("exe_path", "") or ""),
                        title_pattern=str(params.get("window_title", "") or ""),
                        class_name=str(params.get("window_class", "") or ""),
                        aumid=str(params.get("process_aumid", "") or ""),
                        launch_if_missing=True,
                        wait_seconds=float(params.get("launch_wait_seconds", 8.0) or 8.0),
                        target_width=int(params.get("window_width", 0) or 0),
                        target_height=int(params.get("window_height", 0) or 0),
                    ),
                )
            except Exception as e:
                raise ValueError(f"WinControl attach failed: {e}")
            # 디바이스 status 는 사용자 명시적 connect/disconnect 로만 변경 — 여기서 sync 안 함.

            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            if step.type == StepType.WIN_TAP:
                await loop.run_in_executor(None,
                    functools.partial(wc.send_tap, int(params["x"]), int(params["y"]),
                                      params.get("button", "left")))
            elif step.type == StepType.WIN_DOUBLE_CLICK:
                await loop.run_in_executor(None,
                    functools.partial(wc.send_double_click,
                                      int(params["x"]), int(params["y"])))
            elif step.type == StepType.WIN_CLICK_SEQUENCE:
                pts = params.get("points") or []
                await loop.run_in_executor(None,
                    functools.partial(wc.send_click_sequence,
                                      pts,
                                      int(params.get("interval_ms", 150)),
                                      params.get("button", "left")))
            elif step.type == StepType.WIN_LONG_PRESS:
                await loop.run_in_executor(None,
                    functools.partial(wc.send_long_press,
                                      int(params["x"]), int(params["y"]),
                                      int(params.get("duration_ms", 500)),
                                      params.get("button", "left")))
            elif step.type == StepType.WIN_SWIPE:
                await loop.run_in_executor(None,
                    functools.partial(wc.send_swipe,
                                      int(params["x1"]), int(params["y1"]),
                                      int(params["x2"]), int(params["y2"]),
                                      int(params.get("duration_ms", 300))))
            elif step.type == StepType.WIN_INPUT_TEXT:
                cfx = params.get("click_first_x")
                cfy = params.get("click_first_y")
                await loop.run_in_executor(None,
                    functools.partial(wc.send_text, str(params.get("text", "")),
                                      int(cfx) if cfx is not None else None,
                                      int(cfy) if cfy is not None else None))
            elif step.type == StepType.WIN_KEY:
                await loop.run_in_executor(None,
                    functools.partial(wc.send_key, str(params.get("key", ""))))
            elif step.type == StepType.WIN_KEY_COMBO:
                # combo_seq: 여러 조합을 한 컨텍스트 안에서 순서대로 실행 (라우터와 동일 파싱).
                seq_raw = params.get("combo_seq")
                combos: list[list[str]] = []
                if isinstance(seq_raw, (list, tuple)) and seq_raw:
                    for c in seq_raw:
                        ks = [s.strip() for s in str(c).split("+") if s.strip()]
                        if ks:
                            combos.append(ks)
                else:
                    raw = params.get("keys") if "keys" in params else params.get("combo", "")
                    if isinstance(raw, str):
                        import re as _re
                        keys_list = [s.strip() for s in _re.split(r"[+,]", raw) if s.strip()]
                    else:
                        keys_list = [str(k).strip() for k in (raw or []) if str(k).strip()]
                    if keys_list:
                        combos.append(keys_list)
                if combos:
                    cfx = params.get("click_first_x")
                    cfy = params.get("click_first_y")
                    await loop.run_in_executor(None,
                        functools.partial(wc.send_key_combos, combos,
                                          int(cfx) if cfx is not None else None,
                                          int(cfy) if cfy is not None else None))
        else:
            # ADB actions — real_id를 ADB 시리얼(dev.address)로 변환
            adb_serial = real_id
            if adb_serial:
                dev = self.dm.get_device(adb_serial)
                if dev and dev.type != "adb":
                    raise ValueError(f"Device {adb_serial} is not an ADB device, cannot run {step.type.value}")
                if dev:
                    adb_serial = dev.address  # 커스텀 ID → 실제 ADB 시리얼

            # screen_type은 우리 displays 배열 인덱스(0,1,...) → input -d 용 Android logical ID로 변환
            # 폴더블에서 우리 인덱스와 Android logical ID가 어긋날 수 있어 변환 필수
            our_index = None
            st = step.screen_type or params.get("screen_type")
            if st is not None:
                try:
                    our_index = int(st)
                except (ValueError, TypeError):
                    pass
            # 멀티 디스플레이인데 인덱스 미지정이면 0으로 기본값
            if our_index is None and dev and len(dev.info.get("displays", [])) > 1:
                our_index = 0
            from .adb_service import resolve_input_display_id
            adb_display_id = resolve_input_display_id(dev.info if dev else None, our_index)

            if step.type == StepType.TAP:
                await self.adb.tap(params["x"], params["y"], serial=adb_serial, display_id=adb_display_id)
            elif step.type == StepType.REPEAT_TAP:
                await self.adb.repeat_tap(params["x"], params["y"], int(params.get("count", 5)),
                                          int(params.get("interval_ms", 100)),
                                          serial=adb_serial, display_id=adb_display_id)
            elif step.type == StepType.LONG_PRESS:
                await self.adb.long_press(params["x"], params["y"], params.get("duration_ms", 1000), serial=adb_serial, display_id=adb_display_id)
            elif step.type == StepType.SWIPE:
                pts = params.get("points") or []
                if isinstance(pts, list) and len(pts) >= 2:
                    await self.adb.pattern_swipe(
                        pts, params.get("duration_ms", 600),
                        serial=adb_serial, display_id=adb_display_id,
                    )
                else:
                    await self.adb.swipe(
                        params["x1"], params["y1"],
                        params["x2"], params["y2"],
                        params.get("duration_ms", 300),
                        serial=adb_serial, display_id=adb_display_id,
                    )
            elif step.type == StepType.INPUT_TEXT:
                await self.adb.input_text(params["text"], serial=adb_serial, display_id=adb_display_id)
            elif step.type == StepType.KEY_EVENT:
                await self.adb.key_event(params["keycode"], serial=adb_serial, display_id=adb_display_id)
            elif step.type == StepType.ADB_COMMAND:
                await self.adb.run_shell_command(params["command"], serial=adb_serial)
            elif step.type == StepType.MULTI_TOUCH:
                fingers = params.get("fingers", [])
                is_tap = all(f.get("x1") == f.get("x2") and f.get("y1") == f.get("y2") for f in fingers)
                if is_tap:
                    points = [{"x": f["x1"], "y": f["y1"]} for f in fingers]
                    await self.adb.multi_finger_tap(points, serial=adb_serial, display_id=adb_display_id)
                else:
                    await self.adb.multi_finger_swipe(fingers, params.get("duration_ms", 500), serial=adb_serial, display_id=adb_display_id)

    async def _run_image_tap(self, step: Step, real_id: Optional[str]) -> None:
        """IMAGE_TAP 실행: 현재 화면 캡처 → template_match → 중심 좌표 tap.

        params 에 저장된 template 이미지 파일을 screenshots/{scenario}/ 에서 로드.
        매칭 실패(confidence < similarity) 시 RuntimeError 를 던져 스텝을 FAIL 로 만든다.
        """
        import cv2
        import numpy as np

        params = step.params or {}
        tpl_name = params.get("template")
        scenario_name = getattr(self, "_current_scenario_name", "") or ""
        if not tpl_name or not scenario_name:
            raise RuntimeError("image_tap: template 파일명이 없거나 시나리오 컨텍스트 누락")
        tpl_path = SCREENSHOTS_DIR / scenario_name / tpl_name
        if not tpl_path.exists():
            raise RuntimeError(f"image_tap: template not found: {tpl_path}")

        # 1) 디바이스에서 현재 화면 캡처
        if not real_id:
            raise ValueError("image_tap: device_id 필요")
        dev = self.dm.get_device(real_id)
        if not dev:
            raise ValueError(f"image_tap: device {real_id} not found")
        screen_type = step.screen_type or params.get("screen_type")

        png_bytes: Optional[bytes] = None
        if dev.type == "hkmc_agent":
            svc = self.dm.get_hkmc_service(real_id)
            if not svc:
                raise RuntimeError(f"image_tap: HKMC device {real_id} not connected")
            png_bytes = await svc.async_screencap_bytes(
                screen_type=screen_type or "front_center", fmt="png",
            )
        elif dev.type == "isap_agent":
            svc = self.dm.get_isap_service(real_id)
            if not svc:
                raise RuntimeError(f"image_tap: iSAP device {real_id} not connected")
            png_bytes = await svc.async_screencap_bytes(
                screen_type=screen_type or "front_center", fmt="png",
            )
        elif dev.type == "icas_agent":
            svc = self.dm.get_icas_service(real_id)
            if not svc:
                raise RuntimeError(f"image_tap: ICAS device {real_id} not connected")
            png_bytes = await svc.async_screencap_bytes(
                screen_type=screen_type or "HU", fmt="png",
            )
        elif dev.type == "mib_agent":
            svc = self.dm.get_mib_service(real_id)
            if not svc:
                raise RuntimeError(f"image_tap: MIB device {real_id} not connected")
            png_bytes = await svc.async_screencap_bytes(
                screen_type=screen_type or "HU", fmt="png",
            )
        elif dev.type == "wincontrol":
            wc = self.dm.get_wincontrol_service()
            if not wc.is_attached():
                raise RuntimeError("image_tap: WinControl: no window attached")
            import asyncio as _asyncio
            loop = _asyncio.get_event_loop()
            png_bytes = await loop.run_in_executor(None, wc.capture_window, "png")
        else:
            # ADB
            adb_serial = dev.address or real_id
            from .adb_service import resolve_sf_display_id
            adb_did = None
            try:
                adb_did = int(screen_type) if screen_type is not None else None
            except (ValueError, TypeError):
                adb_did = None
            sf_did = resolve_sf_display_id(dev.info, adb_did)
            png_bytes = await self.adb.screencap_bytes(serial=adb_serial, sf_display_id=sf_did)

        if not png_bytes:
            raise RuntimeError("image_tap: 화면 캡처 실패")

        # 2) template_match (직접 cv2 사용 — confidence 와 좌표 동시 획득)
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        src_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if src_img is None:
            raise RuntimeError("image_tap: 화면 디코드 실패")
        tpl_img = safe_imread(tpl_path, cv2.IMREAD_COLOR)
        if tpl_img is None:
            raise RuntimeError(f"image_tap: template 로드 실패: {tpl_path}")
        src_gray = cv2.cvtColor(src_img, cv2.COLOR_BGR2GRAY)
        tpl_gray = cv2.cvtColor(tpl_img, cv2.COLOR_BGR2GRAY)
        if tpl_gray.shape[0] > src_gray.shape[0] or tpl_gray.shape[1] > src_gray.shape[1]:
            raise RuntimeError("image_tap: template 이 화면보다 큼")
        res = cv2.matchTemplate(src_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        confidence = float(max_val)
        threshold = float(params.get("similarity", 0.85))
        if confidence < threshold:
            raise RuntimeError(
                f"image_tap: template not found (confidence={confidence:.3f} < {threshold:.3f})"
            )
        th, tw = tpl_gray.shape[:2]
        center_x = int(max_loc[0]) + tw // 2
        center_y = int(max_loc[1]) + th // 2
        # HKMC 일체형 표시 보정: 매칭은 front_center(AVN) 캡처의 로컬 좌표계에서
        # 이뤄지지만 실제 터치 좌표계는 x로 x_offset(일체형=1920)만큼 밀려 있다.
        # 수동 탭(프론트엔드)·랜덤입력은 이 오프셋을 더하므로 image_tap도 동일하게 적용.
        # 기본형/비-HKMC는 params에 x_offset이 없어 0 → 무영향.
        x_offset = int(params.get("x_offset", 0) or 0)
        tap_x = center_x + x_offset
        # 이미지 롱터치: params.long_press 가 있으면 tap 대신 long press 실행
        long_press = bool(params.get("long_press"))
        duration_ms = max(1, int(params.get("duration_ms", 3000) or 3000))
        logger.info(
            "image_tap match: tpl=%s confidence=%.3f center=(%d,%d) x_offset=%d tap=(%d,%d) long_press=%s device=%s",
            tpl_name, confidence, center_x, center_y, x_offset, tap_x, center_y, long_press, real_id,
        )

        # 3) 디바이스별 tap / long press 실행
        if dev.type in ("hkmc_agent", "isap_agent"):
            svc = (self.dm.get_isap_service(real_id) if dev.type == "isap_agent"
                   else self.dm.get_hkmc_service(real_id))
            if not svc:
                raise RuntimeError(f"image_tap: agent {real_id} not connected")
            if long_press:
                await svc.async_long_press(tap_x, center_y, duration_ms, screen_type or "front_center")
            else:
                await svc.async_tap(tap_x, center_y, screen_type or "front_center")
        elif dev.type in ("icas_agent", "mib_agent"):
            svc = (self.dm.get_mib_service(real_id) if dev.type == "mib_agent"
                   else self.dm.get_icas_service(real_id))
            if not svc:
                raise RuntimeError(f"image_tap: agent {real_id} not connected")
            if long_press:
                await svc.async_long_press(tap_x, center_y, duration_ms, screen_type or "HU")
            else:
                await svc.async_tap(tap_x, center_y, screen_type or "HU")
        elif dev.type == "wincontrol":
            wc = self.dm.get_wincontrol_service()
            import asyncio as _asyncio, functools as _ft
            loop = _asyncio.get_event_loop()
            if long_press:
                await loop.run_in_executor(
                    None, _ft.partial(wc.send_long_press, tap_x, center_y, duration_ms, "left"),
                )
            else:
                await loop.run_in_executor(
                    None, _ft.partial(wc.send_tap, tap_x, center_y, "left"),
                )
        else:
            # ADB
            adb_serial = dev.address or real_id
            from .adb_service import resolve_input_display_id
            our_index = None
            if screen_type is not None:
                try:
                    our_index = int(screen_type)
                except (ValueError, TypeError):
                    our_index = None
            adb_display_id = resolve_input_display_id(dev.info, our_index)
            if long_press:
                await self.adb.long_press(tap_x, center_y, duration_ms,
                                          serial=adb_serial, display_id=adb_display_id)
            else:
                await self.adb.tap(tap_x, center_y, serial=adb_serial, display_id=adb_display_id)

    def _rel_path(self, abs_path: str, scenario_name: str) -> str:
        """절대 경로 → 웹 서빙용 상대 경로.
        런 폴더 내: /results-files/ 기준, 아닌 경우: /screenshots/ 기준."""
        p = Path(abs_path)
        if self._run_output_dir:
            try:
                return str(p.relative_to(RESULTS_DIR)).replace("\\", "/")
            except ValueError:
                pass
        try:
            return str(p.relative_to(SCREENSHOTS_DIR)).replace("\\", "/")
        except ValueError:
            return p.name

    def _setup_run_output_dir(self, scenario_name: str) -> None:
        """재생 런별 출력 디렉토리 생성: results/{timestamp}_{scenario_name}/

        구조:
          results/{ts}_{name}/
          ├── result.json          ← 결과 JSON
          ├── screenshots/         ← 실제 스크린샷 (직접 저장)
          ├── logs/                ← DLT/Serial 로그
          └── recordings/         ← 동영상 파일
        """
        global _current_run_output_dir
        safe_name = re.sub(r'[\\/:*?"<>|→]', '_', scenario_name).replace(" ", "_")
        folder_name = f"{self._result_timestamp}_{safe_name}"
        run_dir = RESULTS_DIR / folder_name
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "logs").mkdir(exist_ok=True)
        (run_dir / "recordings").mkdir(exist_ok=True)

        self._run_output_dir = run_dir
        _current_run_output_dir = run_dir
        logger.info("Run output dir: %s", run_dir)

    def _cleanup_run_output_dir(self) -> None:
        """재생 종료 시 글로벌 런 디렉토리 참조 해제.
        self._run_output_dir은 _save_result에서 사용하므로 여기서는 유지."""
        global _current_run_output_dir
        _current_run_output_dir = None
        self._result_timestamp = ""

    async def _save_result(self, result: ScenarioResult, interim: bool = False) -> str:
        """Save execution result to JSON + HTML (런 폴더 내 result.json + result.html).
        interim=True: 중간 저장 — _run_output_dir을 유지.

        JSON 직렬화/파일 쓰기/HTML 빌드를 thread로 이전해 event loop 블록을 막는다.
        Excel은 무거우므로 자동 생성하지 않고 /api/results/export에서 on-demand 생성한다.
        HTML이 참조할 Tabulator 라이브러리는 런 폴더의 assets/에 한 번 복사해둔다.
        """
        timestamp = self._result_timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")

        if self._run_output_dir and self._run_output_dir.exists():
            filepath = self._run_output_dir / "result.json"
        else:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            filepath = RESULTS_DIR / f"{result.scenario_name}_{timestamp}.json"

        # model_dump 한 번만 수행하고 JSON/HTML 양쪽에 재사용 (JSON round-trip 제거)
        data = result.model_dump()

        def _write_json_and_html():
            import json as _json
            filepath.write_text(_json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            try:
                from ..routers.results import _build_html_report
                html_path = filepath.with_suffix(".html")
                html_str = _build_html_report(data, html_path)
                html_path.write_text(html_str, encoding="utf-8")
            except Exception as e:
                logger.warning("HTML report generation failed: %s", e)

        await asyncio.to_thread(_write_json_and_html)
        logger.info("Result saved%s: %s", " (interim)" if interim else "", filepath)

        if not interim:
            self._run_output_dir = None
        return str(filepath)


