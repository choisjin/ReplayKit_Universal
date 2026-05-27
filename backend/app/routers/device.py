"""Device management API routes."""

import base64
import json as _json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from typing import Optional

logger = logging.getLogger(__name__)

from ..dependencies import adb_service as adb, device_manager as dm
from ..services.adb_service import resolve_sf_display_id, resolve_input_display_id
from ..services.module_service import list_available_modules, get_module_functions, execute_module_function
# 윈도우 컨트롤 라벨/누락 의존성 메시지 — OS 별 분기 (Linux→LinControl/python-xlib, Win→WinControl/pywin32).
from ..services.device_manager import _WIN_CTRL_DISPLAY_NAME, _WIN_CTRL_IS_LINUX
_WC_MISSING_DEP_MSG = "python-xlib not installed" if _WIN_CTRL_IS_LINUX else "pywin32 not installed"


def _with_protected_flag(devices: list) -> list[dict]:
    """ManagedDevice 리스트를 dict로 직렬화하면서 protected 플래그를 주입."""
    result = []
    for d in devices:
        data = d.to_dict()
        data["protected"] = dm.is_protected_device(d.id)
        result.append(data)
    return result

# ── 스캔 설정 ──────────────────────────────────────────────
_SCAN_SETTINGS_FILE = Path(__file__).resolve().parent.parent.parent / "scan_settings.json"

_DEFAULT_SCAN_SETTINGS = {
    "builtin": {
        "adb":            {"enabled": True,  "module": "",             "category": "primary"},
        "serial":         {"enabled": True,  "module": "SerialLogging","category": "auxiliary"},
        "hkmc":           {"enabled": True,  "module": "",             "category": "primary",   "ports": [6655, 5000]},
        "isap":           {"enabled": False, "module": "",             "category": "primary",   "ports": [20000]},
        "icas":           {"enabled": True,  "module": "",             "category": "primary",   "port": 22},
        "mib":            {"enabled": True,  "module": "",             "category": "primary",   "port": 22},
        "dlt":            {"enabled": True,  "module": "DLTLogging",   "category": "auxiliary", "ports": [3490]},
        "bench":          {"enabled": True,  "module": "WoohyunBench", "category": "auxiliary", "host": "192.168.1.101", "port": 25000},
        "vision_camera":  {"enabled": False, "module": "VisionCamera", "category": "primary"},
        "webcam":         {"enabled": True,  "module": "WebcamDevice", "category": "primary"},
        "ssh":            {"enabled": True,  "module": "SSHManager",   "category": "auxiliary", "port": 22},
        "smartbench":     {"enabled": True,  "module": "SmartBench",   "category": "auxiliary", "host": "192.167.0.5", "port": 8000},
    },
    # type: "tcp" | "udp", category: "primary" | "auxiliary"
    # [{"label": "MLP", "type": "tcp", "port": 5001, "module": "MLP", "enabled": true, "category": "auxiliary"}, ...]
    "custom": [],
}


def _load_scan_settings() -> dict:
    if _SCAN_SETTINGS_FILE.exists():
        try:
            data = _json.loads(_SCAN_SETTINGS_FILE.read_text(encoding="utf-8"))
            # 레거시 모듈 이름 마이그레이션: CCIC_BENCH → WoohyunBench
            builtin = data.setdefault("builtin", {})
            for key, entry in builtin.items():
                if isinstance(entry, dict) and entry.get("module") == "CCIC_BENCH":
                    entry["module"] = "WoohyunBench"
            for entry in data.get("custom", []) or []:
                if isinstance(entry, dict) and entry.get("module") == "CCIC_BENCH":
                    entry["module"] = "WoohyunBench"
            # 새로 추가된 기본 스캔 항목 자동 주입 (누락 키만 보충 — 사용자 수정값 유지)
            for key, default_entry in _DEFAULT_SCAN_SETTINGS["builtin"].items():
                if key not in builtin:
                    builtin[key] = dict(default_entry)
            # bench 마이그레이션: 옛 {ports: [25000]} → 새 {host, port} 형태
            bench = builtin.get("bench")
            if isinstance(bench, dict):
                if "host" not in bench:
                    bench["host"] = "192.168.1.101"
                if "port" not in bench:
                    legacy_ports = bench.get("ports") or [25000]
                    try:
                        bench["port"] = int(legacy_ports[0]) if legacy_ports else 25000
                    except (TypeError, ValueError):
                        bench["port"] = 25000
                bench.pop("ports", None)
            return data
        except Exception:
            pass
    return _json.loads(_json.dumps(_DEFAULT_SCAN_SETTINGS))  # deep copy


