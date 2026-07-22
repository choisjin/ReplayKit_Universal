"""ReplayKit — FastAPI Backend."""

from __future__ import annotations

# ── DPI 인식 ─────────────────────────────────────────────────────────
# WinControl 캡처/입력이 DPI-aware 타겟 앱과 좌표/크기를 일치시키려면 백엔드도
# Per-Monitor V2 로 동작해야 함. 미설정 시 Windows 가 GetClientRect/PrintWindow
# 결과를 96 DPI 기준으로 가상화 → 캡처가 잘리거나 클릭 좌표가 빗나감.
# 어떤 Win32 API 보다 먼저 호출되어야 하므로 main 모듈 import 의 가장 처음에 실행.
import sys as _sys
if _sys.platform == "win32":
    import ctypes as _ctypes
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4 (Win10 1703+)
        _ctypes.windll.user32.SetProcessDpiAwarenessContext(-4)
    except Exception:
        try:
            # PROCESS_PER_MONITOR_DPI_AWARE = 2 (Win8.1+)
            _ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            try:
                _ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

import asyncio
import base64
import json
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from .routers import compositor as compositor_router, device, dlt as dlt_router, results, scenario, serial_log as serial_log_router, logcat_log as logcat_log_router, settings, webcam, backup as backup_router
from .dependencies import adb_service, device_manager, playback_service, recording_service, monitor_client
from .services.adb_service import resolve_sf_display_id, resolve_input_display_id
# build_dist.py가 배포 시 __init__.py를 빈 파일로 만들기 때문에 서브모듈 직접 import.
from .services.capture.ffmpeg_runtime import log_runtime_status as _log_capture_runtime_status
from .services.capture.scrcpy_server import log_scrcpy_status as _log_scrcpy_status
from .models.scenario import ScenarioResult
from .services.recording_service import GROUP_JUMP_END
from .services.playback_service import (
    RESULTS_DIR as _RESULTS_DIR,
    STEPS_NDJSON_NAME as _STEPS_NDJSON_NAME,
    append_step_ndjson as _append_step_ndjson,
)


def _result_filename(result_path: str) -> str:
    """결과 파일 절대경로 → RESULTS_DIR 기준 상대경로 (예: '20260401_091200_scen/result.json' 또는 'scen_20260401.json')."""
    try:
        return str(Path(result_path).relative_to(_RESULTS_DIR)).replace("\\", "/")
    except ValueError:
        return Path(result_path).name

import os as _os
from logging.handlers import TimedRotatingFileHandler as _TRFH

_log_fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_log_dir = Path(_os.environ.get("RECORDING_PROJECT_ROOT", str(Path(__file__).resolve().parent.parent.parent))) / "logs"
_log_dir.mkdir(exist_ok=True)

_file_handler = _TRFH(
    str(_log_dir / "backend.log"),
    when="midnight", backupCount=7, encoding="utf-8",
)
_file_handler.setFormatter(logging.Formatter(_log_fmt))

logging.basicConfig(level=logging.INFO, format=_log_fmt, handlers=[
    logging.StreamHandler(),  # 콘솔 (런처가 캡처)
    _file_handler,            # 파일 (날짜별 자동 로테이션)
])
logger = logging.getLogger(__name__)

# ── 상태 폴링 access 로그 억제 ──────────────────────────────────────────────
# 프론트/관제가 주기적으로 호출하는 상태 확인용 엔드포인트는 1~10초마다 access
# 로그 한 줄씩을 만들어 백엔드 로그를 도배한다 (health 3s, memory-usage 1s,
# webcam/status 2s, device/list 10s, ui-focus 5s ...). 성공 응답은 아무 정보가
# 없으므로 숨기고, 오류 응답(4xx/5xx)은 문제 신호이므로 그대로 남긴다.
# ⚠️ 새 폴링 엔드포인트를 추가하면 여기에도 등록할 것.
_POLLING_PATHS = (
    "/api/health",
    "/api/device/list",
    "/api/device/wincontrol/status",
    "/api/scenario/record/status",
    "/api/scenario/playback/status",
    "/api/webcam/status",
    "/api/settings/memory-usage",
    "/api/settings/power-status",
    "/api/compositor/status",
    "/api/monitor/ui-focus",
    "/api/dlt/sessions",
)


class _PollingAccessFilter(logging.Filter):
    """uvicorn.access: 상태 폴링 엔드포인트의 성공 응답 로그를 숨긴다."""

    def filter(self, record: logging.LogRecord) -> bool:
        # uvicorn access 로그 args: (client_addr, method, full_path, http_version, status_code)
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) == 5:
                path, status = str(args[2]), int(args[4])
                if status < 400 and (
                    path in _POLLING_PATHS
                    or any(path.startswith(p + "?") for p in _POLLING_PATHS)
                ):
                    return False
        except Exception:
            pass  # 형식이 다르면(uvicorn 버전 차이 등) 그냥 통과 — 로그 유실보다 소음이 낫다
        return True


# uvicorn이 로깅 설정 후 app을 import하므로, 여기서 logger에 붙인 필터가 유지된다.
logging.getLogger("uvicorn.access").addFilter(_PollingAccessFilter())


async def _reconnect_loop():
    """백그라운드: 끊어진 디바이스 주기적 재연결 시도 (5초 간격).
    재생 중에는 상태 확인만 수행 (파괴적 명령 스킵).
    """
    while True:
        await asyncio.sleep(5)
        try:
            await device_manager.reconnect_disconnected(passive=playback_service.is_running)
        except Exception as e:
            logger.debug("Reconnect loop error: %s", e)


# 모듈/함수 사용 통계 캐시 — 서버 기동 시 1회만 계산해 여기에 담아두고,
# 관제 스냅샷(_get_monitor_status)은 이 값을 읽기만 한다(계산 X).
_usage_stats_cache: dict = {"data": None, "ts": 0.0}

# 관제 보고용 디바이스 상태 갱신 스로틀.
# dev.status 는 가만히 두면 갱신되지 않는다 — refresh_adb()/refresh_auxiliary() 가 채워준다
# (/api/device/list 도 10초 스로틀로 같은 일을 한다). 관제는 ReplayKit UI 가 닫혀 있어도
# 최신 연결 상태를 보고해야 하므로 여기서도 같은 주기로 갱신한다.
_device_refresh: dict = {"ts": 0.0}
_DEVICE_REFRESH_INTERVAL = 10.0


async def _refresh_device_status_for_monitor() -> None:
    """관제 보고용 디바이스 상태 갱신 (10초 스로틀).

    ⚠️ 호출부는 재생 중이 아닐 때만 부른다 — adb 재조회가 스텝의 screencap/입력과 경합해
    테스트에 간섭할 수 있다(재연결 루프의 passive 모드와 같은 이유).
    """
    import time as _t
    now = _t.monotonic()
    if now - _device_refresh["ts"] < _DEVICE_REFRESH_INTERVAL:
        return
    _device_refresh["ts"] = now
    try:
        await device_manager.refresh_adb()
    except Exception as e:
        logger.debug("관제 refresh_adb 실패: %s", e)
    try:
        await device_manager.refresh_auxiliary()
    except Exception as e:
        logger.debug("관제 refresh_auxiliary 실패: %s", e)

# ReplayKit UI(브라우저 창) 포커스 상태 — 프론트가 POST /api/monitor/ui-focus 로 보고한다.
# 관제 activity 판정 우선순위: 재생중(playing) > 녹화중(recording) > 창 최상단 포커스(in_use) > 대기(idle).
# 브라우저는 백엔드와 별개 프로세스라 백엔드가 "그 창이 최상단인지"를 직접 알 수 없으므로,
# 창 포커스를 정확히 아는 프론트가 document.hasFocus() 상태를 주기적으로 보고한다.
_ui_focus_state: dict = {"focused": False, "mode": "normal", "page": "", "ts": 0.0}

# 프론트 useTestMode.ts 의 TEST_ONLY_MODULES 와 같은 목록 —
# URL hash `#test` 모드에서만 UI 에 노출되는 실험 모듈. 관제에서 일반 모드 PC 의 목록과
# 구분해 표시하기 위해 디바이스마다 test_only 플래그로 함께 보낸다.
# ⚠️ 프론트 목록이 바뀌면 여기도 같이 고쳐야 한다(게이트 자체는 순수 프론트 구현).
_TEST_ONLY_MODULES = {"Frame_Check"}
# 이 시간(초) 안에 focused=True 보고가 없으면 최상단 아님(대기)으로 간주 —
# 브라우저 탭이 닫히거나 프리즈되면 자동으로 대기로 떨어진다.
_UI_FOCUS_TTL = 12.0


def _ui_report_fresh() -> bool:
    """프론트의 UI 상태 보고가 최근(_UI_FOCUS_TTL 이내)인지 — 브라우저가 떠 있는지 판단."""
    import time as _time
    return (_time.monotonic() - _ui_focus_state.get("ts", 0.0)) <= _UI_FOCUS_TTL


def _ui_recently_focused() -> bool:
    """프론트가 최근(_UI_FOCUS_TTL 이내)에 '창 최상단 포커스' 를 보고했는지."""
    return bool(_ui_focus_state.get("focused")) and _ui_report_fresh()


def _current_ui_state() -> dict:
    """관제에 보낼 현재 UI 모드/페이지. 보고가 끊겼으면(브라우저 닫힘) 빈 값."""
    if not _ui_report_fresh():
        return {"mode": "", "page": ""}
    return {
        "mode": _ui_focus_state.get("mode") or "normal",
        "page": _ui_focus_state.get("page") or "",
    }


def _compact_usage_stats(stats: dict | None) -> dict | None:
    """usage-stats 를 관제 전송용으로 경량화 — 시나리오 이름 배열(무거움)을 제거하고
    카운트만 남긴다. 관제 서버는 카운트를 PC 간 합산하므로 이름 목록은 불필요."""
    if not stats:
        return None
    return {
        "generated_at": stats.get("generated_at"),
        "scenario_count": stats.get("scenario_count", 0),
        "total_steps": stats.get("total_steps", 0),
        "step_types": [
            {"type": s["type"], "count": s["count"], "scenario_count": s["scenario_count"]}
            for s in stats.get("step_types", [])
        ],
        "modules": [
            {
                "module": m["module"], "count": m["count"],
                "scenario_count": m["scenario_count"], "function_count": m["function_count"],
                "functions": [
                    {"function": f["function"], "count": f["count"], "scenario_count": f["scenario_count"]}
                    for f in m["functions"]
                ],
            }
            for m in stats.get("modules", [])
        ],
        "unused_functions": stats.get("unused_functions", []),
        "available_module_count": stats.get("available_module_count", 0),
        "available_function_count": stats.get("available_function_count", 0),
        "used_module_count": stats.get("used_module_count", 0),
        "used_function_count": stats.get("used_function_count", 0),
    }


async def _usage_stats_compute_once():
    """usage-stats 를 서버 기동 후 **1회만** 계산해 캐시에 저장한다.

    시나리오 목록은 실행 중 거의 바뀌지 않는데 전 시나리오 JSON 을 주기적으로 재파싱하는 것은
    (특히 대형 시나리오가 많은 PC 에서) 디스크·CPU 낭비라 기동 시 1회만 계산한다.
    → 관제 서버로 보고되는 함수통계는 **기동 시점 스냅샷**이다. 실행 중 시나리오를 추가/수정했다면
      ReplayKit 을 재시작해야 관제에 반영된다.
      (로컬 `#stats` 페이지는 /api/scenario/usage-stats 가 요청마다 새로 계산하므로 항상 최신)

    ⚠️ 계산은 반드시 스레드로 오프로드 — 2초 관제 콜백이 막히면 대시보드가 '대기'로 굳는다.
    """
    from .routers.scenario import _compute_usage_stats
    import time as _time
    # 서버 안정화 후 계산 (초기 스캔/자동연결 부하와 겹치지 않게 약간 지연)
    await asyncio.sleep(8)
    for attempt in range(1, 4):
        try:
            full = await asyncio.to_thread(_compute_usage_stats)
            _usage_stats_cache["data"] = _compact_usage_stats(full)
            _usage_stats_cache["ts"] = _time.monotonic()
            s = _usage_stats_cache["data"] or {}
            logger.info(
                "usage-stats 계산 완료(기동 1회): 시나리오 %d개, 사용 모듈 %d개, 미사용 함수 %d개",
                s.get("scenario_count", 0), len(s.get("modules", [])),
                len(s.get("unused_functions", [])),
            )
            return
        except Exception as e:
            # 일시적 실패(디스크/모듈 로드)로 함수통계가 영구히 비지 않도록 몇 번만 재시도
            logger.warning("usage-stats 계산 실패 (%d/3): %s", attempt, e)
            await asyncio.sleep(10)
    logger.warning("usage-stats 계산 최종 실패 — 관제 함수통계가 비어 있을 수 있습니다 (재시작 시 재시도)")


async def _get_monitor_status() -> dict:
    """관제 서버에 보낼 현재 상태를 수집."""
    # 활동 상태 판별 — 우선순위: 재생중 > 녹화중 > 창 최상단 포커스(사용중) > 대기
    activity = "idle"
    if playback_service.is_running:
        activity = "playing"
    elif recording_service.is_recording:
        activity = "recording"
    elif _ui_recently_focused():
        activity = "in_use"

    # 디바이스 목록
    # ⚠️ ManagedDevice 에는 is_connected 속성이 없다 — 문자열 status 필드
    #    ("connected"/"disconnected"/"reconnecting")를 쓴다. (과거 dev.is_connected 는
    #    디바이스가 있을 때마다 AttributeError 를 던져 상태 전송을 통째로 막았다.)
    # ⚠️ DeviceManager 에는 list_devices() 가 없다 — list_all() / list_primary() /
    #    list_auxiliary() 뿐이다. (과거 list_devices() 호출이 매번 AttributeError 를 내
    #    상태 전송을 통째로 막았다.)
    # dev.status 는 refresh_* 가 갱신해야 최신이다. 재생 중에는 adb 재조회가 테스트와
    # 경합할 수 있으므로 건너뛰고 직전 상태를 그대로 보고한다.
    if not playback_service.is_running:
        await _refresh_device_status_for_monitor()

    devices = []
    for dev in device_manager.list_all():
        try:
            info = dev.info or {}
            # ⚠️ status 정규화 — refresh_adb() 는 ADB 원시 상태를 그대로 넣는다(연결됨 = "device").
            #    "connected" 만 연결로 보면 ADB 디바이스가 항상 미연결로 표시된다.
            raw_status = dev.status or ""
            status = "connected" if raw_status in ("connected", "device") else raw_status
            devices.append({
                "device_id": dev.id,
                # dev.name 은 ADB 가 보고한 모델명(예: AIVI2_N_FULL). 관제에서는 카탈로그
                # 모델 기준 이름(dev.id = "Europe_New_1")을 쓰므로 참고용으로만 보낸다.
                "name": dev.name or info.get("name") or dev.id,
                "device_model": info.get("device_model") or "",   # 등록 시 선택한 모델 (예: Europe_New)
                # 연결된 모듈명 (CMD/SHELL/OCR/Frame_Check/SmartBench/TH/SCAR 등).
                # Common·OCR·Frame_Check 는 name 이 모두 "Common" 이라 구분이 안 되므로
                # 대시보드가 auxiliary 에 한해 이 값을 우선 표시한다.
                "module": info.get("module") or "",
                "category": dev.category,   # "primary" | "auxiliary"
                "type": dev.type,
                "status": status,
                "raw_status": raw_status,   # 진단용 (device/offline/unauthorized/reconnecting 등)
                # #test 모드에서만 UI 에 노출되는 실험 모듈인지 — 관제에서 구분 표시용
                "test_only": (info.get("module") or dev.id) in _TEST_ONLY_MODULES,
            })
        except Exception:
            continue

    # 재생 진행 상태
    # ⚠️ activity 와 반드시 같은 조건이어야 한다 — 예전엔 activity 는 is_running 만 보고
    #    playback 은 _monitor_state 까지 요구해서, 둘이 어긋나면 관제 카드가
    #    "재생 중" 태그 + "재생 중 아님" 본문으로 모순돼 보였다(그룹 재생 경로).
    #    _monitor_state 가 없어도 is_running 이면 최소 정보라도 채워 모순을 없앤다.
    playback = None
    ms = getattr(playback_service, "_monitor_state", None)
    if playback_service.is_running and not isinstance(ms, dict):
        ms = {"scenario_name": "(정보 없음)"}
    if playback_service.is_running:
        playback = {
            "scenario_name": ms.get("scenario_name", ""),
            "current_cycle": ms.get("current_cycle", 0),
            "total_cycles": ms.get("total_cycles", 0),
            "current_step": ms.get("current_step", 0),
            "total_steps": ms.get("total_steps", 0),
            "status": "paused" if playback_service.is_paused else "running",
            "passed": ms.get("passed", 0),
            "failed": ms.get("failed", 0),
            "warning": ms.get("warning", 0),
            "error": ms.get("error", 0),
        }

    # 시나리오 목록
    try:
        scenarios = await recording_service.list_scenarios()
    except Exception:
        scenarios = []

    # 모듈/함수 사용 통계 — 기동 시 1회(_usage_stats_compute_once) 채운 캐시를 읽기만 한다(계산 X).
    # 여기서 계산하면 2초 status 루프가 막혀 대시보드가 멈춘다.
    # 전송 측(monitor_client)은 generated_at 이 바뀔 때만 실제로 보내므로, 사실상 연결당 1회 전송된다.
    return {
        "activity": activity,
        "devices": devices,
        "playback": playback,
        "scenarios": scenarios,
        "usage_stats": _usage_stats_cache["data"],
        # 현재 UI 모드(#test/#admin/#stats/normal)와 보고 있는 페이지
        "ui": _current_ui_state(),
    }


