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

시나리오 노출 메서드:
  - Ready(max_retry=3)               -> "API" / "DOCKER" / "NONE"
  - SendApi(url, headers, data)      -> 응답 요약 또는 "FAIL: ..."
  - Exec(cmd, timeout=300)           -> docker exec 결과
  - Reconnect()                      -> scar.sh 백그라운드 spawn + 20s 대기
  - Setup()                          -> netns VLAN 구성 (수동 재실행 가능)
  - NetnsStatus()                    -> docker exec scar ip netns 출력
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
        reconnect_wait_s: float = 20.0,
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
    ):
        self._api = SCARApi(base_url=api_base)
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
            self._setup_last_msg = f"ok (auto_setup disabled, Ready={mode})"
            return self._setup_last_msg
        return self.Setup()

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
        ecus = _split_csv(self.stub_ecus)
        # 검증에 쓸 실제 적용 ecus (빈 칸이면 모드 기본값으로 해석)
        resolved_ecus = ecus or list(DEFAULT_STUB_ECUS.get(self.net_mode, []))
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

        # ── [4] scar.sh 기동 (컨테이너 미기동 시) ─────────
        if self.launch_scar and not self._docker.is_running():
            if self._health.reconnect_script:
                ok = self._health._reconnect()  # noqa: SLF001 — start_via_script + wait
                if ok:
                    log.append(f"[4] scar launch: started (waited {self._health.reconnect_wait_s}s)")
                else:
                    log.append(
                        f"[4] scar launch: FAILED (scar.sh 기동 실패)\n"
                        f"  → 원인 확인: {SCAR_LAUNCH_LOG}\n"
                        f"  → scar.sh 절대경로(파일)·실행권한 확인"
                    )
            else:
                log.append("[4] scar launch: skipped (reconnect_script not set)")
        else:
            running = self._docker.is_running()
            log.append(f"[4] scar launch: {'already running' if running else 'skipped (launch_scar=False)'}")

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

    def Reconnect(self) -> str:
        """원본 'Reconnect SCAR'. setsid 로 scar.sh 백그라운드 spawn + 20s 대기."""
        if not self._health.reconnect_script:
            return "FAIL: reconnect_script not configured"
        # SCARHealth._reconnect 와 동일한 부수효과를 일으키기 위해 헬스 객체 경유
        self._health._reconnect()  # noqa: SLF001 — 의도적 호출
        return "DOCKER" if self._health.force_docker_mode else "NONE"

    def Info(self) -> str:
        """현재 인스턴스 설정 요약 (디버그/검증용). sudo 비밀번호는 마스킹."""
        sudo_state = f"(set, {len(self.sudo_password)} chars)" if self.sudo_password else "(unset → -n)"
        lines = [
            f"container     = {self.container}",
            f"api_base      = {self._api.base_url}",
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
            f"sudo_pw       = {sudo_state}",
            f"setup_ok      = {self._setup_done}",
            f"api_alive     = {self._api.is_alive()}",
            f"docker_running= {self._docker.is_running()}",
        ]
        if self.iface:
            lines.append(f"iface exists  = {self._iface_exists(self.iface)}")
        return "\n".join(lines)