def _save_scan_settings(settings: dict) -> None:
    _SCAN_SETTINGS_FILE.write_text(_json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 디바이스 카탈로그 (프로젝트/모델 콤보 + 모듈 표시여부) ──
_DEVICE_CATALOG_FILE = Path(__file__).resolve().parent.parent.parent / "device_catalog.json"

_DEFAULT_DEVICE_CATALOG: dict = {
    "projects": [
        {
            "name": "HKMC",
            "enabled": True,
            "models": [
                {"value": "ccRC",                  "enabled": True},
                {"value": "ccIC",                  "enabled": True},
                {"value": "ccIC27",                "enabled": True},
                {"value": "Connect Wide",          "enabled": True},
                {"value": "CCU2",                  "enabled": True},
                {"value": "Gen6 Premium",          "enabled": True},
                {"value": "Gen5 Standard (Wide)",  "enabled": True},
                {"value": "Gen5 Standard",         "enabled": True},
                {"value": "Gen5 Premium",          "enabled": True},
            ],
        },
        {
            "name": "GM",
            "enabled": True,
            "models": [
                {"value": "GVM", "enabled": True},
            ],
        },
        {
            "name": "VW",
            "enabled": True,
            "models": [
                {"value": "MIB", "enabled": True, "agent": "MIB Agent"},
            ],
        },
        {
            "name": "General",
            "enabled": True,
            "models": [
                {"value": "Android", "enabled": True},
                {"value": "Phone",   "enabled": True},
                {"value": "SSH",     "enabled": True},
            ],
        },
    ],
    # 모듈 표시 여부 (false = 아직 미구현/숨김). 리스트에 없으면 기본 표시.
    "module_visibility": {},
    # 주 디바이스 조작 에이전트 정의. type은 내부 device type과 매핑 (변경 불가).
    # name은 UI 표시용 + 모델에서 참조하는 식별자.
    "agents": [
        {"name": "ADB",          "type": "adb",           "enabled": True},
        {"name": "HKMC Agent",   "type": "hkmc_agent",    "enabled": True},
        {"name": "HKMC5thWide Agent", "type": "hkmc5th_wide_agent", "enabled": True},
        {"name": "iSAP Agent",   "type": "isap_agent",    "enabled": True},
        {"name": "ICAS Agent",   "type": "icas_agent",    "enabled": True},
        {"name": "MIB Agent",    "type": "mib_agent",     "enabled": True},
        {"name": "VisionCamera", "type": "vision_camera", "enabled": True},
        {"name": "Webcam",       "type": "webcam",        "enabled": True},
    ],
}


def _load_device_catalog() -> dict:
    """카탈로그 로드. 레거시 필드 자동 마이그레이션 + 새 기본값 누락 보충."""
    if _DEVICE_CATALOG_FILE.exists():
        try:
            data = _json.loads(_DEVICE_CATALOG_FILE.read_text(encoding="utf-8"))
            # 레거시 마이그레이션: {label, value} → {value} (label 버림)
            for proj in data.get("projects", []) or []:
                for m in proj.get("models", []) or []:
                    if "label" in m:
                        m.pop("label", None)
            # 에이전트 type 마이그레이션: "hkmc6th" → "hkmc_agent"
            for a in data.get("agents", []) or []:
                if a.get("type") == "hkmc6th":
                    a["type"] = "hkmc_agent"
            # 새 기본값 누락 보충 — 기본 카탈로그의 project/agent가 사용자 카탈로그에 없으면 추가.
            # 사용자가 enabled 토글한 기존 항목은 그대로 유지 (이름 매칭으로 식별).
            existing_proj_names = {p.get("name") for p in (data.get("projects") or []) if p.get("name")}
            for default_proj in _DEFAULT_DEVICE_CATALOG["projects"]:
                if default_proj["name"] not in existing_proj_names:
                    data.setdefault("projects", []).append(_json.loads(_json.dumps(default_proj)))
            existing_agent_types = {a.get("type") for a in (data.get("agents") or []) if a.get("type")}
            for default_agent in _DEFAULT_DEVICE_CATALOG.get("agents", []):
                if default_agent["type"] not in existing_agent_types:
                    data.setdefault("agents", []).append(dict(default_agent))
            return data
        except Exception:
            pass
    return _json.loads(_json.dumps(_DEFAULT_DEVICE_CATALOG))  # deep copy


def _save_device_catalog(cat: dict) -> None:
    _DEVICE_CATALOG_FILE.write_text(_json.dumps(cat, ensure_ascii=False, indent=2), encoding="utf-8")


def _parse_adb_display_id(screen_type: str | None) -> int | None:
    """screen_type 문자열에서 ADB display_id 추출. '0', '2' 등 숫자 또는 None."""
    if screen_type is None:
        return None
    try:
        return int(screen_type)
    except (ValueError, TypeError):
        return None

router = APIRouter(prefix="/api/device", tags=["device"])


def _build_constructor_kwargs(dev) -> dict | None:
    """Build constructor kwargs from device info for module instantiation."""
    if not dev:
        return None
    connect_type = dev.info.get("connect_type", "serial" if dev.type == "serial" else "none")
    if connect_type == "serial":
        kwargs = {"port": dev.address, "bps": dev.info.get("baudrate", 115200)}
        # connect_fields의 추가 필드도 포함 (e.g. CANAT의 log_path, ch1_fd 등)
        for k, v in dev.info.items():
            if k not in ("module", "connect_type", "baudrate"):
                kwargs[k] = v
        return kwargs
    elif connect_type == "socket":
        kwargs = {"host": dev.address}
        # 추가 필드 전달 (예: udp_port) — 생성자 시그니처 매칭으로 필터링됨
        for k, v in dev.info.items():
            if k not in ("module", "connect_type"):
                kwargs[k] = v
        return kwargs
    elif connect_type == "can":
        # CAN modules store extra fields in device info
        return {k: v for k, v in dev.info.items() if k not in ("module", "connect_type")}
    elif connect_type == "vision_camera":
        # VisionCamera: MAC, model, serial, ip, subnetmask
        return {k: v for k, v in dev.info.items() if k not in ("module", "connect_type")}
    return None


class ConnectRequest(BaseModel):
    type: str  # "adb" | "serial" | "module" | "hkmc_agent" | "isap_agent" | "icas_agent" | "vision_camera" | "webcam" | "ssh"
    category: str = ""  # "primary" | "auxiliary" — auto-detected if empty
    address: str = ""  # COM port for serial, IP for socket/HKMC/SSH, etc.
    baudrate: Optional[int] = 115200
    port: Optional[int] = None  # TCP port for HKMC6th / SSH (default 22)
    name: Optional[str] = ""
    device_id: Optional[str] = ""  # custom device ID/alias (e.g. "Android_1", "HKMC_1")
    module: Optional[str] = None  # lge.auto module name (e.g. "POWER", "CAN")
    connect_type: Optional[str] = None  # "serial" | "socket" | "can" | "none" | "vision_camera" | "webcam" | "ssh"
    extra_fields: Optional[dict] = None  # Additional module-specific fields (SSH: username, password, key_file_path; webcam: device_index, width, height)
    device_model: Optional[str] = None  # 장비 모델 (GVM, ccNC, Phone 등) — 하드키 매칭용


class DisconnectRequest(BaseModel):
    address: str


_last_full_refresh = 0.0


@router.get("/list")
async def list_devices():
    """List all managed devices, split by category."""
    import time
    global _last_full_refresh
    now = time.time()
    # ADB refresh는 10초마다 (재연결 루프와 별도로 UI 표시용)
    if now - _last_full_refresh > 10:
        await dm.refresh_adb()
        _last_full_refresh = now
    # auxiliary는 빠른 상태 확인만 (네트워크 I/O 없음)
    await dm.refresh_auxiliary()
    return {
        "primary": _with_protected_flag(dm.list_primary()),
        "auxiliary": _with_protected_flag(dm.list_auxiliary()),
    }


@router.get("/scan-settings")
async def get_scan_settings():
    """현재 스캔 설정 조회."""
    return _load_scan_settings()


@router.post("/scan-settings")
async def save_scan_settings(request: Request):
    """스캔 설정 저장."""
    body = await request.json()
    _save_scan_settings(body)
    return {"status": "ok"}


@router.get("/catalog")
async def get_device_catalog():
    """프로젝트/모델 콤보 + 모듈 표시여부 카탈로그 조회."""
    return _load_device_catalog()


@router.post("/catalog")
async def save_device_catalog(request: Request):
    """관리용 — 프로젝트/모델/모듈 표시여부 카탈로그 저장."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    _save_device_catalog(body)
    return {"status": "ok"}


@router.get("/scan")
async def scan_ports():
    """Scan available connection targets — 스캔 설정에 따라 활성화된 항목만 실행."""
    import asyncio
    from ..services.device_manager import scan_tcp_port

    settings = _load_scan_settings()
    builtin = settings.get("builtin", {})
    custom = settings.get("custom", [])

    def _enabled(key: str) -> bool:
        v = builtin.get(key, {})
        if isinstance(v, dict):
            return v.get("enabled", True)
        return bool(v)  # 레거시 호환 (단순 bool)

    tasks: dict[str, asyncio.Task] = {}

    if _enabled("adb"):
        tasks["adb_devices"] = asyncio.ensure_future(adb.list_devices())
    if _enabled("serial"):
        tasks["serial_ports"] = asyncio.ensure_future(dm.scan_serial())
    def _ports_of(key: str) -> list[int]:
        entry = builtin.get(key, {}) if isinstance(builtin.get(key), dict) else {}
        raw = entry.get("ports") or []
        result: list[int] = []
        for p in raw:
            try:
                result.append(int(p))
            except (TypeError, ValueError):
                pass
        return result

    if _enabled("hkmc"):
        tasks["hkmc_devices"] = asyncio.ensure_future(dm.scan_hkmc(ports=_ports_of("hkmc")))
    if _enabled("isap"):
        tasks["isap_hosts"] = asyncio.ensure_future(dm.scan_isap(ports=_ports_of("isap")))
    if _enabled("bench"):
        # WoohyunBench: 단일 호스트 + 포트로 UDP 프로브 (SmartBench와 동일한 단일-프로브 패턴).
        # LAN 전체 스캔(ARP+ping+UDP)은 제거됨 — 항상 host/port가 설정에 명시되어 있어야 한다.
        bench_entry = builtin.get("bench", {}) if isinstance(builtin.get("bench"), dict) else {}
        bench_host = str(bench_entry.get("host") or "").strip() or None
        bench_port = bench_entry.get("port")
        try:
            bench_port = int(bench_port) if bench_port is not None else None
        except (TypeError, ValueError):
            bench_port = None
        tasks["bench_devices"] = asyncio.ensure_future(dm.scan_bench(host=bench_host, port=bench_port))
    if _enabled("vision_camera"):
        tasks["vision_cameras"] = asyncio.ensure_future(dm.scan_vision_cameras())
    if _enabled("webcam"):
        async def _scan_webcams():
            from ..plugins.WebcamDevice import WebcamDevice
            loop = asyncio.get_event_loop()
            cams = await loop.run_in_executor(None, WebcamDevice.list_available)
            # 이미 등록된 인덱스는 중복 추가 방지를 위해 표시
            registered_indices: set[int] = set()
            for d in dm.list_primary():
                if d.type == "webcam":
                    try:
                        registered_indices.add(int(d.info.get("device_index", -1)))
                    except (TypeError, ValueError):
                        pass
            # 녹화용 싱글톤이 점유 중인 인덱스도 표시
            recording_index = None
            try:
                from ..services.webcam_service import get_webcam_service
                svc = get_webcam_service()
                if svc.is_open():
                    recording_index = getattr(svc, "_device_index", None)
            except Exception:
                pass
            for cam in cams:
                cam["already_registered"] = cam["index"] in registered_indices
                cam["in_use_by_recording"] = (recording_index is not None and cam["index"] == recording_index)
            return cams
        tasks["webcams"] = asyncio.ensure_future(_scan_webcams())
    if _enabled("dlt"):
        tasks["dlt_devices"] = asyncio.ensure_future(dm.scan_dlt(ports=_ports_of("dlt")))
    if _enabled("smartbench"):
        sb_entry = builtin.get("smartbench", {}) if isinstance(builtin.get("smartbench"), dict) else {}
        sb_host = str(sb_entry.get("host") or "").strip() or None
        sb_port = sb_entry.get("port")
        try:
            sb_port = int(sb_port) if sb_port is not None else None
        except (TypeError, ValueError):
            sb_port = None
        tasks["smartbench_devices"] = asyncio.ensure_future(dm.scan_smartbench(host=sb_host, port=sb_port))
    if _enabled("ssh"):
        ssh_entry = builtin.get("ssh", {}) if isinstance(builtin.get("ssh"), dict) else {}
        ssh_port = int(ssh_entry.get("port", 22))
        tasks["ssh_hosts"] = asyncio.ensure_future(dm.scan_ssh(ssh_port))
    if _enabled("icas"):
        icas_entry = builtin.get("icas", {}) if isinstance(builtin.get("icas"), dict) else {}
        try:
            icas_port = int(icas_entry.get("port", 22))
        except (TypeError, ValueError):
            icas_port = 22
        tasks["icas_hosts"] = asyncio.ensure_future(dm.scan_icas(icas_port))
    if _enabled("mib"):
        # MIB도 SSH 22 기반이라 ICAS와 동일한 scan 함수 재사용. 결과 호스트는
        # 등록 시 사용자가 ICAS/MIB Agent 중 선택.
        mib_entry = builtin.get("mib", {}) if isinstance(builtin.get("mib"), dict) else {}
        try:
            mib_port = int(mib_entry.get("port", 22))
        except (TypeError, ValueError):
            mib_port = 22
        tasks["mib_hosts"] = asyncio.ensure_future(dm.scan_icas(mib_port))

    # 커스텀 TCP/UDP 포트 스캔
    custom_tasks: list[tuple[str, asyncio.Task]] = []
    for entry in custom:
        if entry.get("enabled") and entry.get("port"):
            label = entry.get("label", f"{entry.get('type','tcp').upper()}:{entry['port']}")
            proto = entry.get("type", "tcp")
            port = int(entry["port"])
            if proto == "udp":
                custom_tasks.append((label, asyncio.ensure_future(dm.scan_udp_port(port))))
            else:
                custom_tasks.append((label, asyncio.ensure_future(scan_tcp_port(port))))

    # 모든 태스크 병렬 실행
    all_keys = list(tasks.keys())
    all_futures = list(tasks.values())
    for label, fut in custom_tasks:
        all_keys.append(f"custom_{label}")
        all_futures.append(fut)

    results = await asyncio.gather(*all_futures, return_exceptions=True)

    response: dict = {
        "adb_devices": [],
        "serial_ports": [],
        "hkmc_devices": [],
        "bench_devices": [],
        "vision_cameras": [],
        "webcams": [],
        "isap_hosts": [],
        "icas_hosts": [],
        "mib_hosts": [],
        "dlt_devices": [],
        "smartbench_devices": [],
        "ssh_hosts": [],
        "custom_results": [],
    }
    for key, result in zip(all_keys, results):
        if isinstance(result, Exception):
            logger.warning("Scan %s failed: %s", key, result)
            continue
        if key == "adb_devices":
            response["adb_devices"] = [d.to_dict() for d in result]
        elif key.startswith("custom_"):
            label = key[len("custom_"):]
            response["custom_results"].append({"label": label, "hosts": result})
        else:
            response[key] = result

    return response


@router.get("/local-interfaces")
async def get_local_interfaces():
    """PC의 네트워크 인터페이스 목록 반환."""
    interfaces = []
    try:
        import ifaddr
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and not str(ip.ip).startswith("127."):
                    interfaces.append({
                        "name": adapter.nice_name,
                        "ip": str(ip.ip),
                        "prefix": ip.network_prefix,
                    })
    except ImportError:
        import socket
        hostname = socket.gethostname()
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            addr = info[4][0]
            if not addr.startswith("127."):
                interfaces.append({"name": "", "ip": addr, "prefix": 24})
    return {"interfaces": interfaces}


class ForceIPRequest(BaseModel):
    mac: str
    ip: str
    subnet: str = "255.255.255.0"
    gateway: str = "0.0.0.0"


@router.post("/vision-force-ip")
async def vision_force_ip(req: ForceIPRequest):
    """VisionCamera ForceIP — 카메라 IP를 강제 변경."""
    result = await dm.force_ip_camera(req.mac, req.ip, req.subnet, req.gateway)
    if "OK" in result:
        return {"result": result}
    raise HTTPException(status_code=400, detail=result)


@router.post("/connect")
async def connect_device(req: ConnectRequest):
    """Connect to a device."""
    custom_id = req.device_id or ""
    if req.type == "adb":
        if ":" in req.address:
            # WiFi ADB — connect first
            await dm.adb.connect_device(req.address)
        dev = await dm.add_adb_device(req.address, device_id=custom_id, name=req.name or "", device_model=req.device_model or "")
        return {
            "result": f"Connected: {dev.name} (ID: {dev.id})",
            "primary": _with_protected_flag(dm.list_primary()),
            "auxiliary": _with_protected_flag(dm.list_auxiliary()),
        }
    elif req.type == "serial":
        category = req.category or "auxiliary"
        try:
            dev = await dm.add_serial_device(
                req.address, req.baudrate or 115200, req.name or "", category,
                device_id=custom_id,
                module=req.module or "",
                connect_type=req.connect_type or "",
            )
            return {
                "result": f"Serial {req.address} added (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "hkmc_agent":
        # Gen5 모델 자동 라우팅 — frontend가 type을 hkmc_agent로 잘못 보내도 device_model 로 보정.
        # HKMC6th 와 5th_wide 는 통신 프로토콜이 달라 서로 동작 불가.
        if "gen5" in (req.device_model or "").lower():
            if not req.address or not req.port:
                raise HTTPException(status_code=400, detail="HKMC5thWide requires address (IP) and port (TCP port)")
            try:
                dev = await dm.add_hkmc5th_wide_device(
                    req.address, req.port, device_id=custom_id,
                    name=req.name or "",
                    device_model=req.device_model or "",
                )
                return {
                    "result": f"HKMC5thWide registered (auto-routed from Gen5 model): {dev.name} (ID: {dev.id})",
                    "primary": _with_protected_flag(dm.list_primary()),
                    "auxiliary": _with_protected_flag(dm.list_auxiliary()),
                }
            except RuntimeError as e:
                raise HTTPException(status_code=400, detail=str(e))
        if not req.address or not req.port:
            raise HTTPException(status_code=400, detail="HKMC6th requires address (IP) and port (TCP port)")
        ef = req.extra_fields or {}
        try:
            dev = await dm.add_hkmc6th_device(
                req.address, req.port, device_id=custom_id,
                name=req.name or "",
                device_model=req.device_model or "",
                # 클러스터 SSH 캡처용 자격증명. 미입력 시 ICAS QNX 패턴(root/빈 패스워드) fallback.
                ssh_username=(ef.get("ssh_username") or "root"),
                ssh_password=(ef.get("ssh_password") or ""),
                ssh_port=int(ef.get("ssh_port") or 22),
                cluster_resolution=str(ef.get("cluster_resolution") or "2720x720"),
                cluster_display=str(ef.get("cluster_display") or "1"),
            )
            return {
                "result": f"HKMC connected: {dev.name} (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "hkmc5th_wide_agent":
        if not req.address or not req.port:
            raise HTTPException(status_code=400, detail="HKMC5thWide requires address (IP) and port (TCP port)")
        try:
            dev = await dm.add_hkmc5th_wide_device(
                req.address, req.port, device_id=custom_id,
                name=req.name or "",
                device_model=req.device_model or "",
            )
            return {
                "result": f"HKMC5thWide registered: {dev.name} (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "isap_agent":
        if not req.address or not req.port:
            raise HTTPException(status_code=400, detail="iSAP Agent requires address (IP) and port (TCP port, default 20000)")
        try:
            dev = await dm.add_isap_agent_device(req.address, req.port, device_id=custom_id, name=req.name or "", device_model=req.device_model or "")
            return {
                "result": f"iSAP registered: {dev.name} (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "icas_agent":
        if not req.address:
            raise HTTPException(status_code=400, detail="ICAS Agent requires address (host)")
        ef = req.extra_fields or {}
        try:
            dev = await dm.add_icas_agent_device(
                host=req.address,
                port=int(req.port or 22),
                device_id=custom_id,
                name=req.name or "",
                device_model=req.device_model or "",
                username=ef.get("username", "root") or "root",
                password=ef.get("password", "") or "",
                resolution=ef.get("resolution", "1560x700") or "1560x700",
                # private_server_ip는 빈 문자열이면 market 기본값 사용
                private_server_ip=ef.get("private_server_ip", "") or "",
                private_server_password=ef.get("private_server_password", "") or "",
                iid_display=str(ef.get("iid_display", "10") or "10"),
                hud_display=str(ef.get("hud_display", "11") or "11"),
                market=str(ef.get("market", "") or ""),
            )
            # 등록 직후 실제 SSH 연결 시도
            try:
                connect_msg = await dm.connect_device_by_id(dev.id)
            except Exception as e:
                connect_msg = f"registered but connect failed: {e}"
            return {
                "result": f"ICAS registered: {dev.name} (ID: {dev.id}) — {connect_msg}",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "mib_agent":
        if not req.address:
            raise HTTPException(status_code=400, detail="MIB Agent requires address (host)")
        ef = req.extra_fields or {}
        try:
            dev = await dm.add_mib_agent_device(
                host=req.address,
                port=int(req.port or 22),
                device_id=custom_id,
                name=req.name or "",
                device_model=req.device_model or "",
                username=ef.get("username", "root") or "root",
                password=ef.get("password", "") or "",
                resolution=ef.get("resolution", "1560x700") or "1560x700",
                private_server_ip=ef.get("private_server_ip", "") or "",
                private_server_password=ef.get("private_server_password", "") or "",
                iid_display=str(ef.get("iid_display", "10") or "10"),
                hud_display=str(ef.get("hud_display", "11") or "11"),
                market=str(ef.get("market", "") or ""),
            )
            try:
                connect_msg = await dm.connect_device_by_id(dev.id)
            except Exception as e:
                connect_msg = f"registered but connect failed: {e}"
            return {
                "result": f"MIB registered: {dev.name} (ID: {dev.id}) — {connect_msg}",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif req.type == "module":
        category = req.category or "auxiliary"
        dev = await dm.add_module_device(
            address=req.address,
            module=req.module or "",
            connect_type=req.connect_type or "none",
            name=req.name or "",
            extra_fields=req.extra_fields,
            device_id=custom_id,
        )
        # 등록 직후 실제 연결 수행 — Connect() 호출 및 인스턴스 생성.
        # 기존에는 등록만 하고 status="disconnected" 로 남아, UI에서 연결됨으로 보이지
        # 않거나 이후 호출이 실패하는 문제가 있었음.
        try:
            connect_msg = await dm.connect_device_by_id(dev.id)
        except Exception as e:
            logger.warning("Module auto-connect after register failed: %s", e)
            connect_msg = f"registered but connect failed: {e}"
        return {
            "result": f"Module device {req.module} added (ID: {dev.id}) — {connect_msg}",
            "primary": _with_protected_flag(dm.list_primary()),
            "auxiliary": _with_protected_flag(dm.list_auxiliary()),
        }
    elif req.type == "ssh":
        ef = req.extra_fields or {}
        username = ef.get("username", "")
        password = ef.get("password", "")
        key_file_path = ef.get("key_file_path", "")
        if not req.address:
            raise HTTPException(status_code=400, detail="SSH requires address (host)")
        if not username:
            raise HTTPException(status_code=400, detail="SSH requires username")
        if not password and not key_file_path:
            raise HTTPException(status_code=400, detail="SSH requires password or key_file_path")
        category = req.category or "auxiliary"
        try:
            dev = await dm.add_ssh_device(
                host=req.address,
                port=int(req.port or 22),
                username=username,
                password=password,
                category=category,
                name=req.name or "",
                device_id=custom_id,
                key_file_path=key_file_path,
            )
            return {
                "result": f"SSH connected: {dev.name} (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except RuntimeError as e:
            raise HTTPException(status_code=400, detail=str(e))

    elif req.type == "vision_camera":
        ef = req.extra_fields or {}
        mac = ef.get("mac", "")
        logger.info("[VisionCamera] connect request: mac=%s address=%s extra_fields=%s", mac, req.address, ef)
        if not mac:
            raise HTTPException(status_code=400, detail="VisionCamera requires MAC address")
        try:
            dev = await dm.add_vision_camera_device(
                mac=mac,
                model=ef.get("model", ""),
                serial=ef.get("serial", ""),
                ip=req.address or ef.get("ip", ""),
                subnetmask=ef.get("subnetmask", "255.255.0.0"),
                device_id=custom_id,
                name=req.name or "",
            )
            return {
                "result": f"VisionCamera connected: {dev.name} (ID: {dev.id})",
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except Exception as e:
            logger.error("[VisionCamera] connect failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e))

    elif req.type == "wincontrol":
        # WinControl: 시스템 기본 디바이스(WinControl_1 등 추가 등록 불가).
        # 기본 'WinControl' 디바이스가 항상 존재하므로 이 경로로 신규 등록을 막는다.
        raise HTTPException(
            status_code=400,
            detail="WinControl is a system default device — use /connect-registered instead",
        )

    elif req.type == "webcam":
        ef = req.extra_fields or {}
        # address 또는 extra_fields.device_index 중 하나로 카메라 인덱스 전달
        try:
            device_index = int(ef.get("device_index", req.address or 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Webcam requires numeric device_index")
        width = int(ef.get("width") or 0)
        height = int(ef.get("height") or 0)
        logger.info("[Webcam] connect request: index=%d %dx%d", device_index, width, height)
        try:
            dev = await dm.add_webcam_device(
                device_index=device_index,
                width=width,
                height=height,
                device_id=custom_id,
                name=req.name or "",
            )
            # 즉시 연결 시도 (VisionCamera와 동일한 패턴 — 등록 후 connect)
            result = await dm.connect_device_by_id(dev.id)
            return {
                "result": result,
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }
        except HTTPException:
            raise
        except Exception as e:
            logger.error("[Webcam] connect failed: %s", e, exc_info=True)
            raise HTTPException(status_code=400, detail=str(e))
    else:
        raise HTTPException(status_code=400, detail=f"Unknown type: {req.type}")


@router.post("/disconnect")
async def disconnect_device(req: DisconnectRequest):
    """Disconnect/remove a device."""
    if dm.is_protected_device(req.address):
        raise HTTPException(
            status_code=403,
            detail=f"Device '{req.address}' is a protected system default and cannot be removed",
        )
    try:
        result = await dm.remove_device(req.address)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    return {
        "result": result,
        "primary": _with_protected_flag(dm.list_primary()),
        "auxiliary": _with_protected_flag(dm.list_auxiliary()),
    }


class DisconnectOneRequest(BaseModel):
    device_id: str

@router.post("/disconnect-one")
async def disconnect_one_device(req: DisconnectOneRequest):
    """연결만 끊기 (등록 유지)."""
    result = await dm.disconnect_device_by_id(req.device_id)
    return {
        "result": result,
        "primary": _with_protected_flag(dm.list_primary()),
        "auxiliary": _with_protected_flag(dm.list_auxiliary()),
    }


@router.get("/info/{device_id}")
async def get_device_info(device_id: str):
    """Get device information."""
    dev = dm.get_device(device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {device_id} not found")
    if dev.type == "adb":
        try:
            return await adb.get_device_info(dev.address)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif dev.type == "hkmc_agent":
        hkmc = dm.get_hkmc_service(device_id)
        info = dev.to_dict()
        if hkmc:
            info["hkmc_info"] = hkmc.get_info()
        return info
    elif dev.type == "isap_agent":
        isap = dm.get_isap_service(device_id)
        info = dev.to_dict()
        if isap:
            info["isap_info"] = isap.get_info()
        return info
    else:
        return dev.to_dict()


class InputRequest(BaseModel):
    device_id: str
    action: str  # "tap" | "swipe" | "input_text" | "key_event" | "adb_command" | "serial_command" | "module_command" | "hkmc_touch" | "hkmc_swipe" | "hkmc_key" | "icas_touch" | "icas_swipe" | "icas_key"
    params: dict


@router.post("/input")
async def device_input(req: InputRequest):
    """Execute an input action directly on a device (without recording)."""
    dev = dm.get_device(req.device_id)

    try:
        if req.action == "module_command":
            module_name = req.params.get("module", "")
            func_name = req.params.get("function", "")
            func_args = req.params.get("args", {})
            if not module_name or not func_name:
                raise HTTPException(status_code=400, detail="module and function are required")
            # Pass device connection info as constructor kwargs
            ctor_kwargs = _build_constructor_kwargs(dev) if dev else None
            shared_conn = dm.get_serial_conn(req.device_id) if dev else None
            # HKMC6th: 디바이스별 HKMC6thService 인스턴스 주입
            hkmc_svc = None
            if module_name == "HKMC6th":
                hkmc_svc = dm.get_hkmc_service(req.device_id) if dev else None
                if hkmc_svc is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"HKMC6th requires a connected hkmc_agent device (id={req.device_id})",
                    )
            response = await execute_module_function(
                module_name, func_name, func_args, ctor_kwargs, shared_conn,
                hkmc_service=hkmc_svc,
            )
            return {"result": "ok", "response": response}

        if req.action == "serial_command":
            if not dev or dev.type != "serial":
                raise HTTPException(status_code=404, detail=f"Serial device {req.device_id} not found")
            response = await dm.send_serial_command(
                req.device_id, req.params.get("data", ""), req.params.get("read_timeout", 1.0)
            )
            return {"result": "ok", "response": response}

        if req.action in ("hkmc_touch", "hkmc_swipe", "hkmc_key", "hkmc_long_press", "repeat_tap") and dev and dev.type == "isap_agent":
            isap = dm.get_isap_service(req.device_id)
            if not isap:
                raise HTTPException(status_code=400, detail=f"iSAP device {req.device_id} not connected")
            logger.info("[iSAP INPUT] device=%s action=%s params=%s connected=%s",
                        req.device_id, req.action, req.params, isap.is_connected)
            p = req.params
            screen_type = p.get("screen_type", "front_center")
            if req.action == "repeat_tap":
                await isap.async_repeat_tap(p["x"], p["y"], int(p.get("count", 5)),
                                            int(p.get("interval_ms", 100)), screen_type)
            elif req.action == "hkmc_touch":
                await isap.async_tap(p["x"], p["y"], screen_type)
            elif req.action == "hkmc_long_press":
                await isap.async_long_press(p["x"], p["y"],
                                            int(p.get("duration_ms", 3000)), screen_type)
            elif req.action == "hkmc_swipe":
                await isap.async_swipe(p["x1"], p["y1"], p["x2"], p["y2"], screen_type,
                                       int(p.get("duration_ms", 0)))
            elif req.action == "hkmc_key":
                key_name = p.get("key_name")
                if key_name:
                    await isap.async_send_key_by_name(
                        key_name, p.get("sub_cmd", 0x43), screen_type, p.get("direction"),
                        key_source=p.get("key_source"),
                    )
                else:
                    await isap.async_send_key(
                        p["cmd"], p["sub_cmd"], p["key_data"], screen_type, p.get("direction")
                    )
            return {"result": "ok"}

        if req.action in ("icas_touch", "icas_swipe", "icas_key", "icas_long_press", "repeat_tap") and dev and dev.type == "icas_agent":
            icas = dm.get_icas_service(req.device_id)
            if not icas:
                raise HTTPException(status_code=400, detail=f"ICAS device {req.device_id} not connected")
            logger.info("[ICAS INPUT] device=%s action=%s params=%s connected=%s",
                        req.device_id, req.action, req.params, icas.is_connected)
            p = req.params
            screen_type = p.get("screen_type", "HU")
            if req.action == "repeat_tap":
                await icas.async_repeat_tap(p["x"], p["y"], int(p.get("count", 5)),
                                            int(p.get("interval_ms", 100)), screen_type)
            elif req.action == "icas_touch":
                await icas.async_tap(p["x"], p["y"], screen_type)
            elif req.action == "icas_long_press":
                await icas.async_long_press(p["x"], p["y"],
                                            int(p.get("duration_ms", 3000)), screen_type)
            elif req.action == "icas_swipe":
                await icas.async_swipe(p["x1"], p["y1"], p["x2"], p["y2"], screen_type,
                                       int(p.get("duration_ms", 0)))
            elif req.action == "icas_key":
                key_name = p.get("key_name")
                if key_name:
                    await icas.async_send_key_by_name(
                        key_name, p.get("sub_cmd", 0x43), screen_type, p.get("direction")
                    )
                else:
                    await icas.async_send_key(
                        p["cmd"], p["sub_cmd"], p["key_data"], screen_type, p.get("direction")
                    )
            return {"result": "ok"}

        # MIB Agent — ICAS Agent와 동일한 action set을 mib_touch/mib_swipe/mib_key로 노출.
        # 호환 위해 icas_* action도 디바이스 타입이 mib_agent일 때 같이 처리.
        if (req.action in ("mib_touch", "mib_swipe", "mib_key", "mib_long_press", "repeat_tap",
                           "icas_touch", "icas_swipe", "icas_key", "icas_long_press")
                and dev and dev.type == "mib_agent"):
            mib = dm.get_mib_service(req.device_id)
            if not mib:
                raise HTTPException(status_code=400, detail=f"MIB device {req.device_id} not connected")
            logger.info("[MIB INPUT] device=%s action=%s params=%s connected=%s",
                        req.device_id, req.action, req.params, mib.is_connected)
            p = req.params
            screen_type = p.get("screen_type", "HU")
            if req.action == "repeat_tap":
                await mib.async_repeat_tap(p["x"], p["y"], int(p.get("count", 5)),
                                           int(p.get("interval_ms", 100)), screen_type)
            elif req.action in ("mib_touch", "icas_touch"):
                await mib.async_tap(p["x"], p["y"], screen_type)
            elif req.action in ("mib_long_press", "icas_long_press"):
                await mib.async_long_press(p["x"], p["y"],
                                           int(p.get("duration_ms", 3000)), screen_type)
            elif req.action in ("mib_swipe", "icas_swipe"):
                await mib.async_swipe(p["x1"], p["y1"], p["x2"], p["y2"], screen_type,
                                      int(p.get("duration_ms", 0)))
            elif req.action in ("mib_key", "icas_key"):
                key_name = p.get("key_name")
                if key_name:
                    await mib.async_send_key_by_name(
                        key_name, p.get("sub_cmd", 0x43), screen_type, p.get("direction")
                    )
                else:
                    await mib.async_send_key(
                        p["cmd"], p["sub_cmd"], p["key_data"], screen_type, p.get("direction")
                    )
            return {"result": "ok"}

        if req.action in ("hkmc_touch", "hkmc_swipe", "hkmc_key", "hkmc_long_press", "repeat_tap") and dev and dev.type in ("hkmc_agent", "hkmc5th_wide_agent"):
            # Gen5 자동 라우팅 — hkmc_agent로 잘못 등록된 디바이스도 device_model/id에 "gen5"가
            # 있으면 5th_wide service를 사용 (한쪽이라도 동작 가능하게 함). 사용자가 디바이스
            # 삭제·재등록 없이도 hardkey가 작동하도록 input 시점에도 안전망 적용.
            _model_str = (dev.info.get("device_model") or "").lower()
            _id_str = (dev.id or "").lower()
            _is_gen5 = ("gen5" in _model_str) or ("gen5" in _id_str)
            # 진단 로그 — 어느 분기로 라우팅되는지 명확히 식별 (반영 여부 검증용)
            logger.info(
                "[INPUT ROUTING] dev.id=%s dev.type=%s device_model=%r id_str=%r is_gen5=%s",
                req.device_id, dev.type, dev.info.get("device_model"), _id_str, _is_gen5,
            )
            if dev.type == "hkmc5th_wide_agent" or _is_gen5:
                hkmc = dm.get_hkmc5th_wide_service(req.device_id)
                _label = "HKMC5thWide"
                # 잘못된 hkmc_agent 연결이 있을 때 5th_wide service가 없으면 즉시 연결 시도 안내
                if hkmc is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"Device {req.device_id} is Gen5 but bound to hkmc_agent service. "
                            "Disconnect/reconnect to migrate to HKMC5thWide."
                        ),
                    )
            else:
                hkmc = dm.get_hkmc_service(req.device_id)
                _label = "HKMC"
            if not hkmc:
                raise HTTPException(status_code=400, detail=f"{_label} device {req.device_id} not connected")
            logger.info("[%s INPUT] device=%s action=%s params=%s connected=%s",
                        _label, req.device_id, req.action, req.params, hkmc.is_connected)
            p = req.params
            screen_type = p.get("screen_type", "front_center")
            if req.action == "repeat_tap":
                await hkmc.async_repeat_tap(p["x"], p["y"], int(p.get("count", 5)),
                                            int(p.get("interval_ms", 100)), screen_type)
            elif req.action == "hkmc_touch":
                await hkmc.async_tap(p["x"], p["y"], screen_type)
                logger.info("[%s INPUT] tap sent: x=%s y=%s screen=%s", _label, p["x"], p["y"], screen_type)
            elif req.action == "hkmc_long_press":
                await hkmc.async_long_press(p["x"], p["y"],
                                            int(p.get("duration_ms", 3000)), screen_type)
                logger.info("[%s INPUT] long_press sent: x=%s y=%s ms=%s screen=%s",
                            _label, p["x"], p["y"], p.get("duration_ms", 3000), screen_type)
            elif req.action == "hkmc_swipe":
                await hkmc.async_swipe(p["x1"], p["y1"], p["x2"], p["y2"], screen_type,
                                       int(p.get("duration_ms", 0)))
                logger.info("[%s INPUT] swipe sent: duration_ms=%s", _label, p.get("duration_ms", 0))
            elif req.action == "hkmc_key":
                key_name = p.get("key_name")
                if key_name:
                    await hkmc.async_send_key_by_name(
                        key_name, p.get("sub_cmd", 0x43), p.get("monitor", 0x00),
                        p.get("direction"), screen_type,
                        key_source=p.get("key_source"),
                    )
                    logger.info("[%s INPUT] key sent: %s (source=%s)", _label, key_name, p.get("key_source"))
                else:
                    await hkmc.async_send_key(
                        p["cmd"], p["sub_cmd"], p["key_data"], p.get("monitor", 0x00), p.get("direction")
                    )
            return {"result": "ok"}

        if req.action in ("win_tap", "win_double_click", "win_long_press", "win_swipe",
                          "win_input_text", "win_key", "win_key_combo") and dev and dev.type == "wincontrol":
            wc = dm.get_wincontrol_service()
            if not wc.is_available():
                raise HTTPException(
                    status_code=503,
                    detail=f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {wc.import_error() or _WC_MISSING_DEP_MSG}",
                )
            import asyncio
            loop = asyncio.get_event_loop()
            p = req.params
            # params 에 프로세스 정보가 있으면 자동 attach/launch
            proc_name = str(p.get("process_name", "") or "")
            exe_path = str(p.get("exe_path", "") or "")
            title_pattern = str(p.get("window_title", "") or "")
            class_name = str(p.get("window_class", "") or "")
            aumid = str(p.get("process_aumid", "") or "")
            if proc_name or exe_path or title_pattern or aumid:
                import functools as _ft
                try:
                    await loop.run_in_executor(
                        None,
                        _ft.partial(
                            wc.ensure_attached,
                            process_name=proc_name, exe_path=exe_path,
                            title_pattern=title_pattern, class_name=class_name,
                            aumid=aumid,
                            launch_if_missing=True,
                            wait_seconds=float(p.get("launch_wait_seconds", 8.0) or 8.0),
                            target_width=int(p.get("window_width", 0) or 0),
                            target_height=int(p.get("window_height", 0) or 0),
                        ),
                    )
                except Exception as e:
                    raise HTTPException(status_code=500, detail=f"WinControl attach failed: {e}")
            elif not wc.is_attached():
                raise HTTPException(status_code=400, detail="WinControl: no window attached")
            import functools as _ft2
            # capture_after_ms: 액션 후 ms 만큼 대기한 뒤 캡처해 응답에 포함.
            # 0/None 이면 캡처 안 함. 양수면 액션의 deferred_restore 안에서 wait+capture
            # 까지 한 사이클로 처리 → 이중 활성화/플리커 방지.
            capture_after_ms_raw = p.get("capture_after_ms")
            capture_after_ms = int(capture_after_ms_raw) if capture_after_ms_raw else 0

            def _run_action():
                if req.action == "win_tap":
                    wc.send_tap(int(p["x"]), int(p["y"]), p.get("button", "left"))
                elif req.action == "win_double_click":
                    wc.send_double_click(int(p["x"]), int(p["y"]))
                elif req.action == "win_long_press":
                    wc.send_long_press(int(p["x"]), int(p["y"]),
                                       int(p.get("duration_ms", 500)),
                                       p.get("button", "left"))
                elif req.action == "win_swipe":
                    wc.send_swipe(int(p["x1"]), int(p["y1"]),
                                  int(p["x2"]), int(p["y2"]),
                                  int(p.get("duration_ms", 300)))
                elif req.action == "win_input_text":
                    cfx = p.get("click_first_x")
                    cfy = p.get("click_first_y")
                    wc.send_text(str(p.get("text", "")),
                                 int(cfx) if cfx is not None else None,
                                 int(cfy) if cfy is not None else None)
                elif req.action == "win_key":
                    wc.send_key(str(p.get("key", "")))
                elif req.action == "win_key_combo":
                    raw = p.get("keys") if "keys" in p else p.get("combo", "")
                    if isinstance(raw, str):
                        import re as _re
                        keys_list = [s.strip() for s in _re.split(r"[+,]", raw) if s.strip()]
                    else:
                        keys_list = [str(k).strip() for k in (raw or []) if str(k).strip()]
                    if not keys_list:
                        raise ValueError("win_key_combo: empty keys")
                    wc.send_key_combo(keys_list)

            # Watchdog: 대상 앱 메시지 펌프가 막혀 native API 가 영영 안 끝나는 경우
            # 워커 스레드가 풀에 못 돌아와 백엔드 전체가 멈추는 문제 방어. 별도
            # 데몬 스레드에 실제 작업을 격리하고 timeout 후 503 으로 반환.
            # text 입력은 글자수 × 10ms 이상 걸릴 수 있어 여유.
            action_timeout_s = 20.0 if req.action == "win_input_text" else 15.0
            if capture_after_ms > 0:
                # 액션 + 대기 + 캡처 + 복원을 한 활성화 사이클로 처리.
                def _action_and_capture():
                    import time as _time, io as _io, base64 as _b64
                    with wc.deferred_restore():
                        _run_action()
                        # UI 반영 대기 (타겟이 여전히 포어그라운드).
                        _time.sleep(capture_after_ms / 1000.0)
                        # 타겟 FG 상태에서 바로 스크린 캡처 (활성화 추가 없음).
                        img = wc._capture_via_screen(wc._hwnd) if wc.is_attached() else None
                    # 컨텍스트 종료 → 우리 앱으로 포커스 복원 (1회만)
                    if img is None:
                        return None
                    buf = _io.BytesIO()
                    img.save(buf, format="JPEG", quality=70)
                    return _b64.b64encode(buf.getvalue()).decode("ascii")

                try:
                    img_b64 = await loop.run_in_executor(
                        None,
                        lambda: wc.run_action_with_timeout(_action_and_capture, action_timeout_s),
                    )
                except TimeoutError as te:
                    raise HTTPException(status_code=503, detail=str(te))
                return {"result": "ok", "image": img_b64 or "", "format": "jpeg"}
            else:
                try:
                    await loop.run_in_executor(
                        None,
                        lambda: wc.run_action_with_timeout(_run_action, action_timeout_s),
                    )
                except TimeoutError as te:
                    raise HTTPException(status_code=503, detail=str(te))
                return {"result": "ok"}

        # ADB actions — allow even if device is not in managed list (race with refresh)
        if dev and dev.type not in ("adb", None):
            raise HTTPException(status_code=400, detail=f"Action '{req.action}' requires an ADB device")

        # Resolve alias to real ADB serial address
        adb_serial = dev.address if dev else req.device_id
        # screen_type은 우리 displays 배열 인덱스(0,1,...)
        # → input -d 에는 Android DisplayManager logical ID로 변환해서 넘겨야 함
        # (폴더블에서 우리 인덱스와 Android logical ID가 어긋날 수 있음)
        our_index = _parse_adb_display_id(req.params.get("screen_type"))
        display_id = resolve_input_display_id(dev.info if dev else None, our_index)

        p = req.params
        if req.action == "tap":
            await adb.tap(p["x"], p["y"], serial=adb_serial, display_id=display_id)
        elif req.action == "repeat_tap":
            await adb.repeat_tap(p["x"], p["y"], int(p.get("count", 5)), int(p.get("interval_ms", 100)),
                                 serial=adb_serial, display_id=display_id)
        elif req.action == "long_press":
            await adb.long_press(p["x"], p["y"], p.get("duration_ms", 1000), serial=adb_serial, display_id=display_id)
        elif req.action == "swipe":
            pts = p.get("points") or []
            if isinstance(pts, list) and len(pts) >= 2:
                await adb.pattern_swipe(pts, p.get("duration_ms", 600), serial=adb_serial, display_id=display_id)
            else:
                await adb.swipe(p["x1"], p["y1"], p["x2"], p["y2"], p.get("duration_ms", 300), serial=adb_serial, display_id=display_id)
        elif req.action == "input_text":
            await adb.input_text(p["text"], serial=adb_serial, display_id=display_id)
        elif req.action == "key_event":
            await adb.key_event(p["keycode"], serial=adb_serial, display_id=display_id)
        elif req.action == "adb_command":
            await adb.run_shell_command(p["command"], serial=adb_serial)
        elif req.action == "multi_touch":
            fingers = p.get("fingers", [])
            if not fingers:
                raise HTTPException(status_code=400, detail="fingers array required")
            # 탭 vs 스와이프 판별: 시작점과 끝점이 같으면 탭
            is_tap = all(f.get("x1") == f.get("x2") and f.get("y1") == f.get("y2") for f in fingers)
            if is_tap:
                points = [{"x": f["x1"], "y": f["y1"]} for f in fingers]
                await adb.multi_finger_tap(points, serial=adb_serial, display_id=display_id)
            else:
                await adb.multi_finger_swipe(fingers, p.get("duration_ms", 500), serial=adb_serial, display_id=display_id)
        else:
            raise HTTPException(status_code=400, detail=f"Unknown action: {req.action}")

        return {"result": "ok"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error("device_input error: action=%s device=%s error=%s", req.action, req.device_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/adb-restart")
async def restart_adb_server():
    """Kill and restart the ADB server to recover from 'connecting' state."""
    try:
        await adb.restart_server()
        return {"result": "ADB server restarted"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class ConnectRegisteredRequest(BaseModel):
    device_ids: list[str] = []  # 빈 리스트면 전체 연결


@router.post("/connect-registered")
async def connect_registered_devices(req: ConnectRegisteredRequest):
    """등록된 디바이스를 연결. device_ids가 비어있으면 전체 연결."""
    all_devices = dm.list_all()
    if req.device_ids:
        targets = [d for d in all_devices if d.id in req.device_ids]
    else:
        targets = all_devices

    results = []
    for dev in targets:
        msg = await dm.connect_device_by_id(dev.id)
        results.append({"device_id": dev.id, "message": msg})
        logger.info("connect-registered: %s", msg)

    return {
        "results": results,
        "primary": _with_protected_flag(dm.list_primary()),
        "auxiliary": _with_protected_flag(dm.list_auxiliary()),
    }


class ReorderDevicesRequest(BaseModel):
    prefix: str
    ordered_ids: list[str]


@router.post("/reorder")
async def reorder_devices(req: ReorderDevicesRequest):
    """그룹 내 디바이스 순서 변경 (ID 번호 재할당)."""
    try:
        dm.reorder_devices(req.prefix, req.ordered_ids)
        return {
            "primary": _with_protected_flag(dm.list_primary()),
            "auxiliary": _with_protected_flag(dm.list_auxiliary()),
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


class UpdateDeviceRequest(BaseModel):
    device_id: str
    new_device_id: Optional[str] = None
    name: Optional[str] = None
    address: Optional[str] = None
    baudrate: Optional[int] = None
    module: Optional[str] = None
    connect_type: Optional[str] = None
    extra_fields: Optional[dict] = None


@router.post("/update")
async def update_device(req: UpdateDeviceRequest):
    """Update an existing device's info."""
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")

    # 시스템 기본 디바이스(Common 등)는 수정 금지
    if dm.is_protected_device(req.device_id):
        raise HTTPException(
            status_code=403,
            detail=f"Device '{req.device_id}' is a protected system default and cannot be modified",
        )

    # ID 변경
    if req.new_device_id and req.new_device_id != req.device_id:
        new_id = req.new_device_id.strip()
        existing = dm.get_device(new_id)
        if existing:
            # 기존 디바이스와 ID 교체(swap)
            dm.swap_device_ids(req.device_id, new_id)
        else:
            dm.rename_device(req.device_id, new_id)
        dev = dm.get_device(new_id)
        if not dev:
            raise HTTPException(status_code=500, detail="Device rename failed")

    need_serial_reconnect = False
    if req.name is not None:
        dev.name = req.name
    if req.address is not None:
        if req.address != dev.address:
            need_serial_reconnect = True
        dev.address = req.address
    if req.baudrate is not None:
        if req.baudrate != dev.info.get("baudrate"):
            need_serial_reconnect = True
        dev.info["baudrate"] = req.baudrate
    if req.module is not None:
        dev.info["module"] = req.module
        # Reset cached module instance when module changes
        from ..services.module_service import reset_instance
        reset_instance(req.module)
    if req.connect_type is not None:
        dev.info["connect_type"] = req.connect_type
    mib_resolution_changed = False
    if req.extra_fields is not None:
        for k, v in req.extra_fields.items():
            # MIB의 resolution은 dict 스키마({width,height})를 보존해야 하므로
            # 문자열 "WxH"로 들어온 경우 파싱하여 두 형태 모두 갱신.
            if dev.type == "mib_agent" and k == "resolution" and isinstance(v, str):
                try:
                    rw_s, rh_s = v.upper().split("X")
                    rw, rh = int(rw_s), int(rh_s)
                    dev.info["resolution"] = {"width": rw, "height": rh}
                    dev.info["resolution_str"] = f"{rw}x{rh}"
                    mib_resolution_changed = True
                except Exception:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Invalid MIB resolution format: {v} (expected WxH, e.g. 2240x1260)",
                    )
            else:
                dev.info[k] = v
        # 활성 MIBAgentService에 즉시 반영 — _x_mult/_y_mult가 새 해상도로 재계산.
        if mib_resolution_changed:
            svc = dm.get_mib_service(dev.id)
            if svc is not None:
                try:
                    svc.resolution = dev.info["resolution_str"]
                except Exception as e:
                    logger.warning("Failed to update live MIB resolution: %s", e)
        # Reset cached module instance when connection params change
        module_name = dev.info.get("module")
        if module_name:
            from ..services.module_service import reset_instance
            reset_instance(module_name)

    # Persist changes — auxiliary는 항상, primary 중 mib_agent는 해상도 변경 시 저장.
    if dev.category == "auxiliary" or (dev.type == "mib_agent" and mib_resolution_changed):
        dm._save_auxiliary_devices()

    # Reopen serial connection if address or baudrate changed
    if need_serial_reconnect and dev.type == "serial":
        dm._close_serial_conn(req.device_id)
        try:
            import asyncio
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, dm._get_serial_conn, req.device_id)
        except Exception as e:
            dev.status = "disconnected"
            return {
                "result": f"updated (reconnect failed: {e})",
                "device": dev.to_dict(),
                "primary": _with_protected_flag(dm.list_primary()),
                "auxiliary": _with_protected_flag(dm.list_auxiliary()),
            }

    return {
        "result": "updated",
        "device": dev.to_dict(),
        "primary": _with_protected_flag(dm.list_primary()),
        "auxiliary": _with_protected_flag(dm.list_auxiliary()),
    }