async def _handle_monitor_command(cmd: dict) -> dict | None:
    """관제 서버에서 수신한 원격 명령 처리."""
    action = cmd.get("action", "")

    if action == "list_scenarios":
        scenarios = await recording_service.list_scenarios()
        return {"action": "list_scenarios", "scenarios": scenarios}

    elif action == "stop":
        if playback_service.is_running:
            await playback_service.stop()
            return {"action": "stop", "result": "ok"}
        return {"action": "stop", "result": "not_running"}

    elif action == "pause":
        if playback_service.is_running:
            await playback_service.pause()
            return {"action": "pause", "result": "ok"}
        return {"action": "pause", "result": "not_running"}

    elif action == "resume":
        if playback_service.is_running:
            await playback_service.resume()
            return {"action": "resume", "result": "ok"}
        return {"action": "resume", "result": "not_running"}

    elif action == "play":
        scenario_name = cmd.get("scenario", "")
        repeat = cmd.get("repeat", 1)
        verify = cmd.get("verify", True)
        if not scenario_name:
            return {"action": "play", "result": "error", "message": "scenario required"}
        if playback_service.is_running:
            return {"action": "play", "result": "error", "message": "already_running"}

        # 비동기로 재생 시작 (백그라운드)
        asyncio.create_task(_remote_play(scenario_name, repeat, verify))
        return {"action": "play", "result": "started", "scenario": scenario_name}

    return None


