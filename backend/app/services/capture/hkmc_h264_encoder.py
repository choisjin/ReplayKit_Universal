"""HKMC 미러링 H.264 인코딩 백엔드 (PoC).

HKMC Agent 프로토콜은 연속 비디오 스트림을 지원하지 않고 CMD_GETIMG로 BMP를
한 장씩만 돌려준다. 이 백엔드는 그 프레임들을 ffmpeg로 H.264(Annex-B)로 인코딩해
brower(WebCodecs/JMuxer)로 relay할 수 있게 한다. scrcpy_server.ScrcpyBackend 와
**동일한 인터페이스**(video_width/height, stream_h264(), close())를 노출하므로
WS 핸들러(main.py)가 ADB scrcpy 분기와 거의 같은 방식으로 소비할 수 있다.

차이점:
  * scrcpy = 디바이스가 보낸 H.264를 그대로 relay (인코딩 없음)
  * HKMC   = 디바이스 BMP/PNG를 받아 PC ffmpeg에서 인코딩

PoC 방침:
  * HKMC 서비스를 수정하지 않는다. 기존 async_screencap_bytes(fmt="png")(무손실)를
    캡처 소스로 사용하고, ffmpeg가 PNG 디코드 + H.264 인코드를 담당.
  * 따라서 numpy/cv2 추가 의존이 없고 서비스 회귀 위험이 0.

파이프라인:
  capture task: hkmc.async_screencap_bytes(png) 루프 → ffmpeg.stdin write
  ffmpeg proc : -f image2pipe(png) → libx264(zerolatency) → -f h264 stdout
  relay task  : ffmpeg.stdout → NAL 키프레임 정렬 → asyncio.Queue
  stream_h264(): Queue 소비 → yield (WS가 send_bytes)

프로덕션 최적화(후속): PNG round-trip 대신 -f rawvideo -pix_fmt bgr24 로
디코딩된 프레임을 직접 파이프하면 PNG encode/decode 비용 제거 가능.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from typing import AsyncIterator, Optional

from .ffmpeg_runtime import detect_ffmpeg

logger = logging.getLogger(__name__)

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# stdout에서 한 번에 읽을 chunk 크기. scrcpy_server와 동일 기준.
_READ_CHUNK = 64 * 1024

# stream_h264 정상 종료 sentinel.
_EOF_SENTINEL = object()

# 큐 최대 길이 — 소비자가 느리면 backlog 폐기 후 키프레임 재동기.
_QUEUE_MAXSIZE = 60

# 첫 NAL 수신 timeout (초). 캡처+인코딩 첫 IDR이 늦을 수 있어 여유를 둔다.
_FIRST_NAL_TIMEOUT = 8.0


def _find_keyframe_offset(buf: bytes) -> int:
    """Annex-B 버퍼에서 SPS(type 7) 또는 IDR(type 5) NAL의 start code 시작 offset.

    못 찾으면 -1. start code 는 00 00 01 또는 00 00 00 01 둘 다 인정.
    디코더가 깨끗한 GOP 경계에서 시작하도록 SPS를 우선 탐색하되, SPS가 없으면
    IDR도 키프레임 시작점으로 허용한다(repeat-headers=1이면 보통 SPS가 IDR 앞에 옴).
    """
    n = len(buf)
    i = 0
    # i <= n-4: 최소 start code(00 00 01) + NAL 헤더 1바이트 = 4바이트가 들어갈
    # 마지막 위치까지 검사. (n-4 미포함이면 버퍼 끝의 키프레임을 놓친다.)
    while i <= n - 4:
        # start code 탐색
        if buf[i] == 0x00 and buf[i + 1] == 0x00:
            if buf[i + 2] == 0x01:
                hdr_idx = i + 3
                sc_start = i
            elif buf[i + 2] == 0x00 and buf[i + 3] == 0x01:
                hdr_idx = i + 4
                sc_start = i
            else:
                i += 1
                continue
            if hdr_idx < n:
                nal_type = buf[hdr_idx] & 0x1F
                if nal_type in (7, 5):  # SPS or IDR
                    return sc_start
            i = hdr_idx
            continue
        i += 1
    return -1


class HkmcH264Backend:
    """HKMC 캡처 프레임을 H.264로 인코딩해 NAL을 yield하는 백엔드.

    인스턴스는 1회 사용형. close()는 idempotent.
    """

    def __init__(
        self,
        hkmc_service,
        screen_type: str = "front_center",
        fps: int = 10,
        gop: Optional[int] = None,
    ):
        """
        Args:
            hkmc_service: HKMC6thService / HKMC5thWideService 인스턴스
                (async_screencap_bytes, get_screen_size, is_connected 보유).
            screen_type: 캡처 화면.
            fps: 캡처/인코딩 목표 FPS (1~30). 디바이스가 더 느리면 자연히 그 속도.
            gop: IDR 간격(프레임). 미지정 시 fps(=약 1초 간격).
        """
        self._hkmc = hkmc_service
        self._screen_type = screen_type
        self._fps = max(1, min(30, int(fps)))
        self._gop = int(gop) if gop else self._fps
        self._frame_interval = 1.0 / self._fps

        try:
            w, h = hkmc_service.get_screen_size(screen_type)
        except Exception:
            w, h = 0, 0
        # libx264 yuv420p 는 짝수 해상도 요구. 홀수면 1 내려 맞춘다.
        self.video_width: int = (w or 1920) & ~1
        self.video_height: int = (h or 720) & ~1

        self._ff: Optional[asyncio.subprocess.Process] = None
        self._capture_task: Optional[asyncio.Task] = None
        self._relay_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAXSIZE)
        self._first_nal = asyncio.Event()
        self._closed = False
        self._stderr_tail = bytearray()
        # 진단 카운터
        self._frames_captured = 0
        self._bytes_out = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """ffmpeg spawn + capture/relay task 시작 + 첫 NAL 검증.

        Returns True 성공, False 실패(이 경우 자동 cleanup — 호출자는 JPEG 폴백).
        """
        ff = detect_ffmpeg()
        if not ff:
            logger.warning("HKMC H.264: ffmpeg not available — caller should use JPEG path")
            return False
        if not getattr(self._hkmc, "is_connected", False):
            logger.warning("HKMC H.264: service not connected")
            return False

        cmd = [
            ff, "-hide_banner", "-loglevel", "error",
            "-f", "image2pipe", "-framerate", str(self._fps), "-i", "pipe:0",
            "-an",
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-g", str(self._gop), "-keyint_min", str(self._gop), "-bf", "0",
            "-x264-params", "repeat-headers=1:scenecut=0",
            "-flush_packets", "1",
            "-f", "h264", "pipe:1",
        ]
        try:
            self._ff = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                creationflags=_NO_WINDOW,
            )
        except Exception as e:
            logger.warning("HKMC H.264: ffmpeg spawn failed: %s", e)
            return False

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._capture_task = asyncio.create_task(self._capture_loop())
        self._relay_task = asyncio.create_task(self._relay_loop())

        # 첫 NAL(키프레임) 수신 검증
        try:
            await asyncio.wait_for(self._first_nal.wait(), timeout=_FIRST_NAL_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(
                "HKMC H.264: no NAL within %.0fs — ffmpeg stderr: %s",
                _FIRST_NAL_TIMEOUT, self.stderr_tail(),
            )
            await self.close()
            return False

        logger.info(
            "HKMC H.264 backend started: %dx%d @%dfps gop=%d (screen=%s)",
            self.video_width, self.video_height, self._fps, self._gop,
            self._screen_type,
        )
        return True

    # ------------------------------------------------------------------
    # Capture: HKMC PNG → ffmpeg stdin
    # ------------------------------------------------------------------

    async def _capture_loop(self) -> None:
        """디바이스에서 PNG를 받아 ffmpeg stdin에 write. fps throttle 포함."""
        assert self._ff is not None and self._ff.stdin is not None
        stdin = self._ff.stdin
        loop = asyncio.get_event_loop()
        try:
            while not self._closed:
                t0 = loop.time()
                if not getattr(self._hkmc, "is_connected", False):
                    await asyncio.sleep(0.3)
                    continue
                try:
                    png = await self._hkmc.async_screencap_bytes(
                        screen_type=self._screen_type, fmt="png", timeout=3.0,
                    )
                except Exception as e:
                    logger.debug("HKMC H.264 capture error: %s", e)
                    await asyncio.sleep(0.2)
                    continue
                if not png:
                    await asyncio.sleep(0.1)
                    continue
                try:
                    stdin.write(png)
                    await stdin.drain()
                    self._frames_captured += 1
                except (ConnectionResetError, BrokenPipeError):
                    break  # ffmpeg 종료
                except Exception as e:
                    logger.debug("HKMC H.264 stdin write error: %s", e)
                    break

                # fps throttle — 캡처가 빠르면 간격 유지, 느리면 그대로 진행.
                elapsed = loop.time() - t0
                sleep_s = self._frame_interval - elapsed
                if sleep_s > 0:
                    await asyncio.sleep(sleep_s)
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            logger.debug("HKMC H.264 capture loop error: %s", e)
        finally:
            # stdin 닫아 ffmpeg가 EOF 인지하고 종료하도록.
            try:
                if self._ff and self._ff.stdin and not self._ff.stdin.is_closing():
                    self._ff.stdin.close()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Relay: ffmpeg stdout → NAL 키프레임 정렬 → queue
    # ------------------------------------------------------------------

    async def _relay_loop(self) -> None:
        """ffmpeg stdout(H.264 Annex-B)을 읽어 키프레임 정렬 후 큐에 적재.

        scrcpy_server._relay_loop 패턴과 동일: 소비자(WS)가 느려 큐가 차면
        backlog 폐기 후 다음 SPS/IDR부터 재동기. 첫 yield도 키프레임부터 시작.
        """
        assert self._ff is not None and self._ff.stdout is not None
        stdout = self._ff.stdout
        need_keyframe = True
        carry = b""
        try:
            while not self._closed:
                try:
                    chunk = await stdout.read(_READ_CHUNK)
                except (asyncio.CancelledError, GeneratorExit):
                    raise
                except Exception as e:
                    logger.debug("HKMC H.264 stdout read error: %s", e)
                    break
                if not chunk:
                    break  # ffmpeg EOF

                if need_keyframe:
                    buf = carry + chunk
                    off = _find_keyframe_offset(buf)
                    if off < 0:
                        carry = buf[-3:]
                        continue
                    chunk = buf[off:]
                    carry = b""
                    need_keyframe = False

                if self._queue.full():
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
                    self._queue.put_nowait(chunk)
                    self._bytes_out += len(chunk)
                except asyncio.QueueFull:
                    need_keyframe = True
                    continue
                if not self._first_nal.is_set():
                    self._first_nal.set()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as e:
            logger.debug("HKMC H.264 relay loop error: %s", e)
        finally:
            try:
                self._queue.put_nowait(_EOF_SENTINEL)
            except asyncio.QueueFull:
                self._drain_queue()
                try:
                    self._queue.put_nowait(_EOF_SENTINEL)
                except Exception:
                    pass

    def _drain_queue(self) -> None:
        try:
            while True:
                self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pass

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    async def stream_h264(self) -> AsyncIterator[bytes]:
        """큐의 raw H.264 NAL chunk를 yield. ffmpeg/capture 종료 시 자연 정지."""
        try:
            while not self._closed:
                item = await self._queue.get()
                if item is _EOF_SENTINEL:
                    break
                yield item
        except (asyncio.CancelledError, GeneratorExit):
            raise

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    async def _drain_stderr(self) -> None:
        if self._ff is None or self._ff.stderr is None:
            return
        try:
            while True:
                chunk = await self._ff.stderr.read(4096)
                if not chunk:
                    break
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > 8192:
                    del self._stderr_tail[:-4096]
        except Exception:
            pass

    def stderr_tail(self, max_chars: int = 512) -> str:
        if not self._stderr_tail:
            return ""
        try:
            raw = bytes(self._stderr_tail[-max_chars:])
            text = raw.decode("utf-8", errors="replace").strip()
            return " | ".join(ln.strip() for ln in text.splitlines() if ln.strip())
        except Exception:
            return repr(bytes(self._stderr_tail[-max_chars:]))

    def stats(self) -> dict:
        return {
            "frames_captured": self._frames_captured,
            "bytes_out": self._bytes_out,
            "width": self.video_width,
            "height": self.video_height,
            "fps": self._fps,
            "gop": self._gop,
        }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """idempotent 완전 종료. 재사용 불가."""
        if self._closed:
            return
        self._closed = True

        for task in (self._capture_task, self._relay_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self._ff is not None and self._ff.returncode is None:
            try:
                if self._ff.stdin and not self._ff.stdin.is_closing():
                    self._ff.stdin.close()
            except Exception:
                pass
            try:
                self._ff.terminate()
                await asyncio.wait_for(self._ff.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    self._ff.kill()
                    await self._ff.wait()
                except Exception:
                    pass
            except Exception as e:
                logger.debug("HKMC H.264 ffmpeg close error: %s", e)

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        # 큐 비우고 sentinel — 소비 중인 stream_h264 깨우기
        self._drain_queue()
        try:
            self._queue.put_nowait(_EOF_SENTINEL)
        except Exception:
            pass
