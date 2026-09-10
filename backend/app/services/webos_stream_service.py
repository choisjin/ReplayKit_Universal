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

터치는 ``linuxStream`` 이 ``/dev/uinput`` 으로 주입한다(바이너리 usage 의 ``--no-touch``
로 끌 수 있음 = 기본 켜짐). 입력은 **stdin(fd 0) 바이트 스트림** 이다
(ELF 동적 심볼에 socket/bind/connect 가 없고 ``read``/``open`` 만 있으며, 경로 문자열도
``/dev/uinput``·``/dev/dri/card0`` 뿐이라 stdin 외의 입력 채널이 없다).

  ⚠ ``adb exec-out`` 은 device→PC 단방향(stdin 미전달)이라 같은 프로세스로는 터치를 못 보낸다.
    그래서 Linux VM 에 FIFO 를 만들고 ``linuxStream ... 0<>FIFO`` 로 stdin 을 붙인 뒤,
    **별도 ``adb exec-in`` 세션**(``cat > FIFO``)으로 터치 패킷을 흘려 넣는다.
    ``0<>`` (read-write open) 라서 writer 가 없어도 열기가 막히지 않고 EOF 도 나지 않는다.

  ⚠ screenBridge 뷰어 자체는 Linux 레이어에 터치를 보내지 않는다(QNX/Android 로만 보냄).
    즉 uinput 주입 경로는 바이너리에만 존재하는 **미검증** 경로다. 실기에서 안 먹으면
    디바이스 설정 ``webos_touch_via="isap"`` 로 iSAP 전석 터치에 위임할 수 있다
    (isap_agent_service 쪽에서 분기).

와이어 포맷 (screenStream/linuxStream 공통, screenBridge.exe 역분석으로 확인)
--------------------------------------------------------------------------
  풀 프레임::

      [4B total_len BE][1B 'S'][1B N]
      [N x [4B rgb_size][rgb_jpeg]]
      [N x [4B alpha_size][alpha_jpeg]]

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
REMOTE_FIFO = "/tmp/linuxStream.touch"

