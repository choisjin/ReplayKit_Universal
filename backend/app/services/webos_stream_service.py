"""WebOS(Linux VM) 화면 미러링/터치 — screenBridge ``linuxStream`` 경로 재현.

배경
----
HKMC 일부 HU 는 Android(= iSAP agent 탑재) 위에서 webOS 가 **별도 Linux VM** 으로 돌고,
그 화면은 iSAP ``CMD_GETIMG`` 캡처에 잡히지 않는다(다른 VM 의 스캔아웃이라 HW 레이어로
합성됨 — MIB 미러의 CVBS 오버레이 미캡처와 같은 부류). ``Reference/screenBridge`` 는
이 화면을 아래 경로로 가져온다. 본 모듈은 그 경로를 그대로 구현한 것이다.

    PC ──adb exec-out──> Android HU ──/data/ssh_ (SSH)──> Linux VM(172.16.4.1)
                                                            └ /tmp/linuxStream
                                                              (/dev/dri/card0 DRM 스캔아웃
                                                               → JPEG 스트림 → stdout)

화면 구성(참조본 screenBridge 의 합성 순서)::

    Linux(webOS) → QNX dpy-N…dpy-2 → **Android(hole)** → QNX dpy-1

Android 레이어에는 hole(예: [970,0,3840,1440]) 이 뚫려 있고 그 구멍으로 Linux VM 의
스캔아웃(webOS)이 비친다. 그래서 **scrcpy(Android 캡처)로는 webOS 영역이 안 보이고**,
webOS 화면을 보려면 이 모듈의 Linux 스트림이 필요하다 (2026-09-11 실기 확인).

터치는 여기서 보내지 않는다. 참조본 뷰어도 Linux 레이어엔 터치를 보내지 않고
Android/QNX 로만 보내는데, webOS 투사 앱이 Android 터치를 받아 넘기기 때문이다.
→ ReplayKit 도 **터치는 ADB(`input tap`)**, 화면만 이 스트림을 쓴다.
(linuxStream 바이너리에 /dev/uinput 주입 기능이 있지만 사용하지 않는다.)

와이어 포맷 (screenStream/linuxStream 공통, screenBridge.exe 역분석으로 확인)
--------------------------------------------------------------------------
  프레임 타입은 3종이다 (payload 첫 바이트). ⚠ 'S' 는 풀 프레임이 아니라 **skip** 이다 —
  정적 화면에서 매 프레임 날아오며 payload 가 1바이트뿐이라, 풀 프레임으로 오인해 파싱하면
  즉시 IndexError 로 스트림이 죽는다.

  풀 프레임 'L'::

      [4B total_len BE][1B 'L'][1B N]
      [N x [4B rgb_size][rgb_jpeg]]
      [N x [4B alpha_size][alpha_jpeg]]

  skip 프레임 'S'::

      [4B total_len BE][1B 'S']       # 변경 없음 → 마지막 프레임 유지

  델타 프레임::

      [4B total_len BE][1B 'D'][1B tile_cols][1B tile_rows]
      [N x 4B dirty_mask]           # 디스플레이별 타일 비트마스크
      [1B alpha_flags]
      [dirty 타일들: 각 [4B size][jpeg]]   # 디스플레이 순서 → 타일 인덱스 순서
      [alpha_flags 가 선 디스플레이의 alpha JPEG: 각 [4B size][jpeg]]

  터치 패킷(7B)::

      [0xAA][type][touch_id][x_hi][x_lo][y_hi][y_lo]   # type 0=press 1=move 2=release

Linux 는 최하단 불투명 레이어라 alpha 는 쓰지 않는다(파싱만 하고 버린다).
"""

from __future__ import annotations

import asyncio
import logging
import re
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .adb_path import resolve_adb_path

logger = logging.getLogger(__name__)

# 디바이스(Android) 측 경로 — screenBridge run_viewer.bat 과 동일하게 맞춘다.
DEV_SSH = "/data/ssh_"
DEV_LIBSSH = "/data/libssh.so"
DEV_ASKPASS = "/data/askpass_linux.sh"
DEV_STREAM_BIN = "/data/linuxStream"

