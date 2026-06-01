"""scrcpy-server (v1.25) 기반 H.264 라이브 미러링 백엔드 (video 전용).

scrcpy-server.jar를 디바이스에 push 후 app_process로 실행해 MediaCodec API를
직접 호출한다. screenrecord와 달리:
  * idle 시에도 frame 출력이 자연스러움 (인코더 직접 제어)
  * 무한 streaming (segment 175초 제한 없음)
  * raw_video_stream=true 모드로 prefix bytes 없이 순수 H.264 NAL stream 수신

이 백엔드는 **video 미러링 전용**이다. 입력(touch/key/text)은 scrcpy 동작 여부와
무관하게 모든 디바이스에서 동일하게 동작하도록 ADBService의 shell input 경로로
일원화되어 있어, 이 모듈은 control_socket을 사용하지 않는다 (control=false).

v1.25 + adb reverse 선택 이유:
  * v2.x SurfaceControl direct API는 자동차 IVI 컨테이너(HMG 등)에서 차단됨
  * v1.x Surface 간접 mirroring은 임베디드/자동차 Android 호환성 우수
  * tunnel_forward=false (adb reverse) 가 HMG 같은 컨테이너 환경에서 동작.
    forward 방향(device listen, PC connect)은 SELinux/컨테이너 정책에 막힐 수 있지만,
    reverse 방향(PC listen, device connect)은 허용되는 경우가 많다.

디코딩 파이프라인:
  socket → PyAV CodecContext (H.264 직접 디코딩) → cv2.imencode JPEG
  PyAV 미설치/실패 시 try_start False 반환 → 호출자가 screencap PNG 폴백 사용.

흐름:
  1. tools/scrcpy-server.jar(v1.25) 를 /data/local/tmp/scrcpy-server.jar 로 push
  2. PC 측에서 TCP listen (asyncio.start_server, 동적 포트)
  3. adb reverse localabstract:scrcpy tcp:<PC_port>
  4. adb shell CLASSPATH=... app_process / com.genymobile.scrcpy.Server 1.25 ...
     server.jar가 localabstract:scrcpy 로 connect → adb reverse가 PC TCP로 forward
  5. 우리 listen socket이 connection 받음 → reader/writer 획득
  6. async task가 reader.read() → single-thread executor로 PyAV decode + JPEG encode
  7. JPEG 프레임을 asyncio.Queue에 put → stream_jpeg()에서 yield

폴백 트리거:
  * scrcpy-server.jar 부재 (배포 누락)
  * PyAV(av) 미설치
  * adb push / reverse 실패
  * app_process 실행 실패
  * 디바이스 connect 실패 또는 첫 프레임 timeout
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

ADB_PATH = os.environ.get("ADB_PATH", "adb")

# scrcpy 버전 — 옵션 형식과 동작이 버전마다 다르므로 server.jar와 정확히 일치해야 한다.
# scrcpy 1.x server는 client_version과 BuildConfig.VERSION_NAME을 strict 비교하므로
# 불일치 시 즉시 IllegalArgumentException으로 종료. 배포된 jar(v1.25)와 일치시킨다.
SCRCPY_VERSION = "1.25"

# 디바이스 측 jar 경로.
DEVICE_JAR_PATH = "/data/local/tmp/scrcpy-server.jar"

# 첫 JPEG 프레임 수신 timeout (초). IVI 등 정적 화면에서 첫 IDR이 늦게 오는 케이스에
# 대응해 12초로 넉넉히 잡음. codec_options.i-frame-interval=1 로도 보완되지만 디바이스
# 별로 적용 시점에 차이가 있어 timeout 여유와 함께 사용.
_FIRST_FRAME_TIMEOUT = 12.0

# idle keep-alive 간격 (초) — 화면 변화가 없을 때 마지막 프레임을 재전송해 클라이언트
# 측 stale detection을 피한다.
_IDLE_FRAME_TIMEOUT = 1.0

# socket → decoder chunk 크기. 너무 크면 첫 프레임 latency 증가, 너무 작으면 syscall 폭주.
_READ_CHUNK = 64 * 1024

# JPEG 인코딩 품질 (cv2.IMWRITE_JPEG_QUALITY). 75~85 사이가 시각적/대역폭 균형점.
_JPEG_QUALITY = 80


# ----------------------------------------------------------------------
# PyAV 의존성 — 백엔드 활성 조건
# ----------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def detect_av() -> bool:
    """PyAV (av) 라이브러리 가용 여부. 미설치 시 scrcpy 백엔드 비활성 → screencap 폴백."""
    try:
        import av  # noqa: F401
        return True
    except ImportError:
        logger.info(
            "PyAV (av) not installed — scrcpy backend disabled, "
            "screencap PNG polling fallback will be used. "
            "Install with: pip install av"
        )
        return False


@functools.lru_cache(maxsize=1)
def detect_cv2() -> bool:
    """cv2 가용 여부. JPEG 인코딩에 필수."""
    try:
        import cv2  # noqa: F401
        return True
    except ImportError:
        logger.info("cv2 not installed — scrcpy backend disabled")
        return False


# ----------------------------------------------------------------------
# Path discovery (ffmpeg_runtime과 동일 패턴)
# ----------------------------------------------------------------------

def _project_root() -> Path:
    """이 파일은 <root>/backend/app/services/capture/scrcpy_server.py → parents[4]."""
    return Path(__file__).resolve().parents[4]


def _install_root_candidates() -> list[Path]:
    if sys.platform == "win32":
        return [Path(r"C:\ReplayKit")]
    return [Path("/opt/ReplayKit"), Path.home() / ".local" / "share" / "ReplayKit"]


@functools.lru_cache(maxsize=1)
def detect_scrcpy_server() -> Optional[str]:
    """scrcpy-server.jar 경로 반환. 미발견 시 None.

    탐색 우선순위:
      1. SCRCPY_SERVER_PATH 환경변수
      2. <repo>/tools/scrcpy-server.jar (개발)
      3. ./tools/scrcpy-server.jar (CWD)
      4. C:\\ReplayKit\\tools\\scrcpy-server.jar (배포)
    """
    env_path = os.environ.get("SCRCPY_SERVER_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path

    name = "scrcpy-server.jar"
    candidates: list[Path] = [
        _project_root() / "tools" / name,
        Path.cwd() / "tools" / name,
    ]
    for root in _install_root_candidates():
        candidates.append(root / "tools" / name)

    for cand in candidates:
        try:
            if cand.is_file():
                return str(cand)
        except OSError:
            continue
    return None


def log_scrcpy_status() -> None:
    """기동 시 한 번 호출 — scrcpy-server.jar + PyAV 가용성 로그."""
    jar = detect_scrcpy_server()
    av_ok = detect_av()
    cv2_ok = detect_cv2()
    if jar and av_ok and cv2_ok:
        try:
            size = os.path.getsize(jar)
        except OSError:
            size = 0
        logger.info(
            "scrcpy backend ready: path=%s size=%d (PyAV+cv2 decode)",
            jar, size,
        )
    else:
        reasons = []
        if not jar:
            reasons.append("scrcpy-server.jar not found")
        if not av_ok:
            reasons.append("PyAV(av) not installed")
        if not cv2_ok:
            reasons.append("cv2 not installed")
        logger.info(
            "scrcpy backend disabled (%s) — screencap PNG fallback will be used.",
            ", ".join(reasons),
        )


# ----------------------------------------------------------------------
# Backend
# ----------------------------------------------------------------------

# 디바이스 측 jar 해시 캐시 (push 중복 방지). key = (serial, local_jar_path).
_pushed_jar_hashes: dict[tuple[str, str], str] = {}


def _file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


class ScrcpyServerBackend:
    """scrcpy-server.jar → PyAV (H.264 디코딩) → cv2 JPEG 인코딩 파이프라인.

    하나의 인스턴스는 (serial, logical_id) 조합 하나에 1:1 대응. 입력은 이 모듈
    바깥(ADBService.shell input)에서 처리하므로 video 단방향 스트림만 다룬다.
    """

    name = "scrcpy_server"

    def __init__(
        self,
        serial: str,
        logical_id: Optional[int] = None,
        *,
        bitrate: int = 4_000_000,
        max_fps: int = 0,
        jpeg_quality: int = _JPEG_QUALITY,
    ):
        self.serial = serial
        self.logical_id = logical_id or 0
        self.bitrate = bitrate
        self.max_fps = max_fps
        self.jpeg_quality = jpeg_quality
        # scrcpy v1.x는 single-instance 설계 — socket name "scrcpy" 고정, scid 옵션 없음.
        self.local_port = 0
        self._server_proc: Optional[subprocess.Popen] = None
        # 디바이스측 app_process PID — spawn 시 `echo $$`로 캡처. close 시 정확히 이
        # PID를 kill -9 해 인코더 전송버퍼를 확실히 회수한다 (pkill 미지원 디바이스 대비).
        self._device_pid: Optional[int] = None
        # adb reverse 방식: PC가 TCP listen, 디바이스 server.jar가 connect 옴.
        self._listener: Optional[asyncio.base_events.Server] = None
        self._accept_event: Optional[asyncio.Event] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        # 디코더 task와 디코더 전용 single-thread executor
        self._decoder_task: Optional[asyncio.Task] = None
        self._decoder_executor: Optional[ThreadPoolExecutor] = None
        # 인코딩된 JPEG 큐 — maxsize=2로 backpressure (디코더 < 소비자 속도 차 흡수)
        self._jpeg_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._first_frame_event: asyncio.Event = asyncio.Event()
        self._first_frame: Optional[bytes] = None
        self._closed = False
        # 진단용 stdout/stderr tail
        self._stderr_tail: bytearray = bytearray()
        self._stdout_tail: bytearray = bytearray()
        # 디코딩 통계 (진단용)
        self._total_bytes_in: int = 0
        self._total_frames_decoded: int = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def try_start(
        self, first_frame_timeout: float = _FIRST_FRAME_TIMEOUT,
    ) -> bool:
        """시작 + 첫 프레임 수신 검증. 실패 시 cleanup하고 False."""
        if not detect_av() or not detect_cv2():
            return False
        jar = detect_scrcpy_server()
        if not jar:
            return False

        try:
            # 0) 이전 세션에서 비정상 종료된 scrcpy app_process 선제 정리.
            #    v1.x는 single-instance(socket "scrcpy" 고정)라 잔존 인스턴스가 있으면
            #    우리 socket 연결이 충돌해 first-frame 실패 → 재시도 폭주의 씨앗이 된다.
            #    screenrecord는 screencap 폴백이 쓸 수 있으므로 건드리지 않는다.
            await self._cleanup_device_side(include_screenrecord=False)
            # 1) jar push (해시 동일 시 skip)
            if not await self._push_jar(jar):
                return False
            # 2) PC에서 TCP listen 시작 (asyncio.start_server)
            if not await self._setup_reverse_listener():
                return False
            # 3) adb reverse 등록 — device의 localabstract:scrcpy → PC TCP
            if not await self._setup_reverse():
                return False
            # 4) server 프로세스 실행 (백그라운드)
            if not await self._spawn_server():
                return False
            # 5) 디바이스가 우리에게 connect 올 때까지 대기
            if not await self._accept_socket():
                return False
            # 6) PyAV 디코더 task 시작
            if not self._start_decoder_task():
                return False
            # 7) 첫 프레임 검증 — queue에 첫 JPEG가 들어올 때까지
            await asyncio.wait_for(
                self._first_frame_event.wait(), timeout=first_frame_timeout,
            )
            if self._jpeg_queue.empty():
                raise RuntimeError("first_frame event set but queue empty")
            # 첫 프레임을 미리 꺼내둠 — stream_jpeg가 이걸 먼저 yield
            self._first_frame = await self._jpeg_queue.get()
        except (asyncio.TimeoutError, Exception) as e:
            sr_err = self._stderr_tail_str()
            sr_out = self._stdout_tail_str()
            srv_rc = self._server_proc.poll() if self._server_proc else None
            logger.info(
                "scrcpy first-frame check failed (serial=%s display=%s): %s "
                "server_rc=%s bytes_in=%d frames=%d server_out=%r server_err=%r",
                self.serial, self.logical_id, type(e).__name__,
                srv_rc, self._total_bytes_in, self._total_frames_decoded,
                sr_out, sr_err,
            )
            await self.close()
            return False

        logger.info(
            "scrcpy backend started: serial=%s display=%s port=%d bitrate=%d (v%s, PyAV)",
            self.serial, self.logical_id, self.local_port, self.bitrate,
            SCRCPY_VERSION,
        )
        return True

    async def _push_jar(self, local_jar: str) -> bool:
        """디바이스에 jar push. 해시 동일 시 skip."""
        cache_key = (self.serial, local_jar)
        try:
            local_hash = _file_sha256(local_jar)
        except OSError as e:
            logger.warning("scrcpy jar read error: %s", e)
            return False

        cached = _pushed_jar_hashes.get(cache_key)
        if cached == local_hash:
            # 이미 push됨. 다만 디바이스 측에서 파일이 실제로 존재하는지 한 번 확인.
            if await self._device_jar_exists():
                logger.debug("scrcpy jar push skipped (already pushed): %s", self.serial)
                return True

        loop = asyncio.get_event_loop()
        cmd = [ADB_PATH, "-s", self.serial, "push", local_jar, DEVICE_JAR_PATH]
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, timeout=10,
                    creationflags=_NO_WINDOW,
                ),
            )
        except Exception as e:
            logger.warning("scrcpy jar push failed (%s): %s", self.serial, e)
            return False
        if result.returncode != 0:
            logger.warning(
                "scrcpy jar push failed (%s): %s",
                self.serial, result.stderr.decode(errors="replace").strip(),
            )
            return False
        _pushed_jar_hashes[cache_key] = local_hash
        return True

    async def _device_jar_exists(self) -> bool:
        loop = asyncio.get_event_loop()
        cmd = [ADB_PATH, "-s", self.serial, "shell", "ls", DEVICE_JAR_PATH]
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, timeout=3,
                    creationflags=_NO_WINDOW,
                ),
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _setup_reverse_listener(self) -> bool:
        """PC에서 TCP listen 시작. server.jar의 connect를 받는다.

        control=false 모드이므로 connection은 1개(video)만 들어온다.
        """
        self._accept_event = asyncio.Event()

        async def _on_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            # 첫 connect만 받고 나머지는 차단 (single instance).
            if self._reader is None:
                self._reader = reader
                self._writer = writer
                if self._accept_event:
                    self._accept_event.set()
            else:
                try:
                    writer.close()
                except Exception:
                    pass

        try:
            self._listener = await asyncio.start_server(_on_client, "127.0.0.1", 0)
            sockets = self._listener.sockets
            if not sockets:
                return False
            self.local_port = sockets[0].getsockname()[1]
            return True
        except Exception as e:
            logger.warning("scrcpy listener setup failed (%s): %s", self.serial, e)
            return False

    async def _setup_reverse(self) -> bool:
        """adb reverse 등록. 디바이스의 localabstract:scrcpy → PC tcp:<local_port>."""
        loop = asyncio.get_event_loop()
        cmd = [
            ADB_PATH, "-s", self.serial, "reverse",
            "localabstract:scrcpy",
            f"tcp:{self.local_port}",
        ]
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, timeout=5,
                    creationflags=_NO_WINDOW,
                ),
            )
        except Exception as e:
            logger.warning("adb reverse failed (%s): %s", self.serial, e)
            return False
        if result.returncode != 0:
            logger.warning(
                "adb reverse failed (%s): %s",
                self.serial, result.stderr.decode(errors="replace").strip(),
            )
            return False
        return True

    async def _remove_reverse(self) -> None:
        loop = asyncio.get_event_loop()
        cmd = [
            ADB_PATH, "-s", self.serial, "reverse", "--remove",
            "localabstract:scrcpy",
        ]
        try:
            await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, timeout=3,
                    creationflags=_NO_WINDOW,
                ),
            )
        except Exception:
            pass

    def _build_server_cmd(self) -> list[str]:
        """app_process 명령 구성 — scrcpy v1.25 CLI 호환 옵션 셋.

        주요 옵션:
          * tunnel_forward=false: adb reverse 사용 (PC listen, device connect)
            HMG IVI 같은 컨테이너 환경에서 forward 방향 socket binding이 막혀있어
            반대 방향인 reverse가 통하는 경우가 많다 (CLI 검증됨).
          * control=false: 입력 채널 비활성. 입력은 ADBService.shell input 경로로
            scrcpy 동작 여부 무관하게 단일화되어 있다.
          * power_off_on_close=false: 우리 close 시 디바이스 화면 꺼지지 않게
          * raw_video_stream=true: prefix bytes(dummy 1 + device_meta 64) +
            frame_meta(12/frame) 모두 비활성화. PyAV가 raw H.264 NAL stream을 바로
            디코딩 가능.
          * codec_options=i-frame-interval=1: 1초마다 IDR 키프레임 강제. 정적 화면
            디바이스에서 첫 IDR 대기로 인한 first-frame timeout 방지.
        """
        opts = [
            "log_level=info",
            f"bit_rate={self.bitrate}",
            "max_size=0",
            f"max_fps={self.max_fps}",
            "lock_video_orientation=-1",
            "tunnel_forward=false",
            "control=false",
            f"display_id={self.logical_id}",
            "show_touches=false",
            "stay_awake=false",
            "power_off_on_close=false",
            "raw_video_stream=true",
            "codec_options=i-frame-interval=1",
        ]
        # `echo SCRCPYPID:$$` 후 `exec`로 app_process를 띄우면 셸 PID($$)가 그대로
        # app_process PID가 된다 → close 시 이 PID를 정확히 kill -9 가능.
        # echo 출력은 stdout으로 나가 _drain_stdout에서 파싱한다 (scrcpy 로그는 stderr).
        inner = (
            f"echo SCRCPYPID:$$; "
            f"CLASSPATH={DEVICE_JAR_PATH} "
            f"exec app_process / com.genymobile.scrcpy.Server {SCRCPY_VERSION} "
            + " ".join(opts)
        )
        return [ADB_PATH, "-s", self.serial, "shell", inner]

    async def _spawn_server(self) -> bool:
        """server 프로세스를 백그라운드로 spawn. stdout/stderr 모두 진단용으로 캡처."""
        try:
            self._server_proc = subprocess.Popen(
                self._build_server_cmd(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                creationflags=_NO_WINDOW,
                bufsize=0,
            )
        except Exception as e:
            logger.warning("scrcpy server spawn failed (%s): %s", self.serial, e)
            return False

        # stdout/stderr를 각각 백그라운드로 drain (pipe 막힘 방지 + 진단용).
        import threading
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        return True

    def _drain_stdout(self) -> None:
        proc = self._server_proc
        if not proc or not proc.stdout:
            return
        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                self._stdout_tail.extend(chunk)
                if self._device_pid is None:
                    self._maybe_parse_device_pid()
                if len(self._stdout_tail) > 4096:
                    del self._stdout_tail[:-2048]
        except Exception:
            pass

    def _maybe_parse_device_pid(self) -> None:
        """stdout에서 `SCRCPYPID:<pid>` 마커를 찾아 디바이스측 app_process PID 기록."""
        marker = b"SCRCPYPID:"
        idx = self._stdout_tail.find(marker)
        if idx < 0:
            return
        start = idx + len(marker)
        # 개행(종료문자)이 아직 안 들어왔으면 숫자가 chunk 경계에서 잘렸을 수 있어
        # 확정하지 않는다 (잘못된 PID를 kill하는 것을 방지).
        nl = self._stdout_tail.find(b"\n", start)
        if nl < 0:
            return
        digits = bytes(self._stdout_tail[start:nl]).strip()
        if digits.isdigit():
            try:
                self._device_pid = int(digits.decode())
            except ValueError:
                pass

    def _drain_stderr(self) -> None:
        proc = self._server_proc
        if not proc or not proc.stderr:
            return
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > 4096:
                    del self._stderr_tail[:-2048]
        except Exception:
            pass

    def _tail_str(self, buf: bytearray) -> str:
        if not buf:
            return ""
        try:
            raw = bytes(buf[-1024:])
            text = raw.decode("utf-8", errors="replace").strip()
            return " | ".join(line.strip() for line in text.splitlines() if line.strip())
        except Exception:
            return ""

    def _stderr_tail_str(self) -> str:
        return self._tail_str(self._stderr_tail)

    def _stdout_tail_str(self) -> str:
        return self._tail_str(self._stdout_tail)

    async def _accept_socket(self) -> bool:
        """디바이스 server.jar가 우리 PC로 connect 올 때까지 대기.

        adb reverse 방식이라 connect 시작 주체는 디바이스. server.jar가 시작 후
        localabstract:scrcpy로 connect → adb reverse가 우리 TCP listen으로 forward.
        """
        if not self._accept_event:
            return False
        try:
            # server 기동 + connect 까지 약 3초 안에 들어옴. 여유 5초.
            await asyncio.wait_for(self._accept_event.wait(), timeout=5.0)
            return self._reader is not None
        except asyncio.TimeoutError:
            logger.info(
                "scrcpy accept timed out (%s): server_err=%s",
                self.serial, self._stderr_tail_str(),
            )
            return False

    # ------------------------------------------------------------------
    # PyAV 디코딩 파이프라인
    # ------------------------------------------------------------------

    def _start_decoder_task(self) -> bool:
        """디코더 task 시작 — socket reader → PyAV → JPEG → queue."""
        if not self._reader:
            return False
        # codec context는 thread-safe가 아니므로 single-worker executor 사용.
        self._decoder_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix=f"scrcpy-decode-{self.serial[:8]}",
        )
        self._decoder_task = asyncio.create_task(self._decoder_loop())
        return True

    async def _decoder_loop(self) -> None:
        """async: socket에서 H.264 chunk를 읽고 executor로 디코딩 위임.

        디코딩+JPEG 인코딩은 CPU bound이므로 이벤트 루프 차단을 피하기 위해
        백엔드 전용 single-thread executor에서 실행.
        """
        import av  # detect_av로 이미 확인됨
        # CodecContext는 같은 스레드에서만 사용해야 안전 → executor 워커 1개 보장.
        codec = av.CodecContext.create("h264", "r")

        loop = asyncio.get_event_loop()
        reader = self._reader
        executor = self._decoder_executor
        if reader is None or executor is None:
            return

        try:
            while not self._closed:
                try:
                    chunk = await reader.read(_READ_CHUNK)
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as e:
                    logger.debug("scrcpy socket read error: %s", e)
                    break
                if not chunk:
                    # EOF — server.jar 종료 또는 disconnect
                    break
                self._total_bytes_in += len(chunk)

                # CPU bound 디코딩+인코딩을 executor로 위임.
                try:
                    jpegs = await loop.run_in_executor(
                        executor, _decode_chunk_to_jpegs,
                        codec, chunk, self.jpeg_quality,
                    )
                except Exception as e:
                    logger.debug("scrcpy decode error: %s", e)
                    continue

                for jpeg in jpegs:
                    self._total_frames_decoded += 1
                    # backpressure: queue가 가득 차면 가장 오래된 프레임 드롭.
                    # 라이브 스트림에서 stale 프레임은 가치가 낮으므로 drop이 정답.
                    if self._jpeg_queue.full():
                        try:
                            self._jpeg_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    try:
                        self._jpeg_queue.put_nowait(jpeg)
                    except asyncio.QueueFull:
                        pass
                    if not self._first_frame_event.is_set():
                        self._first_frame_event.set()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            logger.debug("scrcpy decoder loop error: %s", e)
        finally:
            # EOF/에러 시 sentinel 넣어 stream_jpeg가 깔끔히 종료되도록.
            try:
                self._jpeg_queue.put_nowait(_EOF_SENTINEL)
            except asyncio.QueueFull:
                # 큐 가득 → 하나 비우고 sentinel 삽입
                try:
                    self._jpeg_queue.get_nowait()
                    self._jpeg_queue.put_nowait(_EOF_SENTINEL)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_jpeg(self) -> AsyncIterator[bytes]:
        """JPEG 프레임 yield.

        디코더 task가 _jpeg_queue에 넣는 프레임을 그대로 소비.
        idle (_IDLE_FRAME_TIMEOUT 동안 새 frame 없음) 시 마지막 프레임 재전송.
        디코더 종료(EOF/에러) 시 sentinel 받아 자연 종료.
        """
        first = self._first_frame
        last_frame: Optional[bytes] = None
        if first is not None:
            self._first_frame = None
            last_frame = first
            yield first

        try:
            while not self._closed:
                try:
                    item = await asyncio.wait_for(
                        self._jpeg_queue.get(), timeout=_IDLE_FRAME_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    if last_frame is not None:
                        yield last_frame
                    continue

                if item is _EOF_SENTINEL:
                    break
                last_frame = item
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            raise

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """idempotent 완전 종료. 재사용 불가."""
        if self._closed:
            return
        self._closed = True

        # 1) decoder task cancel — socket read를 깨운다
        if self._decoder_task and not self._decoder_task.done():
            self._decoder_task.cancel()
            try:
                await self._decoder_task
            except (asyncio.CancelledError, Exception):
                pass
        self._decoder_task = None

        # 2) executor shutdown
        if self._decoder_executor is not None:
            try:
                self._decoder_executor.shutdown(wait=False, cancel_futures=True)
            except Exception:
                pass
            self._decoder_executor = None

        # 3) video socket close → server.jar가 자연 종료
        if self._writer:
            try:
                self._writer.close()
                try:
                    await asyncio.wait_for(self._writer.wait_closed(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
            except Exception:
                pass
            self._writer = None
        self._reader = None

        # 4) server 프로세스 종료
        if self._server_proc and self._server_proc.poll() is None:
            try:
                self._server_proc.terminate()
                try:
                    self._server_proc.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._server_proc.kill()
            except Exception:
                pass
        self._server_proc = None

        # 5) PC listener 종료
        if self._listener:
            try:
                self._listener.close()
                await self._listener.wait_closed()
            except Exception:
                pass
            self._listener = None

        # 6) 디바이스 측 잔존 app_process 정리
        await self._cleanup_device_side()

        # 7) adb reverse 제거
        await self._remove_reverse()

        logger.info(
            "scrcpy backend closed: serial=%s bytes_in=%d frames=%d",
            self.serial, self._total_bytes_in, self._total_frames_decoded,
        )

    async def _cleanup_device_side(self, *, include_screenrecord: bool = True) -> None:
        """디바이스에 남아있을지 모를 scrcpy/screenrecord 프로세스 정리.

        v1.x는 single-instance(socket name "scrcpy" 고정)라 scid 구분 안 함.
        같은 디바이스의 HW 인코더 자원을 다른 백엔드도 쓸 수 있어 cross-cleanup.

        자동차/IVI Android의 toybox는 `pkill`이 없거나 `-f`를 지원 안 하는 경우가
        많아, pkill만으로는 app_process가 살아남아 인코더 전송버퍼가 누적된다
        (→ OOM). 따라서 3단계로 강제 회수한다:
          1. spawn 시 캡처한 정확한 PID를 `kill -9` (kill은 toybox에 항상 존재).
          2. `pkill -f` (지원 디바이스에서 빠른 일괄 정리).
          3. `/proc/<pid>/cmdline` 스캔 폴백 — pkill 미지원 디바이스에서 scrcpy.Server
             cmdline을 가진 잔존 PID를 직접 kill -9.
        """
        loop = asyncio.get_event_loop()

        def _run(args: list[str], timeout: float) -> None:
            try:
                subprocess.run(
                    args, capture_output=True, timeout=timeout,
                    creationflags=_NO_WINDOW,
                )
            except Exception:
                pass

        # 1) 우리가 spawn한 정확한 PID 우선 종료.
        if self._device_pid is not None:
            kill_cmd = [
                ADB_PATH, "-s", self.serial, "shell",
                "kill", "-9", str(self._device_pid),
            ]
            await loop.run_in_executor(None, lambda: _run(kill_cmd, 2))

        # 2) pkill -f (지원 디바이스 한정 빠른 정리).
        patterns = ["scrcpy.Server"]
        if include_screenrecord:
            patterns.append("screenrecord")  # 다른 백엔드 stale (cross-cleanup)
        for pat in patterns:
            cmd = [ADB_PATH, "-s", self.serial, "shell", "pkill", "-f", pat]
            await loop.run_in_executor(None, lambda c=cmd: _run(c, 2))

        # 3) /proc 스캔 폴백 — pkill 미지원(toybox 일부 자동차 Android) 대비.
        proc_scan = (
            'for f in /proc/[0-9]*/cmdline; do '
            'grep -qa scrcpy.Server "$f" 2>/dev/null && '
            '{ p=${f#/proc/}; p=${p%/cmdline}; kill -9 "$p" 2>/dev/null; }; '
            'done'
        )
        scan_cmd = [ADB_PATH, "-s", self.serial, "shell", proc_scan]
        await loop.run_in_executor(None, lambda: _run(scan_cmd, 3))

    def is_alive(self) -> bool:
        return (
            not self._closed
            and self._decoder_task is not None
            and not self._decoder_task.done()
            and self._server_proc is not None
            and self._server_proc.poll() is None
        )


# ----------------------------------------------------------------------
# 디코딩 worker (executor 스레드에서 실행)
# ----------------------------------------------------------------------

# stream_jpeg의 정상 종료 sentinel.
_EOF_SENTINEL: object = object()


def _decode_chunk_to_jpegs(codec, chunk: bytes, jpeg_quality: int) -> list[bytes]:
    """단일 H.264 chunk → JPEG 프레임 리스트.

    같은 codec context를 반복 호출해야 SPS/PPS 컨텍스트가 유지된다. ThreadPoolExecutor
    worker가 1개이므로 race 없음.

    raw H.264 NAL stream에서 chunk 경계는 NAL 경계와 일치하지 않을 수 있어,
    codec.parse(chunk)로 demuxer에 일임해 packet 단위로 정리한 뒤 decode.
    """
    import av
    import cv2

    out: list[bytes] = []
    try:
        packets = codec.parse(chunk)
    except av.InvalidDataError:
        return out
    except Exception:
        return out

    for packet in packets:
        try:
            frames = codec.decode(packet)
        except av.InvalidDataError:
            continue
        except Exception:
            continue
        for frame in frames:
            try:
                arr = frame.to_ndarray(format="bgr24")
            except Exception:
                continue
            try:
                ok, buf = cv2.imencode(
                    ".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality],
                )
            except Exception:
                continue
            if ok:
                out.append(bytes(buf))
    return out