TOUCH_MAGIC = 0xAA
TOUCH_PRESS = 0
TOUCH_MOVE = 1
TOUCH_RELEASE = 2

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
        self._touch_proc: Optional[subprocess.Popen] = None
        self._reader: Optional[threading.Thread] = None
        self._reaper: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._lock = threading.Lock()          # start/stop 직렬화
        self._touch_lock = threading.Lock()    # 터치 패킷 원자 송신

        self._frame: Optional[np.ndarray] = None   # BGR 합성 프레임
        self._frame_lock = threading.Lock()
        self._frame_event = threading.Event()
        self._frame_count = 0
        self._last_error = ""
        self._last_use = time.time()
        # 기동 실패 후 재시도 쿨다운 — 미러 루프가 프레임마다(0.3s) 재시도하면서
        # 매번 20~40s 짜리 배포/핸드셰이크를 다시 돌지 않게 한다.
        self._fail_until = 0.0

        # linuxStream 로그에서 읽어오는 실제 좌표계
        self._stream_w = 0      # 스트리밍(=미러 이미지) 크기
        self._stream_h = 0
        self._touch_w = 0       # uinput 등록 크기(터치 좌표계)
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

        # 1) adb root (webOS/Linux 로 가는 ssh_ 실행과 /data 쓰기에 필요)
        self._run_adb("root", timeout=20.0)
        self._run_adb("wait-for-device", timeout=30.0)

        # 2) ssh_ / libssh.so / askpass 배포
        r = self._run_adb("push", str(ssh_bin), DEV_SSH, timeout=60.0)
        if r.returncode != 0:
            raise WebOSStreamError(f"WebOS: ssh_ push 실패 — {(r.stderr or '').strip()}")
        self._run_adb("shell", f"chmod 755 {DEV_SSH}", timeout=15.0)
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
            self._ssh(f"pkill -f linuxStream; rm -f {REMOTE_BIN}", timeout=20.0)
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

            # FIFO 로 stdin 을 붙여 터치 주입 채널을 연다. 0<> (read-write) 라
            # writer 가 붙기 전에도 열기가 막히지 않고, writer 가 끊겨도 EOF 가 안 난다.
            remote = (
                f"pkill -f {REMOTE_BIN}; rm -f {REMOTE_FIFO}; mkfifo {REMOTE_FIFO} 2>/dev/null; "
                f"({REMOTE_BIN} -scale={self.scale} -quality={self.quality} -fps={self.fps} "
                f"0<>{REMOTE_FIFO}) 2>{REMOTE_LOG}"
            )
            cmd = self._adb_args("exec-out", "sh", "-c", self._ssh_wrap(remote))
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
        for attr in ("_touch_proc", "_proc"):
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
        else:
            logger.warning("WebOS: linuxStream 로그에 touch injection 표시 없음 — "
                           "터치가 비활성일 수 있습니다 (%s)", log[-160:])
        logger.info("WebOS: geometry stream=%dx%d touch=%dx%d",
                    self._stream_w, self._stream_h, self._touch_w, self._touch_h)

    # ------------------------------------------------------------------
    # Frame reader
    # ------------------------------------------------------------------

    @staticmethod
    def _read_exact(stream, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = stream.read(n - len(buf))
            if not chunk:
                raise EOFError("WebOS stream ended")
            buf += chunk
        return buf

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
                    raise ValueError(f"Invalid frame size: {size} hdr={hdr.hex()}")
                data = self._read_exact(stream, size)
                ftype = data[0:1]
                if ftype == b"S":
                    self._on_full_frame(data)
                elif ftype == b"D":
                    self._on_delta_frame(data)
                # 그 외 타입은 무시 (프로토콜 확장 대비)
        except Exception as e:
            if not self._stop.is_set():
                self._last_error = f"{type(e).__name__}: {e}"
                logger.warning("WebOS: stream ended (%s) frames=%d", self._last_error, self._frame_count)
        finally:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:
                pass

    def _on_full_frame(self, data: bytes) -> None:
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
    # Public: touch (linuxStream uinput 주입)
    # ------------------------------------------------------------------

    def _ensure_touch_channel(self) -> None:
        p = self._touch_proc
        if p is not None and p.poll() is None:
            return
        # exec-in = PC stdin → 디바이스 (exec-out 과 반대 방향). ssh 로 Linux VM 의
        # FIFO 에 그대로 흘려 넣으면 linuxStream 이 stdin 으로 읽는다.
        cmd = self._adb_args("exec-in", "sh", "-c", self._ssh_wrap(f"cat > {REMOTE_FIFO}"))
        self._touch_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        logger.info("WebOS: touch channel opened (%s)", self.device_id or self.adb_serial)

    def _map_xy(self, x: float, y: float) -> tuple[int, int]:
        """미러 좌표 → uinput 좌표. 두 좌표계가 같으면 그대로 통과."""
        sw, sh = self.size
        tw, th = (self._touch_w, self._touch_h)
        if sw and tw and sw != tw:
            x = x * tw / sw
        if sh and th and sh != th:
            y = y * th / sh
        return int(round(x)), int(round(y))

    def _send_touch(self, ttype: int, x: float, y: float, touch_id: int = 0) -> None:
        self.ensure_running()
        self._last_use = time.time()
        ix, iy = self._map_xy(x, y)
        ix = max(0, min(65535, ix))
        iy = max(0, min(65535, iy))
        pkt = bytes([TOUCH_MAGIC, ttype & 0xFF, touch_id & 0xFF,
                     (ix >> 8) & 0xFF, ix & 0xFF, (iy >> 8) & 0xFF, iy & 0xFF])
        with self._touch_lock:
            self._ensure_touch_channel()
            p = self._touch_proc
            if p is None or p.stdin is None:
                raise WebOSStreamError("WebOS: 터치 채널을 열 수 없습니다")
            try:
                p.stdin.write(pkt)
                p.stdin.flush()
            except Exception as e:
                try:
                    p.kill()
                except Exception:
                    pass
                self._touch_proc = None
                raise WebOSStreamError(f"WebOS: 터치 전송 실패 — {e}")

    def tap(self, x: int, y: int) -> None:
        self._send_touch(TOUCH_PRESS, x, y)
        time.sleep(0.05)
        self._send_touch(TOUCH_RELEASE, x, y)
        logger.info("[WebOS TAP] (%s,%s)", x, y)

    def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100) -> None:
        for i in range(max(1, count)):
            self.tap(x, y)
            if i < count - 1 and interval_ms > 0:
                time.sleep(interval_ms / 1000.0)

    def long_press(self, x: int, y: int, duration_ms: int = 3000) -> None:
        self._send_touch(TOUCH_PRESS, x, y)
        time.sleep(max(0, duration_ms) / 1000.0)
        self._send_touch(TOUCH_RELEASE, x, y)
        logger.info("[WebOS LONG_PRESS] (%s,%s) %dms", x, y, duration_ms)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300, hold_ms: int = 0) -> None:
        dur = max(int(duration_ms or 0), 100)
        steps = max(3, min(30, dur // 20))
        self._send_touch(TOUCH_PRESS, x1, y1)
        if hold_ms and hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        interval = (dur / 1000.0) / steps
        for i in range(1, steps):
            t = i / steps
            self._send_touch(TOUCH_MOVE, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)
            time.sleep(interval)
        self._send_touch(TOUCH_RELEASE, x2, y2)
        logger.info("[WebOS SWIPE] (%s,%s)->(%s,%s) %dms", x1, y1, x2, y2, dur)

    def multi_finger_tap(self, points: list[dict]) -> None:
        for idx, p in enumerate(points):
            self._send_touch(TOUCH_PRESS, p["x"], p["y"], touch_id=idx)
        time.sleep(0.05)
        for idx, p in enumerate(points):
            self._send_touch(TOUCH_RELEASE, p["x"], p["y"], touch_id=idx)

    def multi_finger_swipe(self, fingers: list[dict], duration_ms: int = 500,
                           hold_ms: int = 0) -> None:
        if not fingers:
            return
        dur = max(int(duration_ms or 0), 100)
        steps = max(3, min(30, dur // 20))
        for idx, f in enumerate(fingers):
            self._send_touch(TOUCH_PRESS, f["x1"], f["y1"], touch_id=idx)
        if hold_ms and hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        interval = (dur / 1000.0) / steps
        for s in range(1, steps):
            t = s / steps
            for idx, f in enumerate(fingers):
                self._send_touch(TOUCH_MOVE,
                                 f["x1"] + (f["x2"] - f["x1"]) * t,
                                 f["y1"] + (f["y2"] - f["y1"]) * t, touch_id=idx)
            time.sleep(interval)
        for idx, f in enumerate(fingers):
            self._send_touch(TOUCH_RELEASE, f["x2"], f["y2"], touch_id=idx)

    # ------------------------------------------------------------------
    # Async wrappers (iSAP 서비스와 시그니처를 맞춘다)
    # ------------------------------------------------------------------

    async def async_screencap_bytes(self, fmt: str = "jpeg", timeout: float = 10.0) -> bytes:
        return await asyncio.get_event_loop().run_in_executor(
            None, lambda: self.screencap_bytes(fmt, timeout))

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