# Linux VM(webOS) 측 경로
REMOTE_BIN = "/tmp/linuxStream"
REMOTE_LOG = "/tmp/linuxStream.log"

_MAX_FRAME = 50_000_000  # screenBridge 와 동일한 프레임 크기 상한(깨진 스트림 조기 검출)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _bundle_dir() -> Path:
    """번들 바이너리(linuxStream/ssh_/libssh.so) 디렉토리.

    adb_path.resolve_adb_path() 와 같은 우선순위(repo → CWD → 설치 경로)를 따른다.
    """
    rel = Path("tools") / "webos"
    candidates = [
        Path(__file__).resolve().parents[3] / rel,   # <repo>/tools/webos
        Path.cwd() / rel,
    ]
    if sys.platform == "win32":
        candidates.append(Path(r"C:\ReplayKit") / rel)
    else:
        candidates.append(Path("/opt/ReplayKit") / rel)
        candidates.append(Path.home() / ".local" / "share" / "ReplayKit" / rel)
    for c in candidates:
        try:
            if (c / "linuxStream").is_file():
                return c
        except OSError:
            continue
    return candidates[0]


class WebOSStreamError(RuntimeError):
    """WebOS 스트림 준비/기동 실패 — 사용자에게 그대로 노출할 메시지를 담는다."""


