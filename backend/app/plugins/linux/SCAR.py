"""SCAR 모듈 — Linux 전용 SDV(Software Defined Vehicle) 제어 플러그인.

원본:
  Reference/Renault_CDC_Plugin/SCAR&TH.txt (Robot 키워드 — 런타임)
  Reference/Renault_CDC_Plugin/RVC_Performance.txt (호출 시퀀스)
  Reference/Renault_CDC_Plugin/collab SCAR 설치 guide.pdf (설치/구성 — netns VLAN)

동작:
  ── 런타임 (원본 Robot 키워드) ──
  매 호출마다 ensure_ready() 가 API / DOCKER / NONE 모드를 자동 판별.
    API:    POST http://localhost:8081/...
    DOCKER: docker exec scar bash -c <equivalent script>
    NONE:   "FAIL: SCAR not available"
  한 번이라도 reconnect 가 일어났다면 그 후로는 force_docker_mode 가 True 가 되어
  API 경로를 다시 시도하지 않음 (원본 'Ensure SCAR Is Ready' 와 동일).

  ── 등록 시 자동 셋업 (설치 가이드 "2. Network Configuration") ──
  auto_setup=True 면 디바이스 등록 직후 Setup() 실행:
    [1] sudo ./netns.sh --setup=hil -i <iface> --clean
    [2] <mode>.json 생성 (multiverse / standalone)
    [3] sudo ./netns.sh -c <config>.json
    [4] (launch_scar) 컨테이너 미기동 시 reconnect_script 로 scar.sh 기동
    [5] docker exec <container> ip netns 검증
  vlan_config_dir 이 비어 있으면 netns 단계는 통째로 건너뛴다 (런타임 기능만 사용).

  ── 연결 직후 UI 자동화 (설치 가이드 "3~4" UI 단계) ──
  post_connect=True 면 Connect() 끝에서 UI 제어 백엔드(port 3000)로:
    [0] POST /config {capabilities:[...]} (capabilities) -> Bench Capabilities 최초 셋업
    [0b]POST /config {interfaces_ethernet:[...]}      -> SomeIP 모니터링 인터페이스(8081 auto-advance 조건)
    [1] POST /config {ends:<ui_version>}            -> UI 버전(ENDS) 선택
    [2] POST /start {service, ecu} (start_services)  -> 토글 의존 SOME/IP 서비스 기동
    [3] POST /bencontrol/buttons/<id> {state}       -> Bench IO 토글 ON
  순서 주의: capabilities 가 먼저여야 benchConfig 에 benchcontrol 키가 생겨 토글이 가능하다
  (미셋업 시 8081 은 'Select Bench Capabilities' 최초 화면, 토글은 서버에서 .length undefined 로 죽음).
  토글은 의존 서비스(InfrastructureGotoSleep 등)가 떠 있어야 유지되므로 서비스를 먼저.
  서비스 start 는 그 ECU 가 netns(stub_ecus)에 있어야 성공("NETNS is not configured" 방지).
  주의: UI 정적 프론트는 8081(api_base), 실제 제어 REST 는 3000(control_base).
  토글 id 는 /config/infos 의 등록목록 → 없으면 toolbox(전체)에서 capability 매칭으로 해석.
  미등록 토글은 auto_register=True 면 POST /config 로 benchconfig 에 자동 등록 후 토글.

시나리오 노출 메서드:
  - Ready(max_retry=3)               -> "API" / "DOCKER" / "NONE"
  - SendApi(url, headers, data)      -> 응답 요약 또는 "FAIL: ..."
  - Exec(cmd, timeout=300)           -> docker exec 결과
  - Reconnect()                      -> scar.sh 백그라운드 spawn + 20s 대기
  - Setup()                          -> netns VLAN 구성 (수동 재실행 가능)
  - NetnsStatus()                    -> docker exec scar ip netns 출력
  - SetCapabilities(caps, force)     -> POST /config {capabilities} (Bench Capabilities 최초 셋업)
  - SetEthernet(interfaces, force)   -> POST /config {interfaces_ethernet} (SomeIP 인터페이스)
  - ListUiVersions()                 -> GET /list/ends (선택 가능 버전)
  - SelectVersion(version)           -> POST /config {ends} (UI 버전 선택)
  - ListBenchToggles()               -> GET /config/infos (등록된 토글 id/name)
  - SetBench(name_or_id, on=True)    -> POST /bencontrol/buttons/<id> (토글 ON/OFF)
  - StartService(service, ecu)       -> POST /start (SOME/IP 서버 기동, 3000 직접)
  - StopService(service, ecu, uuid)  -> POST /stop  (서비스 정지)
  - SendControl(path, data)          -> 임의 3000 제어 POST (8081 게이트 없음, SendApi 대체)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Optional

from .common.scar_api import SCARApi
from .common.scar_docker import SCARDocker, SCAR_LAUNCH_LOG
from .common.scar_health import SCARHealth
from .common.scar_netns import SCARNetns, build_config, DEFAULT_STUB_ECUS, MULTIVERSE_SLOTS, multiverse_verify_ecus


logger = logging.getLogger(__name__)


def _as_bool(v) -> bool:
    """connect_fields select="True"/"False" 문자열을 안전하게 bool 로."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


def _split_csv(s: str) -> list[str]:
    """'PIU_Mst, IVC' → ['PIU_Mst', 'IVC']. 공백/빈 항목 제거."""
    if not s:
        return []
    return [tok.strip() for tok in s.split(",") if tok.strip()]


def _csv_str(v) -> str:
    """multiselect(list/tuple) → 'a,b', CSV/문자열 → strip. 폼이 배열·CSV 어느 쪽을 줘도 허용."""
    if isinstance(v, (list, tuple)):
        return ",".join(str(x).strip() for x in v if str(x).strip())
    return str(v or "").strip()


# 이 ReplayKit 프로세스에서 SCAR 컨테이너를 "최초 연결 정리" 한 적 있는지 (프로세스당 1회).
# 모듈 reload(=ReplayKit 재시작) 시 False 로 초기화되어 다음 최초 연결에서 다시 정리한다.
_session_container_cleaned = False


