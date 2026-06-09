"""ICAS Agent Service — SSH 기반 VW ICAS HU 제어.

References/RemoteController.py, Control_Lib.py 를 바탕으로 HKMC6thService와
동일한 async API 계약을 제공한다.

지원 범위 (MVP):
  - Touch: tap / swipe / long_press / repeat_tap
  - Hardkey: VOLUME_UP, VOLUME_DOWN, MUTE, PTT, HOME, POWER (6개)
  - Screenshot: HU (LayerManagerControl dump + SCP pull)
  - Screen type: HU (향후 IID/HUD 확장 예정)

좌표 인코딩 (RemoteController.excutecmdTouch* 동일):
  x' = round(x / X_MULT), y' = round(y / Y_MULT)
  X_MULT = int(res_x / 1023) + 1, Y_MULT = int(res_y / 1023) + 1
  param1 = 0xFF & ((x' >> 6) + 0x10)
  param2 = ((x' >> 2 & 0xF) << 4) + ((x' << 2) & 0xC) + int(y' / 255)
  param3 = 0xFF & (y' % 255)
  end byte: 0xFD(press) / 0xFE(drag) / 0xFF(release)
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# ── 하드키 서브 커맨드 (HKMC6thService API 호환용 — 내부적으로 press/release 구분) ──
SHORT_KEY = 0x43
LONG_KEY = 0x44
PRESS_KEY = 0x41
RELEASE_KEY = 0x42


# ── ICAS 하드키 테이블 ──
# class: "short" (13B 프레임) / "long" (15B 프레임)
# key: KEY_CODE 바이트 (ksend 프레임의 키 위치)
ICAS_KEYS: dict[str, dict] = {
    "VOLUME_UP":   {"class": "short", "key": 0x10},
    "VOLUME_DOWN": {"class": "short", "key": 0x11},
    "MUTE":        {"class": "short", "key": 0x20},
    "HOME":        {"class": "long",  "key": 0x66},
    "POWER":       {"class": "long",  "key": 0x38},
}


def _encode_touch_xy(x: int, y: int, x_mult: int, y_mult: int) -> tuple[int, int, int]:
    """Touch 좌표를 ksend param1/param2/param3 바이트로 인코딩."""
    x2 = int(round(float(x) / max(1, x_mult)))
    y2 = int(round(float(y) / max(1, y_mult)))
    y_layer = int(y2 / 255)
    param1 = 0xFF & ((x2 >> 6) + 0x10)
    param2 = ((x2 >> 2 & 0xF) << 4) + ((x2 << 2) & 0xC) + y_layer
    param3 = 0xFF & (y2 % 255)
    return param1, param2, param3


def _encode_touch_xy_icas3(px: int, py: int) -> tuple[int, int, int]:
    """ICAS3 CN 변종 touch 좌표 인코딩 (touch_event.sh 참조).

    EU 변종(_encode_touch_xy)과 인코딩 공식이 다름:
      byte3 = ((px >> 6) & 0xff) + 0x10            ← EU와 동일
      byte4 = ((px & 0x3F) << 2) + ((py >> 8) & 0x3)   ← 다름
      byte5 = py & 0xff                             ← 다름

    호출자는 이미 X_MULT/Y_MULT로 분주된 px/py를 넘긴다.
    """
    px = int(px)
    py = int(py)
    byte3 = 0xFF & ((px >> 6) + 0x10)
    byte4 = 0xFF & (((px & 0x3F) << 2) + ((py >> 8) & 0x3))
    byte5 = py & 0xFF
    return byte3, byte4, byte5


def _encode_image(pil_image, fmt: str) -> bytes:
    """PIL Image → PNG/JPEG 바이트."""
    buf = io.BytesIO()
    if (fmt or "png").lower() == "jpeg":
        pil_image.convert("RGB").save(buf, format="JPEG", quality=85)
    else:
        pil_image.save(buf, format="PNG")
    return buf.getvalue()


def _rm_tree(path: str) -> None:
    try:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


def _validate_png_file(path: str) -> bool:
    """PNG 파일이 시그니처 + IEND chunk를 모두 갖춘 완전한 파일인지 빠르게 검증.

    PIL.Image.open의 lazy load는 IEND 부재 등 일부 손상에 무관심하지만 .convert('RGBA')에서
    실제 디코딩이 일어나며 chunk 경계 깨짐을 만나면 실패. SCP 결과를 사용 전 미리 거르기 위함.
    """
    try:
        size = os.path.getsize(path)
        if size < 16:
            return False
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig != b"\x89PNG\r\n\x1a\n":
                return False
            # IEND chunk가 파일 정확히 끝에 오는 게 정상이지만, 일부 LayerManagerControl dump
            # 구현은 IEND 뒤에 패딩/잔여 바이트를 남긴다. 끝에서 일정 구간을 뒤져 IEND 존재를 확인
            # (정확히 마지막 12바이트만 보면 멀쩡한 PNG도 truncated로 오판 → 캡처 전체 폐기되어
            # 'No HU screenshot captured'가 무한 반복되고 backoff 재연결이 폭주함).
            scan = min(size, 4096)
            f.seek(-scan, 2)
            tail = f.read(scan)
            if b"IEND" not in tail:
                return False
        return True
    except Exception:
        return False


class ICASAgentService:
    """SSH 기반 ICAS HU 제어 서비스.

    HKMC6thService와 동일한 async API를 제공하여 playback_service가
    동일한 step 타입(hkmc_touch/hkmc_swipe/hkmc_key)을 그대로 디스패치할 수 있게 한다.
    """

    default_screen = "HU"

    def __init__(self, host: str, port: int = 22, device_id: str = "",
                 username: str = "root", password: str = "",
                 resolution: str = "1560x700",
                 private_server_ip: str = "",
                 private_server_password: str = "",
                 iid_display: str = "10",
                 hud_display: str = "11",
                 market: str = "EU",
                 variant: str = "icas",
                 key_overrides: Optional[dict[str, dict]] = None):
        self.host = host
        self.port = int(port)
        self.device_id = device_id or f"ICAS_{host}"
        self.username = username
        self.password = password or ""
        # variant — ksend frame/encoding 변종 식별자
        #   "icas"  : 기존 EU 등 13B touch frame
        #   "icas3" : 16B touch frame + 다른 좌표 인코딩 (touch_event.sh 패턴)
        # resolution 파싱·market 분기 모두 variant 영향 받으므로 가장 먼저 셋업.
        self.variant = (variant or "icas").lower()
        self._resolution = resolution.upper()
        self._parse_resolution()
        # market 분기 (RemoteController.py 라인 63-75 참조)
        # EU/NAR/CN: legacy 주소 + IPv6 private server
        # GP(KR): 숫자 주소 + IPv4 private server
        # ICAS3 variant: market 무관하게 src=57/dst=43 (정수 kipc id) 강제
        self.market = (market or "EU").upper()
        self._apply_market_defaults(self.market, private_server_ip)
        self.private_server_password = private_server_password
        self.iid_display = str(iid_display or "10")
        self.hud_display = str(hud_display or "11")

        self._connected = False
        self.agent_version = "ICAS Agent"
        # 공유 SSH 세션 — 액션마다 재연결하지 않고 keep-alive로 재사용하여 인증 오버헤드(80ms/call) 제거.
        # 터치/하드키/스크린샷 등 모든 액션이 동일 클라이언트를 공유하므로 _ssh_lock으로 직렬화.
        self._ssh_client = None
        self._ssh_shell = None  # 장수명 invoke_shell 채널 — ksend 등 fire-and-forget 명령용
        self._ssh_lock = threading.RLock()
        # keepalive 간격 — sshd ClientAliveInterval(보통 15s)보다 짧게 설정해야 양방향 idle timeout 방지.
        # IVI 환경은 load가 항시 1.0+ 수준이라 응답이 늦어 disconnect로 오판될 수 있어 10s로.
        self._ssh_keepalive_interval = 10
        # IID/HUD 캡처 — private_server로의 direct-tcpip 터널 + SSH 클라이언트도 장수명 캐시.
        # 매 프레임마다 paramiko.connect() 인증(~300-500ms)을 반복하지 않도록.
        self._ps_ssh = None
        self._ps_tunnel_chan = None
        self._ps_lock = threading.RLock()
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})
        # HU 캡처: ICAS EU / ICAS3 CN 공통으로 screen 0(디바이스 화면) + screen 2(navigation map)를
        # alpha 합성해 최종 화면을 만든다. 분기 없이 두 screen 항상 dump.
        # 연속 실패 backoff — ClientDisconnected가 폭주할 때 sshd 회복 시간을 주기 위함.
        # _consecutive_failures가 임계치 넘으면 _get_shared_ssh에서 짧은 sleep 후 재연결 시도.
        self._consecutive_capture_failures = 0
        self._consecutive_capture_failures_threshold = 3

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------
    def _parse_resolution(self) -> None:
        try:
            rx, ry = self._resolution.upper().split("X")
            self._res_x = int(rx)
            self._res_y = int(ry)
        except Exception:
            # ICAS3 CN 기본 해상도(2240x1260) — EU 기본은 1560x700
            if getattr(self, "variant", "icas") == "icas3":
                self._res_x, self._res_y = 2240, 1260
            else:
                self._res_x, self._res_y = 1560, 700
        # ICAS3 CN(touch_event.sh)은 해상도 크기에 따라 X/3 Y/2 (15") 또는 X/2 Y/1 (10") 분주.
        # 2240x1260 같은 ≥15" 등급은 X/3 Y/2 — 우연히 EU 공식 (int(res/1023)+1) 과 같은 값.
        # 그래도 식을 명시적으로 분기해두면 향후 다른 inch 변종 추가 시 명확.
        if getattr(self, "variant", "icas") == "icas3":
            if self._res_x >= 1800:  # 15-inch 등급 (2240x1260 등)
                self._x_mult, self._y_mult = 3, 2
            else:                    # 10-inch 등급 추정
                self._x_mult, self._y_mult = 2, 1
        else:
            self._x_mult = int(self._res_x / 1023) + 1
            self._y_mult = int(self._res_y / 1023) + 1

    @property
    def resolution(self) -> str:
        return self._resolution

    @resolution.setter
    def resolution(self, value: str) -> None:
        self._resolution = value.upper()
        self._parse_resolution()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def set_addr(self, src: str, dst: str) -> None:
        """src/dst ksend 주소 변경 (EU/NAR/CN/GP 분기)."""
        self.src_addr = src
        self.dst_addr = dst

    def _apply_market_defaults(self, market: str, private_server_ip_override: str = "") -> None:
        """market 값에 따라 ksend src/dst 주소 + private_server_ip 기본값 설정.

        RemoteController.py 라인 63-75 참조:
          EU/NAR/CN: legacy — src=0x200000000000000, dst=0x80000000000, private=IPv6
          GP (그 외): src=57, dst=43, private=IPv4 192.168.0.2
        private_server_ip_override가 비어있지 않으면 그 값을 그대로 사용.
        """
        m = (market or "EU").upper()
        # ICAS3 variant: ksend kipc id가 정수형(-s 57 -d 43)만 허용 — market 무관하게 강제.
        # private_server_ip 기본값은 GP(IPv4) 사용.
        if getattr(self, "variant", "icas") == "icas3":
            self.src_addr = "57"
            self.dst_addr = "43"
            default_private = "192.168.0.2"
        elif m in ("EU", "NAR", "CN"):
            self.src_addr = "0x200000000000000"
            self.dst_addr = "0x80000000000"
            default_private = "fd53:7cb8:383:3::73"
        else:
            self.src_addr = "57"
            self.dst_addr = "43"
            default_private = "192.168.0.2"
        self.private_server_ip = private_server_ip_override or default_private

    def set_market(self, market: str, private_server_ip_override: str = "") -> None:
        """런타임 market 전환 (addr + private_server_ip 동시 갱신)."""
        self.market = (market or "EU").upper()
        self._apply_market_defaults(self.market, private_server_ip_override)

    # ------------------------------------------------------------------
    # Connection (SSH check)
    # ------------------------------------------------------------------
    def _new_ssh(self):
        """새 paramiko SSHClient 생성 및 연결 (IID/HUD hop 등 일회성 용도).

        공유 세션이 필요한 경우는 `_get_shared_ssh()`를 사용할 것.
        """
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        # IVI 환경(load 1.0+ 상시)에서 connect/banner/auth 응답이 늦어지는 경우가 잦아 timeout 넉넉히.
        ssh.connect(self.host, username=self.username, port=self.port,
                    password=self.password, timeout=20,
                    banner_timeout=20, auth_timeout=20,
                    look_for_keys=False, allow_agent=False)
        return ssh

    def _is_ssh_alive(self, ssh) -> bool:
        """paramiko SSHClient의 transport 활성 여부 체크."""
        if ssh is None:
            return False
        try:
            t = ssh.get_transport()
            return bool(t and t.is_active() and t.is_authenticated())
        except Exception:
            return False

    def _get_shared_ssh(self):
        """공유 SSH 세션 반환 — 끊어졌으면 재연결.

        락 안에서 호출해야 함. 최초 호출 시 새로 연결하고,
        transport가 dead면 닫고 재생성. keep-alive를 설정해 일정 주기마다 NO-OP 프레임을 보내
        방화벽/NAT TCP idle timeout으로 끊어지는 것을 방지.
        """
        if self._is_ssh_alive(self._ssh_client):
            return self._ssh_client
        # 죽은 세션 정리
        if self._ssh_client is not None:
            try:
                self._ssh_client.close()
            except Exception:
                pass
            self._ssh_client = None
        # 공유 shell도 dead SSH와 함께 폐기
        if self._ssh_shell is not None:
            try:
                self._ssh_shell.close()
            except Exception:
                pass
            self._ssh_shell = None
        # 새 연결
        ssh = self._new_ssh()
        try:
            t = ssh.get_transport()
            if t is not None:
                t.set_keepalive(self._ssh_keepalive_interval)
        except Exception:
            pass
        self._ssh_client = ssh
        return ssh

    def _get_shared_shell(self):
        """공유 interactive shell 채널 반환 — 죽었으면 새로 오픈하고 초기 배너를 드레인.

        ksend 등 fire-and-forget 명령은 exec_command(채널 당 open_session=sshd MaxSessions 소모)
        대신 단일 shell 채널에 `shell.send(cmd + "\\n")` 으로 보낸다.
        레퍼런스 구현과 동일한 패턴이며, sshd 세션 한도를 소모하지 않아 장기간 안정.
        """
        ssh = self._get_shared_ssh()
        if self._ssh_shell is not None:
            try:
                if not self._ssh_shell.closed:
                    return self._ssh_shell
            except Exception:
                pass
            # 죽은 shell 정리
            try:
                self._ssh_shell.close()
            except Exception:
                pass
            self._ssh_shell = None
        # 새 shell 오픈 + 초기 배너/프롬프트 드레인
        shell = ssh.invoke_shell()
        shell.settimeout(0.5)
        # 초기 프롬프트가 나올 때까지 최대 1s 드레인
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                if shell.recv_ready():
                    shell.recv(65536)
                else:
                    time.sleep(0.05)
            except Exception:
                break
        self._ssh_shell = shell
        return shell

    def _drain_shell(self, shell, max_bytes: int = 65536) -> bytes:
        """공유 shell의 수신 버퍼를 non-blocking으로 비움 (pipe 백프레셔 방지)."""
        buf = b""
        try:
            while shell.recv_ready() and len(buf) < max_bytes:
                chunk = shell.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except Exception:
            pass
        return buf

    def _shell_run(self, commands: list[str], post_sleep_s: float = 0.02) -> None:
        """공유 shell 채널로 명령 여러 개 송신 + drain. transport/shell dead면 1회 리셋 재시도.

        각 명령 후 짧은 post_sleep로 서버가 명령을 소비할 시간을 준 뒤 drain으로 출력을 정리.
        ksend는 수 ms 안에 끝나므로 20ms 기본값으로 충분.
        """
        def _do(shell) -> None:
            for c in commands:
                shell.send(c + "\n")
                if post_sleep_s > 0:
                    time.sleep(post_sleep_s)
                self._drain_shell(shell)

        with self._ssh_lock:
            try:
                shell = self._get_shared_shell()
                _do(shell)
                return
            except Exception as e:
                logger.warning("ICAS shared shell exec failed, retrying: %s", e)
                # shell 리셋 → 다시 시도 (transport가 살아있으면 재사용, 죽었으면 재연결)
                if self._ssh_shell is not None:
                    try:
                        self._ssh_shell.close()
                    except Exception:
                        pass
                    self._ssh_shell = None
            shell = self._get_shared_shell()
            _do(shell)

    def connect(self, timeout: float = 10.0) -> bool:
        """공유 SSH 세션을 확보하여 연결 상태를 확인 + 유지."""
        try:
            with self._ssh_lock:
                self._get_shared_ssh()  # 끊어져 있으면 새로 연결
            self._connected = True
            logger.info("ICAS connected to %s:%d", self.host, self.port)
            return True
        except Exception as e:
            logger.error("ICAS connect failed %s:%d: %s", self.host, self.port, e)
            self._connected = False
            return False

    def disconnect(self) -> None:
        self._connected = False
        with self._ssh_lock:
            if self._ssh_shell is not None:
                try:
                    self._ssh_shell.close()
                except Exception:
                    pass
                self._ssh_shell = None
            if self._ssh_client is not None:
                try:
                    self._ssh_client.close()
                except Exception:
                    pass
                self._ssh_client = None
        self._close_private_server_ssh()

    def _close_private_server_ssh(self) -> None:
        """private_server 공유 SSH와 터널 채널을 닫는다."""
        with self._ps_lock:
            if self._ps_ssh is not None:
                try:
                    self._ps_ssh.close()
                except Exception:
                    pass
                self._ps_ssh = None
            if self._ps_tunnel_chan is not None:
                try:
                    self._ps_tunnel_chan.close()
                except Exception:
                    pass
                self._ps_tunnel_chan = None

    def _get_private_server_ssh(self):
        """IID/HUD용 private_server 공유 SSH 반환 — 죽어있으면 새로 열고 인증.

        매 프레임 새로 paramiko.connect()를 하면 인증만 300-500ms가 들어 FPS가 떨어짐.
        direct-tcpip 터널 + SSH 클라이언트를 프로세스 수명 동안 재사용.
        호출자는 `_ps_lock` 잡고 사용 (SFTP/exec_command가 동시에 돌지 않도록).
        """
        # 살아있으면 그대로 반환
        if self._ps_ssh is not None:
            try:
                t = self._ps_ssh.get_transport()
                if t is not None and t.is_active() and t.is_authenticated():
                    return self._ps_ssh
            except Exception:
                pass
            # 죽었으면 정리
            self._close_private_server_ssh()
        # 새로 연결
        import paramiko
        shared = self._get_shared_ssh()  # HU shared SSH (락 보호됨 — _ssh_lock)
        hu_transport = shared.get_transport()
        if hu_transport is None or not hu_transport.is_active():
            raise RuntimeError("ICAS shared HU transport not active")
        chan = hu_transport.open_channel(
            "direct-tcpip",
            (self.private_server_ip, 22),
            ("127.0.0.1", 0),
            timeout=10,
        )
        ps_ssh = paramiko.SSHClient()
        ps_ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ps_ssh.connect(
            self.private_server_ip, port=22,
            username="root", password=(self.private_server_password or ""),
            sock=chan, timeout=15,
            allow_agent=False, look_for_keys=False,
        )
        try:
            pt = ps_ssh.get_transport()
            if pt is not None:
                pt.set_keepalive(self._ssh_keepalive_interval)
        except Exception:
            pass
        self._ps_tunnel_chan = chan
        self._ps_ssh = ps_ssh
        return ps_ssh

    async def async_connect(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.connect, timeout)

    async def async_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    # ------------------------------------------------------------------
    # Low-level helpers
    # ------------------------------------------------------------------
    def _exec_on_shared(self, commands: list[str], interval_s: float = 0.0,
                        per_cmd_timeout: float = 5.0) -> None:
        """공유 SSH 세션에서 exec_command들을 순차 실행.

        각 명령은 exit_status를 기다려 채널을 즉시 해제함 (sshd MaxSessions=10 한도 보호).
        ksend는 즉시 반환되므로 wait 비용이 무시할 수준. transport 에러 시 세션 리셋 후 1회 재시도.
        """
        def _run_one(ssh, c: str) -> None:
            stdin, stdout, stderr = ssh.exec_command(c, timeout=per_cmd_timeout)
            try:
                stdin.close()
            except Exception:
                pass
            # exit_status 대기 → 채널 즉시 클로즈 (sshd 세션 누수 방지)
            try:
                stdout.channel.settimeout(per_cmd_timeout)
                stdout.channel.recv_exit_status()
            except Exception:
                pass
            finally:
                for f in (stdout, stderr):
                    try:
                        f.close()
                    except Exception:
                        pass

        def _run_all(ssh, cmd_list: list[str]) -> None:
            for i, c in enumerate(cmd_list):
                _run_one(ssh, c)
                if interval_s > 0 and i < len(cmd_list) - 1:
                    time.sleep(interval_s)

        with self._ssh_lock:
            try:
                ssh = self._get_shared_ssh()
                _run_all(ssh, commands)
                return
            except Exception as e:
                # transport 끊김/EOF/채널 한도 초과 등 → 세션 리셋 후 1회 재시도
                logger.warning("ICAS shared SSH exec failed, retrying: %s", e)
                if self._ssh_client is not None:
                    try:
                        self._ssh_client.close()
                    except Exception:
                        pass
                    self._ssh_client = None
            ssh = self._get_shared_ssh()
            _run_all(ssh, commands)

    def _ksend_exec(self, cmds: list[str], interval_s: float = 0.0) -> None:
        """ksend를 exec_command로 송신 — 각 호출마다 새 채널 오픈.

        ICAS3 CN sshd가 invoke_shell + 빠른 연속 명령에서 transport를 끊는 케이스 회피용.
        EU(invoke_shell) 대비 약간 느리지만 (~10ms/호출 오버헤드) ClientDisconnected가 안 남.
        """
        def _run(ssh) -> None:
            for c in cmds:
                try:
                    # IVI load 환경에서 4s는 너무 짧아 ksend 명령조차 timeout 가능 → 8s.
                    stdin, stdout, stderr = ssh.exec_command(c, timeout=8)
                    try:
                        stdin.close()
                    except Exception:
                        pass
                    try:
                        # stdout 한번 읽어 채널 정상 종료 보장 — ksend는 stdout이 비어있어 즉시 EOF.
                        stdout.read()
                        stdout.channel.recv_exit_status()
                    except Exception:
                        pass
                    finally:
                        for f in (stdout, stderr):
                            try:
                                f.close()
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug("ICAS ksend exec failed: %s", e)
                if interval_s > 0:
                    time.sleep(interval_s)

        with self._ssh_lock:
            try:
                ssh = self._get_shared_ssh()
                _run(ssh)
                return
            except Exception as e:
                logger.warning("ICAS ksend exec failed, retrying with fresh SSH: %s", e)
                if self._ssh_client is not None:
                    try:
                        self._ssh_client.close()
                    except Exception:
                        pass
                    self._ssh_client = None
                ssh = self._get_shared_ssh()
                _run(ssh)

    def _ksend(self, data_bytes: str) -> None:
        """ksend 명령 1회 송신. variant에 따라 채널 모드 분기.
          - icas (EU): invoke_shell 공유 채널 (저오버헤드, 빠름)
          - icas3 (CN): exec_command 채널 (안정성 우선 — invoke_shell이 ClientDisconnected 유발)
        """
        cmd = f'/lge/app_ro/bin/ksend -s {self.src_addr} -d {self.dst_addr} -b "{data_bytes}"'
        if self.variant == "icas3":
            self._ksend_exec([cmd])
        else:
            self._shell_run([cmd])

    def _ksend_many(self, data_list: list[str], interval_s: float = 0.1) -> None:
        """ksend 명령 여러 개를 순차 송신. variant 분기 동일."""
        cmds = [
            f'/lge/app_ro/bin/ksend -s {self.src_addr} -d {self.dst_addr} -b "{data}"'
            for data in data_list
        ]
        if self.variant == "icas3":
            self._ksend_exec(cmds, interval_s=interval_s)
        else:
            # 각 cmd 사이 간격은 shell_run의 post_sleep_s로 들어감 — interval_s 우선
            self._shell_run(cmds, post_sleep_s=max(0.02, interval_s))

    # ------------------------------------------------------------------
    # Touch (press/drag/release) — ref RemoteController.excutecmdTouch*
    # ------------------------------------------------------------------
    def _touch_frame(self, x: int, y: int, end_byte: int) -> str:
        # ICAS3 variant: 16B frame + 다른 좌표 인코딩 (touch_event.sh 패턴).
        if self.variant == "icas3":
            return self._touch_frame_icas3(int(x), int(y), int(end_byte))
        p1, p2, p3 = _encode_touch_xy(int(x), int(y), self._x_mult, self._y_mult)
        return (
            f"0x83 0x50 0x20 0x0b 0x00 0x00 0x00 0x00 0x00 0xa0 0x01 0x11 "
            f"0x{p1:02x} 0x{p2:02x} 0x{p3:02x} 0x{end_byte:02x}"
        )

    def _touch_frame_icas3(self, x: int, y: int, end_byte: int) -> str:
        """ICAS3 CN 16B touch frame (touch_event.sh 패턴).

        헤더 12B: 0x83 0x50 0x20 0x0B 0x17 0xF8 0xF1 0x73 0x00 0xA0 0x02 0x11
        좌표  3B: byte3/byte4/byte5 (_encode_touch_xy_icas3)
        종료  1B: 0xFD(press) / 0xFE(drag) / 0xFF(release)
        """
        px = int(round(float(x) / max(1, self._x_mult)))
        py = int(round(float(y) / max(1, self._y_mult)))
        b3, b4, b5 = _encode_touch_xy_icas3(px, py)
        return (
            f"0x83 0x50 0x20 0x0b 0x17 0xf8 0xf1 0x73 0x00 0xa0 0x02 0x11 "
            f"0x{b3:02x} 0x{b4:02x} 0x{b5:02x} 0x{end_byte:02x}"
        )

    def _touch_press(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFD))

    def _touch_drag(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFE))

    def _touch_release(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFF))

    def tap(self, x: int, y: int, screen_type: str = "HU",
            dp: float = 0.2, dr: float = 0.0) -> None:
        """단일 탭. press → (dp초 대기) → release."""
        self._touch_press(x, y)
        if dp > 0:
            time.sleep(dp)
        self._touch_release(x, y)
        if dr > 0:
            time.sleep(dr)

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "HU") -> None:
        self._touch_press(x, y)
        time.sleep(duration_ms / 1000.0)
        self._touch_release(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "HU", duration_ms: int = 300) -> None:
        """press(x1,y1) → drag(보간) → release(x2,y2)."""
        # 보간 스텝 수: duration 기반 (각 스텝 ~20ms 목표, 최소 3 최대 20)
        target_interval_ms = 20
        steps = max(3, min(20, max(1, duration_ms // target_interval_ms)))
        dx = (x2 - x1) / steps
        dy = (y2 - y1) / steps

        # 동일 SSH 세션으로 일괄 송신 — 오버헤드 최소화
        frames: list[str] = []
        frames.append(self._touch_frame(x1, y1, 0xFD))  # press
        for i in range(1, steps):
            ix = int(round(x1 + dx * i))
            iy = int(round(y1 + dy * i))
            frames.append(self._touch_frame(ix, iy, 0xFE))  # drag
        frames.append(self._touch_frame(x2, y2, 0xFF))  # release

        # 간격은 duration_ms에 맞춰 분배
        interval_s = max(0.01, (duration_ms / 1000.0) / max(1, len(frames) - 1))
        self._ksend_many(frames, interval_s=interval_s)

    def repeat_tap(self, x: int, y: int, count: int = 5,
                   interval_ms: int = 100, screen_type: str = "HU") -> None:
        for i in range(count):
            self.tap(x, y, screen_type, dp=0.05, dr=0.0)
            if i < count - 1 and interval_ms > 0:
                time.sleep(interval_ms / 1000.0)

    # ------------------------------------------------------------------
    # Hardkey
    # ------------------------------------------------------------------
    def _hkey_short_frame(self, key_code: int, state: int) -> str:
        """Short 클래스(Volume/Mute/PTT) — 13 bytes."""
        return (
            f"0x83 0x50 0x10 0x0A 0x00 0x00 0x05 0xBF 0x00 "
            f"0x{key_code:02X} 0x{state:02X} 0x00 0x00"
        )

    def _hkey_long_frame(self, key_code: int, state: int) -> str:
        """Long 클래스(Home/Power) — 15 bytes.
        state=0x01 / 0x00 에 따라 tail(0x10 / 0xD9) 변경 (ref 코드 관찰값)."""
        tail = 0x10 if state else 0xD9
        return (
            f"0x83 0x50 0x20 0x0B 0x17 0xF8 0xF1 0x73 0x00 0x30 "
            f"0x{key_code:02X} 0x{state:02X} 0x{tail:02X} 0x00 0x00"
        )

    def resolve_key(self, key_name: str) -> Optional[dict]:
        """키 스펙 반환 (override 병합)."""
        base = ICAS_KEYS.get(key_name)
        if not base:
            return None
        merged = dict(base)
        ov = self._key_overrides.get(key_name) or {}
        for k in ("class", "key"):
            if k in ov:
                merged[k] = ov[k]
        return merged

    def set_key_overrides(self, overrides: Optional[dict[str, dict]]) -> None:
        self._key_overrides = dict(overrides or {})

    def get_key_overrides(self) -> dict[str, dict]:
        return dict(self._key_overrides)

    def send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                         screen_type: Optional[str] = None,
                         direction: Optional[int] = None,
                         hold_ms: Optional[int] = None) -> None:
        """이름 기반 하드키 송신. sub_cmd는 HKMC6th API 호환용(SHORT/LONG).

        ICAS는 press→release 시퀀스가 기본. LONG은 press→대기→release 패턴으로 처리.
        hold_ms: LONG_KEY일 때 press↔release 사이 hold 시간(ms). None이면 기본 1000ms.
        """
        info = self.resolve_key(key_name)
        if not info:
            raise ValueError(f"Unknown ICAS key: {key_name}")
        key_code = int(info["key"])
        klass = info.get("class", "short")

        # press / release — Short class는 release 시 key=0x00, state=0x00 (ref RemoteController:525)
        # Long class는 key_code 유지, state만 0x00 + tail 변경 (ref line 562)
        press = (self._hkey_short_frame(key_code, 0x01) if klass == "short"
                 else self._hkey_long_frame(key_code, 0x01))
        release = (self._hkey_short_frame(0x00, 0x00) if klass == "short"
                   else self._hkey_long_frame(key_code, 0x00))

        if sub_cmd == LONG_KEY:
            hold_s = max(0.05, (hold_ms / 1000.0)) if hold_ms is not None else 1.0
        else:
            hold_s = 0.1
        self._ksend_many([press], interval_s=0)
        time.sleep(hold_s)
        self._ksend_many([release], interval_s=0)

        # POWER 전용 추가 커맨드 (ref ABTpower: command03~05)
        # HU의 power state 전환을 위한 별도 주소(src2/dst2) 메시지
        if key_name == "POWER":
            self._ksend_power_extra()


    def _ksend_power_extra(self) -> None:
        """ABTpower의 command03~05에 해당하는 추가 ksend 송신.
        market에 따라 src2/dst2 주소가 다름 (ref RemoteController.ABTpower).
        """
        if self.market in ("EU", "NAR", "CN"):
            src2 = "0x40000000000"
            dst2 = "0x8000000000000000"
        else:
            src2 = "42"
            dst2 = "63"
        payloads = [
            "0x01 0x91 0xF0 0x01 0x4C 0x00 0x00",
            "0x01 0x91 0xF0 0x02 0x38 0x00 0x00",
            "0x01 0x91 0xF0 0x01 0x01 0x00 0x00",
        ]
        cmds = [
            f'/lge/app_ro/bin/ksend -s {src2} -d {dst2} -b "{p}"'
            for p in payloads
        ]
        self._shell_run(cmds, post_sleep_s=0.1)

    def send_key(self, cmd: int, sub_cmd: int, key_data: int,
                 monitor: int = 0x00, direction: Optional[int] = None,
                 hold_ms: Optional[int] = None) -> None:
        """HKMC 호환용 raw send_key. key_data를 KEY_CODE로 해석해 single press/release 수행.

        ICAS는 cmd 분류가 하나라, 별도 분기 없이 short 프레임을 기본으로 사용.
        long class가 필요하면 key_data 범위로 자동 판별 (POWER=0x38, HOME=0x66).
        hold_ms: LONG_KEY일 때 press↔release 사이 hold 시간(ms). None이면 기본 1000ms.
        """
        klass = "long" if key_data in (0x38, 0x66) else "short"
        press = (self._hkey_short_frame(key_data, 0x01) if klass == "short"
                 else self._hkey_long_frame(key_data, 0x01))
        # Short release는 key=0, state=0 (send_key_by_name과 동일 규칙)
        release = (self._hkey_short_frame(0x00, 0x00) if klass == "short"
                   else self._hkey_long_frame(key_data, 0x00))
        if sub_cmd == LONG_KEY:
            hold_s = max(0.05, (hold_ms / 1000.0)) if hold_ms is not None else 1.0
        else:
            hold_s = 0.1
        self._ksend_many([press], interval_s=0)
        time.sleep(hold_s)
        self._ksend_many([release], interval_s=0)

    # ------------------------------------------------------------------
    # Screenshot (HU only in MVP)
    # ------------------------------------------------------------------
    def screencap_bytes(self, screen_type: str = "HU",
                        fmt: str = "png", timeout: float = 15.0) -> bytes:
        """스크린샷 캡처. 현재는 HU만 지원.

        IID/HUD 경로는 private_server의 `screenshot` 바이너리가 'no displays'를
        반환하는 환경 제약으로 비활성. 향후 지원 시 `_screencap_iid_hud` 재활성.
        """
        # screen_type은 UI 호환을 위해 받되, 실제 경로는 항상 HU.
        return self._screencap_hu(fmt=fmt)

    # ------------------------------------------------------------------
    # HU screenshot — LayerManagerControl dump + SCP pull + composite
    # ------------------------------------------------------------------
    def _wait_remote_files_stable(self, ssh, remote_paths: list[str],
                                  max_wait_s: float = 1.0,
                                  poll_interval_s: float = 0.05,
                                  stable_iters: int = 2) -> None:
        """디바이스 쪽 파일 크기가 stable_iters회 연속 동일할 때까지 폴링.

        LayerManagerControl이 비동기로 PNG를 쓰는 환경(ICAS3 CN 등)에서 SCP가 partial 파일을
        가져가는 race를 막기 위함. 실패해도 silent — 안정성은 _validate_png_file에서 한 번 더 거름.
        """
        if not remote_paths:
            return
        deadline = time.monotonic() + max_wait_s
        size_cmd = " ; ".join([f"wc -c < {rp} 2>/dev/null || echo 0" for rp in remote_paths])
        prev_sizes: Optional[list[int]] = None
        stable_streak = 0
        while time.monotonic() < deadline:
            try:
                stdin, stdout, stderr = ssh.exec_command(size_cmd, timeout=2)
                try:
                    stdin.close()
                except Exception:
                    pass
                out = stdout.read().decode("utf-8", errors="replace")
                lines = [l.strip() for l in out.splitlines() if l.strip()]
                sizes: list[int] = []
                for l in lines:
                    try:
                        sizes.append(int(l.split()[0]))
                    except Exception:
                        sizes.append(0)
                if sizes and all(s > 0 for s in sizes) and sizes == prev_sizes:
                    stable_streak += 1
                    if stable_streak >= stable_iters:
                        return
                else:
                    stable_streak = 0
                prev_sizes = sizes
            except Exception:
                pass
            time.sleep(poll_interval_s)

    def _screencap_hu(self, fmt: str = "png") -> bytes:
        import tempfile
        from PIL import Image, ImageFile
        # _validate_png_file + _wait_remote_files_stable 이 1차 게이트 역할을 하므로
        # IDAT 내부 미세 손상은 PIL의 truncated tolerance에 맡기는 편이 시각 안정성에 유리.
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        # HU sshd는 SFTP 서브시스템 미지원 → SCP(paramiko-scp)로 pull.
        try:
            from scp import SCPClient
        except ImportError as e:
            raise RuntimeError("scp module required: pip install scp") from e

        # ICAS_DUMP_TIMEOUT_S — exec_command stdout 폴링 타임아웃(초). 기본 15.
        # IVI 환경은 load가 상시 1.0+ 라 dump가 4~10초 걸리는 경우 흔함.
        try:
            dump_timeout = float(os.environ.get("ICAS_DUMP_TIMEOUT_S", "15") or 15)
        except Exception:
            dump_timeout = 15.0

        tmp_dir = tempfile.mkdtemp(prefix="icas_cap_")
        try:
            # 공유 SSH 세션에서 dump + SCP pull 을 일괄 수행 (매 프레임마다 재인증 방지).
            # ICAS EU / ICAS3 CN 공통: screen 0(디바이스 화면) + screen 2(navigation map) 합성.
            def _do_capture(ssh) -> list[str]:
                file_map: list[tuple[int, str]] = [(0, "screen1.png"), (2, "screen2.png")]

                # rm + dump + sync 순서 — LayerManagerControl이 IVI 그래픽 파이프라인을 통해
                # 비동기로 PNG를 쓰는 구현체가 있어 exec_command가 끝나도 파일이 완성 전일 수 있음.
                # 각 dump 사이는 ';' 사용 — 한쪽 실패가 다른쪽을 막지 않음. stderr는 진단용 파일로.
                rm_parts = [f"rm -f /tmp/{fname}" for _, fname in file_map]
                dump_parts = [
                    f"LayerManagerControl dump screen {idx} to /tmp/{fname} 2>/tmp/lmc_icas_idx{idx}.err"
                    for idx, fname in file_map
                ]
                dump_cmd = (
                    "export XDG_RUNTIME_DIR=/run/platform/weston ; "
                    + " ; ".join(rm_parts)
                    + " ; "
                    + " ; ".join(dump_parts)
                    + " ; sync"
                )
                remotes = tuple((f"/tmp/{fname}", fname) for _, fname in file_map)
                stdin, stdout, stderr = ssh.exec_command(dump_cmd, timeout=dump_timeout)
                try:
                    stdin.close()
                except Exception:
                    pass
                exit_status = -1
                err_text = ""
                dump_deadline = time.monotonic() + dump_timeout
                try:
                    stdout.channel.settimeout(dump_timeout)
                    while not stdout.channel.exit_status_ready():
                        if time.monotonic() > dump_deadline:
                            # exec_command 자체의 timeout으로 안 끊기는 환경 대비 안전망.
                            logger.warning("ICAS HU dump timeout %.1fs — abandoning cycle", dump_timeout)
                            break
                        if stdout.channel.recv_stderr_ready():
                            try:
                                err_text += stdout.channel.recv_stderr(4096).decode("utf-8", errors="replace")
                            except Exception:
                                pass
                        else:
                            time.sleep(0.05)
                    while stdout.channel.recv_stderr_ready():
                        try:
                            err_text += stdout.channel.recv_stderr(4096).decode("utf-8", errors="replace")
                        except Exception:
                            break
                    exit_status = stdout.channel.recv_exit_status()
                except Exception:
                    pass
                finally:
                    for f in (stdout, stderr):
                        try:
                            f.close()
                        except Exception:
                            pass

                dump_failed = (exit_status != 0)
                if dump_failed:
                    snippet = err_text.strip().replace("\r", " ").replace("\n", " | ")[:200]
                    logger.warning("ICAS HU dump exit=%d stderr=%r — skipping SCP, will reset SSH",
                                   exit_status, snippet)
                    # dump 자체가 실패면 SCP 단계 skip — partial 파일을 가져와 화면이 흔들리는 부작용 방지.
                    # 호출자가 SSH 채널 리셋하도록 RuntimeError 신호.
                    raise RuntimeError(f"ICAS HU dump failed (exit={exit_status})")

                # LayerManagerControl이 비동기로 PNG를 쓰는 환경 대응: 파일 크기가 안정될 때까지 폴링.
                # 최대 1초 대기, 50ms 간격, 2회 연속 동일 크기여야 통과.
                self._wait_remote_files_stable(ssh, [rp for rp, _ in remotes])

                files: list[str] = []
                try:
                    # socket_timeout — SCP 전송 도중 IVI 부하로 응답이 늦어져 ClientDisconnected가
                    # 발생하는 케이스 방어. 기본 paramiko 값보다 넉넉히.
                    with SCPClient(ssh.get_transport(), socket_timeout=15.0) as scp:
                        for remote, fname in remotes:
                            local = os.path.join(tmp_dir, fname)
                            try:
                                scp.get(remote, local)
                                if _validate_png_file(local):
                                    files.append(local)
                                else:
                                    size = os.path.getsize(local) if os.path.exists(local) else 0
                                    logger.debug("ICAS HU %s invalid/truncated PNG (size=%d), skipping",
                                                 fname, size)
                            except Exception as ee:
                                logger.debug("ICAS HU scp %s failed: %s", remote, ee)
                except Exception as ee:
                    logger.warning("ICAS HU SCPClient failed: %s", ee)
                    # transport가 죽은 상태(예: ClientDisconnected)면 외부 retry에 위임 —
                    # _do_capture 내부에서 빈 리스트 반환만 하면 외부 except가 안 잡혀
                    # dead transport에서 매 사이클마다 같은 에러를 반복하게 됨.
                    if not self._is_ssh_alive(ssh):
                        raise RuntimeError(
                            f"ICAS HU SSH transport dead during SCP: {type(ee).__name__}"
                        )
                return files

            local_files: list[str] = []
            # 연속 실패 누적 시 sshd에 회복 시간 제공 — backoff 후 SSH 재연결.
            # 폭주 재연결이 sshd MaxStartups를 초과해 더 깊이 죽는 악순환 방지.
            # 주의: backoff sleep은 _ssh_lock 밖에서 수행한다. 락 안에서 자면 같은 락을 쓰는
            # 터치/하드키(_shell_run)가 매 실패 사이클마다 최대 2초씩 블록되어 입력이 먹통이 됨.
            if self._consecutive_capture_failures >= self._consecutive_capture_failures_threshold:
                backoff = min(2.0, 0.3 * self._consecutive_capture_failures)
                logger.info(
                    "ICAS HU capture backoff %.1fs after %d consecutive failures",
                    backoff, self._consecutive_capture_failures,
                )
                time.sleep(backoff)
                # 강제 SSH 폐기 — 다음 _get_shared_ssh가 새 연결 (close만 락으로 보호)
                with self._ssh_lock:
                    if self._ssh_client is not None:
                        try:
                            self._ssh_client.close()
                        except Exception:
                            pass
                        self._ssh_client = None
            with self._ssh_lock:
                try:
                    ssh = self._get_shared_ssh()
                    local_files = _do_capture(ssh)
                except Exception as e:
                    logger.warning("ICAS HU capture failed on shared SSH, retrying: %s", e)
                    if self._ssh_client is not None:
                        try:
                            self._ssh_client.close()
                        except Exception:
                            pass
                        self._ssh_client = None
                    try:
                        ssh = self._get_shared_ssh()
                        local_files = _do_capture(ssh)
                    except Exception as e2:
                        self._consecutive_capture_failures += 1
                        raise

            if not local_files:
                self._consecutive_capture_failures += 1
                raise RuntimeError("No HU screenshot captured")
            # 성공 시 카운터 리셋
            self._consecutive_capture_failures = 0

            # PNG 시그니처 검증을 통과해도 PIL이 디코딩 실패할 수 있으므로
            # 개별 파일 단위로 try/except 처리하여 일부만 깨져도 가능한 레이어로 합성.
            images: list[Image.Image] = []
            for p in local_files:
                try:
                    images.append(Image.open(p).convert("RGBA"))
                except Exception as ie:
                    logger.debug("ICAS HU skip unreadable %s: %s", p, ie)
            if not images:
                raise RuntimeError("No HU screenshot decodable")
            # 합성 순서 — variant별로 다름:
            #  - ICAS EU: screen 0(UI)을 base, screen 2(map)를 over로 올림.
            #    screen 2가 map 영역만 불투명한 alpha 구조라 디바이스 UI 위에 map만 표출.
            #  - ICAS3 CN: screen 2(map)가 fully opaque라 위에 올리면 UI 전체를 가림.
            #    → screen 2(map)를 base, screen 0(UI with transparent map region)을 위에 올린다.
            if self.variant == "icas3":
                images = list(reversed(images))
            base = images[0]
            for over in images[1:]:
                if over.size != base.size:
                    over = over.resize(base.size)
                base = Image.alpha_composite(base, over)
            return _encode_image(base, fmt)
        finally:
            _rm_tree(tmp_dir)

    # ------------------------------------------------------------------
    # IID/HUD screenshot — HU로 SSH → private server로 ssh hop → screenshot
    # ------------------------------------------------------------------
    def _screencap_iid_hud(self, display_number: str, fmt: str = "png") -> bytes:
        """ref RemoteController.IID_get_capture_path 이식.

        1) HU에 SSH로 2개 세션 연결 (하나는 private_server로 hop, 하나는 SCP 전용)
        2) hop 세션에서 `screenshot -display=N` 실행 → private server의 /tmp/screenshot.bmp 생성
        3) hop 세션에서 scp로 HU의 /tmp/screenshot.bmp로 가져옴
        4) SCP 세션으로 로컬에 pull
        5) BMP → PNG/JPEG 변환
        """
        import tempfile
        import os
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        if not self.private_server_ip:
            raise RuntimeError("ICAS IID/HUD capture: private_server_ip not configured")

        tmp_dir = tempfile.mkdtemp(prefix="icas_iid_")
        local_bmp = os.path.join(tmp_dir, "screenshot.bmp")
        try:
            # 방식: HU의 공유 SSH transport 위에 direct-tcpip 채널을 열어
            #        private_server:22로 터널링한 뒤, paramiko로 native SSH 로그인.
            #        이후 exec_command(+recv_exit_status)로 screenshot 실행,
            #        SFTP로 private_server:/tmp/screenshot.bmp → 로컬로 직접 pull.
            #
            # interactive shell-over-shell + scp password expect 방식은
            # 프롬프트 타이밍에 따라 자주 실패 → direct-tcpip으로 기초부터 제거.
            # paramiko SSH 클라이언트/터널은 _get_private_server_ssh에서 캐시하여 재사용.

            def _do_capture() -> None:
                # 공유 ps_ssh (direct-tcpip 터널 + SSH 인증 캐시됨) 재사용.
                # 죽어있으면 _get_private_server_ssh가 알아서 재연결.
                # _ps_lock으로 동시 호출 직렬화 — SFTP/exec_command 간섭 방지.
                with self._ps_lock:
                    with self._ssh_lock:
                        ps_ssh = self._get_private_server_ssh()
                    # private_server는 busybox 계열이라 bash가 없을 수 있음 → 기본 쉘 사용.
                    # PATH를 명시적으로 prepend + stale bmp 제거 + screenshot 실행.
                    cmd = (
                        "export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:"
                        "/sbin:/bin:$PATH && "
                        "cd /tmp && rm -f /tmp/screenshot.bmp && "
                        f"screenshot -display={display_number}"
                    )
                    stdin, stdout, stderr = ps_ssh.exec_command(
                        cmd, timeout=30, get_pty=True,
                    )
                    try:
                        stdin.close()
                    except Exception:
                        pass
                    out_text = ""
                    err_text = ""
                    exit_status = -1
                    try:
                        stdout.channel.settimeout(30)
                        deadline = time.time() + 30.0
                        while time.time() < deadline:
                            if stdout.channel.exit_status_ready():
                                break
                            if stdout.channel.recv_ready():
                                try:
                                    out_text += stdout.channel.recv(4096).decode("utf-8", errors="replace")
                                except Exception:
                                    pass
                            elif stdout.channel.recv_stderr_ready():
                                try:
                                    err_text += stdout.channel.recv_stderr(4096).decode("utf-8", errors="replace")
                                except Exception:
                                    pass
                            else:
                                time.sleep(0.05)
                        while stdout.channel.recv_ready():
                            try:
                                out_text += stdout.channel.recv(4096).decode("utf-8", errors="replace")
                            except Exception:
                                break
                        while stdout.channel.recv_stderr_ready():
                            try:
                                err_text += stdout.channel.recv_stderr(4096).decode("utf-8", errors="replace")
                            except Exception:
                                break
                        exit_status = stdout.channel.recv_exit_status()
                    except Exception:
                        pass
                    finally:
                        for f in (stdout, stderr):
                            try:
                                f.close()
                            except Exception:
                                pass

                    # SFTP — ps_ssh 공유 transport 위에 subsystem 1개 열어 stat + get + remove
                    sftp = ps_ssh.open_sftp()
                    try:
                        st = None
                        file_deadline = time.time() + 5.0
                        while time.time() < file_deadline:
                            try:
                                candidate = sftp.stat("/tmp/screenshot.bmp")
                                if candidate.st_size > 0:
                                    st = candidate
                                    break
                            except IOError:
                                pass
                            time.sleep(0.2)
                        if st is None:
                            snippet = (out_text + err_text).strip().replace("\r", " ").replace("\n", " | ")
                            if len(snippet) > 240:
                                snippet = snippet[:240] + "..."
                            raise RuntimeError(
                                f"screenshot.bmp not produced on private_server "
                                f"(display={display_number}, exit_status={exit_status}, "
                                f"output={snippet!r})"
                            )
                        sftp.get("/tmp/screenshot.bmp", local_bmp)
                        try:
                            sftp.remove("/tmp/screenshot.bmp")
                        except Exception:
                            pass
                    finally:
                        try:
                            sftp.close()
                        except Exception:
                            pass

            # 1회 재시도 — transport 죽어있으면 공유 ps_ssh/HU 모두 리셋 후 재시도
            try:
                _do_capture()
            except Exception as e:
                logger.warning("ICAS IID/HUD capture via direct-tcpip failed, retrying: %s", e)
                # private_server 세션 먼저 버리고, HU 세션도 같이 리셋 (터널이 HU 위에 있음)
                self._close_private_server_ssh()
                with self._ssh_lock:
                    if self._ssh_client is not None:
                        try:
                            self._ssh_client.close()
                        except Exception:
                            pass
                        self._ssh_client = None
                    if self._ssh_shell is not None:
                        try:
                            self._ssh_shell.close()
                        except Exception:
                            pass
                        self._ssh_shell = None
                _do_capture()

            if not os.path.exists(local_bmp) or os.path.getsize(local_bmp) == 0:
                raise RuntimeError("IID/HUD screenshot transfer failed")

            img = Image.open(local_bmp).convert("RGBA")
            return _encode_image(img, fmt)
        finally:
            _rm_tree(tmp_dir)

    @staticmethod
    def _drain_until(shel, want: Optional[tuple[str, ...]] = None,
                     max_wait_s: float = 5.0, poll_s: float = 0.1) -> str:
        """shell의 수신 버퍼를 누적하면서 want 문자열 중 하나가 나올 때까지 대기.

        want가 None이면 수신이 조용해질 때(quiet period 0.3s)까지만 읽고 리턴.
        리턴값: 누적된 문자열 (마지막 4KB 정도). 타임아웃이어도 누적된 버퍼 반환.
        """
        deadline = time.time() + max_wait_s
        last_data = time.time()
        buf = ""
        while time.time() < deadline:
            got_chunk = False
            try:
                if shel.recv_ready():
                    chunk = shel.recv(65536)
                    if chunk:
                        buf += chunk.decode("utf-8", errors="replace")
                        got_chunk = True
                        last_data = time.time()
            except Exception:
                break
            # want 매칭 체크 — 최근 2KB 만 보면 충분
            if want:
                tail = buf[-2048:]
                for w in want:
                    if w in tail:
                        return buf
            else:
                # quiet period 기반 종료
                if not got_chunk and (time.time() - last_data) > 0.3:
                    return buf
            if not got_chunk:
                time.sleep(poll_s)
        return buf

    @classmethod
    def _wait_for_remote_file(cls, shel, path: str, max_wait_s: float = 8.0) -> bool:
        """원격 shell에서 `ls -la path`를 폴링해서 파일 존재 + size>0 을 확인."""
        deadline = time.time() + max_wait_s
        marker = "__ICAS_FILE_OK__"
        while time.time() < deadline:
            shel.send(f'if [ -s "{path}" ]; then echo {marker}; fi\n')
            buf = cls._drain_until(shel, want=(marker, "$", "#"), max_wait_s=1.5)
            if marker in buf:
                return True
            time.sleep(0.3)
        return False

    @staticmethod
    def _shell_send_recv(shel, data: str, delay: float = 0.3) -> Optional[str]:
        """paramiko invoke_shell에 문자열 1회 송신 후 수신 버퍼를 반환 (ref ssh_send/iid_send)."""
        try:
            shel.send(data + "\r\n")
        except Exception as e:
            logger.debug("ICAS shell send failed: %s", e)
            return None
        time.sleep(delay)
        if shel.recv_ready():
            try:
                return shel.recv(65536).decode("utf-8", errors="replace")
            except Exception:
                return None
        return None

    async def async_screencap_bytes(self, screen_type: str = "HU",
                                    fmt: str = "png", timeout: float = 15.0) -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self.screencap_bytes, screen_type, fmt, timeout
        )

    # ------------------------------------------------------------------
    # Async wrappers (HKMC6th API 호환)
    # ------------------------------------------------------------------
    async def async_tap(self, x: int, y: int, screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.tap, x, y, screen_type)

    async def async_long_press(self, x: int, y: int, duration_ms: int = 3000,
                               screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.long_press, x, y, duration_ms, screen_type)

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type: str = "HU", duration_ms: int = 300) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.swipe, x1, y1, x2, y2, screen_type, duration_ms
        )

    async def async_repeat_tap(self, x: int, y: int, count: int = 5,
                               interval_ms: int = 100, screen_type: str = "HU") -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.repeat_tap, x, y, count, interval_ms, screen_type
        )

    async def async_send_key_by_name(self, key_name: str, sub_cmd: int = SHORT_KEY,
                                     screen_type: Optional[str] = None,
                                     direction: Optional[int] = None,
                                     hold_ms: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.send_key_by_name, key_name, sub_cmd, screen_type, direction, hold_ms
        )

    async def async_send_key(self, cmd: int, sub_cmd: int, key_data: int,
                             monitor: int = 0x00,
                             direction: Optional[int] = None,
                             hold_ms: Optional[int] = None) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.send_key, cmd, sub_cmd, key_data, monitor, direction, hold_ms
        )

    # ------------------------------------------------------------------
    # Meta
    # ------------------------------------------------------------------
    def get_info(self) -> dict:
        """HKMC6th.get_info()와 동형. IID/HUD 해상도는 캡처 시 실제 BMP 크기로 확정되므로
        초기값은 HU 해상도 기반으로 추정 (최초 캡처 전 프레임 렌더링용 기본치).
        """
        return {
            "host": self.host,
            "port": self.port,
            "connected": self._connected,
            "agent_version": self.agent_version,
            "screens": {
                "HU":  {"width": self._res_x, "height": self._res_y},
                "IID": {"width": self._res_x, "height": self._res_y},
                "HUD": {"width": self._res_x, "height": self._res_y},
            },
        }