@router.get("/modules")
async def list_modules():
    """List available lge.auto modules."""
    return {"modules": list_available_modules()}


@router.get("/modules/{module_name}/functions")
async def module_functions(module_name: str):
    """List functions of a specific lge.auto module."""
    from ..services.module_service import _load_guides
    functions = get_module_functions(module_name)
    if not functions:
        raise HTTPException(status_code=404, detail=f"Module '{module_name}' not found or has no functions")
    guides = _load_guides()
    mod_guide = guides.get(module_name, {})
    return {"module": module_name, "functions": functions, "module_description": mod_guide.get("_description", "")}


class DltViewerRequest(BaseModel):
    project_file: str = ""
    log_file: str = ""


# DLT Viewer GUI 전용 싱글톤 (디바이스 연결 없이 GUI만 관리)
_dlt_viewer_instance = None

def _get_dlt_viewer():
    global _dlt_viewer_instance
    if _dlt_viewer_instance is None:
        from ..plugins.DLTViewer import DLTViewer
        _dlt_viewer_instance = DLTViewer()
    return _dlt_viewer_instance


@router.post("/dlt-viewer/launch")
async def launch_dlt_viewer(req: DltViewerRequest):
    """DLT Viewer GUI 실행 (디바이스 연결 불필요)."""
    viewer = _get_dlt_viewer()
    result = viewer.LaunchViewer(req.project_file, req.log_file)
    if result.startswith("ERROR"):
        raise HTTPException(status_code=400, detail=result)
    return {"result": result}