async def _remote_play(scenario_name: str, repeat: int, verify: bool):
    """원격 재생 명령 실행 (백그라운드)."""
    try:
        logger.info("원격 재생 시작: %s (repeat=%d, verify=%s)", scenario_name, repeat, verify)
        scen = await recording_service.load_scenario(scenario_name)

        # Preflight device check
        preflight_errors = await playback_service.preflight_check(scen)
        if preflight_errors:
            logger.error("원격 재생 preflight 실패: %s", preflight_errors)
            # 에러를 monitor_state에 기록하여 대시보드에서 확인 가능
            playback_service._monitor_state = {
                "scenario_name": scenario_name,
                "total_cycles": repeat, "current_cycle": 0,
                "current_step": 0, "total_steps": len(scen.steps),
                "status": "error",
                "passed": 0, "failed": 0, "warning": 0, "error": 0,
                "error_message": "; ".join(preflight_errors),
            }
            return

        playback_service._should_stop = False
        playback_service._pause_event.set()
        playback_service._monitor_state = {
            "scenario_name": scenario_name,
            "total_cycles": repeat,
            "current_cycle": 0,
            "current_step": 0,
            "total_steps": len(scen.steps),
            "passed": 0, "failed": 0, "warning": 0, "error": 0,
        }

        result = ScenarioResult(
            scenario_name=scenario_name,
            device_serial="multi-device",
            status="pass",
            total_steps=len(scen.steps),
            total_repeat=repeat,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        for iteration in range(1, repeat + 1):
            playback_service._monitor_state["current_cycle"] = iteration
            step_idx = 0
            async for item in playback_service.execute_scenario_stream(scen, verify=verify, repeat_index=iteration):
                if isinstance(item, dict) and item.get("_type") == "step_start":
                    step_idx += 1
                    playback_service._monitor_state["current_step"] = step_idx
                else:
                    step_result = item
                    result.step_results.append(step_result)
                    if step_result.excluded_from_result:
                        pass  # 조건부이동 결과 미반영('분기') — 집계/시나리오 판정에서 제외
                    elif step_result.status == "pass":
                        result.passed_steps += 1
                        playback_service._monitor_state["passed"] += 1
                    elif step_result.status == "fail":
                        result.failed_steps += 1
                        playback_service._monitor_state["failed"] += 1
                    else:
                        result.error_steps += 1
                        playback_service._monitor_state["error"] += 1

            if playback_service._should_stop:
                break

        result.finished_at = datetime.now(timezone.utc).isoformat()
        if result.failed_steps > 0 or result.error_steps > 0:
            result.status = "fail"
        else:
            result.status = "pass"
        await playback_service._save_result(result)
        logger.info("원격 재생 완료: %s → %s", scenario_name, result.status)
    except Exception as e:
        logger.error("원격 재생 오류: %s", e, exc_info=True)
        if hasattr(playback_service, '_monitor_state'):
            playback_service._monitor_state["error_message"] = str(e)
    finally:
        if hasattr(playback_service, '_monitor_state'):
            playback_service._monitor_state["status"] = "idle"


async def _auto_connect_all():
    """서버 시작 후 등록된 모든 디바이스를 백그라운드에서 자동 연결."""
    await asyncio.sleep(2)  # 서버 안정화 대기
    all_devices = device_manager.list_all()
    if not all_devices:
        return
    logger.info("백그라운드 자동 연결 시작: %d개 디바이스", len(all_devices))
    for dev in all_devices:
        try:
            msg = await device_manager.connect_device_by_id(dev.id)
            logger.info("자동 연결: %s", msg)
        except Exception as e:
            logger.debug("자동 연결 실패 %s: %s", dev.id, e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # --- Startup ---
    # Linux .deb 설치본: 실행 시작 시 LGE git(main) 최신 커밋을 확인해 자동 업데이트한다.
    # 업데이트 버튼을 수동으로 누르지 않아도 실행하면 최신 코드가 반영된다. 변경 없음/오프라인이면
    # 그대로 진행. 새 버전을 sync 하면 재시작을 트리거하므로, 그 경우 나머지 startup 은 진행하지
    # 않고 곧 종료될 프로세스를 yield 로 넘긴다(launcher 가 새 코드로 respawn).
    # ※ source clone(.git) 개발 환경에서는 reset --hard 로 작업이 날아가므로 절대 실행하지 않음
    #   (REPLAYKIT_INSTALLED==1 게이트).
    try:
        if _sys.platform != "win32" and _os.environ.get("REPLAYKIT_INSTALLED") == "1":
            from .routers.settings import run_startup_autoupdate
            if await asyncio.to_thread(run_startup_autoupdate):
                logger.info("[startup-update] 자동 업데이트 후 재시작 대기 — 나머지 startup 중단")
                yield
                return
    except Exception as e:
        logger.warning("[startup-update] 자동 업데이트 스킵: %s", e)

    # 라이브 미러링 백엔드 진단 (있으면 INFO, 없으면 INFO/WARNING).
    # 미설치라도 동작에는 영향 없음 — 자동 폴백 체인이 알아서 다음 단계로 넘어간다.
    try:
        _log_capture_runtime_status()
    except Exception as e:
        logger.debug("capture runtime status check: %s", e)
    try:
        _log_scrcpy_status()
    except Exception as e:
        logger.debug("scrcpy status check: %s", e)

    # ADB 서버를 명시적으로 미리 시작 (CREATE_NO_WINDOW 포함)
    # adb가 자체적으로 데몬을 spawn하면 별도 콘솔 창이 생길 수 있으므로 선제 실행
    try:
        await adb_service._run("start-server")
        logger.info("ADB server pre-started")
    except Exception as e:
        logger.debug("ADB server pre-start: %s", e)

    # 이벤트 루프 스톨 감시 — 동기 작업이 루프를 오래 막으면(=/api/health 굶김,
    # "서버 연결 중..." 배너의 원인) 그 시점의 메인 스레드 스택을 로그로 덤프한다.
    try:
        from .services import loop_watchdog
        loop_watchdog.start()
    except Exception as e:
        logger.debug("loop watchdog start: %s", e)

    reconnect_task = asyncio.create_task(_reconnect_loop())

    # 자동 백업 스케줄러 — settings.backup_interval_minutes 주기로 전체 스냅샷 저장.
    from .services import backup_service
    backup_task = asyncio.create_task(backup_service.scheduler_loop())

    # 저장된 SSH 디바이스를 시작 시 자동 재연결 시도 (메모리 전용 연결이므로 재시작 시 복구)
    try:
        ssh_devices = [d for d in device_manager.list_all() if d.type == "ssh"]
        for dev in ssh_devices:
            try:
                msg = await device_manager.connect_device_by_id(dev.id)
                logger.info("SSH auto-reconnect on startup: %s", msg)
            except Exception as e:
                logger.warning("SSH auto-reconnect failed for %s: %s", dev.id, e)
    except Exception as e:
        logger.debug("SSH startup reconnect sweep: %s", e)

    # usage-stats 기동 1회 계산 — 관제 상태 콜백은 계산 없이 이 캐시만 읽는다
    # (2초 status 루프 stall 방지 + 전 시나리오 주기적 재파싱 낭비 제거).
    usage_stats_task = asyncio.create_task(_usage_stats_compute_once())

    # 관제 클라이언트 콜백 항상 등록 (URL은 나중에 Settings에서 설정 가능)
    monitor_client.set_status_callback(_get_monitor_status)
    monitor_client.set_command_callback(_handle_monitor_command)
    try:
        from .routers.settings import _load as _load_settings
        cfg = _load_settings()
        monitor_url = cfg.get("monitor_server_url", "")
        if monitor_url:
            await monitor_client.start(monitor_url)
    except Exception as e:
        logger.debug("Monitor client startup: %s", e)

    yield
    # --- Shutdown ---
    try:
        from .services import loop_watchdog
        loop_watchdog.stop()
    except Exception:
        pass
    await monitor_client.stop()
    reconnect_task.cancel()
    backup_task.cancel()
    usage_stats_task.cancel()
    # 모듈 인스턴스 graceful teardown — SCAR(netns 복원=인터넷/cvd-ebr 정리), TH(게이트웨이/cuttlefish
    # 정리) 등 무거운 모듈이 서버 종료 시 잔류 상태를 남기지 않도록. (재시작 후 stale 상태로 인한
    # "connected 인데 동작 안 함" / FqinAlreadyExists 완화)
    try:
        from .services.module_service import cleanup_active_instances
        summary = await asyncio.to_thread(cleanup_active_instances, "server-shutdown")
        logger.info("Module teardown on shutdown: %s", summary)
    except Exception as e:
        logger.warning("Module teardown on shutdown failed: %s", e)
    logger.info("Closing all serial connections...")
    device_manager.close_all_serial_connections()
    # ADB 서버 kill 전에 장기 adb shell 화면 세션부터 정리
    logger.info("Closing ADB screen streamers...")
    try:
        await adb_service.close_all_streamers()
    except Exception as e:
        logger.debug("close_all_streamers: %s", e)
    try:
        await adb_service.close_all_scrcpy_backends()
    except Exception as e:
        logger.debug("close_all_scrcpy_backends: %s", e)
    logger.info("Killing ADB server...")
    try:
        await adb_service._run("kill-server")
    except Exception as e:
        logger.debug("ADB kill-server: %s", e)


app = FastAPI(
    title="ReplayKit",
    description="녹화(Record) → 재생(Playback) → 검증(Verify) 웹 기반 자동화 도구",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow React dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def _track_inflight_requests(request, call_next):
    """진행 중 요청을 loop_watchdog에 등록 — 스톨 시 범인 엔드포인트 경로를 남긴다.

    /api/health 자신은 추적에서 제외(범인일 리 없고 노이즈만 됨).
    """
    from .services import loop_watchdog

    path = request.url.path
    if path == "/api/health":
        return await call_next(request)
    token = loop_watchdog.request_started(request.method, path)
    try:
        return await call_next(request)
    finally:
        loop_watchdog.request_finished(token)


# Routers
app.include_router(device.router)
app.include_router(scenario.router)
app.include_router(results.router)
app.include_router(settings.router)
app.include_router(webcam.router)
app.include_router(compositor_router.router)
app.include_router(dlt_router.router)
app.include_router(serial_log_router.router)
app.include_router(logcat_log_router.router)
app.include_router(backup_router.router)

# Serve app static assets (Tabulator 등 라이브러리)
_static_dir = Path(__file__).resolve().parent / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")

# Serve screenshots statically
screenshots_dir = Path(__file__).resolve().parent.parent / "screenshots"
screenshots_dir.mkdir(parents=True, exist_ok=True)
app.mount("/screenshots", StaticFiles(directory=str(screenshots_dir)), name="screenshots")

recordings_dir = Path(__file__).resolve().parent.parent.parent / "Results" / "Video"
recordings_dir.mkdir(parents=True, exist_ok=True)
app.mount("/recordings", StaticFiles(directory=str(recordings_dir)), name="recordings")

# 런 폴더 내 파일 접근 (logs, recordings 등)
results_dir = Path(__file__).resolve().parent.parent / "results"
results_dir.mkdir(parents=True, exist_ok=True)
app.mount("/results-files", StaticFiles(directory=str(results_dir)), name="results-files")

# Serve docs (user guide / module guide).
# _PROJECT_ROOT/docs 가 1순위, /opt/ReplayKit/docs 가 fallback (.deb 환경에서 symlink 가
# 깨졌거나 user_data 로 sync 안 된 케이스 방어).
_docs_dir = Path(__file__).resolve().parent.parent.parent / "docs"
if not _docs_dir.is_dir():
    _opt_docs = Path("/opt/ReplayKit/docs")
    if _opt_docs.is_dir():
        _docs_dir = _opt_docs
if _docs_dir.is_dir():
    app.mount("/docs", StaticFiles(directory=str(_docs_dir), html=True), name="docs")
    logger.info("Docs mounted from %s", _docs_dir)
else:
    logger.warning("Docs directory not found — /docs requests will fall through to SPA")


async def _resolve_group_jump_step(scenario_name: str, step_uid: Optional[str]) -> int:
    """그룹 점프의 step_uid 를 대상 시나리오의 0-based 인덱스로 해석.

    step_uid 가 없거나(시나리오 처음부터) 찾지 못하면 0 을 반환한다.
    찾지 못하는 경우는 재생 전 validate_group_jumps 가 미리 안내하므로,
    여기서는 조용히 처음부터 재생하는 쪽으로 폴백한다.
    """
    if not step_uid:
        return 0
    try:
        target = await recording_service.load_scenario(scenario_name)
    except Exception:
        return 0
    for i, st in enumerate(target.steps):
        if st.uid == step_uid:
            return i
    logger.warning(f"그룹 점프 대상 스텝을 찾을 수 없습니다 ({scenario_name} / {step_uid}) — 처음부터 재생")
    return 0


@app.get("/api/health")
async def health_check():
    return {"status": "ok"}


@app.post("/api/monitor/ui-focus")
async def report_ui_focus(body: dict):
    """프론트가 자기 창(브라우저 탭)의 최상단 포커스 여부를 보고한다.

    관제 대시보드의 activity 를 '사용중(in_use)' / '대기(idle)' 로 구분하는 데 쓴다.
    body: {"focused": bool}. 재생/녹화 중에는 이 값과 무관하게 재생중/녹화중이 우선한다.
    """
    import time as _time
    _ui_focus_state["focused"] = bool(body.get("focused"))
    # 모드/페이지 — 관제에서 이 PC 가 지금 어떤 화면·모드인지 표시하는 데 쓴다.
    _ui_focus_state["mode"] = str(body.get("mode") or "normal")
    _ui_focus_state["page"] = str(body.get("page") or "")
    _ui_focus_state["ts"] = _time.monotonic()
    return {"status": "ok"}


# 프로덕션: frontend/dist 정적 파일 서빙 (Vite 빌드 결과)
# 반드시 모든 API 라우트 등록 후 마지막에 추가 (catch-all)
_frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _frontend_dist.is_dir():
    from starlette.responses import FileResponse as _FR

    # 캐시 정책:
    #   - index.html / 루트  → no-cache (매번 서버 확인). git pull 후 사용자가 새 asset
    #     해시 참조하는 새 index.html 을 받도록.
    #   - assets/* (vite 가 content-hash 붙임) → immutable 1년. 파일명이 바뀌면 자연 무효화.
    #   - 기타 정적 → 짧은 no-cache (안전).
    _ASSETS_DIR_NAMES = {"assets"}  # Vite 기본. 다른 자산은 hash 없을 수 있어 no-cache 가 안전.

    def _frontend_cache_headers(file: Path) -> dict[str, str]:
        # 부모 디렉토리가 assets/ 이면 immutable. 그 외 (index.html, *.png 루트 등) 는 no-cache.
        try:
            rel = file.relative_to(_frontend_dist)
        except ValueError:
            return {"Cache-Control": "no-cache, no-store, must-revalidate"}
        if rel.parts and rel.parts[0] in _ASSETS_DIR_NAMES:
            return {"Cache-Control": "public, max-age=31536000, immutable"}
        return {
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        }

    @app.get("/{path:path}")
    async def _serve_frontend(path: str):
        # /docs/* 는 mount 가 실패한 케이스 (예: .deb staging 에 docs 누락) 라도 가능한 대안
        # 경로 (/opt/ReplayKit/docs) 에서 직접 서빙. 안 그러면 SPA index.html 이 떠서 사용자
        # 가이드 버튼이 동작 안 함.
        if path.startswith("docs/"):
            rel = path[len("docs/"):]
            for base in (_docs_dir, Path("/opt/ReplayKit/docs")):
                if base and base.is_dir():
                    cand = base / rel
                    if cand.is_file():
                        return _FR(str(cand))
            # docs 파일 못 찾으면 404 — SPA fallback 으로 흘려보내지 않음
            from fastapi.responses import Response as _Resp
            return _Resp(content=f"Docs file not found: {rel}", status_code=404)

        file = _frontend_dist / path
        if file.is_file():
            return _FR(str(file), headers=_frontend_cache_headers(file))
        # SPA fallback: index.html — 항상 no-cache.
        idx = _frontend_dist / "index.html"
        return _FR(str(idx), headers=_frontend_cache_headers(idx))


@app.get("/")
async def root():
    return {
        "app": "ReplayKit",
        "version": "0.1.0",
        "docs": "/docs",
    }


@app.websocket("/ws/dlt-lifecycle")
async def websocket_dlt_lifecycle(websocket: WebSocket):
    """DLT 세션 시작/종료 이벤트 스트림. (/ws/dlt/{...}보다 먼저 등록해야 path 파라미터가 삼키지 않음)"""
    await dlt_router.ws_dlt_lifecycle(websocket)


@app.websocket("/ws/dlt/{session_id:path}")
async def websocket_dlt_stream(websocket: WebSocket, session_id: str):
    """DLT 로그 실시간 스트리밍 (세션별)."""
    await dlt_router.ws_dlt_stream(websocket, session_id)


@app.websocket("/ws/serial-lifecycle")
async def websocket_serial_lifecycle(websocket: WebSocket):
    """Serial 세션 시작/종료 이벤트 스트림. (/ws/serial-log/{...}보다 먼저 등록)"""
    await serial_log_router.ws_serial_lifecycle(websocket)


@app.websocket("/ws/serial-log/{session_id:path}")
async def websocket_serial_stream(websocket: WebSocket, session_id: str):
    """Serial 로그 실시간 스트리밍 (세션별)."""
    await serial_log_router.ws_serial_stream(websocket, session_id)


@app.websocket("/ws/logcat-lifecycle")
async def websocket_logcat_lifecycle(websocket: WebSocket):
    """Android logcat 세션 시작/종료 이벤트 스트림. (/ws/logcat-log/{...}보다 먼저 등록)"""
    await logcat_log_router.ws_logcat_lifecycle(websocket)


@app.websocket("/ws/logcat-log/{session_id:path}")
async def websocket_logcat_stream(websocket: WebSocket, session_id: str):
    """Android logcat 로그 실시간 스트리밍 (세션별)."""
    await logcat_log_router.ws_logcat_stream(websocket, session_id)


@app.websocket("/ws/screen")
async def websocket_screen_mirror(websocket: WebSocket):
    """WebSocket endpoint for real-time screen mirroring.

    클라이언트가 첫 메시지로 {"device_id": "...", "screen_type": "front_center"} 전송.
    JPEG screencap 기반 화면 스트리밍.
    JPEG 모드: JPEG 프레임 전송 (HKMC, VisionCamera, screencap 폴백)
    """
    await websocket.accept()
    logger.debug("Screen mirror WebSocket connected")

    # 클라이언트로부터 device_id 수신 (선택)
    target_device_id = ""
    screen_type = "front_center"
    force_h264 = False
    # ADB 디바이스에 대한 캡처 fps 제한 (1~30, 기본 1).
    # 제한 없이 screencap을 호출하면 디바이스 SurfaceFlinger/CPU/ADB daemon이
    # 지속 점유되어 폰 전체 반응성이 떨어진다.
    fps = 1
    try:
        init_msg = await asyncio.wait_for(websocket.receive_json(), timeout=2.0)
        target_device_id = init_msg.get("device_id", "")
        screen_type = init_msg.get("screen_type", "front_center")
        force_h264 = init_msg.get("force_h264", False)
        try:
            fps = max(1, min(30, int(init_msg.get("fps", fps))))
        except (TypeError, ValueError):
            pass
    except (asyncio.TimeoutError, Exception):
        pass  # 타임아웃이면 ADB 폴백
    adb_frame_interval = 1.0 / fps

    # 디바이스 타입 판별
    dev = device_manager.get_device(target_device_id) if target_device_id else None
    # HKMC5thWide는 HKMC6th와 동일한 async API라 같은 분기로 처리 — service 인스턴스만 다름.
    is_hkmc = dev and dev.type in ("hkmc_agent", "hkmc5th_wide_agent")
    is_hkmc5th_wide = dev and dev.type == "hkmc5th_wide_agent"
    is_isap = dev and dev.type == "isap_agent"
    is_icas = dev and dev.type == "icas_agent"
    is_mib = dev and dev.type == "mib_agent"
    is_bmw = dev and dev.type == "bmw_agent"
    is_vision_camera = dev and dev.type == "vision_camera"
    is_webcam = dev and dev.type == "webcam"
    is_wincontrol = dev and dev.type == "wincontrol"

    dev_type_label = (
        "hkmc" if is_hkmc else
        ("isap" if is_isap else
         ("icas" if is_icas else
          ("mib" if is_mib else
           ("vision_camera" if is_vision_camera else
            ("webcam" if is_webcam else
             ("wincontrol" if is_wincontrol else "adb")))))))
    logger.debug("Screen mirror: device=%s type=%s", target_device_id, dev_type_label)

    # scrcpy 제거 — 항상 JPEG screencap 사용
    h264_mode = False

    # 클라이언트 disconnect 감시 — 장치 미연결/삭제 대기 분기는 send 없이 sleep만
    # 반복하므로 소켓 끊김을 영원히 감지 못한다(경고 로그 무한 스팸). 백그라운드
    # receive 로 disconnect 프레임을 잡아 루프 종료 신호로 쓴다.
    async def _watch_client_disconnect():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    return
        except Exception:
            return

    recv_task = asyncio.create_task(_watch_client_disconnect())

    # ADB 라이브 미러링 전략:
    #   - 즉시: screencap PNG 폴링으로 화면을 띄움 (사용자 대기시간 0초)
    #   - 백그라운드: scrcpy try_start (~1~12초 소요)
    #   - 준비 완료: 다음 iteration에서 scrcpy stream으로 자연스러운 전환
    #   - 영구 실패: screencap PNG 폴링 유지
    BACKEND_RETRY_COOLDOWN = 30.0
    # scrcpy 가 한 번이라도 됐던 기기(capable)는 스트림 끊김/일시 실패 시 30초나 폴링하지
    # 않고 거의 즉시 scrcpy 로 복귀시킨다. 짧은 쿨다운은 app_process 재spawn thrash(→OOM)
    # 만 막는 최소 간격이며, 그 사이엔 screencap 이 다리 역할만 한다 (눌러앉지 않음).
    BACKEND_RETRY_COOLDOWN_CAPABLE = 2.0
    scrcpy_retry_after = 0.0
    # WS 세션별 백그라운드 scrcpy try_start task와 그 결과 backend
    scrcpy_task: Optional[asyncio.Task] = None
    scrcpy_backend = None
    # 현재 프론트에 통지한 렌더 모드. scrcpy(H.264 relay) ↔ screencap(JPEG) 전환 시에만
    # mode 메시지를 보내 프론트가 JMuxer ↔ <img> 경로를 올바르게 토글하도록 한다.
    # 초기값은 아래 send_json({"mode":"jpeg"})와 일치시킨다.
    current_ws_mode = "jpeg"
    # 이 WS 세션이 scrcpy를 시도/점유한 디바이스 serial. disconnect 시 정확히 이
    # serial의 backend만 정리하기 위해 세션 스코프로 보관 (finally에서 adb_serial은
    # ADB 분기 로컬이라 unbound일 수 있음).
    scrcpy_serial: Optional[str] = None
    # WS 세션 진입 시 ADB 분기에 한 번만 dispatch 의도를 INFO 로그로 출력
    adb_dispatch_logged = False

    # ── MIB/ICAS 적응형 화면 리프레시 페이싱 ──
    # SSH+scp 캡처(LayerManagerControl dump)는 디바이스 부하가 커서 무한 폴링하면
    # /tmp 압박·PNG truncation·반응성 저하를 악화시킨다. 그래서:
    #   - 입력(터치/하드키)이 없으면: 10초에 한 번만 갱신 (idle)
    #   - 입력이 있으면: 2초 간격으로 5회 갱신(burst) 후 다시 idle로 복귀
    # 입력 감지는 서비스의 last_input_ts(_ksend에서 갱신)를 폴링해 판단하며,
    # idle 대기 중 입력이 들어오면 즉시 깨어나 burst로 전환한다.
    SSH_REFRESH_IDLE_S = 10.0
    SSH_REFRESH_BURST_S = 2.0
    SSH_REFRESH_BURST_N = 5
    # 최초엔 burst로 시작 — 연결 직후 초기 화면을 빠르게 채운 뒤 idle로 가라앉는다.
    _ssh_burst_remaining = SSH_REFRESH_BURST_N
    _ssh_seen_input_ts = -1.0
    # MIB 라이브 스트리밍: 마지막으로 보낸 프레임 id (중복 송신 방지)
    live_last_frame_id = -1
    # not-ready 경고(웹캠/비전캠) 쓰로틀 — 0.3s 루프에서 매번 찍으면 로그 스팸
    _not_ready_last_warn = 0.0
    # BMW 스크린세이버(대기화면) 라벨 상태 — 변할 때만 screen_state 메시지 송신
    bmw_last_ss: Optional[bool] = None

    async def _adaptive_ssh_pace(svc) -> None:
        """방금 1프레임을 보낸 뒤 다음 캡처까지 입력 유무에 따라 대기.

        새 입력이 감지되면 burst(2초×5회)를 (재)시작하고, idle 대기 중에도 입력이 들어오면
        0.2초 안에 깨어나 즉시 burst로 전환한다.
        """
        nonlocal _ssh_burst_remaining, _ssh_seen_input_ts
        cur_ts = getattr(svc, "last_input_ts", 0.0)
        if cur_ts > _ssh_seen_input_ts:
            _ssh_seen_input_ts = cur_ts
            _ssh_burst_remaining = SSH_REFRESH_BURST_N
        if _ssh_burst_remaining > 0:
            _ssh_burst_remaining -= 1
            interval = SSH_REFRESH_BURST_S
        else:
            interval = SSH_REFRESH_IDLE_S
        waited = 0.0
        step = 0.2
        while waited < interval:
            await asyncio.sleep(step)
            waited += step
            if getattr(svc, "last_input_ts", 0.0) > _ssh_seen_input_ts:
                return  # 새 입력 → 즉시 다음 캡처로 (다음 호출에서 burst 재시작)

    try:
        await websocket.send_json({"mode": "jpeg"})

        while True:
            # 클라이언트가 끊겼으면(대기 분기는 send 부재로 자체 감지 불가) 즉시 종료.
            if recv_task.done():
                logger.info("Screen mirror client disconnected (recv watcher)")
                break
            # 관리 목록에서 디바이스가 삭제됐으면 스트림 종료 — 아니면 not-ready
            # 분기가 존재하지 않는 장치를 상대로 영원히 돈다.
            if dev is not None and device_manager.get_device(target_device_id) is None:
                logger.info(
                    "Screen mirror: device %s removed — closing stream",
                    target_device_id,
                )
                break
            try:
                # 매 프레임마다 최신 서비스 인스턴스 조회 (재연결 대응)
                # HKMC5thWide 디바이스는 별도 service에서 조회 — 같은 _label/분기로 처리.
                if is_hkmc5th_wide:
                    hkmc = device_manager.get_hkmc5th_wide_service(target_device_id)
                elif is_hkmc:
                    hkmc = device_manager.get_hkmc_service(target_device_id)
                else:
                    hkmc = None
                isap = device_manager.get_isap_service(target_device_id) if is_isap else None
                if hkmc and hkmc.is_connected:
                    _cap_kwargs = {"screen_type": screen_type, "fmt": "jpeg", "timeout": 3.0}
                    # HKMC6th cluster 2-레이어 합성: 라이브 토글(cluster_composite_live)을 존중.
                    # 속성이 있는 6th 서비스만 composite 인자를 전달(5thWide는 미지원).
                    if hasattr(hkmc, "cluster_composite_live"):
                        _cap_kwargs["composite"] = hkmc.cluster_composite_live
                    jpeg_bytes = await hkmc.async_screencap_bytes(**_cap_kwargs)
                    await websocket.send_bytes(jpeg_bytes)
                elif is_hkmc:
                    # HKMC 재연결 대기 중 — 빈 프레임 대신 잠시 대기
                    await asyncio.sleep(0.3)
                    continue
                elif isap and isap.is_connected:
                    jpeg_bytes = await isap.async_screencap_bytes(
                        screen_type=screen_type, fmt="jpeg", timeout=3.0
                    )
                    await websocket.send_bytes(jpeg_bytes)
                elif is_isap:
                    await asyncio.sleep(0.3)
                    continue
                elif is_icas:
                    icas = device_manager.get_icas_service(target_device_id)
                    if icas and icas.is_connected:
                        # 라이브 스트리밍(surface 합성) — MIB과 동일. 프레임당 SCP/페이싱 제거.
                        # 스트리머는 HU(screen 0)만 합성 → IID/HUD는 기존 screencap 경로로.
                        _live_hu = screen_type in (None, "", "HU")
                        if _live_hu and not icas.is_live_running():
                            started = await icas.async_start_live_stream()
                            if started:
                                live_last_frame_id = -1
                        if _live_hu and icas.is_live_running():
                            try:
                                jpeg_bytes, fid = icas.get_live_frame()
                                if jpeg_bytes is not None and fid != live_last_frame_id:
                                    live_last_frame_id = fid
                                    await websocket.send_bytes(jpeg_bytes)
                                    await asyncio.sleep(0.015)
                                else:
                                    await asyncio.sleep(0.03)
                                continue
                            except WebSocketDisconnect:
                                break
                            except Exception as ce:
                                cls_name = type(ce).__name__
                                if cls_name in ("ClientDisconnected", "ConnectionClosed",
                                                "ConnectionClosedOK", "ConnectionClosedError"):
                                    break
                                logger.warning("ICAS live send error: type=%s repr=%r", cls_name, ce)
                                await asyncio.sleep(0.3)
                                continue
                        # ── 레거시 폴백: LayerManagerControl dump + scp (스트림 불가 환경) ──
                        try:
                            jpeg_bytes = await icas.async_screencap_bytes(
                                screen_type=screen_type, fmt="jpeg"
                            )
                            await websocket.send_bytes(jpeg_bytes)
                            await _adaptive_ssh_pace(icas)
                            continue
                        except Exception as ce:
                            msg = str(ce) or type(ce).__name__
                            logger.warning("ICAS capture error (%s): %s (%s)", screen_type, msg, type(ce).__name__)
                            await asyncio.sleep(0.5)
                            continue
                    else:
                        await asyncio.sleep(0.3)
                        continue
                elif is_mib:
                    mib = device_manager.get_mib_service(target_device_id)
                    if mib and mib.is_connected:
                        # 라이브 스트리밍 경로: surface 합성 스트리머(device python)에서
                        # 최신 프레임을 받아 보낸다. 프레임당 SCP/재인증·페이싱 없음.
                        # 스트림이 죽었으면(첫 진입 포함) 재기동 시도, 실패 시 레거시 폴백.
                        # 스트리머는 HU(screen 0)만 합성 → IID/HUD는 기존 screencap 경로로.
                        _live_hu = screen_type in (None, "", "HU")
                        if _live_hu and not mib.is_live_running():
                            started = await mib.async_start_live_stream()
                            if started:
                                live_last_frame_id = -1
                        if _live_hu and mib.is_live_running():
                            try:
                                jpeg_bytes, fid = mib.get_live_frame()
                                if jpeg_bytes is not None and fid != live_last_frame_id:
                                    live_last_frame_id = fid
                                    await websocket.send_bytes(jpeg_bytes)
                                    await asyncio.sleep(0.015)
                                else:
                                    # 새 프레임 대기 (device ~6-10fps)
                                    await asyncio.sleep(0.03)
                                continue
                            except WebSocketDisconnect:
                                break
                            except Exception as ce:
                                cls_name = type(ce).__name__
                                if cls_name in ("ClientDisconnected", "ConnectionClosed",
                                                "ConnectionClosedOK", "ConnectionClosedError"):
                                    break
                                logger.warning("MIB live send error: type=%s repr=%r",
                                               cls_name, ce)
                                await asyncio.sleep(0.3)
                                continue
                        # ── 레거시 폴백: LayerManagerControl dump + scp (스트림 불가 환경) ──
                        try:
                            jpeg_bytes = await mib.async_screencap_bytes(
                                screen_type=screen_type, fmt="jpeg"
                            )
                            await websocket.send_bytes(jpeg_bytes)
                            await _adaptive_ssh_pace(mib)
                            continue
                        except WebSocketDisconnect:
                            break
                        except Exception as ce:
                            cls_name = type(ce).__name__
                            if cls_name in ("ClientDisconnected", "ConnectionClosed",
                                            "ConnectionClosedOK", "ConnectionClosedError"):
                                break
                            logger.warning(
                                "MIB capture error (%s): type=%s repr=%r",
                                screen_type, cls_name, ce,
                            )
                            await asyncio.sleep(0.5)
                            continue
                    else:
                        await asyncio.sleep(0.3)
                        continue
                elif is_bmw:
                    bmw = device_manager.get_bmw_service(target_device_id)
                    if bmw and bmw.is_connected:
                        # 스크린세이버(대기화면) 상태 — 내부 1s 캐시라 매 루프 호출해도 저렴.
                        # 변할 때만 라벨용 screen_state 메시지 송신(라이브/폴백 경로 공통).
                        # 현재 보고 있는 화면(screen_type) 기준으로 판별.
                        try:
                            ss_now = await bmw.async_screensaver_active(screen_type)
                            if ss_now != bmw_last_ss:
                                bmw_last_ss = ss_now
                                await websocket.send_json(
                                    {"type": "screen_state", "screensaver": ss_now}
                                )
                        except WebSocketDisconnect:
                            break
                        except Exception:
                            pass
                        # device-side 스트리머(host python) 우선 — 프레임당 adb 스폰/왕복 제거.
                        # 스트림이 죽었거나 screen 이 바뀌었으면 (재)기동. 실패 시 단발 캡처 폴백.
                        # 후석 듀얼은 screen 별 독립 스트림 — 이 WS 의 screen 만 다룬다.
                        if not bmw.is_live_running(screen_type):
                            started = await bmw.async_start_live_stream(screen_type)
                            if started:
                                live_last_frame_id = -1
                        if bmw.is_live_running(screen_type):
                            try:
                                jpeg_bytes, fid = bmw.get_live_frame(screen_type)
                                if jpeg_bytes is not None and fid != live_last_frame_id:
                                    live_last_frame_id = fid
                                    await websocket.send_bytes(jpeg_bytes)
                                    await asyncio.sleep(0.015)
                                else:
                                    await asyncio.sleep(0.03)
                                continue
                            except WebSocketDisconnect:
                                break
                            except Exception as ce:
                                cls_name = type(ce).__name__
                                if cls_name in ("ClientDisconnected", "ConnectionClosed",
                                                "ConnectionClosedOK", "ConnectionClosedError"):
                                    break
                                logger.warning("BMW live send error: type=%s repr=%r", cls_name, ce)
                                await asyncio.sleep(0.3)
                                continue
                        # 폴백: 단발 exec-out 캡처 (스트리머 불가 환경)
                        try:
                            jpeg_bytes = await bmw.async_screencap_bytes(
                                screen_type=screen_type, fmt="jpeg"
                            )
                            await websocket.send_bytes(jpeg_bytes)
                            await asyncio.sleep(0.05)
                            continue
                        except WebSocketDisconnect:
                            break
                        except Exception as ce:
                            cls_name = type(ce).__name__
                            if cls_name in ("ClientDisconnected", "ConnectionClosed",
                                            "ConnectionClosedOK", "ConnectionClosedError"):
                                break
                            logger.warning(
                                "BMW capture error (%s): type=%s repr=%r",
                                screen_type, cls_name, ce,
                            )
                            await asyncio.sleep(0.5)
                            continue
                    else:
                        await asyncio.sleep(0.3)
                        continue
                elif is_vision_camera:
                    cam = device_manager.get_vision_camera(target_device_id)
                    if cam and cam.IsConnected():
                        try:
                            loop = asyncio.get_event_loop()
                            jpeg_bytes = await loop.run_in_executor(
                                None, cam.CaptureBytes, "jpeg"
                            )
                            logger.debug("VisionCam frame: %d bytes", len(jpeg_bytes))
                            await websocket.send_bytes(jpeg_bytes)
                        except RuntimeError as ve:
                            if "No frame available" in str(ve):
                                logger.debug("VisionCamera: waiting for first frame...")
                            else:
                                logger.error("VisionCamera capture error: %s", ve)
                            await asyncio.sleep(0.3)
                            continue
                    else:
                        _now = asyncio.get_event_loop().time()
                        if _now - _not_ready_last_warn >= 5.0:
                            _not_ready_last_warn = _now
                            logger.warning(
                                "VisionCam not ready: cam=%s connected=%s",
                                cam is not None,
                                cam.IsConnected() if cam else "no_cam",
                            )
                        await asyncio.sleep(0.3)
                        continue
                elif is_webcam:
                    cam = device_manager.get_webcam_device(target_device_id)
                    if cam and cam.IsConnected():
                        try:
                            loop = asyncio.get_event_loop()
                            jpeg_bytes = await loop.run_in_executor(
                                None, cam.CaptureBytes, "jpeg"
                            )
                            await websocket.send_bytes(jpeg_bytes)
                        except RuntimeError as we:
                            logger.debug("Webcam capture error: %s", we)
                            await asyncio.sleep(0.3)
                            continue
                    else:
                        _now = asyncio.get_event_loop().time()
                        if _now - _not_ready_last_warn >= 5.0:
                            _not_ready_last_warn = _now
                            logger.warning(
                                "Webcam not ready: cam=%s connected=%s",
                                cam is not None,
                                cam.IsConnected() if cam else "no_cam",
                            )
                        await asyncio.sleep(0.3)
                        continue
                elif is_wincontrol:
                    wc = device_manager.get_wincontrol_service()
                    if wc.is_attached():
                        try:
                            loop = asyncio.get_event_loop()
                            jpeg_bytes = await loop.run_in_executor(
                                None, wc.capture_window, "jpeg",
                            )
                            await websocket.send_bytes(jpeg_bytes)
                        except Exception as we:
                            logger.debug("WinControl capture error: %s", we)
                            await asyncio.sleep(0.3)
                            continue
                    else:
                        # 임베드 전 — 빈 프레임 대신 polling
                        await asyncio.sleep(0.3)
                        continue
                else:
                    # ADB 라이브 미러링 — 2단계 폴백 체인 (screenrecord 제거)
                    #   1순위: scrcpy-server (MediaCodec 직접 제어, idle 문제 없음)
                    #   2순위: screencap PNG streamer — 1~5fps, 모든 환경에서 동작
                    # 검증/녹화 캡처(screencap_bytes)와는 별개 채널.
                    adb_display_id = None
                    try:
                        adb_display_id = int(screen_type)
                    except (ValueError, TypeError):
                        pass
                    adb_serial = dev.address if dev else target_device_id
                    if not adb_serial:
                        await asyncio.sleep(0.3)
                        continue

                    # 디스플레이 활성 여부 결정:
                    #   * 폴더블처럼 "일부만" inactive면 그건 신뢰 가능한 정보 → 차단
                    #   * GVM/IVI 환경처럼 "전체" inactive면 dumpsys 정보가 신뢰 불가
                    #     (실제로는 active인데 viewport API가 false 반환). 무시하고 시도.
                    # Cython 호환성을 위해 generator expression 대신 명시적 loop 사용.
                    if dev and dev.info:
                        _displays = dev.info.get("displays", [])
                    else:
                        _displays = []
                    _all_inactive = False
                    if _displays:
                        _all_inactive = True
                        for _d in _displays:
                            if _d.get("is_active") is not False:
                                _all_inactive = False
                                break
                    _is_active = True
                    if _displays and not _all_inactive:
                        for _d in _displays:
                            if _d.get("id") == adb_display_id and _d.get("is_active") is False:
                                _is_active = False
                                break
                    _logical_id = resolve_input_display_id(
                        dev.info if dev else None, adb_display_id
                    )

                    _now = asyncio.get_event_loop().time()
                    _scrcpy_disabled = adb_service.is_scrcpy_disabled(adb_serial)

                    if not adb_dispatch_logged:
                        _disp_summary = []
                        for _d in _displays:
                            _disp_summary.append({
                                "id": _d.get("id"),
                                "active": _d.get("is_active"),
                                "lid": _d.get("logical_id"),
                            })
                        logger.info(
                            "ADB mirror dispatch: serial=%s screen_type=%r display_id=%s "
                            "logical_id=%s is_active=%s all_inactive_override=%s "
                            "scrcpy_disabled=%s displays=%s",
                            adb_serial, screen_type, adb_display_id, _logical_id,
                            _is_active, _all_inactive,
                            _scrcpy_disabled, _disp_summary,
                        )
                        adb_dispatch_logged = True

                    # ──────────────────────────────────────────────────────────────
                    # 백그라운드 scrcpy try_start — main loop는 차단되지 않음.
                    # 첫 iteration에서 task 시작, 이후 매번 done 여부만 확인.
                    # ──────────────────────────────────────────────────────────────
                    # 쿨다운은 세션 로컬 + serial 단위(서비스) 중 더 늦은 시각을 적용.
                    # 재연결로 세션이 새로 떠도 serial 단위 쿨다운이 유지돼 thrash 방지.
                    _eff_retry_after = max(
                        scrcpy_retry_after,
                        adb_service.get_scrcpy_retry_after(adb_serial),
                    )
                    # 시나리오 재생 중에는 scrcpy를 띄우지 않는다 — 미러는 프론트에서
                    # 중단되며(부하 감소), 재생 중 spawn은 인코더 churn + 재생 종료 후
                    # stale 쿨다운으로 복귀 지연만 유발한다.
                    if (not _scrcpy_disabled and _is_active and _now >= _eff_retry_after
                            and not playback_service.is_running
                            and scrcpy_task is None and scrcpy_backend is None):
                        scrcpy_serial = adb_serial
                        scrcpy_task = asyncio.create_task(
                            adb_service.ensure_scrcpy_backend(adb_serial, _logical_id),
                        )
                        logger.info(
                            "scrcpy try_start dispatched in background (serial=%s display=%s) — "
                            "screencap polling will serve frames until ready",
                            adb_serial, adb_display_id,
                        )

                    # 백그라운드 task 완료 처리
                    if scrcpy_task is not None and scrcpy_task.done():
                        try:
                            scrcpy_backend = scrcpy_task.result()
                        except Exception as e:
                            logger.warning("scrcpy try_start error (%s): %s", adb_serial, e)
                            scrcpy_backend = None
                        scrcpy_task = None
                        if scrcpy_backend is None:
                            # 영구 disable은 ADBService 내부 카운터가 임계치 도달 시 자동 처리.
                            # 단, scrcpy 가 한 번이라도 됐던 기기(capable)는 영구 disable 되지
                            # 않고(서비스가 보장), 짧은 쿨다운으로 즉시 scrcpy 재시도한다.
                            _scrcpy_disabled = adb_service.is_scrcpy_disabled(adb_serial)
                            _capable = adb_service.is_scrcpy_capable(adb_serial)
                            if _scrcpy_disabled:
                                scrcpy_retry_after = float("inf")
                                logger.info(
                                    "scrcpy unavailable for %s (display=%s) — "
                                    "screencap PNG polling will be used permanently",
                                    adb_serial, adb_display_id,
                                )
                            else:
                                _cd = (
                                    BACKEND_RETRY_COOLDOWN_CAPABLE if _capable
                                    else BACKEND_RETRY_COOLDOWN
                                )
                                scrcpy_retry_after = (
                                    asyncio.get_event_loop().time() + _cd
                                )
                                adb_service.set_scrcpy_retry_after(
                                    adb_serial, scrcpy_retry_after,
                                )
                                logger.info(
                                    "scrcpy try_start failed for %s — "
                                    "will retry in %.0fs (%s)",
                                    adb_serial, _cd,
                                    "scrcpy-capable, brief screencap bridge" if _capable
                                    else "screencap PNG polling meanwhile",
                                )

                    # scrcpy 준비됨 → H.264 relay stream 진입 (실패/종료 시 다시 폴링으로 복귀)
                    if scrcpy_backend is not None:
                        # 프론트에 H.264 모드 통지 (JMuxer 초기화 트리거). raw NAL 을 그대로
                        # relay 하므로 브라우저가 GPU 로 디코딩한다 (PC 트랜스코딩 없음).
                        if current_ws_mode != "h264":
                            await websocket.send_json({
                                "mode": "h264",
                                "width": scrcpy_backend.video_width or 1080,
                                "height": scrcpy_backend.video_height or 1920,
                            })
                            current_ws_mode = "h264"
                        # async for 가 정상 종료하면 stream_h264 가 EOF sentinel 로 끝난
                        # 것 = scrcpy 소켓 EOF(서버 실제 종료) → 백엔드 정리 후 재시작 경로.
                        # send_bytes 가 WS/클라이언트 종료 예외를 던지면 그건 "장치 전환 등
                        # 으로 이 WS 만 닫힌 것"이라 백엔드를 죽이지 않고 그대로 유지한다
                        # (정식 scrcpy 처럼 연결 유지 → 복귀 시 살아있는 스트림 즉시 재사용).
                        scrcpy_dead = False
                        try:
                            async for nal in scrcpy_backend.stream_h264():
                                await websocket.send_bytes(nal)
                            scrcpy_dead = True  # 정상 종료 = scrcpy 소켓 EOF
                        except WebSocketDisconnect:
                            raise
                        except Exception as e:
                            cls_name = type(e).__name__
                            if cls_name in (
                                "ClientDisconnected", "ConnectionClosed",
                                "ConnectionClosedOK", "ConnectionClosedError",
                                "WebSocketDisconnect", "RuntimeError",
                                "ScrcpySuperseded",
                            ):
                                # WS(클라이언트) 종료 또는 더 새로운 소비자에게 양보(전환).
                                # 어느 쪽이든 공유 백엔드는 살려두고 이 WS 핸들러만 종료한다.
                                raise
                            # 진짜 scrcpy 스트림 오류 — 백엔드 정리 후 재시작.
                            logger.warning(
                                "scrcpy stream error (%s): type=%s repr=%r "
                                "chunks=%d bytes_in=%d",
                                adb_serial, cls_name, e,
                                getattr(scrcpy_backend, "_total_frames_decoded", -1),
                                getattr(scrcpy_backend, "_total_bytes_in", -1),
                            )
                            scrcpy_dead = True
                        if scrcpy_dead:
                            # scrcpy 가능 기기는 짧은 쿨다운으로 즉시 재시작(장기 폴링 금지).
                            if not playback_service.is_running:
                                _cd = (
                                    BACKEND_RETRY_COOLDOWN_CAPABLE
                                    if adb_service.is_scrcpy_capable(adb_serial)
                                    else BACKEND_RETRY_COOLDOWN
                                )
                                scrcpy_retry_after = (
                                    asyncio.get_event_loop().time() + _cd
                                )
                                adb_service.set_scrcpy_retry_after(
                                    adb_serial, scrcpy_retry_after,
                                )
                            await adb_service.close_scrcpy_backend(
                                adb_serial, expected=scrcpy_backend,
                            )
                            scrcpy_backend = None
                        continue

                    # 폴백/대기: screencap PNG streamer + fps throttle.
                    # scrcpy 준비 중이거나 영구 비활성일 때 사용자가 즉시 화면을 볼 수 있게.
                    sf_did = resolve_sf_display_id(
                        dev.info if dev else None, adb_display_id
                    )
                    _interval = adb_frame_interval
                    if _scrcpy_disabled:
                        # 영구 폴백 — 5fps 정도가 부드러움/부하의 균형점.
                        _interval = min(_interval, 0.2)
                    loop = asyncio.get_event_loop()
                    frame_t0 = loop.time()
                    jpeg_bytes = await adb_service.streaming_screencap_bytes(
                        serial=adb_serial, fmt="jpeg", sf_display_id=sf_did,
                    )
                    if jpeg_bytes:
                        # scrcpy(H.264)에서 screencap(JPEG) 폴백으로 전환 시 프론트에 통지해
                        # JMuxer 경로를 닫고 <img> 경로로 되돌린다.
                        if current_ws_mode != "jpeg":
                            await websocket.send_json({"mode": "jpeg"})
                            current_ws_mode = "jpeg"
                        await websocket.send_bytes(jpeg_bytes)
                    frame_elapsed = loop.time() - frame_t0
                    sleep_s = _interval - frame_elapsed
                    if sleep_s > 0:
                        await asyncio.sleep(sleep_s)
            except WebSocketDisconnect:
                raise
            except Exception as e:
                try:
                    await websocket.send_json({
                        "type": "error",
                        "message": str(e),
                    })
                except Exception:
                    # 클라이언트 이미 끊김 — 루프 탈출
                    break
                await asyncio.sleep(0.3)
                continue
            await asyncio.sleep(0)  # 이벤트 루프 양보 (각 소스가 자체 속도로 전송)
    except (WebSocketDisconnect, Exception) as exc:
        if isinstance(exc, WebSocketDisconnect):
            logger.info("Screen mirror WebSocket disconnected")
        else:
            logger.warning("Screen mirror WebSocket error: %s", exc)
    finally:
        if recv_task and not recv_task.done():
            recv_task.cancel()
        # scrcpy 백엔드는 WS 종료(주로 장치 전환)만으로는 닫지 않는다 — 정식 scrcpy 처럼
        # 연결을 유지해, 전환 후 돌아오면 살아있는 스트림을 그대로 재사용(재시작 갭 0).
        # 실제 정리 책임은 두 곳으로 이관했다:
        #   (1) 장치 분리/명시적 disconnect → device_manager 가 close_scrcpy_backend
        #   (2) 일정 시간 아무 WS 도 소비하지 않는 백엔드 → adb_service idle reaper
        # 단, "아직 첫 프레임 전인 진행 중 try_start" 만 orphan 방지로 취소한다. 이미
        # 성공해 캐시된 백엔드(_scrcpy_backends)는 살려둔다(reaper/장치분리가 관리).
        if scrcpy_task is not None and not scrcpy_task.done():
            scrcpy_task.cancel()
            try:
                await scrcpy_task
            except (asyncio.CancelledError, Exception):
                pass
        # MIB 라이브 스트림(전용 SSH 채널 + 리더 스레드) 정리 — 미종료 시 device
        # python 스트리머가 살아남아 surface dump를 계속 돈다.
        if is_mib:
            _mib = device_manager.get_mib_service(target_device_id)
            if _mib is not None and _mib.is_live_running():
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, _mib.stop_live_stream
                    )
                except Exception as e:
                    logger.debug("MIB live stream stop on disconnect failed: %s", e)
        if is_icas:
            _icas = device_manager.get_icas_service(target_device_id)
            if _icas is not None and _icas.is_live_running():
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, _icas.stop_live_stream
                    )
                except Exception as e:
                    logger.debug("ICAS live stream stop on disconnect failed: %s", e)
        # BMW 라이브 스트림(host python 스트리머 + exec-out 파이프) 정리.
        if is_bmw:
            _bmw = device_manager.get_bmw_service(target_device_id)
            # 이 WS 가 담당하던 screen 의 스트림만 정리(듀얼의 다른 화면 WS 는 유지).
            if _bmw is not None and _bmw.is_live_running(screen_type):
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, _bmw.stop_live_stream, screen_type
                    )
                except Exception as e:
                    logger.debug("BMW live stream stop on disconnect failed: %s", e)


