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

터치 (2026-09-11 실기로 확정한 경로)
-----------------------------------
**Linux VM 의 실제 터치스크린 evdev 노드에 직접 write** 한다. 아래 두 경로는 안 먹었다.
  * Android ``input tap`` — webOS 영역은 Android 레이어의 hole 이라 무반응.
  * linuxStream 의 ``/dev/uinput`` 주입 — 패킷은 도달·파싱되는데(로그 ``touch type=0``)
    화면 무반응. 만들어진 장치가 ``PROP=0``(INPUT_PROP_DIRECT 없음) + ``ABS_X/Y/PRESSURE``
    (ABS_MT_* 없음) 인 구형 단일터치 스펙이라 webOS 입력 스택이 터치스크린으로 안 본다.
    (실제 터치스크린은 ``PROP=2`` + MT_SLOT/TRACKING_ID/MT_POSITION_X/Y)

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

# evdev 주입용 상수 (linux/input-event-codes.h)
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39


def _ev(etype: int, code: int, value: int) -> bytes:
    """struct input_event — aarch64: timeval(16B) + type/code/value = 24B.

    시각은 0 으로 둔다(커널이 input_inject_event 에서 현재 시각으로 채운다).
    """
    return struct.pack("<qqHHi", 0, 0, etype, code, value)


def _mt_down(x: int, y: int, slot: int = 0, tracking: int = 1) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_TRACKING_ID, tracking),
        _ev(EV_ABS, ABS_MT_POSITION_X, x),
        _ev(EV_ABS, ABS_MT_POSITION_Y, y),
        _ev(EV_KEY, BTN_TOUCH, 1),
        _ev(EV_ABS, ABS_X, x),
        _ev(EV_ABS, ABS_Y, y),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])


def _mt_move(x: int, y: int, slot: int = 0) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_POSITION_X, x),
        _ev(EV_ABS, ABS_MT_POSITION_Y, y),
        _ev(EV_ABS, ABS_X, x),
        _ev(EV_ABS, ABS_Y, y),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])


def _mt_up(slot: int = 0) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_TRACKING_ID, -1),
        _ev(EV_KEY, BTN_TOUCH, 0),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])

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


# serial 단위 공유 인스턴스 — 같은 HU 를 보는 주체가 둘 이상일 때 서로의
# 디바이스측 linuxStream 을 pkill 로 끄는 핑퐁을 막는다.
_SHARED: dict = {}
_SHARED_LOCK = threading.Lock()


def get_shared_stream(**kwargs) -> "WebOSStreamService":
    """설정이 같으면 같은 스트림 인스턴스를 돌려준다(없으면 생성).

    key 에 접속·화질 설정을 모두 넣어, 설정이 다르면 별도 인스턴스가 되게 한다.
    """
    key = (
        (kwargs.get("adb_serial") or "").strip(),
        (kwargs.get("linux_ip") or "").strip(),
        int(kwargs.get("scale") or 2),
        int(kwargs.get("quality") or 60),
        int(kwargs.get("fps") or 30),
    )
    with _SHARED_LOCK:
        svc = _SHARED.get(key)
        if svc is None:
            svc = WebOSStreamService(**kwargs)
            svc._shared_key = key
            _SHARED[key] = svc
        return svc