@router.post("/dlt-viewer/close")
async def close_dlt_viewer():
    """DLT Viewer GUI 종료."""
    viewer = _get_dlt_viewer()
    result = viewer.CloseViewer()
    return {"result": result}


@router.get("/isap-keys")
async def list_isap_keys(device_id: Optional[str] = None):
    """List iSAP Agent hardware keys (merged with per-device override).

    device_id가 지정되면 해당 디바이스의 info["isap_keys"] 오버라이드를
    spec default에 병합하여 반환한다. cmd/key/dial/visible 각 항목별로
    override가 있으면 덮어쓰고, 없으면 spec 값을 사용 (visible 기본 True).
    """
    from ..services.isap_agent_service import (
        ISAP_KEYS, SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, KNOB_KEY,
        MONITOR_MAP, SCREEN_PORT_MAP,
    )
    overrides: dict[str, dict] = {}
    if device_id:
        dev = dm.get_device(device_id)
        if dev:
            overrides = dev.info.get("isap_keys") or {}
    keys = []
    for name, info in ISAP_KEYS.items():
        ov = overrides.get(name, {})
        group = name.split("_")[0]  # MKBD, CCP, SWRC, RRC, MIRROR, OVERHEAD, TRIP, GRIP, OPTICAL, RHEOSTAT
        cmd = ov.get("cmd", info["cmd"])
        key = ov.get("key", info["key"])
        is_dial = ov.get("dial", info.get("dial", False))
        visible = ov.get("visible", True)
        keys.append({
            "name": name,
            "group": group,
            "cmd": cmd,
            "key": key,
            "is_dial": is_dial,
            "visible": visible,
        })
    return {
        "keys": keys,
        "sub_commands": {
            "SHORT_KEY": SHORT_KEY,
            "LONG_KEY": LONG_KEY,
            "PRESS_KEY": PRESS_KEY,
            "RELEASE_KEY": RELEASE_KEY,
            "KNOB_KEY": KNOB_KEY,
        },
        "monitors": MONITOR_MAP,
        "screen_ports": SCREEN_PORT_MAP,
    }


