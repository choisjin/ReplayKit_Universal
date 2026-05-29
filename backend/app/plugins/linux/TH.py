"""TH 모듈 — Linux 전용 Test Harness 신호 송신 + 시각화 패널 플러그인.

원본:
  Reference/Renault_CDC_Plugin/TH_Lib.py (tkinter)
  Reference/Renault_CDC_Plugin/RVC_Performance.txt (Robot 키워드)
  Reference/TH/connect_th.sh, host_ends_setup.sh, ensure-adb.sh (네트워크/ADB/launch 절차)

차이:
  - tkinter 대신 PySide6 패널을 별도 프로세스로 호스팅 (충돌 회피)
  - subprocess line-buffering 대신 raw fd byte scanner → trigger 즉시 인식

생성자 인자 매핑 (connect_th.sh 의 USER CONFIG ↔):
  eth_if    ← ETH_IF      USB Ethernet 어댑터 (radmoon 장비, enx<mac> 형태)
  th_home   ← TH_HOME     선택된 TH 버전 디렉터리 (예: /home/cdc/Desktop/TH/TH_0.60.12)
  host_ip   ← HOST_IP     호스트 측 cvd-ebr 대역 IP (기본 192.168.1.152/24)
  cvd_br    ← CVD_BR      bridge 이름 (기본 cvd-ebr)
  rbvm_ip   ← RBVM_IP     ADB target (기본 192.168.140.1:5555)
  th_adb    ← TH_ADB      CVD ADB host:port (기본 0.0.0.0:6520)
  grpc_ip   ← GRPC_IP     SOME/IP gRPC broker (기본 192.168.1.99:50051) — client.py --ip_address

  client.py 위치는 <th_home>/harness/harness/grpc_client/src 로 자동 도출.

시나리오 노출 메서드 (SHELL.py 반환 규약과 동일: "FAIL: ..." 접두사로 자동 실패 처리):
  - Send(topic_name, json_path, timeout=10)              fire-and-forget
  - SendAndUpdate(topic_name, json_path, timeout=10)     trigger 감지 시 패널 점등
  - PanelShow()                                          빈 패널만 띄움
  - PanelReset()                                         패널 검정으로 리셋
  - PanelClose()                                         패널 호스트 종료
  - Info()                                               현재 설정 요약 (디버그용)
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Optional

from .common.th_panel_client import PanelClient
from .common.th_signal import THSignal


logger = logging.getLogger(__name__)


# connect_th.sh 의 USER CONFIG 디폴트 — 변경 시 README/connect_fields 와 함께 갱신.
DEFAULT_HOST_IP = "192.168.1.152/24"
DEFAULT_CVD_BR = "cvd-ebr"
DEFAULT_RBVM_IP = "192.168.140.1:5555"
DEFAULT_TH_ADB = "0.0.0.0:6520"
DEFAULT_GRPC_IP = "192.168.1.99:50051"
DEFAULT_TH_ROOT = "/home/cdc/Desktop/TH"
DEFAULT_PANEL_TRIGGER = "GEAR_LEVER_ACCEPTED_T_REVERSE"

# Setup 단계별 timeout
SETUP_IP_TIMEOUT_S = 10.0
SETUP_SCRIPT_TIMEOUT_S = 120.0
SETUP_ADB_TIMEOUT_S = 15.0


def _derive_client_dir(th_home: str) -> str:
    """<th_home>/harness/harness/grpc_client/src — connect_th.sh step 6 의 cwd."""
    return os.path.join(th_home.rstrip("/"), "harness", "harness", "grpc_client", "src")


def _as_bool(v) -> bool:
    """connect_fields select="True"/"False" 문자열을 안전하게 bool 로."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    return str(v).strip().lower() in ("true", "1", "yes", "y", "on")


