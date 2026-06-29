"""scrcpy-server (v1.25) 기반 H.264 라이브 미러링 백엔드 (video 전용).

scrcpy-server.jar를 디바이스에 push 후 app_process로 실행해 MediaCodec API를
직접 호출한다. screenrecord와 달리:
  * idle 시에도 frame 출력 유지 (repeat-previous-frame-after 로 정적 화면도 직전 프레임 재송출)
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

전송 파이프라인 (raw H.264 relay):
  socket → (디코딩 없이) raw H.264 NAL chunk → asyncio.Queue → stream_h264() yield
  → main.py 가 WebSocket 으로 그대로 relay → 브라우저 JMuxer(MSE) 가 GPU 디코딩.

  ★ 과거에는 PyAV 로 디코딩 후 cv2 로 JPEG 재인코딩(MJPEG)해 보냈으나, 그 Python
    트랜스코딩이 4Mbps 를 못 따라가 디바이스측 인코더 송신버퍼가 역압으로 누적 →
    자동차 IVI 의 적은 RAM 에서 OOM → 디바이스 재부팅을 유발했다. relay 는 PC 가
    소켓을 scrcpy.exe 수준으로 빠르게 비워 인코더가 밀리지 않으므로 OOM 을 근절한다.

디바이스 보호 불변식:
  relay reader 태스크는 **WebSocket 전송 속도와 무관하게** 디바이스 소켓을 항상 읽어
  비운다. 소비자(WS)가 느려 큐가 가득 차면 큐 backlog 를 버리고 다음 SPS/IDR
  키프레임부터 재동기한다 (i-frame-interval=1 로 ~1초 내 복구). 절대 디바이스 소켓
  읽기를 WS 전송에 막지 않는다 → 디바이스측 버퍼 누적(OOM) 방지.

흐름:
  1. tools/scrcpy-server.jar(v1.25) 를 /data/local/tmp/scrcpy-server.jar 로 push
  2. PC 측에서 TCP listen (asyncio.start_server, 동적 포트)
  3. adb reverse localabstract:scrcpy tcp:<PC_port>
  4. adb shell CLASSPATH=... app_process / com.genymobile.scrcpy.Server 1.25 ...
     server.jar가 localabstract:scrcpy 로 connect → adb reverse가 PC TCP로 forward
  5. 우리 listen socket이 connection 받음 → reader/writer 획득
  6. relay 태스크가 reader.read() → 키프레임 정렬 후 raw NAL 을 큐에 put
  7. stream_h264()가 큐에서 꺼내 yield

폴백 트리거:
  * scrcpy-server.jar 부재 (배포 누락)
  * adb push / reverse 실패
  * app_process 실행 실패
  * 디바이스 connect 실패 또는 첫 NAL 수신 timeout
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 전 PC 동일 adb 보장 — 번들 tools/platform-tools/adb 우선 (../adb_path 공용 resolver).
from ..adb_path import resolve_adb_path
ADB_PATH = resolve_adb_path()

# scrcpy 버전 — 옵션 형식과 동작이 버전마다 다르므로 server.jar와 정확히 일치해야 한다.
# scrcpy server 는 client_version 을 첫 인자로 받고 BuildConfig.VERSION 과 비교하므로
# 불일치 시 즉시 종료. 따라서 버전 문자열 ↔ jar 파일을 1:1로 묶는다.
#
# 듀얼 버전 운용 이유 (Android 16 호환):
#   * v1.25 는 미러링 시 SurfaceControl.createDisplay(String, boolean) 로 가상 디스플레이를
#     만든다. 이 메서드는 Android 14(API 34)부터 사라져 Android 14+ 일반 폰에서
#     NoSuchMethodException 으로 서버가 즉사한다 (갤럭시 S23 Android 16 등).
#   * v3.x 는 신형 디스플레이 생성 API 를 쓰지만, 그 SurfaceControl direct API 가
#     자동차 IVI 컨테이너(HMG 등)에서 차단되는 경우가 있어 거기선 v1.25 가 필요하다.
#   → 둘 다 번들하고, 호출자(adb_service)가 디바이스 Android SDK 로 우선순위를 정한 뒤
#     실패 시 다른 버전으로 교차 폴백한다. (SDK>=34 → v3 우선, 그 외 → v1 우선)
SCRCPY_V1 = "1.25"
SCRCPY_V3 = "3.3.4"
SCRCPY_VERSION = SCRCPY_V1  # 하위호환 별칭 (기존 로그/외부 참조용 기본값)

# 버전 → 후보 jar 파일명(우선순위순). v1.25 는 레거시 무버전 파일명도 허용한다
# (이미 배포된 설치본이 tools/scrcpy-server.jar 로 v1.25 를 갖고 있으므로).
_JAR_FILENAMES: dict[str, tuple[str, ...]] = {
    SCRCPY_V1: ("scrcpy-server-v1.25.jar", "scrcpy-server.jar"),
    SCRCPY_V3: ("scrcpy-server-v3.3.4.jar",),
}

# 디바이스 측 jar 경로 — 버전별로 분리한다. 단일 경로를 공유하면 버전 전환 시 push 해시
# 캐시는 "이미 push 됨"으로 보지만 디바이스 파일은 다른 버전이라, 잘못된 jar 로 서버를
# 띄워 버전 불일치로 죽는다. 버전별 파일명으로 그 혼선을 원천 차단한다.
def _device_jar_path(version: str) -> str:
    return f"/data/local/tmp/scrcpy-server-{version}.jar"


def _is_v2plus(version: str) -> bool:
    """v2.0 이상이면 True — 옵션 키 이름과 scid 소켓 명명이 v1.x 와 다르다."""
    try:
        return int(version.split(".", 1)[0]) >= 2
    except (ValueError, IndexError):
        return False

# 첫 NAL 수신 timeout (초). IVI 등 정적 화면에서 첫 IDR이 늦게 오는 케이스에
# 대응해 12초로 넉넉히 잡음. codec_options.i-frame-interval=1 로도 보완되지만 디바이스
# 별로 적용 시점에 차이가 있어 timeout 여유와 함께 사용.
_FIRST_FRAME_TIMEOUT = 12.0

# 미러링 기본 프레임레이트 상한 (scrcpy max_fps 인코더 옵션). 0 = 무제한.
# 디바이스 인코더 부하·PC 디코딩 부하·WS 대역폭을 낮추기 위해 15fps 로 캡한다.
# (자동화 미러는 부드러운 60fps 가 필요 없고, 낮을수록 OOM/thrash 여유가 커진다.)
_DEFAULT_MAX_FPS = 15

# socket → relay chunk 크기. 너무 크면 첫 프레임 latency 증가, 너무 작으면 syscall 폭주.
_READ_CHUNK = 64 * 1024

# 프레임 흐름 갭 진단 프로브 간격(초). stream_h264 가 이 간격으로 큐를 폴링한다. 프레임이
# 끊기면(정적 화면) 첫 갭만 1회 진단 로깅하고 연결은 그대로 유지한다 — 정식 scrcpy 처럼
# "프레임이 안 온다"는 이유로 스트림을 죽이지 않는다(과거의 stall watchdog 제거).
# 실제 종료는 socket EOF(relay 가 sentinel 주입)로만 일어난다.
# ※ 과거 stall watchdog(6s→30s)은 정적 화면(프레임 없음)을 freeze 로 오인해 멀쩡한 연결을
#   죽이고 재시작 thrash 를 유발했다. v3.x 에서 repeat-previous-frame-after 가 안 먹혀 정적
#   화면 프레임이 아예 끊기는 케이스까지 겹쳐, watchdog 자체를 폐기하고 연결 유지로 전환했다.
_FLOW_GAP_PROBE = 5.0

# relay 큐 최대 chunk 수 (~64KB/chunk). 소비자(WS)가 느릴 때 여기까지만 backlog 를
# 허용하고, 초과 시 backlog 를 버린 뒤 다음 키프레임부터 재동기한다. 4MB(=64) 정도면
# 일시적 네트워크 지터를 흡수하면서도 PC 메모리 증가를 제한한다.
_QUEUE_MAX_CHUNKS = 64


# ----------------------------------------------------------------------
# PyAV 의존성 — 백엔드 활성 조건
# ----------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def detect_av() -> bool:
    """PyAV (av) 가용 여부.

    raw H.264 relay 백엔드는 PyAV/cv2 없이 동작하므로 더 이상 활성 조건이 아니다.
    (브라우저 JMuxer 가 디코딩) — 호환성을 위해 함수는 유지하되 결과는 사용되지 않는다.
    """
    try:
        import av  # noqa: F401
        return True
    except ImportError:
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


def _tools_dirs() -> list[Path]:
    """scrcpy-server jar 탐색 디렉토리(우선순위순): repo/tools, CWD/tools, 배포 설치 경로."""
    dirs = [_project_root() / "tools", Path.cwd() / "tools"]
    for root in _install_root_candidates():
        dirs.append(root / "tools")
    return dirs


@functools.lru_cache(maxsize=8)
def detect_scrcpy_server(version: Optional[str] = None) -> Optional[str]:
    """scrcpy-server jar 경로 반환. 미발견 시 None.

    version 지정 시 그 버전의 jar 만 탐색한다. None 이면 가용한 아무 버전이나
    (v1 우선) 반환한다 — "scrcpy 자체가 가능한가" 게이트 용도.

    탐색 우선순위(각 후보 파일명에 대해):
      1. SCRCPY_SERVER_PATH 환경변수 (v1/기본에만 적용 — 레거시 호환)
      2. <repo>/tools/<name> (개발)
      3. ./tools/<name> (CWD)
      4. <설치경로>/tools/<name> (배포)
    """
    # 레거시 env override 는 v1(또는 버전 미지정) 경로에만 적용.
    if version is None or version == SCRCPY_V1:
        env_path = os.environ.get("SCRCPY_SERVER_PATH")
        if env_path and os.path.isfile(env_path):
            return env_path

    versions = [version] if version else list(_JAR_FILENAMES.keys())
    for ver in versions:
        for fname in _JAR_FILENAMES.get(ver, ()):
            for d in _tools_dirs():
                cand = d / fname
                try:
                    if cand.is_file():
                        return str(cand)
                except OSError:
                    continue
    return None


def log_scrcpy_status() -> None:
    """기동 시 한 번 호출 — scrcpy-server.jar 가용성 로그.

    raw H.264 relay 는 PyAV/cv2 가 필요 없으므로 jar 존재만으로 활성화된다.
    """
    available = []
    for ver in _JAR_FILENAMES:
        jar = detect_scrcpy_server(ver)
        if jar:
            try:
                size = os.path.getsize(jar)
            except OSError:
                size = 0
            available.append(f"v{ver}({size}B)")
    if available:
        logger.info(
            "scrcpy backend ready: versions=%s (raw H.264 relay; "
            "SDK>=34→v%s 우선, 그 외→v%s 우선, 실패 시 교차 폴백)",
            ", ".join(available), SCRCPY_V3, SCRCPY_V1,
        )
    else:
        logger.info(
            "scrcpy backend disabled (no scrcpy-server jar found) — "
            "screencap PNG fallback will be used.",
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
        version: str = SCRCPY_V1,
        bitrate: int = 4_000_000,
        max_fps: int = _DEFAULT_MAX_FPS,
    ):
        self.serial = serial
        self.logical_id = logical_id or 0
        self.version = version
        self.bitrate = bitrate
        self.max_fps = max_fps
        # 디바이스 측 jar 경로(버전별 분리).
        self._device_jar_path = _device_jar_path(version)
        # 소켓 이름 — v1.x 는 "scrcpy" 고정(single-instance). v2+ 는 scid 를 받아
        # "scrcpy_<8hex>" 로 분리되므로 세션마다 고유 scid 를 생성한다.
        if _is_v2plus(version):
            self._scid = f"{int.from_bytes(os.urandom(4), 'big') & 0x7FFFFFFF:08x}"
            self._socket_name = f"scrcpy_{self._scid}"
        else:
            self._scid = None
            self._socket_name = "scrcpy"
        # 디바이스 해상도 (JMuxer/<video> 레이아웃 힌트). try_start 에서 best-effort 로
        # wm size 조회. 실패 시 None → 프론트가 기본값(1080x1920) 사용.
        self.video_width: Optional[int] = None
        self.video_height: Optional[int] = None
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
        # relay task — 디바이스 소켓을 항상 읽어(디바이스 보호) 큐에 raw NAL 적재.
        self._relay_task: Optional[asyncio.Task] = None
        # raw H.264 NAL chunk 큐. 가득 차면 relay_loop 가 backlog 를 버리고 키프레임
        # 재동기 (디바이스 소켓 읽기는 막지 않음).
        self._frame_queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX_CHUNKS)
        self._first_frame_event: asyncio.Event = asyncio.Event()
        self._closed = False
        # 활성 소비자 seq — stream_h264 가 시작할 때마다 증가. 한 백엔드의 단일 큐를
        # 둘 이상이 동시에 빨면 H.264 NAL 이 쪼개져 디코딩이 깨지므로, "최신 소비자만
        # 활성"으로 강제한다(이전 소비자는 seq 불일치를 보고 스스로 퇴출). 장치 전환으로
        # 같은 백엔드를 다시 볼 때 이전 WS 의 잔존(좀비) 소비자가 새 소비자와 큐를
        # 나눠 빠는 것을 막는다.
        self._consumer_seq = 0
        # 진단용 stdout/stderr tail
        self._stderr_tail: bytearray = bytearray()
        self._stdout_tail: bytearray = bytearray()
        # relay 통계 (진단용)
        self._total_bytes_in: int = 0
        self._total_frames_decoded: int = 0  # 큐에 넣은 chunk 수 (relay 단위)
        # 마지막으로 stream_h264 가 NAL 을 소비(yield)한 event-loop 시각. idle reaper 가
        # "아무 WS 도 안 보는 백엔드"를 판별하는 데 쓴다. try_start 성공 시 now 로 초기화.
        self._last_consumed: float = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def try_start(
        self, first_frame_timeout: float = _FIRST_FRAME_TIMEOUT,
    ) -> bool:
        """시작 + 첫 NAL 수신 검증. 실패 시 cleanup하고 False.

        raw H.264 relay 는 PyAV/cv2 가 필요 없으므로 jar 존재만으로 시도한다.
        """
        jar = detect_scrcpy_server(self.version)
        if not jar:
            return False

        try:
            # 0) 이전 세션에서 비정상 종료된 scrcpy app_process 선제 정리.
            #    v1.x는 single-instance(socket "scrcpy" 고정)라 잔존 인스턴스가 있으면
            #    우리 socket 연결이 충돌해 first-frame 실패 → 재시도 폭주의 씨앗이 된다.
            #    screenrecord는 screencap 폴백이 쓸 수 있으므로 건드리지 않는다.
            #    deep=False: /proc 전수 스캔(느림)은 생략 — pkill만으로 hot path 경량화.
            #    (정상 종료 시 close()의 deep 정리가 잔존을 이미 회수하므로 충분)
            await self._cleanup_device_side(include_screenrecord=False, deep=False)
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
            # 6) relay task 시작 (디코딩 없이 raw NAL 을 큐로)
            if not self._start_relay_task():
                return False
            # 7) 첫 NAL 검증 — queue에 첫 키프레임 chunk가 들어올 때까지
            await asyncio.wait_for(
                self._first_frame_event.wait(), timeout=first_frame_timeout,
            )
            if self._frame_queue.empty():
                raise RuntimeError("first_frame event set but queue empty")
            # 8) 해상도 힌트 best-effort 조회 (실패해도 미러는 정상 — 프론트 기본값 사용)
            await self._fetch_resolution()
            # 소비 시각 초기화 — 막 시작한 백엔드가 WS 소비 전에 idle reaper 에 걷히지 않게.
            self._last_consumed = asyncio.get_event_loop().time()
        except (asyncio.TimeoutError, Exception) as e:
            sr_err = self._stderr_tail_str()
            sr_out = self._stdout_tail_str()
            srv_rc = self._server_proc.poll() if self._server_proc else None
            logger.info(
                "scrcpy first-frame check failed (serial=%s display=%s): %s "
                "server_rc=%s bytes_in=%d chunks=%d server_out=%r server_err=%r",
                self.serial, self.logical_id, type(e).__name__,
                srv_rc, self._total_bytes_in, self._total_frames_decoded,
                sr_out, sr_err,
            )
            await self.close()
            return False

        logger.info(
            "scrcpy backend started: serial=%s display=%s port=%d bitrate=%d "
            "max_fps=%d size=%sx%s (v%s, H.264 relay)",
            self.serial, self.logical_id, self.local_port, self.bitrate,
            self.max_fps, self.video_width, self.video_height, self.version,
        )
        return True

    async def _fetch_resolution(self) -> None:
        """`adb shell wm size` 로 디바이스 해상도 best-effort 조회 (JMuxer 레이아웃 힌트).

        MSE 는 SPS 에서 실제 해상도를 자동 인식하므로 이 값은 초기 레이아웃 힌트일 뿐.
        실패/파싱 불가 시 None 유지 → 프론트가 기본값을 쓴다. 미러 동작에 영향 없음.
        """
        loop = asyncio.get_event_loop()
        cmd = [ADB_PATH, "-s", self.serial, "shell", "wm", "size"]
        try:
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    cmd, capture_output=True, timeout=3,
                    creationflags=_NO_WINDOW,
                ),
            )
            text = result.stdout.decode(errors="replace")
            # "Physical size: 1080x1920" / "Override size: 1080x1920"
            m = re.search(r"(\d{2,5})x(\d{2,5})", text)
            if m:
                self.video_width = int(m.group(1))
                self.video_height = int(m.group(2))
        except Exception:
            pass

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
        cmd = [ADB_PATH, "-s", self.serial, "push", local_jar, self._device_jar_path]
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
        cmd = [ADB_PATH, "-s", self.serial, "shell", "ls", self._device_jar_path]
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
        """adb reverse 등록. 디바이스의 localabstract:<socket> → PC tcp:<local_port>.

        socket 이름은 v1.x="scrcpy", v2+="scrcpy_<scid>" (버전별 self._socket_name).
        """
        loop = asyncio.get_event_loop()
        cmd = [
            ADB_PATH, "-s", self.serial, "reverse",
            f"localabstract:{self._socket_name}",
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
            f"localabstract:{self._socket_name}",
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
        """app_process 명령 구성 — 버전(v1.x / v2+)에 맞는 옵션 셋을 선택.

        공통 설계:
          * tunnel_forward=false: adb reverse 사용 (PC listen, device connect).
            HMG IVI 같은 컨테이너 환경에서 forward 방향 socket binding이 막혀있어
            반대 방향인 reverse가 통하는 경우가 많다.
          * control=false: 입력 채널 비활성. 입력은 ADBService.shell input 경로로
            scrcpy 동작 여부 무관하게 단일화되어 있다.
          * audio 비활성(v2+): 오디오 소켓을 열고 기다리지 않게 한다.
          * raw stream: prefix/메타 바이트 없이 순수 H.264 NAL 만 흘려보낸다.
          * power_off_on_close=false: 우리 close 시 디바이스 화면이 꺼지지 않게.
          * codec_options(i-frame-interval=1 + repeat-previous-frame-after:long=100000):
              1초마다 IDR 강제 + 정적 화면에서 직전 프레임 재송출(100ms). MediaCodec
              surface 인코더는 입력이 없으면 출력을 멈춰 NAL 이 끊기는데, 재송출 옵션이
              그 stall 을 막는다(정적 화면 무한 재시작 thrash 의 근본 해결책). :long 필수.

        버전별 옵션 키 차이 (v3.3.4 dex 에서 확인):
          v1.25            → v3.3.4
          bit_rate         → video_bit_rate
          codec_options    → video_codec_options
          raw_video_stream → raw_stream
          lock_video_orientation(-1) → (없음; v3 는 capture_orientation, 미러링엔 생략)
          (scid 없음, 소켓 "scrcpy") → scid=<8hex>, 소켓 "scrcpy_<scid>"
        """
        codec_opts = "i-frame-interval=1,repeat-previous-frame-after:long=100000"
        if _is_v2plus(self.version):
            opts = [
                f"scid={self._scid}",
                "log_level=info",
                "audio=false",
                "video=true",
                "control=false",
                f"video_bit_rate={self.bitrate}",
                "max_size=0",
                f"max_fps={self.max_fps}",
                "video_codec=h264",
                "tunnel_forward=false",
                f"display_id={self.logical_id}",
                "show_touches=false",
                "stay_awake=false",
                "power_off_on_close=false",
                "cleanup=true",
                # raw_stream=true → send_device_meta/frame_meta/codec_meta/dummy_byte
                # 모두 off 로 강제. 디바이스가 connect 즉시 순수 H.264 를 흘려보내므로
                # 기존 relay/키프레임 정렬 로직을 그대로 재사용한다.
                "raw_stream=true",
                f"video_codec_options={codec_opts}",
            ]
        else:
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
                f"codec_options={codec_opts}",
            ]
        # `echo SCRCPYPID:$$` 후 `exec`로 app_process를 띄우면 셸 PID($$)가 그대로
        # app_process PID가 된다 → close 시 이 PID를 정확히 kill -9 가능.
        # echo 출력은 stdout으로 나가 _drain_stdout에서 파싱한다 (scrcpy 로그는 stderr).
        inner = (
            f"echo SCRCPYPID:$$; "
            f"CLASSPATH={self._device_jar_path} "
            f"exec app_process / com.genymobile.scrcpy.Server {self.version} "
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
    # raw H.264 relay 파이프라인
    # ------------------------------------------------------------------

    def _start_relay_task(self) -> bool:
        """relay task 시작 — socket reader → (키프레임 정렬) → raw NAL queue."""
        if not self._reader:
            return False
        self._relay_task = asyncio.create_task(self._relay_loop())
        return True

    async def _relay_loop(self) -> None:
        """디바이스 소켓을 항상 읽어 raw H.264 NAL chunk 를 큐에 적재.

        디바이스 보호 불변식: 이 루프는 WS 전송 속도와 무관하게 reader.read() 를
        계속 호출해 디바이스측 인코더 송신버퍼를 비운다 (OOM 방지). 소비자(WS)가
        느려 큐가 가득 차면 backlog 를 버리고 다음 SPS/IDR 키프레임부터 재동기한다.
        디코딩은 하지 않으므로 CPU 부담이 거의 없다.
        """
        reader = self._reader
        if reader is None:
            return

        # 최초 join 시 깨끗한 GOP 경계(SPS/IDR)부터 시작하도록 키프레임 대기 상태로 출발.
        need_keyframe = True
        # start code 가 chunk 경계에 걸칠 때를 대비한 carry (최대 3바이트).
        carry = b""

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

                # 키프레임 재동기 중이면 SPS/IDR 시작점까지 버린다.
                if need_keyframe:
                    buf = carry + chunk
                    off = _find_keyframe_offset(buf)
                    if off < 0:
                        carry = buf[-3:]  # start code 경계 보존
                        continue
                    chunk = buf[off:]
                    carry = b""
                    need_keyframe = False

                # 큐가 가득 → backlog 폐기 후 키프레임 재동기 요청 (디바이스 읽기는 유지).
                if self._frame_queue.full():
                    self._drain_queue()
                    need_keyframe = True
                    off = _find_keyframe_offset(chunk)
                    if off < 0:
                        carry = chunk[-3:]
                        continue
                    chunk = chunk[off:]
                    carry = b""
                    need_keyframe = False

                try:
                    self._frame_queue.put_nowait(chunk)
                    self._total_frames_decoded += 1
                except asyncio.QueueFull:
                    # 직전 full 체크와 put 사이 경합 — 다음 키프레임부터 재동기.
                    need_keyframe = True
                    continue
                if not self._first_frame_event.is_set():
                    self._first_frame_event.set()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            logger.debug("scrcpy relay loop error: %s", e)
        finally:
            # EOF/에러 시 sentinel 넣어 stream_h264가 깔끔히 종료되도록.
            try:
                self._frame_queue.put_nowait(_EOF_SENTINEL)
            except asyncio.QueueFull:
                self._drain_queue()
                try:
                    self._frame_queue.put_nowait(_EOF_SENTINEL)
                except Exception:
                    pass

    def _drain_queue(self) -> None:
        """큐의 모든 항목을 비운다 (backlog 폐기)."""
        try:
            while True:
                self._frame_queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_h264(self) -> AsyncIterator[bytes]:
        """raw H.264 NAL chunk yield.

        relay task가 _frame_queue에 넣는 chunk를 그대로 소비해 WS로 relay 한다.
        H.264 는 <video>/MSE 가 마지막 프레임을 유지하므로 정적 화면에서 프레임이 없어도
        마지막 프레임이 그대로 보인다(정식 scrcpy 와 동일). 따라서 "프레임이 안 온다"는
        이유로 스트림을 죽이지 않는다 — 진짜 종료(소켓 EOF/에러)는 relay 가 sentinel 을
        넣어 자연 종료시키고, 그 외에는 연결을 계속 유지한다.

        ※ 과거엔 30s "stall watchdog" 으로 no-NAL 시 RuntimeError 를 던져 main.py 가
          백엔드를 재시작하게 했는데, 정적 화면(특히 v3.x 에서 repeat-previous-frame-after
          미존중)을 freeze 로 오인해 멀쩡한 연결을 죽이고 5~10초 재시작 갭을 만들었다.
          정식 scrcpy 는 이런 워치독이 없고 연결을 유지하므로, 우리도 제거한다. 갭은
          진단용으로만 1회 로깅한다(비치명적).
        """
        loop = asyncio.get_event_loop()
        self._last_consumed = loop.time()
        # 이 소비자의 세대 번호 — 더 새로운 stream_h264 가 시작하면 seq 가 증가해
        # 이 루프가 다음 점검에서 ScrcpySuperseded 로 빠진다(한 큐=한 소비자 보장).
        self._consumer_seq += 1
        my_seq = self._consumer_seq
        gap_logged = False
        try:
            while not self._closed:
                if self._consumer_seq != my_seq:
                    # 더 새로운 소비자(주로 장치 전환 후 재시청)가 들어옴 → 양보·종료.
                    raise ScrcpySuperseded()
                try:
                    item = await asyncio.wait_for(
                        self._frame_queue.get(), timeout=_FLOW_GAP_PROBE,
                    )
                except asyncio.TimeoutError:
                    # no-NAL — 정적 화면일 뿐 죽이지 않는다. 갭만 1회 진단 로깅.
                    if not gap_logged:
                        logger.info(
                            "scrcpy frame flow gap: no NAL for ~%.0fs "
                            "(serial=%s display=%s v%s bytes_in=%d) — 정적 화면(마지막 "
                            "프레임 유지). 연결은 그대로 유지.",
                            _FLOW_GAP_PROBE, self.serial, self.logical_id,
                            self.version, self._total_bytes_in,
                        )
                        gap_logged = True
                    continue
                if item is _EOF_SENTINEL:
                    break
                if self._consumer_seq != my_seq:
                    # 방금 꺼낸 프레임은 버리고 양보 — 새 소비자가 다음 키프레임부터 재동기.
                    raise ScrcpySuperseded()
                if gap_logged:
                    logger.info(
                        "scrcpy frame flow resumed (serial=%s display=%s)",
                        self.serial, self.logical_id,
                    )
                    gap_logged = False
                self._last_consumed = loop.time()
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

        # 1) relay task cancel — socket read를 깨운다
        if self._relay_task and not self._relay_task.done():
            self._relay_task.cancel()
            try:
                await self._relay_task
            except (asyncio.CancelledError, Exception):
                pass
        self._relay_task = None

        # 2) video socket close → server.jar가 자연 종료
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
            "scrcpy backend closed: serial=%s bytes_in=%d chunks=%d",
            self.serial, self._total_bytes_in, self._total_frames_decoded,
        )

    async def _cleanup_device_side(
        self, *, include_screenrecord: bool = True, deep: bool = True,
    ) -> None:
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

        deep=False면 3)을 생략한다. 3)은 디바이스의 모든 프로세스를 전수 grep하므로
        프로세스 많은 IVI에서 수 초가 걸려, 매 기동 전 선제 정리(hot path)에는 부담이
        크다. 따라서 try_start 선제 정리는 deep=False(pkill만), close 시에는 deep=True.
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
        #    전수 grep이라 느리므로 deep=True(주로 close 경로)에서만 실행.
        if deep:
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
            and self._relay_task is not None
            and not self._relay_task.done()
            and self._server_proc is not None
            and self._server_proc.poll() is None
        )

    def idle_seconds(self, now: float) -> float:
        """마지막 NAL 소비 이후 경과 초. now 는 호출자의 event-loop time.

        WS 가 stream_h264 를 소비 중이면 0 에 가깝고, 아무 WS 도 안 보면 계속 증가한다.
        idle reaper 가 이 값으로 "버려진 백엔드"를 판별해 닫는다.
        """
        return max(0.0, now - self._last_consumed)


# ----------------------------------------------------------------------
# H.264 키프레임 탐색 (relay 재동기용)
# ----------------------------------------------------------------------

# stream_h264의 정상 종료 sentinel.
_EOF_SENTINEL: object = object()


class ScrcpySuperseded(Exception):
    """더 새로운 stream_h264 소비자가 들어와 이 소비자가 퇴출됐음을 알리는 신호.

    main.py 는 이 예외를 (WS 종료와 동일하게) 백엔드를 닫지 않고 해당 WS 핸들러만
    종료시키는 신호로 취급한다 — 클래스명 "ScrcpySuperseded" 로 매칭.
    """

# 키프레임으로 취급할 NAL unit type: 5=IDR slice, 7=SPS.
# scrcpy v1.25 raw_video_stream 은 IDR 앞에 SPS/PPS(7/8) config 를 보내므로,
# SPS(7) 또는 IDR(5) 시작점에서 재동기하면 JMuxer 가 깨끗한 GOP 로 디코딩을 재개한다.
_KEYFRAME_NAL_TYPES = frozenset({5, 7})


def _find_keyframe_offset(buf: bytes) -> int:
    """Annex-B H.264 바이트열에서 다음 키프레임(SPS/IDR) start code 위치 반환.

    start code(00 00 01) 뒤 NAL header 의 type(하위 5비트)이 SPS/IDR 이면 그 위치를
    돌려준다. 4바이트 start code(00 00 00 01)면 앞의 0x00 까지 포함해 반환. 미발견 시 -1.
    디코딩 없이 바이트 스캔만 하므로 비용이 거의 없다.
    """
    n = len(buf)
    i = 0
    while True:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0 or j + 3 >= n:
            return -1
        nal_type = buf[j + 3] & 0x1F
        if nal_type in _KEYFRAME_NAL_TYPES:
            if j > 0 and buf[j - 1] == 0:
                return j - 1  # 4바이트 start code 포함
            return j
        i = j + 3
