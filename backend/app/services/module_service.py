"""Module introspection and execution service.

Supports both lge.auto modules and local plugins (backend/app/plugins/).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import importlib.util  # importlib.util 은 import importlib 만으로는 로드되지 않음 — file-based 플러그인 폴백에서 필요
import inspect
import functools
import json
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Cache: module_name -> class instance
_instances: dict[str, Any] = {}

# 인스턴스 키별 생성 락 — 생성+auto-Connect 가 수십 초 걸리는 모듈(SCAR 컨테이너 기동
# ~60s, TH CVD 부팅 ~40s+)에서, 첫 Connect 가 끝나기 전에 들어온 두 번째 연결 요청이
# 캐시 미스(_instances 에는 Connect 완료 후에야 등록)로 중복 인스턴스를 만들어
# Setup(netns clean/apply, start_ui.sh)을 동시에 재실행하던 레이스를 차단한다.
# 락을 기다린 요청은 더블체크로 먼저 만들어진 같은 인스턴스를 받는다.
_instance_locks: dict[str, threading.Lock] = {}
_instance_locks_guard = threading.Lock()

# Tracks modules that went through auto-connect successfully
_auto_connected: set[str] = set()

# Cache: module_name -> (plugin_file_mtime, guides_mtime, list of function info).
# mtime 기반 무효화로 플러그인 .py 또는 가이드 JSON 변경 시 자동 재스캔.
_module_functions_cache: dict[str, tuple[float, float, list[dict]]] = {}

# 모듈별 전용 단일 스레드 executor.
# 이유:
#   - CANoe 등 win32com 기반 모듈은 STA(Single-Threaded Apartment) COM 객체로,
#     객체를 만든 스레드 외에서 호출하면 RPC_E_WRONG_THREAD(0x8001010E) 발생.
#   - default ThreadPoolExecutor는 매 호출마다 다른 스레드를 쓰므로 COM affinity가 깨짐.
#   - 모듈별로 max_workers=1 executor를 두면 같은 모듈은 항상 동일 스레드에서 실행되어
#     COM 객체 affinity가 보장됨. 부수적으로 모듈 내부 상태에 대한 동시 호출도 직렬화됨.
_module_executors: dict[str, concurrent.futures.ThreadPoolExecutor] = {}
_module_executors_lock = threading.Lock()


def _module_thread_initializer() -> None:
    """모듈 executor 워커 스레드 초기화 — Windows COM 모듈을 위해 CoInitialize.

    pythoncom.CoInitialize()는 STA 아파트먼트로 스레드를 초기화하고, 이미 초기화된
    경우 무해하게 통과(S_FALSE 리턴). 비-Windows / pythoncom 미설치 환경은 ImportError로
    조용히 패스.
    """
    try:
        import pythoncom  # type: ignore[import-not-found]
        pythoncom.CoInitialize()
    except Exception:
        pass


def _get_module_executor(module_name: str) -> concurrent.futures.ThreadPoolExecutor:
    """모듈 함수 실행 전용 단일 스레드 executor를 lazy 생성.

    같은 module_name에 대한 모든 호출이 동일 워커 스레드에서 직렬 실행되어
    COM affinity를 유지한다.
    """
    with _module_executors_lock:
        ex = _module_executors.get(module_name)
        if ex is None or getattr(ex, "_shutdown", False):
            ex = concurrent.futures.ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix=f"mod-{module_name}",
                initializer=_module_thread_initializer,
            )
            _module_executors[module_name] = ex
        return ex


def shutdown_module_executors() -> None:
    """앱 종료 시 모든 모듈 executor 정리. (선택적 호출)"""
    with _module_executors_lock:
        for name, ex in list(_module_executors.items()):
            try:
                ex.shutdown(wait=False, cancel_futures=True)
            except Exception as e:
                logger.warning("Failed to shutdown executor for %s: %s", name, e)
        _module_executors.clear()

# Plugins directory
_PLUGINS_DIR = Path(__file__).resolve().parent.parent / "plugins"

# Modules directory (DLL 등 모듈 런타임 파일)
_MODULES_DIR = Path(__file__).resolve().parent.parent / "modules"

# 모듈별 마지막 import 실패 사유 — _get_instance 에서 ValueError 메시지에 포함시켜
# UI(디바이스 연결 실패 메시지) 에 실제 원인 노출. e.g. lge.auto 미설치 시
# "Module 'CANAT' not found" 만 보이던 것을 "... — lge.auto import 실패: No module ..." 로 보강.
_last_import_error: dict[str, str] = {}

# 모듈 가이드 JSON (함수/파라미터 설명)
_GUIDES_FILE = Path(__file__).resolve().parent / "module_guides.json"
_guides_cache: dict | None = None
_guides_mtime: float = 0


def _load_guides() -> dict:
    """가이드 JSON을 로드 (파일 변경 시 자동 리로드)."""
    global _guides_cache, _guides_mtime
    if not _GUIDES_FILE.is_file():
        return {}
    try:
        mtime = _GUIDES_FILE.stat().st_mtime
        if _guides_cache is None or mtime != _guides_mtime:
            with open(_GUIDES_FILE, "r", encoding="utf-8") as f:
                _guides_cache = json.load(f)
            _guides_mtime = mtime
            logger.info("Module guides loaded from %s", _GUIDES_FILE)
    except Exception as e:
        logger.warning("Failed to load module guides: %s", e)
        if _guides_cache is None:
            _guides_cache = {}
    return _guides_cache


def _load_plugin_from_file(py_file: Path):
    """Load a plugin module directly from file path (no package dependency).

    중요: 같은 파일이 ``backend.app.plugins.<name>`` 경로로 정식 import 되어 있을 수도 있다
    (예: ``dlt.py``가 ``from ..plugins.DLTLogging import DLT_HUB``로 사용). 이 경우 새 모듈로
    재로드하면 module-level 싱글톤(``DLT_HUB`` 등)이 분리되어 emit/subscribe가 따로 놀게
    된다. 따라서:
      1) 이미 ``backend.app.plugins.<name>``로 sys.modules에 있으면 **그 객체를 재사용**
      2) 없으면 ``importlib.import_module``로 정식 패키지 경로 import 시도
      3) 그래도 안 되면(파일이 패키지 외부 등) 마지막 수단으로 file-based 로드
    또한 file-based 로드 결과는 sys.modules에 등록해 두 번 이상 생성되는 것을 막는다.

    플랫폼 서브폴더(plugins/linux/, plugins/windows/ 등) 도 지원 — 파일이
    _PLUGINS_DIR 의 한 단계 하위 디렉터리에 있으면 그 디렉터리 이름을 패키지 경로에 포함시킨다.
    """
    # .pyd: "CCIC_BENCH.cp310-win_amd64.pyd" → module_name "CCIC_BENCH"
    module_name = py_file.stem.split(".")[0] if py_file.suffix == ".pyd" else py_file.stem

    # 서브폴더 감지 — plugins/<subpkg>/<file>.py 형태면 subpkg 를 패키지 경로에 끼워넣는다.
    parent = py_file.parent
    if parent != _PLUGINS_DIR and parent.parent == _PLUGINS_DIR:
        subpkg = parent.name
        full_name = f"backend.app.plugins.{subpkg}.{module_name}"
    else:
        full_name = f"backend.app.plugins.{module_name}"

    # 1) 이미 정식 패키지 경로로 import된 모듈이 있으면 재사용 (싱글톤 보존의 핵심)
    cached = sys.modules.get(full_name)
    if cached is not None:
        return cached

    # 2) 정식 패키지 경로로 import 시도
    try:
        return importlib.import_module(full_name)
    except Exception:
        pass

    # 3) 폴백: file-based 로드. sys.modules에 등록해 향후 호출에서 재사용되도록 한다.
    spec = importlib.util.spec_from_file_location(full_name, str(py_file))
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(full_name, None)
        raise
    return mod


def _list_plugin_modules() -> list[dict]:
    """Discover local plugins in the plugins directory."""
    plugins = []
    if not _PLUGINS_DIR.is_dir():
        return plugins
    # OS 별 모듈 가시성 정책 — 사용자 혼동 방지.
    #   Windows: CMD 만 노출, SHELL 숨김
    #   Linux/macOS: SHELL 만 노출, CMD 숨김
    # 둘 다 시나리오 호환을 위해 클래스 자체는 import 가능 (시나리오 파일에 다른 OS 의 모듈명이
    # 박혀 있어도 _import_module_class 로 로드는 됨). UI 드롭다운에서만 제거.
    if sys.platform == "win32":
        _hidden_modules = {"SHELL"}
    else:
        _hidden_modules = {"CMD"}
    # 플랫폼 전용 서브폴더 — 현재 OS 와 매칭되는 것만 탐색에 포함.
    # plugins/linux/*.py 는 Linux 에서만, plugins/windows/*.py 는 Windows 에서만 노출.
    _plat_subdirs: list[Path] = []
    if sys.platform.startswith("linux"):
        _plat_subdirs.append(_PLUGINS_DIR / "linux")
    elif sys.platform == "win32":
        _plat_subdirs.append(_PLUGINS_DIR / "windows")
    seen = set()
    py_files = list(_PLUGINS_DIR.glob("*.py")) + list(_PLUGINS_DIR.glob("*.pyd"))
    for sub in _plat_subdirs:
        if sub.is_dir():
            py_files += list(sub.glob("*.py")) + list(sub.glob("*.pyd"))
    for py_file in py_files:
        if py_file.name.startswith("_"):
            continue
        # .pyd: "CCIC_BENCH.cp310-win_amd64.pyd" → stem "CCIC_BENCH.cp310-win_amd64" → 첫 점 앞
        module_name = py_file.stem.split(".")[0] if py_file.suffix == ".pyd" else py_file.stem
        if module_name in seen or module_name in _hidden_modules:
            continue
        seen.add(module_name)
        try:
            mod = _load_plugin_from_file(py_file)
            if mod is None:
                continue
            cls = getattr(mod, module_name, None)
            if cls is None:
                continue
            # Determine connect_type from constructor signature
            sig = inspect.signature(cls.__init__)
            params = [p for p in sig.parameters if p != "self"]
            if "host" in params:
                connect_type = "socket"
            elif "port" in params or "bps" in params:
                connect_type = "serial"
            else:
                connect_type = "none"
            # Use a cleaner label: strip "Plugin" suffix if present
            label = module_name.replace("Plugin", "") if module_name.endswith("Plugin") else module_name
            plugins.append({
                "name": module_name,
                "label": label,
                "connect_type": connect_type,
                "connect_fields": [],
                "_source": "plugin",
            })
        except Exception as e:
            logger.warning("Cannot load plugin %s: %s", module_name, e)
    return plugins


def list_available_modules() -> list[dict]:
    """List all available modules (lge.auto + local plugins)."""
    # connect_params: fields required when adding a device with this module
    #   "serial" = needs COM port + baudrate (default connection type)
    #   "socket" = needs IP host address
    #   custom list = specific fields [{name, label, type, default?}]
    # connect_fields: extra fields shown in the UI when adding a device
    #   Each field: {name, label, type("text"|"number"|"select"|"multiselect"), default?, options?[]}
    #   multiselect: 고정 options 중 중복 선택(배열). default 는 CSV 문자열, 백엔드로는 CSV 전달.
    modules = [
        {"name": "POWER", "label": "POWER", "connect_type": "serial",
         "connect_fields": []},
        {"name": "RIDEN", "label": "RIDEN", "connect_type": "serial",
         "connect_fields": []},
        {"name": "CAN", "label": "CAN", "connect_type": "can",
         "connect_fields": [
             {"name": "interface", "label": "Interface", "type": "select", "default": "pcan",
              "options": ["pcan", "vector", "kvaser", "socketcan", "ixxat"]},
             {"name": "channel", "label": "Channel", "type": "text", "default": "PCAN_USBBUS1"},
             {"name": "bitrate", "label": "Bitrate", "type": "select", "default": "500000",
              "options": ["125000", "250000", "500000", "1000000"]},
             {"name": "fd", "label": "CAN FD", "type": "select", "default": "False",
              "options": ["True", "False"]},
         ]},
        {"name": "CANoe_RBS", "label": "CANoe_RBS (py-canoe RBS)", "connect_type": "none",
         "connect_fields": []},
        {"name": "CANoe_Ctrl", "label": "CANoe_Ctrl (Vector HW)", "connect_type": "none",
         "connect_fields": [
             {"name": "device_info", "label": "채널 구성", "type": "object_list",
              "default_items": [
                  {"channel": 0, "app_name": "CANoe", "bitrate": 500000, "data_bitrate": None, "is_fd": False},
              ],
              "item_fields": [
                  {"name": "channel", "label": "Channel", "type": "select", "default": "0",
                   "options": ["0", "1", "2", "3", "4", "5", "6", "7"]},
                  {"name": "app_name", "label": "App Name", "type": "select", "default": "CANoe",
                   "options": ["CANoe", "CANalyzer", "CANalyzer Demo", "CANoe Demo"]},
                  {"name": "bitrate", "label": "Bitrate", "type": "select", "default": "500000",
                   "options": ["125000", "250000", "500000", "1000000"]},
                  {"name": "is_fd", "label": "CAN FD", "type": "select", "default": "False",
                   "options": ["True", "False"]},
                  {"name": "data_bitrate", "label": "Data Bitrate (FD)", "type": "select", "default": "None",
                   "options": ["None", "1000000", "2000000", "4000000", "5000000", "8000000"]},
              ]},
         ]},
        {"name": "CANAT", "label": "CANAT", "connect_type": "serial",
         "connect_fields": [
             {"name": "log_path", "label": "Log Path", "type": "text", "default": ""},
             {"name": "ch1_fd", "label": "CH1 CAN FD", "type": "select", "default": "True",
              "options": ["True", "False"]},
             {"name": "ch2_fd", "label": "CH2 CAN FD", "type": "select", "default": "False",
              "options": ["True", "False"]},
         ]},
        {"name": "BENCH", "label": "BENCH", "connect_type": "socket",
         "connect_fields": []},
        {"name": "WoohyunBench", "label": "WoohyunBench", "connect_type": "socket",
         "connect_fields": [
             {"name": "udp_port", "label": "UDP Port", "type": "number", "default": "25000"},
             {"name": "signal_file", "label": "CAN FD 신호 정의 파일 (.xls/.xlsx/.CAN, 선택)", "type": "text", "default": ""},
         ]},
        {"name": "IVIQEBenchIOClient", "label": "IVIQEBenchIOClient", "connect_type": "serial",
         "connect_fields": []},
        {"name": "SP25Bench", "label": "SP25Bench", "connect_type": "serial",
         "connect_fields": []},
        {"name": "Uart", "label": "Uart", "connect_type": "serial",
         "connect_fields": []},
        {"name": "Ignition", "label": "Ignition", "connect_type": "serial",
         "connect_fields": []},
        {"name": "KeysightPower", "label": "KeysightPower", "connect_type": "socket",
         "connect_fields": []},
        {"name": "SSHManager", "label": "SSHManager", "connect_type": "socket",
         "connect_fields": []},
        {"name": "AudioLibrary", "label": "AudioLibrary", "connect_type": "none",
         "connect_fields": []},
        {"name": "ImageProcessing", "label": "ImageProcessing", "connect_type": "none",
         "connect_fields": []},
        {"name": "DLTLogging", "label": "DLTLogging", "connect_type": "socket",
         "connect_fields": [
             {"name": "port", "label": "DLT Port", "type": "number", "default": "3490"},
         ]},
        {"name": "SerialLogging", "label": "SerialLogging", "connect_type": "serial",
         "connect_fields": []},
        {"name": "SmartBench", "label": "SmartBench", "connect_type": "socket",
         "connect_fields": [
             {"name": "port", "label": "TCP Port", "type": "number", "default": "5000"},
         ]},
        {"name": "DLTViewer", "label": "DLTViewer", "connect_type": "socket",
         "connect_fields": [
             {"name": "port", "label": "DLT Port", "type": "number", "default": "3490"},
             {"name": "project_file", "label": "프로젝트 파일 (.dlp)", "type": "text", "default": ""},
         ]},
        {"name": "MLP", "label": "MLP", "connect_type": "none",
         "connect_fields": []},
        {"name": "PCANClient", "label": "PCANClient", "connect_type": "none",
         "connect_fields": []},
        {"name": "TigrisCheck", "label": "TigrisCheck", "connect_type": "none",
         "connect_fields": []},
        {"name": "Trace", "label": "Trace", "connect_type": "none",
         "connect_fields": []},
        {"name": "COMMON_WINDOWS", "label": "COMMON_WINDOWS", "connect_type": "none",
         "connect_fields": []},
        {"name": "Android", "label": "Android", "connect_type": "none",
         "connect_fields": []},
        {"name": "HKMC6th", "label": "HKMC6th", "connect_type": "none",
         "connect_fields": []},
        {"name": "HKMC5thWide", "label": "HKMC5thWide (Wide)", "connect_type": "none",
         "connect_fields": []},
        {"name": "VisionCamera", "label": "VisionCamera", "connect_type": "vision_camera",
         "connect_fields": [
             {"name": "mac", "label": "MAC Address", "type": "text", "default": ""},
             {"name": "model", "label": "Model", "type": "text", "default": "exo264CGE"},
             {"name": "serial", "label": "Serial Number", "type": "text", "default": ""},
             {"name": "ip", "label": "IP Address", "type": "text", "default": ""},
             {"name": "subnetmask", "label": "Subnet Mask", "type": "text", "default": "255.255.0.0"},
         ]},
        # Linux 전용 플러그인 — plugins/linux/ 서브폴더에 위치. connect_type="none" 이지만
        # constructor 인자가 많아서 UI 입력 폼이 필요하다.
        # TH 의 필드는 Reference/TH/connect_th.sh 의 USER CONFIG 블록과 1:1 대응.
        # eth_if 는 radmoon(USB Ethernet) 스캔 결과에서 자동 채워짐. th_home 은 수동 선택.
        {"name": "TH", "label": "TH (Test Harness, Linux)", "connect_type": "none",
         "connect_fields": [
             # 표시 항목 — 사용자가 매번 입력/확인.
             {"name": "eth_if", "label": "USB Ethernet 인터페이스 (RAD_Moon)", "type": "text", "default": ""},
             {"name": "th_home", "label": "TH 버전 디렉터리 (선택 필요)", "type": "folder", "default": ""},
             {"name": "th_root", "label": "TH root (host_ends_setup.sh / ensure-adb.sh 위치)",
              "type": "folder", "default": "/home/cdc/Desktop/TH"},
             {"name": "microservice_gateways", "label": "게이트웨이 번호 (공백 구분, 예: 57 89 191 207)",
              "type": "text", "default": ""},
             {"name": "sudo_password", "label": "sudo 비밀번호 (passwordless 미설정 시 필수)",
              "type": "password", "default": ""},
             # 숨김 항목 — connect_th.sh USER CONFIG 디폴트 그대로 사용. extra_fields 에는 들어가서
             # 백엔드 TH 생성자에 전달됨. UI 폼에는 표시 안 함 (요청에 따라 깔끔하게).
             {"name": "host_ip", "label": "Host IP / mask", "type": "text", "default": "192.168.1.152/24",
              "hidden": True},
             {"name": "cvd_br", "label": "CVD bridge 이름", "type": "text", "default": "cvd-ebr",
              "hidden": True},
             {"name": "rbvm_ip", "label": "RBVM ADB", "type": "text", "default": "192.168.140.1:5555",
              "hidden": True},
             {"name": "th_adb", "label": "CVD ADB host:port", "type": "text", "default": "0.0.0.0:6520",
              "hidden": True},
             {"name": "grpc_ip", "label": "gRPC broker (client.py --ip_address)", "type": "text",
              "default": "192.168.1.99:50051", "hidden": True},
             {"name": "python_bin", "label": "Python 인터프리터 (빈 칸 = ReplayKit 임베드 Python)",
              "type": "text", "default": "", "hidden": True},
             {"name": "panel", "label": "PySide6 시각화 패널", "type": "select", "default": "True",
              "options": ["True", "False"], "hidden": True},
             {"name": "panel_trigger", "label": "패널 점등 트리거 토큰", "type": "text",
              "default": "GEAR_LEVER_ACCEPTED_T_REVERSE", "hidden": True},
             {"name": "auto_setup", "label": "등록 시 자동 Setup 실행", "type": "select", "default": "True",
              "options": ["True", "False"], "hidden": True},
             {"name": "launch_cvd", "label": "Setup 시 launch_cvd 자동 spawn", "type": "select", "default": "True",
              "options": ["True", "False"], "hidden": True},
             {"name": "run_microservice", "label": "Setup 시 게이트웨이(th_run_microservice.sh) 자동 기동",
              "type": "select", "default": "True", "options": ["True", "False"], "hidden": True},
             # ── 연결 해제/서버 종료 시 정리 (Disconnect) ──
             {"name": "microservice_stop_cmd",
              "label": "게이트웨이 정지 명령 (해제 시, 비우면 skip)", "type": "text",
              "default": "", "hidden": True},
             {"name": "stop_cvd_on_disconnect", "label": "해제 시 cuttlefish 종료",
              "type": "select", "default": "False", "options": ["True", "False"], "hidden": True},
         ]},
        # SCAR — 런타임(REST/Docker) + 등록 시 netns VLAN 자동 구성.
        #   표시 필드: SCAR 설치 가이드 "2. Network Configuration" 에서 사용자가 결정해야 하는 값.
        #   iface 는 SCAR/radmoon 스캔 결과의 인터페이스로 자동 채워진다 (없으면 수동 입력).
        #   숨김 필드: 가이드 예시 디폴트 그대로 — extra_fields 로 SCAR 생성자에 전달되지만 폼엔 미표시.
        {"name": "SCAR", "label": "SCAR (SDV Control, Linux)", "connect_type": "none",
         "connect_fields": [
             {"name": "api_base", "label": "SCAR REST URL", "type": "text",
              "default": "http://localhost:8081", "hidden": True},
             {"name": "container", "label": "Docker container 이름", "type": "text", "default": "scar"},
             # ── netns VLAN 구성 (등록 시 자동 셋업) ──
             {"name": "vlan_config_dir", "label": "sdv_vlan_config 디렉터리 (netns.sh 위치, 비우면 netns 건너뜀)",
              "type": "folder", "default": ""},
             {"name": "iface", "label": "네트워크 인터페이스 (RAD_Moon/Technica, 스캔 자동 채움)",
              "type": "text", "default": "", "hidden": True},
             {"name": "net_mode", "label": "구성 모드", "type": "select", "default": "multiverse",
              "options": ["multiverse", "standalone"]},
             # cvd-ebr(TH/cuttlefish) 보존 — net_config 에 cuttlefish=true. multiverse 도 적용해야
             #   netns clean 이 cvd-ebr 를 flush 한 뒤 cuttlefish 용으로 복원, TH adb 가 안 끊긴다.
             {"name": "cuttlefish", "label": "cuttlefish(cvd-ebr) 보존", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
             {"name": "ends", "label": "ENDS 버전", "type": "text", "default": "FaceStep1_2025_R10"},
             # stub_ecus: netns 에 namespace 를 만들 ECU 목록(netns valid 이름).
             #   ⚠️ /start 의 'Simulated ECU target'(예: PCU_PROXY_FrontEnd_PIU_Mst)과 이름이 다르다!
             #   netns 는 base 이름(PCU_PROXY_FrontEnd). 빠진 ECU 의 서비스는 "NETNS is not configured"
             #   로 start 실패하고, netns apply 자체가 invalid ECU 면 Setup 실패→post_connect skip.
             #   기본값은 start_services 기본(PCU_PROXY_FrontEnd_PIU_Mst 서비스)에 맞춘 base ECU 셋.
             {"name": "stub_ecus",
              "label": "stub_ecus (netns ECU, 콤마 구분). /start의 _PIU_Mst 접미사 빼고 base 이름 사용",
              "type": "text", "default": "PIU_Mst, PCU_PROXY_FrontEnd", "hidden": True},
             {"name": "sudo_password", "label": "sudo 비밀번호 (passwordless 미설정 시 필수)",
              "type": "password", "default": ""},
             # ── 재기동 스크립트 (scar.sh) ──
             # 폴더 찾아보기로 scar.sh 가 있는 디렉터리를 고르면 자동으로 '/scar.sh' 부착.
             {"name": "reconnect_script", "label": "scar.sh 경로 (폴더 선택 시 /scar.sh 자동 부착)",
              "type": "folder", "append": "/scar.sh", "default": ""},
             {"name": "reconnect_args", "label": "scar.sh 인자 (공백 구분)", "type": "text",
              "default": "-t 2.2.0 --ui --arti tls"},
             {"name": "reconnect_cwd", "label": "scar.sh cwd (선택)", "type": "text", "default": "",
              "hidden": True},
             # cold boot([0] 컨테이너 정리→재기동→start_ui.sh) 실측 60s 초과 벤치 존재 — 150s.
             {"name": "reconnect_wait_s", "label": "재기동 후 8081 폴링 상한 (초)", "type": "number", "default": "150",
              "hidden": True},
             # ── 숨김: 가이드 디폴트 ──
             {"name": "standalone_ip", "label": "standalone 모드 IP", "type": "text",
              "default": "192.168.1.10", "hidden": True},
             {"name": "ufw", "label": "ufw", "type": "select", "default": "off",
              "options": ["off", "on"], "hidden": True},
             {"name": "log_folder", "label": "log_folder", "type": "text", "default": "/tmp",
              "hidden": True},
             {"name": "auto_setup", "label": "등록 시 자동 Setup(netns) 실행", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
             {"name": "netns_clean", "label": "apply 전 --clean 수행", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
             {"name": "launch_scar", "label": "Setup 중 scar.sh 자동 기동", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
             {"name": "clean_container_on_connect", "label": "최초 연결 시 컨테이너 정리 후 연결 (scar.sh -c, 세션 1회)",
              "type": "select", "default": "True", "options": ["True", "False"]},
             {"name": "stop_container_on_disconnect", "label": "연결 해제 시 컨테이너도 정지 (완전 정리)",
              "type": "select", "default": "False", "options": ["True", "False"]},
             # ── 컨테이너 내부 UI 재기동 (host scar.sh -it TTY 함정 우회) ──
             {"name": "ui_dir", "label": "컨테이너 안 start_ui.sh 디렉터리", "type": "text",
              "default": "/home/scar/ui", "hidden": True},
             {"name": "ui_home", "label": "start_ui.sh HOME_SCAR", "type": "text",
              "default": "/home/scar", "hidden": True},
             # ── 연결 직후 UI 자동화 (port 3000 제어 백엔드) ──
             #   UI 정적 프론트는 8081, 실제 제어 REST 는 3000. 버전선택/토글은 3000 으로 간다.
             #   UI 버전(ENDS)은 별도 필드 없이 상단 'ENDS 버전'(ends)에서 도출(_resolve_ui_version):
             #   netns ENDS(FaceStep1_2025_R10) → /list/ends 매칭 → UI ENDS(2025_r10).
             # ── Bench Capabilities (8081 'Select Bench Capabilities' 최초 셋업) ──
             #   이게 안 들어가면 서버 benchConfig 에 capabilities/benchcontrol 키가 안 생겨
             #   토글이 scar-server.js 에서 '.length of undefined' 로 죽고(500), 8081 은 최초 화면에 멈춤.
             #   표시이름 → 서버 id 변환(소문자+공백→'_')은 SCAR._compute_cap_id 가 처리.
             {"name": "capabilities", "label": "Bench Capabilities (중복 선택)", "type": "multiselect",
              "default": "Multiverse, Without PCU HW",
              "options": ["RelayCard", "Multiverse", "Without PCU HW", "Without PCU HW but CF PCU",
                          "With PCU HW", "PCU DTOOL", "CAN Multiverse"]},
             # ethernet_interfaces: SomeIP 모니터링/NETWORK_INTERFACES 용. 8081 auto-advance 3조건 중 하나
             #   (ethernet_interfaces && benchcontrol && capabilities). 비우면 SCAR 가 iface(스캔값)로 대체.
             #   유효값은 벤치의 GET :3000/setup/list/interfaces(=ls /sys/class/net, cvd/lo/docker 제외).
             {"name": "ethernet_interfaces", "label": "Ethernet 인터페이스 (SomeIP, 콤마 구분 / 비우면 iface 사용)",
              "type": "text", "default": ""},
             # 토글 전에 자동 start 할 SOME/IP 서비스 (UI 의 'Simulated ECU target' + 'Service to
             #   simulate/register' → Start 와 동일). bench 토글은 InfrastructureGotoSleep 등이 떠
             #   있어야 유지되므로 토글보다 먼저 순서대로 start. 해당 ECU 는 stub_ecus 에도 포함돼야 함.
             {"name": "start_services", "label": "연결 후 자동 start할 서비스 (토글 전, 순서대로)",
              "type": "object_list", "hidden": True,
              "default_items": [
                  {"ecu": "PCU_PROXY_FrontEnd_PIU_Mst", "service": "VehicleUtcTime"},
                  {"ecu": "PCU_PROXY_FrontEnd_PIU_Mst", "service": "InfrastructureGotoSleep"},
              ],
              "item_fields": [
                  {"name": "ecu", "label": "Simulated ECU target", "type": "text",
                   "default": "PCU_PROXY_FrontEnd_PIU_Mst"},
                  {"name": "service", "label": "Service to simulate/register", "type": "text",
                   "default": ""},
              ]},
             {"name": "bench_toggle", "label": "연결 후 활성화할 Bench 토글 이름 (비우면 건너뜀)",
              "type": "text", "default": "Wake up/Sleep minimal CDC/SA", "hidden": True},
             {"name": "control_base", "label": "SCAR UI 제어 API (port 3000)", "type": "text",
              "default": "http://localhost:3000", "hidden": True},
             {"name": "post_connect", "label": "연결 직후 버전선택+토글 자동 실행", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
             {"name": "bench_state", "label": "토글 상태", "type": "select", "default": "switched",
              "options": ["switched", "unswitched"], "hidden": True},
             {"name": "auto_register", "label": "미등록 토글 자동 등록", "type": "select",
              "default": "True", "options": ["True", "False"], "hidden": True},
         ]},
    ]
    available = []
    for m in modules:
        # HKMC6th는 ReplayKit 내장 서비스(HKMC6thService) 기반 가상 모듈
        if m["name"] == "HKMC6th":
            m["_source"] = "internal"
            available.append(m)
            continue
        # HKMC5thWide는 ReplayKit 내장 서비스(HKMC5thWideService) 기반 가상 모듈
        if m["name"] == "HKMC5thWide":
            m["_source"] = "internal"
            available.append(m)
            continue
        try:
            __import__(f"lge.auto.{m['name']}", fromlist=[m["name"]])
            m["_source"] = "lge.auto"
            available.append(m)
        except Exception:
            # lge.auto에 없으면 플러그인 폴백 — 루트와 OS 전용 서브폴더 모두 확인.
            if _find_plugin_file(m["name"]) is not None:
                m["_source"] = "plugin"
                available.append(m)

    # 아직 등록되지 않은 추가 플러그인
    listed_names = {m["name"] for m in available}
    for p in _list_plugin_modules():
        if p["name"] not in listed_names:
            available.append(p)
    return available


def _ensure_module_deps(module_name: str, module_dir: Path) -> None:
    """모듈이 필요로 하는 native library 를 modules/ 폴더에서 모듈 위치로 복사.

    플랫폼별 확장자: Windows .dll, Linux .so, macOS .dylib.
    """
    import shutil
    import sys as _sys
    if not _MODULES_DIR.is_dir():
        return
    if _sys.platform == "win32":
        patterns = ("*.dll",)
    elif _sys.platform == "darwin":
        patterns = ("*.dylib", "*.so")
    else:
        patterns = ("*.so",)
    for pat in patterns:
        for lib in _MODULES_DIR.glob(pat):
            dest = module_dir / lib.name
            if not dest.exists():
                shutil.copy2(str(lib), str(dest))
                logger.info("Copied %s → %s", lib.name, dest)


def _candidate_plugin_dirs() -> list[Path]:
    """플러그인 파일 후보 디렉터리. 루트 + 현재 OS 의 전용 서브폴더."""
    dirs = [_PLUGINS_DIR]
    if sys.platform.startswith("linux"):
        dirs.append(_PLUGINS_DIR / "linux")
    elif sys.platform == "win32":
        dirs.append(_PLUGINS_DIR / "windows")
    return dirs


def _find_plugin_file(module_name: str) -> Optional[Path]:
    """module_name 에 해당하는 .py(없으면 .pyd) 파일을 후보 디렉터리에서 찾는다."""
    for d in _candidate_plugin_dirs():
        py_file = d / f"{module_name}.py"
        if py_file.is_file():
            return py_file
        pyd_files = list(d.glob(f"{module_name}.*.pyd"))
        if pyd_files:
            return pyd_files[0]
    return None


def _import_module_class(module_name: str):
    """Import and return the class for a given module name (lge.auto or plugin)."""
    # 이전 실패 사유 초기화 — 이번 호출이 성공해도 잔재가 남지 않도록
    _last_import_error.pop(module_name, None)
    plugin_err: Optional[str] = None
    lge_err: Optional[str] = None

    # Try local plugin first (file-based loading to avoid package path issues)
    # .py 우선, 없으면 .pyd (배포 환경). 루트 폴더 → OS 전용 서브폴더 순.
    py_file = _find_plugin_file(module_name)
    if py_file is None:
        py_file = _PLUGINS_DIR / f"{module_name}.py"  # 존재하지 않지만 아래 is_file() 가 False 로 폴스루
    if py_file.is_file():
        try:
            _ensure_module_deps(module_name, py_file.parent)
            mod = _load_plugin_from_file(py_file)
            if mod is not None:
                cls = getattr(mod, module_name, None)
                if cls is not None:
                    return cls
                plugin_err = f"plugin module loaded but class '{module_name}' not found"
            else:
                plugin_err = "plugin module returned None"
        except Exception as e:
            logger.warning("Cannot load plugin %s from file: %s", module_name, e)
            plugin_err = f"plugin load failed: {e}"

    # Try lge.auto
    try:
        mod = __import__(f"lge.auto.{module_name}", fromlist=[module_name])
        # lge.auto 모듈 위치에 DLL 등 의존 파일 복사
        mod_dir = Path(mod.__file__).parent if hasattr(mod, "__file__") else None
        if mod_dir:
            _ensure_module_deps(module_name, mod_dir)
        cls = getattr(mod, module_name, None)
        if cls is not None:
            return cls
        lge_err = f"lge.auto.{module_name} loaded but class '{module_name}' missing"
    except Exception as e:
        logger.warning("Cannot import module %s: %s", module_name, e)
        lge_err = f"lge.auto.{module_name} import failed: {e}"

    # 두 경로 모두 실패 → 사용자에게 노출할 수 있도록 기록
    parts = [p for p in (plugin_err, lge_err) if p]
    _last_import_error[module_name] = " | ".join(parts) if parts else "no plugin file and lge.auto import not attempted"
    return None


def _plugin_file_mtime(module_name: str) -> float:
    """플러그인 .py(없으면 .pyd)의 mtime. 찾지 못하면 0. OS 전용 서브폴더 포함."""
    py_file = _find_plugin_file(module_name)
    if py_file is None:
        return 0.0
    try:
        return py_file.stat().st_mtime
    except OSError:
        return 0.0


def get_module_functions(module_name: str) -> list[dict]:
    """Get all public callable methods of a module's main class.

    플러그인 파일 또는 가이드 JSON이 변경되면 캐시가 자동 무효화된다.
    """
    plugin_mtime = _plugin_file_mtime(module_name)
    guides_mtime = _GUIDES_FILE.stat().st_mtime if _GUIDES_FILE.is_file() else 0.0
    cached = _module_functions_cache.get(module_name)
    if cached is not None:
        cpm, cgm, cfuncs = cached
        if cpm == plugin_mtime and cgm == guides_mtime:
            return cfuncs

    # HKMC6th: ReplayKit 내장 HKMC6thService를 가상 모듈로 노출.
    # 각 디바이스(hkmc_agent)별 인스턴스를 device_manager가 관리하므로
    # 클래스 자체에서 introspect 한다(_get_instance를 거치지 않음).
    if module_name == "HKMC6th":
        from .hkmc6th_service import HKMC6thService
        # 모듈 스텝에서 노출하지 않을 메서드 (연결 lifecycle, 비동기 wrapper, 키 오버라이드 등)
        # tap/swipe/long_press/repeat_tap는 전용 HKMC_* 스텝 타입으로 이미 제공되므로 제외.
        # send_key/send_key_by_name은 hkmc_key 스텝 타입으로 이미 제공되므로 제외.
        # screencap_bytes/get_screen_size는 모듈 스텝에서 활용도가 낮아 제외.
        excluded = {
            "connect", "disconnect", "is_connected",
            "set_key_overrides", "get_key_overrides", "resolve_key", "get_info",
            "tap", "swipe", "long_press", "repeat_tap",
            "send_key", "send_key_by_name",
            "screencap_bytes", "get_screen_size",
        }
        functions = []
        for name in sorted(dir(HKMC6thService)):
            if name.startswith("_") or name.startswith("async_") or name in excluded:
                continue
            attr = getattr(HKMC6thService, name, None)
            if not callable(attr):
                continue
            try:
                sig = inspect.signature(attr)
            except (ValueError, TypeError):
                continue
            params = []
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                param_info: dict[str, Any] = {"name": pname, "required": True}
                if p.default is not inspect.Parameter.empty:
                    param_info["required"] = False
                    param_info["default"] = repr(p.default)
                params.append(param_info)
            functions.append({"name": name, "params": params})
        # 가이드 병합
        guides = _load_guides()
        mod_guide = guides.get(module_name, {})
        func_guides = mod_guide.get("functions", {})
        for fn in functions:
            fg = func_guides.get(fn["name"], {})
            fn["description"] = fg.get("description", "")
            param_guides = fg.get("params", {})
            for p in fn["params"]:
                p["description"] = param_guides.get(p["name"], "")
        _module_functions_cache[module_name] = (plugin_mtime, guides_mtime, functions)
        return functions

    # HKMC5thWide: HKMC5thWideService 기반 가상 모듈 (hkmc5th_wide_agent 디바이스 전용)
    if module_name == "HKMC5thWide":
        from .hkmc5th_wide_service import HKMC5thWideService
        excluded = {
            "connect", "disconnect", "is_connected",
            "set_key_overrides", "get_key_overrides", "resolve_key", "get_info",
            "tap", "swipe", "long_press", "repeat_tap",
            "send_key", "send_key_by_name", "send_key_message",
            "screencap_bytes", "get_screen_size",
            "req_resource_info",
        }
        functions = []
        for name in sorted(dir(HKMC5thWideService)):
            if name.startswith("_") or name.startswith("async_") or name in excluded:
                continue
            attr = getattr(HKMC5thWideService, name, None)
            if not callable(attr):
                continue
            try:
                sig = inspect.signature(attr)
            except (ValueError, TypeError):
                continue
            params = []
            for pname, p in sig.parameters.items():
                if pname == "self":
                    continue
                param_info: dict[str, Any] = {"name": pname, "required": True}
                if p.default is not inspect.Parameter.empty:
                    param_info["required"] = False
                    param_info["default"] = repr(p.default)
                params.append(param_info)
            functions.append({"name": name, "params": params})
        guides = _load_guides()
        mod_guide = guides.get(module_name, {})
        func_guides = mod_guide.get("functions", {})
        for fn in functions:
            fg = func_guides.get(fn["name"], {})
            fn["description"] = fg.get("description", "")
            param_guides = fg.get("params", {})
            for p in fn["params"]:
                p["description"] = param_guides.get(p["name"], "")
        _module_functions_cache[module_name] = (plugin_mtime, guides_mtime, functions)
        return functions

    # OCR: 가상 모듈 — ocr_service를 통해 현재 화면 텍스트 검출/클릭
    if module_name == "OCR":
        # 공통 language 파라미터 정의 — 모든 OCR 함수에 동일하게 추가
        _language_param = {
            "name": "language", "required": False, "default": "'korean'",
            "description": (
                "OCR 인식 언어. 'korean'(한+영+숫자, 기본), 'english', 'japan', 'chinese', "
                "'latin'(스/프/독/이 등), 'cyrillic', 'arabic', 'devanagari'. "
                "모델 미설치 언어는 번들 기본(중국어)으로 폴백 — "
                "설치: python scripts/download_ocr_models.py <language>"
            ),
        }
        functions = [
            {
                "name": "CheckText",
                "description": "현재 화면에서 텍스트 존재 여부를 판단합니다. 쉼표 구분으로 여러 개 지정 시 모두 존재해야 PASS (AND 조건).",
                "params": [
                    {"name": "text", "required": True,
                     "description": "찾을 텍스트 (쉼표로 여러 개 지정 가능, 예: 'OK,Save')"},
                    {"name": "mode", "required": False, "default": "'Full Screen'",
                     "description": "검색 범위: 'Full Screen' 또는 'Region'"},
                    {"name": "region", "required": False, "default": "'0,0,0,0'",
                     "description": "영역 'x,y,width,height' (Region 모드, 쉼표 구분)"},
                    {"name": "threshold", "required": False, "default": "'0.8'",
                     "description": "유사도 임계값 (0.0~1.0, 기본 0.8)"},
                    _language_param,
                ],
            },
            {
                "name": "ClickText",
                "description": "현재 화면에서 텍스트를 찾아 클릭합니다. 찾지 못하면 FAIL 반환.",
                "params": [
                    {"name": "text", "required": True, "description": "클릭할 텍스트"},
                    {"name": "mode", "required": False, "default": "'Full Screen'",
                     "description": "검색 범위: 'Full Screen' 또는 'Region'"},
                    {"name": "region", "required": False, "default": "'0,0,0,0'",
                     "description": "영역 'x,y,width,height' (Region 모드, 쉼표 구분)"},
                    {"name": "threshold", "required": False, "default": "'0.8'",
                     "description": "유사도 임계값 (0.0~1.0, 기본 0.8)"},
                    _language_param,
                ],
            },
            {
                "name": "ExtractAllText",
                "description": "현재 화면(또는 지정 영역)의 모든 텍스트를 추출하여 결과 메시지로 반환합니다. 디버깅 및 시나리오 작성 시 화면에 어떤 텍스트가 있는지 데이터로 확인하는 용도. 항상 PASS (텍스트가 없어도 PASS). 한 글자짜리 검출은 아이콘 오인식이 많아 기본 제외 (min_length=2).",
                "params": [
                    {"name": "mode", "required": False, "default": "'Full Screen'",
                     "description": "검색 범위: 'Full Screen' 또는 'Region'"},
                    {"name": "region", "required": False, "default": "'0,0,0,0'",
                     "description": "영역 'x,y,width,height' (Region 모드, 쉼표 구분)"},
                    {"name": "min_length", "required": False, "default": "'2'",
                     "description": "결과에 포함할 최소 글자 수 (기본 2 — 아이콘 오인식 제거). 1로 설정하면 모든 결과 표시."},
                    _language_param,
                ],
            },
        ]
        _module_functions_cache[module_name] = (plugin_mtime, guides_mtime, functions)
        return functions

    # Android: 네이티브 lge.auto.Android 함수들은 노출하지 않고
    # ReplayKit 자체 ADBService 기반의 Send_adb_command 단일 가상 함수만 제공
    if module_name == "Android":
        functions = [
            {
                "name": "Send_adb_command",
                "params": [
                    {"name": "command", "required": True},
                    {"name": "serial", "required": False, "default": "''"},
                ],
            },
            {
                "name": "StartLogging",
                "params": [
                    {"name": "serial", "required": False, "default": "''"},
                    {"name": "clear", "required": False, "default": "True"},
                ],
            },
            {
                "name": "StopLogging",
                "params": [
                    {"name": "serial", "required": False, "default": "''"},
                    {"name": "save_path", "required": False, "default": "''"},
                ],
            },
            {
                "name": "ClearLog",
                "params": [
                    {"name": "serial", "required": False, "default": "''"},
                ],
            },
            {
                "name": "Monitor_pass_on_keyword",
                "params": [
                    {"name": "keyword", "required": True},
                    {"name": "time", "required": False, "default": "5"},
                    {"name": "serial", "required": False, "default": "''"},
                    {"name": "include_past", "required": False, "default": "True"},
                ],
            },
            {
                "name": "Monitor_fail_on_keyword",
                "params": [
                    {"name": "keyword", "required": True},
                    {"name": "time", "required": False, "default": "5"},
                    {"name": "serial", "required": False, "default": "''"},
                    {"name": "include_past", "required": False, "default": "True"},
                ],
            },
        ]
        # 가이드 병합 후 캐싱하고 즉시 반환
        guides = _load_guides()
        mod_guide = guides.get(module_name, {})
        func_guides = mod_guide.get("functions", {})
        for fn in functions:
            fg = func_guides.get(fn["name"], {})
            fn["description"] = fg.get("description", "")
            param_guides = fg.get("params", {})
            for p in fn["params"]:
                p["description"] = param_guides.get(p["name"], "")
        _module_functions_cache[module_name] = (plugin_mtime, guides_mtime, functions)
        return functions

    cls = _import_module_class(module_name)
    if cls is None:
        return []

    # 모듈 스텝 UI에서 숨길 메서드 (시나리오는 자동으로 연결을 관리하므로 노출 불필요)
    per_module_excluded: dict[str, set[str]] = {
        "SerialLogging": {"Connect", "Disconnect", "IsConnected"},
        # SCAR.Disconnect 는 device_manager 연결해제/등록삭제 시 netns 복원용으로 자동 호출 —
        # 시나리오 스텝에 노출할 필요 없음 (Reconnect/Setup/SendApi/Exec 등은 그대로 노출).
        "SCAR": {"Disconnect"},
        "CMD": {"CheckCapture", "RunCapture", "ListBackground"},
        # SHELL 은 CMD 의 Linux/macOS 대응 모듈 — 노출 정책 동일
        "SHELL": {"CheckCapture", "RunCapture", "RunBackground", "ListBackground"},
        # CANoe_RBS 내부 비교/유틸 헬퍼는 시나리오 스텝에 노출할 필요 없음
        # (CompareValue/CompareString/compareDIAG/GetByteDataList 는 Check* 함수가 내부 사용,
        #  make_timestamp_log_dir 는 Init 내부 사용).
        "CANoe_RBS": {
            "CompareValue", "CompareString", "compareDIAG",
            "GetByteDataList", "make_timestamp_log_dir",
        },
    }
    excluded = per_module_excluded.get(module_name, set())

    # 모듈 스텝 UI에 노출할 메서드 화이트리스트.
    # 지정된 모듈은 이 set에 포함된 이름만 드롭다운에 표시. 미지정 모듈은 전체 노출.
    per_module_included: dict[str, set[str]] = {
        "IVIQEBenchIOClient": {
            "BatteryOnOff", "BatteryVoltage",
            "ACCOnOff", "AccVoltage",
            "IGNControl", "IGN3Control",
            "USBFrontSwitchControl", "USBRearSwitchControl",
        },
        # DLTLogging: 콤보 목록/가이드에는 문서화된 핵심 함수만 노출.
        # (count/assert/fail_on/StartSave/StopSave/MarkStep/SearchAll/SearchRange/GetStatus/
        #  ClearLogs 및 미문서 헬퍼 GetRecentLogs/GetStepMarks/IsConnected/SearchAllDetailed/
        #  SearchSection/SearchSectionDetailed/WatchAndStop/get_count_details 등은 제외)
        "DLTLogging": {
            "StartLogging", "StopLogging",
            "WaitLog", "ExpectFound", "ExpectNotFound",
        },
    }
    included = per_module_included.get(module_name)

    functions = []
    for name in sorted(dir(cls)):
        if name.startswith("_") or name in excluded:
            continue
        if included is not None and name not in included:
            continue
        attr = getattr(cls, name, None)
        if not callable(attr):
            continue
        try:
            sig = inspect.signature(attr)
        except (ValueError, TypeError):
            continue

        params = []
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            param_info: dict[str, Any] = {"name": pname, "required": True}
            if p.default is not inspect.Parameter.empty:
                param_info["required"] = False
                param_info["default"] = repr(p.default)
            params.append(param_info)

        functions.append({
            "name": name,
            "params": params,
        })

    # CANAT.CAN_PANEL — CAN 반응속도 측정 시각화 패널 가상 함수 (실제 .pyd 에는 없음).
    # 인자: state(on/off 콤보) + send_can_message 인자 그대로.
    if module_name == "CANAT":
        functions.append({
            "name": "CAN_PANEL",
            "params": [
                {"name": "state", "required": False, "default": "'on'"},
                {"name": "message_id", "required": False, "default": "''"},
                {"name": "cycle_time", "required": False, "default": "0"},
                {"name": "can_message", "required": False, "default": "''"},
                {"name": "bus_channel", "required": False, "default": "0"},
                {"name": "message_type", "required": False, "default": "'FD'"},
                {"name": "x", "required": False, "default": "''"},
                {"name": "y", "required": False, "default": "''"},
                {"name": "width", "required": False, "default": "300"},
                {"name": "height", "required": False, "default": "300"},
            ],
        })

    # SSHManager: 스트리밍 send_command 가상 함수 추가 (실제 클래스에는 없음)
    if module_name == "SSHManager":
        functions.append({
            "name": "send_command_stream",
            "params": [
                {"name": "command", "required": True},
            ],
        })
        # CMD.Check / Check_Logic 와 동일한 합부 판정 가상 함수 (실제 클래스에는 없음).
        # send_command 와 같은 방식으로 명령을 실행한 뒤 출력을 기대값/키워드로 판정한다.
        functions.append({
            "name": "Check",
            "params": [
                {"name": "command", "required": True},
                {"name": "expected", "required": False, "default": "''"},
                {"name": "match_mode", "required": False, "default": "'contains'"},
                {"name": "timeout", "required": False, "default": "60"},
            ],
        })
        functions.append({
            "name": "Check_Logic",
            "params": [
                {"name": "command", "required": True},
                {"name": "keywords", "required": True},
                {"name": "logic", "required": False, "default": "'and'"},
                {"name": "timeout", "required": False, "default": "60"},
            ],
        })

    # 가이드 데이터 병합
    guides = _load_guides()
    mod_guide = guides.get(module_name, {})
    func_guides = mod_guide.get("functions", {})
    mod_description = mod_guide.get("_description", "")

    for fn in functions:
        fg = func_guides.get(fn["name"], {})
        fn["description"] = fg.get("description", "")
        param_guides = fg.get("params", {})
        for p in fn["params"]:
            p["description"] = param_guides.get(p["name"], "")

    _module_functions_cache[module_name] = (plugin_mtime, guides_mtime, functions)
    return functions


def _is_connected(instance) -> bool:
    """Check if a module instance appears to have a live connection."""
    # VisionCamera: IsConnected() 메서드
    if hasattr(instance, "IsConnected") and callable(getattr(instance, "IsConnected")):
        try:
            return instance.IsConnected()
        except Exception:
            return False
    # Serial: check _conn attribute (e.g. IVIQEBenchIOClient)
    if hasattr(instance, "_conn"):
        conn = getattr(instance, "_conn", None)
        if conn is None:
            return False
        is_open = getattr(conn, "is_open", None) or getattr(conn, "isOpen", None)
        if callable(is_open):
            return is_open()
        if isinstance(is_open, bool):
            return is_open
        return True
    # DLL-based: check hdll attribute (e.g. CANAT)
    if hasattr(instance, "hdll"):
        return getattr(instance, "hdll", None) is not None
    # Socket: check _socket or sock attribute
    sock = getattr(instance, "_socket", None) or getattr(instance, "sock", None)
    if sock is not None:
        return True
    if hasattr(instance, "_socket") or hasattr(instance, "sock"):
        return False
    return True  # no known indicator → assume OK


def _instance_key(module_name: str, constructor_kwargs: Optional[dict] = None) -> str:
    """캐시 키 — 같은 모듈이라도 물리 엔드포인트(port/host)가 다르면 별도 인스턴스로 관리.

    멀티 시리얼(예: SerialLogging 을 /dev/ttyUSB2 캡처 + /dev/ttyACM1 명령송신으로 동시 사용)
    환경에서, 과거에는 단일 'SerialLogging' 키를 공유하다가 포트가 바뀔 때마다 활성 캡처
    인스턴스를 pop → 버퍼(self._logs) 고아화(저장 빈 파일 / Monitor 판독 실패) + 같은 포트
    이중 open 으로 인한 재연결 폭주가 발생했다. 포트/호스트를 키에 포함시켜 근본 차단한다.
    """
    if constructor_kwargs:
        port = constructor_kwargs.get("port")
        if port:
            return f"{module_name}@{port}"
        host = constructor_kwargs.get("host")
        if host:
            return f"{module_name}@{host}"
    return module_name


def _keys_for(module_name: str) -> list[str]:
    """해당 module_name 에 속한 모든 캐시 키(포트/호스트별 인스턴스 포함)."""
    prefix = module_name + "@"
    return [k for k in list(_instances.keys()) if k == module_name or k.startswith(prefix)]


def _instance_lock(key: str) -> threading.Lock:
    """키별 생성 락 반환 (없으면 생성). dict 갱신 자체는 guard 락으로 보호."""
    with _instance_locks_guard:
        return _instance_locks.setdefault(key, threading.Lock())


def _get_instance(module_name: str, constructor_kwargs: Optional[dict] = None,
                  shared_serial_conn=None, ssh_credentials: Optional[dict] = None) -> Any:
    """Get or create a singleton instance of the module class.

    Args:
        shared_serial_conn: device_manager가 이미 열어둔 Serial 객체.
            전달되면 모듈의 Connect()를 호출하지 않고 _conn에 직접 주입.
        ssh_credentials: SSH 디바이스의 자격증명 {host, port, username, password, key_file_path}.
            전달되면 SSHManager 인스턴스에 instance.create_ssh_client()로 정식 연결.
    """
    # SSHManager는 디바이스별로 다른 자격증명을 가질 수 있으므로 매 호출마다 캐시 무효화
    # (SSHManager 내부 상태 때문에 create_ssh_client를 새로 호출해야 안정적)
    if module_name == "SSHManager":
        _instances.pop(module_name, None)
        _auto_connected.discard(module_name)
    # 캐시 키 — 포트/호스트가 다르면 다른 키가 되어 같은 모듈의 다른 엔드포인트끼리
    # 서로를 덮어쓰지 않는다. (포트 변경 시 pop 하던 파괴적 로직 제거 — 그게 버퍼 고아화 버그였다)
    key = _instance_key(module_name, constructor_kwargs)

    # 기존 인스턴스가 연결 끊어진 경우 재생성
    if key in _instances:
        if not _is_connected(_instances[key]):
            logger.info("Connection lost for %s, recreating instance", key)
            _instances.pop(key, None)
            _auto_connected.discard(key)

    if key not in _instances:
        # 생성+auto-Connect 는 키별 락으로 직렬화 — Connect 가 오래 걸리는 모듈(SCAR/TH)에서
        # 동시 연결 요청이 인스턴스를 중복 생성해 Setup 을 재실행하는 레이스 방지.
        with _instance_lock(key):
            # 락 대기 동안 다른 요청이 생성을 끝냈으면 그 인스턴스를 그대로 사용 (더블체크)
            if key not in _instances:
                _create_and_register(module_name, key, constructor_kwargs, shared_serial_conn)

    # SSHManager: 디바이스 자격증명으로 공식 create_ssh_client 호출 (매 호출마다)
    if module_name == "SSHManager" and ssh_credentials is not None:
        instance = _instances[key]
        host = ssh_credentials.get("host", "")
        username = ssh_credentials.get("username", "")
        password = ssh_credentials.get("password", "")
        key_file = ssh_credentials.get("key_file_path", "") or None
        try:
            if key_file:
                instance.create_ssh_client(host, username, password, key_file)
            else:
                instance.create_ssh_client(host, username, password)
            _auto_connected.add(key)
            logger.info("SSHManager.create_ssh_client(%s@%s) OK", username, host)
        except Exception as e:
            logger.error("SSHManager.create_ssh_client(%s@%s) failed: %s", username, host, e)
            raise

    return _instances[key]


def _create_and_register(module_name: str, key: str, constructor_kwargs: Optional[dict],
                         shared_serial_conn) -> None:
    """인스턴스 생성 + auto-Connect/init + _instances[key] 등록.

    반드시 _instance_lock(key) 안에서 호출 — _instances 에는 Connect 완료 후에야
    등록되므로, 락 없이 부르면 장시간 Connect 중 중복 생성이 가능하다.
    """
    cls = _import_module_class(module_name)
    if cls is None:
        # 진단 정보 포함 — lge.auto 미설치 / DLL 로드 실패 등 실제 원인을
        # UI(디바이스 연결 실패 토스트) 까지 노출시켜 사용자가 즉시 원인 파악.
        reason = _last_import_error.get(module_name)
        if reason:
            raise ValueError(f"Module '{module_name}' not found ({reason})")
        raise ValueError(f"Module '{module_name}' not found")
    # Try to pass constructor kwargs (e.g. port, bps) if the class needs them
    if constructor_kwargs:
        sig = inspect.signature(cls.__init__)
        ctor_args = {}
        type_map = {"int": int, "float": float, "bool": bool, "str": str}
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            if pname in constructor_kwargs:
                val = constructor_kwargs[pname]
                # 타입 힌트에 맞게 캐스팅
                ann = p.annotation
                if ann is not inspect.Parameter.empty:
                    if isinstance(ann, str):
                        ann = type_map.get(ann, ann)
                    if ann in (int, float, str) and not isinstance(val, ann):
                        try:
                            val = ann(val)
                        except (ValueError, TypeError):
                            pass
                ctor_args[pname] = val
        if ctor_args:
            instance = cls(**ctor_args)
            if shared_serial_conn and hasattr(instance, "_conn"):
                # device_manager가 이미 열어둔 시리얼 연결 주입
                instance._conn = shared_serial_conn
                _auto_connected.add(key)
                logger.info("Injected shared serial conn into %s (_conn)", module_name)
            else:
                # Serial modules (e.g. IVIQEBenchIOClient): constructor sets port/bps
                # but doesn't open the connection — call Connect() afterward
                for method_name in ("Connect", "connect"):
                    connect_fn = getattr(instance, method_name, None)
                    if callable(connect_fn):
                        try:
                            sig = inspect.signature(connect_fn)
                            # Only auto-call if every non-self param is optional (has a default).
                            # 예: WoohyunBench.Connect(self, rx_callback=None) — 선택 인자만 있으므로 자동 연결.
                            required = [
                                p for n, p in sig.parameters.items()
                                if n != "self" and p.default is inspect.Parameter.empty
                                and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
                            ]
                            if len(required) == 0:
                                result = connect_fn()
                                logger.info("Auto-called %s.%s() → %s", module_name, method_name, result)
                                if isinstance(result, str) and result.upper() in ("ERROR", "FAIL", "FAILED"):
                                    logger.warning("Auto-connect %s.%s() returned %s", module_name, method_name, result)
                                else:
                                    _auto_connected.add(key)
                        except Exception as e:
                            logger.warning("Auto-connect %s.%s() failed: %s", module_name, method_name, e)
                        break
            _instances[key] = instance
            # 연결 실패한 인스턴스는 다음 호출 시 재생성되도록 auto_connected에 등록
            if key not in _auto_connected and _is_connected(instance):
                _auto_connected.add(key)
        else:
            # Constructor doesn't accept the provided kwargs (e.g. BENCH, CANAT)
            # Create instance normally, then try auto-connect/init
            instance = cls()
            connected = False
            if "host" in constructor_kwargs:
                # Socket-based modules: auto-call connect method
                for method_name in ("socket_connect", "connect", "Connect"):
                    connect_fn = getattr(instance, method_name, None)
                    if callable(connect_fn):
                        connect_fn(constructor_kwargs["host"])
                        connected = True
                        break
            # init() 메서드가 있는 모듈 (e.g. CANAT): constructor_kwargs에서 매핑
            if not connected:
                init_fn = getattr(instance, "init", None)
                if callable(init_fn):
                    try:
                        init_sig = inspect.signature(init_fn)
                        init_args = {}
                        # comport ← port 매핑
                        kwarg_aliases = {"comport": "port", "port": "port"}
                        for pname, p in init_sig.parameters.items():
                            if pname == "self":
                                continue
                            if pname in constructor_kwargs:
                                init_args[pname] = constructor_kwargs[pname]
                            elif pname in kwarg_aliases and kwarg_aliases[pname] in constructor_kwargs:
                                init_args[pname] = constructor_kwargs[kwarg_aliases[pname]]
                            elif p.default is not inspect.Parameter.empty:
                                pass  # 기본값 사용
                            else:
                                # 필수 인자 없으면 빈 문자열로 채움
                                init_args[pname] = ""
                        # log_path 기본값: {프로젝트루트}/results/CANAT_Log
                        if "log_path" in init_args and not init_args["log_path"]:
                            default_log = Path(__file__).resolve().parent.parent.parent / "results" / "CANAT_Log"
                            default_log.mkdir(parents=True, exist_ok=True)
                            init_args["log_path"] = str(default_log)
                        result = init_fn(**init_args)
                        logger.info("Auto-called %s.init(%s) → %s", module_name, init_args, result)
                        _auto_connected.add(key)
                    except Exception as e:
                        logger.warning("Auto-init %s.init() failed: %s", module_name, e)
            _instances[key] = instance
    else:
        _instances[key] = cls()


def _cast_arg(val: Any, target_type: type) -> Any:
    """문자열 값을 target_type으로 변환. int인 경우 hex/oct/bin prefix 자동 인식.

    - int: "200" → 200, "0xC8" → 200, "0b1010" → 10, "0o17" → 15
    - bool: "true"/"1"/"yes" → True, "false"/"0"/"no"/"" → False
    - 그 외(이미 올바른 타입 등)는 target_type(val) 그대로
    """
    if not isinstance(val, str):
        return target_type(val)
    s = val.strip()
    if target_type is bool:
        return s.lower() not in ("0", "false", "no", "")
    if target_type is int:
        if not s:
            raise ValueError("empty string")
        # 0x/0o/0b 접두사가 있으면 base=0으로 자동 인식, 그 외는 일반 10진수
        sl = s.lstrip("+-").lower()
        if sl.startswith(("0x", "0o", "0b")):
            return int(s, 0)
        return int(s)
    return target_type(s)


# `bash -s` 류의 stdin 스크립트 실행에서 `< 로컬경로` 리디렉션을 탐지하는 패턴.
# 따옴표("..." / '...') 또는 공백 없는 경로 토큰을 캡처.
_SSH_STDIN_REDIRECT_RE = re.compile(r'<\s*("[^"]*"|\'[^\']*\'|\S+)')


def _extract_local_stdin(command: str) -> tuple[str, bytes | None]:
    """command 안의 `bash -s < 로컬파일` 패턴을 탐지해 (정리된 command, 파일내용) 반환.

    paramiko exec_command 는 로컬(PC) stdin 리디렉션을 처리하지 못한다. 그래서 명령에
    `bash -s ... < <경로>` 가 있고 그 경로가 PC 에 실제 존재하는 파일이면, 별도 파라미터
    없이 그 내용을 stdin 으로 주입하고 명령에서 `< <경로>` 부분을 제거한다 (업로드 불필요).
    경로가 로컬 파일이 아니면 원격 리디렉션으로 보고 명령을 그대로 둔다.
    """
    # `bash` + `-s` 플래그가 함께 있을 때만 동작 (일반 원격 리디렉션 오작동 방지)
    if "<" not in command or "bash" not in command:
        return command, None
    if not re.search(r'(^|\s)-s(\s|$|<|\'|")', command):
        return command, None
    m = _SSH_STDIN_REDIRECT_RE.search(command)
    if not m:
        return command, None
    raw = m.group(1).strip().strip('"').strip("'").strip()
    if not raw:
        return command, None
    p = Path(raw)
    if not p.is_file():
        return command, None  # 로컬 파일 아님 → 원격 리디렉션으로 그대로 실행
    # CRLF/CR 정규화: Windows에서 저장한 .sh 가 `\r\n` 이면 원격 bash 가
    # `then\r` 을 then 으로 인식 못해 `unexpected "fi"` / `bash: \r: not found` 발생.
    # bash -s 로 들어가는 건 항상 셸 스크립트 텍스트라 LF 통일이 안전하다.
    data = p.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    # `< <경로>` 부분만 제거 (나머지 인자/플래그는 보존)
    cleaned = (command[:m.start()] + command[m.end():]).strip()
    return cleaned, data


def _ssh_exec_decoded(client, command: str, timeout: int = 60) -> str:
    """SSH로 명령을 실행하고 stdout+stderr 를 디코딩한 문자열을 반환 (strip 됨, 비어있을 수 있음).

    command 에 `bash -s < 로컬파일` 패턴이 있으면 그 로컬 파일을 stdin 으로 자동 주입한다
    (업로드 없이 로컬 스크립트를 원격에서 실행). 인코딩 fallback: utf-8 → cp949 → euc-kr → cp437.
    """
    command, feed = _extract_local_stdin(command)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        if feed is not None:
            try:
                stdin.write(feed)
                stdin.flush()
            finally:
                # EOF 신호 — 원격 `bash -s` 가 입력 종료를 인식하도록
                try:
                    stdin.channel.shutdown_write()
                except Exception:
                    pass
        out_bytes = stdout.read()
        err_bytes = stderr.read()
    except Exception as e:
        raise RuntimeError(f"SSH exec failed: {e}") from e
    combined = out_bytes + (b"\n" + err_bytes if err_bytes else b"")
    for enc in ("utf-8", "cp949", "euc-kr", "cp437"):
        try:
            return combined.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return combined.decode(errors="replace").strip()


def _execute_sync(module_name: str, function_name: str, args: dict,
                  constructor_kwargs: Optional[dict] = None,
                  shared_serial_conn=None, ssh_credentials: Optional[dict] = None,
                  adb_serial: Optional[str] = None,
                  hkmc_service: Any = None) -> Any:
    """Execute a module function synchronously."""
    # HKMC6th: device_manager가 디바이스별로 관리하는 HKMC6thService 인스턴스에 직접 호출
    if module_name == "HKMC6th":
        if hkmc_service is None:
            raise RuntimeError(
                "HKMC6th module step requires an hkmc_agent device "
                "(no HKMC6thService instance bound to this step)"
            )
        func = getattr(hkmc_service, function_name, None)
        if func is None or not callable(func):
            raise ValueError(f"Function '{function_name}' not found in HKMC6thService")
        sig = inspect.signature(func)
        call_args = {}
        type_map = {"int": int, "float": float, "bool": bool, "str": str}
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            if pname in args:
                val = args[pname]
                ann = p.annotation
                if ann is not inspect.Parameter.empty:
                    if isinstance(ann, str):
                        ann = type_map.get(ann, ann)
                    if ann in (int, float, bool, str):
                        try:
                            val = _cast_arg(val, ann)
                        except (ValueError, TypeError) as e:
                            raise ValueError(
                                f"{module_name}.{function_name}: parameter '{pname}' "
                                f"could not be cast to {ann.__name__}: {val!r} ({e})"
                            )
                call_args[pname] = val
            elif p.default is inspect.Parameter.empty:
                raise ValueError(f"Missing required parameter: {pname}")
        result = func(**call_args)
        return result

    # HKMC5thWide: device_manager가 디바이스별로 관리하는 HKMC5thWideService 인스턴스에 직접 호출.
    # hkmc_service 파라미터는 HKMC6th/HKMC5thWide 양쪽 호환 (호출자가 적절한 인스턴스 주입).
    if module_name == "HKMC5thWide":
        if hkmc_service is None:
            raise RuntimeError(
                "HKMC5thWide module step requires an hkmc5th_wide_agent device "
                "(no HKMC5thWideService instance bound to this step)"
            )
        func = getattr(hkmc_service, function_name, None)
        if func is None or not callable(func):
            raise ValueError(f"Function '{function_name}' not found in HKMC5thWideService")
        sig = inspect.signature(func)
        call_args = {}
        type_map = {"int": int, "float": float, "bool": bool, "str": str}
        for pname, p in sig.parameters.items():
            if pname == "self":
                continue
            if pname in args:
                val = args[pname]
                ann = p.annotation
                if ann is not inspect.Parameter.empty:
                    if isinstance(ann, str):
                        ann = type_map.get(ann, ann)
                    if ann in (int, float, bool, str):
                        try:
                            val = _cast_arg(val, ann)
                        except (ValueError, TypeError) as e:
                            raise ValueError(
                                f"{module_name}.{function_name}: parameter '{pname}' "
                                f"could not be cast to {ann.__name__}: {val!r} ({e})"
                            )
                call_args[pname] = val
            elif p.default is inspect.Parameter.empty:
                raise ValueError(f"Missing required parameter: {pname}")
        result = func(**call_args)
        return result

    # Android.Send_adb_command — ReplayKit 자체 ADBService로 라우팅 (가상 함수)
    if module_name == "Android" and function_name == "Send_adb_command":
        from .adb_service import ADBService
        from ..dependencies import adb_service as _adb
        command = args.get("command", "")
        if not command:
            return "(empty command)"
        # args.serial이 명시되어 있으면 우선 사용 (스텝에서 사용자가 콤보로 선택한 시리얼)
        # 비어 있으면 step.device_id에서 derive된 adb_serial 사용
        target_serial = (args.get("serial") or "").strip() or adb_serial
        if not target_serial:
            raise RuntimeError("Send_adb_command requires an ADB device (serial missing)")
        # async 호출이지만 _execute_sync는 sync context (run_in_executor 안에서 호출됨)
        # → asyncio.run을 사용할 수 없음 (이미 이벤트 루프 중). loop.run_until_complete도 위험.
        # → ADBService 내부의 _run_device가 subprocess.run을 호출하는지 확인 필요.
        # 안전한 방법: 별도 이벤트 루프에서 비동기 실행
        import asyncio as _asyncio
        loop = _asyncio.new_event_loop()
        try:
            output = loop.run_until_complete(_adb.run_shell_command(command, serial=target_serial))
        finally:
            loop.close()
        return output if output is not None else "(no output)"

    # Android logcat 캡처/판독 가상 함수 — LogcatService로 라우팅 (adb로 명령 push 안 함)
    if module_name == "Android" and function_name in (
        "StartLogging", "StopLogging", "ClearLog",
        "Monitor_pass_on_keyword", "Monitor_fail_on_keyword",
    ):
        from .logcat_service import get_logcat_service
        svc = get_logcat_service()
        target_serial = (args.get("serial") or "").strip() or adb_serial
        if not target_serial:
            raise RuntimeError(f"Android.{function_name} requires an ADB device (serial missing)")
        if function_name == "StartLogging":
            return svc.start(target_serial, clear=_cast_arg(args.get("clear", True), bool))
        if function_name == "StopLogging":
            return svc.stop(target_serial, save_path=str(args.get("save_path") or ""))
        if function_name == "ClearLog":
            return svc.clear(target_serial)
        keyword = str(args.get("keyword") or "")
        time_s = _cast_arg(args.get("time", 5), float)
        include_past = _cast_arg(args.get("include_past", True), bool)
        if function_name == "Monitor_pass_on_keyword":
            return svc.monitor_pass(target_serial, keyword, time_s, include_past)
        return svc.monitor_fail(target_serial, keyword, time_s, include_past)

    # CANAT.CAN_PANEL — CAN 반응속도 측정용 시각화 패널 가상 함수.
    #   state=on,  신호 없음 → 검은 패널만 띄움 (대기)
    #   state=on,  신호 있음 → 패널을 노랑으로 점등 + send_can_message (지연 없이 동시)
    #   state=off          → 패널 종료 (신호 유무 무관)
    # '신호 있음' 판정 = message_id 와 can_message 가 모두 채워졌는가.
    if module_name == "CANAT" and function_name == "CAN_PANEL":
        from .can_panel import get_can_panel
        panel = get_can_panel()
        state = str(args.get("state", "on") or "on").strip().lower()

        if state in ("off", "false", "0", "close", "stop"):
            panel.close()
            return "ok: CAN_PANEL closed (state=off)"

        # 위치/크기 — 비거나 잘못되면 기본값(좌하단/300x300)
        def _int_arg(name: str, default: int) -> int:
            try:
                v = args.get(name, "")
                if v is None or str(v).strip() == "":
                    return default
                return int(_cast_arg(v, int))
            except (ValueError, TypeError):
                return default
        px = _int_arg("x", -1)
        py = _int_arg("y", -1)
        pw = _int_arg("width", 300)
        ph = _int_arg("height", 300)

        # state=on
        message_id = str(args.get("message_id", "") or "").strip()
        can_message = str(args.get("can_message", "") or "").strip()
        has_signal = bool(message_id) and bool(can_message)

        if not has_signal:
            panel.show_black(px, py, pw, ph)
            return f"ok: CAN_PANEL black panel shown (state=on, no signal, geom={pw}x{ph}@{px},{py})"

        # state=on + 신호 있음 → send_can_message 인스턴스 확보
        instance = _get_instance(module_name, constructor_kwargs, shared_serial_conn, ssh_credentials)
        send_fn = getattr(instance, "send_can_message", None)
        if send_fn is None or not callable(send_fn):
            raise ValueError("CANAT.send_can_message not available on instance")

        # send_can_message 인자 타입을 시그니처에 맞춰 캐스팅 (일반 send_can_message 스텝과 동일 규약).
        cycle_time = args.get("cycle_time", 0)
        bus_channel = args.get("bus_channel", 0)
        message_type = str(args.get("message_type", "FD") or "FD").strip() or "FD"
        try:
            sig = inspect.signature(send_fn)
            type_map = {"int": int, "float": float, "bool": bool, "str": str}

            def _cast_by_ann(pname: str, val: Any, default_type: type) -> Any:
                p = sig.parameters.get(pname)
                ann = p.annotation if p is not None else inspect.Parameter.empty
                if isinstance(ann, str):
                    ann = type_map.get(ann, ann)
                target = ann if ann in (int, float, bool, str) else default_type
                try:
                    return _cast_arg(val, target)
                except (ValueError, TypeError):
                    return val
            cycle_time = _cast_by_ann("cycle_time", cycle_time, int)
            bus_channel = _cast_by_ann("bus_channel", bus_channel, int)
        except (ValueError, TypeError):
            # 컴파일 .pyd 가 시그니처를 노출하지 않으면 best-effort int 캐스팅
            try:
                cycle_time = _cast_arg(cycle_time, int)
            except (ValueError, TypeError):
                pass
            try:
                bus_channel = _cast_arg(bus_channel, int)
            except (ValueError, TypeError):
                pass

        # 패널이 떠 있지 않으면 먼저 검정(지정 위치/크기)으로 띄워 창/소켓을 준비(사전 연결).
        if not panel.is_running():
            panel.show_black(px, py, pw, ph)
        # ── 점등 + 전송: 지연 없이 동시 ──
        # highlight() 는 사전 연결된 소켓에 1바이트만 쏘므로 마이크로초. 곧바로 CAN 전송.
        panel.highlight()
        result = send_fn(message_id, cycle_time, can_message, bus_channel, message_type)
        return f"ok: CAN_PANEL highlighted + sent (id={message_id} ch={bus_channel} type={message_type}) → {result}"

    instance = _get_instance(module_name, constructor_kwargs, shared_serial_conn, ssh_credentials)

    # SSHManager.send_command 특수 처리: SSHManager가 UTF-8로 강제 디코딩하면서
    # Windows(CP949) 등 비-UTF8 출력이 깨지므로, paramiko를 직접 호출해서 raw bytes를
    # 다중 인코딩 fallback으로 처리한다.
    if module_name == "SSHManager" and function_name == "send_command":
        client = getattr(instance, "ssh_client", None)
        if client is None:
            raise RuntimeError("SSH client not connected")
        command = args.get("command", "")
        output = _ssh_exec_decoded(client, command, 60)
        return output or "(no output)"

    # SSHManager.Check / Check_Logic 가상 함수: send_command 와 동일하게 명령을 실행한 뒤
    # 출력을 기대값/키워드로 합부 판정 (CMD.Check / CMD.Check_Logic 와 동일 규약).
    # 실패 시 "FAIL:" 접두사를 반환 → playback_service 가 스텝을 fail 로 판정.
    if module_name == "SSHManager" and function_name in ("Check", "Check_Logic"):
        client = getattr(instance, "ssh_client", None)
        if client is None:
            raise RuntimeError("SSH client not connected")
        command = args.get("command", "")
        timeout = _cast_arg(args.get("timeout", 60), int)
        actual = _ssh_exec_decoded(client, command, timeout)
        output = actual or "(no output)"

        if function_name == "Check":
            expected = str(args.get("expected", "") or "").strip()
            match_mode = str(args.get("match_mode", "contains") or "contains")
            if not expected:
                # expected 가 비어있으면 "출력 없음"일 때만 pass (no-output 검증)
                if actual == "":
                    return "(no output)"
                return f"FAIL: expected({match_mode}): (no output)\n---\n{output}"
            if match_mode == "exact":
                passed = actual == expected
            else:
                passed = expected in actual
            if passed:
                return output
            return f"FAIL: expected({match_mode}): {expected}\n---\n{output}"

        # Check_Logic
        keywords = str(args.get("keywords", "") or "")
        logic = str(args.get("logic", "and") or "and").strip().lower()
        kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
        if not kw_list:
            return f"FAIL: logic({logic}): no keywords provided\n---\n{output}"
        if logic not in ("and", "or"):
            return f"FAIL: logic: unknown mode '{logic}' (use 'and' or 'or')\n---\n{output}"
        if logic == "and":
            passed = all(k in actual for k in kw_list)
        else:
            passed = any(k in actual for k in kw_list)
        if passed:
            return output
        return f"FAIL: logic({logic}): {keywords}\n---\n{output}"

    # SSHManager.send_command_stream 가상 함수: 실시간 스트리밍 (bg_task_store 사용)
    if module_name == "SSHManager" and function_name == "send_command_stream":
        client = getattr(instance, "ssh_client", None)
        if client is None:
            raise RuntimeError("SSH client not connected")
        command = args.get("command", "")
        from . import bg_task_store
        task_id = bg_task_store.create_streaming_task(command)

        def _stream_reader(cmd: str, tid: str, ssh_client):
            """백그라운드 스레드에서 paramiko 채널을 폴링하며 chunk를 bg task에 append.

            bg_task_store에 cancel_requested 플래그가 설정되면 즉시 채널을 닫고 종료한다.
            """
            import time as _time
            channel = None
            try:
                transport = ssh_client.get_transport()
                if transport is None or not transport.is_active():
                    bg_task_store.append_stderr(tid, "SSH transport not active")
                    bg_task_store.mark_done(tid, status="error", rc=1)
                    return
                channel = transport.open_session()
                channel.settimeout(0.0)
                channel.exec_command(cmd)
                out_buffer = bytearray()
                err_buffer = bytearray()

                def _decode_chunk(buf: bytearray) -> tuple[str, bytearray]:
                    """버퍼에서 가능한 만큼 디코딩하고 불완전한 뒷부분은 남김."""
                    if not buf:
                        return "", buf
                    for enc in ("utf-8", "cp949", "euc-kr", "cp437"):
                        try:
                            text = buf.decode(enc)
                            return text, bytearray()
                        except UnicodeDecodeError as e:
                            # 잘린 multibyte일 수 있으니 뒷부분 남김
                            if enc == "utf-8" and e.start > 0:
                                try:
                                    text = buf[:e.start].decode(enc)
                                    return text, bytearray(buf[e.start:])
                                except UnicodeDecodeError:
                                    continue
                            continue
                    return buf.decode(errors="replace"), bytearray()

                while True:
                    # 취소 요청 확인
                    if bg_task_store.is_cancel_requested(tid):
                        logger.info("SSH stream task %s cancel requested — closing channel", tid)
                        try:
                            channel.close()
                        except Exception:
                            pass
                        bg_task_store.append_stderr(tid, "\n[cancelled by user]")
                        bg_task_store.mark_done(tid, status="cancelled", rc=130)
                        return

                    if channel.recv_ready():
                        chunk = channel.recv(4096)
                        if chunk:
                            out_buffer.extend(chunk)
                            text, out_buffer = _decode_chunk(out_buffer)
                            if text:
                                bg_task_store.append_stdout(tid, text)
                    if channel.recv_stderr_ready():
                        chunk = channel.recv_stderr(4096)
                        if chunk:
                            err_buffer.extend(chunk)
                            text, err_buffer = _decode_chunk(err_buffer)
                            if text:
                                bg_task_store.append_stderr(tid, text)
                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break
                    _time.sleep(0.05)

                # flush 잔여 버퍼
                if out_buffer:
                    text, _ = _decode_chunk(out_buffer)
                    if text:
                        bg_task_store.append_stdout(tid, text)
                if err_buffer:
                    text, _ = _decode_chunk(err_buffer)
                    if text:
                        bg_task_store.append_stderr(tid, text)

                rc = channel.recv_exit_status()
                try:
                    channel.close()
                except Exception:
                    pass
                bg_task_store.mark_done(tid, status="done", rc=rc)
            except Exception as e:
                logger.exception("SSH stream reader error for %s", tid)
                bg_task_store.append_stderr(tid, f"\n[stream error] {e}")
                try:
                    if channel is not None:
                        channel.close()
                except Exception:
                    pass
                bg_task_store.mark_done(tid, status="error", rc=1)

        import threading
        threading.Thread(
            target=_stream_reader,
            args=(command, task_id, client),
            daemon=True,
            name=f"ssh-stream-{task_id}",
        ).start()
        return f"[BG_TASK:{task_id}]"

    func = getattr(instance, function_name, None)
    if func is None:
        raise ValueError(f"Function '{function_name}' not found in {module_name}")

    # Build call args from the function signature
    sig = inspect.signature(func)
    call_args = {}
    for pname, p in sig.parameters.items():
        if pname in args:
            val = args[pname]
            # Try to cast to the expected type based on annotation
            if p.annotation is not inspect.Parameter.empty:
                try:
                    ann = p.annotation
                    # from __future__ import annotations 환경에서는 문자열로 평가됨
                    type_map = {"int": int, "float": float, "bool": bool, "str": str}
                    if isinstance(ann, str):
                        ann = type_map.get(ann, ann)
                    if ann in (int, float, bool, str):
                        if ann is bool and isinstance(val, str):
                            val = val.lower() not in ("0", "false", "no", "")
                        else:
                            val = ann(val)
                except (ValueError, TypeError):
                    pass
            call_args[pname] = val
        elif p.default is inspect.Parameter.empty:
            raise ValueError(f"Missing required parameter: {pname}")

    # 런 폴더 활성 시 빈 경로 파라미터를 런 폴더 logs/로 리다이렉트
    # DLTLogging/SerialLogging은 자체 런 폴더 로직이 있으므로 제외
    if module_name not in ("DLTLogging", "SerialLogging"):
        _redirect_path_args_to_run_dir(call_args, module_name, function_name)

    result = func(**call_args)
    return result


# 경로성 파라미터 이름 패턴 (빈 값일 때만 런 폴더로 리다이렉트)
_PATH_PARAM_NAMES = {
    "save_path", "path_log", "path_dir_log",
    "mlp_ivi_file_path", "mlp_safe_file_path",
    "log_file", "file_path", "logfilepath",
    "csv_file",
}


def _redirect_path_args_to_run_dir(call_args: dict, module_name: str, function_name: str) -> None:
    """경로 파라미터가 빈 값이면 현재 런 폴더의 logs/ 하위로 리다이렉트."""
    from .playback_service import get_run_output_dir
    run_dir = get_run_output_dir()
    if not run_dir:
        return

    log_dir = run_dir / "logs"
    log_dir.mkdir(exist_ok=True)

    for param_name in _PATH_PARAM_NAMES:
        if param_name in call_args and not call_args[param_name]:
            # 빈 값 → 런 폴더 내 자동 경로 생성
            import time
            ts = time.strftime("%Y%m%d_%H%M%S")
            safe_mod = module_name.replace(" ", "_")
            safe_func = function_name.replace(" ", "_")

            if "dir" in param_name:
                # 디렉토리 경로
                target = log_dir / safe_mod
                target.mkdir(exist_ok=True)
                call_args[param_name] = str(target)
            else:
                # 파일 경로
                ext = ".log"
                if "csv" in param_name:
                    ext = ".csv"
                elif "image" in param_name:
                    ext = ".png"
                call_args[param_name] = str(log_dir / f"{safe_mod}_{safe_func}_{ts}{ext}")


DEFAULT_MODULE_TIMEOUT_S = 3600.0  # 1시간 — 플러그인이 retry 기반 장시간 작업을 할 수 있음
MODULE_TIMEOUT_BUFFER_S = 60.0     # 네트워크/초기화 오버헤드 여유


def _compute_module_timeout(args: dict, user_timeout: Optional[float]) -> float:
    """모듈 함수 실행의 유효 타임아웃 계산.

    우선순위:
    1. 호출자가 명시적으로 `user_timeout`을 넘기면 그 값 사용
    2. args에 `timeout` 키가 있으면 `timeout * max_retries * 1.5 + buffer`로 계산
       - 예: DLTLogging.ExpectFound(timeout=60, max_retries=5) → 60*5*1.5+60 = 510s
    3. 그 외에는 DEFAULT_MODULE_TIMEOUT_S 사용

    주의: 모든 플러그인이 동일한 key 네이밍을 쓰지 않을 수 있으므로 이건 힌트일 뿐.
    감지 실패 시에도 default가 충분히 크도록(10분) 잡아 정당한 작업을 끊지 않음.
    """
    if user_timeout is not None and user_timeout > 0:
        return float(user_timeout)
    if not isinstance(args, dict):
        return DEFAULT_MODULE_TIMEOUT_S
    t = args.get("timeout")
    if not isinstance(t, (int, float)) or t <= 0:
        return DEFAULT_MODULE_TIMEOUT_S
    retries = args.get("max_retries") or args.get("retries") or 1
    if not isinstance(retries, (int, float)) or retries <= 0:
        retries = 1
    computed = float(t) * float(retries) * 1.5 + MODULE_TIMEOUT_BUFFER_S
    # 추정치가 default보다 작으면 default가 우선 (너무 짧은 것 방지)
    return max(computed, DEFAULT_MODULE_TIMEOUT_S)


async def execute_module_function(
    module_name: str, function_name: str, args: dict,
    constructor_kwargs: Optional[dict] = None,
    shared_serial_conn=None, ssh_credentials: Optional[dict] = None,
    adb_serial: Optional[str] = None,
    timeout_s: Optional[float] = None,
    hkmc_service: Any = None,
) -> str:
    """Execute a module function asynchronously (runs in thread pool).

    timeout_s: 모듈 함수 실행 상한(초). None이면 args 힌트로 자동 계산.
    초과 시 TimeoutError 발생하여 playback이 좀비 상태에 빠지지 않음.
    단, run_in_executor는 cancel 시 백그라운드 스레드를 강제 종료할 수 없으므로
    hang된 스레드는 백그라운드에 남음(스레드풀 슬롯 1개 소모). 모듈 자체의 내부
    타임아웃과 이중 안전장치로 동작.
    """
    effective_timeout = _compute_module_timeout(args, timeout_s)
    loop = asyncio.get_event_loop()
    # 모듈별 전용 단일 스레드 executor 사용 — COM(win32com/CANoe) STA affinity 유지.
    # HKMC6th 가상 모듈은 device_manager의 HKMC6thService를 그대로 호출하므로 굳이
    # 같은 스레드에 묶을 필요가 없지만, 일관성과 모듈 내부 상태 직렬화를 위해 동일하게 사용.
    module_executor = _get_module_executor(module_name)
    try:
        future = loop.run_in_executor(
            module_executor,
            functools.partial(_execute_sync, module_name, function_name, args,
                              constructor_kwargs, shared_serial_conn, ssh_credentials,
                              adb_serial, hkmc_service),
        )
        result = await asyncio.wait_for(future, timeout=effective_timeout)
        return str(result) if result is not None else "OK"
    except asyncio.TimeoutError:
        logger.error("Module execution timeout (%.1fs): %s.%s",
                     effective_timeout, module_name, function_name)
        raise TimeoutError(
            f"Module {module_name}.{function_name} exceeded {effective_timeout:.0f}s timeout"
        )
    except Exception as e:
        logger.error("Module execution error: %s.%s -> %s", module_name, function_name, e)
        raise


# 연결 해제/등록 삭제 시 graceful teardown(disconnect_instance)을 적용할 module 화이트리스트.
# 다른 module 의 기존 동작(해제 시 Disconnect/Close 미호출)을 바꾸지 않기 위해 명시적으로 좁힌다.
# SCAR: 해제 시 netns 복원(인터넷/cvd-ebr). TH: 해제 시 게이트웨이 정리(FqinAlreadyExists 방지)
# +선택적 cuttlefish 종료. 추가하려면 해당 module 에 Disconnect 구현 필요.
MODULES_WITH_DISCONNECT_TEARDOWN = {"SCAR", "TH"}

# ReplayKit 재시작 시 자동 연결(startup Setup/Connect)을 '건너뛸' module 화이트리스트.
# SCAR/TH 는 연결 시 netns 재구성·UI 재기동·cuttlefish/microservice 등 무거운(그리고 cvd-ebr 를
# 건드리는) Setup 을 돌리므로, 재시작마다 자동 연결되면 부작용(TH "connected 인데 동작 안 함",
# cvd-ebr flush 등)이 크다. 등록은 유지하되 status=disconnected 로 두고 사용자가 수동 연결하게 한다.
MODULES_NO_STARTUP_AUTOCONNECT = {"SCAR", "TH"}


def reset_instance(module_name: str) -> None:
    """Remove cached instance (단순 무효화 — 재생성용. teardown 호출 안 함).

    포트별 키(module@port)로 분리 관리되므로, 해당 모듈의 모든 엔드포인트 인스턴스를 제거한다.
    """
    for key in _keys_for(module_name):
        _instances.pop(key, None)
        _auto_connected.discard(key)


def disconnect_instance(module_name: str):
    """연결 해제/등록 삭제 시 모듈 인스턴스에 graceful teardown 후 캐시 제거.

    인스턴스가 Disconnect/Close/close 를 가지면 호출(예외는 문자열로 캡처)하고 pop 한다.
    SCAR 처럼 해제 시 정리(netns 복원 등)가 필요한 모듈을 위함. '단순 무효화(재생성용)' 에는
    teardown 을 부르면 안 되므로 그 경우는 reset_instance 를 계속 쓸 것.

    Returns:
        teardown 메서드 반환값(문자열) 또는 None(해당 메서드 없음/인스턴스 없음).
    """
    result = None
    # 포트별 키(module@port)로 분리되므로 해당 모듈의 모든 엔드포인트 인스턴스를 정리한다.
    for key in _keys_for(module_name):
        inst = _instances.get(key)
        if inst is not None:
            for method_name in ("Disconnect", "disconnect", "Close", "close"):
                method = getattr(inst, method_name, None)
                if callable(method):
                    try:
                        ret = method()
                        result = str(ret) if ret is not None else "ok"
                    except Exception as e:
                        result = f"error: {e}"
                    break
        _instances.pop(key, None)
        _auto_connected.discard(key)
    return result


def cleanup_active_instances(reason: str = "") -> dict[str, str]:
    """모든 활성 모듈 인스턴스에 graceful Disconnect를 시도하고 캐시를 비운다.

    재생이 중간 종료되거나 예외로 끝난 경우 호출. 시리얼/DLT 등의 포트가 leak되지 않도록
    하기 위함이다. 인스턴스가 Disconnect/Close 같은 메서드를 가지면 호출하고, 어떤 결과든
    조용히 무시한 뒤 캐시에서 제거한다.

    Returns:
        {module_name: "ok" | "skipped" | "error: <msg>"} — 호출 결과 요약 (디버그/로그용).
    """
    summary: dict[str, str] = {}
    # 순회 중 dict 변경을 피하기 위해 키 스냅샷
    for name in list(_instances.keys()):
        inst = _instances.get(name)
        if inst is None:
            continue
        called = False
        # Disconnect → Close → close → StopLogging/StopSave 순서로 시도 (대소문자 다양성).
        # SerialLogging은 Disconnect가 진행 중 로깅 세션 발견 시 자체적으로 StopLogging을 먼저
        # 호출하므로, 여기서 StopLogging을 별도로 부를 필요 없음.
        for method_name in ("Disconnect", "disconnect", "Close", "close",
                             "StopLogging", "StopSave"):
            method = getattr(inst, method_name, None)
            if callable(method):
                try:
                    # StopLogging/StopSave는 빈 인자 허용. 일부는 추가 인자가 있을 수 있어 try/except로 보호.
                    method()
                    called = True
                    summary[name] = f"ok({method_name})"
                    break
                except TypeError:
                    # 시그니처 불일치 → 다음 후보 시도
                    continue
                except Exception as e:
                    summary[name] = f"error({method_name}): {e}"
                    called = True
                    break
        if not called:
            summary[name] = "skipped"
        # 캐시 무효화 — 다음 사용 시 fresh 인스턴스로 시작
        _instances.pop(name, None)
        _auto_connected.discard(name)
    # Android logcat 세션은 _instances가 아닌 별도 싱글톤(LogcatService)이 관리하므로
    # 여기서 명시적으로 정리 — 진행 중인 캡처가 있으면 버퍼를 파일로 저장 후 종료.
    try:
        from .logcat_service import get_logcat_service
        get_logcat_service().stop_all()
    except Exception as e:
        logger.warning("cleanup_active_instances: logcat stop_all failed: %s", e)
    if summary:
        logger.info("cleanup_active_instances(reason=%s): %s", reason or "-", summary)
    return summary
