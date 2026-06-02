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
import select
import subprocess
import sys
import time
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
# launch_cvd 띄운 후 즉시 죽지 않는지 확인하는 짧은 대기 — 5초 안에 살아있으면 OK 간주.
# 원본 connect_th.sh 의 sleep 40 보다 짧음 — boot 완료는 백그라운드에서 진행되며
# 실제 디바이스 사용 시점에는 이미 부팅 끝나 있을 거라는 전제.
SETUP_CVD_VERIFY_S = 5.0

# th_run_microservice.sh 타임아웃 — 내부 adb wait-for-device + 게이트웨이 기동 + sleep 5 포함.
# CVD 부팅이 늦으면 wait-for-device 에서 대기하므로 넉넉히.
SETUP_MICROSERVICE_TIMEOUT_S = 120.0

# launch_cvd 백그라운드 프로세스 추적 파일
LAUNCH_CVD_LOG = "/tmp/replaykit-launch_cvd.log"
LAUNCH_CVD_PID_FILE = "/tmp/replaykit-launch_cvd.pid"


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
        # ── 권한 ─────────────────────────────────────────
        sudo_password: str = "",                          # 비어있으면 sudo -n (passwordless 필요)
        # ── 플러그인 동작 ─────────────────────────────────
        python_bin: str = "",                             # 빈 값이면 sys.executable (임베드 Python)
        panel = True,                                     # bool 또는 'True'/'False' (UI select)
        panel_trigger: str = DEFAULT_PANEL_TRIGGER,
        auto_setup = True,                                # 등록 시 자동으로 Setup() 호출
        launch_cvd = True,                                # Setup step 5: launch_cvd 자동 spawn
        microservice_gateways: str = "",                  # th_run_microservice.sh 게이트웨이 번호 (공백 구분, 예: 57 89 191 207)
        run_microservice = True,                          # Setup step 6: 게이트웨이 자동 기동
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
        # sudo password — 메모리에만 보관. Info() / 로그에 절대 노출되지 않도록.
        # 입력이 비어 있으면 -n 으로 동작 → passwordless sudo 가 사전에 설정되어 있어야 함.
        self.sudo_password = sudo_password
        # python_bin 이 비어 있으면 sys.executable (현재 ReplayKit 실행 중인 임베드 Python).
        # 임베드 Python 의 site-packages 에 grpc/protobuf 등 client.py 의존성이 사전 설치되어
        # 있으므로 사용자가 시스템 python3 에 별도 설치할 필요 없음.
        self.python_bin = python_bin or sys.executable
        self.panel_enabled = _as_bool(panel)
        self.panel_trigger = panel_trigger
        self.auto_setup = _as_bool(auto_setup)
        self.launch_cvd = _as_bool(launch_cvd)
        self.microservice_gateways = (microservice_gateways or "").strip()
        self.run_microservice = _as_bool(run_microservice)

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
            python_bin=self.python_bin,
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

        # ── [5] launch_cvd (TH server) — 백그라운드 spawn ─
        if self.launch_cvd:
            rc, msg = self._launch_cvd_background()
            log.append(f"[5] launch_cvd:\n{msg}")
            if rc != 0:
                return self._mark_fail("launch_cvd spawn", log)
        else:
            log.append("[5] launch_cvd: skipped (launch_cvd=False)")

        # ── [6] microservice 게이트웨이 (th_run_microservice.sh) ─
        # 게이트웨이가 떠 있어야 브로커에 토픽이 등록됨 → client.py 의 "Topic not found" 회피.
        if self.run_microservice and self.microservice_gateways:
            rc, msg = self._run_microservice()
            log.append(f"[6] microservice:\n{msg}")
            if rc != 0:
                return self._mark_fail("microservice gateways", log)
        else:
            log.append("[6] microservice: skipped (게이트웨이 번호 없음 / 비활성)")

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

        # 기존 IP 조회 후, host_ip 와 다르면 삭제. 이미 있는지 플래그로 기록해서 add 스킵.
        host_ip_already_present = False
        if self._iface_exists(self.cvd_br):
            try:
                res = subprocess.run(
                    ["ip", "-4", "addr", "show", "dev", self.cvd_br],
                    capture_output=True, text=True, timeout=SETUP_IP_TIMEOUT_S,
                )
            except FileNotFoundError:
                return 1, "\n".join(msgs + ["  'ip' command not found in PATH"])
            except subprocess.TimeoutExpired:
                return 1, "\n".join(msgs + [f"  timeout: ip addr show dev {self.cvd_br}"])
            for line in res.stdout.splitlines():
                m = re.search(r"\binet\s+(\d+\.\d+\.\d+\.\d+/\d+)\b", line)
                if not m:
                    continue
                old = m.group(1)
                if old == self.host_ip:
                    host_ip_already_present = True
                    msgs.append(f"  {self.host_ip} already on {self.cvd_br}")
                    continue
                rc, out = self._sudo_ip(["addr", "del", old, "dev", self.cvd_br])
                if rc != 0:
                    return rc, "\n".join(msgs + [f"  delete old IP {old} failed: {out}"])
                msgs.append(f"  deleted {old} from {self.cvd_br}")

        # host_ip 추가 — 이미 있으면 스킵. 없으면 ip addr add 호출.
        # 커널 버전별로 중복 추가 에러 메시지가 다양함: "File exists",
        # "Error: ipv4: Address already assigned.", "RTNETLINK answers" 등 모두 양성으로 처리.
        if host_ip_already_present:
            msgs.append(f"  skip add (already present)")
        else:
            rc, out = self._sudo_ip(["addr", "add", self.host_ip, "dev", self.cvd_br])
            already_msgs = ("File exists", "already assigned", "already exists")
            already = any(s in out for s in already_msgs)
            if rc != 0 and not already:
                return rc, "\n".join(msgs + [f"  add {self.host_ip} failed: {out}"])
            msgs.append(f"  added {self.host_ip} to {self.cvd_br}"
                        + (" (race: already present)" if already else ""))

        # eth_if 를 bridge 멤버로 — 이미 멤버여도 ip link set 은 idempotent (exit 0).
        rc, out = self._sudo_ip(["link", "set", self.eth_if, "master", self.cvd_br])
        if rc != 0:
            return rc, "\n".join(msgs + [f"  set {self.eth_if} master {self.cvd_br} failed: {out}"])
        msgs.append(f"  {self.eth_if} → bridge {self.cvd_br}")

        return 0, "\n".join(msgs)

    def _sudo_argv_prefix(self) -> list[str]:
        """sudo 호출 prefix. password 있으면 -S (stdin 으로 비번 전달), 없으면 -n (passwordless)."""
        if self.sudo_password:
            # -p "" → 프롬프트 텍스트 억제 (stderr 에 안 섞이게)
            return ["sudo", "-S", "-p", ""]
        return ["sudo", "-n"]

    def _sudo_stdin(self) -> Optional[str]:
        """sudo 에 흘릴 stdin. password 있을 때만."""
        return (self.sudo_password + "\n") if self.sudo_password else None

    def _sudo_run(self, cmd: list[str], timeout: float, indent: str = "") -> tuple[int, str]:
        """sudo + cmd 실행. 결과 stdout+stderr 마지막 500자 반환. password 절대 출력 안 함."""
        argv = [*self._sudo_argv_prefix(), *cmd]
        try:
            res = subprocess.run(
                argv,
                input=self._sudo_stdin(),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return 1, f"{indent}timeout: sudo {' '.join(cmd[:2])}"
        except FileNotFoundError:
            return 1, f"{indent}sudo or '{cmd[0]}' command not found"
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        # sudo 인증 실패 시 ("Sorry, try again." 또는 "a password is required") 명확한 에러로
        if res.returncode != 0 and any(
            s in out for s in ("a password is required", "Sorry, try again",
                               "incorrect password attempts")
        ):
            return res.returncode, f"{indent}sudo 인증 실패 — 비밀번호 확인 또는 passwordless sudo 설정 필요"
        return res.returncode, out[-500:] if out else ""

    def _sudo_ip(self, ip_args: list[str]) -> tuple[int, str]:
        """sudo ip <args>."""
        return self._sudo_run(["ip", *ip_args], timeout=SETUP_IP_TIMEOUT_S)

    def _run_setup_script(self, script_path: str, args: list[str]) -> tuple[int, str]:
        """sudo bash <script> <args>. 스크립트가 없으면 skip 으로 처리(rc=0)."""
        if not os.path.isfile(script_path):
            return 0, f"  skipped (not found: {script_path})"
        rc, out = self._sudo_run(
            ["bash", script_path, *args],
            timeout=SETUP_SCRIPT_TIMEOUT_S,
            indent="  ",
        )
        if rc != 0:
            return rc, f"  exit {rc}\n{out}"
        return 0, f"  ok\n{out}" if out else "  ok"

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

    # ── microservice 게이트웨이 기동 ──────────────────────
    def _run_microservice(self) -> tuple[int, str]:
        """th_run_microservice.sh 를 '사람처럼' 비대화형 기동 (번호 입력 그대로).

        사용자 수동: 메뉴가 다 뜬 뒤 'Enter your choices:' 에 번호(예: 57 89 191 207)를
        타이핑하면 정상 동작한다. 자동화에서 stdin 을 일반 PIPE 로 주면 두 가지가 깨진다:
          1) 번호를 미리 부어두면, 메뉴 read 이전에 실행되는
             `adb shell "...ls grpc_*_gateway"`(17행)가 파이프의 번호를 먼저 소비해버려
             read 가 빈 값을 받아 "No service selected" 로 끝난다.
          2) 번호를 안 부어두면 메뉴는 뜨지만, bash `read -p` 의 프롬프트는 stdin 이
             '터미널일 때만' 출력되므로 PIPE 에서는 'Enter your choices:' 가 아예 안 나와
             프롬프트 감지가 불가능하다(→ timeout).

        → 그래서 가짜 터미널(pty)을 자식에게 붙여 사람의 터미널을 그대로 재현한다.
          이러면 (1) adb 가 stdin 을 가로채지 않고(사람과 동일) (2) read -p 프롬프트가
          정상 출력된다. 출력에서 'Enter your choices:' 를 본 '뒤에' 번호를 주입한다.
        sudo 불필요. 내부 adb wait-for-device 대비 timeout 필요. (pty → Linux 전용)
        """
        import pty

        th_script_dir = os.path.join(self.th_home, "harness", "harness", "th_script")
        script = os.path.join(th_script_dir, "th_run_microservice.sh")
        if not os.path.isfile(script):
            return 1, f"  th_run_microservice.sh not found at {script}"

        # 번호 검증 — 공백 구분 정수만 허용 (오타로 엉뚱한 입력 방지).
        nums = self.microservice_gateways.split()
        if not all(n.isdigit() for n in nums):
            return 1, f"  게이트웨이 번호는 공백 구분 숫자여야 함: '{self.microservice_gateways}'"

        answer = (" ".join(nums) + "\n").encode()
        needle = b"Enter your choices:"
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["bash", script, self.th_adb],
                stdin=slave, stdout=slave, stderr=slave,
                cwd=th_script_dir, close_fds=True,
            )
        except FileNotFoundError:
            os.close(master); os.close(slave)
            return 1, "  bash not found"
        os.close(slave)                            # 부모는 slave 미사용 — master 로만 입출력

        buf = b""
        sent = False
        start = time.monotonic()
        try:
            while True:
                if time.monotonic() - start > SETUP_MICROSERVICE_TIMEOUT_S:
                    proc.kill()
                    tail = buf[-1000:].decode(errors="replace")
                    return 1, (f"  timeout ({SETUP_MICROSERVICE_TIMEOUT_S}s) — 프롬프트 미도달 또는 "
                               f"adb wait-for-device 막힘 (CVD({self.th_adb}) 부팅/연결 확인)\n{tail}")
                r, _, _ = select.select([master], [], [], 0.5)
                if r:
                    try:
                        chunk = os.read(master, 4096)
                    except OSError:
                        break                      # 자식 종료 시 pty 는 EIO 를 던짐
                    if not chunk:
                        break
                    buf += chunk
                    if not sent and needle in buf:
                        # 사람처럼 — 프롬프트가 뜬 '뒤에' 번호 주입
                        os.write(master, answer)
                        sent = True
                elif proc.poll() is not None:
                    break                          # 프로세스 종료 + 더 읽을 것 없음
            proc.wait(timeout=10)
        except Exception as e:
            proc.kill()
            return 1, f"  microservice 실행 오류: {e}"
        finally:
            try:
                os.close(master)
            except OSError:
                pass

        out = buf.decode(errors="replace").strip()
        tail = out[-1000:]
        if not sent:
            return 1, f"  'Enter your choices:' 프롬프트 미도달 — 번호 주입 실패\n{tail}"
        if "Invalid choice" in out or "No service selected" in out:
            return 1, f"  게이트웨이 번호 오류 (메뉴 범위 밖이거나 디바이스 목록과 불일치)\n{tail}"
        if "Script execution is completed." not in out:
            return 1, f"  게이트웨이 기동 미확인 (rc={proc.returncode})\n{tail}"
        return 0, f"  ok — gateways [{self.microservice_gateways}] 기동\n{tail}"

    # ── launch_cvd 백그라운드 ─────────────────────────────
    def _launch_cvd_background(self) -> tuple[int, str]:
        """connect_th.sh [4] 등가. <th_home>/bin/launch_cvd 를 sudo + setsid 로 spawn.

        - 이미 떠 있으면 skip (PID 파일 + /proc cmdline 확인)
        - start_new_session=True 로 parent (uvicorn) 종료해도 살아있음
        - 5초 안에 죽으면 (sudo 인증 실패 등) FAIL 로 보고
        - 5초 살아있으면 OK — 실제 boot 은 백그라운드에서 진행
        """
        launch_bin = os.path.join(self.th_home, "bin", "launch_cvd")
        if not os.path.isfile(launch_bin):
            return 1, f"  launch_cvd not found at {launch_bin}"

        existing = self._cvd_running_pid()
        if existing is not None:
            return 0, f"  launch_cvd already running (pid={existing})"

        # 원본 명령:
        #   sudo HOME=$PWD ANDROID_HOST_OUT=$PWD ./bin/launch_cvd \
        #     -report_anonymous_usage_stats=n -guest-enforce-security=false \
        #     --extra_bootconfig_args="androidboot.selinux=permissive androidboot.sdv.authz.enable=false"
        # sudo -E 로 env 보존 + 명시적 env 변수.
        sudo_argv = [
            *self._sudo_argv_prefix(),
            "-E",
            f"HOME={self.th_home}",
            f"ANDROID_HOST_OUT={self.th_home}",
            launch_bin,
            "-report_anonymous_usage_stats=n",
            "-guest-enforce-security=false",
            "--extra_bootconfig_args=androidboot.selinux=permissive androidboot.sdv.authz.enable=false",
        ]

        try:
            log_f = open(LAUNCH_CVD_LOG, "ab")
        except OSError as e:
            return 1, f"  cannot open log {LAUNCH_CVD_LOG}: {e}"

        try:
            proc = subprocess.Popen(
                sudo_argv,
                stdin=subprocess.PIPE,
                stdout=log_f,
                stderr=subprocess.STDOUT,
                cwd=self.th_home,
                start_new_session=True,    # parent (uvicorn) 종료해도 살아있음
            )
        except (OSError, FileNotFoundError) as e:
            log_f.close()
            return 1, f"  launch_cvd spawn failed: {e}"
        finally:
            # log_f 의 close 는 Popen 이 fd 를 복제했으므로 OK.
            try:
                log_f.close()
            except OSError:
                pass

        # password 전달 (있을 때만)
        if self.sudo_password and proc.stdin is not None:
            try:
                proc.stdin.write((self.sudo_password + "\n").encode())
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass

        # PID 저장 — sudo 의 PID 이지만 _cvd_running_pid 가 /proc cmdline 으로 launch_cvd 검증
        try:
            with open(LAUNCH_CVD_PID_FILE, "w") as pf:
                pf.write(str(proc.pid))
        except OSError:
            pass

        # 짧게 polling — 즉시 죽으면 (sudo 인증 실패 / 실행 권한 등) 즉시 FAIL
        import time as _t
        deadline = _t.monotonic() + SETUP_CVD_VERIFY_S
        while _t.monotonic() < deadline:
            _t.sleep(0.2)
            if proc.poll() is not None:
                tail = self._read_log_tail(LAUNCH_CVD_LOG, 800)
                return proc.returncode or 1, (
                    f"  launch_cvd died immediately (rc={proc.returncode})\n"
                    f"  log tail:\n{tail}"
                )

        return 0, (
            f"  launch_cvd spawned (pid={proc.pid}), booting in background\n"
            f"  log: {LAUNCH_CVD_LOG}\n"
            f"  CVD adb {self.th_adb} should become available in ~40s"
        )

    def _cvd_running_pid(self) -> Optional[int]:
        """launch_cvd 가 살아있으면 그 PID, 아니면 None.

        PID 파일이 있으면 그것부터 확인 (cmdline 으로 launch_cvd 인지 검증해서 PID 재사용 방어).
        없으면 pgrep -f launch_cvd 폴백.
        """
        try:
            with open(LAUNCH_CVD_PID_FILE) as f:
                pid = int(f.read().strip())
            os.kill(pid, 0)  # alive check (signal 0 = no-op, OSError 면 사망)
            # 같은 pid 인지 cmdline 으로 검증 — PID 재사용 방지
            try:
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmdline = f.read()
                if b"launch_cvd" in cmdline:
                    return pid
            except OSError:
                pass
        except (OSError, ValueError):
            pass
        # pgrep 폴백 — pid 파일이 없거나 stale 한 경우
        try:
            res = subprocess.run(
                ["pgrep", "-f", "launch_cvd"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                return int(res.stdout.strip().split("\n")[0])
        except (subprocess.TimeoutExpired, FileNotFoundError, ValueError):
            pass
        return None

    def _read_log_tail(self, path: str, n: int = 500) -> str:
        try:
            with open(path, "rb") as f:
                return f.read()[-n:].decode("utf-8", "replace")
        except OSError as e:
            return f"(log read error: {e})"

    # ── launch_cvd 시나리오 노출 메서드 ───────────────
    def Launch(self) -> str:
        """connect_th.sh [4] launch_cvd 만 수동 실행. Setup 의 step 5 와 동일.

        Setup 전체를 다시 안 돌리고 cvd 서버만 띄우고 싶을 때 (또는 사망 후 재기동).
        """
        if not self.th_home:
            return "FAIL: th_home not configured"
        rc, msg = self._launch_cvd_background()
        return msg if rc == 0 else "FAIL: launch_cvd\n" + msg

    def StopCvd(self) -> str:
        """현재 떠 있는 launch_cvd 를 종료. SIGTERM → 3초 → SIGKILL."""
        pid = self._cvd_running_pid()
        if pid is None:
            return "ok (launch_cvd not running)"

        # launch_cvd 는 sudo 로 띄워서 root 권한. kill 도 sudo 필요.
        import signal as _sig
        import time as _t
        try:
            rc, msg = self._sudo_run(
                ["kill", "-TERM", str(pid)],
                timeout=5.0,
            )
        except Exception as e:
            return f"FAIL: kill TERM failed: {e}"
        if rc != 0:
            return f"FAIL: kill TERM rc={rc}\n{msg}"

        # 3초 대기 후 alive 면 SIGKILL
        for _ in range(15):  # 3초 / 0.2초
            _t.sleep(0.2)
            try:
                os.kill(pid, 0)
            except OSError:
                # 죽었음
                self._cleanup_cvd_pid_file()
                return f"ok (terminated pid={pid})"
        # 아직 살아있음 → SIGKILL
        self._sudo_run(["kill", "-KILL", str(pid)], timeout=5.0)
        self._cleanup_cvd_pid_file()
        return f"ok (killed pid={pid} after grace)"

    def CvdStatus(self) -> str:
        """launch_cvd 가 떠 있는지 + 로그 마지막 몇 줄."""
        pid = self._cvd_running_pid()
        tail = self._read_log_tail(LAUNCH_CVD_LOG, 400) if os.path.isfile(LAUNCH_CVD_LOG) else "(no log)"
        if pid is None:
            return f"not running\nlog tail:\n{tail}"
        return f"running pid={pid}\nlog tail:\n{tail}"

    def _cleanup_cvd_pid_file(self) -> None:
        try:
            os.unlink(LAUNCH_CVD_PID_FILE)
        except OSError:
            pass

    # ── 시나리오 노출 ─────────────────────────────────
    def _precheck(self) -> Optional[str]:
        """client.py 호출 전 필수 설정 확인. 문제 있으면 'FAIL: ...' 메시지 반환."""
        if not self.th_home:
            return "FAIL: th_home not configured — set the TH version directory"
        if not os.path.isfile(os.path.join(self.client_dir, "client.py")):
            return f"FAIL: client.py not found at {self.client_dir} — check th_home"
        return None

    def _precheck_signal(self, topic_name: str, json_path: str) -> Optional[str]:
        """Send/SendAndUpdate 인자까지 검증. client.py 가 빈 인자에도 rc=0 으로 끝나
        거짓 PASS 가 나던 문제를 호출 전에 차단한다."""
        pre = self._precheck()
        if pre is not None:
            return pre
        if not topic_name or not str(topic_name).strip():
            return "FAIL: topic_name 이 비어 있음"
        if not json_path or not str(json_path).strip():
            return "FAIL: json_path 가 비어 있음 — payload JSON 파일 경로 필요"
        # client.py 는 cwd=client_dir 에서 실행되므로 상대경로(예: ../generated_json/X.json)는
        # client_dir 기준으로 해석해야 한다. 백엔드 cwd 기준으로 보면 안 됨.
        check_path = json_path if os.path.isabs(json_path) else os.path.join(self.client_dir, json_path)
        # exists 사용: 실제 .json + /dev/null(테스트) 모두 허용, 빈 문자열·오타 경로만 차단.
        if not os.path.exists(check_path):
            return f"FAIL: json_path 경로 없음: {json_path} (client_dir 기준 해석: {check_path})"
        return None

    @staticmethod
    def _coerce_timeout(timeout, default: float = 10.0) -> float:
        """timeout 을 안전하게 float 으로. 비숫자/음수면 default — ValueError 크래시 방지."""
        try:
            t = float(timeout)
        except (TypeError, ValueError):
            return default
        return t if t > 0 else default

    def Send(self, topic_name: str, json_path: str, timeout: int = 10) -> str:
        """패널 갱신 없이 신호만 전송.

        Returns:
          정상: "rc=<n>\\n<stdout 마지막 1KB>"
          실패: "FAIL: ..."
        """
        pre = self._precheck_signal(topic_name, json_path)
        if pre is not None:
            return pre
        to = self._coerce_timeout(timeout)
        try:
            sr = self._signal.send(
                topic_name=topic_name,
                json_path=json_path,
                trigger=None,
                on_trigger=None,
                timeout=to,
            )
        except Exception as e:
            return f"FAIL: TH client spawn error: {type(e).__name__}: {e}"

        if sr.timed_out:
            return f"FAIL: TH timeout ({to}s) rc={sr.rc}"

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
        pre = self._precheck_signal(topic_name, json_path)
        if pre is not None:
            return pre
        to = self._coerce_timeout(timeout)
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
                timeout=to,
            )
        except Exception as e:
            return f"FAIL: TH client spawn error: {type(e).__name__}: {e}"

        tok = trig_bytes.decode("utf-8", "replace")
        # stdout 의 브로커 오류(Topic not found 등) 를 trigger 미검출보다 먼저 보고 — 더 정확한 사유.
        full = sr.stdout.decode("utf-8", "replace")
        for marker in ("Topic not found", "Unexpected error has occurred"):
            if marker in full:
                return f"FAIL: TH client error — {marker} (rc={sr.rc})\n{full[-1024:].strip()}"
        if sr.timed_out:
            return f"FAIL: TH timeout ({to}s) — trigger '{tok}' not seen rc={sr.rc}"
        if not sr.trigger_hit:
            return f"FAIL: trigger '{tok}' not detected (timeout={to}s) rc={sr.rc}"

        return _format_result(sr.rc, sr.e2e_ms, sr.stdout, trigger_hit=sr.trigger_hit)

    def Info(self) -> str:
        """현재 인스턴스의 설정 요약 (디버그/검증용). sudo 비밀번호는 *** 로 마스킹."""
        sudo_state = f"(set, {len(self.sudo_password)} chars)" if self.sudo_password else "(unset → -n)"
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
            f"launch_cvd= {self.launch_cvd}",
            f"microsvc  = {self.run_microservice} gateways=[{self.microservice_gateways or '(none)'}]",
            f"sudo_pw   = {sudo_state}",
            f"setup_ok  = {self._setup_done}",
            f"cvd_pid   = {self._cvd_running_pid() or '(not running)'}",
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

    # client.py 는 토픽 미발견/내부 오류에도 rc=0 으로 종료한다 → rc 만으로는 성공 판정 불가.
    # stdout 의 명확한 실패 마커를 감지해 "FAIL:" 로 보고 (시나리오가 PASS 로 오인하지 않도록).
    full = stdout_bytes.decode("utf-8", "replace")
    for marker in ("Topic not found", "Unexpected error has occurred"):
        if marker in full:
            return f"FAIL: TH client error — {marker} ({header})\n{tail}"

    return f"{header}\n{tail}" if tail else header