class TH:
    """Linux Test Harness 플러그인."""

    def __init__(
        self,
        # ── 필수 (scan + 사용자 선택) ─────────────────────────
        eth_if: str = "",                                 # USB Ethernet 인터페이스 (radmoon)
        th_home: str = "",                                # TH 버전 디렉터리
        th_root: str = DEFAULT_TH_ROOT,                   # host_ends_setup.sh / ensure-adb.sh 위치
        # ── 네트워크 디폴트 (connect_th.sh) ─────────────────
        host_ip: str = DEFAULT_HOST_IP,
        cvd_br: str = DEFAULT_CVD_BR,
        rbvm_ip: str = DEFAULT_RBVM_IP,
        th_adb: str = DEFAULT_TH_ADB,
        grpc_ip: str = DEFAULT_GRPC_IP,
        # ── 플러그인 동작 ─────────────────────────────────
        python_bin: str = "python3",
        panel = True,                                     # bool 또는 'True'/'False' (UI select)
        panel_trigger: str = DEFAULT_PANEL_TRIGGER,
        auto_setup = True,                                # 등록 시 자동으로 Setup() 호출
    ):
        # 원본 USER CONFIG 보관.
        self.eth_if = eth_if
        self.th_home = th_home
        self.th_root = th_root
        self.host_ip = host_ip
        self.cvd_br = cvd_br
        self.rbvm_ip = rbvm_ip
        self.th_adb = th_adb
        self.grpc_ip = grpc_ip
        self.python_bin = python_bin
        self.panel_enabled = _as_bool(panel)
        self.panel_trigger = panel_trigger
        self.auto_setup = _as_bool(auto_setup)

        # Setup 결과 추적 (IsConnected 반환에 사용)
        self._setup_done = False
        self._setup_last_msg = ""

        # client.py 위치는 th_home 로부터 자동 도출.
        # th_home 이 비어있으면 self.client_dir 도 빈 문자열에 가까운 잘못된 경로가 되고,
        # Send/SendAndUpdate 호출 시 "FAIL: TH client spawn error" 로 떨어진다.
        self.client_dir = _derive_client_dir(th_home) if th_home else ""
        self.th_addr = grpc_ip

        self._signal = THSignal(
            client_dir=self.client_dir,
            th_addr=self.th_addr,
            python_bin=python_bin,
        )
        self._panel: Optional[PanelClient] = PanelClient() if self.panel_enabled else None
        self._trigger_bytes = panel_trigger.encode("utf-8")

    # ── 자동 호출 (device_manager 가 등록 직후 호출) ──────────
    def Connect(self) -> str:
        """device_manager 가 module 등록 직후 호출.

        auto_setup=True 이면 Setup() 실행 — connect_th.sh step 1-3 (네트워크 + ADB) 등가.
        auto_setup=False 면 setup 을 건너뛰고 즉시 ok (PanelClient 만 lazy 준비).
        """
        if not self.auto_setup:
            logger.info("TH.Connect: auto_setup disabled, skipping setup")
            self._setup_done = True
            self._setup_last_msg = "ok (auto_setup disabled)"
            return self._setup_last_msg
        return self.Setup()

    def IsConnected(self) -> bool:
        """device_manager._is_connected 가 호출. Setup 이 한 번 성공했으면 True."""
        return self._setup_done

    # ── 시나리오 노출: Setup (수동 재실행 가능) ───────────
    def Setup(self) -> str:
        """connect_th.sh step 1-3 등가. 네트워크 셋업 + ADB ensure.

        흐름:
          [1] cvd_br 의 기존 IP 정리 + host_ip 추가 + eth_if 를 bridge 멤버로
          [2] host_ends_setup.sh <eth_if>   (th_root/host_ends_setup.sh 존재 시)
          [3] ensure-adb.sh <eth_if>        (th_root/ensure-adb.sh 존재 시)
          [4] adb devices 검증 — RBVM 가 보이고 디바이스 ≥ 2개

        sudo 명령은 모두 -n (non-interactive) 으로 호출. passwordless sudo 필요.
        설정되지 않은 경우 step 1/2/3 가 즉시 sudo 권한 에러로 FAIL.
        """
        # ── 사전 검증 ─────────────────────────────────────
        if not self.eth_if:
            return self._mark_fail("eth_if not configured (radmoon scan 으로 인터페이스 선택)")
        if not self.th_home:
            return self._mark_fail("th_home not configured")
        if not os.path.isdir(self.th_home):
            return self._mark_fail(f"th_home does not exist: {self.th_home}")
        if not self._iface_exists(self.eth_if):
            return self._mark_fail(f"interface '{self.eth_if}' not found in /sys/class/net/")

        log: list[str] = []

        # ── [1] bridge 네트워크 ──────────────────────────
        rc, msg = self._setup_bridge_network()
        log.append(f"[1] bridge network:\n{msg}")
        if rc != 0:
            return self._mark_fail("bridge network setup", log)

        # ── [2] host_ends_setup.sh ───────────────────────
        rc, msg = self._run_setup_script(
            os.path.join(self.th_root, "host_ends_setup.sh"), [self.eth_if]
        )
        log.append(f"[2] host_ends_setup.sh:\n{msg}")
        if rc != 0:
            return self._mark_fail("host_ends_setup.sh", log)

        # ── [3] ensure-adb.sh ────────────────────────────
        # 사용자 환경 사례: adb 가 이미 RBVM 에 붙어있는데도 ensure-adb.sh 의
        # `adb connect localhost` 폴백이 hang 한 적 있음. 그래서 먼저 한 번 verify 해서
        # 이미 OK 면 스크립트 자체를 건너뛴다.
        adb_rc, adb_msg = self._verify_adb()
        if adb_rc == 0:
            log.append("[3] ensure-adb.sh:\n  skipped — adb already connected to RBVM\n" + adb_msg)
        else:
            rc, msg = self._run_setup_script(
                os.path.join(self.th_root, "ensure-adb.sh"), [self.eth_if]
            )
            log.append(f"[3] ensure-adb.sh:\n{msg}")
            if rc != 0:
                return self._mark_fail("ensure-adb.sh", log)

            # ── [4] ADB 재검증 ───────────────────────────
            rc, msg = self._verify_adb()
            log.append(f"[4] adb verify:\n{msg}")
            if rc != 0:
                return self._mark_fail("adb verification", log)

        self._setup_done = True
        self._setup_last_msg = "ok\n" + "\n".join(log)
        logger.info("TH.Setup ok (eth_if=%s, th_home=%s)", self.eth_if, self.th_home)
        return self._setup_last_msg

    # ── Setup 내부 헬퍼 ───────────────────────────────────
    def _mark_fail(self, stage: str, log: Optional[list] = None) -> str:
        msg = f"FAIL: {stage}"
        if log:
            msg += "\n" + "\n".join(log)
        self._setup_done = False
        self._setup_last_msg = msg
        logger.warning("TH.Setup failed at '%s'", stage)
        return msg

    def _iface_exists(self, name: str) -> bool:
        return os.path.isdir(os.path.join("/sys/class/net", name))

    def _setup_bridge_network(self) -> tuple[int, str]:
        """cvd_br 의 IP 정리 + host_ip 추가 + eth_if 를 bridge 멤버로."""
        msgs: list[str] = []

        # cvd_br 존재 확인 — 없으면 host_ends_setup.sh 가 만드는 경우도 있어 경고만.
        if not self._iface_exists(self.cvd_br):
            msgs.append(f"  warn: bridge '{self.cvd_br}' not present yet; will be created by setup script")

        # 기존 IP 조회 후, host_ip 와 다르면 삭제
        if self._iface_exists(self.cvd_br):
            res = subprocess.run(
                ["ip", "-4", "addr", "show", "dev", self.cvd_br],
                capture_output=True, text=True, timeout=SETUP_IP_TIMEOUT_S,
            )
            for line in res.stdout.splitlines():
                m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)\b", line)
                if not m:
                    continue
                old = m.group(1)
                if old == self.host_ip:
                    msgs.append(f"  {self.host_ip} already on {self.cvd_br}")
                    continue
                rc, out = self._sudo_ip(["addr", "del", old, "dev", self.cvd_br])
                if rc != 0:
                    return rc, "\n".join(msgs + [f"  delete old IP {old} failed: {out}"])
                msgs.append(f"  deleted {old} from {self.cvd_br}")

        # host_ip 추가 (이미 있으면 RTNETLINK answers: File exists — 무시)
        rc, out = self._sudo_ip(["addr", "add", self.host_ip, "dev", self.cvd_br])
        if rc != 0 and "File exists" not in out:
            return rc, "\n".join(msgs + [f"  add {self.host_ip} failed: {out}"])
        msgs.append(f"  added {self.host_ip} to {self.cvd_br}"
                    + (" (already present)" if rc != 0 else ""))

        # eth_if 를 bridge 멤버로
        rc, out = self._sudo_ip(["link", "set", self.eth_if, "master", self.cvd_br])
        if rc != 0:
            return rc, "\n".join(msgs + [f"  set {self.eth_if} master {self.cvd_br} failed: {out}"])
        msgs.append(f"  {self.eth_if} → bridge {self.cvd_br}")

        return 0, "\n".join(msgs)

    def _sudo_ip(self, ip_args: list[str]) -> tuple[int, str]:
        """sudo -n ip <args>. 결과 stdout+stderr 마지막 300자 반환."""
        try:
            res = subprocess.run(
                ["sudo", "-n", "ip", *ip_args],
                capture_output=True, text=True, timeout=SETUP_IP_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, f"timeout running: sudo -n ip {' '.join(ip_args)}"
        except FileNotFoundError:
            return 1, "sudo or ip command not found"
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        return res.returncode, out[-300:] if out else ""

    def _run_setup_script(self, script_path: str, args: list[str]) -> tuple[int, str]:
        """sudo -n bash <script> <args>. 스크립트가 없으면 skip 으로 처리(rc=0)."""
        if not os.path.isfile(script_path):
            return 0, f"  skipped (not found: {script_path})"
        try:
            res = subprocess.run(
                ["sudo", "-n", "bash", script_path, *args],
                capture_output=True, text=True, timeout=SETUP_SCRIPT_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, f"  timeout (>{int(SETUP_SCRIPT_TIMEOUT_S)}s): {script_path}"
        except FileNotFoundError:
            return 1, "  sudo or bash command not found"
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        tail = out[-500:] if out else ""
        if res.returncode != 0:
            return res.returncode, f"  exit {res.returncode}\n{tail}"
        return 0, f"  ok\n{tail}" if tail else "  ok"

    def _verify_adb(self) -> tuple[int, str]:
        """adb devices 출력에서 디바이스 ≥ 2개 + RBVM 가 보이는지 확인."""
        try:
            res = subprocess.run(
                ["adb", "devices"], capture_output=True, text=True, timeout=SETUP_ADB_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, "  timeout running 'adb devices'"
        except FileNotFoundError:
            return 1, "  adb command not found in PATH"
        if res.returncode != 0:
            return res.returncode, f"  adb devices failed: {(res.stderr or '').strip()}"
        # connect_th.sh 와 동일 판정: 헤더 제외, `\tdevice` 있는 행만 카운트
        lines = [
            ln for ln in res.stdout.splitlines()[1:]
            if "\tdevice" in ln and "offline" not in ln
        ]
        has_rbvm = any(self.rbvm_ip in ln for ln in lines)
        diag = f"  devices: {len(lines)}, RBVM seen: {has_rbvm}\n  {res.stdout.strip()[-400:]}"
        if len(lines) < 2:
            return 1, "  fewer than 2 adb devices connected\n" + diag
        if not has_rbvm:
            return 1, f"  RBVM ({self.rbvm_ip}) not in adb devices\n" + diag
        return 0, diag

    # ── 시나리오 노출 ─────────────────────────────────
    def _precheck(self) -> Optional[str]:
        """client.py 호출 전 필수 설정 확인. 문제 있으면 'FAIL: ...' 메시지 반환."""
        if not self.th_home:
            return "FAIL: th_home not configured — set the TH version directory"
        if not os.path.isfile(os.path.join(self.client_dir, "client.py")):
            return f"FAIL: client.py not found at {self.client_dir} — check th_home"
        return None

    def Send(self, topic_name: str, json_path: str, timeout: int = 10) -> str:
        """패널 갱신 없이 신호만 전송.

        Returns:
          정상: "rc=<n>\\n<stdout 마지막 1KB>"
          실패: "FAIL: ..."
        """
        pre = self._precheck()
        if pre is not None:
            return pre
        try:
            sr = self._signal.send(
                topic_name=topic_name,
                json_path=json_path,
                trigger=None,
                on_trigger=None,
                timeout=float(timeout),
            )
        except (OSError, FileNotFoundError) as e:
            return f"FAIL: TH client spawn error: {e}"

        if sr.timed_out:
            return f"FAIL: TH timeout ({timeout}s) rc={sr.rc}"

        return _format_result(sr.rc, None, sr.stdout)

    def SendAndUpdate(
        self,
        topic_name: str,
        json_path: str,
        timeout: int = 10,
        trigger: Optional[str] = None,
    ) -> str:
        """원본 'Send Signal And Update Panel' 등가.

        trigger 가 stdout 에 등장하면 즉시 패널 점등. 패널이 없으면 detection 만.

        Returns:
          매치: "rc=<n> trigger_hit=<tok> e2e_ms=<x.x>"
          미매치: "FAIL: trigger '<tok>' not detected (timeout=<n>s) rc=<rc>"
        """
        pre = self._precheck()
        if pre is not None:
            return pre
        trig_bytes = trigger.encode("utf-8") if trigger else self._trigger_bytes

        def _on_trig(_ts: float) -> None:
            if self._panel is not None:
                self._panel.highlight()

        try:
            sr = self._signal.send(
                topic_name=topic_name,
                json_path=json_path,
                trigger=trig_bytes,
                on_trigger=_on_trig,
                timeout=float(timeout),
            )
        except (OSError, FileNotFoundError) as e:
            return f"FAIL: TH client spawn error: {e}"

        if not sr.trigger_hit:
            tok = trig_bytes.decode("utf-8", "replace")
            return f"FAIL: trigger '{tok}' not detected (timeout={timeout}s) rc={sr.rc}"

        return _format_result(sr.rc, sr.e2e_ms, sr.stdout, trigger_hit=sr.trigger_hit)

    def Info(self) -> str:
        """현재 인스턴스의 설정 요약 (디버그/검증용)."""
        lines = [
            f"eth_if    = {self.eth_if or '(unset)'}",
            f"th_home   = {self.th_home or '(unset)'}",
            f"th_root   = {self.th_root}",
            f"client    = {self.client_dir or '(unset)'}",
            f"host_ip   = {self.host_ip}",
            f"cvd_br    = {self.cvd_br}",
            f"rbvm_ip   = {self.rbvm_ip}",
            f"th_adb    = {self.th_adb}",
            f"grpc_ip   = {self.grpc_ip}",
            f"python    = {self.python_bin}",
            f"panel     = {self.panel_enabled} (trigger='{self.panel_trigger}')",
            f"auto_setup= {self.auto_setup}",
            f"setup_ok  = {self._setup_done}",
        ]
        if self.th_home:
            client_py = os.path.join(self.client_dir, "client.py")
            lines.append(f"client.py exists = {os.path.isfile(client_py)}")
        for script in ("host_ends_setup.sh", "ensure-adb.sh"):
            p = os.path.join(self.th_root, script)
            lines.append(f"{script} exists = {os.path.isfile(p)} ({p})")
        return "\n".join(lines)

    def PanelShow(self) -> str:
        if self._panel is None:
            return "FAIL: panel disabled at construction"
        self._panel.reset()  # show + reset
        return "ok"

    def PanelReset(self) -> str:
        if self._panel is None:
            return "FAIL: panel disabled at construction"
        self._panel.reset()
        return "ok"

    def PanelClose(self) -> str:
        if self._panel is None:
            return "ok"  # nothing to close
        self._panel.close()
        return "ok"


def _format_result(
    rc: Optional[int],
    e2e_ms: Optional[float],
    stdout_bytes: bytes,
    trigger_hit: Optional[bytes] = None,
    tail_bytes: int = 1024,
) -> str:
    parts = [f"rc={rc}"]
    if trigger_hit is not None:
        parts.append(f"trigger_hit={trigger_hit.decode('utf-8', 'replace')}")
    if e2e_ms is not None:
        parts.append(f"e2e_ms={e2e_ms:.2f}")
    header = " ".join(parts)
    tail = stdout_bytes[-tail_bytes:].decode("utf-8", "replace").strip()
    return f"{header}\n{tail}" if tail else header