class UpdateIsapKeysRequest(BaseModel):
    device_id: str
    # name → {cmd?, key?, dial?, visible?}  (각 필드 선택적)
    keys: dict[str, dict]


@router.post("/isap-keys")
async def update_isap_keys(req: UpdateIsapKeysRequest):
    """Save per-device iSAP key overrides (차종별 키 값 + 표시 여부)."""
    from ..services.isap_agent_service import ISAP_KEYS
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "isap_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not an iSAP agent")
    # 정규화: 알려진 키만 수용, 빈/잘못된 값 필터
    clean: dict[str, dict] = {}
    for name, ov in (req.keys or {}).items():
        if name not in ISAP_KEYS:
            continue
        entry: dict = {}
        if "cmd" in ov and ov["cmd"] is not None:
            try:
                entry["cmd"] = int(ov["cmd"])
            except (TypeError, ValueError):
                pass
        if "key" in ov and ov["key"] is not None:
            try:
                entry["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if "dial" in ov and ov["dial"] is not None:
            entry["dial"] = bool(ov["dial"])
        if "visible" in ov and ov["visible"] is not None:
            entry["visible"] = bool(ov["visible"])
        if entry:
            clean[name] = entry
    if clean:
        dev.info["isap_keys"] = clean
    else:
        dev.info.pop("isap_keys", None)
    # 연결된 service에 즉시 반영
    svc = dm.get_isap_service(req.device_id)
    if svc:
        svc.set_key_overrides(dev.info.get("isap_keys"))
    dm._save_auxiliary_devices()
    return {"status": "ok", "device_id": req.device_id, "count": len(clean)}


@router.get("/hkmc-keys")
async def list_hkmc_keys(device_id: Optional[str] = None):
    """List HKMC hardware keys (merged with per-device override).

    device_id가 지정되면 해당 디바이스의 info["hkmc_keys"] 오버라이드를
    spec default에 병합하여 반환한다.
    """
    from ..services.hkmc6th_service import (
        HKMC_KEYS, SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, DIAL_ACTION,
        resolve_device_variant,
    )
    overrides: dict[str, dict] = {}
    device_variant = "navi"  # 미지정/미상 모델 기본
    if device_id:
        dev = dm.get_device(device_id)
        if dev:
            overrides = dev.info.get("hkmc_keys") or {}
            device_variant = resolve_device_variant(dev.info.get("device_model", ""))
    keys = []
    for name, info in HKMC_KEYS.items():
        ov = overrides.get(name, {})
        group = name.split("_")[0]  # MKBD, MKBD2, CCP, RRC, SWRC, SWRC2, MIRROR
        cmd = ov.get("cmd", info["cmd"])
        key = ov.get("key", info["key"])
        is_dial = ov.get("dial", info.get("dial", False))
        key_variant = info.get("variant")  # "navi" | "non_navi" | None(공용)
        # variant 필터: 디바이스가 Navi 면 non_navi 전용 키 숨김, 그 반대도 동일.
        # 명시적 override 가 있으면 그 값을 우선 (사용자가 강제로 노출 가능).
        if "visible" in ov:
            visible = ov["visible"]
        elif key_variant and key_variant != device_variant:
            visible = False
        else:
            visible = True
        keys.append({
            "name": name,
            "group": group,
            "cmd": cmd,
            "key": key,
            "is_dial": is_dial,
            "visible": visible,
            "variant": key_variant,
        })
    return {
        "keys": keys,
        "sub_commands": {
            "SHORT_KEY": SHORT_KEY,
            "LONG_KEY": LONG_KEY,
            "PRESS_KEY": PRESS_KEY,
            "RELEASE_KEY": RELEASE_KEY,
            "DIAL_ACTION": DIAL_ACTION,
        },
    }


@router.get("/hkmc5th-wide-keys")
async def list_hkmc5th_wide_keys(device_id: Optional[str] = None):
    """List HKMC 5th gen (Wide) hardware keys (merged with per-device override)."""
    from ..services.hkmc5th_wide_service import (
        HKMC5TH_WIDE_KEYS, SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY, DIAL_ACTION,
    )
    overrides: dict[str, dict] = {}
    if device_id:
        dev = dm.get_device(device_id)
        if dev:
            overrides = dev.info.get("HKMC5TH_WIDE_KEYS") or {}
    keys = []
    for name, info in HKMC5TH_WIDE_KEYS.items():
        ov = overrides.get(name, {})
        if info.get("msg"):
            group = "MSG"
        else:
            group = name.split("_")[0]
        cmd = ov.get("cmd", info["cmd"])
        key = ov.get("key", info.get("key", 0))
        is_dial = ov.get("dial", info.get("dial", False))
        visible = ov.get("visible", True)
        keys.append({
            "name": name,
            "group": group,
            "cmd": cmd,
            "key": key,
            "is_dial": is_dial,
            "is_msg": bool(info.get("msg")),
            "visible": visible,
        })
    return {
        "keys": keys,
        "sub_commands": {
            "SHORT_KEY": SHORT_KEY,
            "LONG_KEY": LONG_KEY,
            "PRESS_KEY": PRESS_KEY,
            "RELEASE_KEY": RELEASE_KEY,
            "DIAL_ACTION": DIAL_ACTION,
        },
    }


class UpdateHkmc5thWideKeysRequest(BaseModel):
    device_id: str
    keys: dict[str, dict]  # name → {cmd?, key?, dial?, visible?}


@router.post("/hkmc5th-wide-keys")
async def update_hkmc5th_wide_keys(req: UpdateHkmc5thWideKeysRequest):
    """Save per-device HKMC 5th gen (Wide) key overrides."""
    from ..services.hkmc5th_wide_service import HKMC5TH_WIDE_KEYS
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "hkmc5th_wide_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not a HKMC5thWide agent")
    clean: dict[str, dict] = {}
    for name, ov in (req.keys or {}).items():
        if name not in HKMC5TH_WIDE_KEYS:
            continue
        entry: dict = {}
        if "cmd" in ov and ov["cmd"] is not None:
            try:
                entry["cmd"] = int(ov["cmd"])
            except (TypeError, ValueError):
                pass
        if "key" in ov and ov["key"] is not None:
            try:
                entry["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if "dial" in ov and ov["dial"] is not None:
            entry["dial"] = bool(ov["dial"])
        if "visible" in ov and ov["visible"] is not None:
            entry["visible"] = bool(ov["visible"])
        if entry:
            clean[name] = entry
    if clean:
        dev.info["HKMC5TH_WIDE_KEYS"] = clean
    else:
        dev.info.pop("HKMC5TH_WIDE_KEYS", None)
    svc = dm.get_hkmc5th_wide_service(req.device_id)
    if svc:
        svc.set_key_overrides(dev.info.get("HKMC5TH_WIDE_KEYS"))
    dm._save_auxiliary_devices()
    return {"status": "ok", "device_id": req.device_id, "count": len(clean)}


@router.get("/icas-keys")
async def list_icas_keys(device_id: Optional[str] = None):
    """List ICAS hardware keys (merged with per-device override).

    device_id가 지정되면 해당 디바이스의 info["icas_keys"] 오버라이드를
    spec default에 병합하여 반환한다. class(short|long)/key/visible 필드 병합.
    """
    from ..services.icas_agent_service import ICAS_KEYS, SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY
    overrides: dict[str, dict] = {}
    if device_id:
        dev = dm.get_device(device_id)
        if dev:
            overrides = dev.info.get("icas_keys") or {}
    keys = []
    for name, info in ICAS_KEYS.items():
        ov = overrides.get(name, {})
        group = "ICAS"  # ICAS는 단일 그룹 — 프리셋 확장 시 세분화
        klass = ov.get("class", info.get("class", "short"))
        key_code = ov.get("key", info["key"])
        visible = ov.get("visible", True)
        # hkmc/isap 구조와 호환: cmd=0(더미), is_dial=False
        keys.append({
            "name": name,
            "group": group,
            "cmd": 0,
            "key": key_code,
            "class": klass,
            "is_dial": False,
            "visible": visible,
        })
    return {
        "keys": keys,
        "sub_commands": {
            "SHORT_KEY": SHORT_KEY,
            "LONG_KEY": LONG_KEY,
            "PRESS_KEY": PRESS_KEY,
            "RELEASE_KEY": RELEASE_KEY,
        },
    }


class UpdateIcasKeysRequest(BaseModel):
    device_id: str
    keys: dict[str, dict]  # name → {class?, key?, visible?}


@router.post("/icas-keys")
async def update_icas_keys(req: UpdateIcasKeysRequest):
    """Save per-device ICAS key overrides."""
    from ..services.icas_agent_service import ICAS_KEYS
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "icas_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not an ICAS agent")
    clean: dict[str, dict] = {}
    for name, ov in (req.keys or {}).items():
        if name not in ICAS_KEYS:
            continue
        entry: dict = {}
        if "class" in ov and ov["class"] in ("short", "long"):
            entry["class"] = ov["class"]
        if "key" in ov and ov["key"] is not None:
            try:
                entry["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if "visible" in ov and ov["visible"] is not None:
            entry["visible"] = bool(ov["visible"])
        if entry:
            clean[name] = entry
    if clean:
        dev.info["icas_keys"] = clean
    else:
        dev.info.pop("icas_keys", None)
    svc = dm.get_icas_service(req.device_id)
    if svc:
        svc.set_key_overrides(dev.info.get("icas_keys"))
    dm._save_auxiliary_devices()
    return {"status": "ok", "device_id": req.device_id, "count": len(clean)}


@router.get("/mib-keys")
async def list_mib_keys(device_id: Optional[str] = None):
    """List MIB hardware keys (merged with per-device override)."""
    from ..services.mib_agent_service import MIB_KEYS, SHORT_KEY, LONG_KEY, PRESS_KEY, RELEASE_KEY
    overrides: dict[str, dict] = {}
    if device_id:
        dev = dm.get_device(device_id)
        if dev:
            overrides = dev.info.get("mib_keys") or {}
    keys = []
    for name, info in MIB_KEYS.items():
        ov = overrides.get(name, {})
        klass = ov.get("class", info.get("class", "short"))
        key_code = ov.get("key", info["key"])
        visible = ov.get("visible", True)
        keys.append({
            "name": name,
            "group": "MIB",
            "cmd": 0,
            "key": key_code,
            "class": klass,
            "is_dial": False,
            "visible": visible,
        })
    return {
        "keys": keys,
        "sub_commands": {
            "SHORT_KEY": SHORT_KEY,
            "LONG_KEY": LONG_KEY,
            "PRESS_KEY": PRESS_KEY,
            "RELEASE_KEY": RELEASE_KEY,
        },
    }


class UpdateMibKeysRequest(BaseModel):
    device_id: str
    keys: dict[str, dict]


@router.post("/mib-keys")
async def update_mib_keys(req: UpdateMibKeysRequest):
    """Save per-device MIB key overrides."""
    from ..services.mib_agent_service import MIB_KEYS
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "mib_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not a MIB agent")
    clean: dict[str, dict] = {}
    for name, ov in (req.keys or {}).items():
        if name not in MIB_KEYS:
            continue
        entry: dict = {}
        if "class" in ov and ov["class"] in ("short", "long"):
            entry["class"] = ov["class"]
        if "key" in ov and ov["key"] is not None:
            try:
                entry["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if "visible" in ov and ov["visible"] is not None:
            entry["visible"] = bool(ov["visible"])
        if entry:
            clean[name] = entry
    if clean:
        dev.info["mib_keys"] = clean
    else:
        dev.info.pop("mib_keys", None)
    svc = dm.get_mib_service(req.device_id)
    if svc:
        svc.set_key_overrides(dev.info.get("mib_keys"))
    dm._save_auxiliary_devices()
    return {"status": "ok", "device_id": req.device_id, "count": len(clean)}


class MibDetectResolutionRequest(BaseModel):
    device_id: str


@router.post("/mib/detect_resolution")
async def mib_detect_resolution(req: MibDetectResolutionRequest):
    """MIB 디바이스 실제 해상도를 1회 캡처로 감지하고 영구 저장."""
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "mib_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not a MIB agent")
    svc = dm.get_mib_service(req.device_id)
    if svc is None or not getattr(svc, "is_connected", False):
        raise HTTPException(
            status_code=409,
            detail=f"Device {req.device_id} is not connected. Connect first then retry.",
        )
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        width, height = await loop.run_in_executor(None, svc.detect_resolution)
    except Exception as e:
        logger.warning("MIB detect_resolution failed for %s: %s", req.device_id, e)
        raise HTTPException(status_code=500, detail=f"Detect failed: {e}")
    return {
        "device_id": req.device_id,
        "width": int(width),
        "height": int(height),
        "resolution_str": f"{int(width)}x{int(height)}",
        "device": dev.to_dict(),
    }


