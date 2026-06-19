"""Unified Device Manager — ADB + Serial 장치를 통합 관리."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .adb_service import ADBService
from .hkmc6th_service import HKMC6thService
from .hkmc5th_wide_service import HKMC5thWideService
from .isap_agent_service import ISAPAgentService
from .icas_agent_service import ICASAgentService
from .mib_agent_service import MIBAgentService
from .ssh_service import SSHConnection
from .wincontrol_service import WinControlService
from .lincontrol_service import LinControlService

# OS 에 따라 WinControl(Win32) vs LinControl(X11) 분기.
# 양쪽 모두 동일 API surface (is_available/list_processes/attach/.../send_*)를 노출하므로
# 라우터/메인 캡처 루프는 단일 get_wincontrol_service() 호출만으로 OS 호환.
_WIN_CTRL_IS_LINUX = sys.platform.startswith("linux")
_WindowControlService = LinControlService if _WIN_CTRL_IS_LINUX else WinControlService
_WIN_CTRL_DISPLAY_NAME = "LinuxControl" if _WIN_CTRL_IS_LINUX else "WinControl"

logger = logging.getLogger(__name__)

_AUX_DEVICES_FILE = Path(__file__).resolve().parent.parent.parent / "auxiliary_devices.json"


def _scan_serial_ports() -> list[dict]:
    """USB/시리얼 장치 스캔.

    Linux 노이즈 필터 (REPLAYKIT_SHOW_ALL_SERIAL=1 로 비활성화 가능):
      - /dev/ttyS0~ttyS31 : 보드 내장 UART 슬롯. BIOS 가 무조건 등록하는 가상
        포트라 보통 물리적으로 미사용. ReplayKit 워크플로우는 USB-Serial
        (ttyUSB*/ttyACM*) 만 의미 있음.
      - description "n/a" 또는 빈 값 : pyserial 이 메타데이터를 못 읽어낸
        장치. 의미 있는 USB 장치라면 보통 vendor/product 문자열이 채워짐.

    Windows / macOS 에서는 필터링 안 함 (각 OS 의 의미 있는 포트는 다름).
    """
    from serial.tools import list_ports

    show_all = bool(os.environ.get("REPLAYKIT_SHOW_ALL_SERIAL"))
    is_linux = sys.platform.startswith("linux")

    ports = []
    for p in list_ports.comports():
        if is_linux and not show_all:
            # ttyS* 슬롯 노이즈 제거
            if p.device and re.match(r"^/dev/ttyS\d+$", p.device):
                continue
            # description 없음 / "n/a" 제거
            desc = (p.description or "").strip().lower()
            if desc in ("", "n/a"):
                continue

        ports.append({
            "port": p.device,
            "description": p.description,
            "hwid": p.hwid,
            "manufacturer": p.manufacturer or "",
            "vid": f"0x{p.vid:04X}" if p.vid else "",
            "pid": f"0x{p.pid:04X}" if p.pid else "",
        })
    return ports


HKMC_SCAN_PORTS = [6655, 5000]


def _collect_local_subnets_192() -> tuple[set[str], list]:
    """로컬 192.168.* 서브넷만 수집 (IDS 오탐 방지를 위한 스캔 대상 제한).

    /20보다 큰(즉 prefix < 20) 대규모 네트워크는 제외.

    Returns:
        (local_ips_set, subnet_list) — 자기 자신 IP 집합과 IPv4Network 목록
    """
    import ipaddress
    import ifaddr

    local_ips: set[str] = {"127.0.0.1"}
    subnets: list = []
    for adapter in ifaddr.get_adapters():
        for ip_info in adapter.ips:
            if not isinstance(ip_info.ip, str):
                continue
            ip_str = ip_info.ip
            prefix = ip_info.network_prefix
            if ip_str.startswith("127.") or ip_str.startswith("169.254."):
                continue
            # 192.168.* 대역만 허용 — 10.x, 172.16-31.x 등은 스캔 제외
            if not ip_str.startswith("192.168."):
                continue
            local_ips.add(ip_str)
            try:
                net = ipaddress.IPv4Network(f"{ip_str}/{prefix}", strict=False)
                if net.prefixlen >= 20:
                    subnets.append(net)
            except ValueError:
                pass
    # 중복 서브넷 제거
    unique = list({str(s): s for s in subnets}.values())
    return local_ips, unique


def _collect_candidate_ips_192() -> set[str]:
    """192.168.* 대역에 속한 후보 IP 집합 반환 (자기 자신 제외)."""
    local_ips, subnets = _collect_local_subnets_192()
    candidate_ips: set[str] = set()
    for subnet in subnets:
        for host in subnet.hosts():
            ip_str = str(host)
            if ip_str not in local_ips:
                candidate_ips.add(ip_str)
    return candidate_ips
HKMC_HANDSHAKE_VALUES = {
    bytes.fromhex("6161000000035e002185fd6f6f"),
    bytes.fromhex("6161000000035e0000df856f6f"),
}


async def _probe_hkmc_host(
    ip: str, port: int, timeout: float, semaphore: asyncio.Semaphore
) -> dict | None:
    """단일 IP에 TCP 연결 시도 + HKMC 핸드셰이크 검증."""
    async with semaphore:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            try:
                data = await asyncio.wait_for(reader.read(13), timeout=2.0)
                verified = data in HKMC_HANDSHAKE_VALUES
            except Exception:
                verified = False
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if verified:
                return {"ip": ip, "port": port}
            return None
        except (asyncio.TimeoutError, OSError, ConnectionRefusedError):
            return None


async def _scan_hkmc_tcp(
    ports: list[int] | None = None,
    connect_timeout: float = 0.3,
    max_concurrent: int = 100,
) -> list[dict]:
    """LAN 서브넷의 모든 IP에 TCP 연결을 시도하여 HKMC 에이전트를 탐지한다.
    스캔 범위는 192.168.* 로 제한한다.
    """
    if not ports:
        logger.info("HKMC scan skipped: no ports configured")
        return []

    candidate_ips = _collect_candidate_ips_192()
    if not candidate_ips:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        _probe_hkmc_host(ip, port, connect_timeout, semaphore)
        for ip in candidate_ips
        for port in ports
    ]
    results = await asyncio.gather(*tasks)
    found = [r for r in results if r is not None]

    seen_ips: set[str] = set()
    deduped: list[dict] = []
    for r in found:
        if r["ip"] not in seen_ips:
            seen_ips.add(r["ip"])
            deduped.append(r)
            logger.info("HKMC TCP scan: found device at %s:%d", r["ip"], r["port"])

    return deduped


# ── 범용 TCP 포트 스캔 ──────────────────────────────────────────────

async def _probe_tcp_port(
    ip: str, port: int, timeout: float, semaphore: asyncio.Semaphore
) -> dict | None:
    """단일 IP:Port에 TCP 연결 시도."""
    async with semaphore:
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return {"ip": ip, "port": port}
        except Exception:
            pass
    return None


async def _tcp_reachable(ip: str, port: int, timeout: float = 0.5) -> bool:
    """IP:Port 에 TCP 연결이 되는지만 빠르게 확인 (세마포어 불필요한 단발 프로브)."""
    if not ip or not port:
        return False
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def scan_tcp_port(
    port: int,
    connect_timeout: float = 0.3,
    max_concurrent: int = 100,
) -> list[dict]:
    """LAN 서브넷(192.168.*)에서 특정 TCP 포트가 열린 호스트를 탐지."""
    candidate_ips = _collect_candidate_ips_192()
    if not candidate_ips:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [_probe_tcp_port(ip, port, connect_timeout, semaphore) for ip in candidate_ips]
    results = await asyncio.gather(*tasks)
    found = [r for r in results if r is not None]
    logger.info("TCP port %d scan: found %d hosts", port, len(found))
    return found


# ── 범용 UDP 포트 스캔 ──────────────────────────────────────────────

async def _probe_udp_port(
    ip: str, port: int, timeout: float, semaphore: asyncio.Semaphore
) -> dict | None:
    """단일 IP:Port에 UDP 프로브 전송 후 응답 확인."""
    async with semaphore:
        try:
            loop = asyncio.get_event_loop()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(timeout)
            sock.setblocking(False)
            # 빈 패킷 전송 후 응답 대기
            await loop.sock_sendto(sock, b"\x00", (ip, port))
            try:
                data = await asyncio.wait_for(
                    loop.sock_recv(sock, 1024), timeout=timeout
                )
                sock.close()
                if data:
                    return {"ip": ip, "port": port}
            except (asyncio.TimeoutError, Exception):
                sock.close()
        except Exception:
            pass
    return None


async def _scan_udp_port(
    port: int,
    connect_timeout: float = 0.5,
    max_concurrent: int = 100,
) -> list[dict]:
    """LAN 서브넷(192.168.*)에서 특정 UDP 포트에 응답하는 호스트를 탐지."""
    candidate_ips = _collect_candidate_ips_192()
    if not candidate_ips:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [_probe_udp_port(ip, port, connect_timeout, semaphore) for ip in candidate_ips]
    results = await asyncio.gather(*tasks)
    found = [r for r in results if r is not None]
    logger.info("UDP port %d scan: found %d hosts", port, len(found))
    return found


# ── DLT 데몬 TCP 스캔 ──────────────────────────────────────────────

DLT_SCAN_PORTS = [3490]


async def _probe_dlt_host(
    ip: str, port: int, timeout: float, semaphore: asyncio.Semaphore
) -> dict | None:
    """단일 IP에 TCP 연결 시도로 DLT 데몬 탐지."""
    async with semaphore:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout
            )
            # DLT 데몬은 연결 즉시 메시지를 보내므로 짧게 읽어 검증
            verified = False
            try:
                data = await asyncio.wait_for(reader.read(4), timeout=1.5)
                if len(data) >= 4:
                    # DLT Standard Header: version 1 = (htyp >> 5) & 0x07 == 1
                    htyp = data[0]
                    version = (htyp >> 5) & 0x07
                    verified = version == 1
                elif len(data) > 0:
                    # 데이터가 오면 DLT일 가능성 있음
                    verified = True
            except Exception:
                # 연결은 되지만 데이터가 없을 수도 있음 — 포트 열림만으로 후보 처리
                verified = True
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if verified:
                return {"ip": ip, "port": port}
        except Exception:
            pass
    return None


async def _scan_dlt_tcp(
    ports: list[int] | None = None,
    connect_timeout: float = 0.3,
    max_concurrent: int = 100,
) -> list[dict]:
    """LAN 서브넷(192.168.*)에서 DLT 데몬을 탐지."""
    if not ports:
        logger.info("DLT scan skipped: no ports configured")
        return []

    candidate_ips = _collect_candidate_ips_192()
    if not candidate_ips:
        return []

    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        _probe_dlt_host(ip, port, connect_timeout, semaphore)
        for ip in candidate_ips
        for port in ports
    ]
    results = await asyncio.gather(*tasks)
    found = [r for r in results if r is not None]

    seen_ips: set[str] = set()
    deduped: list[dict] = []
    for r in found:
        if r["ip"] not in seen_ips:
            seen_ips.add(r["ip"])
            deduped.append(r)
            logger.info("DLT scan: found daemon at %s:%d", r["ip"], r["port"])

    return deduped


# ── SmartBench 자동 탐지 ──
# 설정(scan_settings.builtin.smartbench)의 host/port를 기반으로 TCP 프로브.
SMARTBENCH_HOST = "192.167.0.5"
SMARTBENCH_PORT = 8000


async def _scan_smartbench(host: str | None = None, port: int | None = None) -> list[dict]:
    """SmartBench 장비 탐지 — 설정된 host/port로 TCP 연결 프로브.

    과거에는 로컬 PC에 특정 IP(192.167.0.4)가 붙어 있을 때만 동작했으나,
    이제 호스트/포트가 스캔 설정으로 구성되므로 해당 프리체크는 제거됨.
    연결 실패 시 2초 timeout 후 빈 리스트 반환.
    """
    target_host = (host or SMARTBENCH_HOST).strip()
    target_port = int(port) if port else SMARTBENCH_PORT

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _probe_smartbench_sync, target_host, target_port, 2.0,
    )
    if result:
        logger.info("SmartBench scan: found %s:%d", target_host, target_port)
        return [result]
    logger.debug("SmartBench scan: %s:%d not reachable", target_host, target_port)
    return []


def _probe_smartbench_sync(ip: str, port: int, timeout: float) -> dict | None:
    """SmartBench 프로브 — TCP 연결 후 CONNECT 핸드셰이크까지 검증.

    단순 TCP 포트 listen 만으로는 다른 장비도 오탐되므로, SmartBench 플러그인이
    사용하는 프로토콜 `CONNECT\\n` → `CONNECTED` 응답을 확인한 경우에만 발견으로 처리.
    이후 플러그인 연결에 영향이 없도록 `DISCONNECT\\n`로 정리 후 소켓 종료.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect((ip, port))
        # 핸드셰이크
        sock.settimeout(timeout)
        sock.sendall(b"CONNECT\n")
        rc = sock.recv(1024).decode("utf-8", errors="replace").replace("\n", "").replace(" ", "").strip()
        if rc != "CONNECTED":
            logger.debug("SmartBench probe: unexpected handshake response %r at %s:%d", rc, ip, port)
            return None
        # 깨끗한 종료 — 다음 연결에 영향 없도록
        try:
            sock.sendall(b"DISCONNECT\n")
            sock.settimeout(0.5)
            sock.recv(1024)
        except Exception:
            pass
        return {
            "ip": ip,
            "port": port,
            "label": "SmartBench",
            "module": "SmartBench",
        }
    except Exception as e:
        logger.debug("SmartBench probe %s:%d failed: %s", ip, port, e)
        return None
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ── radmoon (TH host bridge cvd-ebr) 자동 탐지 ──
# 사용자 환경에서 TH 셋업은 `connect_th.sh` 가 만드는 bridge cvd-ebr 가 이미 존재하는 형태로 시작한다.
# 따라서 "radmoon 스캔" 은 USB 어댑터 enumeration 이 아니라 **cvd-ebr bridge 가 있는지** 확인 +
# 현재 IP, 현재 bridge member(잠재 eth_if) 까지 함께 보고.
async def _scan_radmoon(bridge: str | None = None) -> list[dict]:
    if not sys.platform.startswith("linux"):
        logger.debug("radmoon scan skipped: not Linux (%s)", sys.platform)
        return []

    bridge_name = (bridge or "cvd-ebr").strip() or "cvd-ebr"
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _scan_radmoon_sync, bridge_name)