# 현재 백그라운드 재생 태스크 (단일 재생만 허용)
_playback_bg_task: asyncio.Task | None = None


class _WebcamPlaybackSession:
    """재생 1회 동안의 웹캠 녹화 컨텍스트.

    멀티 사이클 시 cycle별로 stop+start 하면서 임시 파일들을 누적했다가
    재생 종료 시 결과 폴더의 recordings/ 안으로 일괄 이동한다.

    cycle_files 항목은 (iteration, path, started_at_iso) 형태로 저장되며,
    started_at_iso는 해당 cycle 녹화가 실제로 시작된 wall-clock 시각이다.
    프론트엔드에서 step → 비디오 시간 매핑(스킵 점프)에 사용된다.
    """
    def __init__(self) -> None:
        self.temp_dir: Optional[Path] = None
        # (iteration, finalized path, started_at ISO string)
        self.cycle_files: list[tuple[int, Path, str]] = []
        self.current_cycle: int = 0
        self.current_path: Optional[Path] = None
        self.current_started_at: Optional[str] = None
        # "webcam" (단일 카메라 — 기본) | "compositor" (다중 소스 합성)
        self.kind: str = "webcam"

    def is_active(self) -> bool:
        return self.temp_dir is not None


def _file_prefix_for_kind(kind: str) -> str:
    """결과 파일명 prefix — 호환성을 위해 webcam은 'webcam_r', compositor는 'composite_r'.

    프론트의 results recordings 목록은 *.mp4를 모두 노출하므로 두 종류 모두 표시된다.
    """
    return "composite_r" if kind == "compositor" else "webcam_r"