class WebOSStreamService:
    """Android HU 를 경유해 webOS(Linux VM) 화면을 스트리밍하고 터치를 주입한다.

    한 인스턴스 = 한 대의 HU(adb serial) = 한 개의 webOS 화면.
    ``start()`` 는 비용이 큰 준비 작업(바이너리 배포 + SSH preflight)을 포함하므로
    첫 사용 시 한 번만 수행하고, 이후에는 ``ensure_running()`` 이 살아있는지만 본다.
    """

    def __init__(self, adb_serial: str, linux_ip: str = "172.16.4.1",
                 linux_user: str = "root", linux_password: str = "root",
                 scale: int = 2, quality: int = 60, fps: int = 30,
                 idle_timeout: float = 120.0, device_id: str = ""):
        self.adb_serial = (adb_serial or "").strip()
        self.linux_ip = (linux_ip or "172.16.4.1").strip()
        self.linux_user = (linux_user or "root").strip()
        self.linux_password = linux_password if linux_password is not None else "root"
        self.scale = max(1, int(scale or 1))
        self.quality = max(1, min(100, int(quality or 60)))
        self.fps = max(1, int(fps or 30))
        self.idle_timeout = float(idle_timeout or 0)
        self.device_id = device_id

        self._adb = resolve_adb_path()
        self._bundle = _bundle_dir()

        self._proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()          # start/stop 직렬화

        self._frame: Optional[np.ndarray] = None   # BGR 합성 프레임
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._frame_count = 0
        self._last_error = ""
        self._last_use = time.time()
        # 기동 실패 후 재시도 쿨다운 — 미러 루프가 프레임마다(0.3s) 재시도하면서
        # 매번 20~40s 짜리 배포/핸드셰이크를 다시 돌지 않게 한다.
        self._fail_until = 0.0
        # 스트림에서 실제로 받은 첫 바이트들(진단용). 프레임이 아니라 텍스트가
        # 흘러들어오는 경우(ssh 경고 등)를 눈으로 확인하려고 남긴다.
        self._first_bytes = b""

        # linuxStream 로그에서 읽어오는 실제 좌표계
        self._stream_w = 0      # 스트리밍(=미러 이미지) 크기
        self._stream_h = 0
        self._touch_w = 0       # linuxStream 이 보고하는 uinput 크기(미사용 — 진단용)
        self._touch_h = 0
        self._deployed = False

    # ------------------------------------------------------------------
    # adb / ssh helpers
    # ------------------------------------------------------------------

    def _adb_args(self, *args: str) -> list[str]:
        base = [self._adb]
        if self.adb_serial:
            base += ["-s", self.adb_serial]
        return base + list(args)

    def _run_adb(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._adb_args(*args),
            capture_output=True, text=True, timeout=timeout,
            creationflags=_NO_WINDOW,
        )

    def _ssh_wrap(self, remote_cmd: str) -> str:
        """Linux VM 에서 실행할 명령을 Android 측 ssh_ 호출 문자열로 감싼다.

        run_viewer.bat 과 동일하게 작은따옴표는 ``'"'"'`` 로 이스케이프한다.
        """
        esc = remote_cmd.replace("'", "'\"'\"'")
        return (
            f"LD_LIBRARY_PATH=/data SSH_ASKPASS={DEV_ASKPASS} DISPLAY=:0 {DEV_SSH} "
            f"-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -q "
            f"{self.linux_user}@{self.linux_ip} '{esc}'"
        )

    def _ssh(self, remote_cmd: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
        return self._run_adb("shell", self._ssh_wrap(remote_cmd), timeout=timeout)

    # ------------------------------------------------------------------
    # Deploy (Android 측 ssh_ + Linux VM 측 linuxStream)
    # ------------------------------------------------------------------

    def _android_size_matches(self, remote_path: str, local_size: int) -> bool:
        """Android 측 파일이 이미 같은 크기면 True (재-push 생략용)."""
        try:
            r = self._run_adb("shell", f"stat -c %s {remote_path} 2>/dev/null || echo 0",
                              timeout=10.0)
            return int((r.stdout or "0").strip().split()[-1]) == local_size
        except Exception:
            return False

    def _deploy(self) -> None:
        if self._deployed:
            return
        if not self.adb_serial:
            raise WebOSStreamError("WebOS: ADB 시리얼이 설정되지 않았습니다")

        ssh_bin = self._bundle / "ssh_"
        ssh_lib = self._bundle / "libssh.so"
        stream_bin = self._bundle / "linuxStream"
        for p in (ssh_bin, ssh_lib, stream_bin):
            if not p.is_file():
                raise WebOSStreamError(f"WebOS: 번들 바이너리 없음 — {p}")

        # 1) adb root — /data 쓰기와 /data/ssh_ 실행에 필요.
        #    ⚠ adb root 는 adbd 를 **재시작**한다. 같은 HU 를 ADB 디바이스로도 등록해
        #    쓰고 있으면(Connect Wide 하드키/미러링) 그 트랜스포트가 끊겨 플래핑한다.
        #    → 이미 root 면 건너뛴다. (iSAP TCP 소켓은 별개 프로세스라 무관)
        already_root = False
        try:
            r = self._run_adb("shell", "id -u", timeout=10.0)
            already_root = (r.stdout or "").strip().endswith("0")
        except Exception:
            already_root = False
        if not already_root:
            logger.info("WebOS: adb root 실행 (adbd 재시작 — 같은 serial 의 ADB 연결이 잠시 끊길 수 있음): %s",
                        self.adb_serial)
            self._run_adb("root", timeout=20.0)
            self._run_adb("wait-for-device", timeout=30.0)

        # 2) ssh_ / libssh.so / askpass 배포 — 이미 같은 크기면 push 생략.
        #    iSAP 가 같은 이더넷 링크를 쓰는 환경(네트워크 adb)에서 재연결마다 1MB 를
        #    다시 밀어 iSAP 캡처와 대역폭을 다투지 않게 한다.
        if not self._android_size_matches(DEV_SSH, ssh_bin.stat().st_size):
            r = self._run_adb("push", str(ssh_bin), DEV_SSH, timeout=60.0)
            if r.returncode != 0:
                raise WebOSStreamError(f"WebOS: ssh_ push 실패 — {(r.stderr or '').strip()}")
            self._run_adb("shell", f"chmod 755 {DEV_SSH}", timeout=15.0)
        if not self._android_size_matches(DEV_LIBSSH, ssh_lib.stat().st_size):
            r = self._run_adb("push", str(ssh_lib), DEV_LIBSSH, timeout=60.0)
            if r.returncode != 0:
                raise WebOSStreamError(f"WebOS: libssh.so push 실패 — {(r.stderr or '').strip()}")
        self._run_adb(
            "shell",
            f"echo '#!/system/bin/sh' > {DEV_ASKPASS}; "
            f"echo 'echo {self.linux_password}' >> {DEV_ASKPASS}; "
            f"chmod 755 {DEV_ASKPASS}",
            timeout=15.0,
        )

        # 3) SSH preflight — 여기서 막히면 원인이 네트워크/계정임을 분명히 알린다.
        r = self._ssh("echo LINUX_SSH_OK", timeout=30.0)
        if "LINUX_SSH_OK" not in (r.stdout or ""):
            raise WebOSStreamError(
                f"WebOS: Linux VM({self.linux_user}@{self.linux_ip}) SSH 실패 — "
                f"{((r.stderr or '') + (r.stdout or '')).strip()[:200]}"
            )

        # 4) linuxStream 배포 — 크기가 같으면 재전송 생략(재연결 때마다 push 하지 않게).
        local_size = stream_bin.stat().st_size
        r = self._ssh(f"stat -c %s {REMOTE_BIN} 2>/dev/null || echo 0", timeout=20.0)
        try:
            remote_size = int((r.stdout or "0").strip().split()[-1])
        except Exception:
            remote_size = 0
        if remote_size != local_size:
            r = self._run_adb("push", str(stream_bin), DEV_STREAM_BIN, timeout=60.0)
            if r.returncode != 0:
                raise WebOSStreamError(f"WebOS: linuxStream push 실패 — {(r.stderr or '').strip()}")
            self._ssh(f"pkill -x linuxStream 2>/dev/null; rm -f {REMOTE_BIN}; true", timeout=20.0)
            # Android → Linux VM 전송은 stdin 파이프(cat) 로 — scp 가 없는 환경 대비.
            shell_cmd = (
                f"cat {DEV_STREAM_BIN} | "
                + self._ssh_wrap(f"cat > {REMOTE_BIN}; chmod +x {REMOTE_BIN}")
            )
            r = self._run_adb("shell", shell_cmd, timeout=120.0)
            if r.returncode != 0:
                raise WebOSStreamError(
                    f"WebOS: linuxStream 전송 실패 — {(r.stderr or '').strip()[:200]}"
                )
            logger.info("WebOS: linuxStream deployed to %s (%d bytes)", self.linux_ip, local_size)

        self._deployed = True

    # ------------------------------------------------------------------
    # Stream lifecycle
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        p = self._proc
        return bool(p and p.poll() is None and not self._stop.is_set())

    @property
    def last_error(self) -> str:
        return self._last_error

    def ensure_running(self, timeout: float = 40.0) -> None:
        """스트림이 없으면 기동한다(첫 프레임 수신까지 대기)."""
        self._last_use = time.time()
        if self.is_running and self._frame is not None:
            return
        self.start(timeout=timeout)

    def start(self, timeout: float = 40.0) -> None:
        if time.time() < self._fail_until:
            raise WebOSStreamError(self._last_error or "WebOS: 기동 실패 (재시도 대기 중)")
        try:
            self._start_inner(timeout)
        except Exception as e:
            self._last_error = str(e)
            self._fail_until = time.time() + 15.0
            raise

    def _start_inner(self, timeout: float) -> None:
        with self._lock:
            if self.is_running and self._frame is not None:
                return
            self._stop_locked()
            self._deploy()

            self._stop.clear()
            self._frame_event.clear()
            self._frame_count = 0
            self._last_error = ""
            self._first_bytes = b""

            # 이전 스트림 정리는 **별도 세션**에서. 런처와 같은 명령줄에 두면
            # `pkill -f` 가 자기 자신(그 명령줄에 "linuxStream" 을 포함한 sh)까지
            # 매칭해 방금 띄운 스트림을 즉사시킨다 → 프레임이 하나도 안 온다.
            # 이름 정확 매칭(-x)으로 셸 오탐도 막는다.
            self._ssh("pkill -x linuxStream 2>/dev/null; true", timeout=20.0)
            remote = (
                f"{REMOTE_BIN} -scale={self.scale} -quality={self.quality} "
                f"-fps={self.fps} 2>{REMOTE_LOG}"
            )
            cmd = self._adb_args(
                "exec-out", "sh", "-c", self._ssh_wrap(remote) + " 2>/dev/null")
            logger.info("WebOS: starting stream serial=%s linux=%s scale=%d q=%d fps=%d",
                        self.adb_serial, self.linux_ip, self.scale, self.quality, self.fps)
            try:
                self._proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    creationflags=_NO_WINDOW,
                )
            except Exception as e:
                raise WebOSStreamError(f"WebOS: adb exec-out 기동 실패 — {e}")

            self._reader = threading.Thread(
                target=self._read_loop, name=f"webos-rx-{self.device_id or self.adb_serial}",
                daemon=True,
            )
            self._reader.start()

            if self.idle_timeout > 0 and (self._reaper is None or not self._reaper.is_alive()):
                self._reaper = threading.Thread(
                    target=self._idle_reaper, name="webos-idle", daemon=True)
                self._reaper.start()

        # 첫 프레임 대기 — 여기서 실패하면 원인을 Linux 측 로그에서 읽어 붙인다.
        if not self._frame_event.wait(timeout=timeout):
            err = self._last_error or self._read_remote_log()
            self.stop()
            raise WebOSStreamError(f"WebOS: 첫 프레임 수신 실패({timeout:.0f}s) — {err or '원인 불명'}")
        self._probe_geometry()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stop.set()
        for attr in ("_proc",):
            p = getattr(self, attr)
            if p is not None:
                try:
                    if p.stdin:
                        p.stdin.close()
                except Exception:
                    pass
                try:
                    p.kill()
                except Exception:
                    pass
                setattr(self, attr, None)
        if self._reader and self._reader.is_alive() and self._reader is not threading.current_thread():
            self._reader.join(timeout=3)
        self._reader = None
        with self._frame_lock:
            self._frame = None
        self._frame_event.clear()

    def _idle_reaper(self) -> None:
        """일정 시간 프레임/터치 요청이 없으면 스트림을 내려 디바이스 부하를 없앤다."""
        while not self._stop.is_set():
            time.sleep(5.0)
            if not self.is_running:
                continue
            if time.time() - self._last_use > self.idle_timeout:
                logger.info("WebOS: idle %.0fs — stopping stream (%s)",
                            self.idle_timeout, self.device_id or self.adb_serial)
                self.stop()
                return

    def _read_remote_log(self) -> str:
        try:
            r = self._ssh(f"tail -n 20 {REMOTE_LOG} 2>/dev/null", timeout=15.0)
            return (r.stdout or "").strip()[:400]
        except Exception:
            return ""

    def _probe_geometry(self) -> None:
        """linuxStream 로그에서 스트림/터치 좌표계를 읽는다.

        ``native=%ux%u scale=%d -> %ux%u q=%d fps=%d swap=%d``
        ``touch injection enabled (%ux%u)``
        터치 주입 좌표계가 스트림 해상도와 다를 수 있어(스케일 전 native) 반드시 확인한다.
        """
        log = self._read_remote_log()
        if not log:
            return
        m = re.search(r"native=(\d+)x(\d+)\s+scale=\d+\s*->\s*(\d+)x(\d+)", log)
        if m:
            self._stream_w, self._stream_h = int(m.group(3)), int(m.group(4))
        m = re.search(r"touch injection enabled \((\d+)x(\d+)\)", log)
        if m:
            self._touch_w, self._touch_h = int(m.group(1)), int(m.group(2))

        logger.info("WebOS: geometry stream=%dx%d touch=%dx%d",
                    self._stream_w, self._stream_h, self._touch_w, self._touch_h)

    # ------------------------------------------------------------------
    # Frame reader
    # ------------------------------------------------------------------

    def _read_exact(self, stream, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                raise EOFError("WebOS stream ended")
            buf += chunk
            if len(self._first_bytes) < 256:
                self._first_bytes += chunk[:256 - len(self._first_bytes)]
        return buf

    def _head_preview(self) -> str:
        """수신 첫 바이트를 hex + ascii 로 — 프레임인지 텍스트인지 한눈에 본다."""
        b = self._first_bytes[:64]
        if not b:
            return "(수신 없음)"
        ascii_ = "".join(chr(c) if 32 <= c < 127 else "." for c in b)
        return f"hex={b.hex()} ascii={ascii_!r}"

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        stream = proc.stdout
        try:
            while not self._stop.is_set():
                hdr = self._read_exact(stream, 4)
                (size,) = struct.unpack(">I", hdr)
                if size == 0 or size > _MAX_FRAME:
                    # 프레임이 아니라 텍스트(ssh 경고/adb 에러)가 흘러들어온 경우가 많다.
                    raise ValueError(
                        f"Invalid frame size: {size} hdr={hdr.hex()} "
                        f"stream_head={self._head_preview()}"
                    )
                data = self._read_exact(stream, size)
                ftype = data[0:1]
                if ftype == b"L":
                    self._on_full_frame(data)
                elif ftype == b"D":
                    self._on_delta_frame(data)
                elif ftype == b"S":
                    # skip = 직전 프레임과 동일. payload 는 1바이트뿐이라 파싱하지 않는다.
                    # (정적 화면이면 이것만 계속 온다 — 미러는 마지막 프레임을 그대로 유지)
                    if self._frame is not None:
                        self._frame_count += 1
                        self._frame_event.set()
                # 그 외 타입은 무시 (프로토콜 확장 대비)
        except Exception as e:
            if not self._stop.is_set():
                self._last_error = f"{type(e).__name__}: {e}"
                logger.warning(
                    "WebOS: stream ended (%s) frames=%d recv=%dB %s",
                    self._last_error, self._frame_count,
                    len(self._first_bytes), self._head_preview(),
                    exc_info=True,
                )
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    def _on_full_frame(self, data: bytes) -> None:
        if len(data) < 2:
            return
        num = data[1]
        off = 2
        rgb_jpegs: list[bytes] = []
        for _ in range(num):
            (sz,) = struct.unpack(">I", data[off:off + 4])
            off += 4
            rgb_jpegs.append(data[off:off + sz])
            off += sz
        # alpha 는 Linux(불투명 베이스)라 쓰지 않는다 — 파싱 생략.
        if not rgb_jpegs:
            return
        img = cv2.imdecode(np.frombuffer(rgb_jpegs[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return
        with self._frame_lock:
            self._frame = img
        self._frame_count += 1
        if not self._frame_event.is_set():
            h, w = img.shape[:2]
            if not self._stream_w:
                self._stream_w, self._stream_h = w, h
            logger.info("WebOS: first frame %dx%d displays=%d", w, h, num)
        self._frame_event.set()

    def _on_delta_frame(self, data: bytes) -> None:
        with self._frame_lock:
            base = self._frame
            if base is None:
                return  # 풀 프레임 이전의 델타는 버린다
            if len(data) < 8:          # [1B type][2B cols/rows][4B mask][1B alpha_flags]
                return
            frame = base  # in-place 갱신 (읽기는 encode 시점에 복사)
            fh, fw = frame.shape[:2]

            tile_cols = data[1]
            tile_rows = data[2]
            off = 3
            # 디스플레이 개수는 풀 프레임에서 1개만 쓰므로 마스크도 1개만 읽는다.
            # (Linux 스트림은 항상 단일 디스플레이 — 다중이면 첫 번째만 표시)
            (mask,) = struct.unpack(">I", data[off:off + 4])
            off += 4
            off += 1  # alpha_flags

            tw = fw // tile_cols if tile_cols > 0 else fw
            th = fh // tile_rows if tile_rows > 0 else fh
            for i in range(max(1, tile_cols) * max(1, tile_rows)):
                if not (mask & (1 << i)):
                    continue
                if off + 4 > len(data):
                    break
                (ts,) = struct.unpack(">I", data[off:off + 4])
                off += 4
                tile = cv2.imdecode(np.frombuffer(data[off:off + ts], dtype=np.uint8),
                                    cv2.IMREAD_COLOR)
                off += ts
                if tile is None:
                    continue
                col = i % tile_cols if tile_cols else 0
                row = i // tile_cols if tile_cols else 0
                x0, y0 = col * tw, row * th
                h, w = tile.shape[:2]
                if x0 + w > fw:
                    w = fw - x0
                if y0 + h > fh:
                    h = fh - y0
                if w <= 0 or h <= 0:
                    continue
                frame[y0:y0 + h, x0:x0 + w] = tile[:h, :w]
            self._frame_count += 1
        self._frame_event.set()

    # ------------------------------------------------------------------
    # Public: capture
    # ------------------------------------------------------------------

    @property
    def size(self) -> tuple[int, int]:
        """미러 이미지(=터치 입력 좌표계) 크기."""
        if self._stream_w and self._stream_h:
            return self._stream_w, self._stream_h
        with self._frame_lock:
            if self._frame is not None:
                h, w = self._frame.shape[:2]
                return w, h
        return 0, 0

    def screencap_bytes(self, fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        # 첫 호출은 배포(push/ssh)까지 포함이라 호출자의 짧은 타임아웃(미러 3s)으로는 부족하다.
        self.ensure_running(timeout=max(timeout, 60.0))
        self._last_use = time.time()
        with self._frame_lock:
            frame = None if self._frame is None else self._frame.copy()
        if frame is None:
            raise WebOSStreamError("WebOS: 프레임 없음")
        ext = ".png" if fmt == "png" else ".jpg"
        params = [] if fmt == "png" else [cv2.IMWRITE_JPEG_QUALITY, 70]
        ok, buf = cv2.imencode(ext, frame, params)
        if not ok:
            raise WebOSStreamError("WebOS: 프레임 인코딩 실패")
        return buf.tobytes()

    # ------------------------------------------------------------------
    # Async wrappers (iSAP 서비스와 시그니처를 맞춘다)
    # ------------------------------------------------------------------

    async def async_screencap_bytes(self, fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.screencap_bytes(fmt, timeout))

    def probe(self) -> dict:
        """WebOS 경로를 단계별로 점검한다 — 어디서 막혔는지 한 번에 보여주는 브링업 도구.

        스트림을 띄우지 않고 각 구간(adb → Android ssh_ → Linux VM → linuxStream)만
        확인한다. 단계마다 SSH 세션을 새로 여므로 수 초 걸린다.
        """
        steps: list[dict] = []

        def _cap(r) -> str:
            return ((r.stdout or "") + (r.stderr or "")).strip()

        def _step(name: str, fn) -> None:
            try:
                steps.append({"step": name, "result": str(fn())[:800]})
            except Exception as e:
                steps.append({"step": name, "result": f"ERROR {type(e).__name__}: {e}"})

        _step("1. adb get-state",
              lambda: _cap(self._run_adb("get-state", timeout=10.0)) or "(빈 응답)")
        _step("2. adb shell id -u (0=root)",
              lambda: _cap(self._run_adb("shell", "id -u", timeout=10.0)))
        _step("3. Android /data/ssh_", lambda: _cap(self._run_adb(
            "shell", f"ls -l {DEV_SSH} {DEV_LIBSSH} {DEV_ASKPASS} 2>&1", timeout=10.0)))
        _step("4. Linux VM SSH (echo)",
              lambda: _cap(self._ssh("echo LINUX_SSH_OK", timeout=30.0))
              or "(빈 응답 — SSH 실패 가능)")
        _step("5. VM /dev/dri", lambda: _cap(self._ssh("ls -l /dev/dri 2>&1", timeout=20.0)))
        _step("6. VM /tmp/linuxStream",
              lambda: _cap(self._ssh(f"ls -l {REMOTE_BIN} 2>&1", timeout=20.0)))
        _step("7. VM linuxStream 실행 중?", lambda: _cap(self._ssh(
            "pgrep -x linuxStream 2>/dev/null || echo NONE", timeout=20.0)))
        _step("8. VM linuxStream.log", lambda: _cap(self._ssh(
            f"tail -n 30 {REMOTE_LOG} 2>&1", timeout=20.0)) or "(로그 없음 — 아직 실행 안 됨)")

        info = self.get_info()
        info["bundle_dir"] = str(self._bundle)
        info["adb_path"] = self._adb
        info["deployed"] = self._deployed
        return {"info": info, "steps": steps}

    def get_info(self) -> dict:
        w, h = self.size
        return {
            "adb_serial": self.adb_serial,
            "linux_ip": self.linux_ip,
            "running": self.is_running,
            "frames": self._frame_count,
            "width": w,
            "height": h,
            "touch_width": self._touch_w,
            "touch_height": self._touch_h,
            "last_error": self._last_error,
        }