def _scan_radmoon_sync(bridge_name: str) -> list[dict]:
    sysnet = Path("/sys/class/net")
    if not sysnet.is_dir():
        return []

    bridge_path = sysnet / bridge_name
    if not bridge_path.is_dir():
        logger.debug("radmoon scan: bridge '%s' not present", bridge_name)
        return []

    # 현재 bridge 의 IPv4 — `ip -4 addr show dev <bridge>`
    current_ips: list[str] = []
    try:
        res = subprocess.run(
            ["ip", "-4", "addr", "show", "dev", bridge_name],
            capture_output=True, text=True, timeout=5,
        )
        for line in res.stdout.splitlines():
            m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)\b", line)
            if m:
                current_ips.append(m.group(1))
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # 현재 bridge member — /sys/class/net/<iface>/master symlink 가 <bridge> 를 가리키는 인터페이스
    members: list[dict] = []
    for iface_path in sorted(sysnet.iterdir()):
        iface = iface_path.name
        if iface == bridge_name:
            continue
        master_link = iface_path / "master"
        try:
            if master_link.exists():
                target = os.path.realpath(str(master_link))
                if os.path.basename(target) == bridge_name:
                    try:
                        mac = (iface_path / "address").read_text().strip()
                    except OSError:
                        mac = ""
                    try:
                        operstate = (iface_path / "operstate").read_text().strip()
                    except OSError:
                        operstate = "unknown"
                    members.append({
                        "interface": iface,
                        "mac": mac,
                        "operstate": operstate,
                    })
        except OSError:
            continue

    try:
        bridge_operstate = (bridge_path / "operstate").read_text().strip()
    except OSError:
        bridge_operstate = "unknown"

    # 멤버 우선순위 정렬 — 자동 채움 (frontend 가 members[0] 을 선택) 시 의미 있는 어댑터가
    # 첫 자리에 오도록.
    #   1) enx* 명명 (radmoon 실 어댑터) + up
    #   2) enx* + 그 외
    #   3) up 상태이지만 enx* 아님 (v-IVC 등 가상)
    #   4) 나머지 (cvd-etap-* 가상 down 등)
    def _member_priority(m: dict) -> tuple:
        iface = m.get("interface", "")
        is_enx = iface.startswith("enx")
        is_up = m.get("operstate") == "up"
        return (0 if is_enx else 1, 0 if is_up else 1, iface)

    members.sort(key=_member_priority)

    logger.info(
        "radmoon scan: bridge=%s state=%s ips=%s members=%d (first=%s)",
        bridge_name, bridge_operstate, current_ips, len(members),
        members[0]["interface"] if members else "(none)",
    )
    return [{
        "bridge": bridge_name,
        "bridge_operstate": bridge_operstate,
        "current_ips": current_ips,
        "members": members,
        "label": "RAD_Moon (cvd-ebr)",
        "module": "TH",
    }]


# ── SCAR 자동 탐지 (Linux 전용 플러그인) ──
# REST API (http://<host>:<port>/) 와 docker container 두 가지를 병렬로 프로브.
# 한쪽이라도 살아있으면 후보 1행 반환 — SCAR 플러그인이 매 호출마다 Ready() 로 모드 자동 판별.
async def _scan_scar(host: str | None = None, port: int | None = None,
                     container: str | None = None) -> list[dict]:
    target_host = (host or "localhost").strip() or "localhost"
    try:
        target_port = int(port) if port else 8081
    except (TypeError, ValueError):
        target_port = 8081
    target_container = (container or "scar").strip() or "scar"

    # Linux 가 아니면 docker / SCAR 플러그인 자체가 사용 불가 — 즉시 빈 결과.
    if not sys.platform.startswith("linux"):
        logger.debug("SCAR scan skipped: not Linux (%s)", sys.platform)
        return []

    loop = asyncio.get_event_loop()
    api_fut = loop.run_in_executor(None, _probe_scar_api_sync, target_host, target_port, 2.0)
    docker_fut = loop.run_in_executor(None, _probe_scar_docker_sync, target_container, 3.0)
    docker_cli_fut = loop.run_in_executor(None, _docker_cli_available_sync)
    api_alive, docker_running, docker_installed = await asyncio.gather(
        api_fut, docker_fut, docker_cli_fut
    )

    # SCAR 는 컨테이너가 아직 안 떠 있어도 "등록 → auto_setup 이 scar.sh 로 자동 기동" 하는
    # 흐름을 위해 docker CLI 가 설치돼 있으면 후보 1행을 항상 노출한다 (닭-달걀 해소).
    # docker 자체가 없는 PC + API/컨테이너 모두 다운이면 띄울 방법이 없으므로 숨긴다.
    if not api_alive and not docker_running and not docker_installed:
        logger.debug("SCAR scan: API %s:%d down, container '%s' not running, docker CLI absent",
                     target_host, target_port, target_container)
        return []

    # netns VLAN 구성 폼의 iface 자동 채움용 — RAD_Moon/Technica 후보 인터페이스.
    # interfaces[0] 는 인터넷(default route) 어댑터를 강등한 안전한 후보.
    interfaces = await loop.run_in_executor(None, _scan_net_interfaces_sync)
    # 인터넷 어댑터 목록 — UI 경고용 (netns 가 가져가면 인터넷 끊김).
    internet_ifaces = await loop.run_in_executor(None, _default_route_ifaces_sync)

    logger.info("SCAR scan: api_alive=%s docker_running=%s docker_installed=%s "
                "(%s:%d, container=%s, ifaces=%s, internet=%s)",
                api_alive, docker_running, docker_installed,
                target_host, target_port, target_container, interfaces, sorted(internet_ifaces))
    return [{
        "ip": target_host,
        "port": target_port,
        "container": target_container,
        "api_alive": bool(api_alive),
        "docker_running": bool(docker_running),
        "docker_installed": bool(docker_installed),
        "interfaces": interfaces,
        "internet_ifaces": sorted(internet_ifaces),
        "label": "SCAR",
        "module": "SCAR",
    }]


def _default_route_ifaces_sync() -> set[str]:
    """default route(인터넷 경로)를 가진 인터페이스 집합.

    netns 가 이 인터페이스를 네임스페이스로 가져가면 호스트 인터넷이 끊기므로
    SCAR netns 후보에서 제외/강등하는 데 쓴다.
    """
    try:
        res = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=3.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return set()
    if res.returncode != 0:
        return set()
    ifaces: set[str] = set()
    for line in (res.stdout or "").splitlines():
        # 예: "default via 10.176.146.1 dev enp3s0 proto dhcp metric 100"
        toks = line.split()
        if "dev" in toks:
            idx = toks.index("dev") + 1
            if idx < len(toks):
                ifaces.add(toks[idx])
    return ifaces


def _scan_net_interfaces_sync() -> list[str]:
    """SCAR netns 용 후보 네트워크 인터페이스.

    ⚠️ 인터넷(default route)을 들고 있는 어댑터는 netns 가 가져가면 호스트 인터넷이
    끊기므로 후보 **최하위로 강등**한다 (USB LAN 으로 인터넷이 나가는 PC 보호).
    그 외에는 enx*(USB, 미디어 컨버터) 우선, up 우선.

    radmoon(cvd-ebr) 멤버 탐지와 달리 SCAR 는 모드(multiverse/standalone)에 따라
    bridge 가 없을 수도 있으므로 물리 USB 어댑터(enx*)를 직접 나열한다.
    lo / docker / veth / cvd-* 등 가상/시스템 인터페이스는 제외.
    """
    sysnet = Path("/sys/class/net")
    if not sysnet.is_dir():
        return []
    inet_ifaces = _default_route_ifaces_sync()
    skip_prefix = ("lo", "docker", "veth", "br-", "cvd-", "virbr", "tap", "tun")
    cands: list[tuple[tuple, str]] = []
    for iface_path in sysnet.iterdir():
        iface = iface_path.name
        if iface.startswith(skip_prefix):
            continue
        try:
            operstate = (iface_path / "operstate").read_text().strip()
        except OSError:
            operstate = "unknown"
        has_default = iface in inet_ifaces       # 인터넷 NIC → 무조건 뒤로
        is_enx = iface.startswith("enx")
        is_up = operstate == "up"
        # 우선순위: default route 없음 → enx* → up → 이름순
        cands.append((
            (1 if has_default else 0, 0 if is_enx else 1, 0 if is_up else 1, iface),
            iface,
        ))
    cands.sort(key=lambda c: c[0])
    if inet_ifaces:
        logger.info("SCAR iface scan: default-route(인터넷) 어댑터 강등: %s", sorted(inet_ifaces))
    return [name for _, name in cands]


def _probe_scar_api_sync(host: str, port: int, timeout: float) -> bool:
    """GET http://host:port/ — 2xx~3xx 면 True. requests 가 없으면 stdlib http.client 폴백."""
    try:
        import http.client
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", "/")
            resp = conn.getresponse()
            return 200 <= resp.status < 400
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        logger.debug("SCAR API probe %s:%d failed: %s", host, port, e)
        return False


def _docker_cli_available_sync() -> bool:
    """docker CLI 가 PATH 에 존재하는지. 컨테이너 기동 가능 여부 판정용.

    실행은 하지 않고 바이너리 존재만 확인 (subprocess 비용 회피).
    """
    return shutil.which("docker") is not None


def _probe_scar_docker_sync(container: str, timeout: float) -> bool:
    """docker inspect -f {{.State.Running}} <container> — true 이면 True."""
    import subprocess
    try:
        res = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.debug("docker inspect timed out (container=%s)", container)
        return False
    except FileNotFoundError:
        logger.debug("docker binary not found")
        return False
    if res.returncode != 0:
        return False
    return res.stdout.strip().lower() == "true"


# ── WoohyunBench 자동 탐지 ──
# SmartBench와 동일한 단일-프로브 방식: 설정에 명시된 host:port로 한 번만 UDP 프로브.
# LAN 전체(ARP + ping + UDP 스윕)는 시간이 길고 다른 장비를 깨우는 부작용이 있어 제거.
BENCH_DEFAULT_HOST = "192.168.1.101"
BENCH_DEFAULT_PORT = 25000
BENCH_UDP_PROBE = bytes([0x55, 0xAA, 100, 0, 0x20, 0x02, 0x00, 0x00])


def _probe_udp_bench_sync(ip: str, port: int, timeout: float) -> dict | None:
    """UDP 프로브 전송 후 0x55 0xAA 응답이면 verified."""
    import socket as _socket
    sock = None
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        sock.connect((ip, port))
        sock.sendto(BENCH_UDP_PROBE, (ip, port))
        sock.settimeout(timeout)
        data = sock.recv(16)
        sock.settimeout(None)
        if len(data) >= 2 and data[0] == 0x55 and data[1] == 0xAA:
            return {"ip": ip, "port": port, "verified": True}
    except Exception:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass
    return None


async def _scan_woohyun_bench(host: str | None = None, port: int | None = None) -> list[dict]:
    """WoohyunBench 단일 호스트 UDP 프로브.

    설정의 host/port (기본 192.168.1.101:25000)에 대해 한 번만 UDP 프로브를 수행한다.
    응답 받으면 verified=True로 반환, 응답이 없으면 빈 리스트(미발견).
    """
    target_host = (host or BENCH_DEFAULT_HOST).strip()
    target_port = int(port) if port else BENCH_DEFAULT_PORT

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None, _probe_udp_bench_sync, target_host, target_port, 2.0,
    )
    if result is not None:
        logger.info("Bench scan: found %s:%d (UDP verified)", target_host, target_port)
        return [result]
    logger.debug("Bench scan: %s:%d not reachable", target_host, target_port)
    return []


def _validate_serial(port: str, baudrate: int) -> str:
    import serial
    s = serial.Serial(port, baudrate=baudrate, timeout=1)
    s.close()
    return f"OK: {port} @ {baudrate} baud"


def _send_serial_persistent(conn, data: str, read_timeout: float = 1.0) -> str:
    """Send data on an already-open serial connection and return the response."""
    import time
    # Drain any leftover data before sending
    if conn.in_waiting:
        conn.read(conn.in_waiting)
    # Ensure newline terminator for Arduino readStringUntil('\n')
    if not data.endswith("\n"):
        data += "\n"
    conn.write(data.encode())
    conn.flush()
    time.sleep(read_timeout)
    response = b""
    while conn.in_waiting:
        response += conn.read(conn.in_waiting)
    # Strip null bytes from response
    return response.replace(b"\x00", b"").decode(errors="replace").strip()


class ManagedDevice:
    """A device tracked by the manager (ADB or Serial)."""

    def __init__(
        self,
        id: str,
        type: str,  # "adb" | "serial"
        category: str,  # "primary" | "auxiliary"
        address: str,
        status: str = "connected",
        name: str = "",
        info: Optional[dict] = None,
    ):
        self.id = id
        self.type = type
        self.category = category
        self.address = address
        self.status = status
        self.name = name
        self.info = info or {}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "category": self.category,
            "address": self.address,
            "status": self.status,
            "name": self.name,
            "info": self.info,
        }