class UpdateHkmcKeysRequest(BaseModel):
    device_id: str
    keys: dict[str, dict]  # name → {cmd?, key?, dial?, visible?}


@router.post("/hkmc-keys")
async def update_hkmc_keys(req: UpdateHkmcKeysRequest):
    """Save per-device HKMC key overrides (차종별 키 값 + 표시 여부)."""
    from ..services.hkmc6th_service import HKMC_KEYS
    dev = dm.get_device(req.device_id)
    if not dev:
        raise HTTPException(status_code=404, detail=f"Device {req.device_id} not found")
    if dev.type != "hkmc_agent":
        raise HTTPException(status_code=400, detail=f"Device {req.device_id} is not an HKMC device")
    clean: dict[str, dict] = {}
    for name, ov in (req.keys or {}).items():
        if name not in HKMC_KEYS:
            continue
        entry: dict = {}
        if "cmd" in ov and ov["cmd"] is not None:
            try:
                entry["cmd"] = int(ov["cmd"])
            except (TypeError, ValueError):
                pass
        if "key" in ov and ov["key"] is not None:
            try:
                entry["key"] = int(ov["key"])
            except (TypeError, ValueError):
                pass
        if "dial" in ov and ov["dial"] is not None:
            entry["dial"] = bool(ov["dial"])
        if "visible" in ov and ov["visible"] is not None:
            entry["visible"] = bool(ov["visible"])
        if entry:
            clean[name] = entry
    if clean:
        dev.info["hkmc_keys"] = clean
    else:
        dev.info.pop("hkmc_keys", None)
    svc = dm.get_hkmc_service(req.device_id)
    if svc:
        svc.set_key_overrides(dev.info.get("hkmc_keys"))
    dm._save_auxiliary_devices()
    return {"status": "ok", "device_id": req.device_id, "count": len(clean)}


