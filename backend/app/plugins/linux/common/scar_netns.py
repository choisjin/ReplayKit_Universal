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


# multiverse 모드 net_config 슬롯 — 슬롯별로 RAD_Moon 인터페이스를 하나씩 배정한다.
#   DTOOL / OBS_TOOL : netns 없이(arp on) 물리 인터페이스만 배정 (OBS_TOOL 은 cuttlefish=false)
#   PIU_Mst          : veth 브리지(PIU_Mst) + 네임스페이스 생성 → [5] verify 대상
MULTIVERSE_SLOTS = ("DTOOL", "OBS_TOOL", "PIU_Mst")

# standalone 은 IVC 네임스페이스가 항상 필요 — 폼/구등록 값에 빠져 있어도 강제 추가.
STANDALONE_REQUIRED_ECUS = ("IVC",)


def build_config(
    ends: str,
    iface: str,
    mode: str = "multiverse",
    stub_ecus: Optional[list[str]] = None,
    standalone_ip: str = "192.168.1.10",
    ufw: str = "off",
    log_folder: str = "/tmp",
    cuttlefish: bool = True,
    multiverse_ifaces: Optional[dict] = None,
) -> dict:
    """가이드의 multiverse.json / standalone.json 동등 dict 생성.

    multiverse: vcans=0, net_config 는 슬롯(DTOOL/OBS_TOOL/PIU_Mst)별 3개 항목.
      multiverse_ifaces={"DTOOL": iface, "OBS_TOOL": iface, "PIU_Mst": iface} 로 슬롯마다
      RAD_Moon 인터페이스를 배정. 값이 빈 슬롯은 항목을 생략한다(PIU_Mst 는 iface 로 폴백).
      이 모드에서는 cuttlefish 인자를 쓰지 않는다 (사용자 지정 JSON 과 동일하게 — OBS_TOOL 만 false).
      multiverse_ifaces 자체가 없으면(구등록 호환) 종전대로 iface 단일 항목 + stub_ecus.
    standalone: ip / stub_groups / conf_type=veth / cuttlefish=true 추가, vcans 없음.
      stub_ecus 에 IVC 가 없으면 자동 추가(STANDALONE_REQUIRED_ECUS).

    cuttlefish=True 면 net_config 에 "cuttlefish": true 를 넣어 netns.sh 가 cvd-ebr 브리지를
    cuttlefish 용으로 구성/보존하게 한다. 이게 빠진 multiverse 는 clean 이 cvd-ebr 를 flush 한
    뒤 복원하지 않아 TH/cuttlefish(같은 cvd-ebr 사용)의 adb 가 끊긴다 → 이를 막기 위해 둘 다 적용.
    """
    mode = (mode or "multiverse").strip().lower()
    if mode not in ("multiverse", "standalone"):
        mode = "multiverse"
    ecus = stub_ecus if stub_ecus else list(DEFAULT_STUB_ECUS[mode])

    if mode == "standalone":
        for req in STANDALONE_REQUIRED_ECUS:
            if req not in ecus:
                ecus = [*ecus, req]
        # interface 다음에 ip 가 오도록 순서 재구성 (가이드 예시 가독성 유지)
        net_entry = {
            "interface": iface,
            "ip": standalone_ip,
            "arp": "on",
            "stub_ecus": ecus,
            "stub_groups": [],
            "conf_type": "veth",
            "cuttlefish": bool(cuttlefish),
        }
        config = {
            "ends": ends,
            "ufw": ufw,
            "log_folder": log_folder,
            "net_config": [net_entry],
        }
    else:  # multiverse
        entries: list[dict] = []
        if multiverse_ifaces is not None:
            slots = {k: (v or "").strip() for k, v in (multiverse_ifaces or {}).items()}
            if not slots.get("PIU_Mst"):
                slots["PIU_Mst"] = iface  # 주 인터페이스 폴백
            if slots.get("DTOOL"):
                entries.append({
                    "interface": slots["DTOOL"],
                    "arp": "on",
                    "netns": False,
                    "stub_ecus": ["DTOOL"],
                })
            if slots.get("OBS_TOOL"):
                entries.append({
                    "interface": slots["OBS_TOOL"],
                    "arp": "on",
                    "netns": False,
                    "stub_ecus": ["OBS_TOOL"],
                    "cuttlefish": False,
                })
            if slots.get("PIU_Mst"):
                piu = {
                    "interface": slots["PIU_Mst"],
                    "arp": "on",
                    "conf_type": "veth",
                    "bridge_name": "PIU_Mst",
                    "stub_ecus": ["PIU_Mst"],
                }
                # 사용자 지정 multiverse.json 그대로 (2026-08-25): PIU_Mst 항목에 cuttlefish 키를 넣지 않는다.
                # ⚠️ 같은 PC 에서 TH(cuttlefish)를 함께 쓰면 netns clean 이 cvd-ebr 를 flush 한 뒤
                #    복원하지 않아 TH adb 가 끊길 수 있다 — 그 경우 TH 재연결 필요 (standalone/구방식은 종전대로 보존).
                entries.append(piu)
        else:
            # 구등록 호환: 단일 인터페이스 + stub_ecus
            net_entry = {
                "interface": iface,
                "arp": "on",
                "stub_ecus": ecus,
            }
            if cuttlefish:
                net_entry["cuttlefish"] = True
            entries.append(net_entry)
        config = {
            "ends": ends,
            "ufw": ufw,
            "vcans": 0,
            "log_folder": log_folder,
            "net_config": entries,
        }
    return config


def multiverse_verify_ecus(config: dict) -> list[str]:
    """[5] verify 대상 — net_config 중 netns 를 만드는 항목(netns!=False)의 stub_ecus."""
    out: list[str] = []
    for e in config.get("net_config") or []:
        if e.get("netns") is False:
            continue
        out.extend(e.get("stub_ecus") or [])
    return out


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

    # ── [3b] host verify ─────────────────────────────────
    def verify_host(self, expect_ns: Optional[list[str]] = None) -> tuple[int, str]:
        """호스트 `ip netns` — apply 직후 네임스페이스 생성 확인 (예: 'PIU_Mstns (id: 0)').

        expect_ns 의 각 항목에 대해 '<name>ns' 가 출력에 있으면 OK. 없으면 'ns' 토큰 하나면 OK.
        /var/run/netns 목록 조회라 sudo 불필요.
        """
        try:
            res = subprocess.run(["ip", "netns"], capture_output=True, text=True,
                                 timeout=NETNS_VERIFY_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return 1, "timeout: ip netns"
        except FileNotFoundError:
            return 1, "ip not found"
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        if res.returncode != 0:
            return res.returncode, f"ip netns failed: {out}"
        if not out:
            return 1, "no network namespaces present (ip netns empty)"
        if expect_ns:
            missing = [e for e in expect_ns if f"{e}ns" not in out]
            if missing:
                return 1, f"missing namespaces for {missing}\n{out[-600:]}"
        elif "ns" not in out:
            return 1, f"no 'ns' namespace in output\n{out[-600:]}"
        return 0, out[-600:]

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