class DeviceManager:
    """Manages all connected devices (ADB + Serial)."""

    def __init__(self, adb: ADBService):
        self.adb = adb
        self._devices: dict[str, ManagedDevice] = {}
        self._serial_conns: dict[str, "serial.Serial"] = {}  # device_id -> open serial connection
        self._hkmc_conns: dict[str, HKMC6thService] = {}  # device_id -> HKMC6thService
        self._hkmc_reconnect_attempts: dict[str, int] = {}  # device_id -> 연속 재연결 실패 횟수
        self._hkmc5th_wide_conns: dict[str, HKMC5thWideService] = {}  # device_id -> HKMC5thWideService
        self._hkmc5th_wide_reconnect_attempts: dict[str, int] = {}
        self._isap_conns: dict[str, ISAPAgentService] = {}  # device_id -> ISAPAgentService
        self._isap_reconnect_attempts: dict[str, int] = {}
        self._icas_conns: dict[str, ICASAgentService] = {}  # device_id -> ICASAgentService
        self._icas_reconnect_attempts: dict[str, int] = {}
        self._mib_conns: dict[str, MIBAgentService] = {}  # device_id -> MIBAgentService
        self._mib_reconnect_attempts: dict[str, int] = {}
        self._adb_reconnect_attempts: dict[str, int] = {}  # device_id -> 연속 재연결 실패 횟수
        # 디바이스별 재연결 락: playback의 _ensure_device_connected와 백그라운드 monitor 루프가
        # 같은 디바이스를 동시에 재연결하지 못하도록 직렬화. race condition 제거용.
        self._reconnect_locks: dict[str, asyncio.Lock] = {}
        self._vision_cams: dict[str, object] = {}  # device_id -> VisionCamera instance
        self._webcam_devs: dict[str, object] = {}  # device_id -> WebcamDevice instance
        self._ssh_conns: dict[str, SSHConnection] = {}  # device_id -> SSHConnection
        self._ever_connected: set[str] = set()  # 사용자가 명시적으로 연결한 디바이스만 자동 재연결
        # OS 분기: Linux 면 X11 기반 LinControlService, Windows 면 Win32 기반 WinControlService.
        # 디바이스 ID/타입은 양쪽 모두 "WinControl"/"wincontrol" 로 유지 (프론트/시나리오 호환).
        # 표시명만 OS 별로 다르게 적용.
        self._wincontrol = _WindowControlService()  # type: ignore[assignment]
        self._load_auxiliary_devices()
        self._ensure_default_common_device()
        self._ensure_default_wincontrol_device()
        self._ensure_default_ocr_device()

    # 기본 Common 디바이스 ID — 삭제/수정 금지
    DEFAULT_COMMON_DEVICE_ID = "Common"
    # 기본 WinControl 디바이스 ID — 삭제/수정 금지. 미연결 상태가 기본.
    DEFAULT_WINCONTROL_DEVICE_ID = "WinControl"
    # 기본 OCR 디바이스 ID — 삭제/수정 금지
    DEFAULT_OCR_DEVICE_ID = "OCR"

    def _ensure_default_common_device(self) -> None:
        """Common 디바이스를 기본값으로 등록 + 상태를 항상 connected로 고정.

        OS 별 module 분기:
          - Windows: CMD (cmd.exe 기반)
          - Linux/macOS: SHELL (bash 기반)
        시나리오 이식성을 위해 device ID 는 "Common" 으로 동일. 다른 OS 에서 만든
        시나리오를 열면 device 가 자동으로 host OS 의 module 을 사용 — module_command 의
        params.module 도 device 의 info.module 을 따른다.
        """
        default_module = "SHELL" if sys.platform.startswith("linux") else "CMD"
        existing = self._devices.get(self.DEFAULT_COMMON_DEVICE_ID)
        if existing:
            # 기존 디바이스가 있으면 상태/타입 복구 + cross-OS module 자동 교정.
            existing.status = "connected"
            existing.type = "module"
            existing.category = "auxiliary"
            # 이전 OS 의 module 명이 박혀 있으면 (예: Windows 빌드본 → Linux 사용) 현재 OS 의 것으로 교체.
            cur_module = (existing.info or {}).get("module")
            if cur_module != default_module:
                existing.info = {**(existing.info or {}), "module": default_module, "connect_type": "none"}
                logger.info("Common device module auto-migrated: %s → %s", cur_module, default_module)
                self._save_auxiliary_devices()
            return
        dev = ManagedDevice(
            id=self.DEFAULT_COMMON_DEVICE_ID,
            type="module",
            category="auxiliary",
            address="",
            status="connected",  # 연결 불필요한 모듈이므로 바로 사용 가능
            name="Common",
            info={"module": default_module, "connect_type": "none"},
        )
        self._devices[self.DEFAULT_COMMON_DEVICE_ID] = dev
        self._save_auxiliary_devices()
        logger.info("Registered default 'Common' device (%s module)", default_module)

    def _ensure_default_ocr_device(self) -> None:
        """OCR 가상 디바이스를 기본값으로 등록 + 상태를 항상 connected로 고정."""
        existing = self._devices.get(self.DEFAULT_OCR_DEVICE_ID)
        if existing and existing.info.get("module") == "OCR":
            existing.status = "connected"
            existing.type = "module"
            existing.category = "auxiliary"
            return
        dev = ManagedDevice(
            id=self.DEFAULT_OCR_DEVICE_ID,
            type="module",
            category="auxiliary",
            address="",
            status="connected",
            name="Common",
            info={"module": "OCR", "connect_type": "none"},
        )
        self._devices[self.DEFAULT_OCR_DEVICE_ID] = dev
        self._save_auxiliary_devices()
        logger.info("Registered default 'OCR' device (OCR module)")

    def _ensure_default_wincontrol_device(self) -> None:
        """WinControl 디바이스를 기본값으로 등록 (미연결 상태가 기본).

        디바이스 status 는 사용자의 명시적 connect/disconnect 로만 변경되며,
        attach/detach(임베드 여부) 와는 독립적이다 — 임베드를 풀어도 디바이스 자체는 연결됨.
        """
        existing = self._devices.get(self.DEFAULT_WINCONTROL_DEVICE_ID)
        if existing and existing.type == "wincontrol":
            existing.category = "auxiliary"
            # 재시작 시점엔 항상 미임베드 상태이므로 disconnected 로 시작.
            existing.status = "disconnected"
            # OS 가 바뀌었거나 기존 디바이스 표시명이 다른 경우 갱신 (Linux→LinControl, Win→WinControl).
            # 디스크 캐시(auxiliary_devices.json) 에도 즉시 반영 — 안 그러면 다음 부팅 때 옛 name 으로 로드됨.
            if existing.name != _WIN_CTRL_DISPLAY_NAME:
                existing.name = _WIN_CTRL_DISPLAY_NAME
                self._save_auxiliary_devices()
            return
        dev = ManagedDevice(
            id=self.DEFAULT_WINCONTROL_DEVICE_ID,
            type="wincontrol",
            category="auxiliary",
            address="",
            status="disconnected",
            name=_WIN_CTRL_DISPLAY_NAME,
            info={"connect_type": "none"},
        )
        self._devices[self.DEFAULT_WINCONTROL_DEVICE_ID] = dev
        self._save_auxiliary_devices()
        logger.info("Registered default '%s' device (disconnected)", _WIN_CTRL_DISPLAY_NAME)

    def get_wincontrol_service(self):
        """OS 에 따라 WinControlService(Windows) 또는 LinControlService(Linux) 반환.

        반환 타입은 동일한 API surface 를 가진 union — 호출자(라우터, main 캡처 루프)는
        구체 타입을 신경 쓰지 않고 is_available/is_attached/list_processes/capture_window/
        send_tap/.../detach 만 사용. 함수명은 외부 호환을 위해 그대로 유지.
        """
        return self._wincontrol

    def sync_wincontrol_status(self) -> None:
        """attach 상태에 맞춰 디바이스 status를 동기화."""
        dev = self._devices.get(self.DEFAULT_WINCONTROL_DEVICE_ID)
        if not dev:
            return
        dev.status = "connected" if self._wincontrol.is_attached() else "disconnected"

    def is_protected_device(self, device_id: str) -> bool:
        """삭제/수정이 금지된 시스템 기본 디바이스인지 여부."""
        return device_id in (self.DEFAULT_COMMON_DEVICE_ID, self.DEFAULT_WINCONTROL_DEVICE_ID, self.DEFAULT_OCR_DEVICE_ID)

    def _load_auxiliary_devices(self) -> None:
        """Load saved auxiliary devices from disk.

        레거시 자동 마이그레이션:
          - type "hkmc6th" → "hkmc_agent"
          - info.module "CCIC_BENCH" → "WoohyunBench"
        """
        if not _AUX_DEVICES_FILE.exists():
            return
        try:
            data = json.loads(_AUX_DEVICES_FILE.read_text(encoding="utf-8"))
            migrated = False
            for d in data:
                dev_type = d.get("type", "")
                if dev_type == "hkmc6th":
                    dev_type = "hkmc_agent"
                    d["type"] = dev_type
                    migrated = True
                info = d.get("info") or {}
                if isinstance(info, dict) and info.get("module") == "CCIC_BENCH":
                    info["module"] = "WoohyunBench"
                    d["info"] = info
                    migrated = True
                dev = ManagedDevice(
                    id=d["id"],
                    type=dev_type,
                    category=d.get("category", "primary" if dev_type == "adb" else "auxiliary"),
                    address=d["address"],
                    status="unknown",
                    name=d.get("name", d["id"]),
                    info=d.get("info", {}),
                )
                self._devices[dev.id] = dev
            logger.info("Loaded %d auxiliary devices from %s", len(data), _AUX_DEVICES_FILE)
            if migrated:
                # 마이그레이션된 경우 즉시 디스크에 반영
                self._save_auxiliary_devices()
        except Exception as e:
            logger.warning("Failed to load auxiliary devices: %s", e)

    def _generate_device_id(self, dev_type: str, module_name: str = "", device_model: str = "") -> str:
        """Auto-generate a device ID like Connected_Wide_1, GVM_1, HKMC_1, POWER_1, etc."""
        if device_model:
            prefix = device_model.replace(" ", "_")
        elif module_name:
            prefix = module_name
        elif dev_type == "adb":
            prefix = "Android"
        elif dev_type == "serial":
            prefix = "Serial"
        elif dev_type == "hkmc_agent":
            prefix = "HKMC"
        elif dev_type == "hkmc5th_wide_agent":
            prefix = "HKMC5thWide"
        elif dev_type == "isap_agent":
            prefix = "iSAP"
        elif dev_type == "icas_agent":
            prefix = "ICAS"
        elif dev_type == "mib_agent":
            prefix = "MIB"
        elif dev_type == "vision_camera":
            prefix = "VisionCam"
        elif dev_type == "webcam":
            prefix = "Webcam"
        elif dev_type == "ssh":
            prefix = "SSH"
        else:
            prefix = "Device"
        # Find the highest existing number for this prefix
        pattern = re.compile(rf"^{re.escape(prefix)}_(\d+)$", re.IGNORECASE)
        max_num = 0
        for existing_id in self._devices:
            m = pattern.match(existing_id)
            if m:
                max_num = max(max_num, int(m.group(1)))
        return f"{prefix}_{max_num + 1}"

    def _save_auxiliary_devices(self) -> None:
        """Persist all manually registered devices (auxiliary + ADB + HKMC + SSH) to disk."""
        aux = [
            d.to_dict()
            for d in self._devices.values()
            if d.category == "auxiliary" or d.type in ("adb", "hkmc_agent", "hkmc5th_wide_agent", "isap_agent", "icas_agent", "mib_agent", "vision_camera", "webcam", "ssh")
        ]
        try:
            _AUX_DEVICES_FILE.write_text(json.dumps(aux, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to save auxiliary devices: %s", e)

    async def refresh_adb(self) -> None:
        """Sync ADB device statuses — only update already-registered ADB devices."""
        # 등록된 ADB 디바이스가 없으면 ADB 호출 안 함
        has_adb = any(v.type == "adb" and v.id in self._ever_connected
                      for v in self._devices.values())
        if not has_adb:
            return
        adb_devices = await self.adb.list_devices()
        adb_status_map = {d.serial: d for d in adb_devices}

        for k, v in list(self._devices.items()):
            if v.type != "adb":
                continue
            # 사용자가 연결 끊기한 디바이스는 자동 상태 갱신 안 함
            if v.id not in self._ever_connected:
                continue
            # Check by address (actual ADB serial) instead of device id (may be alias)
            adb_serial = v.address
            if adb_serial in adb_status_map:
                d = adb_status_map[adb_serial]
                v.status = d.status
                if d.status == "device":
                    try:
                        info = await self.adb.get_device_info(d.serial)
                        if not v.name or v.name == v.address:
                            v.name = info.get("model", v.name)
                        v.info = info
                    except Exception:
                        pass
            else:
                v.status = "offline"

    async def add_adb_device(self, serial: str, device_id: str = "", name: str = "", device_model: str = "") -> ManagedDevice:
        """ADB 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행)."""
        final_id = device_id or self._generate_device_id("adb", device_model=device_model)
        display_name = name or serial

        # 디바이스 정보만 조회 (연결 시도 아님)
        info = {}
        try:
            adb_devices = await self.adb.list_devices()
            found = next((d for d in adb_devices if d.serial == serial), None)
            if found:
                if found.status == "device":
                    info = await self.adb.get_device_info(serial)
                if not name:
                    display_name = info.get("model", serial) if info else serial
        except Exception:
            pass

        if device_model:
            info["device_model"] = device_model
        dev = ManagedDevice(
            id=final_id,
            type="adb",
            category="primary",
            address=serial,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    async def add_hkmc6th_device(self, host: str, port: int, device_id: str = "", name: str = "", device_model: str = "",
                                 ssh_username: str = "root", ssh_password: str = "",
                                 ssh_port: int = 10022,
                                 cluster_resolution: str = "2720x720",
                                 cluster_display: str = "1",
                                 cluster_overlay_display: str = "",
                                 cluster_composite_mode: str = "off",
                                 cluster_overlay_key_color: str = "0,0,0",
                                 cluster_overlay_threshold: int = 24,
                                 cluster_composite_live: bool = True,
                                 cluster_crop: str = "") -> ManagedDevice:
        """HKMC 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행).

        클러스터 캡처는 legacy CLU_IMG_GET와 동일한 SSH+screenshot+SCP 경로를 사용한다.
        ssh_username 기본값은 ICAS QNX와 동일한 `root`(빈 패스워드). SSH 실패 시 TCP CMD_GETIMG 폴백.
        """
        final_id = device_id or self._generate_device_id("hkmc_agent", device_model=device_model)
        display_name = name or f"HKMC ({host}:{port})"
        info: dict = {"port": port}
        if device_model:
            info["device_model"] = device_model
        # 클러스터 SSH 캡처 설정 — 항상 저장(기본 root/빈 패스워드).
        info["ssh_username"] = ssh_username if ssh_username else "root"
        info["ssh_password"] = ssh_password if ssh_password is not None else ""
        info["ssh_port"] = int(ssh_port) if ssh_port else 10022
        info["cluster_resolution"] = cluster_resolution or "2720x720"
        info["cluster_display"] = str(cluster_display) if cluster_display is not None else "1"
        # 클러스터 2-레이어 합성 설정 (배경 + 알람/정보 오버레이 플레인).
        info["cluster_overlay_display"] = str(cluster_overlay_display or "").strip()
        info["cluster_composite_mode"] = str(cluster_composite_mode or "off").strip().lower()
        info["cluster_overlay_key_color"] = str(cluster_overlay_key_color or "0,0,0")
        info["cluster_overlay_threshold"] = int(cluster_overlay_threshold) if cluster_overlay_threshold else 24
        info["cluster_composite_live"] = bool(cluster_composite_live)
        info["cluster_crop"] = str(cluster_crop or "")

        dev = ManagedDevice(
            id=final_id,
            type="hkmc_agent",
            category="primary",
            address=host,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_hkmc_service(self, device_id: str) -> Optional[HKMC6thService]:
        """Get HKMC6thService instance for a device. Returns None if not found."""
        svc = self._hkmc_conns.get(device_id)
        if svc:
            return svc
        # Fallback: device_map이 address로 해석된 경우, address로 디바이스를 찾아 ID로 재조회
        dev = self.get_device(device_id)
        if dev and dev.type == "hkmc_agent":
            return self._hkmc_conns.get(dev.id)
        return None

    async def add_hkmc5th_wide_device(self, host: str, port: int, device_id: str = "", name: str = "",
                                      device_model: str = "") -> ManagedDevice:
        """HKMC 5th gen (Wide) 디바이스 등록 (연결은 connect_device_by_id로 별도 수행)."""
        final_id = device_id or self._generate_device_id("hkmc5th_wide_agent", device_model=device_model)
        display_name = name or f"HKMC5thWide ({host}:{port})"
        info: dict = {"port": port}
        if device_model:
            info["device_model"] = device_model

        dev = ManagedDevice(
            id=final_id,
            type="hkmc5th_wide_agent",
            category="primary",
            address=host,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_hkmc5th_wide_service(self, device_id: str) -> Optional[HKMC5thWideService]:
        """Get HKMC5thWideService instance for a device. Returns None if not found."""
        svc = self._hkmc5th_wide_conns.get(device_id)
        if svc:
            return svc
        dev = self.get_device(device_id)
        if dev and dev.type == "hkmc5th_wide_agent":
            return self._hkmc5th_wide_conns.get(dev.id)
        return None

    async def add_isap_agent_device(self, host: str, port: int, device_id: str = "",
                                    name: str = "", device_model: str = "") -> ManagedDevice:
        """iSAP Agent 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행)."""
        final_id = device_id or self._generate_device_id("isap_agent", device_model=device_model)
        display_name = name or f"iSAP ({host}:{port})"
        info: dict = {"port": port}
        if device_model:
            info["device_model"] = device_model

        dev = ManagedDevice(
            id=final_id,
            type="isap_agent",
            category="primary",
            address=host,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_isap_service(self, device_id: str) -> Optional[ISAPAgentService]:
        """Get ISAPAgentService instance for a device. Returns None if not found."""
        svc = self._isap_conns.get(device_id)
        if svc:
            return svc
        dev = self.get_device(device_id)
        if dev and dev.type == "isap_agent":
            return self._isap_conns.get(dev.id)
        return None

    async def add_icas_agent_device(self, host: str, port: int = 22, device_id: str = "",
                                    name: str = "", device_model: str = "",
                                    username: str = "root", password: str = "",
                                    resolution: str = "1560x700",
                                    private_server_ip: str = "",
                                    private_server_password: str = "",
                                    iid_display: str = "10",
                                    hud_display: str = "11",
                                    market: str = "") -> ManagedDevice:
        """ICAS Agent 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행).

        market이 비어있으면 device_model에서 추론 (EU/NAR/CN/GP). 추론 실패 시 "EU" 기본.
        """
        final_id = device_id or self._generate_device_id("icas_agent", device_model=device_model)
        display_name = name or f"ICAS ({host}:{port})"
        # "WxH" 문자열을 dict으로 파싱 (프론트엔드 deviceRes 호환). 파싱 실패 시 기본값.
        try:
            rw_s, rh_s = str(resolution).upper().split("X")
            res_dict = {"width": int(rw_s), "height": int(rh_s)}
        except Exception:
            res_dict = {"width": 1560, "height": 700}

        # market 추론: 명시 > device_model 키워드 매칭 > EU 기본
        resolved_market = (market or "").strip().upper()
        if not resolved_market and device_model:
            dm_upper = device_model.upper()
            for m in ("EU", "NAR", "CN", "GP"):
                if m in dm_upper:
                    resolved_market = m
                    break
        if not resolved_market:
            resolved_market = "EU"

        info: dict = {
            "port": int(port),
            "username": username,
            "password": password,
            "resolution": res_dict,         # dict form — HKMC/iSAP/ADB와 동일한 스키마
            "resolution_str": str(resolution),  # ICAS 서비스 생성자용 원본 문자열
            "private_server_ip": private_server_ip,  # 빈 문자열이면 market 기본값 사용
            "private_server_password": private_server_password,
            "iid_display": str(iid_display),
            "hud_display": str(hud_display),
            "market": resolved_market,
        }
        if device_model:
            info["device_model"] = device_model

        dev = ManagedDevice(
            id=final_id,
            type="icas_agent",
            category="primary",
            address=host,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_icas_service(self, device_id: str) -> Optional[ICASAgentService]:
        """Get ICASAgentService instance for a device. Returns None if not found."""
        svc = self._icas_conns.get(device_id)
        if svc:
            return svc
        dev = self.get_device(device_id)
        if dev and dev.type == "icas_agent":
            return self._icas_conns.get(dev.id)
        return None

    async def add_mib_agent_device(self, host: str, port: int = 22, device_id: str = "",
                                   name: str = "", device_model: str = "",
                                   username: str = "root", password: str = "",
                                   resolution: str = "1560x700",
                                   private_server_ip: str = "",
                                   private_server_password: str = "",
                                   iid_display: str = "10",
                                   hud_display: str = "11",
                                   market: str = "") -> ManagedDevice:
        """MIB Agent 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행).

        VW MIB(Modular Infotainment Building Block) HU용. ksend의 bit-position form 사용,
        해상도 자동 감지/보정, screen 인덱스 동적 학습 등이 활성화됨.
        """
        final_id = device_id or self._generate_device_id("mib_agent", device_model=device_model)
        display_name = name or f"MIB ({host}:{port})"
        try:
            rw_s, rh_s = str(resolution).upper().split("X")
            res_dict = {"width": int(rw_s), "height": int(rh_s)}
        except Exception:
            res_dict = {"width": 1560, "height": 700}

        resolved_market = (market or "EU").strip().upper() or "EU"

        info: dict = {
            "port": int(port),
            "username": username,
            "password": password,
            "resolution": res_dict,
            "resolution_str": str(resolution),
            "private_server_ip": private_server_ip,
            "private_server_password": private_server_password,
            "iid_display": str(iid_display),
            "hud_display": str(hud_display),
            "market": resolved_market,
        }
        if device_model:
            info["device_model"] = device_model

        dev = ManagedDevice(
            id=final_id,
            type="mib_agent",
            category="primary",
            address=host,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_mib_service(self, device_id: str) -> Optional[MIBAgentService]:
        """Get MIBAgentService instance for a device. Returns None if not found."""
        svc = self._mib_conns.get(device_id)
        if svc:
            return svc
        dev = self.get_device(device_id)
        if dev and dev.type == "mib_agent":
            return self._mib_conns.get(dev.id)
        return None

    async def add_vision_camera_device(self, mac: str, model: str = "", serial: str = "",
                                       ip: str = "", subnetmask: str = "255.255.0.0",
                                       device_id: str = "", name: str = "") -> ManagedDevice:
        """비전 카메라 등록만 (연결은 connect_device_by_id로 별도 수행)."""
        final_id = device_id or self._generate_device_id("vision_camera")
        display_name = name or f"VisionCam ({mac})"

        dev = ManagedDevice(
            id=final_id,
            type="vision_camera",
            category="primary",
            address=ip or mac,
            status="disconnected",
            name=display_name,
            info={
                "mac": mac,
                "model": model,
                "serial_number": serial,
                "ip": ip,
                "subnetmask": subnetmask,
            },
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_vision_camera(self, device_id: str):
        """Get VisionCamera instance for a device. Returns None if not found."""
        cam = self._vision_cams.get(device_id)
        if cam:
            return cam
        dev = self.get_device(device_id)
        if dev and dev.type == "vision_camera":
            return self._vision_cams.get(dev.id)
        return None

    async def add_webcam_device(self, device_index: int, width: int = 640, height: int = 480,
                                device_id: str = "", name: str = "") -> ManagedDevice:
        """웹캠 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행)."""
        final_id = device_id or self._generate_device_id("webcam")
        display_name = name or f"Webcam {device_index}"

        dev = ManagedDevice(
            id=final_id,
            type="webcam",
            category="primary",
            address=f"webcam:{device_index}",
            status="disconnected",
            name=display_name,
            info={
                "device_index": int(device_index),
                "width": int(width) if width else 0,
                "height": int(height) if height else 0,
            },
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    def get_webcam_device(self, device_id: str):
        """Get WebcamDevice instance. Returns None if not found."""
        cam = self._webcam_devs.get(device_id)
        if cam:
            return cam
        dev = self.get_device(device_id)
        if dev and dev.type == "webcam":
            return self._webcam_devs.get(dev.id)
        return None

    async def refresh_auxiliary(self) -> None:
        """빠른 상태 확인만 수행 (네트워크 I/O 없음). 재연결은 백그라운드에서."""
        # Serial/Module(COM 포트 기반) 디바이스의 물리적 연결 여부를 확인하기 위해
        # 현재 시스템의 COM 포트 목록을 한 번만 스캔 (USB 제거 감지용)
        available_com_ports: Optional[set[str]] = None
        needs_com_scan = any(
            (d.type in ("serial", "module"))
            and d.id in self._ever_connected
            and isinstance(d.address, str)
            and d.address.upper().startswith("COM")
            for d in self._devices.values()
        )
        if needs_com_scan:
            loop = asyncio.get_event_loop()
            try:
                def _list_com_ports() -> set[str]:
                    from serial.tools import list_ports
                    return {p.device for p in list_ports.comports()}
                available_com_ports = await loop.run_in_executor(None, _list_com_ports)
            except Exception as e:
                logger.debug("COM port scan failed: %s", e)
                available_com_ports = None

        for dev in self._devices.values():
            # WinControl: 외부 연결 없음 + 디바이스 status 는 사용자 명시적 토글로만 변경.
            # attach/detach(임베드) 는 디바이스 status 에 영향을 주지 않는다.
            if dev.type == "wincontrol":
                continue
            # 사용자가 연결 끊기한 디바이스는 자동 상태 갱신 안 함
            if dev.id not in self._ever_connected:
                continue
            if dev.type == "hkmc_agent":
                hkmc = self._hkmc_conns.get(dev.id)
                if hkmc and hkmc.is_connected:
                    dev.status = "connected"
                elif dev.status != "reconnecting":
                    dev.status = "disconnected"
                continue
            if dev.type == "isap_agent":
                isap = self._isap_conns.get(dev.id)
                if isap and isap.is_connected:
                    dev.status = "connected"
                elif dev.status != "reconnecting":
                    dev.status = "disconnected"
                continue
            if dev.type == "icas_agent":
                icas = self._icas_conns.get(dev.id)
                if icas and icas.is_connected:
                    dev.status = "connected"
                elif dev.status != "reconnecting":
                    dev.status = "disconnected"
                continue
            if dev.type == "mib_agent":
                mib = self._mib_conns.get(dev.id)
                if mib and mib.is_connected:
                    dev.status = "connected"
                elif dev.status != "reconnecting":
                    dev.status = "disconnected"
                continue
            if dev.type == "webcam":
                cam = self._webcam_devs.get(dev.id)
                if cam and cam.IsConnected():
                    dev.status = "connected"
                elif dev.status != "reconnecting":
                    dev.status = "disconnected"
                continue
            if dev.type == "vision_camera":
                cam = self._vision_cams.get(dev.id)
                if cam and cam.IsConnected():
                    dev.status = "connected"
                else:
                    dev.status = "disconnected"
                continue
            if dev.type == "ssh":
                conn = self._ssh_conns.get(dev.id)
                if conn and conn.is_alive():
                    dev.status = "connected"
                else:
                    dev.status = "disconnected"
                continue
            if dev.category != "auxiliary":
                continue
            # Serial/Module: COM 포트가 시스템에서 사라진 경우(USB 제거 등) disconnected로 전환
            if dev.type in ("serial", "module"):
                if available_com_ports is None:
                    continue  # 스캔 실패 시 기존 상태 유지
                address = dev.address
                if not (isinstance(address, str) and address.upper().startswith("COM")):
                    continue  # COM 포트 기반이 아닌 모듈(네트워크 모듈 등)은 제외
                if address not in available_com_ports:
                    if dev.status not in ("disconnected", "reconnecting"):
                        logger.info(
                            "Serial/Module %s: COM port %s no longer available, marking disconnected",
                            dev.id, address,
                        )
                        # 스테일 시리얼 핸들 정리 (USB 제거 후에도 is_open이 True로 남는 경우 대비)
                        try:
                            self._close_serial_conn(dev.id)
                        except Exception as e:
                            logger.debug("Close stale serial conn %s failed: %s", dev.id, e)
                        # 모듈 싱글톤도 정리해 재연결 시 새 인스턴스를 만들도록 유도
                        module_name = dev.info.get("module", "")
                        if module_name:
                            try:
                                from .module_service import _instances, _auto_connected
                                _instances.pop(module_name, None)
                                _auto_connected.discard(module_name)
                            except Exception as e:
                                logger.debug("Clear module instance %s failed: %s", module_name, e)
                    dev.status = "disconnected"

    # 최대 연속 재연결 실패 횟수 — 초과 시 "error" 상태로 전환
    HKMC_MAX_RECONNECT_ATTEMPTS = 12  # 5초 × 12 = 60초간 실패하면 error
    ISAP_MAX_RECONNECT_ATTEMPTS = 12
    ADB_MAX_RECONNECT_ATTEMPTS = 12   # 5초 × 12 = 60초간 실패하면 error

    async def reconnect_disconnected(self, passive: bool = False) -> None:
        """끊어진 디바이스 재연결 시도 (백그라운드 태스크용, 5초 간격 호출).

        Args:
            passive: True이면 상태 확인만 수행 (adb devices 조회).
                     reconnect/connect 등 파괴적 명령은 실행하지 않음.
                     디바이스가 스스로 복귀하면 상태를 갱신하여 재생에서 사용 가능.
        """
        # 등록된 디바이스 중 연결된 적 있는 것만 대상
        targets = [d for d in list(self._devices.values()) if d.id in self._ever_connected]
        if not targets:
            return

        # ADB 디바이스가 있을 때만 ADB 호출
        has_adb = any(d.type == "adb" for d in targets)
        adb_status_map: dict = {}
        if has_adb:
            try:
                adb_devices = await self.adb.list_devices()
                adb_status_map = {d.serial: d for d in adb_devices}
            except Exception:
                pass

        for dev in list(self._devices.values()):
            # 사용자가 명시적으로 연결한 적 없는 디바이스는 자동 재연결 안 함
            if dev.id not in self._ever_connected:
                continue
            # ── ADB 디바이스 재연결 ──
            if dev.type == "adb":
                adb_serial = dev.address
                adb_dev = adb_status_map.get(adb_serial)
                current_adb_status = adb_dev.status if adb_dev else "offline"

                if current_adb_status == "device":
                    # 정상 연결 — 카운터 리셋 + 상태 갱신
                    was_offline = dev.status != "device"
                    if was_offline:
                        logger.info("ADB device back online: %s", dev.id)
                        # 재연결 시 이전 streamer 세션은 끊겼을 것 → 재생성
                        try:
                            await self.adb.close_streamer(dev.address)
                            await self.adb.ensure_streamer(dev.address)
                        except Exception as se:
                            logger.debug("ADB streamer restart on reconnect %s: %s", dev.id, se)
                        # scrcpy 미러링 백엔드도 재시작이 필요 (이전 프로세스는 끊김)
                        try:
                            await self.adb.close_scrcpy_backend(dev.address)
                        except Exception as se:
                            logger.debug("ADB scrcpy close on reconnect %s: %s", dev.id, se)
                        # 재연결 시 scrcpy 비활성 캐시 초기화 — 디바이스 환경이 바뀌었을 수 있음.
                        self.adb.clear_scrcpy_disabled(dev.address)
                    self._adb_reconnect_attempts.pop(dev.id, None)
                    dev.status = "device"
                    continue

                # passive 모드: 상태 갱신만 하고 reconnect 명령 실행 안 함
                if passive:
                    if current_adb_status != dev.status and dev.status not in ("error",):
                        dev.status = current_adb_status or "offline"
                    continue

                # error 상태면 재시도 안 함
                if dev.status == "error":
                    continue

                attempts = self._adb_reconnect_attempts.get(dev.id, 0)
                if attempts >= self.ADB_MAX_RECONNECT_ATTEMPTS:
                    dev.status = "error"
                    logger.warning("ADB reconnect give up after %d attempts: %s", attempts, dev.id)
                    continue

                dev.status = "reconnecting"
                try:
                    if ":" in adb_serial:
                        # WiFi ADB — adb connect 재시도
                        await self.adb.connect_device(adb_serial)
                    else:
                        # USB ADB — adb reconnect (USB 재인식 유도)
                        await self.adb._run(f"-s {adb_serial} reconnect")

                    # 재연결 후 상태 확인
                    check = await self.adb.list_devices()
                    found = next((d for d in check if d.serial == adb_serial), None)
                    if found and found.status == "device":
                        self._adb_reconnect_attempts.pop(dev.id, None)
                        dev.status = "device"
                        logger.info("ADB auto-reconnect success: %s (after %d attempts)", dev.id, attempts)
                    else:
                        self._adb_reconnect_attempts[dev.id] = attempts + 1
                        dev.status = current_adb_status  # connecting/offline 등 원래 상태 유지
                except Exception as e:
                    self._adb_reconnect_attempts[dev.id] = attempts + 1
                    dev.status = "offline"
                    logger.debug("ADB auto-reconnect failed (%d/%d): %s: %s",
                                 attempts + 1, self.ADB_MAX_RECONNECT_ATTEMPTS, dev.id, e)
                continue

            if dev.type == "hkmc_agent":
                hkmc = self._hkmc_conns.get(dev.id)
                if hkmc and hkmc.is_connected:
                    # 연결 정상 — 실패 카운터 리셋
                    self._hkmc_reconnect_attempts.pop(dev.id, None)
                    if dev.status != "connected":
                        dev.status = "connected"
                    continue
                port = dev.info.get("port", 0)
                if not port:
                    continue
                # error 상태(연속 실패로 포기)라도, 디바이스가 전원 ON 으로 복귀해
                # 포트가 다시 열렸으면 자동 복구한다. 장치 off→on 후 영구 미재연결되던
                # 회귀의 핵심 수정 — 예전엔 error 면 무조건 continue 로 영영 포기했음.
                if dev.status == "error":
                    if await _tcp_reachable(dev.address, port):
                        logger.info("HKMC device %s reachable again — resetting error state", dev.id)
                        self._hkmc_reconnect_attempts.pop(dev.id, None)
                        dev.status = "reconnecting"
                    else:
                        continue
                attempts = self._hkmc_reconnect_attempts.get(dev.id, 0)
                if attempts >= self.HKMC_MAX_RECONNECT_ATTEMPTS:
                    dev.status = "error"
                    logger.warning("HKMC reconnect give up after %d attempts: %s", attempts, dev.id)
                    continue
                # 재연결 락: playback의 _ensure_device_connected와 race 방지
                lock = self.get_reconnect_lock(dev.id)
                if lock.locked():
                    # 다른 경로(주로 playback)가 이미 재연결 중 — 스킵하고 다음 주기에 확인
                    continue
                async with lock:
                    # 락 획득 후 재검사: playback이 이미 성공시켰을 수 있음
                    hkmc = self._hkmc_conns.get(dev.id)
                    if hkmc and hkmc.is_connected:
                        self._hkmc_reconnect_attempts.pop(dev.id, None)
                        if dev.status != "connected":
                            dev.status = "connected"
                        continue
                    dev.status = "reconnecting"
                    try:
                        if hkmc:
                            hkmc.disconnect()
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
                            self._hkmc_conns[dev.id] = svc
                            self._hkmc_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                            logger.info("HKMC auto-reconnect success: %s (after %d attempts)", dev.id, attempts)
                        else:
                            self._hkmc_reconnect_attempts[dev.id] = attempts + 1
                            dev.status = "disconnected"
                    except Exception as e:
                        self._hkmc_reconnect_attempts[dev.id] = attempts + 1
                        dev.status = "disconnected"
                        logger.debug("HKMC auto-reconnect failed (%d/%d): %s: %s",
                                     attempts + 1, self.HKMC_MAX_RECONNECT_ATTEMPTS, dev.id, e)

            if dev.type == "hkmc5th_wide_agent":
                hkmc5 = self._hkmc5th_wide_conns.get(dev.id)
                if hkmc5 and hkmc5.is_connected:
                    self._hkmc5th_wide_reconnect_attempts.pop(dev.id, None)
                    if dev.status != "connected":
                        dev.status = "connected"
                    continue
                port = dev.info.get("port", 0)
                if not port:
                    continue
                if dev.status == "error":
                    continue
                attempts = self._hkmc5th_wide_reconnect_attempts.get(dev.id, 0)
                if attempts >= self.HKMC_MAX_RECONNECT_ATTEMPTS:
                    dev.status = "error"
                    logger.warning("HKMC5thWide reconnect give up after %d attempts: %s", attempts, dev.id)
                    continue
                lock = self.get_reconnect_lock(dev.id)
                if lock.locked():
                    continue
                async with lock:
                    hkmc5 = self._hkmc5th_wide_conns.get(dev.id)
                    if hkmc5 and hkmc5.is_connected:
                        self._hkmc5th_wide_reconnect_attempts.pop(dev.id, None)
                        if dev.status != "connected":
                            dev.status = "connected"
                        continue
                    try:
                        if hkmc5:
                            hkmc5.disconnect()
                        svc = HKMC5thWideService(dev.address, port, device_id=dev.id,
                                                 key_overrides=dev.info.get("HKMC5TH_WIDE_KEYS"),
                                                 device_model=dev.info.get("device_model", ""))
                        ok = await svc.async_connect()
                        if ok:
                            self._hkmc5th_wide_conns[dev.id] = svc
                            self._hkmc5th_wide_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                            logger.info("HKMC5thWide auto-reconnect success: %s (after %d attempts)", dev.id, attempts)
                        else:
                            self._hkmc5th_wide_reconnect_attempts[dev.id] = attempts + 1
                            dev.status = "disconnected"
                    except Exception as e:
                        self._hkmc5th_wide_reconnect_attempts[dev.id] = attempts + 1
                        dev.status = "disconnected"
                        logger.debug("HKMC5thWide auto-reconnect failed (%d/%d): %s: %s",
                                     attempts + 1, self.HKMC_MAX_RECONNECT_ATTEMPTS, dev.id, e)

            if dev.type == "isap_agent":
                isap = self._isap_conns.get(dev.id)
                if isap and isap.is_connected:
                    self._isap_reconnect_attempts.pop(dev.id, None)
                    if dev.status != "connected":
                        dev.status = "connected"
                    continue
                port = dev.info.get("port", 0)
                if not port:
                    continue
                if dev.status == "error":
                    continue
                attempts = self._isap_reconnect_attempts.get(dev.id, 0)
                if attempts >= self.ISAP_MAX_RECONNECT_ATTEMPTS:
                    dev.status = "error"
                    logger.warning("iSAP reconnect give up after %d attempts: %s", attempts, dev.id)
                    continue
                lock = self.get_reconnect_lock(dev.id)
                if lock.locked():
                    continue
                async with lock:
                    isap = self._isap_conns.get(dev.id)
                    if isap and isap.is_connected:
                        self._isap_reconnect_attempts.pop(dev.id, None)
                        if dev.status != "connected":
                            dev.status = "connected"
                        continue
                    dev.status = "reconnecting"
                    try:
                        if isap:
                            isap.disconnect()
                        svc = ISAPAgentService(dev.address, port, device_id=dev.id,
                                       key_overrides=dev.info.get("isap_keys"))
                        ok = await svc.async_connect()
                        if ok:
                            self._isap_conns[dev.id] = svc
                            self._isap_reconnect_attempts.pop(dev.id, None)
                            dev.status = "connected"
                            dev.info["agent_version"] = svc.agent_version
                            dev.info["screens"] = svc.get_info()["screens"]
                            dev.info["resolution"] = dev.info["screens"].get(
                                svc.default_screen, {"width": 1920, "height": 720}
                            )
                            logger.info("iSAP auto-reconnect success: %s", dev.id)
                        else:
                            self._isap_reconnect_attempts[dev.id] = attempts + 1
                            dev.status = "disconnected"
                    except Exception as e:
                        self._isap_reconnect_attempts[dev.id] = attempts + 1
                        dev.status = "disconnected"
                        logger.debug("iSAP auto-reconnect failed (%d/%d): %s: %s",
                                     attempts + 1, self.ISAP_MAX_RECONNECT_ATTEMPTS, dev.id, e)

    def reset_reconnect_attempts(self, device_id: str) -> None:
        """수동 재연결 시 실패 카운터 리셋 (error 상태에서 복구 가능하게)."""
        self._hkmc_reconnect_attempts.pop(device_id, None)
        self._isap_reconnect_attempts.pop(device_id, None)
        self._adb_reconnect_attempts.pop(device_id, None)
        dev = self._devices.get(device_id)
        if dev and dev.status == "error":
            dev.status = "disconnected"

    def get_reconnect_lock(self, device_id: str) -> asyncio.Lock:
        """디바이스별 재연결 락 획득 (lazy 생성).

        playback._ensure_device_connected와 monitor 루프의 재연결 블록이
        동일 디바이스에 대해 동시에 실행되지 않도록 직렬화.
        """
        lock = self._reconnect_locks.get(device_id)
        if lock is None:
            lock = asyncio.Lock()
            self._reconnect_locks[device_id] = lock
        return lock

    async def scan_serial(self) -> list[dict]:
        """Scan available serial ports."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _scan_serial_ports)

    def _com_port_exists(self, address: str) -> bool:
        """지정 COM 포트가 시스템에 물리적으로 존재하는지 확인.
        COM 주소가 아니면 True를 반환해 기존 로직 흐름 유지.
        DLL 기반 모듈(CANAT 등)은 포트가 없어도 조용히 초기화되므로 이 가드로 차단.
        """
        if not isinstance(address, str) or not address.upper().startswith("COM"):
            return True
        try:
            from serial.tools import list_ports
            return any(p.device == address for p in list_ports.comports())
        except Exception as e:
            logger.debug("COM port existence check failed for %s: %s", address, e)
            return True  # 스캔 실패 시 기존 연결 시도 흐름 유지

    async def scan_hkmc(self, ports: list[int] | None = None) -> list[dict]:
        """TCP 포트 스캔으로 LAN(192.168.*) 상의 HKMC 디바이스 탐지.
        ports가 비어있으면 스캔하지 않음.
        """
        return await _scan_hkmc_tcp(ports=ports)

    async def scan_bench(self, host: str | None = None, port: int | None = None) -> list[dict]:
        """WoohyunBench 단일 호스트 UDP 프로브. host/port 미지정 시 기본값(192.168.1.101:25000)."""
        return await _scan_woohyun_bench(host=host, port=port)

    async def scan_smartbench(self, host: str | None = None, port: int | None = None) -> list[dict]:
        """SmartBench 장비 탐지. host/port 미지정 시 기본값(192.167.0.5:8000) 사용."""
        return await _scan_smartbench(host=host, port=port)

    async def scan_scar(
        self,
        host: str | None = None,
        port: int | None = None,
        container: str | None = None,
    ) -> list[dict]:
        """SCAR (Linux 전용) 자동 탐지 — REST API + docker container 양쪽 프로브."""
        return await _scan_scar(host=host, port=port, container=container)

    async def scan_radmoon(self, bridge: str | None = None) -> list[dict]:
        """radmoon (TH host의 cvd-ebr bridge, Linux 전용) 탐지.

        bridge 인자가 주어지면 그 이름의 bridge 를 찾고, 없으면 'cvd-ebr' 기본.
        결과는 bridge 가 존재할 때 1행, 현재 IP / member 인터페이스 정보 포함.
        """
        return await _scan_radmoon(bridge=bridge)

    async def scan_dlt(self, ports: list[int] | None = None) -> list[dict]:
        """TCP 포트 스캔으로 LAN(192.168.*) 상의 DLT 데몬 탐지."""
        return await _scan_dlt_tcp(ports=ports)

    async def scan_isap(self, ports: list[int] | None = None) -> list[dict]:
        """TCP 포트 스캔으로 LAN(192.168.*) 상의 iSAP Agent 탐지.
        단순 TCP open 체크 (iSAP은 핸드셰이크를 푸시하지 않는 경우가 있어 검증 생략).
        """
        if not ports:
            logger.info("iSAP scan skipped: no ports configured")
            return []
        candidate_ips = _collect_candidate_ips_192()
        if not candidate_ips:
            return []
        import asyncio as _a
        semaphore = _a.Semaphore(100)
        tasks = [
            _probe_tcp_port(ip, port, 0.3, semaphore)
            for ip in candidate_ips
            for port in ports
        ]
        results = await _a.gather(*tasks)
        found = [r for r in results if r is not None]
        seen: set[str] = set()
        deduped: list[dict] = []
        for r in found:
            if r["ip"] not in seen:
                seen.add(r["ip"])
                deduped.append(r)
                logger.info("iSAP scan: found open port at %s:%d", r["ip"], r["port"])
        return deduped

    async def scan_ssh(self, port: int = 22) -> list[dict]:
        """TCP 포트 스캔으로 LAN 상의 SSH 호스트 탐지."""
        return await scan_tcp_port(port)

    async def scan_icas(self, port: int = 22) -> list[dict]:
        """LAN 상의 ICAS 후보 호스트 탐지 (SSH 포트 기반 단순 탐지)."""
        return await scan_tcp_port(port)

    async def scan_udp_port(self, port: int) -> list[dict]:
        """LAN에서 특정 UDP 포트 응답 호스트 탐지."""
        return await _scan_udp_port(port)

    async def scan_vision_cameras(self) -> list[dict]:
        """GigE Vision 카메라 스캔 (Harvester 기반)."""
        loop = asyncio.get_event_loop()
        try:
            from ..plugins.VisionCameraClient import scan_vision_cameras
            return await loop.run_in_executor(None, scan_vision_cameras)
        except Exception as e:
            logger.debug("VisionCamera scan failed: %s", e)
            return []

    async def force_ip_camera(self, mac: str, ip: str, subnet: str, gateway: str) -> str:
        """GigE Vision 카메라 ForceIP (vmbpy 기반)."""
        loop = asyncio.get_event_loop()
        try:
            from ..plugins.VisionCameraClient import force_ip_camera
            return await loop.run_in_executor(None, force_ip_camera, mac, ip, subnet, gateway)
        except Exception as e:
            return f"ForceIP error: {e}"

    async def add_serial_device(self, port: str, baudrate: int = 115200, name: str = "", category: str = "auxiliary", device_id: str = "", module: str = "", connect_type: str = "") -> ManagedDevice:
        """시리얼 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행).

        module이 지정되면 device_id prefix로 사용 (예: CANAT_1). 아니면 "Serial_N".
        """
        final_id = device_id or self._generate_device_id("serial", module_name=module)
        info: dict = {"baudrate": baudrate}
        if module:
            info["module"] = module
            info["connect_type"] = connect_type or "serial"
        dev = ManagedDevice(
            id=final_id,
            type="serial",
            category=category,
            address=port,
            status="disconnected",
            name=name or final_id,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()
        return dev

    async def add_ssh_device(self, host: str, port: int, username: str, password: str,
                             category: str = "auxiliary", name: str = "", device_id: str = "",
                             key_file_path: str = "") -> ManagedDevice:
        """SSH 디바이스 등록 + 즉시 인증 시도. 실패 시 RuntimeError.

        Args:
            host: SSH 호스트 (IP 또는 hostname)
            port: SSH 포트 (보통 22)
            username, password: 자격증명
            category: "primary" | "auxiliary"
            name: 표시 이름 (없으면 자동 생성)
            device_id: 사용자 지정 ID (없으면 자동 생성)
            key_file_path: SSH 키 파일 경로 (선택)
        """
        final_id = device_id or self._generate_device_id("ssh")
        display_name = name or f"{username}@{host}"

        # 1. 먼저 SSH 연결 시도 (실패 시 디바이스 등록 안 함)
        loop = asyncio.get_event_loop()
        ssh_conn = SSHConnection(host=host, port=port, username=username,
                                 password=password, key_file_path=key_file_path or None)
        try:
            await loop.run_in_executor(None, ssh_conn.connect)
        except Exception as e:
            raise RuntimeError(f"SSH connect failed: {e}") from e

        info = {
            "host": host,
            "port": port,
            "username": username,
            "password": password,  # 평문 저장 (auxiliary_devices.json)
            "key_file_path": key_file_path,
            "module": "SSHManager",  # 모듈 스텝 추가에서 자동으로 SSHManager 함수 사용
            "connect_type": "ssh",
        }
        dev = ManagedDevice(
            id=final_id,
            type="ssh",
            category=category,
            address=host,
            status="connected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._ssh_conns[final_id] = ssh_conn
        self._ever_connected.add(final_id)
        self._save_auxiliary_devices()
        logger.info("SSH device added: %s (%s@%s:%d)", final_id, username, host, port)
        return dev

    def get_ssh_conn(self, device_id: str) -> Optional[SSHConnection]:
        """등록된 SSH 디바이스의 SSHConnection 반환. 없으면 None."""
        return self._ssh_conns.get(device_id)

    def _close_ssh_conn(self, device_id: str) -> None:
        """SSH 연결 종료 + 캐시에서 제거."""
        conn = self._ssh_conns.pop(device_id, None)
        if conn:
            try:
                conn.disconnect()
            except Exception as e:
                logger.warning("SSH disconnect error %s: %s", device_id, e)

    async def add_module_device(self, address: str, module: str, connect_type: str = "none",
                               name: str = "", extra_fields: dict | None = None, device_id: str = "") -> ManagedDevice:
        """모듈 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행).

        connect_type="none" 모듈(TH/SCAR 등)은 인스턴스가 모듈명 기준 1개로 공유(_instances[module])
        되므로 디바이스도 1개만 의미가 있다. 같은 모듈의 none 디바이스가 이미 있으면 새로 만들지 않고
        그것을 재사용(설정 갱신 + 캐시 인스턴스 무효화)해서 중복 등록(TH_1/TH_2)을 막는다.
        — 중복이 생기면 startup/재연결 시 두 디바이스가 한 인스턴스를 두고 충돌해
          "connected 인데 동작 안 함" / "th 디렉토리 못 찾음" 이 발생했다.
        """
        if connect_type == "none" and not device_id:
            dup = next(
                (d for d in self._devices.values()
                 if d.type == "module"
                 and d.info.get("module") == module
                 and (d.info.get("connect_type") or "none") == "none"),
                None,
            )
            if dup is not None:
                if extra_fields:
                    dup.info.update(extra_fields)
                if name:
                    dup.name = name
                if address:
                    dup.address = address
                dup.status = "disconnected"
                self._save_auxiliary_devices()
                # 설정(th_home 등)이 바뀌었을 수 있으니 공유 인스턴스를 버려 재연결 시 새 kwargs 로 재생성.
                try:
                    from .module_service import reset_instance
                    reset_instance(module)
                except Exception as e:
                    logger.debug("reset_instance(%s) on dedup failed: %s", module, e)
                logger.info("Reusing existing '%s' module device %s (dedup, connect_type=none)", module, dup.id)
                return dup

        final_id = device_id or self._generate_device_id("module", module)
        display_name = name or (f"{module} ({address})" if address else module)
        info: dict = {"module": module, "connect_type": connect_type}
        if extra_fields:
            info.update(extra_fields)
        dev = ManagedDevice(
            id=final_id,
            type="module",
            category="auxiliary",
            address=address,
            status="disconnected",
            name=display_name,
            info=info,
        )
        self._devices[final_id] = dev
        self._save_auxiliary_devices()

        return dev

    async def add_adb_wifi(self, address: str) -> ManagedDevice:
        """ADB WiFi 디바이스 등록만 (연결은 connect_device_by_id로 별도 수행)."""
        if address in self._devices:
            return self._devices[address]
        dev = ManagedDevice(
            id=address,
            type="adb",
            category="primary",
            address=address,
            status="disconnected",
            name=address,
            info={},
        )
        self._devices[address] = dev
        self._save_auxiliary_devices()
        return dev

    def swap_device_ids(self, id_a: str, id_b: str) -> None:
        """두 디바이스의 ID를 서로 교체합니다."""
        if self.is_protected_device(id_a) or self.is_protected_device(id_b):
            raise ValueError("Protected system device cannot be renamed or swapped")
        dev_a = self._devices.pop(id_a, None)
        dev_b = self._devices.pop(id_b, None)
        if not dev_a or not dev_b:
            raise ValueError(f"Device {id_a} or {id_b} not found")
        dev_a.id = id_b
        dev_b.id = id_a
        self._devices[id_b] = dev_a
        self._devices[id_a] = dev_b
        # 연결 객체도 교체
        for store in (self._serial_conns, self._hkmc_conns, self._isap_conns, self._vision_cams, self._webcam_devs):
            a_val = store.pop(id_a, None)
            b_val = store.pop(id_b, None)
            if a_val is not None:
                store[id_b] = a_val
            if b_val is not None:
                store[id_a] = b_val
        self._save_auxiliary_devices()
        logger.info("Device IDs swapped: %s <-> %s", id_a, id_b)

    def rename_device(self, old_id: str, new_id: str) -> None:
        """디바이스 ID를 변경합니다."""
        if self.is_protected_device(old_id):
            raise ValueError(f"Device '{old_id}' is a protected system default and cannot be renamed")
        dev = self._devices.pop(old_id, None)
        if not dev:
            raise ValueError(f"Device {old_id} not found")
        dev.id = new_id
        self._devices[new_id] = dev
        # 시리얼 연결도 이관
        if old_id in self._serial_conns:
            self._serial_conns[new_id] = self._serial_conns.pop(old_id)
        if old_id in self._hkmc_conns:
            self._hkmc_conns[new_id] = self._hkmc_conns.pop(old_id)
        if old_id in self._isap_conns:
            self._isap_conns[new_id] = self._isap_conns.pop(old_id)
        if old_id in self._vision_cams:
            self._vision_cams[new_id] = self._vision_cams.pop(old_id)
        if old_id in self._webcam_devs:
            self._webcam_devs[new_id] = self._webcam_devs.pop(old_id)
        if old_id in self._ever_connected:
            self._ever_connected.discard(old_id)
            self._ever_connected.add(new_id)
        self._save_auxiliary_devices()
        logger.info("Device renamed: %s → %s", old_id, new_id)

    def reorder_devices(self, prefix: str, ordered_ids: list[str]) -> None:
        """그룹 내 디바이스 순서를 변경하고 ID 번호를 재할당합니다.
        예: prefix="Android", ordered_ids=["Android_2","Android_1"]
        → Android_2→Android_1, Android_1→Android_2

        보호된 시스템 기본 디바이스(Common, WinControl)는 ID 고정 — 번호 재할당에서 제외.
        WinControl 은 UI 상 Common 그룹에 표시되지만 ID 는 'WinControl' 로 유지되어야 함.
        """
        # 1) 유효성 검증
        for did in ordered_ids:
            if did not in self._devices:
                raise ValueError(f"Device {did} not found")

        # 2) 새 ID 매핑 생성 — 보호 디바이스는 건너뛰고 일반 디바이스만 1부터 재번호.
        remap: dict[str, str] = {}  # old_id → new_id
        renum_idx = 0
        for old_id in ordered_ids:
            if self.is_protected_device(old_id):
                continue  # ID 변경 없음 — 그룹 내 위치만 보존
            renum_idx += 1
            new_id = f"{prefix}_{renum_idx}"
            if old_id != new_id:
                remap[old_id] = new_id

        if not remap:
            return  # 변경 없음

        # 3) 충돌 방지: 임시 ID로 이동
        temp_map: dict[str, str] = {}  # temp_id → new_id
        stores = (self._serial_conns, self._hkmc_conns, self._isap_conns, self._vision_cams, self._webcam_devs)
        for old_id, new_id in remap.items():
            temp_id = f"__reorder_{old_id}__"
            dev = self._devices.pop(old_id)
            dev.id = temp_id
            self._devices[temp_id] = dev
            temp_map[temp_id] = new_id
            for store in stores:
                if old_id in store:
                    store[temp_id] = store.pop(old_id)
            if old_id in self._ever_connected:
                self._ever_connected.discard(old_id)
                self._ever_connected.add(temp_id)

        # 4) 최종 ID 적용
        for temp_id, new_id in temp_map.items():
            dev = self._devices.pop(temp_id)
            dev.id = new_id
            self._devices[new_id] = dev
            for store in stores:
                if temp_id in store:
                    store[new_id] = store.pop(temp_id)
            if temp_id in self._ever_connected:
                self._ever_connected.discard(temp_id)
                self._ever_connected.add(new_id)

        self._save_auxiliary_devices()
        logger.info("Devices reordered [%s]: %s", prefix, remap)

    async def remove_device(self, device_id: str) -> str:
        """Remove a device from managed list."""
        dev = self.get_device(device_id)
        if not dev:
            return f"Device {device_id} not found"

        if self.is_protected_device(dev.id):
            raise ValueError(f"Device '{dev.id}' is a protected system default and cannot be removed")

        # 장기 screencap 세션 정리 (ADB 디바이스 제거 시)
        if dev.type == "adb":
            try:
                await self.adb.close_streamer(dev.address)
            except Exception as se:
                logger.debug("ADB streamer close on remove failed for %s: %s", dev.id, se)
            try:
                await self.adb.close_scrcpy_backend(dev.address)
            except Exception as se:
                logger.debug("ADB scrcpy close on remove failed for %s: %s", dev.id, se)
            self.adb.clear_scrcpy_disabled(dev.address)

        if dev.type == "adb" and ":" in dev.address:
            result = await self.adb.disconnect_device(dev.address)
        else:
            result = f"Removed {dev.id}"

        self._close_serial_conn(dev.id)
        # Close HKMC connection if applicable
        hkmc = self._hkmc_conns.pop(dev.id, None)
        if hkmc:
            hkmc.disconnect()
        # Close HKMC5thWide connection if applicable
        hkmc5 = self._hkmc5th_wide_conns.pop(dev.id, None)
        if hkmc5:
            hkmc5.disconnect()
        # Close iSAP Agent connection if applicable
        isap = self._isap_conns.pop(dev.id, None)
        if isap:
            isap.disconnect()
        # Close VisionCamera connection if applicable
        cam = self._vision_cams.pop(dev.id, None)
        if cam:
            try:
                cam.Disconnect()
            except Exception:
                pass
        # Close Webcam device connection if applicable
        wcam = self._webcam_devs.pop(dev.id, None)
        if wcam:
            try:
                wcam.Disconnect()
            except Exception:
                pass
        # Close SSH connection if applicable
        self._close_ssh_conn(dev.id)
        # 모듈 인스턴스 정리: 화이트리스트 module 은 teardown(SCAR: netns 복원) + pop, 나머지는 pop 만.
        module_name = dev.info.get("module")
        if module_name:
            from .module_service import (MODULES_WITH_DISCONNECT_TEARDOWN,
                                         disconnect_instance, reset_instance)
            if module_name in MODULES_WITH_DISCONNECT_TEARDOWN:
                try:
                    msg = disconnect_instance(module_name)
                    if msg:
                        logger.info("module '%s' remove teardown: %s", module_name, msg)
                except Exception as e:
                    logger.debug("module teardown failed for %s: %s", module_name, e)
            else:
                reset_instance(module_name)
        self._devices.pop(dev.id, None)
        self._ever_connected.discard(dev.id)
        self._save_auxiliary_devices()
        return result

    def list_all(self) -> list[ManagedDevice]:
        """List all managed devices."""
        return list(self._devices.values())

    def list_primary(self) -> list[ManagedDevice]:
        """List primary devices (screen-controllable: ADB, Linux, etc.)."""
        return [d for d in self._devices.values() if d.category == "primary"]

    def list_auxiliary(self) -> list[ManagedDevice]:
        """List auxiliary devices (serial, USB, etc.)."""
        return [d for d in self._devices.values() if d.category == "auxiliary"]

    def get_device(self, device_id: str) -> Optional[ManagedDevice]:
        """Look up device by id first, then by address as fallback."""
        dev = self._devices.get(device_id)
        if dev:
            return dev
        # Fallback: search by address (real serial/port)
        for d in self._devices.values():
            if d.address == device_id:
                return d
        return None

    def _get_serial_conn(self, device_id: str):
        """Get or create a persistent serial connection (no DTR reset on reuse)."""
        import serial as pyserial
        dev = self.get_device(device_id)
        if not dev:
            raise ValueError(f"Device {device_id} not found")
        conn = self._serial_conns.get(device_id)
        if conn and conn.is_open:
            return conn
        port = dev.address
        baudrate = dev.info.get("baudrate", 115200)
        conn = pyserial.Serial(port, baudrate=baudrate, timeout=1)
        # Wait for Arduino bootloader + setup() to finish
        import time
        time.sleep(3)
        try:
            # Drain all startup garbage (null bytes, boot messages, etc.)
            conn.reset_input_buffer()
            conn.reset_output_buffer()
        except Exception as e:
            conn.close()
            raise RuntimeError(f"Serial drain failed for {port}: {e}")
        self._serial_conns[device_id] = conn
        logger.info("Serial port opened and drained: %s (%s @ %d)", device_id, port, baudrate)
        return conn

    def get_serial_conn(self, device_id: str):
        """Get an existing open serial connection for a device (by id or address)."""
        # 1) device_id로 직접 검색
        conn = self._serial_conns.get(device_id)
        if conn and conn.is_open:
            return conn
        # 2) device_id가 address(COM포트)인 경우 — 해당 address를 가진 디바이스의 연결 검색
        for did, dev in self._devices.items():
            if dev.address == device_id and did in self._serial_conns:
                conn = self._serial_conns[did]
                if conn and conn.is_open:
                    return conn
        return None

    def _close_serial_conn(self, device_id: str) -> None:
        """Close a persistent serial connection."""
        conn = self._serial_conns.pop(device_id, None)
        if conn and conn.is_open:
            conn.close()

    async def open_all_serial_connections(self) -> None:
        """Open persistent serial/HKMC/module connections for all registered devices."""
        loop = asyncio.get_event_loop()
        for dev in list(self._devices.values()):
            if dev.type == "serial":
                try:
                    await loop.run_in_executor(None, self._get_serial_conn, dev.id)
                    dev.status = "connected"
                    logger.info("Serial connection opened: %s (%s)", dev.id, dev.address)
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open serial %s (%s): %s", dev.id, dev.address, e)
            elif dev.type == "hkmc_agent":
                port = dev.info.get("port", 0)
                if not port:
                    continue
                try:
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
                        self._hkmc_conns[dev.id] = svc
                        dev.status = "connected"
                        dev.info["agent_version"] = svc.agent_version
                        dev.info["screens"] = svc.get_info()["screens"]
                        dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                        logger.info("HKMC connection opened: %s (%s:%d)", dev.id, dev.address, port)
                    else:
                        dev.status = "disconnected"
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open HKMC %s (%s:%d): %s", dev.id, dev.address, port, e)
            elif dev.type == "hkmc5th_wide_agent":
                port = dev.info.get("port", 0)
                if not port:
                    continue
                try:
                    svc = HKMC5thWideService(dev.address, port, device_id=dev.id,
                                             key_overrides=dev.info.get("HKMC5TH_WIDE_KEYS"),
                                             device_model=dev.info.get("device_model", ""))
                    ok = await svc.async_connect()
                    if ok:
                        self._hkmc5th_wide_conns[dev.id] = svc
                        dev.status = "connected"
                        dev.info["agent_version"] = svc.agent_version
                        dev.info["screens"] = svc.get_info()["screens"]
                        dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                        logger.info("HKMC5thWide connection opened: %s (%s:%d)", dev.id, dev.address, port)
                    else:
                        dev.status = "disconnected"
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open HKMC5thWide %s (%s:%d): %s", dev.id, dev.address, port, e)
            elif dev.type == "isap_agent":
                port = dev.info.get("port", 0)
                if not port:
                    continue
                try:
                    svc = ISAPAgentService(dev.address, port, device_id=dev.id,
                                       key_overrides=dev.info.get("isap_keys"))
                    ok = await svc.async_connect()
                    if ok:
                        self._isap_conns[dev.id] = svc
                        dev.status = "connected"
                        dev.info["agent_version"] = svc.agent_version
                        dev.info["screens"] = svc.get_info()["screens"]
                        dev.info["resolution"] = dev.info["screens"].get(
                            svc.default_screen, {"width": 1920, "height": 720}
                        )
                        logger.info("iSAP connection opened: %s (%s:%d)", dev.id, dev.address, port)
                    else:
                        dev.status = "disconnected"
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open iSAP %s (%s:%d): %s", dev.id, dev.address, port, e)
            elif dev.type == "module":
                # 모듈 디바이스: 서버 시작 시 인스턴스 생성 + 연결 시도
                module_name = dev.info.get("module", "")
                if not module_name:
                    continue
                # 재시작 시 자동 연결을 건너뛸 모듈(SCAR/TH) — 등록은 유지, status=disconnected 로 두고
                # 사용자가 수동 연결. (자동 연결이 netns/cvd-ebr/cuttlefish 에 부작용을 일으켜서)
                try:
                    from .module_service import MODULES_NO_STARTUP_AUTOCONNECT
                    if module_name in MODULES_NO_STARTUP_AUTOCONNECT:
                        dev.status = "disconnected"
                        logger.info("Module %s (%s): startup auto-connect skipped (manual connect required)",
                                    dev.id, module_name)
                        continue
                except Exception as e:
                    logger.debug("startup auto-connect skip check for %s failed: %s", module_name, e)
                # 가상 모듈 (plugins/*.py(.pyd)에도 lge.auto에도 클래스 없음) — 실제 인스턴스 생성 불필요.
                # 예: "OCR" — module_service가 직접 처리(playback_service에서 가상 핸들러).
                # _ensure_default_ocr_device가 미리 설정한 status="connected"를 유지하기 위해 skip.
                # 일반 모듈은 _import_module_class가 클래스를 찾아 반환 → 아래 _get_instance 경로로 진입.
                try:
                    from .module_service import _import_module_class
                    if _import_module_class(module_name) is None:
                        logger.info("Module %s is virtual (no class file); keeping status=%s",
                                    dev.id, dev.status)
                        continue
                except Exception as e:
                    logger.debug("Virtual module check for %s failed (continuing with init): %s",
                                 module_name, e)
                # 시리얼 기반 모듈: COM 포트 존재 여부 선검증 (DLL 모듈 오탐 차단)
                if dev.info.get("connect_type") == "serial":
                    if not await loop.run_in_executor(None, self._com_port_exists, dev.address):
                        dev.status = "disconnected"
                        logger.warning("Module %s skipped: COM port %s not present", dev.id, dev.address)
                        continue
                try:
                    from .module_service import _get_instance, _is_connected
                    from ..routers.device import _build_constructor_kwargs
                    ctor_kwargs = _build_constructor_kwargs(dev)
                    # device_manager가 이미 같은 포트로 열어둔 시리얼 연결이 있으면 전달
                    shared_conn = self.get_serial_conn(dev.id)
                    instance = await loop.run_in_executor(
                        None, functools.partial(_get_instance, module_name, ctor_kwargs, shared_conn),
                    )
                    if _is_connected(instance):
                        dev.status = "connected"
                        logger.info("Module connection opened: %s (%s on %s)", dev.id, module_name, dev.address)
                    else:
                        dev.status = "disconnected"
                        logger.warning("Module instance created but not connected: %s (%s)", dev.id, module_name)
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to init module %s (%s): %s", dev.id, module_name, e)
            elif dev.type == "vision_camera":
                mac = dev.info.get("mac", "")
                if not mac:
                    continue
                try:
                    from ..plugins.VisionCamera import VisionCamera
                    cam = VisionCamera(
                        mac=mac,
                        model=dev.info.get("model", ""),
                        serial=dev.info.get("serial_number", ""),
                        ip=dev.info.get("ip", ""),
                        subnetmask=dev.info.get("subnetmask", "255.255.0.0"),
                    )
                    result = await loop.run_in_executor(None, cam.Connect)
                    self._vision_cams[dev.id] = cam
                    dev.status = "connected"
                    logger.info("VisionCamera connection opened: %s (%s)", dev.id, mac)
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open VisionCamera %s (%s): %s", dev.id, mac, e)
            elif dev.type == "webcam":
                try:
                    from ..plugins.WebcamDevice import WebcamDevice
                    cam = WebcamDevice(
                        device_index=int(dev.info.get("device_index", 0)),
                        width=int(dev.info.get("width", 0)),
                        height=int(dev.info.get("height", 0)),
                    )
                    await loop.run_in_executor(None, cam.Connect)
                    self._webcam_devs[dev.id] = cam
                    dev.status = "connected"
                    logger.info("Webcam connection opened: %s (index=%s)", dev.id, dev.info.get("device_index"))
                except Exception as e:
                    dev.status = "disconnected"
                    logger.warning("Failed to open Webcam %s: %s", dev.id, e)

    async def connect_device_by_id(self, device_id: str) -> str:
        """등록된 디바이스 1개를 연결. 결과 메시지 반환."""
        dev = self._devices.get(device_id)
        if not dev:
            return f"Device {device_id} not found"
        loop = asyncio.get_event_loop()

        # 자동 마이그레이션 — 옛 카탈로그에서 hkmc_agent로 등록된 Gen5 디바이스를 5th_wide로 보정.
        # HKMC6th와 5th_wide는 통신 프로토콜이 달라 잘못된 service로 연결하면 무응답.
        # device_model 필드가 비어있을 수도 있어 device_id(자동 prefix에 모델명 포함)도 함께 체크.
        if dev.type == "hkmc_agent":
            _model_str = (dev.info.get("device_model") or "").lower()
            _id_str = (dev.id or "").lower()
            if "gen5" in _model_str or "gen5" in _id_str:
                logger.info(
                    "Auto-migrating %s: hkmc_agent → hkmc5th_wide_agent (Gen5 detected: model=%r id=%r)",
                    dev.id, dev.info.get("device_model"), dev.id,
                )
                dev.type = "hkmc5th_wide_agent"
                # device_model 누락 시 채워 넣기 — 이후 reconnect/save 시 일관성 유지
                if not dev.info.get("device_model"):
                    dev.info["device_model"] = dev.id  # 최소한 ID 그대로 채움 (사람이 보면 식별 가능)
                # 옛 hkmc_agent 연결이 남아있을 수 있어 정리
                old = self._hkmc_conns.pop(dev.id, None)
                if old:
                    try:
                        old.disconnect()
                    except Exception:
                        pass
                self._save_auxiliary_devices()

        def _mark_connected():
            self._ever_connected.add(device_id)

        if dev.type == "wincontrol":
            # 별도 외부 연결 없음 — 서비스 가용 여부만 점검 후 status 전환.
            if not self._wincontrol.is_available():
                # OS 에 따라 누락된 의존성 안내 메시지가 다름 (Win=pywin32, Linux=python-xlib/X11 display).
                default_err = ("python-xlib unavailable or X server not reachable"
                               if _WIN_CTRL_IS_LINUX else "pywin32 not installed")
                err = self._wincontrol.import_error() or default_err
                dev.status = "disconnected"
                return f"{_WIN_CTRL_DISPLAY_NAME} unavailable: {err}"
            dev.status = "connected"
            _mark_connected()
            return f"{_WIN_CTRL_DISPLAY_NAME} connected: {dev.id}"

        if dev.type == "serial":
            module_name = dev.info.get("module", "")

            # COM 포트 존재 여부 선검증 — DLL 기반 모듈(CANAT 등)은 포트가 없어도
            # init()이 조용히 성공하고 hdll이 설정되므로 _is_connected가 오탐하는 문제 차단
            if not await loop.run_in_executor(None, self._com_port_exists, dev.address):
                dev.status = "disconnected"
                logger.info("Serial connect skipped: %s — COM port %s not present", dev.id, dev.address)
                return f"Serial connect failed: {dev.id} — COM port {dev.address} not available"

            # DLL 기반 모듈(CANAT 등)은 자체적으로 COM 포트를 관리하므로
            # pyserial로 포트를 열면 충돌 발생 — 모듈 init만 수행
            if module_name:
                try:
                    from .module_service import _get_instance, _is_connected
                    from ..routers.device import _build_constructor_kwargs
                    ctor_kwargs = _build_constructor_kwargs(dev)
                    instance = await loop.run_in_executor(
                        None, functools.partial(_get_instance, module_name, ctor_kwargs, None),
                    )
                    if _is_connected(instance):
                        dev.status = "connected"
                        _mark_connected()
                        return f"Module connected: {dev.id} ({dev.address}) + {module_name} init OK"
                    else:
                        # DLL이 없는 모듈은 shared_serial_conn 방식으로 폴백
                        try:
                            await loop.run_in_executor(None, self._get_serial_conn, dev.id)
                            shared_conn = self.get_serial_conn(dev.id)
                            instance = await loop.run_in_executor(
                                None, functools.partial(_get_instance, module_name, ctor_kwargs, shared_conn),
                            )
                            dev.status = "connected"
                            _mark_connected()
                            return f"Serial connected: {dev.id} ({dev.address}) + {module_name}"
                        except Exception as e2:
                            dev.status = "disconnected"
                            return f"Module connect failed: {dev.id} — {e2}"
                except Exception as e:
                    logger.warning("Module init failed for %s on %s: %s", module_name, dev.id, e)
                    dev.status = "disconnected"
                    return f"Module connect failed: {dev.id} ({module_name}) — {e}"

            # 순수 시리얼 디바이스 (모듈 없음)
            try:
                await loop.run_in_executor(None, self._get_serial_conn, dev.id)
                dev.status = "connected"
                _mark_connected()
                return f"Serial connected: {dev.id} ({dev.address})"
            except Exception as e:
                dev.status = "disconnected"
                return f"Serial connect failed: {dev.id} — {e}"

        elif dev.type == "hkmc_agent":
            port = dev.info.get("port", 0)
            if not port:
                return f"HKMC {dev.id}: no port configured"
            try:
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
                    self._hkmc_conns[dev.id] = svc
                    dev.status = "connected"
                    _mark_connected()
                    dev.info["agent_version"] = svc.agent_version
                    dev.info["screens"] = svc.get_info()["screens"]
                    dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                    return f"HKMC connected: {dev.id} ({dev.address}:{port})"
                else:
                    dev.status = "disconnected"
                    return f"HKMC connect failed: {dev.id}"
            except Exception as e:
                dev.status = "disconnected"
                return f"HKMC connect failed: {dev.id} — {e}"

        elif dev.type == "hkmc5th_wide_agent":
            port = dev.info.get("port", 0)
            if not port:
                return f"HKMC5thWide {dev.id}: no port configured"
            try:
                svc = HKMC5thWideService(dev.address, port, device_id=dev.id,
                                         key_overrides=dev.info.get("HKMC5TH_WIDE_KEYS"),
                                         device_model=dev.info.get("device_model", ""))
                ok = await svc.async_connect()
                if ok:
                    self._hkmc5th_wide_conns[dev.id] = svc
                    dev.status = "connected"
                    _mark_connected()
                    dev.info["agent_version"] = svc.agent_version
                    dev.info["screens"] = svc.get_info()["screens"]
                    dev.info["resolution"] = dev.info["screens"].get("front_center", {"width": 1920, "height": 720})
                    return f"HKMC5thWide connected: {dev.id} ({dev.address}:{port})"
                else:
                    dev.status = "disconnected"
                    return f"HKMC5thWide connect failed: {dev.id}"
            except Exception as e:
                dev.status = "disconnected"
                return f"HKMC5thWide connect failed: {dev.id} — {e}"

        elif dev.type == "isap_agent":
            port = dev.info.get("port", 0)
            if not port:
                return f"iSAP {dev.id}: no port configured"
            try:
                svc = ISAPAgentService(dev.address, port, device_id=dev.id,
                                       key_overrides=dev.info.get("isap_keys"))
                ok = await svc.async_connect()
                if ok:
                    self._isap_conns[dev.id] = svc
                    dev.status = "connected"
                    _mark_connected()
                    dev.info["agent_version"] = svc.agent_version
                    dev.info["screens"] = svc.get_info()["screens"]
                    dev.info["resolution"] = dev.info["screens"].get(
                        svc.default_screen, {"width": 1920, "height": 720}
                    )
                    return f"iSAP connected: {dev.id} ({dev.address}:{port})"
                else:
                    dev.status = "disconnected"
                    return f"iSAP connect failed: {dev.id}"
            except Exception as e:
                dev.status = "disconnected"
                return f"iSAP connect failed: {dev.id} — {e}"

        elif dev.type == "icas_agent":
            port = int(dev.info.get("port", 22) or 22)
            username = dev.info.get("username", "root") or "root"
            password = dev.info.get("password", "") or ""
            # ICAS3 변종 식별 — device_model에 "ICAS3" 포함이면 ksend frame/encoding 변경 + 해상도 기본 2240x1260
            _dm_upper = (dev.info.get("device_model") or "").upper()
            icas_variant = "icas3" if "ICAS3" in _dm_upper else "icas"
            # resolution_str(원본 "WxH") 우선, 없으면 resolution(dict) → "WxH" 복원, 모두 없으면 variant별 기본값
            res_str = dev.info.get("resolution_str")
            if not res_str:
                res_val = dev.info.get("resolution")
                if isinstance(res_val, dict) and "width" in res_val and "height" in res_val:
                    res_str = f"{res_val['width']}x{res_val['height']}"
                elif isinstance(res_val, str):
                    res_str = res_val
                else:
                    res_str = "2240x1260" if icas_variant == "icas3" else "1560x700"
            # market 추론: info.market > device_model 키워드 > EU 기본
            market = (dev.info.get("market") or "").strip().upper()
            if not market:
                dm_val = (dev.info.get("device_model") or "").upper()
                for _m in ("EU", "NAR", "CN", "GP"):
                    if _m in dm_val:
                        market = _m
                        break
            if not market:
                market = "EU"
            dev.info["market"] = market  # 정규화 후 저장

            # 레거시 private_server_ip 치환: EU/NAR/CN인데 IPv4 "192.168.0.2"로 남아있는 경우
            # (이전 버전 기본값) → 빈 값으로 바꿔 market 기본(IPv6)을 쓰게 함
            private_ip = dev.info.get("private_server_ip", "") or ""
            if market in ("EU", "NAR", "CN") and private_ip == "192.168.0.2":
                private_ip = ""
                dev.info["private_server_ip"] = ""
            try:
                svc = ICASAgentService(
                    dev.address, port=port, device_id=dev.id,
                    username=username, password=password, resolution=res_str,
                    # private_server_ip는 빈 문자열이면 market 기본값 사용
                    private_server_ip=private_ip,
                    private_server_password=dev.info.get("private_server_password", "") or "",
                    iid_display=dev.info.get("iid_display", "10") or "10",
                    hud_display=dev.info.get("hud_display", "11") or "11",
                    market=market,
                    variant=icas_variant,
                    key_overrides=dev.info.get("icas_keys"),
                )
                ok = await svc.async_connect()
                if ok:
                    self._icas_conns[dev.id] = svc
                    dev.status = "connected"
                    _mark_connected()
                    dev.info["agent_version"] = svc.agent_version
                    dev.info["screens"] = svc.get_info()["screens"]
                    # 프론트엔드용 dict 정규화 (HKMC/iSAP와 동일 스키마)
                    dev.info["resolution"] = dev.info["screens"].get(
                        svc.default_screen, {"width": 1560, "height": 700}
                    )
                    dev.info["resolution_str"] = res_str
                    return f"ICAS connected: {dev.id} ({dev.address}:{port})"
                else:
                    dev.status = "disconnected"
                    return f"ICAS connect failed: {dev.id}"
            except Exception as e:
                dev.status = "disconnected"
                return f"ICAS connect failed: {dev.id} — {e}"

        elif dev.type == "mib_agent":
            port = int(dev.info.get("port", 22) or 22)
            username = dev.info.get("username", "root") or "root"
            password = dev.info.get("password", "") or ""
            # resolution_str(원본 "WxH") 우선, 없으면 resolution(dict) → "WxH" 복원, 모두 없으면 기본값
            res_str = dev.info.get("resolution_str")
            if not res_str:
                res_val = dev.info.get("resolution")
                if isinstance(res_val, dict) and "width" in res_val and "height" in res_val:
                    res_str = f"{res_val['width']}x{res_val['height']}"
                elif isinstance(res_val, str):
                    res_str = res_val
                else:
                    res_str = "1560x700"
            market = (dev.info.get("market") or "EU").strip().upper() or "EU"
            dev.info["market"] = market
            private_ip = dev.info.get("private_server_ip", "") or ""

            # 캡처 시 PNG 실제 크기와 입력 해상도가 다르면 dev.info 자동 갱신 + 영구 저장.
            target_dev_id = dev.id
            def _on_mib_resolution_changed(wxh: str, _did: str = target_dev_id) -> None:
                d = self._devices.get(_did)
                if d is None or d.type != "mib_agent":
                    return
                try:
                    rw_s, rh_s = wxh.upper().split("X")
                    rw, rh = int(rw_s), int(rh_s)
                except Exception:
                    return
                cur = d.info.get("resolution") if isinstance(d.info.get("resolution"), dict) else None
                if cur and cur.get("width") == rw and cur.get("height") == rh:
                    return
                d.info["resolution"] = {"width": rw, "height": rh}
                d.info["resolution_str"] = f"{rw}x{rh}"
                if isinstance(d.info.get("screens"), dict):
                    for k in d.info["screens"]:
                        d.info["screens"][k] = {"width": rw, "height": rh}
                logger.info("MIB auto-detected resolution: %s → %s", _did, f"{rw}x{rh}")
                try:
                    self._save_auxiliary_devices()
                except Exception as e:
                    logger.warning("MIB auto-detect persist failed: %s", e)

            def _on_mib_addr_changed(src: str, dst: str, _did: str = target_dev_id) -> None:
                """ksend src/dst가 자동 보정될 때 dev.info에 저장 + 영구 저장."""
                d = self._devices.get(_did)
                if d is None or d.type != "mib_agent":
                    return
                cur_src = d.info.get("ksend_src", "")
                cur_dst = d.info.get("ksend_dst", "")
                if cur_src == src and cur_dst == dst:
                    return
                d.info["ksend_src"] = src
                d.info["ksend_dst"] = dst
                logger.info("MIB auto-corrected ksend addr: %s → src=%s dst=%s", _did, src, dst)
                try:
                    self._save_auxiliary_devices()
                except Exception as e:
                    logger.warning("MIB addr auto-correct persist failed: %s", e)

            # screen 인덱스 — 디바이스마다 가용 layer 다름. 저장된 값이 있으면 사용.
            stored_indices = dev.info.get("screen_indices")
            screen_indices = None
            if isinstance(stored_indices, list) and stored_indices:
                try:
                    screen_indices = [int(i) for i in stored_indices]
                except Exception:
                    screen_indices = None

            try:
                svc = MIBAgentService(
                    dev.address, port=port, device_id=dev.id,
                    username=username, password=password, resolution=res_str,
                    private_server_ip=private_ip,
                    private_server_password=dev.info.get("private_server_password", "") or "",
                    iid_display=dev.info.get("iid_display", "10") or "10",
                    hud_display=dev.info.get("hud_display", "11") or "11",
                    market=market,
                    key_overrides=dev.info.get("mib_keys"),
                    on_resolution_changed=_on_mib_resolution_changed,
                    on_addr_changed=_on_mib_addr_changed,
                    screen_indices=screen_indices,
                )
                # 저장된 ksend src/dst override가 있으면 market default를 덮어씀
                stored_src = dev.info.get("ksend_src")
                stored_dst = dev.info.get("ksend_dst")
                if stored_src and stored_dst:
                    svc.set_addr(str(stored_src), str(stored_dst))
                # 저장된 터치 보정 오프셋 적용 (디바이스별 터치 원점 어긋남 보정)
                _tox = dev.info.get("touch_x_offset")
                _toy = dev.info.get("touch_y_offset")
                if _tox is not None or _toy is not None:
                    try:
                        svc.set_touch_offsets(int(_tox or 0), int(_toy or 0))
                    except Exception as e:
                        logger.warning("MIB touch offset apply failed: %s", e)
                # 저장된 터치 디지타이저 스케일 override (패널 고유값, 예: 13.1" y=0.25)
                _txs = dev.info.get("touch_x_scale")
                _tys = dev.info.get("touch_y_scale")
                if _txs is not None or _tys is not None:
                    try:
                        svc.set_touch_scale(_txs, _tys)
                    except Exception as e:
                        logger.warning("MIB touch scale apply failed: %s", e)
                ok = await svc.async_connect()
                if ok:
                    self._mib_conns[dev.id] = svc
                    dev.status = "connected"
                    _mark_connected()
                    dev.info["agent_version"] = svc.agent_version
                    dev.info["screens"] = svc.get_info()["screens"]
                    dev.info["resolution"] = dev.info["screens"].get(
                        svc.default_screen, {"width": 1560, "height": 700}
                    )
                    dev.info["resolution_str"] = res_str
                    return f"MIB connected: {dev.id} ({dev.address}:{port})"
                else:
                    dev.status = "disconnected"
                    return f"MIB connect failed: {dev.id}"
            except Exception as e:
                dev.status = "disconnected"
                return f"MIB connect failed: {dev.id} — {e}"

        elif dev.type == "module":
            module_name = dev.info.get("module", "")
            if not module_name:
                return f"Module {dev.id}: no module configured"
            # 가상 모듈 (plugins/*.py(.pyd)에도 lge.auto에도 클래스 없음) — 인스턴스 생성 불필요.
            # 예: "OCR" — playback_service가 직접 처리. _ensure_default_*가 설정한 connected를 유지.
            # open_all_serial_connections와 동일한 가드 — 부팅 시뿐 아니라 전체연결/개별연결 시에도 skip.
            try:
                from .module_service import _import_module_class
                if _import_module_class(module_name) is None:
                    dev.status = "connected"
                    _mark_connected()
                    logger.info("Module %s is virtual (no class file); marking connected", dev.id)
                    return f"Module ready (virtual): {dev.id} ({module_name})"
            except Exception as e:
                logger.debug("Virtual module check for %s failed (continuing with init): %s",
                             module_name, e)
            # 시리얼 기반 모듈: COM 포트 존재 여부 선검증 (DLL 모듈 오탐 차단)
            if dev.info.get("connect_type") == "serial":
                if not await loop.run_in_executor(None, self._com_port_exists, dev.address):
                    dev.status = "disconnected"
                    logger.info("Module connect skipped: %s — COM port %s not present", dev.id, dev.address)
                    return f"Module connect failed: {dev.id} — COM port {dev.address} not available"
            try:
                from .module_service import _get_instance, _is_connected
                from ..routers.device import _build_constructor_kwargs
                ctor_kwargs = _build_constructor_kwargs(dev)
                shared_conn = self.get_serial_conn(dev.id)
                # 장시간 Setup(SCAR 컨테이너 기동/TH CVD 부팅) 동안 UI 카드에 '연결 중' 표시.
                # 진행 단계 문구는 플러그인이 connect_progress 레지스트리로 보고 →
                # /device/list 가 connect_progress 필드로 노출.
                dev.status = "reconnecting"
                instance = await loop.run_in_executor(
                    None, functools.partial(_get_instance, module_name, ctor_kwargs, shared_conn),
                )
                if _is_connected(instance):
                    dev.status = "connected"
                    _mark_connected()
                    return f"Module connected: {dev.id} ({module_name})"
                else:
                    dev.status = "disconnected"
                    return f"Module not connected: {dev.id} ({module_name})"
            except Exception as e:
                dev.status = "disconnected"
                return f"Module connect failed: {dev.id} — {e}"

        elif dev.type == "vision_camera":
            mac = dev.info.get("mac", "")
            if not mac:
                return f"VisionCamera {dev.id}: no MAC configured"
            try:
                from ..plugins.VisionCamera import VisionCamera
                cam = VisionCamera(
                    mac=mac,
                    model=dev.info.get("model", ""),
                    serial=dev.info.get("serial_number", ""),
                    ip=dev.info.get("ip", ""),
                    subnetmask=dev.info.get("subnetmask", "255.255.0.0"),
                )
                await loop.run_in_executor(None, cam.Connect)
                self._vision_cams[dev.id] = cam
                dev.status = "connected"
                _mark_connected()
                return f"VisionCamera connected: {dev.id} ({mac})"
            except Exception as e:
                dev.status = "disconnected"
                return f"VisionCamera connect failed: {dev.id} — {e}"

        elif dev.type == "webcam":
            try:
                # 기존 연결 정리
                old = self._webcam_devs.pop(dev.id, None)
                if old:
                    try:
                        await loop.run_in_executor(None, old.Disconnect)
                    except Exception:
                        pass
                # 녹화용 싱글톤이 같은 인덱스를 열고 있으면 자동 해제 (하드웨어 경합 방지)
                try:
                    target_index = int(dev.info.get("device_index", 0))
                    from .webcam_service import get_webcam_service
                    wsvc = get_webcam_service()
                    if wsvc.is_open() and getattr(wsvc, "_device_index", None) == target_index:
                        logger.info("Closing recording webcam singleton (index=%d) — device being registered as primary", target_index)
                        await loop.run_in_executor(None, wsvc.close)
                except Exception as _e:
                    logger.debug("Failed to close recording singleton pre-connect: %s", _e)
                from ..plugins.WebcamDevice import WebcamDevice
                cam = WebcamDevice(
                    device_index=int(dev.info.get("device_index", 0)),
                    width=int(dev.info.get("width", 0)),
                    height=int(dev.info.get("height", 0)),
                )
                await loop.run_in_executor(None, cam.Connect)
                self._webcam_devs[dev.id] = cam
                info = cam.GetInfo()
                dev.info["width"] = info.get("width", dev.info.get("width", 0))
                dev.info["height"] = info.get("height", dev.info.get("height", 0))
                dev.info["resolution"] = {
                    "width": info.get("width", 0),
                    "height": info.get("height", 0),
                }
                dev.status = "connected"
                _mark_connected()
                return f"Webcam connected: {dev.id} (index={dev.info.get('device_index')})"
            except Exception as e:
                dev.status = "disconnected"
                return f"Webcam connect failed: {dev.id} — {e}"

        elif dev.type == "ssh":
            host = dev.info.get("host", dev.address)
            port = int(dev.info.get("port", 22))
            username = dev.info.get("username", "")
            password = dev.info.get("password", "")
            key_file_path = dev.info.get("key_file_path", "") or None
            try:
                # 기존 연결 닫고 새로 생성
                self._close_ssh_conn(dev.id)
                conn = SSHConnection(host=host, port=port, username=username,
                                     password=password, key_file_path=key_file_path)
                await loop.run_in_executor(None, conn.connect)
                self._ssh_conns[dev.id] = conn
                dev.status = "connected"
                _mark_connected()
                return f"SSH connected: {dev.id} ({username}@{host}:{port})"
            except Exception as e:
                dev.status = "disconnected"
                return f"SSH connect failed: {dev.id} — {e}"

        elif dev.type == "adb":
            try:
                # WiFi: adb connect, USB: adb reconnect
                if ":" in dev.address:
                    await self.adb.connect_device(dev.address)
                else:
                    # USB 디바이스: reconnect 시도 (connecting 상태 해결)
                    try:
                        await self.adb._run(f"-s {dev.address} reconnect")
                    except Exception:
                        pass

                # 연결 확인 (최대 3회 재시도)
                for attempt in range(3):
                    devs = await self.adb.list_devices()
                    found = next((d for d in devs if d.serial == dev.address), None)
                    if found and found.status == "device":
                        dev.status = "device"
                        _mark_connected()
                        self._adb_reconnect_attempts.pop(dev.id, None)
                        # 화면 미러링용 장기 adb shell 세션 선제 시작 (프레임당 spawn 회피)
                        try:
                            await self.adb.ensure_streamer(dev.address)
                        except Exception as se:
                            logger.debug("ADB streamer pre-start failed for %s: %s", dev.id, se)
                        # 패턴 스와이프용 touch input 캐시 백그라운드 적재
                        # (첫 패턴 입력 시 _find_touch_device + 권한 탐지로 ADB shell이 점유되어
                        #  화면 캡처가 멈춰 보이는 현상 방지)
                        asyncio.create_task(self.adb.prewarm_touch_input(dev.address))
                        return f"ADB connected: {dev.id} ({dev.address})"
                    if attempt < 2:
                        await asyncio.sleep(1)

                dev.status = found.status if found else "offline"
                return f"ADB not ready: {dev.id} ({dev.status})"
            except Exception as e:
                dev.status = "offline"
                return f"ADB connect failed: {dev.id} — {e}"

        return f"Unknown device type: {dev.type}"

    async def disconnect_device_by_id(self, device_id: str) -> str:
        """등록된 디바이스 1개의 연결만 끊기 (등록은 유지). 결과 메시지 반환."""
        dev = self._devices.get(device_id)
        if not dev:
            return f"Device {device_id} not found"

        # Common/OCR: 항상 연결 상태 유지 (no-op) — 가상 모듈 디바이스로 외부 연결 없음.
        # WinControl은 사용자가 명시적으로 disconnect 가능 (별도 처리).
        if device_id in (self.DEFAULT_COMMON_DEVICE_ID, self.DEFAULT_OCR_DEVICE_ID):
            return f"Device '{device_id}' is a protected system default (no-op)"

        self._ever_connected.discard(device_id)

        if dev.type == "wincontrol":
            try:
                self._wincontrol.detach()
            except Exception:
                pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        if dev.type == "serial" or dev.type == "module":
            # 화이트리스트 module 만 teardown (SCAR: netns 복원으로 인터넷 복구). 다른 module 은 무영향.
            if dev.type == "module":
                module_name = dev.info.get("module")
                from .module_service import MODULES_WITH_DISCONNECT_TEARDOWN, disconnect_instance
                if module_name in MODULES_WITH_DISCONNECT_TEARDOWN:
                    try:
                        msg = disconnect_instance(module_name)
                        if msg:
                            logger.info("module '%s' disconnect teardown: %s", module_name, msg)
                    except Exception as e:
                        logger.debug("module teardown failed for %s: %s", module_name, e)
            self._close_serial_conn(device_id)
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "hkmc_agent":
            svc = self._hkmc_conns.pop(device_id, None)
            if svc:
                try:
                    svc.disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "hkmc5th_wide_agent":
            svc = self._hkmc5th_wide_conns.pop(device_id, None)
            if svc:
                try:
                    svc.disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "isap_agent":
            svc = self._isap_conns.pop(device_id, None)
            if svc:
                try:
                    svc.disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "icas_agent":
            svc = self._icas_conns.pop(device_id, None)
            if svc:
                try:
                    svc.disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "mib_agent":
            svc = self._mib_conns.pop(device_id, None)
            if svc:
                try:
                    svc.disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "vision_camera":
            cam = self._vision_cams.pop(device_id, None)
            if cam:
                try:
                    cam.Disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "webcam":
            cam = self._webcam_devs.pop(device_id, None)
            if cam:
                try:
                    cam.Disconnect()
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "ssh":
            self._close_ssh_conn(device_id)
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        elif dev.type == "adb":
            # 장기 화면 streamer 세션 먼저 닫기
            try:
                await self.adb.close_streamer(dev.address)
            except Exception as se:
                logger.debug("ADB streamer close failed for %s: %s", dev.id, se)
            try:
                await self.adb.close_scrcpy_backend(dev.address)
            except Exception as se:
                logger.debug("ADB scrcpy close failed for %s: %s", dev.id, se)
            self.adb.clear_scrcpy_disabled(dev.address)
            if ":" in dev.address:
                try:
                    await self.adb._run(f"disconnect {dev.address}")
                except Exception:
                    pass
            dev.status = "disconnected"
            return f"Disconnected: {dev.id}"

        dev.status = "disconnected"
        return f"Disconnected: {dev.id}"

    def close_all_serial_connections(self) -> None:
        """Close all persistent serial/HKMC/VisionCamera/SSH connections (called on shutdown)."""
        for device_id in list(self._serial_conns.keys()):
            self._close_serial_conn(device_id)
            logger.info("Serial connection closed: %s", device_id)
        for device_id, hkmc in list(self._hkmc_conns.items()):
            hkmc.disconnect()
            logger.info("HKMC connection closed: %s", device_id)
        self._hkmc_conns.clear()
        for device_id, isap in list(self._isap_conns.items()):
            isap.disconnect()
            logger.info("iSAP connection closed: %s", device_id)
        self._isap_conns.clear()
        for device_id in list(self._ssh_conns.keys()):
            self._close_ssh_conn(device_id)
            logger.info("SSH connection closed: %s", device_id)
        for device_id, cam in list(self._vision_cams.items()):
            try:
                cam.Disconnect()
            except Exception:
                pass
            logger.info("VisionCamera connection closed: %s", device_id)
        self._vision_cams.clear()
        for device_id, cam in list(self._webcam_devs.items()):
            try:
                cam.Disconnect()
            except Exception:
                pass
            logger.info("Webcam connection closed: %s", device_id)
        self._webcam_devs.clear()

    async def send_serial_command(self, device_id: str, data: str, read_timeout: float = 1.0) -> str:
        """Send a command to a serial device and return the response."""
        dev = self.get_device(device_id)
        if not dev or dev.type != "serial":
            raise ValueError(f"Serial device {device_id} not found")
        loop = asyncio.get_event_loop()
        conn = await loop.run_in_executor(None, self._get_serial_conn, device_id)
        logger.info("Serial send [%s] port=%s open=%s data=%r", device_id, dev.address, conn.is_open, data)
        result = await loop.run_in_executor(
            None, functools.partial(_send_serial_persistent, conn, data, read_timeout)
        )
        logger.info("Serial recv [%s] response=%r", device_id, result)
        return result