class WebcamExposureRequest(BaseModel):
    value: Optional[float] = None
    auto: Optional[bool] = None


@router.get("/webcam-exposure/{device_id}")
async def get_webcam_exposure(device_id: str):
    """주 디바이스로 등록된 웹캠의 현재 노출값/모드 조회."""
    dev = dm.get_device(device_id)
    if not dev or dev.type != "webcam":
        raise HTTPException(status_code=400, detail=f"Device {device_id} is not a webcam")
    cam = dm.get_webcam_device(device_id)
    if not cam:
        raise HTTPException(status_code=400, detail=f"Webcam {device_id} not connected")
    import asyncio
    loop = asyncio.get_event_loop()
    info = await loop.run_in_executor(None, cam.GetExposure)
    return info


@router.post("/webcam-exposure/{device_id}")
async def set_webcam_exposure(device_id: str, req: WebcamExposureRequest):
    """주 디바이스로 등록된 웹캠의 노출값 설정 (value 또는 auto 지정)."""
    dev = dm.get_device(device_id)
    if not dev or dev.type != "webcam":
        raise HTTPException(status_code=400, detail=f"Device {device_id} is not a webcam")
    cam = dm.get_webcam_device(device_id)
    if not cam:
        raise HTTPException(status_code=400, detail=f"Webcam {device_id} not connected")
    import asyncio
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(None, cam.SetExposure, req.value, req.auto)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to set exposure")
    info = await loop.run_in_executor(None, cam.GetExposure)
    return info


