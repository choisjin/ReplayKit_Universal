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
    [1] POST /config {ends:<ui_version>}            -> UI 버전(ENDS) 선택
    [2] POST /start {service, ecu} (start_services)  -> 토글 의존 SOME/IP 서비스 기동
    [3] POST /bencontrol/buttons/<id> {state}       -> Bench IO 토글 ON
  순서 주의: 토글은 의존 서비스(InfrastructureGotoSleep 등)가 떠 있어야 유지되므로 서비스를 먼저.
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
import time
from typing import Optional

from .common.scar_api import SCARApi
from .common.scar_docker import SCARDocker, SCAR_LAUNCH_LOG
from .common.scar_health import SCARHealth
from .common.scar_netns import SCARNetns, build_config, DEFAULT_STUB_ECUS


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


class SCAR:
    """Linux SCAR 플러그인."""

    def __init__(
        self,
        api_base: str = "http://localhost:8081",
        container: str = "scar",
        reconnect_script: Optional[str] = None,
        reconnect_args: Optional[str] = None,   # 공백 구분 문자열 (시나리오 호환)
        reconnect_cwd: Optional[str] = None,
        reconnect_wait_s: float = 60.0,  # scar.sh --ui 가 8081/3000 올리는 데 20s 로는 부족 — 폴링 상한
        # ── netns VLAN 구성 (설치 가이드 2단계) ──────────────
        vlan_config_dir: str = "",               # sdv_vlan_config 디렉터리 (netns.sh 위치)
        ends: str = "FaceStep1_2025_R10",        # ENDS 버전
        net_mode: str = "multiverse",            # "multiverse" | "standalone"
        iface: str = "",                         # 네트워크 인터페이스 (스캔 자동 채움)
        stub_ecus: str = "",                     # 공백 아닌 콤마 구분 (빈 칸 = 모드 기본값)
        standalone_ip: str = "192.168.1.10",     # standalone 모드 전용 IP
        ufw: str = "off",
        log_folder: str = "/tmp",
        sudo_password: str = "",                 # 비어있으면 sudo -n (passwordless 필요)
        # ── 등록 시 동작 ─────────────────────────────────
        auto_setup = True,                       # 등록 직후 Setup() 자동 실행
        netns_clean = True,                      # apply 전에 --clean 먼저
        launch_scar = True,                      # Setup 중 컨테이너 미기동 시 scar.sh 기동
        # ── 연결 직후 UI 자동화 (port 3000 제어 백엔드) ──────
        # 주의: UI 정적 프론트는 8081(api_base), 실제 제어 REST 는 3000(control_base).
        #       버전 선택(/config {ends})·bench 토글(/bencontrol/buttons/<id>)은 3000 으로 간다.
        control_base: str = "http://localhost:3000",  # scar-server.js 제어 API
        post_connect = True,                     # Connect 끝에 버전선택+bench토글 자동 실행
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
        self.iface = iface
        self.stub_ecus = stub_ecus
        self.standalone_ip = standalone_ip
        self.ufw = ufw
        self.log_folder = log_folder
        self.sudo_password = sudo_password
        self.auto_setup = _as_bool(auto_setup)
        self.netns_clean = _as_bool(netns_clean)
        self.launch_scar = _as_bool(launch_scar)
        # 연결 직후 UI 자동화
        self.post_connect = _as_bool(post_connect)
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
    def Connect(self) -> str:
        """device_manager 가 module 등록 직후 호출.

        auto_setup=True 이면 Setup() 실행 — 설치 가이드 2단계(netns VLAN) 등가.
        auto_setup=False 면 setup 을 건너뛰고 readiness 만 확인.
        """
        if not self.auto_setup:
            logger.info("SCAR.Connect: auto_setup disabled, skipping netns setup")
            mode = self.Ready()
            self._setup_done = mode != "NONE"
            msg = f"ok (auto_setup disabled, Ready={mode})"
        else:
            msg = self.Setup()

        # ── 연결 직후 UI 자동화 (버전 선택 + bench 토글) ──────
        # setup 이 성공(_setup_done)했고 할 일이 있을 때만. 실패해도 등록 자체는 유지(경고만).
        if self.post_connect and self._setup_done and (self.ui_version or self.start_services or self.bench_toggle):
            pc = self._post_connect()
            msg = f"{msg}\n{pc}"
            self._setup_last_msg = msg
        return msg

    def IsConnected(self) -> bool:
        """device_manager._is_connected 가 호출. Setup 성공 또는 SCAR 가용이면 True."""
        if self._setup_done:
            return True
        # netns 미사용(vlan_config_dir 빈 칸) 등록도 컨테이너/API 살아있으면 connected.
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

        # ── [1] clean ────────────────────────────────────
        if self.netns_clean:
            rc, msg = self._netns.clean(self.iface)
            log.append(f"[1] netns clean:\n{self._indent(msg)}")
            if rc != 0:
                return self._mark_fail("netns clean", log)
        else:
            log.append("[1] netns clean: skipped (netns_clean=False)")

        # ── [2] config 생성 ──────────────────────────────
        # 명시 stub_ecus(빈 칸이면 모드 기본값) + start_services 의 ECU 를 자동 병합.
        # start_services 의 ecu 이름(예: PCU_PROXY_FrontEnd_PIU_Mst)이 곧 stub_ecu 이름과 같아,
        # 그 ECU 의 netns 가 없으면 서비스 start 가 "NETNS is not configured" 로 실패한다.
        # → 서비스에 쓸 ECU 를 netns 에 자동 포함시켜 수동 정합성 부담을 없앤다.
        base_ecus = _split_csv(self.stub_ecus) or list(DEFAULT_STUB_ECUS.get(self.net_mode, []))
        ecus = list(base_ecus)
        for s in self.start_services:
            if s["ecu"] not in ecus:
                ecus.append(s["ecu"])
        resolved_ecus = ecus  # 검증에 쓸 실제 적용 ecus
        config = build_config(
            ends=self.ends,
            iface=self.iface,
            mode=self.net_mode,
            stub_ecus=ecus or None,
            standalone_ip=self.standalone_ip,
            ufw=self.ufw,
            log_folder=self.log_folder,
        )
        cfg_path, cfg_msg = self._netns.write_config(config, self.net_mode)
        if cfg_path is None:
            return self._mark_fail(f"config write: {cfg_msg}", log)
        log.append(f"[2] config:\n  {cfg_msg}\n{self._indent(json.dumps(config, indent=2))}")

        # ── [3] apply ────────────────────────────────────
        rc, msg = self._netns.apply(cfg_path)
        log.append(f"[3] netns apply:\n{self._indent(msg)}")
        if rc != 0:
            return self._mark_fail("netns apply", log)

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
            ok = self._health._reconnect()  # noqa: SLF001
            if ok:
                log.append(f"[4] scar launch: container start via scar.sh (polled {self._health.reconnect_wait_s}s)")
            else:
                log.append(
                    f"[4] scar launch: FAILED (scar.sh 기동 실패)\n"
                    f"  → 원인 확인: {SCAR_LAUNCH_LOG}\n"
                    f"  → scar.sh 절대경로(파일)·실행권한 확인"
                )
            if self._api.is_alive():
                self._health.force_docker_mode = False
                log.append("  → 8081 alive: API 모드 활성 (force_docker_mode 해제)")
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
        if not self._netns.is_available() or not self.iface:
            return "ok: netns not in use (nothing to clean)"
        rc, msg = self._netns.clean(self.iface)
        if rc != 0:
            return f"FAIL: netns clean (iface={self.iface})\n{self._indent(msg)}"
        return f"ok: netns cleaned (iface={self.iface})"

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

    def _control_ready(self, retries: int = 10, wait_s: float = 2.0) -> bool:
        """제어 백엔드(3000) 가 응답할 때까지 대기. GET /list/ends 로 프로브.

        주의: 3000 의 GET / 는 응답을 안 보내(hang) 프로브로 못 쓴다 → /list/ends 사용.
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
        """버튼 capability 가 벤치 capability 에 모두 포함되면 적용 가능.

        벤치 capability 정보가 없으면(미구성) 판별 불가 → True(필터 안 함).
        """
        if not bench_caps:
            return True
        want = {str(c).strip().lower() for c in (button_caps or [])}
        have = {str(c).strip().lower() for c in bench_caps}
        return want.issubset(have)

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
            log.append("  FAIL: 제어 백엔드(3000) 미응답 — 버전/서비스/토글 건너뜀")
            return "\n".join(log)
        if self.ui_version:
            log.append("  [version] " + self.SelectVersion(self.ui_version))
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
            f"ui_version    = {self.ui_version or '(unset → skip)'}",
            f"start_services= {[s['service'] + '@' + s['ecu'] for s in self.start_services] or '(none)'}",
            f"bench_toggle  = {self.bench_toggle or '(unset → skip)'} state={self.bench_state}",
            f"auto_register = {self.auto_register}",
            f"vlan_config   = {self.vlan_config_dir or '(unset → netns skip)'}",
            f"netns.sh      = {self._netns.is_available()} ({self._netns.script_path})",
            f"ends          = {self.ends}",
            f"net_mode      = {self.net_mode}",
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
