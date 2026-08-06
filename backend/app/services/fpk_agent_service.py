"""FPK Agent Service — SSH 기반 VW FPK(클러스터) 화면 캡처 전용 에이전트.

MIB/ICAS와 같은 SSH 접속이지만 플랫폼이 전혀 다르다:
  - Telechips SoC + BusyBox 유저랜드 (`Linux 5.4.x-tcc armv7l`)
  - weston/LayerManagerControl 없음 → MIB의 `dump screen` 캡처 경로 사용 불가
  - ksend 없음 → 터치/하드키 등 화면 조작 **불가** (캡처·이미지비교 전용 디바이스)
  - `/dev/fb0` 프레임버퍼를 직접 읽을 수 있고 `/usr/bin/python3`가 존재

캡처 방식 (실측 fpkgen2 / 1280x480):
  1) 디바이스에서 python3가 FBIOGET_VSCREENINFO(0x4600)로 **현재 표시중인 버퍼의 yoffset**을 읽는다.
     이 패널은 yres_virtual=1440 = 480x3 트리플 버퍼 패닝이라 yoffset이 0→960→480으로 순환한다.
     고정 오프셋으로 읽으면 3프레임 중 2개가 과거 화면이 되므로 매 프레임 ioctl이 필수.
  2) 해당 오프셋에서 한 화면(yres * stride)을 읽어 zlib(level 1)로 압축해 stdout으로 송출.
  3) 서버(PIL, C 구현)가 zlib 해제 후 raw BGRX → RGB 디코딩. 디바이스에서 채널 스왑/PNG 인코딩을
     하지 않는다 — 순수 파이썬 PNG 인코딩은 armv7에서 ~990ms로 병목이었다(level6 933ms).

  실측 프레임 비용: fb read ~19ms + zlib level1 ~294ms(719KB) → 약 2.5~3fps.
  압축 레벨은 zlib level 6 대비 3.2배 빠르고 크기는 17%만 크다 → level 1 채택.

프레임 형식: b"FPKR" + struct('<IHH', zlen, sw, sh) + zlib(tightly-packed BGRX rows)
  헤더는 LiveStreamMixin 공통 포맷과 동일. stride != width*4 인 패널을 위해 디바이스가
  행 단위로 잘라 촘촘하게 보내므로 서버는 항상 width*4*height 바이트를 기대하면 된다.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import threading
import time
import zlib
from typing import Callable, Optional

from .live_stream_mixin import LiveStreamMixin

logger = logging.getLogger(__name__)

# ── 디바이스측 스크립트 공통 프롤로그 ───────────────────────────────────────
# FBIOGET_VSCREENINFO=0x4600 / FBIOGET_FSCREENINFO=0x4602
# fb_var_screeninfo: xres,yres,xres_virtual,yres_virtual,xoffset,yoffset,bits_per_pixel,...
# fb_fix_screeninfo(32bit): id[16] smem_start smem_len type type_aux visual
#                           xpanstep(u16) ypanstep(u16) ywrapstep line_length@44
_FB_PROLOGUE = r'''
import sys, os, fcntl, struct, zlib, time
FB = "__FB__"
LEVEL = __ZLEVEL__
fd = os.open(FB, os.O_RDONLY)
var = bytearray(160)
fix = bytearray(80)
fcntl.ioctl(fd, 0x4602, fix, True)
STRIDE = struct.unpack("<I", bytes(fix[44:48]))[0]
fcntl.ioctl(fd, 0x4600, var, True)
XR, YR = struct.unpack("<2I", bytes(var[:8]))
BPP = struct.unpack("<I", bytes(var[24:28]))[0]
ROW = XR * (BPP // 8)
if STRIDE <= 0:
    STRIDE = ROW


def grab():
    """현재 표시중인 버퍼(yoffset)에서 한 화면을 촘촘한 행으로 읽어 반환."""
    fcntl.ioctl(fd, 0x4600, var, True)
    yoff = struct.unpack("<I", bytes(var[20:24]))[0]
    os.lseek(fd, yoff * STRIDE, 0)
    data = os.read(fd, YR * STRIDE)
    if len(data) < YR * STRIDE:
        return None
    if STRIDE != ROW:
        data = b"".join(data[i * STRIDE:i * STRIDE + ROW] for i in range(YR))
    return data
'''

# 단발 캡처 — 한 프레임만 stdout으로 내보내고 종료.
_FPK_CAPTURE = _FB_PROLOGUE + r'''
out = sys.stdout.buffer
data = None
for _ in range(5):
    data = grab()
    if data:
        break
    time.sleep(0.05)
if data is None:
    sys.stderr.write("FPKCAP read failed (short read)\n")
    sys.exit(2)
comp = zlib.compress(data, LEVEL)
sys.stderr.write("FPKCAP %dx%d bpp=%d stride=%d raw=%d z=%d\n"
                 % (XR, YR, BPP, STRIDE, len(data), len(comp)))
sys.stderr.flush()
out.write(b"FPKR" + struct.pack("<IHH", len(comp), XR, YR) + comp)
out.flush()
'''

# 라이브 스트리머 — 프레임을 계속 송출 (LiveStreamMixin이 소비).
_FPK_LIVE_STREAMER = _FB_PROLOGUE + r'''
sys.stderr.write("FPKSCR fb=%s %dx%d bpp=%d stride=%d\n" % (FB, XR, YR, BPP, STRIDE))
sys.stderr.flush()
out = sys.stdout.buffer
while True:
    try:
        data = grab()
        if data is None:
            time.sleep(0.05)
            continue
        comp = zlib.compress(data, LEVEL)
        out.write(b"FPKR" + struct.pack("<IHH", len(comp), XR, YR) + comp)
        out.flush()
    except (BrokenPipeError, IOError):
        break
    except Exception:
        time.sleep(0.1)
'''

# 프레임버퍼 기하 프로브 — 연결 시 1회 실행해 해상도/bpp 검증.
_FPK_PROBE = r'''
import os, fcntl, struct
fd = os.open("__FB__", os.O_RDONLY)
var = bytearray(160); fcntl.ioctl(fd, 0x4600, var, True)
fix = bytearray(80); fcntl.ioctl(fd, 0x4602, fix, True)
xr, yr, xv, yv, xo, yo, bpp = struct.unpack("<7I", bytes(var[:28]))
print("FBGEO %d %d %d %d %d %d %s" % (
    xr, yr, xv, yv, bpp, struct.unpack("<I", bytes(fix[44:48]))[0],
    bytes(fix[:16]).rstrip(b"\x00").decode("ascii", "replace")))
os.close(fd)
'''


def _encode_image(pil_image, fmt: str) -> bytes:
    """PIL Image → PNG/JPEG 바이트."""
    buf = io.BytesIO()
    if (fmt or "png").lower() == "jpeg":
        pil_image.convert("RGB").save(buf, format="JPEG", quality=85)
    else:
        pil_image.save(buf, format="PNG")
    return buf.getvalue()


class FPKAgentService(LiveStreamMixin):
    """SSH + /dev/fb0 기반 FPK 클러스터 **캡처 전용** 서비스.

    화면 조작(터치/스와이프/하드키)은 이 플랫폼에서 지원되지 않는다. 다른 에이전트와 같은
    async 캡처 API(`async_screencap_bytes`)를 제공해 이미지 비교/OCR 스텝이 그대로 동작하고,
    조작 계열 API는 호출 시 명확한 메시지로 실패한다(AttributeError로 죽지 않게).
    """

    default_screen = "HU"
    # 라이브 스트림: fb0 raw + zlib 페이로드로 교체 (기본 dump screen PNG 대신)
    _live_streamer_src = ""   # __init__에서 fb/level 치환 후 인스턴스 속성으로 확정
    _live_magic = b"FPKR"

    def __init__(self, host: str, port: int = 22, device_id: str = "",
                 username: str = "root", password: str = "",
                 resolution: str = "1280x480",
                 fb_device: str = "/dev/fb0",
                 zlib_level: int = 1,
                 pixel_order: str = "BGRX",
                 ipv6_address: str = "",
                 on_resolution_changed: Optional[Callable[[str], None]] = None,
                 on_ipv6_detected: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = int(port)
        # 실제 SSH 대상. 스캔은 IPv4(화이트리스트)로 후보를 잡지만 이 장비는 IPv6로만
        # SSH가 열려 있는 경우가 있어, 알려진/자동 감지된 IPv6를 우선 사용한다.
        self.ipv6_address = (ipv6_address or "").strip()
        self.connect_host = self.ipv6_address or host
        self._on_ipv6_detected = on_ipv6_detected
        self.device_id = device_id or f"FPK_{host}"
        self.username = username or "root"
        self.password = password or ""
        self._resolution = str(resolution).upper()
        self._parse_resolution()

        self.fb_device = fb_device or "/dev/fb0"
        try:
            self.zlib_level = max(0, min(9, int(os.environ.get("FPK_ZLIB_LEVEL", zlib_level))))
        except Exception:
            self.zlib_level = 1
        # 메모리 바이트 순서. 실측 fpkgen2: BITF R=16 G=8 B=0 A=24 → BGRA(=PIL rawmode "BGRX").
        # 패널이 RGBA면 "RGBX"로 override (env FPK_PIXEL_ORDER 또는 디바이스 info).
        self.pixel_order = (os.environ.get("FPK_PIXEL_ORDER") or pixel_order or "BGRX").upper()
        self._on_resolution_changed = on_resolution_changed

        self._connected = False
        self.agent_version = "FPK Agent"
        self.fb_info: dict = {}

        # 캡처 전용 공유 SSH — 단발 캡처는 이 연결에서 exec_command로 수행.
        self._ssh_client = None
        self._ssh_lock = threading.RLock()
        self._ssh_keepalive_interval = 30

        # 디바이스 스크립트에 fb 경로/압축레벨 주입
        self._live_streamer_src = self._render(_FPK_LIVE_STREAMER)
        self._live_label = "FPK"
        self._live_sync_res = True  # 첫 프레임 해상도로 등록 해상도 1회 보정
        self._init_live_stream()

    # ------------------------------------------------------------------
    # 기본 속성
    # ------------------------------------------------------------------
    def _render(self, src: str) -> str:
        return src.replace("__FB__", self.fb_device).replace("__ZLEVEL__", str(self.zlib_level))

    def _parse_resolution(self) -> None:
        try:
            w_s, h_s = self._resolution.upper().split("X")
            self.res_x, self.res_y = int(w_s), int(h_s)
        except Exception:
            self.res_x, self.res_y = 1280, 480

    @property
    def resolution(self) -> str:
        return self._resolution

    @resolution.setter
    def resolution(self, value: str) -> None:
        self._resolution = str(value).upper()
        self._parse_resolution()

    @property
    def is_connected(self) -> bool:
        return self._connected

    def _maybe_autoupdate_resolution(self, width: int, height: int) -> bool:
        """실제 프레임버퍼 해상도가 등록값과 다르면 갱신하고 콜백으로 알린다."""
        if width <= 0 or height <= 0:
            return False
        if width == self.res_x and height == self.res_y:
            return False
        old = self._resolution
        self._resolution = f"{width}x{height}"
        self._parse_resolution()
        logger.info("FPK resolution auto-updated: %s → %s (%s)", old, self._resolution, self.device_id)
        if self._on_resolution_changed:
            try:
                self._on_resolution_changed(self._resolution)
            except Exception as e:
                logger.warning("FPK resolution callback failed: %s", e)
        return True

    # ------------------------------------------------------------------
    # SSH
    # ------------------------------------------------------------------
    def _new_ssh(self, host: str = ""):
        """새 paramiko SSHClient 생성 및 연결 (라이브 스트림 전용 연결 등).

        host 미지정이면 확정된 connect_host(IPv6 우선)로 접속한다. paramiko는 IPv6
        리터럴을 그대로 받는다(대괄호 없이) — getaddrinfo가 처리.
        """
        import paramiko
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(host or self.connect_host, username=self.username, port=self.port,
                    password=self.password, timeout=10,
                    allow_agent=False, look_for_keys=False)
        return ssh

    @staticmethod
    def _detect_device_ipv6(ssh) -> str:
        """디바이스가 가진 **글로벌 IPv6** 주소를 조회한다 (없으면 빈 문자열).

        `/proc/net/if_inet6` 는 BusyBox에도 항상 있어(`ip`/`ifconfig` 유무와 무관) 가장 안전하다.
        형식: <32자리 hex 주소> <ifindex> <prefixlen> <scope> <flags> <devname>
        scope 00 = global. VW 진단망(fd53:*)이 있으면 최우선 선택.
        """
        import ipaddress
        try:
            _in, out, _err = ssh.exec_command("cat /proc/net/if_inet6", timeout=10)
            text = out.read().decode("utf-8", "replace")
        except Exception as e:
            logger.debug("FPK IPv6 probe failed: %r", e)
            return ""
        fallback = ""
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            hexaddr, _idx, _plen, scope, _flags, dev = parts[:6]
            if len(hexaddr) != 32 or scope != "00" or dev == "lo":
                continue
            try:
                addr = ipaddress.IPv6Address(
                    ":".join(hexaddr[i:i + 4] for i in range(0, 32, 4)))
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local:
                continue
            compressed = addr.compressed
            if compressed.startswith("fd53:"):
                return compressed  # VW 진단망 — 최우선
            if not fallback:
                fallback = compressed
        return fallback

    def _is_ssh_alive(self, ssh) -> bool:
        try:
            tr = ssh.get_transport() if ssh else None
            return bool(tr and tr.is_active())
        except Exception:
            return False

    def _get_shared_ssh(self):
        """단발 캡처용 공유 SSH — 끊겨 있으면 재연결."""
        with self._ssh_lock:
            if self._is_ssh_alive(self._ssh_client):
                return self._ssh_client
            if self._ssh_client is not None:
                try:
                    self._ssh_client.close()
                except Exception:
                    pass
                self._ssh_client = None
            ssh = self._new_ssh()
            return self._adopt_ssh(ssh)

    def _adopt_ssh(self, ssh):
        """공유 SSH로 채택 + keepalive 설정."""
        with self._ssh_lock:
            try:
                tr = ssh.get_transport()
                if tr is not None:
                    tr.set_keepalive(self._ssh_keepalive_interval)
            except Exception:
                pass
            self._ssh_client = ssh
            return ssh

    # ------------------------------------------------------------------
    # 연결 / 해제
    # ------------------------------------------------------------------
    def connect(self, timeout: float = 10.0) -> bool:
        """SSH 접속 + python3/프레임버퍼 가용성 검증 + 실제 해상도 반영.

        주소 해석: 스캔은 IPv4 화이트리스트로 후보를 잡지만 이 장비는 IPv6로만 SSH가
        열려 있을 수 있다. 따라서
          1) 알려진 IPv6 → 2) 등록 주소(보통 IPv4) 순으로 접속을 시도하고,
          3) 접속에 성공하면 디바이스의 글로벌 IPv6(fd53:* 우선)를 조회해 저장하고
             현재 접속 주소와 다르면 그 IPv6로 전환한다.
        """
        ssh = None
        candidates: list[str] = []
        for h in (self.ipv6_address, self.host):
            h = (h or "").strip()
            if h and h not in candidates:
                candidates.append(h)
        last_err: Optional[Exception] = None
        for host in candidates:
            try:
                with self._ssh_lock:
                    if self._ssh_client is not None:
                        try:
                            self._ssh_client.close()
                        except Exception:
                            pass
                        self._ssh_client = None
                ssh = self._adopt_ssh(self._new_ssh(host))
                self.connect_host = host
                break
            except Exception as e:
                last_err = e
                logger.debug("FPK SSH connect failed on %s:%s — %r", host, self.port, e)
        if ssh is None:
            logger.warning("FPK SSH connect failed (%s:%s, tried %s): %r",
                           self.host, self.port, candidates, last_err)
            self._connected = False
            return False

        # 디바이스가 알려주는 글로벌 IPv6로 승격 — 다음 연결부터는 이 주소를 먼저 쓴다.
        try:
            detected = self._detect_device_ipv6(ssh)
        except Exception:
            detected = ""
        if detected and detected != self.ipv6_address:
            self.ipv6_address = detected
            logger.info("FPK IPv6 detected: %s → %s", self.device_id, detected)
            if self._on_ipv6_detected:
                try:
                    self._on_ipv6_detected(detected)
                except Exception as e:
                    logger.warning("FPK IPv6 callback failed: %s", e)
        if detected and detected != self.connect_host:
            # 현재 IPv4로 붙어 있어도 IPv6로 갈아탄다(실패하면 기존 연결 유지).
            try:
                new_ssh = self._new_ssh(detected)
                try:
                    ssh.close()
                except Exception:
                    pass
                ssh = self._adopt_ssh(new_ssh)
                logger.info("FPK switched SSH host: %s → %s (%s)",
                            self.connect_host, detected, self.device_id)
                self.connect_host = detected
            except Exception as e:
                logger.info("FPK IPv6 switch failed (%s), keeping %s: %r",
                            detected, self.connect_host, e)

        try:
            geo = self._probe_fb(ssh)
        except Exception as e:
            logger.warning("FPK framebuffer probe failed (%s): %r", self.device_id, e)
            self._connected = False
            return False

        if not geo:
            logger.warning("FPK framebuffer probe returned nothing (%s) — "
                           "python3 또는 %s 접근 불가", self.device_id, self.fb_device)
            self._connected = False
            return False

        self.fb_info = geo
        if geo.get("bpp") != 32:
            # 16bpp(RGB565) 등은 현재 디코더가 지원하지 않음 — 조용히 깨진 화면을 내보내는 대신 실패.
            logger.error("FPK unsupported framebuffer depth: bpp=%s (32bpp만 지원) — %s",
                         geo.get("bpp"), self.device_id)
            self._connected = False
            return False

        self._maybe_autoupdate_resolution(geo.get("xres", 0), geo.get("yres", 0))
        self._connected = True
        logger.info("FPK connected: %s (%s:%s) fb=%s %dx%d bpp=%d stride=%d id=%s zlevel=%d",
                    self.device_id, self.connect_host, self.port, self.fb_device,
                    geo.get("xres", 0), geo.get("yres", 0), geo.get("bpp", 0),
                    geo.get("stride", 0), geo.get("fb_id", ""), self.zlib_level)
        return True

    def _probe_fb(self, ssh) -> Optional[dict]:
        """디바이스 프레임버퍼 기하 조회. 실패 시 None."""
        stdin, stdout, stderr = ssh.exec_command("python3 -u -", timeout=15)
        stdin.write(self._render(_FPK_PROBE))
        stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode("utf-8", "replace")
        err = stderr.read().decode("utf-8", "replace").strip()
        if err:
            logger.debug("FPK probe stderr: %s", err)
        for line in out.splitlines():
            if not line.startswith("FBGEO "):
                continue
            parts = line.split()
            try:
                return {
                    "xres": int(parts[1]), "yres": int(parts[2]),
                    "xres_virtual": int(parts[3]), "yres_virtual": int(parts[4]),
                    "bpp": int(parts[5]), "stride": int(parts[6]),
                    "fb_id": parts[7] if len(parts) > 7 else "",
                }
            except (IndexError, ValueError):
                return None
        if out.strip():
            logger.debug("FPK probe unexpected output: %s", out.strip()[:300])
        return None

    def disconnect(self) -> None:
        self.stop_live_stream()
        with self._ssh_lock:
            ssh = self._ssh_client
            self._ssh_client = None
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
        self._connected = False
        logger.info("FPK disconnected: %s", self.device_id)

    async def async_connect(self, timeout: float = 10.0) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, lambda: self.connect(timeout))

    async def async_disconnect(self) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.disconnect)

    # ------------------------------------------------------------------
    # 캡처
    # ------------------------------------------------------------------
    def _decode_payload(self, payload: bytes, sw: int, sh: int):
        """zlib(BGRX raw) 페이로드 → PIL RGB Image."""
        from PIL import Image
        raw = zlib.decompress(payload)
        expected = sw * sh * 4
        if len(raw) < expected:
            raise ValueError(f"FPK frame short: {len(raw)} < {expected} ({sw}x{sh})")
        return Image.frombytes("RGB", (sw, sh), raw[:expected], "raw", self.pixel_order)

    def _live_decode_frame(self, payload: bytes, sw: int, sh: int):
        """LiveStreamMixin 훅 — 라이브 프레임 디코드."""
        return self._decode_payload(payload, sw, sh)

    def screencap_bytes(self, screen_type: str = "HU", fmt: str = "png") -> bytes:
        """현재 클러스터 화면을 PNG/JPEG 바이트로 반환.

        라이브 스트림이 이미 돌고 있으면 그 최신 프레임을 재사용하지 않고 항상 새로 캡처한다
        (이미지 비교는 '지금 이 순간'의 화면이어야 하므로 최대 1프레임 지연도 허용하지 않음).
        """
        t0 = time.time()
        ssh = self._get_shared_ssh()
        chan = ssh.get_transport().open_session()
        try:
            chan.settimeout(20.0)
            chan.exec_command("python3 -u -")
            chan.sendall(self._render(_FPK_CAPTURE).encode("utf-8"))
            chan.shutdown_write()
            buf = b""
            err = b""
            while True:
                if chan.recv_stderr_ready():
                    chunk = chan.recv_stderr(4096)
                    if chunk:
                        # EOF면 recv_stderr가 b''를 반환하면서도 ready가 유지될 수 있어
                        # 빈 청크에서는 continue하지 않는다(무한 루프 방지).
                        err += chunk
                        continue
                data = chan.recv(262144)
                if not data:
                    break
                buf += data
            while chan.recv_stderr_ready():
                chunk = chan.recv_stderr(4096)
                if not chunk:
                    break
                err += chunk
        finally:
            try:
                chan.close()
            except Exception:
                pass

        if err:
            logger.debug("FPK capture stderr (%s): %s", self.device_id,
                         err.decode("utf-8", "replace").strip())

        idx = buf.find(b"FPKR")
        if idx < 0 or len(buf) < idx + 12:
            raise RuntimeError(
                f"FPK capture failed: no frame from {self.device_id} "
                f"({len(buf)}B stdout, stderr={err.decode('utf-8', 'replace').strip()[:200]!r})"
            )
        zlen, sw, sh = struct.unpack("<IHH", buf[idx + 4: idx + 12])
        payload = buf[idx + 12: idx + 12 + zlen]
        if len(payload) < zlen:
            raise RuntimeError(f"FPK capture truncated: {len(payload)}/{zlen} bytes")

        img = self._decode_payload(payload, sw, sh)
        self._maybe_autoupdate_resolution(sw, sh)
        out = _encode_image(img, fmt)
        logger.debug("FPK screencap %s: %dx%d %s %dB in %.0fms",
                     self.device_id, sw, sh, fmt, len(out), (time.time() - t0) * 1000)
        return out

    async def async_screencap_bytes(self, screen_type: str = "HU", fmt: str = "png") -> bytes:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self.screencap_bytes(screen_type=screen_type, fmt=fmt))

    # ------------------------------------------------------------------
    # 화면 조작 — 이 플랫폼에서는 미지원
    # ------------------------------------------------------------------
    _NO_INPUT_MSG = ("FPK 클러스터는 화면 조작(터치/스와이프/하드키)을 지원하지 않습니다 "
                     "— 캡처·이미지 비교 전용 디바이스입니다.")

    def _no_input(self, action: str):
        raise NotImplementedError(f"{self._NO_INPUT_MSG} (요청: {action})")

    def tap(self, x: int, y: int, screen_type: str = "HU", **kwargs) -> None:
        self._no_input("tap")

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "HU", **kwargs) -> None:
        self._no_input("long_press")

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "HU", duration_ms: int = 0, **kwargs) -> None:
        self._no_input("swipe")

    def repeat_tap(self, x: int, y: int, count: int = 5,
                   screen_type: str = "HU", **kwargs) -> None:
        self._no_input("repeat_tap")

    def send_key_by_name(self, key_name: str, *args, **kwargs) -> None:
        self._no_input(f"key={key_name}")

    def send_key(self, *args, **kwargs) -> None:
        self._no_input("send_key")

    async def async_tap(self, x: int, y: int, screen_type: str = "HU") -> None:
        self._no_input("tap")

    async def async_long_press(self, x: int, y: int, duration_ms: int = 3000,
                               screen_type: str = "HU") -> None:
        self._no_input("long_press")

    async def async_swipe(self, x1: int, y1: int, x2: int, y2: int,
                          screen_type: str = "HU", duration_ms: int = 0, **kwargs) -> None:
        self._no_input("swipe")

    async def async_repeat_tap(self, x: int, y: int, count: int = 5,
                               screen_type: str = "HU", **kwargs) -> None:
        self._no_input("repeat_tap")

    async def async_send_key_by_name(self, key_name: str, *args, **kwargs) -> None:
        self._no_input(f"key={key_name}")

    async def async_send_key(self, *args, **kwargs) -> None:
        self._no_input("send_key")

    # ------------------------------------------------------------------
    def get_info(self) -> dict:
        return {
            "type": "fpk_agent",
            "host": self.host,
            "connect_host": self.connect_host,
            "ipv6_address": self.ipv6_address,
            "port": self.port,
            "device_id": self.device_id,
            "connected": self._connected,
            "agent_version": self.agent_version,
            "resolution": self._resolution,
            "fb_device": self.fb_device,
            "fb_info": self.fb_info,
            "pixel_order": self.pixel_order,
            "zlib_level": self.zlib_level,
            "input_supported": False,
            "screens": {"HU": {"width": self.res_x, "height": self.res_y}},
        }