@router.get("/screenshot/{device_id}")
async def get_screenshot(device_id: str, fmt: str = "jpeg", screen_type: str = "front_center"):
    """Capture and return a screenshot for a specific primary device."""
    dev = dm.get_device(device_id)
    try:
        if dev and dev.type == "hkmc_agent":
            hkmc = dm.get_hkmc_service(device_id)
            if not hkmc:
                raise HTTPException(status_code=400, detail=f"HKMC device {device_id} not connected")
            w, h = hkmc.get_screen_size(screen_type)
            logger.debug("[HKMC SCREENSHOT] device=%s screen=%s size=%dx%d connected=%s",
                         device_id, screen_type, w, h, hkmc.is_connected)
            img_bytes = await hkmc.async_screencap_bytes(screen_type=screen_type, fmt=fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "isap_agent":
            isap = dm.get_isap_service(device_id)
            if not isap:
                raise HTTPException(status_code=400, detail=f"iSAP device {device_id} not connected")
            img_bytes = await isap.async_screencap_bytes(screen_type=screen_type, fmt=fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "icas_agent":
            icas = dm.get_icas_service(device_id)
            if not icas:
                raise HTTPException(status_code=400, detail=f"ICAS device {device_id} not connected")
            # ICAS 기본 화면은 HU. HKMC 호환으로 기본이 front_center로 들어올 수 있어 변환.
            st = screen_type if screen_type in ("HU", "IID", "HUD") else "HU"
            img_bytes = await icas.async_screencap_bytes(screen_type=st, fmt=fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "mib_agent":
            mib = dm.get_mib_service(device_id)
            if not mib:
                raise HTTPException(status_code=400, detail=f"MIB device {device_id} not connected")
            st = screen_type if screen_type in ("HU", "IID", "HUD") else "HU"
            img_bytes = await mib.async_screencap_bytes(screen_type=st, fmt=fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "vision_camera":
            cam = dm.get_vision_camera(device_id)
            if not cam:
                raise HTTPException(status_code=400, detail=f"VisionCamera {device_id} not connected")
            import asyncio
            loop = asyncio.get_event_loop()
            img_bytes = await loop.run_in_executor(None, cam.CaptureBytes, fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "webcam":
            cam = dm.get_webcam_device(device_id)
            if not cam:
                raise HTTPException(status_code=400, detail=f"Webcam {device_id} not connected")
            import asyncio
            loop = asyncio.get_event_loop()
            img_bytes = await loop.run_in_executor(None, cam.CaptureBytes, fmt)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
        elif dev and dev.type == "wincontrol":
            wc = dm.get_wincontrol_service()
            if not wc.is_attached():
                # 임베드 전 또는 윈도우 핸들 무효 → 빈 이미지 + attached=false
                # 프론트엔드 폴링 루프가 이 플래그를 보고 자동 재attach 트리거.
                return {"image": "", "format": fmt, "attached": False}
            import asyncio
            loop = asyncio.get_event_loop()
            try:
                img_bytes = await loop.run_in_executor(None, wc.capture_window, fmt)
                b64 = base64.b64encode(img_bytes).decode("ascii")
                return {"image": b64, "format": fmt, "attached": True}
            except Exception:
                # 캡처 일시 실패: attach 상태는 유지, 빈 응답만 반환
                return {"image": "", "format": fmt, "attached": wc.is_attached()}
        elif dev and dev.type not in ("adb",):
            raise HTTPException(status_code=400, detail="Screenshot only available for ADB, HKMC, iSAP, ICAS, VisionCamera, Webcam, or WinControl devices")
        else:
            # ADB device
            adb_serial = dev.address if dev else device_id
            display_id = _parse_adb_display_id(screen_type)
            sf_did = resolve_sf_display_id(dev.info if dev else None, display_id)
            img_bytes = await adb.screencap_bytes(serial=adb_serial, fmt=fmt, sf_display_id=sf_did)
            b64 = base64.b64encode(img_bytes).decode("ascii")
            return {"image": b64, "format": fmt}
    except HTTPException:
        raise
    except Exception:
        # Transient ADB/HKMC capture failure — return empty image so the
        # browser doesn't log a 500 error on every polling cycle.
        return {"image": "", "format": fmt}


# ── WinControl 전용 엔드포인트 ────────────────────────────────────


class WinControlAttachRequest(BaseModel):
    hwnd: int


class WinControlResizeRequest(BaseModel):
    """타겟 윈도우의 client area 를 (client_w, client_h) 로 리사이즈."""
    client_w: int
    client_h: int


@router.get("/wincontrol/processes")
async def wincontrol_list_processes():
    """현재 시스템의 가시 윈도우/프로세스 목록 (콤보용)."""
    wc = dm.get_wincontrol_service()
    if not wc.is_available():
        raise HTTPException(status_code=503, detail=f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {wc.import_error() or _WC_MISSING_DEP_MSG}")
    import asyncio
    loop = asyncio.get_event_loop()
    procs = await loop.run_in_executor(None, wc.list_processes)
    return {"processes": procs}


@router.get("/wincontrol/status")
async def wincontrol_status():
    """현재 임베드된 윈도우 정보."""
    return dm.get_wincontrol_service().status()


@router.post("/wincontrol/attach")
async def wincontrol_attach(req: WinControlAttachRequest):
    """대상 프로세스 윈도우에 임베드 — 디바이스 connection 과는 별개."""
    wc = dm.get_wincontrol_service()
    if not wc.is_available():
        raise HTTPException(status_code=503, detail=f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {wc.import_error() or _WC_MISSING_DEP_MSG}")
    try:
        info = wc.attach(req.hwnd)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))
    # 디바이스 status 는 사용자의 명시적 connect/disconnect 로만 변경 — sync 호출 안 함.
    return {"result": "attached", "status": info}


@router.post("/wincontrol/detach")
async def wincontrol_detach():
    """임베드만 해제 — WinControl 디바이스 자체의 연결 상태는 유지."""
    dm.get_wincontrol_service().detach()
    return {"result": "detached"}


@router.post("/wincontrol/resize")
async def wincontrol_resize(req: WinControlResizeRequest):
    """현재 임베드된 윈도우의 client area 를 지정 크기로 리사이즈.
    웹 위젯 영역에 비율 맞춰 임베드하기 위해 프론트가 측정한 목표 client 크기를 보냄.
    """
    wc = dm.get_wincontrol_service()
    if not wc.is_available():
        raise HTTPException(status_code=503, detail=f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {wc.import_error() or _WC_MISSING_DEP_MSG}")
    if not wc.is_attached():
        raise HTTPException(status_code=400, detail="WinControl: no window attached")
    if req.client_w <= 0 or req.client_h <= 0:
        raise HTTPException(status_code=400, detail="client_w/client_h must be positive")
    import asyncio
    loop = asyncio.get_event_loop()
    import functools as _ft
    actual_w, actual_h = await loop.run_in_executor(
        None, _ft.partial(wc.resize_client, int(req.client_w), int(req.client_h)),
    )
    return {"result": "resized", "actual_w": actual_w, "actual_h": actual_h, "status": wc.status()}