async def _compositor_session_start(iteration: int = 1) -> Optional[_WebcamPlaybackSession]:
    """Compositor 세션 시작 — 활성 프리셋이 있을 때만.

    1) 활성 프리셋 → CompositorService.configure
    2) start_capture (소스 오픈 + compose 스레드)
    3) start_recording (cycle 임시 파일)
    실패하면 None 반환 → 호출자가 webcam 폴백을 시도.
    """
    try:
        from .routers.compositor import get_active_layout
        layout = get_active_layout()
        if not layout:
            return None
        from .services.compositor_service import get_compositor_service
        svc = get_compositor_service()
        # configure는 가벼움 — 메인 루프에서 처리해도 무방하지만 일관성 위해 thread로
        await asyncio.to_thread(svc.configure, layout)
        result = await asyncio.to_thread(svc.start_capture)
        opened = result.get("opened") or []
        if not opened:
            logger.warning("Compositor: no source opened — fall back")
            await asyncio.to_thread(svc.stop_capture)
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        session = _WebcamPlaybackSession()
        session.kind = "compositor"
        session.temp_dir = _RESULTS_DIR / f"_tmp_composite_{ts}"
        session.temp_dir.mkdir(parents=True, exist_ok=True)
        path = session.temp_dir / f"{_file_prefix_for_kind('compositor')}{iteration}.mp4"
        started = await asyncio.to_thread(svc.start_recording, str(path))
        if not started:
            await asyncio.to_thread(svc.stop_capture)
            return None
        session.current_cycle = iteration
        session.current_path = path
        session.current_started_at = datetime.now(timezone.utc).isoformat()
        logger.info("Compositor session started: cycle %d → %s (sources opened=%d)", iteration, path, len(opened))
        return session
    except Exception as e:
        logger.warning("Failed to start compositor session: %s", e)
        return None


async def _webcam_session_start(iteration: int = 1) -> Optional[_WebcamPlaybackSession]:
    """첫 cycle의 녹화를 시작 + 세션 객체 반환. 웹캠 미오픈 시 None.

    Compositor 활성 프리셋이 있으면 우선 시도하고, 실패 시 단일 webcam 폴백.
    start_recording()이 카메라 초기화/코덱 세팅에서 blocking 가능 → thread 이전.
    """
    # 1) Compositor 우선
    comp_session = await _compositor_session_start(iteration)
    if comp_session is not None:
        return comp_session
    # 2) 단일 webcam 폴백 (기존 동작)
    try:
        from .services.webcam_service import get_webcam_service
        svc = get_webcam_service()
        if not svc.is_open():
            return None
        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        session = _WebcamPlaybackSession()
        session.kind = "webcam"
        session.temp_dir = _RESULTS_DIR / f"_tmp_webcam_{ts}"
        session.temp_dir.mkdir(parents=True, exist_ok=True)
        path = session.temp_dir / f"webcam_r{iteration}.mp4"
        started = await asyncio.to_thread(svc.start_recording, str(path))
        if not started:
            return None
        session.current_cycle = iteration
        session.current_path = path
        # 녹화 시작 직후 wall-clock 시각을 캡처. 프론트에서 step.timestamp -
        # recording.started_at 으로 비디오 내 정확한 오프셋을 계산하기 위해 사용.
        session.current_started_at = datetime.now(timezone.utc).isoformat()
        logger.info("Webcam session started: cycle %d → %s", iteration, path)
        return session
    except Exception as e:
        logger.warning("Failed to start webcam session: %s", e)
        return None