def drop_shared_stream(svc) -> None:
    """공유 인스턴스를 레지스트리에서 제거하고 종료(설정 변경/디바이스 삭제 시)."""
    key = getattr(svc, "_shared_key", None)
    with _SHARED_LOCK:
        if key is not None and _SHARED.get(key) is svc:
            _SHARED.pop(key, None)
    try:
        svc.stop()
    except Exception as e:
        logger.debug("WebOS shared stream stop failed: %s", e)


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
                 idle_timeout: float = 120.0, device_id: str = "",
                 evdev_path: str = ""):
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
        self._touch_lock = threading.Lock()    # 터치 이벤트 원자 송신
        self._evdev_path = ""                  # 주입 대상 터치스크린 노드(자동 탐색 캐시)
        self._forced_evdev = evdev_path or ""  # 설정으로 직접 지정한 노드

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
        # webOS 전면 여부 판별 캐시 (dumpsys 는 비싸서 TTL 내 재사용)
        self._fg_cache: tuple = (0.0, False)
        self._fg_ttl = 1.5
        self._fg_cost = 0.0      # 최근 판별 소요시간(초) — TTL 자동 조정용

        # linuxStream 로그에서 읽어오는 실제 좌표계
        self._stream_w = 0      # 스트리밍(=미러 이미지) 크기
        self._stream_h = 0
        self._touch_w = 0       # uinput 등록 크기 (linuxStream 로그)
        self._touch_h = 0
        self._native_w = 0      # Linux VM 프레임버퍼 native 크기 (= 패널 좌표계)
        self._native_h = 0
        self._deployed = False
        self._shared_key = None      # get_shared_stream 이 채운다
        # 기동 전 **살아있는** linuxStream 까지 죽일지. 기본 False —
        # 인스턴스는 공존 가능함이 실측으로 확인됐고(dual 테스트, 각 30fps),
        # 죽이면 같은 HU 를 보는 다른 주체(iSAP/ADB 디바이스, 테스트툴)의 스트림이
        # 끊긴다. 고아 정리는 항상 하므로 평소엔 켤 일이 없다.
        self.kill_existing = False

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

            self._reap_orphans()
            if self.kill_existing:
                self._ssh("pkill -x linuxStream 2>/dev/null; true", timeout=20.0)
            # -verbose 필수: 없으면 linuxStream 이 아무것도 안 찍어 /tmp/linuxStream.log 가
            # 비고 native/scale 기하를 못 읽는다. (전부 stderr → 로그 파일)
            remote = (
                f"{REMOTE_BIN} -scale={self.scale} -quality={self.quality} "
                f"-fps={self.fps} -verbose 2>{REMOTE_LOG}"
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
        # ⚠ 여기서 geometry 를 ssh 로 읽지 않는다.
        # 스트림이 사는 ssh 세션과 **동시에** 두 번째 ssh 를 붙이면 스트림이 끊긴다
        # (첫 프레임 직후 매번 EOF). native 크기는 스트림 크기 × scale 로 역산되고
        # (native_size 프로퍼티) touch= 는 쓰지 않는 진단값이라 손해가 없다.
        logger.info("WebOS: geometry stream=%dx%d native=%dx%d (scale=%d, 프레임에서 역산)",
                    *self.size, *self.native_size, self.scale)

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

    def _reap_orphans(self) -> None:
        """ssh 세션이 끊겨 고아가 된(PPID=1) linuxStream 만 정리한다.

        살아있는 남의 스트림은 건드리지 않는다 — 같은 HU 를 여러 주체가 동시에 볼 수
        있고(인스턴스 공존 확인됨), 무조건 pkill 하면 서로 끄는 핑퐁이 된다.
        """
        script = (
            "for p in $(pgrep -x linuxStream 2>/dev/null); do "
            "  ppid=$(awk '{print $4}' /proc/$p/stat 2>/dev/null); "
            "  [ \"$ppid\" = \"1\" ] && kill $p 2>/dev/null; "
            "done; true"
        )
        try:
            self._ssh(script, timeout=20.0)
        except Exception as e:
            logger.debug("WebOS: 고아 linuxStream 정리 실패 — %s", e)

    def _read_remote_log(self) -> str:
        try:
            r = self._ssh(f"tail -n 20 {REMOTE_LOG} 2>/dev/null", timeout=15.0)
            return (r.stdout or "").strip()[:400]
        except Exception:
            return ""

    def _probe_geometry(self) -> None:
        """linuxStream 로그에서 스트림/터치 좌표계를 읽는다.

        ⚠ **스트림이 도는 중에는 호출 금지** — 두 번째 ssh 세션이 스트림을 끊는다.
        진단(probe) 경로에서만 쓴다.

        ``native=%ux%u scale=%d -> %ux%u q=%d fps=%d swap=%d``
        ``touch injection enabled (%ux%u)``
        터치 주입 좌표계가 스트림 해상도와 다를 수 있어(스케일 전 native) 반드시 확인한다.
        """
        log = self._read_remote_log()
        if not log:
            return
        m = re.search(r"native=(\d+)x(\d+)\s+scale=\d+\s*->\s*(\d+)x(\d+)", log)
        if m:
            self._native_w, self._native_h = int(m.group(1)), int(m.group(2))
            self._stream_w, self._stream_h = int(m.group(3)), int(m.group(4))
        m = re.search(r"touch injection enabled \((\d+)x(\d+)\)", log)
        if m:
            self._touch_w, self._touch_h = int(m.group(1)), int(m.group(2))

        logger.info("WebOS: geometry native=%dx%d stream=%dx%d touch=%dx%d",
                    self._native_w, self._native_h,
                    self._stream_w, self._stream_h, self._touch_w, self._touch_h)
        # touch=... 는 linuxStream 이 만든 uinput 장치 크기 — 우리는 그 장치를 쓰지
        # 않는다(스펙 불일치로 webOS 가 무시함). 진단 표시 용도로만 남긴다.

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

    def is_foreground(self, display_id: Optional[int] = None,
                      max_age: Optional[float] = None) -> bool:
        """지금 화면 전면이 webOS 투사 앱인지 (Android dumpsys 기준, TTL 캐시).

        BMW 에이전트와 같은 판별이다 — ``topResumedActivity`` 가
        ``com.lge.app.car.webosprojectionhmi`` 면 webOS 화면, 그 외 패키지면 네이티브
        Android 화면. 실패하면 False(= 기존 iSAP 화면 유지).

        max_age: 허용할 캐시 나이(초). 터치처럼 틀리면 안 되는 경로는 짧게 준다
        (화면 전환 직후 낡은 캐시로 반대쪽에 주입하는 사고 방지).

        dumpsys 출력::

            Display #0 (activities from top to bottom):
                  topResumedActivity=ActivityRecord{... com.lge.app.car.settingshmi/... }
        """
        now = time.monotonic()
        # 링크가 느리면(미러링으로 adb 포화) 판별 비용이 그대로 입력 지연이 된다.
        # 실측 소요시간의 8배를 최소 간격으로 잡아 부하를 12% 이하로 억제한다.
        floor = min(10.0, max(self._fg_ttl, self._fg_cost * 8))
        ttl = floor if max_age is None else max(min(max_age, floor), 0.0)
        if max_age is not None and self._fg_cost > 0.4:
            ttl = floor          # 느린 링크에서는 터치 경로도 캐시를 존중
        if now - self._fg_cache[0] < ttl:
            return self._fg_cache[1]
        want = int(display_id or 0)
        result = False
        _t0 = time.monotonic()
        try:
            r = self._run_adb(
                "shell",
                "dumpsys activity activities | grep -iE 'Display #|topResumedActivity'",
                timeout=8.0,
            )
            cur = None
            for line in (r.stdout or "").splitlines():
                m = re.search(r"Display\s+#(\d+)", line)
                if m:
                    cur = int(m.group(1))
                    continue
                if "topResumedActivity" in line and cur is not None:
                    if cur == want:
                        result = "webosprojectionhmi" in line
                        break
                    cur = None
        except Exception as e:
            logger.debug("WebOS foreground probe failed: %s", e)
        cost = time.monotonic() - _t0
        if cost > self._fg_cost * 1.5 or cost < self._fg_cost * 0.5:
            if cost > 0.4 and self._fg_cost <= 0.4:
                logger.info("WebOS 전면 판별이 느립니다(%.2fs) — 판별 주기를 %.1fs 로 늦춥니다 "
                            "(미러링이 adb 대역을 쓰는 중일 수 있음)", cost, min(10.0, cost * 8))
            self._fg_cost = cost
        self._fg_cache = (time.monotonic(), result)
        return result

    # ------------------------------------------------------------------
    # Public: capture
    # ------------------------------------------------------------------

    @property
    def touch_size(self) -> tuple[int, int]:
        """linuxStream 이 만든 uinput 장치 크기 — **미사용**(진단 표시용).

        그 장치는 INPUT_PROP_DIRECT/ABS_MT_* 가 없어 webOS 가 터치스크린으로 보지 않는다.
        터치는 실제 터치스크린 evdev 노드에 직접 주입한다(evdev_path 참고).
        """
        return self._touch_w, self._touch_h

    @property
    def touch_size(self) -> tuple[int, int]:
        """linuxStream 이 만든 uinput 장치 크기 — **미사용**(진단 표시용).

        터치는 실제 터치스크린 evdev 노드에 직접 주입한다(evdev_path 참고).
        """
        return self._touch_w, self._touch_h

    @property
    def native_size(self) -> tuple[int, int]:
        """Linux VM 프레임버퍼 native 크기 = 패널 좌표계 = 터치 좌표계.

        linuxStream 로그를 못 읽었으면 스트림 출력 × scale 로 역산한다.
        """
        if self._native_w and self._native_h:
            return self._native_w, self._native_h
        sw, sh = self.size
        if sw and sh:
            return sw * self.scale, sh * self.scale
        return 0, 0

    @property
    def size(self) -> tuple[int, int]:
        """미러 이미지 크기(linuxStream 출력, -scale 축소본)."""
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
    # 터치 주입 — 실제 터치스크린 evdev 노드에 MT 프로토콜 B 이벤트 직접 write
    # ------------------------------------------------------------------
    #
    # 왜 이 방식인가 (2026-09-11 실기 확인):
    #   * Android `input tap` → webOS 영역은 Android 레이어의 hole 이라 반응 없음.
    #   * linuxStream 의 /dev/uinput 주입 → 패킷은 도달·파싱되는데("touch type=0" 로그)
    #     화면 무반응. 만들어지는 장치가 `PROP=0`(INPUT_PROP_DIRECT 없음) +
    #     `ABS=ABS_X/Y/PRESSURE`(ABS_MT_* 축 없음)인 **구형 단일터치 스펙**이라
    #     webOS 입력 스택이 터치스크린으로 인식하지 않는다. (실제 터치스크린은
    #     PROP=2 + MT_SLOT/TRACKING_ID/MT_POSITION_X/Y)
    #   * → evdev 노드는 write() 로 이벤트 주입이 되므로 **진짜 터치스크린 장치에**
    #     물리 터치와 동일한 시퀀스를 써 넣는다. 이게 동작한다.
    #
    # 전송은 `cat > /dev/input/eventN` 세션 하나를 열어두고 raw 구조체를 흘려보낸다
    # (제스처마다 SSH 를 새로 열면 왕복 때문에 드래그 타이밍이 무너진다).
    # ⚠ `adb exec-out` 은 stdin 을 전달하지 않으므로 반대 방향인 `adb exec-in` 을 쓴다.

    @property
    def evdev_path(self) -> str:
        """주입 대상 터치스크린 노드 (자동 탐색 결과 캐시)."""
        return self._evdev_path

    def _find_touch_device(self) -> str:
        """화면 터치스크린 evdev 노드를 찾는다 — **INPUT_PROP_DIRECT 필수 우선**.

        ⚠ MT 축(ABS_MT_POSITION_X/Y)만 보면 안 된다. **터치패드(터치 리모컨)도 MT 축을
        가진다.** 그걸 고르면 좌표가 커서 이동으로만 쓰이고 탭은 '클릭'이 돼, 어디를
        눌러도 **현재 포커스 항목이 눌리는** 증상이 난다(2026-09-11 실기: 어디를 눌러도
        SPOTV NOW). 화면 터치스크린은 PROP 에 INPUT_PROP_DIRECT(bit1)가 있고 터치패드는
        POINTER(bit0)만 있다. 장치 번호(eventN)는 부팅마다 바뀔 수 있어 순서도 믿지 않는다.
        linuxStream-touch(우리가 띄운 가짜 장치)는 제외한다.
        """
        if self._evdev_path:
            return self._evdev_path
        forced = (self._forced_evdev or "").strip()
        if forced:
            self._evdev_path = forced
            return forced
        r = self._ssh("cat /proc/bus/input/devices", timeout=20.0)
        text = (r.stdout or "") + (r.stderr or "")
        prop_direct = 1 << 1                      # INPUT_PROP_DIRECT
        direct: list = []
        others: list = []
        name = handlers = absbits = props = ""
        for line in text.splitlines() + [""]:
            line = line.strip()
            if line.startswith("N: Name="):
                name = line.split("=", 1)[1].strip('"')
            elif line.startswith("H: Handlers="):
                handlers = line.split("=", 1)[1]
            elif line.startswith("B: PROP="):
                props = line.split("=", 1)[1].strip()
            elif line.startswith("B: ABS="):
                absbits = line.split("=", 1)[1].replace(" ", "")
            elif not line:
                if name and "linuxStream" not in name and absbits and handlers:
                    try:
                        mask = int(absbits, 16)
                    except ValueError:
                        mask = 0
                    evs = [h for h in handlers.split() if h.startswith("event")]
                    if evs and (mask >> ABS_MT_POSITION_X & 1) and (mask >> ABS_MT_POSITION_Y & 1):
                        try:
                            prop = int(props or "0", 16)
                        except ValueError:
                            prop = 0
                        cand = (f"/dev/input/{evs[0]}", name, prop)
                        (direct if prop & prop_direct else others).append(cand)
                name = handlers = absbits = props = ""
        for path, nm, prop in direct + others:
            logger.info("WebOS: 터치 후보 %s name=%r PROP=%#x (%s)", path, nm, prop,
                        "화면 터치스크린" if prop & prop_direct else "터치패드/기타 — 제외 대상")
        if direct:
            if len(direct) > 1:
                logger.warning("WebOS: 화면 터치스크린이 %d개 — 첫 번째(%s) 사용. 다르면 "
                               "디바이스 설정 webos_evdev 로 지정", len(direct), direct[0][0])
            self._evdev_path, nm, _p = direct[0]
        elif others:
            self._evdev_path, nm, _p = others[0]
            logger.warning("WebOS: INPUT_PROP_DIRECT 터치스크린이 없어 %s(%s) 사용 — 터치패드면 "
                           "위치가 무시되고 포커스 항목이 눌린다 (webos_evdev 로 지정)",
                           self._evdev_path, nm)
        else:
            raise WebOSStreamError(
                "WebOS: 멀티터치 터치스크린 노드를 찾지 못했습니다 "
                "(info['webos_evdev'] 로 직접 지정 가능)")
        logger.info("WebOS: 터치 주입 대상 = %s (%s)", self._evdev_path, nm)
        return self._evdev_path

    def _ensure_evdev_channel(self) -> None:
        p = self._touch_proc
        if p is not None and p.poll() is None:
            return
        dev = self._find_touch_device()
        cmd = self._adb_args("exec-in", "sh", "-c", self._ssh_wrap(f"cat > {dev}"))
        self._touch_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=_NO_WINDOW,
        )
        logger.info("WebOS: 터치 채널 열림 %s (%s)", dev, self.device_id or self.adb_serial)

    def _write_ev(self, payload: bytes) -> None:
        """input_event 구조체들을 한 번에 write. 채널이 끊겼으면 1회 재연결."""
        self._last_use = time.time()
        with self._touch_lock:
            for attempt in (1, 2):
                self._ensure_evdev_channel()
                p = self._touch_proc
                if p is None or p.stdin is None:
                    raise WebOSStreamError("WebOS: 터치 채널을 열 수 없습니다")
                try:
                    p.stdin.write(payload)
                    p.stdin.flush()
                    return
                except Exception as e:
                    try:
                        p.kill()
                    except Exception:
                        pass
                    self._touch_proc = None
                    if attempt == 2:
                        raise WebOSStreamError(f"WebOS: 터치 전송 실패 — {e}")

    # --- 제스처 (좌표는 패널 기준) ---

    def tap(self, x: int, y: int, hold_ms: int = 60) -> None:
        self._write_ev(_mt_down(int(x), int(y)))
        time.sleep(max(0, hold_ms) / 1000.0)
        self._write_ev(_mt_up())
        logger.info("[WebOS TAP] (%s,%s) -> %s", x, y, self._evdev_path)

    def long_press(self, x: int, y: int, duration_ms: int = 3000) -> None:
        self.tap(x, y, hold_ms=duration_ms)
        logger.info("[WebOS LONG_PRESS] (%s,%s) %dms", x, y, duration_ms)

    def repeat_tap(self, x: int, y: int, count: int = 5, interval_ms: int = 100) -> None:
        for i in range(max(1, count)):
            self.tap(x, y)
            if i < count - 1 and interval_ms > 0:
                time.sleep(interval_ms / 1000.0)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300, hold_ms: int = 0) -> None:
        """드래그. 엣지 제스처(가장자리→안쪽)까지 인식되도록 실제 손가락에 가깝게 보낸다.

        * DOWN 직후 **같은 위치로 한 프레임** 더 보낸다 — 접촉이 먼저 성립해야 이어지는
          이동이 '드래그'로 읽힌다(첫 이벤트부터 큰 점프면 무시하는 구현이 있다).
        * 이동은 촘촘하게(기본 24프레임) 보낸다.
        * 마지막 이동 후 잠깐 머문 뒤 UP — 곧바로 떼면 '취소'로 처리되는 경우가 있다.
        """
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        dur = max(int(duration_ms or 0), 100)
        steps = max(8, min(40, dur // 12))
        self._write_ev(_mt_down(x1, y1))
        time.sleep(0.02)
        self._write_ev(_mt_move(x1, y1))          # 접촉 성립(같은 위치 1프레임)
        if hold_ms and hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        interval = (dur / 1000.0) / steps
        for i in range(1, steps + 1):
            t = i / steps
            self._write_ev(_mt_move(int(round(x1 + (x2 - x1) * t)),
                                    int(round(y1 + (y2 - y1) * t))))
            time.sleep(interval)
        time.sleep(0.05)                          # 끝점에서 잠시 정지 후 뗌
        self._write_ev(_mt_up())
        logger.info("[WebOS SWIPE] (%s,%s)->(%s,%s) %dms steps=%d",
                    x1, y1, x2, y2, dur, steps)

    def multi_finger_tap(self, points: list) -> None:
        """진짜 멀티터치 — 손가락마다 MT slot 을 따로 쓴다."""
        if not points:
            return
        down = b"".join(_mt_down(int(p["x"]), int(p["y"]), slot=i, tracking=i + 1)
                        for i, p in enumerate(points))
        self._write_ev(down)
        time.sleep(0.06)
        self._write_ev(b"".join(_mt_up(slot=i) for i in range(len(points))))
        logger.info("[WebOS MULTI_TAP] %d fingers", len(points))

    def multi_finger_swipe(self, fingers: list, duration_ms: int = 500,
                           hold_ms: int = 0) -> None:
        if not fingers:
            return
        dur = max(int(duration_ms or 0), 100)
        steps = max(3, min(30, dur // 20))
        self._write_ev(b"".join(
            _mt_down(int(f["x1"]), int(f["y1"]), slot=i, tracking=i + 1)
            for i, f in enumerate(fingers)))
        if hold_ms and hold_ms > 0:
            time.sleep(hold_ms / 1000.0)
        interval = (dur / 1000.0) / steps
        for s in range(1, steps + 1):
            t = s / steps
            self._write_ev(b"".join(
                _mt_move(int(round(f["x1"] + (f["x2"] - f["x1"]) * t)),
                         int(round(f["y1"] + (f["y2"] - f["y1"]) * t)), slot=i)
                for i, f in enumerate(fingers)))
            time.sleep(interval)
        self._write_ev(b"".join(_mt_up(slot=i) for i in range(len(fingers))))
        logger.info("[WebOS MULTI_SWIPE] %d fingers %dms", len(fingers), dur)

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
        info["touch_size"] = list(self.touch_size)
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
            "evdev_path": self._evdev_path,
            "native_width": self.native_size[0],
            "native_height": self.native_size[1],
            "last_error": self._last_error,
        }
