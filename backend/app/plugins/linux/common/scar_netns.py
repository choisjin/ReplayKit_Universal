"""SCAR VLAN / netns 네트워크 구성 자동화.

원본 절차:
  Reference/Renault_CDC_Plugin/collab SCAR 설치 guide.pdf — "2. Network Configuration"
    1) sudo ./netns.sh --setup=hil -i <iface> --clean        (기존 구성 정리)
    2) <mode>.json 생성 (multiverse / standalone)
    3) ./netns.sh -c <config>.json                           (구성 적용)
    4) docker exec <container> ip netns                       (네임스페이스 검증)

설계:
  - netns.sh 는 내부적으로 sudo 를 호출한다. clean 은 가이드에서 직접 `sudo ./netns.sh`,
    apply 는 `./netns.sh -c` (스크립트가 내부에서 sudo 프롬프트). 양쪽 모두 **우리가 sudo 래퍼로**
    실행하면 스크립트 내부 sudo 가 캐시된 root 세션을 재사용하므로 추가 프롬프트가 안 뜬다.
  - sudo 호출은 TH.Setup 과 동일 규약: password 있으면 -S(stdin), 없으면 -n(passwordless).
  - JSON 은 vlan_config_dir 에 `replaykit-<mode>.json` 으로 기록 (사용자 작성 파일 보존).

이 모듈은 SCAR 컨테이너/REST 가용성과 무관하게 **호스트 측** 네트워크만 다룬다.
가이드: "Since SCAR is performing only SOME/IP messages, the VLAN configuration must be done
before launching any command of SCAR".
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Optional


logger = logging.getLogger(__name__)


# netns.sh 단계별 timeout
NETNS_CLEAN_TIMEOUT_S = 60.0
NETNS_APPLY_TIMEOUT_S = 180.0
NETNS_VERIFY_TIMEOUT_S = 15.0

# 모드별 stub_ecus 기본값 (가이드 예시 그대로). 폼에서 비워두면 이 값 사용.
DEFAULT_STUB_ECUS = {
    "multiverse": ["PIU_Mst"],
    "standalone": ["PIU_Mst", "PCU_PROXY_FrontEnd", "IVC"],
}


def build_config(
    ends: str,
    iface: str,
    mode: str = "multiverse",
    stub_ecus: Optional[list[str]] = None,
    standalone_ip: str = "192.168.1.10",
    ufw: str = "off",
    log_folder: str = "/tmp",
) -> dict:
    """가이드의 multiverse.json / standalone.json 동등 dict 생성.

    multiverse: vcans=0, net_config 항목에 interface/arp/stub_ecus.
    standalone: ip / stub_groups / conf_type=veth / cuttlefish=true 추가, vcans 없음.
    """
    mode = (mode or "multiverse").strip().lower()
    if mode not in ("multiverse", "standalone"):
        mode = "multiverse"
    ecus = stub_ecus if stub_ecus else list(DEFAULT_STUB_ECUS[mode])

    net_entry: dict = {
        "interface": iface,
        "arp": "on",
        "stub_ecus": ecus,
    }

    if mode == "standalone":
        # interface 다음에 ip 가 오도록 순서 재구성 (가이드 예시 가독성 유지)
        net_entry = {
            "interface": iface,
            "ip": standalone_ip,
            "arp": "on",
            "stub_ecus": ecus,
            "stub_groups": [],
            "conf_type": "veth",
            "cuttlefish": True,
        }
        config = {
            "ends": ends,
            "ufw": ufw,
            "log_folder": log_folder,
            "net_config": [net_entry],
        }
    else:  # multiverse
        config = {
            "ends": ends,
            "ufw": ufw,
            "vcans": 0,
            "log_folder": log_folder,
            "net_config": [net_entry],
        }
    return config


class SCARNetns:
    """sdv_vlan_config (netns.sh) 래퍼 — clean / apply / verify."""

    def __init__(
        self,
        vlan_config_dir: str,
        sudo_password: str = "",
        script_name: str = "netns.sh",
    ):
        self.vlan_config_dir = vlan_config_dir
        self.sudo_password = sudo_password
        self.script_name = script_name

    # ── 경로/검증 ─────────────────────────────────────────
    @property
    def script_path(self) -> str:
        return os.path.join(self.vlan_config_dir, self.script_name)

    def is_available(self) -> bool:
        return bool(self.vlan_config_dir) and os.path.isfile(self.script_path)

    # ── sudo 래퍼 (TH.Setup 과 동일 규약) ─────────────────
    def _sudo_prefix(self) -> list[str]:
        if self.sudo_password:
            return ["sudo", "-S", "-p", ""]
        return ["sudo", "-n"]

    def _sudo_stdin(self) -> Optional[str]:
        return (self.sudo_password + "\n") if self.sudo_password else None

    def _run(self, argv: list[str], timeout: float) -> tuple[int, str]:
        """vlan_config_dir 를 cwd 로 sudo + argv 실행. 마지막 1KB 반환."""
        full = [*self._sudo_prefix(), *argv]
        try:
            res = subprocess.run(
                full,
                input=self._sudo_stdin(),
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.vlan_config_dir,
            )
        except subprocess.TimeoutExpired:
            return 1, f"timeout: {' '.join(argv[:2])} ({timeout}s)"
        except FileNotFoundError:
            return 1, f"sudo or '{argv[0]}' not found"
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        if res.returncode != 0 and any(
            s in out for s in ("a password is required", "Sorry, try again",
                               "incorrect password attempts")
        ):
            return res.returncode, "sudo 인증 실패 — 비밀번호 확인 또는 passwordless sudo 설정 필요"
        return res.returncode, out[-1024:] if out else ""

    # ── [1] clean ────────────────────────────────────────
    def clean(self, iface: str) -> tuple[int, str]:
        """sudo ./netns.sh --setup=hil -i <iface> --clean."""
        return self._run(
            [f"./{self.script_name}", "--setup=hil", "-i", iface, "--clean"],
            timeout=NETNS_CLEAN_TIMEOUT_S,
        )

    # ── [2] config 작성 ──────────────────────────────────
    def write_config(self, config: dict, mode: str) -> tuple[Optional[str], str]:
        """vlan_config_dir/replaykit-<mode>.json 으로 기록. (경로, 메시지)."""
        if not self.vlan_config_dir:
            return None, "vlan_config_dir not set"
        fname = f"replaykit-{mode}.json"
        path = os.path.join(self.vlan_config_dir, fname)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
                f.write("\n")
        except OSError as e:
            return None, f"config write failed: {e}"
        return path, f"wrote {fname}"

    # ── [3] apply ────────────────────────────────────────
    def apply(self, config_path: str) -> tuple[int, str]:
        """sudo ./netns.sh -c <config_path>.

        config_path 가 vlan_config_dir 안이면 파일명만 넘겨도 되지만 절대경로로 안전하게.
        """
        return self._run(
            [f"./{self.script_name}", "-c", config_path],
            timeout=NETNS_APPLY_TIMEOUT_S,
        )

    # ── [4] verify ───────────────────────────────────────
    def verify(self, container: str, expect_ns: Optional[list[str]] = None) -> tuple[int, str]:
        """docker exec <container> ip netns — 네임스페이스가 생성됐는지 확인.

        expect_ns 의 각 항목(예: 'PIU_Mst')에 대해 '<name>ns' 형태가 출력에 보이면 OK.
        expect_ns 가 없으면 'ns' 토큰이 하나라도 있으면 OK.
        """
        try:
            res = subprocess.run(
                ["docker", "exec", container, "ip", "netns"],
                capture_output=True, text=True, timeout=NETNS_VERIFY_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return 1, "timeout: docker exec ip netns"
        except FileNotFoundError:
            return 1, "docker not found"
        out = (res.stdout or "").strip()
        if res.returncode != 0:
            err = (res.stderr or "").strip()
            return res.returncode, f"docker exec failed: {err or out}"
        if not out:
            return 1, "no network namespaces present (ip netns empty)"
        if expect_ns:
            missing = [e for e in expect_ns if f"{e}ns" not in out]
            if missing:
                return 1, f"missing namespaces for {missing}\n{out[-600:]}"
        elif "ns" not in out:
            return 1, f"no 'ns' namespace in output\n{out[-600:]}"
        return 0, out[-600:]