def _write_recording_meta(video_path: Path, started_at_iso: Optional[str]) -> None:
    """녹화 파일과 같은 폴더에 webcam_r{N}.meta.json 사이드카를 작성한다.

    프론트엔드(ResultsPage)는 이 파일의 started_at을 사용해 step.timestamp를
    비디오 내 정확한 오프셋으로 변환한다. 사이드카가 없으면 레거시 휴리스틱
    (첫 스텝 timestamp 기준)으로 폴백한다.
    """
    if not started_at_iso:
        return
    try:
        meta_path = video_path.with_suffix(video_path.suffix + ".meta.json")
        meta_path.write_text(
            json.dumps({"started_at": started_at_iso}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Failed to write recording meta for %s: %s", video_path, e)


def _try_move_cycle_to_final(iteration: int, src: Path, started_at_iso: Optional[str] = None,
                              kind: str = "webcam") -> Optional[Path]:
    """완료된 cycle 녹화 파일을 가능한 경우 즉시 최종 recordings/ 위치로 이동한다.

    `_run_output_dir`이 설정되어 있지 않으면 (single-cycle 초기 등) None을 반환하여
    호출 측이 임시 경로를 그대로 유지하도록 한다.

    started_at_iso가 주어지면 동일 폴더에 사이드카 메타 파일도 함께 생성한다.
    """
    try:
        from .services.playback_service import get_run_output_dir
        run_dir = get_run_output_dir()
        if run_dir is None:
            return None
        final_dir = run_dir / "recordings"
        final_dir.mkdir(parents=True, exist_ok=True)
        dst = final_dir / f"{_file_prefix_for_kind(kind)}{iteration}.mp4"
        if not src.exists():
            return None
        if src.resolve() == dst.resolve():
            _write_recording_meta(dst, started_at_iso)
            return dst
        import shutil
        shutil.move(str(src), str(dst))
        # 사이드카에 시작 시각 기록 (있을 때만)
        _write_recording_meta(dst, started_at_iso)
        # 임시 폴더에 남아 있을 수 있는 사이드카도 정리
        legacy_meta = src.with_suffix(src.suffix + ".meta.json")
        if legacy_meta.exists():
            try:
                legacy_meta.unlink()
            except Exception:
                pass
        logger.info("Webcam cycle %d published immediately: %s", iteration, dst)
        return dst
    except Exception as e:
        logger.warning("Failed to publish webcam cycle %d: %s", iteration, e)
        return None


async def _webcam_session_next_cycle(session: Optional[_WebcamPlaybackSession], iteration: int) -> None:
    """현재 cycle 녹화 종료 + 다음 cycle 녹화 시작.

    완료된 이전 cycle 파일은 즉시 `run_dir/recordings/`로 이동하여
    재생 중에도 결과 상세에서 해당 cycle의 영상을 조회할 수 있게 한다.

    stop_recording()은 코덱 finalize(프레임 flush, MP4 trailer 작성) 때문에
    수 초 단위 blocking이 가능하므로 thread로 이전하여 event loop를 지킨다.
    """
    if session is None or not session.is_active():
        return
    try:
        if session.kind == "compositor":
            from .services.compositor_service import get_compositor_service
            svc: Any = get_compositor_service()
        else:
            from .services.webcam_service import get_webcam_service
            svc = get_webcam_service()
        await asyncio.to_thread(svc.stop_recording)
        if session.current_path is not None:
            # 완료된 cycle 파일을 즉시 최종 위치로 이동 시도 (shutil.move = blocking)
            prev_started = session.current_started_at
            moved = await asyncio.to_thread(
                _try_move_cycle_to_final,
                session.current_cycle, session.current_path, prev_started, session.kind,
            )
            session.cycle_files.append((
                session.current_cycle,
                moved or session.current_path,
                prev_started or "",
            ))
        path = session.temp_dir / f"{_file_prefix_for_kind(session.kind)}{iteration}.mp4"  # type: ignore[union-attr]
        started = await asyncio.to_thread(svc.start_recording, str(path))
        if started:
            session.current_cycle = iteration
            session.current_path = path
            session.current_started_at = datetime.now(timezone.utc).isoformat()
            logger.info("%s session next cycle %d → %s", session.kind, iteration, path)
        else:
            session.current_path = None
            session.current_started_at = None
    except Exception as e:
        logger.warning("Failed to rotate %s recording: %s", session.kind, e)


def _webcam_session_finalize_sync(session: _WebcamPlaybackSession, result_path: Optional[str]) -> None:
    """Blocking 작업(stop/move/rmdir)을 모은 동기 함수. thread에서 실행."""
    try:
        if session.kind == "compositor":
            from .services.compositor_service import get_compositor_service
            svc: Any = get_compositor_service()
            svc.stop_recording()
            # compositor는 capture도 멈춰야 다음 사용 시 깨끗하게 재구성됨
            try:
                svc.stop_capture()
            except Exception:
                pass
        else:
            from .services.webcam_service import get_webcam_service
            svc = get_webcam_service()
            svc.stop_recording()
        if session.current_path is not None:
            prev_started = session.current_started_at
            moved = _try_move_cycle_to_final(
                session.current_cycle, session.current_path, prev_started, session.kind,
            )
            session.cycle_files.append((
                session.current_cycle,
                moved or session.current_path,
                prev_started or "",
            ))
    except Exception as e:
        logger.warning("Failed to stop %s session: %s", session.kind, e)

    import shutil
    try:
        if not session.cycle_files:
            return
        if result_path:
            result_file = Path(result_path)
            if result_file.name == "result.json":
                run_dir = result_file.parent
            else:
                run_dir = result_file.parent / result_file.stem
                run_dir.mkdir(parents=True, exist_ok=True)
            final_dir = run_dir / "recordings"
            final_dir.mkdir(parents=True, exist_ok=True)
            for iteration, src, started_at_iso in session.cycle_files:
                if not src.exists():
                    continue
                dst = final_dir / f"{_file_prefix_for_kind(session.kind)}{iteration}.mp4"
                try:
                    if src.resolve() == dst.resolve():
                        # 이미 최종 위치에 있어도 사이드카 갱신
                        _write_recording_meta(dst, started_at_iso or None)
                        continue
                except Exception:
                    pass
                try:
                    shutil.move(str(src), str(dst))
                    _write_recording_meta(dst, started_at_iso or None)
                    # 임시 폴더의 사이드카도 정리
                    legacy_meta = src.with_suffix(src.suffix + ".meta.json")
                    if legacy_meta.exists():
                        try:
                            legacy_meta.unlink()
                        except Exception:
                            pass
                    logger.info("Webcam recording moved: %s → %s", src.name, dst)
                except Exception as e:
                    logger.warning("Failed to move %s: %s", src, e)
        else:
            logger.warning("Result path unknown — %d webcam files left at %s",
                           len(session.cycle_files), session.temp_dir)
            return
    except Exception as e:
        logger.warning("Failed to finalize webcam session: %s", e)
    finally:
        try:
            td = session.temp_dir
            if td and td.exists() and not any(td.iterdir()):
                td.rmdir()
        except Exception:
            pass


async def _webcam_session_finalize(session: Optional[_WebcamPlaybackSession], result_path: Optional[str]) -> None:
    """재생 종료 시 마지막 cycle 녹화 정지 + 남은 cycle 파일을 결과 폴더로 이동.

    cycle별 파일은 이미 `_webcam_session_next_cycle`에서 즉시 최종 위치로 옮겨져 있는 경우가 많으며,
    이 함수는 마지막(진행 중이던) cycle과 early-move가 실패했던 파일만 보완 이동한다.

    stop_recording + shutil.move 여러 번 = 수 초 블록 가능 → thread 이전.
    """
    if session is None or not session.is_active():
        return
    await asyncio.to_thread(_webcam_session_finalize_sync, session, result_path)


async def _frame_check_analyze(session: Optional[_WebcamPlaybackSession],
                               result: "ScenarioResult") -> list[tuple[Path, str]]:
    """Frame_Check 마커가 있으면 녹화 영상을 프레임 분석해 result.frame_check_results 를 채운다.

    반드시 _save_result 호출 **전에** 실행 — 결과가 model_dump 를 타고 result.json/html 에
    자연스럽게 포함되도록 한다. 진행 중이던 마지막 cycle 녹화는 여기서 조기 종료한다
    (finalize 의 stop_recording 은 이중 호출에 안전, current_path=None 처리로 중복 이동 방지).

    측정 구간 클립(MeasureStart 실행 5초 전 ~ 종료점 5초 후)도 pass/fail 무관하게
    임시 폴더에 추출하고, 반환값 [(임시경로, 최종파일명)] 을 finalize 이후
    `_frame_check_publish_clips` 가 run_dir/recordings/ 로 이동한다. entry["clip"] 에는
    최종 상대 경로를 미리 기록해 result.json 에 포함시킨다.
    """
    from .services.frame_check_service import get_frame_check_service
    fc = get_frame_check_service()
    if not fc.has_markers():
        return []

    entries: list[dict] = []
    clips: list[tuple[Path, str]] = []
    if session is None or not session.is_active():
        for it in fc.iterations_with_markers():
            entries.append({
                "iteration": it, "status": "no_video",
                "message": "웹캠 녹화 없음 — 재생 시작 전 PIP 웹캠이 열려 있어야 측정 가능",
            })
        result.frame_check_results = entries
        return []

    # 진행 중이던 마지막 cycle 녹화 종료 + (run_dir 이 있으면) 최종 위치로 이동.
    # moov atom 이 기록된 완성 mp4 여야 프레임 분석이 가능하다.
    try:
        if session.kind == "compositor":
            from .services.compositor_service import get_compositor_service
            svc: Any = get_compositor_service()
        else:
            from .services.webcam_service import get_webcam_service
            svc = get_webcam_service()
        await asyncio.to_thread(svc.stop_recording)
        if session.current_path is not None:
            prev_started = session.current_started_at
            moved = await asyncio.to_thread(
                _try_move_cycle_to_final,
                session.current_cycle, session.current_path, prev_started, session.kind,
            )
            session.cycle_files.append((
                session.current_cycle,
                moved or session.current_path,
                prev_started or "",
            ))
            session.current_path = None
            session.current_started_at = None
    except Exception as e:
        logger.warning("Frame_Check: early recording stop failed: %s", e)

    by_iter: dict[int, tuple[Path, str]] = {
        it: (p, started) for it, p, started in session.cycle_files
    }
    # 클립/매치 프레임 이미지 공용 임시 폴더 — finalize 이후 recordings/ 로 이동
    clip_tmp_dir = _RESULTS_DIR / f"_tmp_fcclips_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    clip_tmp_dir.mkdir(parents=True, exist_ok=True)
    for it in fc.iterations_with_markers():
        rec = by_iter.get(it)
        if rec is None:
            entries.append({"iteration": it, "status": "no_video",
                            "message": "해당 회차의 녹화 파일 없음"})
            continue
        video_path, started_at = rec
        try:
            rows = await asyncio.to_thread(
                fc.analyze_video, video_path, started_at, it, clip_tmp_dir)
        except Exception as e:
            logger.exception("Frame_Check analysis failed (cycle %d)", it)
            rows = [{"iteration": it, "status": "error", "message": str(e)}]
        if rows:
            entries.extend(rows)
        else:
            entries.append({"iteration": it, "status": "no_pair",
                            "message": "Frame_Measure 측정 마커 없음"})

    # 매치 프레임 이미지(분석 중 임시 폴더에 저장됨)를 publish 목록에 등록하고
    # entry 경로를 최종 상대 경로(recordings/...)로 rewrite.
    for entry in entries:
        names = list(entry.get("frames") or [])
        rewritten: list[str] = []
        for n in names:
            p = clip_tmp_dir / n
            if p.exists():
                clips.append((p, n))
                rewritten.append(f"recordings/{n}")
        if rewritten:
            entry["frames"] = rewritten
        elif "frames" in entry:
            del entry["frames"]
        mi = entry.get("match_image")
        if mi:
            if (clip_tmp_dir / mi).exists():
                entry["match_image"] = f"recordings/{mi}"  # 파일은 frames 등록으로 함께 이동
            else:
                del entry["match_image"]

    # 측정 구간 클립 추출 (pass/fail 모두) — 임시 폴더에 만들고 finalize 이후 이동.
    by_iter_src = {it: p for it, (p, _s) in by_iter.items()}
    for entry in entries:
        if entry.get("clip_from_ms") is None or entry.get("clip_to_ms") is None:
            continue
        src = by_iter_src.get(entry.get("iteration"))
        if src is None or not src.exists():
            continue
        final_name = f"framecheck_r{entry['iteration']}_p{entry.get('pair_index', 1)}.mp4"
        tmp_out = clip_tmp_dir / final_name
        ok = await asyncio.to_thread(
            fc.extract_clip, src, tmp_out,
            float(entry["clip_from_ms"]), float(entry["clip_to_ms"]),
        )
        if ok:
            entry["clip"] = f"recordings/{final_name}"
            clips.append((tmp_out, final_name))
        else:
            logger.warning("Frame_Check: clip extraction skipped/failed for %s", final_name)

    # 아무것도 만들어지지 않았으면 임시 폴더 정리
    if not clips:
        try:
            if clip_tmp_dir.exists() and not any(clip_tmp_dir.iterdir()):
                clip_tmp_dir.rmdir()
        except Exception:
            pass

    if entries:
        result.frame_check_results = entries
        ok_rows = [e for e in entries if e.get("status") == "ok"]
        logger.info("Frame_Check: %d/%d measurement(s) succeeded, %d clip(s) extracted",
                    len(ok_rows), len(entries), len(clips))
    return clips


def _frame_check_publish_clips_sync(clips: list[tuple[Path, str]],
                                    result_path: Optional[str]) -> None:
    """분석 때 임시 폴더에 추출한 측정 클립을 run_dir/recordings/ 로 이동.

    finalize 이후에 호출 — result_path 로 run_dir 을 확정할 수 있는 시점.
    result.json 의 entry["clip"] 은 이 최종 위치를 미리 가리키고 있다.
    result_path 가 없으면(저장 전 예외 종료) 클립은 폐기한다.
    """
    if not clips:
        return
    import shutil
    tmp_dir = clips[0][0].parent
    try:
        if result_path:
            result_file = Path(result_path)
            if result_file.name == "result.json":
                run_dir = result_file.parent
            else:
                run_dir = result_file.parent / result_file.stem
            final_dir = run_dir / "recordings"
            final_dir.mkdir(parents=True, exist_ok=True)
            for src, name in clips:
                if not src.exists():
                    continue
                try:
                    shutil.move(str(src), str(final_dir / name))
                    logger.info("Frame_Check clip published: %s", final_dir / name)
                except Exception as e:
                    logger.warning("Frame_Check clip move failed (%s): %s", name, e)
        else:
            logger.warning("Frame_Check: result path unknown — discarding %d clip(s)", len(clips))
            for src, _name in clips:
                try:
                    src.unlink(missing_ok=True)
                except Exception:
                    pass
    finally:
        try:
            if tmp_dir.exists() and not any(tmp_dir.iterdir()):
                tmp_dir.rmdir()
        except Exception:
            pass


def _parse_until_time(raw: Any) -> Optional[datetime]:
    """프론트가 보낸 'until_time' 문자열을 timezone-aware datetime으로 변환.

    허용 포맷:
    - ISO 8601 (예: "2026-05-20T18:30:00+09:00", "2026-05-20T09:30:00Z")
    - tz 없는 ISO 문자열은 로컬 타임존으로 해석한다 (UI DatePicker가 통상 로컬 시각을 ISO로 보냄).

    파싱 실패 시 None — 호출부는 무한정 회차 실행 모드로 폴백한다.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        s = raw.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.astimezone()  # 시스템 로컬 타임존 부여
        return dt
    except (TypeError, ValueError):
        return None


async def _run_play_job(data: dict):
    """백그라운드 태스크로 실행되는 play 로직. WebSocket과 무관하게 끝까지 실행된다.

    이벤트는 playback_service.publish_event를 통해 broadcaster에 전달되고,
    연결된 모든 WebSocket 구독자가 forward 태스크로 받아 전송한다.
    """
    from .services.playback_service import (
        publish_event, clear_event_buffer, mark_playback_active,
        mark_runtime_fail_active, consume_runtime_fails,
    )
    scenario_name = data.get("scenario")
    verify = data.get("verify", True)
    repeat = data.get("repeat", 1)
    until_time = _parse_until_time(data.get("until_time"))
    device_map_override = data.get("device_map")
    # 건너뛸 스텝 — step.uid 기준. step.id 는 편집 때마다 재부여되므로,
    # 선택 시점과 재생 시점 사이에 시나리오가 편집되면 엉뚱한 스텝을 건너뛴다.
    # (구버전 프론트/오래된 탭이 보내는 정수 id 도 관용 처리 — uid 는 8자리 hex 라
    #  작은 정수 문자열과 절대 겹치지 않으므로 오탐 위험이 없다)
    skip_steps: set[str] = {str(x) for x in data.get("skip_steps", [])}
    _is_multi_cycle = False
    result_path: Optional[str] = None
    webcam_session: Optional[_WebcamPlaybackSession] = None
    # Frame_Check 측정 클립 [(임시경로, 최종파일명)] — finalize 이후 recordings/ 로 이동
    _fc_clips: list[tuple[Path, str]] = []
    # 종료 이벤트는 finally에서 모든 리소스 정리(웹캠 finalize 등) 후에만 발행.
    # 프론트가 결과 상세에 진입했을 때 녹화 파일·결과 파일이 모두 최종 위치에 있어야 404/깨짐을 방지.
    terminal_event: Optional[dict] = None
    try:
        playback_service._should_stop = False
        playback_service._pause_event.set()
        clear_event_buffer()
        mark_playback_active(True)
        mark_runtime_fail_active(True)  # SerialLogging/DLTLogging assert_keyword fail 누적 활성화
        publish_event({"type": "playback_reset", "scenario": scenario_name})
        # 재생 준비 단계별 진행 표시 — 준비 구간(녹화/로드/디바이스확인)은 정상적으로 시간이
        # 걸릴 수 있어, "서버 연결 중" 배너 대신 "재생 준비 중(k/3)..." 로 진행상황을 알린다.
        # key 는 프론트에서 i18n 라벨로 매핑한다.
        publish_event({"type": "prepare", "key": "record", "index": 1, "total": 3})
        # Frame_Check 마커 초기화 — 이전 런/단발 스텝 테스트의 잔여 마커 제거
        from .services.frame_check_service import get_frame_check_service
        get_frame_check_service().reset()
        # 웹캠 녹화 시작 (열려 있을 때만)
        webcam_session = await _webcam_session_start(iteration=1)

        publish_event({"type": "prepare", "key": "load", "index": 2, "total": 3})
        scen = await recording_service.load_scenario(scenario_name)
        if skip_steps:
            scen.steps = [
                s for s in scen.steps
                if s.uid not in skip_steps and str(s.id) not in skip_steps
            ]

        publish_event({"type": "prepare", "key": "check", "index": 3, "total": 3})
        preflight_errors = await playback_service.preflight_check(scen, device_map_override)
        if preflight_errors:
            publish_event({"type": "preflight_error", "errors": preflight_errors})
            return

        playback_service._monitor_state = {
            "scenario_name": scenario_name,
            "total_cycles": repeat,
            "current_cycle": 0,
            "current_step": 0,
            "total_steps": len(scen.steps),
            "passed": 0, "failed": 0, "warning": 0, "error": 0,
        }

        result = ScenarioResult(
            scenario_name=scenario_name,
            device_serial="multi-device",
            status="pass",
            total_steps=len(scen.steps),
            total_repeat=repeat,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

        # until_time이 지정되면 시각 한도가 우선 — repeat 한도는 안전 cap으로 매우 크게 잡는다.
        # multi_cycle UI/로그 처리(회차별 분할 등)도 활성화.
        _MAX_REPEAT_CAP = 99999
        if until_time is not None:
            effective_repeat = _MAX_REPEAT_CAP
            _is_multi_cycle = True
        else:
            effective_repeat = repeat
            _is_multi_cycle = repeat > 1
        # 스텝 결과 NDJSON 스트리밍 싱크 — 멀티사이클/aging 런에서 step_results를
        # 인메모리에 무한 누적하지 않고 디스크에 한 줄씩 흘려 저장한다. result.json/HTML은
        # _save_result에서 이 NDJSON을 정본으로 스트리밍 조립하므로 피크 메모리가 고정된다.
        _steps_ndjson: Optional[Path] = None
        if _is_multi_cycle:
            playback_service._result_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            playback_service._setup_run_output_dir(scenario_name)
            if playback_service._run_output_dir:
                _steps_ndjson = playback_service._run_output_dir / _STEPS_NDJSON_NAME
                try:
                    _steps_ndjson.write_text("", encoding="utf-8")  # 새 런 시작 — 빈 파일로 초기화
                except Exception as e:
                    logger.warning("steps NDJSON init failed: %s", e)
                    _steps_ndjson = None

        # 인메모리 step_results 상한 — 출력(result.json/HTML)은 NDJSON에서 조립하므로
        # 인메모리 리스트는 안전망용 최근 tail만 유지해 장시간 런에서도 메모리를 고정한다.
        _STEP_MEM_CAP = 1000
        # interim 저장 쓰로틀 — 매 사이클 전량 재직렬화(O(N²)) 대신 시간 간격으로 제한.
        _INTERIM_MIN_INTERVAL_S = 60.0
        _last_interim_mono = 0.0

        global_step_seq = 0
        last_completed_iteration = 0
        iteration = 0  # 외부 스코프 보존 — 중단 시 finally/이후 처리에서 참조
        _step_idx = 0  # 외부 스코프 보존 — 중단 시점의 step 번호 (사이클마다 0으로 리셋됨)
        # exec_seq 는 재생 세션 전체에서 monotonic 해야 한다(사이클 경계를 넘어 고유).
        # _step_idx 는 매 사이클 0으로 리셋되므로(아래 1453행) exec_seq 로 쓰면 2회차부터 1회차와
        # 충돌 → 프론트 dedup(prev.exec_seq===d.exec_seq)에 걸려 2회차 진행이 표시되지 않는다.
        _exec_seq = 0
        for iteration in range(1, effective_repeat + 1):
            playback_service._monitor_state["current_cycle"] = iteration
            if _is_multi_cycle:
                publish_event({
                    "type": "iteration_start",
                    "iteration": iteration,
                    "total": repeat if until_time is None else 0,
                    "until_time_mode": until_time is not None,
                })
                # 두 번째 이상 cycle 시작 시 웹캠 녹화 분할 (rotate)
                if iteration > 1 and webcam_session is not None:
                    await _webcam_session_next_cycle(webcam_session, iteration)

            _step_idx = 0
            _pending_seq = 0
            async for item in playback_service.execute_scenario_stream(
                scen, verify=verify, repeat_index=iteration,
                device_map_override=device_map_override,
                group_scenario_index=iteration if _is_multi_cycle else 0,
            ):
                if isinstance(item, dict) and item.get("_type") == "step_start":
                    _step_idx += 1
                    _exec_seq += 1
                    playback_service._monitor_state["current_step"] = _step_idx
                    start_data = {k: v for k, v in item.items() if k != "_type"}
                    if _is_multi_cycle:
                        global_step_seq += 1
                        _pending_seq = global_step_seq
                        start_data["step_id"] = _pending_seq
                        start_data["description"] = f"[Cycle {iteration}] {start_data.get('description', '')}"
                    # exec_seq: 실행 단위 고유 ID — 조건부이동/사이클 반복으로 같은 step_id를 재실행해도
                    # 매 실행마다 새 값. 프론트는 이걸로 dedup하므로 세션 전체에서 monotonic해야 한다
                    # (_step_idx는 사이클마다 0으로 리셋되어 2회차부터 충돌 → 쓰면 안 됨).
                    # 버퍼 replay 시에도 발행된 이벤트에 박힌 값 그대로 재전송되므로 안정적.
                    start_data["exec_seq"] = _exec_seq
                    publish_event({
                        "type": "step_start",
                        "data": start_data,
                        "iteration": iteration,
                    })
                else:
                    step_result = item
                    # parent_step_id가 있는 항목은 sync 모드 fail_on_keyword가 trigger한 인라인 fail.
                    # 일반 스텝과 다르게 처리: step_id(9000+)는 보존, parent_step_id를 직전 스텝의 _pending_seq로 remap.
                    is_runtime_fail = step_result.parent_step_id is not None
                    if _is_multi_cycle:
                        if is_runtime_fail:
                            step_result.parent_step_id = _pending_seq
                            step_result.description = f"[Cycle {iteration}] {step_result.description}" if step_result.description else f"[Cycle {iteration}]"
                        else:
                            step_result.step_id = _pending_seq
                            step_result.description = f"[Cycle {iteration}] {step_result.description}" if step_result.description else f"[Cycle {iteration}]"
                    # NDJSON에 먼저 durable 기록 (remap된 step_id/description 반영분)
                    if _steps_ndjson is not None:
                        _append_step_ndjson(_steps_ndjson, step_result)
                    result.step_results.append(step_result)
                    # 인메모리 리스트는 최근 tail만 유지 (출력은 NDJSON 정본에서 조립)
                    if _steps_ndjson is not None and len(result.step_results) > _STEP_MEM_CAP:
                        del result.step_results[:-_STEP_MEM_CAP]
                    if step_result.excluded_from_result:
                        pass  # 조건부이동 결과 미반영('분기') — 집계/시나리오 판정에서 제외
                    elif step_result.status == "pass":
                        result.passed_steps += 1
                        playback_service._monitor_state["passed"] += 1
                    elif step_result.status == "fail":
                        result.failed_steps += 1
                        playback_service._monitor_state["failed"] += 1
                    else:
                        result.error_steps += 1
                        playback_service._monitor_state["error"] += 1
                    sr_data = step_result.model_dump()
                    # step_start와 동일 exec_seq를 부착 — 프론트가 placeholder 행과 매칭하기 위함
                    sr_data["exec_seq"] = _exec_seq
                    publish_event({
                        "type": "step_result",
                        "data": sr_data,
                        "iteration": iteration,
                    })

            if playback_service._should_stop:
                break
            last_completed_iteration = iteration

            # "지정 시각을 포함하는 회차까지" — 회차 완료 후 검사. now >= until_time 이면 다음 회차 시작 안 함.
            # 현재 회차는 항상 끝까지 진행되므로 종료 시각이 회차 진행 중에 도래해도 해당 회차는 완주됨.
            if until_time is not None and datetime.now(until_time.tzinfo) >= until_time:
                publish_event({
                    "type": "until_time_reached",
                    "iteration": iteration,
                    "until_time": until_time.isoformat(),
                })
                break

            # interim 저장은 시간 간격으로 쓰로틀 — 매 사이클 전량 재직렬화(O(N²)) 폭주를 막는다.
            # step_results는 NDJSON에서 스트리밍 조립하므로 인메모리 리스트를 복사하지 않는다
            # (NDJSON 미사용 폴백 시에만 현재 리스트를 넘긴다).
            if _is_multi_cycle:
                _now_mono = time.monotonic()
                if _now_mono - _last_interim_mono >= _INTERIM_MIN_INTERVAL_S:
                    _last_interim_mono = _now_mono
                    _interim = ScenarioResult(
                        scenario_name=scenario_name,
                        device_serial="multi-device",
                        status="fail" if result.failed_steps > 0 or result.error_steps > 0 else "pass",
                        total_steps=global_step_seq,
                        total_repeat=last_completed_iteration,
                        started_at=result.started_at,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        step_results=[] if _steps_ndjson is not None else list(result.step_results),
                        passed_steps=result.passed_steps,
                        failed_steps=result.failed_steps,
                        error_steps=result.error_steps,
                    )
                    await playback_service._save_result(_interim, interim=True)

        # 시나리오 동안 모듈이 보고한 legacy runtime fail (parent_step_id 없는 항목, 예: assert_keyword 미일치)을
        # tail에 흡수. sync 모드 fail은 이미 인라인으로 step_results에 들어가 있어 buffer에 남아있지 않음.
        runtime_fails = consume_runtime_fails()
        if runtime_fails:
            if _steps_ndjson is not None:
                for _rf in runtime_fails:
                    _append_step_ndjson(_steps_ndjson, _rf)
            result.step_results.extend(runtime_fails)
            result.failed_steps += len(runtime_fails)
            if _steps_ndjson is not None and len(result.step_results) > _STEP_MEM_CAP:
                del result.step_results[:-_STEP_MEM_CAP]

        # Frame_Check 마커가 있으면 녹화를 조기 종료하고 영상을 프레임 분석해
        # result.frame_check_results 에 측정 시간을 기록 (result 저장 전이어야 함).
        # 측정 구간 클립은 임시 추출 후 finally 에서 recordings/ 로 이동.
        try:
            _fc_clips = await _frame_check_analyze(webcam_session, result)
        except Exception as e:
            logger.warning("Frame_Check analyze error: %s", e)

        # 중단 처리 — 진행 중이던 회차의 부분 step도 보존하고 영상도 함께 남김.
        if playback_service._should_stop:
            # 진행 중이던 회차가 끝까지 안 갔으면 그 회차를 stopped로 마킹
            in_progress_iter = iteration if iteration > last_completed_iteration else None
            if _is_multi_cycle:
                result.total_steps = global_step_seq
            # 카운트 재계산은 전체 step_results가 인메모리에 있을 때만 (NDJSON 미사용 폴백).
            # 스트리밍 런은 인메모리 리스트가 최근 tail로 캡되어 있어 합산이 틀어지므로,
            # 진행 중 증분 누적된 result.passed/failed/error_steps를 정본으로 신뢰한다.
            if _steps_ndjson is None:
                result.passed_steps = sum(1 for sr in result.step_results if sr.status == "pass")
                result.failed_steps = sum(1 for sr in result.step_results if sr.status == "fail")
                result.error_steps = sum(1 for sr in result.step_results if sr.status not in ("pass", "fail"))
            # total_repeat = 진행 시도한 마지막 회차 번호 (완료/중단 무관)
            result.total_repeat = max(iteration, 1)
            result.stopped_at_iteration = in_progress_iter
            if in_progress_iter is not None:
                result.stopped_at_step = _step_idx if _step_idx > 0 else None
            result.finished_at = datetime.now(timezone.utc).isoformat()
            result.status = "stopped"
            result_path = await playback_service._save_result(result)
            terminal_event = {"type": "playback_stopped", "result_filename": _result_filename(result_path)}
        else:
            if _is_multi_cycle:
                result.total_steps = global_step_seq
            result.finished_at = datetime.now(timezone.utc).isoformat()
            result.status = "fail" if (result.failed_steps > 0 or result.error_steps > 0) else "pass"
            result_path = await playback_service._save_result(result)
            terminal_event = {"type": "playback_complete", "result_filename": _result_filename(result_path)}
    except Exception as e:
        logger.exception("Play job failed")
        terminal_event = {"type": "error", "message": str(e)}
    finally:
        # 웹캠 녹화 finalize (blocking할 수 있어 수 초 소요) — 완료 후에야 결과 폴더가 최종 상태
        try:
            await _webcam_session_finalize(webcam_session, result_path)
        except Exception as e:
            logger.warning("webcam finalize error: %s", e)
        # Frame_Check 측정 클립을 결과 폴더 recordings/ 로 이동 (result.json 의 clip 경로와 일치)
        if _fc_clips:
            try:
                await asyncio.to_thread(_frame_check_publish_clips_sync, _fc_clips, result_path)
            except Exception as e:
                logger.warning("Frame_Check clip publish error: %s", e)
        mark_playback_active(False)
        mark_runtime_fail_active(False)
        # 중단/예외로 끝난 경우 모듈 인스턴스를 정리해 포트/스레드 leak 방지.
        # 정상 종료(playback_complete)에서는 시나리오 마지막 스텝이 StopLogging 등을 통해 직접 정리한 것으로 간주.
        # NOTE: cleanup은 _cleanup_run_output_dir()보다 먼저 호출되어야 함 — StopLogging이
        # _auto_save_path를 통해 현재 run_dir/logs/에 시리얼·DLT 로그를 저장하기 때문.
        terminal_type = (terminal_event or {}).get("type")
        if terminal_type in ("playback_stopped", "error"):
            try:
                from .services.module_service import cleanup_active_instances
                await asyncio.to_thread(cleanup_active_instances, terminal_type)
            except Exception as e:
                logger.warning("module cleanup failed: %s", e)
        # 모듈 정리 후 글로벌 run_dir 참조 해제. multi-cycle 또는 stream finally에서
        # 이미 정리된 경우(정상 완료)에도 idempotent하게 동작.
        playback_service._cleanup_run_output_dir()
        if _is_multi_cycle:
            playback_service._running = False
        # 모든 리소스 정리가 끝난 뒤에야 프론트에 종료 이벤트 전파
        # (이전 순서에선 publish가 먼저 나가 프론트가 결과 상세에 진입 → 파일이 아직 없어 404 발생)
        if terminal_event is not None:
            publish_event(terminal_event)


async def _run_play_group_job(data: dict):
    """백그라운드 태스크로 실행되는 play_group 로직."""
    from .services.playback_service import (
        publish_event, clear_event_buffer, mark_playback_active,
        mark_runtime_fail_active, consume_runtime_fails,
    )
    group_members = data.get("scenarios", [])
    verify = data.get("verify", True)
    repeat = data.get("repeat", 1)
    until_time = _parse_until_time(data.get("until_time"))
    device_map_override = data.get("device_map")

    entries: list[dict] = []
    for m in group_members:
        if isinstance(m, str):
            entries.append({"name": m, "on_pass_goto": None, "on_fail_goto": None})
        else:
            entries.append(m)

    result_path: Optional[str] = None
    webcam_session: Optional[_WebcamPlaybackSession] = None
    # 종료 이벤트는 finally에서 모든 리소스 정리 후에만 발행 (_run_play_job 참고)
    terminal_event: Optional[dict] = None
    try:
        playback_service._should_stop = False
        playback_service._pause_event.set()
        clear_event_buffer()
        mark_playback_active(True)
        mark_runtime_fail_active(True)
        publish_event({"type": "playback_reset", "group": True})
        # 재생 준비 단계별 진행 표시 — 그룹은 멤버 시나리오마다 로드/디바이스확인이 필요해
        # 준비 구간이 길 수 있다. "재생 준비 중" + 멤버별 진행(k/N)으로 알린다.
        publish_event({"type": "prepare", "key": "record", "index": 0, "total": len(entries)})
        # Frame_Check 마커 초기화 — 그룹 재생은 영상 분석 미지원(회차↔시나리오 매핑 모호),
        # 잔여 마커가 다음 단일 재생을 오염시키지 않도록만 정리한다.
        from .services.frame_check_service import get_frame_check_service
        get_frame_check_service().reset()
        webcam_session = await _webcam_session_start(iteration=1)

        all_preflight_errors: list[str] = []
        for _mi, entry in enumerate(entries, start=1):
            publish_event({
                "type": "prepare", "key": "check_member",
                "index": _mi, "total": len(entries), "name": entry.get("name", ""),
            })
            try:
                scen = await recording_service.load_scenario(entry["name"])
                # 그룹 일괄 매핑(device_map_override)을 각 멤버 시나리오의 preflight에도 적용.
                # 누락 시 사용자가 보낸 매핑을 무시하고 alias 자체로 검사 → 매번 실패.
                errs = await playback_service.preflight_check(scen, device_map_override)
                for e in errs:
                    msg = f"[{entry['name']}] {e}"
                    if msg not in all_preflight_errors:
                        all_preflight_errors.append(msg)
            except FileNotFoundError:
                all_preflight_errors.append(f"시나리오 '{entry['name']}'을(를) 찾을 수 없습니다")
        if all_preflight_errors:
            publish_event({"type": "preflight_error", "errors": all_preflight_errors})
            return

        group_name = data.get("group_name", entries[0]["name"])

        # 그룹 점프 대상 검증 — 멤버 삭제/시나리오 편집으로 끊긴 참조를 사용자에게 안내.
        # 재생은 막지 않는다(끊긴 점프는 자연 진행으로 폴백). 다만 사용자가 의도한
        # 흐름이 아닐 수 있으므로 조용히 넘어가지 않는다.
        try:
            jump_warnings = await recording_service.validate_group_jumps(group_name)
            if jump_warnings:
                publish_event({"type": "group_jump_warning", "warnings": jump_warnings})
                logger.warning(f"[{group_name}] 끊긴 그룹 점프 {len(jump_warnings)}건: {jump_warnings}")
        except Exception as e:
            logger.warning(f"그룹 점프 검증 실패(무시하고 계속): {e}")

        total_steps = 0
        for entry in entries:
            try:
                scen = await recording_service.load_scenario(entry["name"])
                total_steps += len(scen.steps)
            except FileNotFoundError:
                pass

        playback_service._result_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        playback_service._setup_run_output_dir(group_name)

        # 스텝 결과 NDJSON 스트리밍 싱크 (_run_play_job과 동일 설계) — 장시간 그룹 aging
        # 런에서 step_results 인메모리 무한 누적/매 사이클 전량 재직렬화로 인한 OOM 방지.
        _steps_ndjson: Optional[Path] = None
        if playback_service._run_output_dir:
            _steps_ndjson = playback_service._run_output_dir / _STEPS_NDJSON_NAME
            try:
                _steps_ndjson.write_text("", encoding="utf-8")
            except Exception as e:
                logger.warning("steps NDJSON init failed: %s", e)
                _steps_ndjson = None
        _STEP_MEM_CAP = 1000
        _INTERIM_MIN_INTERVAL_S = 60.0
        _last_interim_mono = 0.0

        unified_result = ScenarioResult(
            scenario_name=group_name,
            device_serial="multi-device",
            status="pass",
            total_steps=total_steps,
            total_repeat=repeat,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        global_step_seq = 0
        iteration = 0  # 외부 보존 — 중단 시 stopped_at_iteration 표기에 사용
        last_completed_iteration = 0

        # 관제 보고용 상태 — 그룹 재생도 단일 재생(_run_play_job)과 동일하게 채운다.
        # (예전엔 그룹 경로만 이걸 안 세워서, execute_scenario_stream 이 _running=True 로 만든
        #  탓에 관제 카드가 'activity=재생 중' 인데 상세는 '재생 중 아님' 으로 모순돼 보였다)
        playback_service._monitor_state = {
            "scenario_name": group_name,
            "total_cycles": repeat if until_time is None else 0,
            "current_cycle": 0,
            "current_step": 0,
            "total_steps": total_steps,
            "passed": 0, "failed": 0, "warning": 0, "error": 0,
        }

        # until_time이 지정되면 시각 한도가 우선 — repeat 한도는 안전 cap으로 매우 크게.
        _MAX_REPEAT_CAP = 99999
        effective_repeat = _MAX_REPEAT_CAP if until_time is not None else repeat
        _multi = (repeat > 1) or (until_time is not None)

        _cycle_step = 0   # 관제 표시용 사이클 내 스텝 번호 (global_step_seq 는 그룹 전체 누적이라 부적합)
        for iteration in range(1, effective_repeat + 1):
            if playback_service._should_stop:
                break
            playback_service._monitor_state["current_cycle"] = iteration
            _cycle_step = 0
            if _multi:
                publish_event({
                    "type": "iteration_start",
                    "iteration": iteration,
                    "total": repeat if until_time is None else 0,
                    "until_time_mode": until_time is not None,
                })
                # 두 번째 이상 cycle 시작 시 웹캠 녹화 분할
                if iteration > 1 and webcam_session is not None:
                    await _webcam_session_next_cycle(webcam_session, iteration)

            sc_idx = 0
            start_step = 0
            uid_to_member_idx = {m.get("uid"): i for i, m in enumerate(entries) if m.get("uid")}
            while sc_idx < len(entries):
                if playback_service._should_stop:
                    break
                entry = entries[sc_idx]
                sc_name = entry["name"]
                # 관제에 "그룹 (현재 시나리오)" 로 표시 — 어떤 멤버를 돌고 있는지 보이게 한다.
                playback_service._monitor_state["scenario_name"] = f"{group_name} ({sc_name})"
                step_jumps = entry.get("step_jumps", {})
                try:
                    play_count = max(1, int(entry.get("play_count", 1)))
                except (TypeError, ValueError):
                    play_count = 1

                step_jump_target = None
                # play_count > 1 시 멤버 단위 jump 판정에 쓸 마지막 sub-iter 상태
                last_member_status = "pass"

                for play_i in range(1, play_count + 1):
                    if playback_service._should_stop:
                        break
                    scen = await recording_service.load_scenario(sc_name)
                    # description prefix: play_count>1 이면 회차 표시 포함
                    sc_prefix = f"[{sc_name} #{play_i}/{play_count}]" if play_count > 1 else f"[{sc_name}]"

                    publish_event({
                        "type": "group_scenario_start",
                        "scenario_name": sc_name,
                        "scenario_index": sc_idx + 1,
                        "total_scenarios": len(entries),
                        "start_step": start_step,
                        "scenario_play_index": play_i,
                        "scenario_play_total": play_count,
                    })

                    _pending_seq = 0
                    # sub-iteration 종료 시점의 마지막 일반 step 상태 (runtime fail 제외)
                    last_step_status = "pass"
                    # 직전 일반 step이 조건부이동 '결과 미반영'(분기)이었는지 — 그 상세 fail row도 제외하기 위함
                    last_step_excluded = False
                    async for item in playback_service.execute_scenario_stream(
                        scen, verify=verify, repeat_index=iteration, start_step=start_step,
                        device_map_override=device_map_override, group_scenario_index=sc_idx + 1,
                    ):
                        if isinstance(item, dict) and item.get("_type") == "step_start":
                            global_step_seq += 1
                            _pending_seq = global_step_seq
                            # 관제 진행 표시 — 사이클 내 누적 스텝 번호 (멤버를 이어 돌며 증가, 회차마다 리셋)
                            _cycle_step += 1
                            playback_service._monitor_state["current_step"] = _cycle_step
                            start_data = {k: v for k, v in item.items() if k != "_type"}
                            start_data["step_id"] = _pending_seq
                            start_data["description"] = f"{sc_prefix} {start_data.get('description', '')}" if start_data.get('description') else sc_prefix
                            # exec_seq: 그룹 전체에서 monotonic — 시나리오 간/조건부이동 revisit 모두 새 값.
                            # 프론트가 step_id+repeat_index 대신 이것으로 dedup해서 revisit 행이 누락되지 않도록.
                            start_data["exec_seq"] = _pending_seq
                            publish_event({
                                "type": "step_start",
                                "data": start_data,
                                "iteration": iteration,
                                "scenario_name": sc_name,
                            })
                            continue
                        step_result = item
                        original_step_id = step_result.step_id
                        # 그룹 step_jumps 는 uid 키 (step_id 는 편집 시 재부여되어 어긋남)
                        original_step_uid = step_result.step_uid
                        is_runtime_fail = step_result.parent_step_id is not None
                        if is_runtime_fail:
                            # parent를 직전 일반 스텝의 _pending_seq로 remap, step_id(9000+)는 보존
                            step_result.parent_step_id = _pending_seq
                        else:
                            step_result.step_id = _pending_seq
                        step_result.description = f"{sc_prefix} {step_result.description}" if step_result.description else sc_prefix

                        # 조건부이동 '결과 미반영' 처리 — status(실제 pass/fail)는 그대로 두어
                        # 라우팅이 정상 동작하게 하고, 그룹 step_jumps의 exclude가 걸린 방향이면
                        # excluded_from_result만 True로 마킹한다. (Step 모델 자체 exclude는
                        # playback_service가 yield 시점에 이미 마킹함 — 여기선 그룹 설정만 추가 반영)
                        real_status = step_result.status
                        _sj = None if is_runtime_fail else step_jumps.get(original_step_uid)
                        if _sj:
                            if real_status == "pass" and _sj.get("exclude_pass_from_result"):
                                step_result.excluded_from_result = True
                            elif real_status in ("fail", "error") and _sj.get("exclude_fail_from_result"):
                                step_result.excluded_from_result = True
                        if is_runtime_fail:
                            # 부모가 분기(결과 미반영)면 그 상세 fail row도 집계에서 제외.
                            # (Step 모델 exclude는 playback_service가 이미 마킹, 그룹 step_jumps
                            #  exclude는 여기서 last_step_excluded로 보강)
                            if last_step_excluded:
                                step_result.excluded_from_result = True
                        else:
                            last_step_excluded = step_result.excluded_from_result

                        if _steps_ndjson is not None:
                            _append_step_ndjson(_steps_ndjson, step_result)
                        unified_result.step_results.append(step_result)
                        if _steps_ndjson is not None and len(unified_result.step_results) > _STEP_MEM_CAP:
                            del unified_result.step_results[:-_STEP_MEM_CAP]
                        if step_result.excluded_from_result:
                            pass  # 결과 미반영('분기') — 집계/시나리오 판정에서 제외
                        elif step_result.status == "pass":
                            unified_result.passed_steps += 1
                            playback_service._monitor_state["passed"] += 1
                        elif step_result.status == "fail":
                            unified_result.failed_steps += 1
                            playback_service._monitor_state["failed"] += 1
                        else:
                            unified_result.error_steps += 1
                            playback_service._monitor_state["error"] += 1
                        sr_data = step_result.model_dump()
                        sr_data["exec_seq"] = _pending_seq
                        publish_event({
                            "type": "step_result",
                            "data": sr_data,
                            "iteration": iteration,
                            "scenario_name": sc_name,
                        })

                        # step_jump는 일반 스텝에만 적용 (인라인 fail에는 무의미)
                        if is_runtime_fail:
                            continue
                        # 멤버/스텝 점프는 실제 결과(real_status) 기준 — 미반영은 집계·표시 전용
                        last_step_status = real_status
                        sj = step_jumps.get(original_step_uid)
                        if sj:
                            if real_status == "pass":
                                sj_jump = sj.get("on_pass_goto")
                            else:
                                sj_jump = sj.get("on_fail_goto")
                            if sj_jump is not None:
                                step_jump_target = sj_jump
                                break

                    # step_jump 발사 시 남은 sub-iteration 즉시 종료
                    if step_jump_target is not None:
                        break
                    last_member_status = last_step_status
                    # 2회차 이후 sub-iter는 항상 처음 스텝부터 — 첫 sub-iter에 적용된 start_step 소비
                    start_step = 0

                if playback_service._should_stop:
                    break

                next_idx = sc_idx + 1
                start_step = 0
                jump = None

                if step_jump_target is not None:
                    jump = step_jump_target
                else:
                    if last_member_status == "pass":
                        jump = entry.get("on_pass_goto")
                    else:
                        jump = entry.get("on_fail_goto")

                if jump is not None:
                    # 점프 대상은 member_uid + step_uid (인덱스가 아님).
                    # 멤버 순서변경·삭제, 대상 시나리오의 스텝 편집에도 어긋나지 않는다.
                    target_uid = jump.get("member_uid") if isinstance(jump, dict) else None
                    if target_uid == GROUP_JUMP_END:
                        break
                    next_idx = uid_to_member_idx.get(target_uid)
                    if next_idx is None:
                        # 재생 전 검증에서 걸러지지만, 방어적으로 자연 진행
                        logger.warning(
                            f"그룹 점프 대상 멤버를 찾을 수 없어 무시합니다 (uid={target_uid})"
                        )
                        next_idx = sc_idx + 1
                        start_step = 0
                    else:
                        # step_uid 가 없으면(None) 대상 시나리오 처음부터
                        start_step = await _resolve_group_jump_step(
                            entries[next_idx]["name"], jump.get("step_uid")
                        )

                sc_idx = next_idx

            if not playback_service._should_stop:
                last_completed_iteration = iteration
            # 회차 완료 후 종료시각 검사 (그룹). 현재 회차는 완주, 다음 회차만 차단.
            _until_reached = (
                not playback_service._should_stop
                and until_time is not None
                and datetime.now(until_time.tzinfo) >= until_time
            )
            if _until_reached:
                publish_event({
                    "type": "until_time_reached",
                    "iteration": iteration,
                    "until_time": until_time.isoformat(),
                })
            if _multi and not playback_service._should_stop:
                _now_mono = time.monotonic()
                if _now_mono - _last_interim_mono >= _INTERIM_MIN_INTERVAL_S:
                    _last_interim_mono = _now_mono
                    _interim = ScenarioResult(
                        scenario_name=group_name,
                        device_serial="multi-device",
                        status="fail" if unified_result.failed_steps > 0 or unified_result.error_steps > 0 else "pass",
                        total_steps=global_step_seq,
                        total_repeat=iteration,
                        started_at=unified_result.started_at,
                        finished_at=datetime.now(timezone.utc).isoformat(),
                        step_results=[] if _steps_ndjson is not None else list(unified_result.step_results),
                        passed_steps=unified_result.passed_steps,
                        failed_steps=unified_result.failed_steps,
                        error_steps=unified_result.error_steps,
                    )
                    await playback_service._save_result(_interim, interim=True)

            if _until_reached:
                break

        # runtime fail (assert_keyword) 흡수
        runtime_fails = consume_runtime_fails()
        if runtime_fails:
            if _steps_ndjson is not None:
                for _rf in runtime_fails:
                    _append_step_ndjson(_steps_ndjson, _rf)
            unified_result.step_results.extend(runtime_fails)
            unified_result.failed_steps += len(runtime_fails)
            if _steps_ndjson is not None and len(unified_result.step_results) > _STEP_MEM_CAP:
                del unified_result.step_results[:-_STEP_MEM_CAP]

        unified_result.finished_at = datetime.now(timezone.utc).isoformat()
        unified_result.total_steps = global_step_seq
        if playback_service._should_stop:
            unified_result.status = "stopped"
            in_progress_iter = iteration if iteration > last_completed_iteration else None
            unified_result.stopped_at_iteration = in_progress_iter
            unified_result.total_repeat = max(iteration, 1)
        elif unified_result.failed_steps > 0 or unified_result.error_steps > 0:
            unified_result.status = "fail"
        else:
            unified_result.status = "pass"
        result_path = await playback_service._save_result(unified_result)
        rf = _result_filename(result_path)

        if playback_service._should_stop:
            terminal_event = {"type": "playback_stopped", "result_filename": rf}
        else:
            terminal_event = {"type": "playback_complete", "result_filename": rf}
    except Exception as e:
        logger.exception("Play group job failed")
        terminal_event = {"type": "error", "message": str(e)}
    finally:
        try:
            await _webcam_session_finalize(webcam_session, result_path)
        except Exception as e:
            logger.warning("webcam finalize error (group): %s", e)
        playback_service._running = False
        mark_playback_active(False)
        mark_runtime_fail_active(False)
        # 중단/예외로 끝난 경우 모듈 인스턴스 정리 (단일 재생과 동일 정책).
        # cleanup은 _cleanup_run_output_dir()보다 먼저 — StopLogging이 결과 폴더 logs/에
        # 시리얼·DLT 로그를 저장할 수 있게 run_dir 참조를 유지한 채로 호출.
        terminal_type = (terminal_event or {}).get("type")
        if terminal_type in ("playback_stopped", "error"):
            try:
                from .services.module_service import cleanup_active_instances
                await asyncio.to_thread(cleanup_active_instances, terminal_type)
            except Exception as e:
                logger.warning("module cleanup failed (group): %s", e)
        # 모듈 정리 후 글로벌 run_dir 참조 해제
        playback_service._cleanup_run_output_dir()
        # 리소스 정리 완료 후에 프론트에 알림 — 결과 상세 진입 시 파일이 모두 제자리에 있도록
        if terminal_event is not None:
            publish_event(terminal_event)


@app.websocket("/ws/webcam")
async def websocket_webcam(websocket: WebSocket):
    """Webcam preview WebSocket — 백엔드의 최신 프레임을 JPEG binary로 push.

    클라이언트 옵션 (첫 메시지 JSON):
      {"fps": 15, "quality": 70}
    fps: 1~30 (기본 15), quality: 1~100 (기본 70)

    녹화와 무관 — 캡처 스레드가 만든 _latest_frame을 단순 fan-out.
    """
    from .services.webcam_service import get_webcam_service
    await websocket.accept()
    logger.info("Webcam preview WS connected")
    fps = 15
    quality = 70
    svc = get_webcam_service()
    try:
        # 클라이언트 옵션 수신 (선택)
        try:
            opts = await asyncio.wait_for(websocket.receive_json(), timeout=0.2)
            if isinstance(opts, dict):
                fps = max(1, min(30, int(opts.get("fps", fps))))
                quality = max(1, min(100, int(opts.get("quality", quality))))
        except (asyncio.TimeoutError, Exception):
            pass
        interval = 1.0 / fps
        while True:
            t0 = asyncio.get_event_loop().time()
            jpg = svc.get_latest_jpeg(quality=quality)
            if jpg is None:
                # 카메라 미오픈 → 잠시 대기 후 재시도 (옵션: 끊기)
                await asyncio.sleep(0.5)
                continue
            try:
                await websocket.send_bytes(jpg)
            except Exception:
                break
            elapsed = asyncio.get_event_loop().time() - t0
            sleep_s = interval - elapsed
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Webcam preview WS error: %s", e)
    finally:
        logger.info("Webcam preview WS disconnected")


@app.websocket("/ws/compositor")
async def websocket_compositor(websocket: WebSocket):
    """Compositor 합성 캔버스의 최신 프레임을 JPEG binary로 push.

    클라이언트 옵션 (첫 메시지 JSON): {"fps": 15, "quality": 70}.
    /ws/webcam과 동일 프로토콜. 캡처 미실행 시 일정 간격으로 wait.
    """
    from .services.compositor_service import get_compositor_service
    await websocket.accept()
    logger.info("Compositor preview WS connected")
    fps = 15
    quality = 70
    svc = get_compositor_service()
    try:
        try:
            opts = await asyncio.wait_for(websocket.receive_json(), timeout=0.2)
            if isinstance(opts, dict):
                fps = max(1, min(30, int(opts.get("fps", fps))))
                quality = max(1, min(100, int(opts.get("quality", quality))))
        except (asyncio.TimeoutError, Exception):
            pass
        interval = 1.0 / fps
        while True:
            t0 = asyncio.get_event_loop().time()
            jpg = svc.get_latest_jpeg(quality=quality)
            if jpg is None:
                await asyncio.sleep(0.5)
                continue
            try:
                await websocket.send_bytes(jpg)
            except Exception:
                break
            elapsed = asyncio.get_event_loop().time() - t0
            sleep_s = interval - elapsed
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("Compositor preview WS error: %s", e)
    finally:
        logger.info("Compositor preview WS disconnected")


@app.websocket("/ws/playback")
async def websocket_playback(websocket: WebSocket):
    """WebSocket endpoint: subscribe to playback events + handle commands.

    - WS가 닫혀도 백그라운드 재생 태스크는 계속 실행됨
    - 새 WS가 연결되면 최근 이벤트 버퍼를 replay 받아 현재 상태를 복구
    """
    global _playback_bg_task
    from .services.playback_service import subscribe_events, unsubscribe_events, publish_event, mark_playback_active, set_bg_playback_task
    await websocket.accept()
    logger.info("Playback WebSocket connected")

    # 구독 + forward task 생성
    event_queue = subscribe_events()

    async def _forward_loop():
        """event_queue → websocket으로 이벤트 forwarding."""
        try:
            while True:
                ev = await event_queue.get()
                try:
                    await websocket.send_json(ev)
                except Exception:
                    # WS 전송 실패 → 좀비 연결 방지를 위해 WS 명시적으로 close.
                    # 그래야 outer receive_json 루프가 WebSocketDisconnect로 빠져나와
                    # 정상 cleanup 경로를 탄다.
                    try:
                        await websocket.close()
                    except Exception:
                        pass
                    return
        except asyncio.CancelledError:
            return

    forward_task = asyncio.create_task(_forward_loop())

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "play":
                if playback_service.is_running or (_playback_bg_task and not _playback_bg_task.done()):
                    publish_event({"type": "error", "message": "이미 재생 중입니다"})
                    continue
                _playback_bg_task = asyncio.create_task(_run_play_job(data))
                set_bg_playback_task(_playback_bg_task)

            elif action == "play_group":
                if playback_service.is_running or (_playback_bg_task and not _playback_bg_task.done()):
                    publish_event({"type": "error", "message": "이미 재생 중입니다"})
                    continue
                _playback_bg_task = asyncio.create_task(_run_play_group_job(data))
                set_bg_playback_task(_playback_bg_task)

            elif action == "stop":
                # stop()은 내부적으로 백그라운드 재생 태스크가 완전 종료될 때까지 대기.
                # 반환 시점에 이전 run은 정리되었으므로 바로 다음 play를 받을 수 있다.
                mark_playback_active(False)  # race 방지: 다른 WS가 연결돼도 이전 run 버퍼 replay 금지
                await playback_service.stop()
                publish_event({"type": "playback_stopped", "result_filename": ""})

            elif action == "pause":
                await playback_service.pause()
                publish_event({"type": "playback_paused"})

            elif action == "resume":
                await playback_service.resume()
                publish_event({"type": "playback_resumed"})

            elif action == "subscribe":
                # 재연결 → 이미 subscribe_events가 최근 버퍼를 replay함
                pass

    except WebSocketDisconnect:
        logger.info("Playback WebSocket disconnected (playback continues in background)")
    finally:
        forward_task.cancel()
        try:
            await forward_task
        except (asyncio.CancelledError, Exception):
            pass
        unsubscribe_events(event_queue)