class SCAR:
    """Linux SCAR 플러그인."""

    def __init__(
        self,
        api_base: str = "http://localhost:8081",
        container: str = "scar",
        reconnect_script: Optional[str] = None,
        reconnect_args: Optional[str] = None,   # 공백 구분 문자열 (시나리오 호환)
        reconnect_cwd: Optional[str] = None,
        reconnect_wait_s: float = 150.0,  # cold boot([0] 정리→재기동→UI)가 60s 를 넘는 벤치 실측 — 폴링 상한
        # ── netns VLAN 구성 (설치 가이드 2단계) ──────────────
        vlan_config_dir: str = "",               # sdv_vlan_config 디렉터리 (netns.sh 위치)
        ends: str = "FaceStep1_2025_R10",        # ENDS 버전
        net_mode: str = "multiverse",            # "multiverse" | "standalone"
        cuttlefish = True,                       # net_config 에 cuttlefish=true (cvd-ebr/TH 보존)
        iface: str = "",                         # 네트워크 인터페이스 (스캔 자동 채움)
        stub_ecus: str = "",                     # 공백 아닌 콤마 구분 (빈 칸 = 모드 기본값)
        # multiverse 슬롯별 RAD_Moon 인터페이스 (스캔 드롭다운). 비면 PIU_Mst 는 iface 로 폴백.
        iface_dtool: str = "",
        iface_obs_tool: str = "",
        iface_piu_mst: str = "",
        standalone_ip: str = "192.168.1.10",     # standalone 모드 전용 IP
        ufw: str = "off",
        log_folder: str = "/tmp",
        sudo_password: str = "",                 # 비어있으면 sudo -n (passwordless 필요)
        # ── 등록 시 동작 ─────────────────────────────────
        auto_setup = True,                       # 등록 직후 Setup() 자동 실행
        netns_clean = True,                      # apply 전에 --clean 먼저
        launch_scar = True,                      # Setup 중 컨테이너 미기동 시 scar.sh 기동
        clean_container_on_connect = True,       # 세션 최초 연결 시 stale 컨테이너 정지(scar.sh -c) 후 새로 기동
        stop_container_on_disconnect = False,    # 연결 해제 시 컨테이너도 정지(완전 정리). 기본 유지(빠른 재연결)
        # ── 연결 직후 UI 자동화 (port 3000 제어 백엔드) ──────
        # 주의: UI 정적 프론트는 8081(api_base), 실제 제어 REST 는 3000(control_base).
        #       버전 선택(/config {ends})·bench 토글(/bencontrol/buttons/<id>)은 3000 으로 간다.
        control_base: str = "http://localhost:3000",  # scar-server.js 제어 API
        post_connect = True,                     # Connect 끝에 capabilities+버전선택+bench토글 자동 실행
        capabilities: str = "",                  # Bench Capabilities (CSV, 예: "Multiverse,Without PCU HW") — 최초 셋업
        ethernet_interfaces: str = "",           # SomeIP 모니터링/NETWORK_INTERFACES 용 (CSV, 빈 칸=iface 로 대체)
        ui_version: str = "",                    # UI 에서 선택할 ENDS 버전 (빈 칸 = 건너뜀)
        start_services = None,                   # 토글 전 자동 start: [{ecu, service}, ...] 또는 JSON
        bench_toggle: str = "",                  # 활성화할 Bench IO 토글 이름/ID (빈 칸 = 건너뜀)
        bench_state: str = "switched",           # 토글 상태 ("switched"=ON / "unswitched"=OFF)
        auto_register = True,                    # 미등록 토글이면 toolbox 에서 찾아 자동 등록
        # ── 컨테이너 내부 UI 재기동 (host scar.sh -it TTY 함정 우회) ──
        ui_dir: str = "/home/scar/ui",           # 컨테이너 안 start_ui.sh 디렉터리
        ui_home: str = "/home/scar",             # start_ui.sh 가 쓰는 HOME_SCAR
    ):
        self._api = SCARApi(base_url=api_base)
        # 제어 백엔드(3000) — 버전 선택 / bench 토글 / 목록 조회. 비면 api_base 호스트에서 유추.
        self._control = SCARApi(base_url=control_base or self._derive_control_base(api_base))
        self._docker = SCARDocker(container=container)
        self.container = container
        args_list = reconnect_args.split() if reconnect_args else None
        # 옛 폼 기본값(60)으로 저장된 기존 등록 자동 보정 — cold boot 가 60s 를 넘는 벤치에서
        # [4] 폴링이 조기 포기 → 8081/3000 미기동 상태로 post-connect 전체 skip 사례(2026-06-11).
        # 명시적으로 60 이 아닌 값을 준 등록은 그대로 존중한다.
        try:
            reconnect_wait_s = float(reconnect_wait_s)
        except (TypeError, ValueError):
            reconnect_wait_s = 150.0
        if reconnect_wait_s == 60.0:
            reconnect_wait_s = 150.0
        self._health = SCARHealth(
            api=self._api,
            docker=self._docker,
            reconnect_script=reconnect_script,
            reconnect_args=args_list,
            reconnect_cwd=reconnect_cwd,
            reconnect_wait_s=reconnect_wait_s,
        )

        # netns 구성
        self.vlan_config_dir = vlan_config_dir
        self.ends = ends
        self.net_mode = (net_mode or "multiverse").strip().lower()
        self.cuttlefish = _as_bool(cuttlefish)
        self.iface = iface
        self.stub_ecus = stub_ecus
        self.multiverse_ifaces = {
            "DTOOL": (iface_dtool or "").strip(),
            "OBS_TOOL": (iface_obs_tool or "").strip(),
            "PIU_Mst": (iface_piu_mst or "").strip(),
        }
        # multiverse 에서 주 인터페이스(iface: clean -i / ethernet 폴백)가 비면 PIU_Mst 슬롯으로 대체.
        if self.net_mode == "multiverse" and not self.iface and self.multiverse_ifaces["PIU_Mst"]:
            self.iface = self.multiverse_ifaces["PIU_Mst"]
        self.standalone_ip = standalone_ip
        self.ufw = ufw
        self.log_folder = log_folder
        self.sudo_password = sudo_password
        self.auto_setup = _as_bool(auto_setup)
        self.netns_clean = _as_bool(netns_clean)
        self.launch_scar = _as_bool(launch_scar)
        self.clean_container_on_connect = _as_bool(clean_container_on_connect)
        self.stop_container_on_disconnect = _as_bool(stop_container_on_disconnect)
        # 연결 직후 UI 자동화
        self.post_connect = _as_bool(post_connect)
        self.capabilities = _csv_str(capabilities)   # multiselect(list) 또는 CSV 문자열 허용
        self.ethernet_interfaces = _csv_str(ethernet_interfaces)
        self.ui_version = (ui_version or "").strip()
        self.start_services = self._parse_services(start_services)
        self.bench_toggle = (bench_toggle or "").strip()
        self.bench_state = (bench_state or "switched").strip() or "switched"
        self.auto_register = _as_bool(auto_register)
        self.ui_dir = (ui_dir or "/home/scar/ui").strip()
        self.ui_home = (ui_home or "/home/scar").strip()
        self._netns = SCARNetns(
            vlan_config_dir=vlan_config_dir,
            sudo_password=sudo_password,
        )

        # Setup 결과 추적 (IsConnected 반환에 사용)
        self._setup_done = False
        self._setup_last_msg = ""

    # ── 자동 호출 (device_manager 가 등록 직후 호출) ──────────
    def _report(self, text: str) -> None:
        """연결 진행 단계 보고 — /device/list 의 connect_progress 로 카드에 표시.

        장시간 Setup(컨테이너 기동 등) 동안 '아무것도 안 하는 것처럼' 보이지 않게.
        실패해도 연결 자체에는 영향 없도록 조용히 무시.
        """
        try:
            from ...services.connect_progress import set_progress
            set_progress("SCAR", text)
        except Exception:
            pass

    def Connect(self) -> str:
        """device_manager 가 module 등록 직후 호출.

        auto_setup=True 이면 Setup() 실행 — 설치 가이드 2단계(netns VLAN) 등가.
        auto_setup=False 면 setup 을 건너뛰고 readiness 만 확인.
        """
        try:
            if not self.auto_setup:
                logger.info("SCAR.Connect: auto_setup disabled, skipping netns setup")
                mode = self.Ready()
                self._setup_done = mode != "NONE"
                msg = f"ok (auto_setup disabled, Ready={mode})"
            else:
                msg = self.Setup()

            # ── 연결 직후 UI 자동화 (버전 선택 + bench 토글) ──────
            # setup 이 성공(_setup_done)했고 할 일이 있을 때만. 개별 항목 실패는 경고로 유지하되,
            # 제어 백엔드(3000) 자체가 안 떠서 '전부' skip 된 경우는 connected 로 래치하지 않는다 —
            # 래치되면 인스턴스가 캐시에 살아남아 이후 재연결이 아무것도 재시도하지 않는
            # 영구 반설정(half-configured) 상태가 된다 (2026-06-11 cold-boot>폴링상한 사례).
            if self.post_connect and self._setup_done and (self.capabilities or self.ethernet_interfaces or self.ui_version or self.ends or self.start_services or self.bench_toggle):
                self._report("UI 자동화 중 (capabilities/버전/서비스/토글)")
                pc = self._post_connect()
                msg = f"{msg}\n{pc}"
                if "제어 백엔드(3000) 미응답" in pc:
                    self._setup_done = False
                    msg = ("FAIL: post-connect 미완료 — UI 제어 백엔드(3000) 미응답 "
                           "(연결을 다시 시도하면 Setup+UI 자동화를 재실행합니다)\n" + msg)
                self._setup_last_msg = msg
            return msg
        finally:
            try:
                from ...services.connect_progress import clear_progress
                clear_progress("SCAR")
            except Exception:
                pass

    def IsConnected(self) -> bool:
        """device_manager._is_connected 가 호출. Setup 성공 또는 SCAR 가용이면 True."""
        if self._setup_done:
            return True
        if self.auto_setup and self.vlan_config_dir:
            # full-setup 등록인데 Setup/post-connect 미완 — 컨테이너가 떠 있어도 connected 로
            # 보지 않는다. 여기서 True 를 주면 module_service 가 인스턴스를 재사용해
            # 재연결 클릭이 Setup/post-connect 를 영영 재시도하지 않는다.
            return False
        # netns 미사용(vlan_config_dir 빈 칸) 등록은 컨테이너/API 살아있으면 connected.
        return self._health.force_docker_mode or self._api.is_alive() or self._docker.is_running()

    # ── 시나리오 노출: Setup (수동 재실행 가능) ───────────
    def Setup(self) -> str:
        """설치 가이드 "2. Network Configuration" 자동화.

        흐름:
          [1] sudo ./netns.sh --setup=hil -i <iface> --clean   (netns_clean=True)
          [2] <mode>.json 생성
          [3] sudo ./netns.sh -c <config>.json
          [4] (launch_scar) 컨테이너 미기동 시 scar.sh 기동 + 대기
          [5] docker exec <container> ip netns 검증

        vlan_config_dir 이 비어 있으면 netns 단계 전체 skip — readiness/launch 만.
        sudo 명령은 password 있으면 -S, 없으면 -n. passwordless sudo 미설정 시 [1]/[3] FAIL.
        """
        log: list[str] = []

        # ── netns 미사용 경로 — 런타임 기능만 쓰는 등록 ─────
        if not self.vlan_config_dir:
            log.append("[netns] skipped (vlan_config_dir not set)")
            return self._finish_setup_no_netns(log)

        # ── 사전 검증 ─────────────────────────────────────
        if not self._netns.is_available():
            return self._mark_fail(
                f"netns.sh not found at {self._netns.script_path}", log
            )
        if not self.iface:
            return self._mark_fail("iface not configured (스캔으로 인터페이스 선택)", log)
        if not self._iface_exists(self.iface):
            return self._mark_fail(f"interface '{self.iface}' not in /sys/class/net/", log)
        if self.net_mode == "multiverse":
            # multiverse 는 RAD_Moon 3대가 각각 DTOOL/OBS_TOOL/PIU_Mst — 3슬롯 모두 배정 + 서로 다른
            # 인터페이스여야 정상 연결 가능. 하나라도 빠지거나 겹치면 여기서 FAIL 로 알린다.
            missing = [k for k in MULTIVERSE_SLOTS if not self.multiverse_ifaces.get(k)]
            if missing:
                return self._mark_fail(
                    f"multiverse 인터페이스 미배정: {', '.join(missing)} — RAD_Moon 3대가 모두 인식되어야 하며 "
                    f"DTOOL/OBS_TOOL/PIU_Mst 각각에 인터페이스를 지정하세요 (수정 모달에서 선택)", log)
            vals = [self.multiverse_ifaces[k] for k in MULTIVERSE_SLOTS]
            dups = sorted({v for v in vals if vals.count(v) > 1})
            if dups:
                return self._mark_fail(
                    f"multiverse 인터페이스 중복: {', '.join(dups)} — DTOOL/OBS_TOOL/PIU_Mst 는 서로 다른 "
                    f"인터페이스여야 합니다", log)
            for slot in MULTIVERSE_SLOTS:
                sif = self.multiverse_ifaces[slot]
                if not self._iface_exists(sif):
                    return self._mark_fail(
                        f"{slot} interface '{sif}' not in /sys/class/net/ (RAD_Moon 재스캔 후 수정)", log)
            log.append("[pre] multiverse 슬롯: " + ", ".join(f"{k}={self.multiverse_ifaces[k]}" for k in MULTIVERSE_SLOTS))

        # ── [0] 최초 연결 시 컨테이너 정리 후 연결 ─────────────────────
        # 이 ReplayKit 세션에서 SCAR 를 처음 연결할 때(프로세스당 1회), 이전 세션에서 남은
        # stale 컨테이너를 먼저 정지(scar.sh -c)한다. 이후 [4]에서 깨끗하게 다시 기동된다.
        # 재연결마다 매번 내렸다 올리면 느리므로 세션 1회만 수행. clean_container_on_connect 로 끔.
        global _session_container_cleaned
        if self.clean_container_on_connect and not _session_container_cleaned:
            _session_container_cleaned = True  # 실패해도 매 연결 재시도 방지(세션 1회)
            self._report("이전 세션 컨테이너 정리 중")
            if self._docker.is_running():
                _script, _tag, _cwd = self._scar_script_info()
                if _script:
                    ok, msg = self._docker.stop_via_script(_script, tag=_tag, cwd=_cwd)
                    log.append(f"[0] 최초 연결 컨테이너 정리: {'ok' if ok else 'FAIL'} — "
                               f"{(msg.splitlines()[0] if msg else '')}")
                else:
                    log.append("[0] 최초 연결 컨테이너 정리: skipped (scar.sh 경로 미설정)")
            else:
                log.append("[0] 최초 연결 컨테이너 정리: skipped (컨테이너 미기동)")

        # ── [1] clean ────────────────────────────────────
        if self.netns_clean:
            self._report("netns 정리 중")
            # multiverse: 슬롯별 인터페이스 3개를 각각 타겟으로 clean (예: --setup=hil -i enxb038... --clean)
            clean_targets = ([self.multiverse_ifaces[k] for k in MULTIVERSE_SLOTS]
                             if self.net_mode == "multiverse" else [self.iface])
            for ci in clean_targets:
                rc, msg = self._netns.clean(ci)
                log.append(f"[1] netns clean -i {ci}:\n{self._indent(msg)}")
                if rc != 0:
                    return self._mark_fail(f"netns clean (-i {ci})", log)
        else:
            log.append("[1] netns clean: skipped (netns_clean=False)")

        # ── [2] config 생성 ──────────────────────────────
        # ⚠️ netns 의 stub_ecu 이름과 /start 의 ecu(Simulated ECU target) 이름은 다르다!
        #   netns valid:  PCU_PROXY_FrontEnd  (base ECU)
        #   /start ecu:   PCU_PROXY_FrontEnd_PIU_Mst  (= base_ + 시뮬 대상 master)
        #   → start_services 의 ecu 를 netns 에 그대로 병합하면 "Invalid ECU" 로 apply 실패.
        #   매핑 규칙이 단순치 않아 자동 병합하지 않는다. stub_ecus 는 netns 이름으로 직접 지정.
        ecus = _split_csv(self.stub_ecus)
        config = build_config(
            ends=self.ends,
            iface=self.iface,
            mode=self.net_mode,
            stub_ecus=ecus or None,
            standalone_ip=self.standalone_ip,
            ufw=self.ufw,
            log_folder=self.log_folder,
            cuttlefish=self.cuttlefish,
            # multiverse: 슬롯(DTOOL/OBS_TOOL/PIU_Mst)별 인터페이스 3항목. 하나도 없으면 구방식(iface 단일).
            multiverse_ifaces=(self.multiverse_ifaces
                               if self.net_mode == "multiverse" and any(self.multiverse_ifaces.values())
                               else None),
        )
        # 검증용 실제 적용 ecus — netns 를 만드는 항목(netns!=False)만
        resolved_ecus = multiverse_verify_ecus(config)
        cfg_path, cfg_msg = self._netns.write_config(config, self.net_mode)
        if cfg_path is None:
            return self._mark_fail(f"config write: {cfg_msg}", log)
        log.append(f"[2] config:\n  {cfg_msg}\n{self._indent(json.dumps(config, indent=2))}")

        # ── [3] apply ────────────────────────────────────
        self._report("netns 적용 중")
        rc, msg = self._netns.apply(cfg_path)
        log.append(f"[3] netns apply:\n{self._indent(msg)}")
        if rc != 0:
            return self._mark_fail("netns apply", log)
        # 정상연결 판정: 호스트 `ip netns` 에 netns 대상 ECU 의 '<ecu>ns'(multiverse: PIU_Mstns) 가 있어야 한다.
        rc, msg = self._netns.verify_host(expect_ns=resolved_ecus or None)
        log.append(f"[3b] host ip netns:\n{self._indent(msg)}")
        if rc != 0:
            return self._mark_fail(
                f"host ip netns 검증 실패 — {', '.join(f'{e}ns' for e in resolved_ecus)} 미생성 "
                f"(netns apply 출력 확인)", log)

        # ── [4] UI 기동 (컨테이너 미기동 또는 8081 UI 미응답 시) ─────────
        # 두 경우를 구분해야 한다:
        #   (a) 컨테이너 running + 8081 down → 컨테이너 안 UI(start_ui.sh)만 직접 재기동.
        #       host scar.sh --ui 는 running 컨테이너에 `docker exec -it`(TTY)로 들어가는데,
        #       우리 setsid </dev/null 무TTY 환경에선 'cannot attach stdin to a TTY' 로 실패한다.
        #       → 그래서 docker exec(무TTY)로 start_ui.sh 를 직접 돌린다 (screen -dm, TTY 불필요).
        #   (b) 컨테이너 자체가 down → host scar.sh 로 컨테이너+UI 통째 기동.
        running = self._docker.is_running()
        api_alive = self._api.is_alive()
        if not self.launch_scar:
            log.append("[4] UI launch: skipped (launch_scar=False)")
        elif running and api_alive:
            log.append("[4] UI launch: already running (container up, 8081 alive)")
        elif running and not api_alive:
            # (a) 컨테이너 안 UI 만 죽음 — 가장 흔한 재발 케이스.
            self._report("UI 재기동(start_ui.sh) — 8081 대기 중")
            ok, msg = self._docker.restart_ui_in_container(self.ui_dir, self.ui_home)
            log.append(f"[4] UI restart (in-container start_ui.sh): {self._indent(msg).lstrip()}")
            if ok and self._wait_api_up(self._health.reconnect_wait_s):
                self._health.force_docker_mode = False  # 직접 기동이라 래치 불필요
                log.append("  → 8081 alive: API 모드 활성")
            elif ok:
                log.append(f"  → 8081 still down after {self._health.reconnect_wait_s}s "
                           f"(원인 확인: docker exec scar tail /rhw/logs/scar/ui_log_*.log)")
            else:
                log.append("  → start_ui.sh 기동 실패 (위 출력/ui_dir·ui_home 확인)")
        elif self._health.reconnect_script:
            # (b) 컨테이너 down — host scar.sh 로 통째 기동 (start_via_script + 폴링).
            self._report(f"컨테이너 기동(scar.sh) — 8081 대기 중 (최대 {self._health.reconnect_wait_s:g}s)")
            _t0 = time.time()
            ok = self._health._reconnect()  # noqa: SLF001
            _polled = time.time() - _t0
            if ok:
                log.append(f"[4] scar launch: container start via scar.sh "
                           f"(8081 대기 {_polled:.0f}s / 상한 {self._health.reconnect_wait_s:g}s)")
            else:
                log.append(
                    f"[4] scar launch: FAILED (scar.sh 기동 실패)\n"
                    f"  → 원인 확인: {SCAR_LAUNCH_LOG}\n"
                    f"  → scar.sh 절대경로(파일)·실행권한 확인"
                )
            if self._api.is_alive():
                self._health.force_docker_mode = False
                log.append("  → 8081 alive: API 모드 활성 (force_docker_mode 해제)")
            elif ok and self._docker.is_running():
                # 컨테이너는 떴는데 8081 이 폴링 상한까지 안 옴 — scar.sh 가 '정지된 기존
                # 컨테이너'를 재시작한 경우 UI 단계가 `docker exec -it`(TTY) 라 무TTY 환경에서
                # 죽는 함정(launch.log 'cannot attach stdin to a TTY...'). 컨테이너가 없을 때의
                # 신규 기동은 UI 까지 올라오지만([0] 정리 후 재기동은 이 함정을 밟는다),
                # 그 경우 branch (a) 와 동일한 in-container start_ui.sh 직접 기동으로 폴백.
                self._report("start_ui.sh 폴백 — 8081 대기 중")
                ok2, msg2 = self._docker.restart_ui_in_container(self.ui_dir, self.ui_home)
                log.append(f"  → 8081 still down after {self._health.reconnect_wait_s:g}s "
                           f"(컨테이너는 running) — in-container start_ui.sh 폴백:\n"
                           f"{self._indent(msg2, '    ')}")
                if ok2 and self._wait_api_up(60.0):
                    self._health.force_docker_mode = False
                    log.append("  → 8081 alive: API 모드 활성 (start_ui.sh 폴백 성공)")
                else:
                    log.append(
                        f"  → 8081 여전히 down — 원인 확인: {SCAR_LAUNCH_LOG} / "
                        f"docker exec {self.container} tail /rhw/logs/scar/ui_log_*.log"
                    )
            elif ok:
                log.append(
                    f"  → 8081 still down after {self._health.reconnect_wait_s:g}s — 컨테이너도 미기동\n"
                    f"  → 원인 확인: {SCAR_LAUNCH_LOG}"
                )
        else:
            log.append("[4] UI launch: skipped (container down, reconnect_script not set)")

        # ── [5] netns 검증 (컨테이너 떠 있을 때만) ────────
        if self._docker.is_running():
            # multiverse: stub_ecus → '<ecu>ns' 직접 검증. standalone: 고정 namespace 셋이라 lenient.
            expect = resolved_ecus if self.net_mode == "multiverse" else None
            rc, msg = self._netns.verify(self.container, expect_ns=expect)
            log.append(f"[5] netns verify:\n{self._indent(msg)}")
            if rc != 0:
                # 검증 실패는 경고로만 — apply 는 성공했으므로 등록은 유지.
                log.append("  warn: ip netns 검증 실패 (컨테이너 부팅 지연 가능). NetnsStatus() 로 재확인.")
        else:
            # scar.sh 는 돌았지만 컨테이너가 안 떴다 — 출력 로그로 원인 안내.
            log.append(
                f"[5] netns verify: skipped (container '{self.container}' not running)\n"
                f"  → scar.sh 기동 출력 확인: {SCAR_LAUNCH_LOG}\n"
                f"  → 컨테이너 이름 확인: docker ps -a (이름이 '{self.container}' 가 아니면 폼의 container 수정)"
            )

        self._setup_done = True
        self._setup_last_msg = "ok\n" + "\n".join(log)
        logger.info("SCAR.Setup ok (mode=%s, iface=%s)", self.net_mode, self.iface)
        return self._setup_last_msg

    def _finish_setup_no_netns(self, log: list) -> str:
        """vlan_config_dir 없을 때 — 가용성만 '비파괴적으로' 확인하고 ok 반환.

        주의: Ready() 는 양쪽 다운 시 _reconnect 로 force_docker_mode 를 켜는 부수효과가 있다.
        단순 등록 시점에 그 상태를 오염시키지 않도록 api/docker 를 직접 프로브만 한다.
        실제 모드 판별은 시나리오에서 Ready() 가 수행.
        """
        api = self._api.is_alive()
        dock = self._docker.is_running()
        log.append(f"[runtime] api_alive={api} docker_running={dock}")
        self._setup_done = api or dock
        prefix = "ok" if self._setup_done else "ok (SCAR not available yet — 시나리오 Ready() 로 자동 판별)"
        self._setup_last_msg = prefix + "\n" + "\n".join(log)
        return self._setup_last_msg

    # ── Setup 내부 헬퍼 ───────────────────────────────────
    def _mark_fail(self, stage: str, log: Optional[list] = None) -> str:
        msg = f"FAIL: {stage}"
        if log:
            msg += "\n" + "\n".join(log)
        self._setup_done = False
        self._setup_last_msg = msg
        logger.warning("SCAR.Setup failed at '%s'", stage)
        return msg

    @staticmethod
    def _iface_exists(name: str) -> bool:
        return os.path.isdir(os.path.join("/sys/class/net", name))

    @staticmethod
    def _indent(text: str, prefix: str = "  ") -> str:
        return "\n".join(prefix + ln for ln in (text or "").splitlines())

    def NetnsStatus(self) -> str:
        """docker exec <container> ip netns 출력 — netns 검증 수동 재확인."""
        if not self._docker.is_running():
            return "FAIL: SCAR container not running"
        rc, msg = self._netns.verify(self.container, expect_ns=None)
        return msg if rc == 0 else f"FAIL: {msg}"

    # ── 시나리오 노출 (런타임) ────────────────────────────
    def Ready(self, max_retry: int = 3) -> str:
        """현재 SCAR 가 사용 가능한 모드를 결정. 'API' / 'DOCKER' / 'NONE'."""
        return self._health.ensure_ready(int(max_retry))

    def SendApi(
        self,
        url: str,
        headers: str = "",
        data: str = "",
        max_retry: int = 3,
    ) -> str:
        """원본 'Send request in SCAR by using API'.

        headers/data 는 JSON 문자열로 전달 (Robot 키워드와 동일 호환).
        Ready 결과가 API 가 아니면 FAIL — Docker 모드에서 동등 동작이 필요한 호출은
        Exec() 로 명시적으로 부르는 것이 옳다.
        """
        mode = self.Ready(int(max_retry))
        if mode == "NONE":
            return "FAIL: SCAR not available"
        if mode == "DOCKER":
            return "FAIL: SCAR API down, use Exec() in docker mode"

        try:
            hdr = json.loads(headers) if headers else {}
            body = json.loads(data) if data else {}
        except json.JSONDecodeError as e:
            return f"FAIL: bad JSON in headers/data: {e}"

        resp = self._api.post(url, headers=hdr, data=body)
        if resp is None:
            return "FAIL: SCAR API POST request failed"
        snippet = resp.text[:512] if resp.text else ""
        return f"status={resp.status_code}\n{snippet}".strip()

    def Exec(self, cmd: str, timeout: int = 300, max_retry: int = 3) -> str:
        """원본 'Exec In SCAR Container'. docker exec scar bash -c <cmd>.

        Ready 결과가 NONE 이면 FAIL. API 든 DOCKER 든 컨테이너만 떠있으면 동작 가능.
        """
        mode = self.Ready(int(max_retry))
        if mode == "NONE":
            return "FAIL: SCAR not available"
        if not self._docker.is_running():
            return "FAIL: SCAR container not running"

        res = self._docker.exec(cmd, timeout=float(timeout))
        if res.timed_out:
            return f"FAIL: docker exec timeout ({timeout}s) rc={res.rc}"
        tail = res.stdout[-2048:].decode("utf-8", "replace").strip()
        header = f"rc={res.rc}"
        return f"{header}\n{tail}" if tail else header

    def _scar_script_info(self):
        """scar.sh 경로 / 버전 태그 / cwd 추출 (stop/clean 용). reconnect_* 설정에서 도출."""
        script = self._health.reconnect_script
        args = self._health.reconnect_args or []
        cwd = self._health.reconnect_cwd
        tag = "2.2.0"
        for i, a in enumerate(args):
            if a == "-t" and i + 1 < len(args):
                tag = args[i + 1]
                break
        return script, tag, cwd

    def StopContainer(self) -> str:
        """scar.sh -t <tag> -c 로 scar 도커 컨테이너 정지 (설치 가이드 'To stop Scar')."""
        script, tag, cwd = self._scar_script_info()
        if not script:
            return "FAIL: scar.sh 경로(reconnect_script) 미설정 — 컨테이너 정지 불가"
        if not self._docker.is_running():
            return "ok: 컨테이너가 이미 정지 상태"
        ok, msg = self._docker.stop_via_script(script, tag=tag, cwd=cwd)
        return f"ok: {msg}" if ok else f"FAIL: scar 컨테이너 정지\n{self._indent(msg)}"

    def Cleanup(self) -> str:
        """SCAR 완전 정리 — 컨테이너 정지(scar.sh -c) + netns 복원(호스트 인터넷 복구).

        Disconnect 는 netns 만 복원하고 컨테이너는 유지하는데(빠른 재연결용), 이 메서드는
        컨테이너까지 내려 완전히 정리한다. 시나리오 스텝 / 수동 정리용.
        """
        ok_all = True
        parts: list[str] = []
        cmsg = self.StopContainer()
        if cmsg.startswith("FAIL"):
            ok_all = False
        parts.append("[container] " + cmsg.split("\n")[0])
        if self._netns.is_available() and self.iface:
            rc, msg = self._netns.clean(self.iface)
            if rc != 0:
                ok_all = False
                parts.append(f"[netns] FAIL clean (iface={self.iface})")
            else:
                parts.append(f"[netns] ok clean (iface={self.iface})")
        else:
            parts.append("[netns] skipped (netns 미사용)")
        return ("ok: " if ok_all else "FAIL: ") + " | ".join(parts)

    def Disconnect(self) -> str:
        """연결 해제/등록 삭제 시 netns VLAN 구성 복원 (호스트 인터넷 복구).

        등록 시 Setup 이 netns 를 구성하는데, netns 가 default-route(인터넷) 어댑터를
        네임스페이스로 가져가면 호스트 인터넷이 끊긴다. 해제 시 대칭으로 clean 하지 않으면
        그 상태가 그대로 남으므로, 여기서 `netns.sh --setup=hil -i <iface> --clean` 으로
        되돌린다. 컨테이너(scar.sh)는 유지한다 — 다음 연결 시 재사용(A안).
        netns 미사용(vlan_config_dir 빈칸) 등록이면 정리할 것이 없다.

        device_manager 의 연결 해제(disconnect_device_by_id) / 등록 삭제(remove_device)
        에서 module teardown 으로 호출된다.
        """
        parts: list[str] = []
        # netns 복원 (호스트 인터넷 복구)
        if not self._netns.is_available() or not self.iface:
            parts.append("netns: not in use")
        else:
            rc, msg = self._netns.clean(self.iface)
            parts.append(f"netns: cleaned (iface={self.iface})" if rc == 0
                         else f"netns: FAIL clean (iface={self.iface})")
        # 옵션: 컨테이너도 정지(완전 정리). 기본은 유지 — 다음 연결 시 재사용(빠른 재연결).
        if self.stop_container_on_disconnect:
            parts.append("container: " + self.StopContainer().split("\n")[0])
        else:
            parts.append("container: kept (stop_container_on_disconnect=False)")
        # 해제 후 인스턴스가 어떤 경로로든 재사용되더라도 connected 로 오인하지 않게 (TH 와 대칭).
        self._setup_done = False
        return ("FAIL: " if any("FAIL" in p for p in parts) else "ok: ") + " | ".join(parts)

    def Reconnect(self) -> str:
        """원본 'Reconnect SCAR'. setsid 로 scar.sh 백그라운드 spawn + 20s 대기."""
        if not self._health.reconnect_script:
            return "FAIL: reconnect_script not configured"
        # SCARHealth._reconnect 와 동일한 부수효과를 일으키기 위해 헬스 객체 경유
        self._health._reconnect()  # noqa: SLF001 — 의도적 호출
        return "DOCKER" if self._health.force_docker_mode else "NONE"

    # ── 연결 직후 UI 자동화 (port 3000 제어 백엔드) ─────────
    @staticmethod
    def _derive_control_base(api_base: str) -> str:
        """api_base(8081) 호스트를 재사용해 제어 백엔드(3000) URL 유추."""
        from urllib.parse import urlparse
        try:
            u = urlparse(api_base if "://" in api_base else "http://" + api_base)
            host = u.hostname or "localhost"
            scheme = u.scheme or "http"
            return f"{scheme}://{host}:3000"
        except Exception:
            return "http://localhost:3000"

    def _wait_api_up(self, timeout_s: float, interval_s: float = 2.0) -> bool:
        """8081 UI(api_base)가 뜰 때까지 폴링. start_ui.sh 기동 후 준비 확인용."""
        deadline = time.time() + max(0.0, timeout_s)
        while True:
            if self._api.is_alive():
                return True
            if time.time() >= deadline:
                return False
            time.sleep(min(interval_s, max(0.1, timeout_s)))

    def _control_ready(self, retries: int = 15, wait_s: float = 2.0) -> bool:
        """제어 백엔드(3000) 가 응답할 때까지 대기. GET /list/ends 로 프로브.

        주의: 3000 의 GET / 는 응답을 안 보내(hang) 프로브로 못 쓴다 → /list/ends 사용.
        8081 이 방금 떠도 3000(node backend_ui)이 몇 초 늦을 수 있어 30s 까지 본다.
        """
        url = self._control.base_url + "/list/ends"
        for _ in range(max(1, retries)):
            resp = self._control.get(url)
            if resp is not None and resp.status_code < 500:
                return True
            time.sleep(wait_s)
        return False

    def _fetch_infos(self):
        """GET /config/infos → 전체 dict (benchcontrol/toolbox/capabilities). 실패 시 None."""
        resp = self._control.get(self._control.base_url + "/config/infos")
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    def _fetch_benchcontrol(self):
        """현재 '등록된' 토글 리스트. 실패 시 None, 없으면 []."""
        data = self._fetch_infos()
        if data is None:
            return None
        bc = data.get("benchcontrol")
        return bc if isinstance(bc, list) else []

    @staticmethod
    def _flatten_toolbox(toolbox) -> list:
        """configToolbox({category:[btn,...]}) → 전체 버튼 리스트(평탄화)."""
        out: list = []
        if isinstance(toolbox, dict):
            for items in toolbox.values():
                if isinstance(items, list):
                    out.extend(items)
        return out

    @staticmethod
    def _cap_ok(button_caps, bench_caps) -> bool:
        """버튼이 이 벤치에 적용 가능한지 — UI setup.js showPopupPluginsBtns 와 동일 규칙.

        규칙: 버튼의 모든 capability 가 '어떤 벤치 capability 의 접두사' 이면 적용 가능.
          item.capability.every(cap => benchCaps.some(bc => bc.startsWith(cap)))
        ⚠️ 버튼 capability 는 짧은 형태('with_pcu'/'without_pcu'), 벤치는 긴 형태
          ('with_pcu_hw'/'without_pcu_hw') 라 동등/부분집합(issubset) 비교로는 절대 안 맞는다.
          예) 'Wake up/Sleep minimal CDC/SA' = sleep_mini_power_sequence_no_pcu(capability
          ['multiverse','without_pcu']) → 벤치 ['multiverse','without_pcu_hw'] 와 startswith 로만 매칭.
        벤치 capability 정보가 없으면(미구성) 판별 불가 → True(필터 안 함).
        """
        if not bench_caps:
            return True
        have = [str(c).strip().lower() for c in bench_caps]
        for cap in (button_caps or []):
            c = str(cap).strip().lower()
            if not any(bc.startswith(c) for bc in have):
                return False
        return True

    def ListUiVersions(self) -> str:
        """GET /list/ends — UI 에서 선택 가능한 ENDS 버전 목록."""
        resp = self._control.get(self._control.base_url + "/list/ends")
        if resp is None:
            return "FAIL: control API(3000) not reachable"
        try:
            versions = resp.json().get("versions", [])
        except ValueError:
            return f"FAIL: bad JSON from /list/ends (status={resp.status_code})"
        return "\n".join(versions) if versions else "(no versions)"

    def _resolve_ui_version(self) -> str:
        """netns ENDS(self.ends, 예: 'FaceStep1_2025_R10') → UI ENDS(예: '2025_r10').

        상단 'ENDS 버전' 필드 하나로 UI 버전까지 처리하기 위함. netns 와 UI 의 ENDS 표기가
        달라(FaceStep1_ 접두어/대소문자) /list/ends 실제 목록과 매칭해 정확한 값을 고른다.
        매칭 실패 시 정규화값(접두어 제거+소문자) 반환. ends 비어있으면 "".
        """
        raw = (self.ends or "").strip()
        if not raw:
            return ""
        norm = raw.lower()
        if norm.startswith("facestep1_"):
            norm = norm[len("facestep1_"):]
        versions = []
        resp = self._control.get(self._control.base_url + "/list/ends")
        if resp is not None:
            try:
                versions = resp.json().get("versions", []) or []
            except ValueError:
                versions = []
        for v in versions:
            vl = str(v).lower()
            if vl == norm or vl == raw.lower() or raw.lower().endswith(vl):
                return v
        return norm

    @staticmethod
    def _compute_cap_id(name: str) -> str:
        """capability 표시이름 → 서버 id. UI check_capabilities.compute_id 와 동일.

        규칙: 소문자화 + 연속 공백 → '_'. 이미 id 형태('without_pcu_hw')면 그대로 통과.
          'Without PCU HW' → 'without_pcu_hw',  'Multiverse' → 'multiverse'
        """
        return re.sub(r"\s+", "_", str(name).strip().lower())

    def _fetch_capabilities(self):
        """현재 서버에 설정된 capabilities 리스트. 미설정/실패 시 None."""
        data = self._fetch_infos()
        if data is None:
            return None
        caps = data.get("capabilities")
        return caps if isinstance(caps, list) and caps else None

    def SetCapabilities(self, caps: str = "", force: bool = False) -> str:
        """POST /config {capabilities:[...]} — Bench Capabilities 최초 셋업.

        8081 'Select Bench Capabilities' 최초 화면 등가. 이 단계가 빠지면 서버 benchConfig 에
        capabilities/benchcontrol 키가 생기지 않아, 토글이 scar-server.js 에서
        'Cannot read property length of undefined' 로 죽는다(/bencontrol/buttons → 500).
        caps: CSV/리스트 표시이름·ID (예: 'Multiverse,Without PCU HW'). 미지정 시 생성자 capabilities.
        force=False 면 이미 설정돼 있으면 건너뜀(UI 의 `if(!res.capabilities)` 와 동일).
        """
        raw = _csv_str(caps) or self.capabilities
        if not raw:
            return "FAIL: no capabilities given (capabilities 비어있음)"
        ids = [self._compute_cap_id(c) for c in _split_csv(raw)]
        if not ids:
            return "FAIL: no valid capability ids"
        if not force:
            existing = self._fetch_capabilities()
            if existing:
                return f"skip: capabilities already set ({existing})"
        resp = self._control.post(
            self._control.base_url + "/config",
            data={"capabilities": ids},
        )
        if resp is None:
            return "FAIL: control API(3000) POST /config (capabilities) failed"
        if resp.status_code != 200:
            return f"FAIL: /config capabilities status={resp.status_code} {resp.text[:256]}"
        return f"ok: capabilities={ids} selected"

    def _fetch_ethernet(self):
        """현재 서버에 설정된 ethernet_interfaces 리스트. 미설정/실패 시 None."""
        data = self._fetch_infos()
        if data is None:
            return None
        eth = data.get("ethernet_interfaces")
        return eth if isinstance(eth, list) and eth else None

    def SetEthernet(self, interfaces: str = "", force: bool = False) -> str:
        """POST /config {interfaces_ethernet:[...]} — SomeIP 모니터링/NETWORK_INTERFACES 인터페이스.

        8081 'ethernet interface 선택' 단계 등가. 이게 비면 8081 이 최초 화면을 안 벗어난다
        (auto-advance 조건: ethernet_interfaces && benchcontrol && capabilities 모두 필요).
        interfaces: CSV/리스트. 미지정 시 생성자 ethernet_interfaces → 그것도 없으면 self.iface 로 대체
        (스캔된 SCAR 네트워크 인터페이스). 유효값은 서버 GET /setup/list/interfaces(=ls /sys/class/net).
        force=False 면 이미 설정돼 있으면 건너뜀.
        """
        raw = _csv_str(interfaces) or self.ethernet_interfaces or (self.iface or "").strip()
        if not raw:
            return "FAIL: no ethernet interface given (ethernet_interfaces/iface 비어있음)"
        ifaces = _split_csv(raw)
        if not force:
            existing = self._fetch_ethernet()
            if existing:
                return f"skip: ethernet_interfaces already set ({existing})"
        resp = self._control.post(
            self._control.base_url + "/config",
            data={"interfaces_ethernet": ifaces},
        )
        if resp is None:
            return "FAIL: control API(3000) POST /config (interfaces_ethernet) failed"
        if resp.status_code != 200:
            return f"FAIL: /config interfaces_ethernet status={resp.status_code} {resp.text[:256]}"
        return f"ok: ethernet_interfaces={ifaces} selected"

    def SelectVersion(self, version: str = "") -> str:
        """POST /config {ends:<version>} — UI 버전(ENDS) 선택.

        version 미지정 시 생성자 ui_version 사용. 시나리오 스텝으로도 호출 가능.
        """
        version = (version or self.ui_version or "").strip()
        if not version:
            return "FAIL: no version given (ui_version 비어있음)"
        resp = self._control.post(
            self._control.base_url + "/config",
            data={"ends": version},
        )
        if resp is None:
            return "FAIL: control API(3000) POST /config failed"
        if resp.status_code != 200:
            return f"FAIL: /config status={resp.status_code} {resp.text[:256]}"
        return f"ok: version='{version}' selected"

    def ListBenchToggles(self) -> str:
        """GET /config/infos — 현재 등록된 Bench 토글(id/name) 목록."""
        toggles = self._fetch_benchcontrol()
        if toggles is None:
            return "FAIL: control API(3000) /config/infos not reachable"
        if not toggles:
            return "(no bench toggles registered — UI Setup 에서 추가 필요)"
        return "\n".join(f"{t.get('id')}  |  {t.get('name')}" for t in toggles)

    @staticmethod
    def _match_in(items: list, key: str, bench_caps=None):
        """items 에서 id 정확매칭 → name 매칭(capability 필터). (matched_id, note, ambiguous_ids)."""
        # 1) id 정확 매칭
        for t in items:
            if str(t.get("id", "")).lower() == key:
                return t.get("id"), "matched by id", []
        # 2) name 정확 매칭
        named = [t for t in items if str(t.get("name", "")).strip().lower() == key]
        if len(named) > 1 and bench_caps is not None:
            # capability 로 좁히기 (with_pcu/without_pcu/relay 등 변종 구분)
            narrowed = [t for t in named if SCAR._cap_ok(t.get("capability"), bench_caps)]
            if narrowed:
                named = narrowed
        if len(named) == 1:
            return named[0].get("id"), f"matched by name → id={named[0].get('id')}", []
        if len(named) > 1:
            return None, "", [str(t.get("id")) for t in named]
        return None, "not found", []

    def _resolve_toggle_id(self, name_or_id: str):
        """토글 name/ID 해석 — 등록목록 우선, 없으면 toolbox(전체)에서 capability 매칭.

        반환: (id, registered: bool, note: str). 못 찾으면 (None, False, 사유).
          registered=True  → 이미 benchconfig 에 등록됨(바로 토글 가능)
          registered=False → toolbox 에만 있음(토글 전 등록 필요)
        """
        data = self._fetch_infos()
        if data is None:
            return None, False, "control API(3000) /config/infos not reachable"
        key = name_or_id.strip().lower()
        registered = data.get("benchcontrol") if isinstance(data.get("benchcontrol"), list) else []
        bench_caps = data.get("capabilities")

        # 1) 이미 등록된 토글에서 (등록목록은 벤치 capability 로 이미 걸러져 있음)
        tid, note, ambig = self._match_in(registered, key, bench_caps=None)
        if tid is not None:
            return tid, True, note
        if ambig:
            return None, False, f"name '{name_or_id}' 등록목록서 모호 (복수 id: {', '.join(ambig)}) — id 로 지정"

        # 2) toolbox(전체 버튼)에서 — capability 로 변종 구분
        toolbox = self._flatten_toolbox(data.get("toolbox"))
        if not toolbox:
            return None, False, f"toggle '{name_or_id}' not found (toolbox 비어있음)"
        tid, note, ambig = self._match_in(toolbox, key, bench_caps=bench_caps)
        if tid is not None:
            return tid, False, note + " (toolbox)"
        if ambig:
            return None, False, (f"name '{name_or_id}' toolbox 서 모호 (복수 id: {', '.join(ambig)}) "
                                 f"— bench capabilities={bench_caps} 로 좁혀지지 않음, id 로 지정")
        return None, False, f"toggle '{name_or_id}' not found in registered/toolbox"

    def _register_toggle(self, tid: str) -> str:
        """POST /config {benchcontrol:[기존ids + tid]} — toolbox 버튼을 benchconfig 에 등록.

        /config 의 benchcontrol 은 '치환' 이라 기존 등록 id 를 모두 포함해 보낸다(누락 방지).
        """
        registered = self._fetch_benchcontrol() or []
        ids = [str(t.get("id")) for t in registered if t.get("id")]
        if tid not in ids:
            ids.append(tid)
        resp = self._control.post(
            self._control.base_url + "/config",
            data={"benchcontrol": ids},
        )
        if resp is None:
            return "FAIL: POST /config (register) failed"
        if resp.status_code != 200:
            return f"FAIL: register status={resp.status_code} {resp.text[:256]}"
        return f"ok: registered '{tid}' (benchcontrol={ids})"

    def SetBench(self, name_or_id: str = "", on: bool = True) -> str:
        """POST /bencontrol/buttons/<id> {state} — Bench 토글 ON/OFF.

        name_or_id: 토글 표시이름('Wake up/Sleep minimal CDC/SA') 또는 id.
        미등록 토글이면 auto_register=True 일 때 toolbox 에서 찾아 자동 등록 후 토글.
        on=True → switched(=생성자 bench_state), False → unswitched. 시나리오 스텝 호출 가능.
        """
        name_or_id = (name_or_id or self.bench_toggle or "").strip()
        if not name_or_id:
            return "FAIL: no bench toggle given (bench_toggle 비어있음)"
        tid, registered, note = self._resolve_toggle_id(name_or_id)
        if tid is None:
            return f"FAIL: {note}"

        prefix = ""
        if not registered:
            if not self.auto_register:
                return f"FAIL: toggle '{tid}' not registered (auto_register=False; UI Setup 에서 추가)"
            reg = self._register_toggle(tid)
            if reg.startswith("FAIL"):
                return f"FAIL: auto-register '{tid}': {reg}"
            prefix = f"[auto-registered] {reg}\n  "

        state = self.bench_state if on else "unswitched"
        resp = self._control.post(
            self._control.base_url + f"/bencontrol/buttons/{tid}",
            data={"state": state},
        )
        if resp is None:
            return f"{prefix}FAIL: POST /bencontrol/buttons/{tid} failed"
        if resp.status_code != 200:
            return f"{prefix}FAIL: toggle '{tid}' status={resp.status_code} {resp.text[:256]}"
        return f"{prefix}ok: bench '{tid}' → {state} ({note})"

    def SendControl(self, path: str, data: str = "", wait_ready: bool = True) -> str:
        """UI 제어 백엔드(3000)로 임의 POST — /start, /stop, /cmd/* 등.

        SendApi 와의 차이: SendApi 는 8081 Ready() 게이트에 막혀(DOCKER면 거부) 3000 제어용으로
        부적합하다. 이건 8081 과 무관하게 3000 으로 바로 보내고, 필요시 3000 준비를 기다린다.
        path: '/start' 처럼 슬래시 시작. data: JSON 문자열(시나리오 호환) 또는 빈 문자열.
        """
        if wait_ready and not self._control_ready():
            return "FAIL: control API(3000) not ready (scar.sh --ui 기동/대기 확인)"
        try:
            body = json.loads(data) if data else {}
        except json.JSONDecodeError as e:
            return f"FAIL: bad JSON in data: {e}"
        url = self._control.base_url + ("/" + path.lstrip("/"))
        resp = self._control.post(url, data=body)
        if resp is None:
            return f"FAIL: control POST {path} failed"
        snippet = resp.text[:512] if resp.text else ""
        return f"status={resp.status_code}\n{snippet}".strip()

    def StartService(self, service: str, ecu: str) -> str:
        """POST /start {service, ecu} — SCAR 서비스(SOME/IP 서버) 기동.

        service='All Services' 면 해당 ecu 전체 기동. 예: StartService('VehicleUtcTime',
        'PCU_PROXY_FrontEnd_PIU_Mst'). UI 의 Start 버튼과 동일 경로(3000).
        """
        if not service or not ecu:
            return "FAIL: service/ecu required"
        return self.SendControl("/start", json.dumps({"service": service, "ecu": ecu}))

    def StopService(self, service: str, ecu: str, uuid: str = "") -> str:
        """POST /stop {service, ecu, uuid} — SCAR 서비스 정지."""
        if not service or not ecu:
            return "FAIL: service/ecu required"
        payload = {"service": service, "ecu": ecu, "uuid": uuid or (ecu + service)}
        return self.SendControl("/stop", json.dumps(payload))

    @staticmethod
    def _parse_services(spec):
        """start_services 입력 정규화 → [{'ecu':.., 'service':..}, ...].

        object_list(list[dict]) / JSON 문자열 / None 모두 허용. ecu·service 둘 다 있는 항목만.
        """
        if not spec:
            return []
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                return []
        out = []
        if isinstance(spec, list):
            for it in spec:
                if isinstance(it, dict):
                    ecu = str(it.get("ecu", "")).strip()
                    svc = str(it.get("service", "")).strip()
                    if ecu and svc:
                        out.append({"ecu": ecu, "service": svc})
        return out

    def _post_connect(self) -> str:
        """Connect 직후 자동: 제어 백엔드 준비 → 버전 선택 → 서비스 start → bench 토글.

        토글(Wake up/Sleep minimal CDC/SA)은 의존 서비스(예: InfrastructureGotoSleep)가 떠 있어야
        '유지'되므로(update_powerseq_status.sh 가 미기동 시 강제 OFF), 서비스 start 를 토글보다 먼저 한다.
        """
        log = [f"[post-connect] UI 자동화 (control={self._control.base_url})"]
        if not self._control_ready():
            log.append("  FAIL: 제어 백엔드(3000) 미응답 — capabilities/버전/서비스/토글 건너뜀")
            return "\n".join(log)
        # Bench Capabilities: 최초 셋업(8081 'Select Bench Capabilities'). 버전/토글보다 먼저 —
        # 이게 없으면 benchcontrol 키가 안 생겨 토글이 서버에서 .length undefined 로 죽는다.
        if self.capabilities:
            log.append("  [caps]    " + self.SetCapabilities())
        # ethernet_interfaces: 8081 auto-advance 3조건 중 하나. 명시값 없으면 iface 로 대체.
        if self.ethernet_interfaces or self.iface:
            log.append("  [eth]     " + self.SetEthernet())
        # UI 버전: 명시 ui_version 우선, 없으면 상단 ENDS 버전(self.ends)에서 도출.
        ui_ver = self.ui_version or self._resolve_ui_version()
        if ui_ver:
            log.append(f"  [version] (ends={self.ends!r}→{ui_ver}) " + self.SelectVersion(ui_ver))
        for svc in self.start_services:
            log.append(f"  [service] {svc['service']}@{svc['ecu']}: "
                       + self.StartService(svc["service"], svc["ecu"]))
        if self.bench_toggle:
            log.append("  [bench]   " + self.SetBench(self.bench_toggle, on=True))
        return "\n".join(log)

    def Info(self) -> str:
        """현재 인스턴스 설정 요약 (디버그/검증용). sudo 비밀번호는 마스킹."""
        sudo_state = f"(set, {len(self.sudo_password)} chars)" if self.sudo_password else "(unset → -n)"
        lines = [
            f"container     = {self.container}",
            f"api_base      = {self._api.base_url}",
            f"control_base  = {self._control.base_url}",
            f"post_connect  = {self.post_connect}",
            f"capabilities  = {self.capabilities or '(unset → skip)'}",
            f"ethernet_ifs  = {self.ethernet_interfaces or '(unset → iface 대체)'}",
            f"ui_version    = {self.ui_version or '(unset → skip)'}",
            f"start_services= {[s['service'] + '@' + s['ecu'] for s in self.start_services] or '(none)'}",
            f"bench_toggle  = {self.bench_toggle or '(unset → skip)'} state={self.bench_state}",
            f"auto_register = {self.auto_register}",
            f"vlan_config   = {self.vlan_config_dir or '(unset → netns skip)'}",
            f"netns.sh      = {self._netns.is_available()} ({self._netns.script_path})",
            f"ends          = {self.ends}",
            f"net_mode      = {self.net_mode} (cuttlefish={self.cuttlefish})",
            f"iface         = {self.iface or '(unset)'}",
            f"stub_ecus     = {_split_csv(self.stub_ecus) or '(mode default)'}",
            f"standalone_ip = {self.standalone_ip}",
            f"auto_setup    = {self.auto_setup}",
            f"netns_clean   = {self.netns_clean}",
            f"launch_scar   = {self.launch_scar}",
            f"reconnect     = {self._health.reconnect_script or '(unset)'}",
            f"ui_dir/home   = {self.ui_dir} / {self.ui_home}",
            f"sudo_pw       = {sudo_state}",
            f"setup_ok      = {self._setup_done}",
            f"api_alive     = {self._api.is_alive()}",
            f"docker_running= {self._docker.is_running()}",
        ]
        if self.iface:
            lines.append(f"iface exists  = {self._iface_exists(self.iface)}")
        return "\n".join(lines)
