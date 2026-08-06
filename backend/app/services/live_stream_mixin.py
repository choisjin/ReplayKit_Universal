"""라이브 미러 스트리밍 공용 믹스인 — MIB/ICAS 등 weston+LayerManagerControl 플랫폼 공유.

실화면(screen dump) 방식: 컴포지터가 z순서+픽셀알파 룰로 합성한 최종 화면을
`LayerManagerControl dump screen 0`(항상 PNG)으로 읽어 그대로 송출한다.
과거의 서피스 합성(black-key) 방식은 24bpp 덤프에서 알파가 소실되어 '진짜 검정 UI'와
'투명 구멍'을 원리적으로 구분할 수 없었고(겹침/고스트/블링킹), 전 패널 실화면으로 통일했다.
대가는 fps — PNG 인코딩이 해상도에 비례(800x480 실측 ~165ms=~6fps, 1560x878 실측 ~0.63s).

프레임 형식: b"MIBP" + struct('<IHH', png_len, sw, sh) + PNG bytes

사용 서비스 요구사항:
  - self._new_ssh(): paramiko SSHClient 생성+연결
  - self._ssh_keepalive_interval: int
  - (선택) self._maybe_autoupdate_resolution(w, h): bool — _live_sync_res=True일 때만 사용
  - __init__에서 self._init_live_stream() 호출, disconnect()에서 self.stop_live_stream() 호출
  - self._live_sync_res (bool): 첫 프레임의 화면해상도(SW,SH)로 등록 해상도 1회 보정 여부.
    터치 좌표 매핑이 라이브 이미지 좌표공간과 어긋나는 플랫폼(MIB)만 True.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import struct
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# 실화면 스트리머 — dump screen(비동기 PNG 기록)을 반복하며 완성 프레임을 stdout으로 송출.
_LIVE_STREAMER = r'''
import sys, os, time, struct, subprocess, re
os.environ["XDG_RUNTIME_DIR"] = "/run/platform/weston"

def lmc(a):
    try:
        return subprocess.run(["LayerManagerControl"] + a, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    except Exception:
        return ""

m = re.search(r"resolution:\D*x=(\d+),\s*y=(\d+)", lmc(["get", "screen", "0"]))
SW, SH = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
sys.stderr.write("LIVESCR screen mode SW=%d SH=%d\n" % (SW, SH))
sys.stderr.flush()
out = sys.stdout.buffer
P = "/tmp/_mlscr.png"
while True:
    try:
        try:
            os.remove(P)
        except OSError:
            pass
        subprocess.run(["LayerManagerControl", "dump", "screen", "0", "to", P],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # PNG 완성 대기 — dump는 비동기 기록: 크기 안정화 + IEND(마지막 청크) 확인
        data = None; t0 = time.time(); last = -1
        while time.time() - t0 < 3.0:
            try:
                sz = os.path.getsize(P)
            except OSError:
                sz = 0
            if sz > 12 and sz == last:
                with open(P, "rb") as f:
                    b = f.read()
                if b[-8:-4] == b"IEND":
                    data = b; break
            last = sz
            time.sleep(0.02)
        if not data:
            time.sleep(0.1); continue
        out.write(b"MIBP" + struct.pack("<IHH", len(data), SW, SH) + data)
        out.flush()
    except (BrokenPipeError, IOError):
        break
    except Exception:
        time.sleep(0.1)
'''


class LiveStreamMixin:
    """실화면(screen dump) 라이브 스트리밍 — 캡처/전송 공용. 터치/해상도 정책은 서비스별.

    디바이스 스트리머와 프레임 페이로드는 서브클래스가 교체할 수 있다:
      - `_live_streamer_src`: 디바이스에서 `python3 -u -` 로 실행할 소스 (기본 = LayerManagerControl dump)
      - `_live_magic`: 프레임 매직 4바이트 (기본 b"MIBP")
      - `_live_decode_frame(payload, sw, sh)`: 페이로드 → PIL.Image (기본 = PNG 디코드)
    헤더는 공통으로 magic(4) + struct('<IHH', payload_len, sw, sh).
    """

    # 서브클래스 override 지점 (FPK: fb0 raw + zlib) — 기본값은 MIB/ICAS의 dump screen PNG.
    _live_streamer_src: str = _LIVE_STREAMER
    _live_magic: bytes = b"MIBP"

    def _live_decode_frame(self, payload: bytes, sw: int, sh: int):
        """프레임 페이로드 → PIL.Image. 기본 구현은 완성 PNG 바이트."""
        from PIL import Image
        return Image.open(io.BytesIO(payload)).convert("RGB")

    def _init_live_stream(self) -> None:
        # 라이브는 전용 SSH 연결을 쓴다 — 풀해상도 screencap이 공유 SSH를 리셋해도 안 끊기게 격리.
        self._live_ssh = None
        self._live_chan = None
        self._live_thread = None
        self._live_stop = threading.Event()
        self._live_lock = threading.Lock()
        self._latest_live_jpeg: Optional[bytes] = None
        self._live_frame_id = 0
        self._live_res_synced = False
        # 첫 프레임 화면해상도로 등록 해상도 1회 보정 여부(서비스가 override). 기본 False.
        if not hasattr(self, "_live_sync_res"):
            self._live_sync_res = False
        # 로그 라벨 (서비스가 override 가능)
        if not hasattr(self, "_live_label"):
            self._live_label = "LIVE"

        def _ei(names, d):
            for n in names:
                v = os.environ.get(n)
                if v:
                    try:
                        return int(v)
                    except Exception:
                        pass
            return d
        self._live_jpeg_q = _ei(["LIVE_JPEG_Q", "MIB_LIVE_JPEG_Q"], 60)

    def is_live_running(self) -> bool:
        t = getattr(self, "_live_thread", None)
        return bool(t and t.is_alive())

    def start_live_stream(self) -> bool:
        """device 스트리머를 전용 SSH 채널로 기동하고 리더 스레드를 띄운다.

        이미 돌고 있으면 True. 실패 시 False(호출자가 레거시 screencap 경로로 폴백).
        """
        with self._live_lock:
            if self._live_thread and self._live_thread.is_alive():
                return True
            self._live_stop.clear()
            self._live_res_synced = False
        ssh = None
        try:
            ssh = self._new_ssh()  # 전용 연결 (공유 SSH 리셋과 격리)
            try:
                tr = ssh.get_transport()
                if tr is not None:
                    tr.set_keepalive(self._ssh_keepalive_interval)
            except Exception:
                pass
            transport = ssh.get_transport()
            if transport is None:
                try: ssh.close()
                except Exception: pass
                return False
            chan = transport.open_session()
            chan.exec_command("python3 -u -")
            chan.sendall(self._live_streamer_src.encode("utf-8"))
            chan.shutdown_write()  # 스트리머는 stdin을 읽지 않음 → 즉시 EOF
            t = threading.Thread(target=self._live_reader, args=(chan,), daemon=True)
            with self._live_lock:
                self._live_ssh = ssh
                self._live_chan = chan
                self._live_thread = t
            t.start()
            logger.info("%s live stream started (screen mode, q=%d)",
                        self._live_label, self._live_jpeg_q)
            return True
        except Exception as e:
            logger.warning("%s live stream start failed: %r", self._live_label, e)
            if ssh is not None:
                try:
                    ssh.close()
                except Exception:
                    pass
            with self._live_lock:
                self._live_ssh = None
                self._live_chan = None
                self._live_thread = None
            return False

    def _live_reader(self, chan) -> None:
        """MIBP 프레임(완성 PNG)을 받아 JPEG 변환만 수행.

        합성/black-key 없음 — 컴포지터가 이미 알파·z순서 룰로 합성한 화면을 그대로 표시.
        프레임 형식: b"MIBP" + struct('<IHH', png_len, sw, sh) + PNG bytes
        """
        MAGIC = self._live_magic
        HDR = 12  # magic(4) + '<IHH'(8)
        buf = b""
        try:
            chan.settimeout(5.0)
        except Exception:
            pass
        while not self._live_stop.is_set():
            try:
                # 스트리머 stderr(진단)를 로그로 회수
                while chan.recv_stderr_ready():
                    err = chan.recv_stderr(4096)
                    if err:
                        logger.info("%s streamer: %s", self._live_label,
                                    err.decode("utf-8", "replace").strip())
            except Exception:
                pass
            try:
                data = chan.recv(262144)
            except Exception:
                break
            if not data:
                break  # 채널 닫힘 → 스트리머 종료
            buf += data
            while True:
                idx = buf.find(MAGIC)
                if idx < 0:
                    if len(buf) > (1 << 23):
                        buf = buf[-8:]
                    break
                if len(buf) < idx + HDR:
                    break
                png_len, sw, sh = struct.unpack("<IHH", buf[idx + 4: idx + HDR])
                if png_len <= 0 or png_len > (1 << 23):
                    buf = buf[idx + 4:]  # 손상 헤더 — 매직 재탐색
                    continue
                total = idx + HDR + png_len
                if len(buf) < total:
                    break
                png = buf[idx + HDR: total]
                buf = buf[total:]
                # (MIB) 실제 화면해상도로 등록 해상도 1회 보정 — 프론트 터치 매핑 일치용.
                # ICAS 등 자체 터치 캘리브레이션 플랫폼은 _live_sync_res=False라 건너뜀.
                if (self._live_sync_res and not self._live_res_synced and sw > 0 and sh > 0):
                    self._live_res_synced = True
                    fn = getattr(self, "_maybe_autoupdate_resolution", None)
                    if fn is not None:
                        try:
                            if fn(sw, sh):
                                logger.info("%s live: 등록 해상도 보정 → %dx%d",
                                            self._live_label, sw, sh)
                        except Exception as e:
                            logger.debug("%s live resolution sync failed: %s", self._live_label, e)
                try:
                    img = self._live_decode_frame(png, sw, sh)
                    bio = io.BytesIO()
                    img.save(bio, format="JPEG", quality=self._live_jpeg_q)
                    jpg = bio.getvalue()
                    with self._live_lock:
                        self._latest_live_jpeg = jpg
                        self._live_frame_id += 1
                except Exception:
                    continue
        logger.info("%s live reader exited", self._live_label)

    def get_live_frame(self) -> "tuple[Optional[bytes], int]":
        """(최신 JPEG bytes, frame_id). 아직 없으면 (None, 0)."""
        with self._live_lock:
            return self._latest_live_jpeg, self._live_frame_id

    def stop_live_stream(self) -> None:
        if not hasattr(self, "_live_stop"):
            return
        self._live_stop.set()
        with self._live_lock:
            chan = self._live_chan
            ssh = self._live_ssh
            self._live_chan = None
            self._live_ssh = None
            t = self._live_thread
            self._live_thread = None
        if chan is not None:
            try:
                chan.close()
            except Exception:
                pass
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass
        if t and t.is_alive():
            t.join(timeout=1.5)
        with self._live_lock:
            self._latest_live_jpeg = None

    async def async_start_live_stream(self) -> bool:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.start_live_stream)
