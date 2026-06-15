"""라이브 미러 스트리밍 공용 믹스인 — MIB/ICAS 등 weston+LayerManagerControl 플랫폼 공유.

weston screen dump은 PNG 인코딩 때문에 0.63s/frame(1.6fps 천장)이라 라이브에 부적합.
surface를 무압축 BMP로 dump(~20ms)해서, 연결 시 `LayerManagerControl get scene`을 파싱해
HMI(전체화면 dest 서피스)+MAP(최대 면적의 비검정 서브 서피스)을 자동 식별(인치 무관).
device엔 PIL/numpy가 없어 순수 stdlib로 nearest 다운스케일만 하고 HMI/MAP을 따로 송출:
  b"MIBF" + struct('<HHhhHHHH', tw, th, mx, my, mw, mh, sw, sh) + HMI(tw*th*3 BGR) + MAP(mw*mh*3 BGR)
백엔드(이 믹스인)가 numpy black-key 합성(HMI가 검정=투명인 곳에만 MAP) 후 JPEG.

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

# __TW__ / __TH__(0=화면비율 자동)는 start_live_stream에서 .replace()로 주입.
_LIVE_STREAMER = r'''
import sys, os, time, struct, subprocess, re
os.environ["XDG_RUNTIME_DIR"] = "/run/platform/weston"
TW = __TW__
TH_OVR = __TH__

def lmc(a):
    try:
        return subprocess.run(["LayerManagerControl"] + a, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL).stdout.decode("utf-8", "replace")
    except Exception:
        return ""

def f_xy(t, k):
    m = re.search(k + r"\D*x=(-?\d+),\s*y=(-?\d+)", t)
    return (int(m.group(1)), int(m.group(2))) if m else None

def f_reg(t, k):
    m = re.search(k + r"\D*x=(-?\d+),\s*y=(-?\d+),\s*w=(\d+),\s*h=(\d+)", t)
    return tuple(int(m.group(i)) for i in range(1, 5)) if m else None

def f_ids(t, k):
    m = re.search(k + r"\s*(.*)", t)
    return re.findall(r"(\d+)\(0x[0-9a-fA-F]+\)", m.group(1)) if m else []

def parse(b):
    off = struct.unpack("<I", b[10:14])[0]
    w = struct.unpack("<i", b[18:22])[0]; h = struct.unpack("<i", b[22:26])[0]
    px = struct.unpack("<H", b[28:30])[0] // 8
    return b, w, abs(h), off, ((w * px + 3) & ~3), px

def dump(sid, path, timeout=2.0):
    try: os.remove(path)
    except OSError: pass
    subprocess.run(["LayerManagerControl", "dump", "surface", sid, "to", path],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    dec = None; t0 = time.time()
    while time.time() - t0 < timeout:
        try: sz = os.path.getsize(path)
        except OSError: sz = 0
        if sz >= 6 and dec is None:
            with open(path, "rb") as f:
                f.seek(2); dec = struct.unpack("<I", f.read(4))[0]
        if dec and sz >= dec:
            with open(path, "rb") as f: return f.read()
        time.sleep(0.003)
    return None

def mostly_black(bm, samples=2048):
    data, w, h, off, stride, px = bm
    end = off + stride * h; step = max(px, ((end - off) // samples // px or 1) * px); i = off
    while i + 3 <= end:
        if data[i] > 16 or data[i + 1] > 16 or data[i + 2] > 16:
            return False
        i += step
    return True

def region(bm, sx, sy, sw, sh, dw, dh):
    data, w, h, off, stride, px = bm
    cols = [(sx + dx * sw // dw) * px for dx in range(dw)]
    rows = []
    for dy in range(dh):
        rb = off + (h - 1 - (sy + dy * sh // dh)) * stride
        rows.append(b"".join(data[rb + c: rb + c + 3] for c in cols))
    return rows

# ── scene 파싱: HMI(전체화면) + MAP(최대 면적 비검정 서브 서피스) 자동 식별 ──
scr = lmc(["get", "screen", "0"])
SW, SH = f_xy(scr, "resolution:") or (1560, 878)
TH = TH_OVR if TH_OVR > 0 else max(1, round(TW * SH / SW))
LW = LH = 0; order = []
for lid in f_ids(scr, "layer render order:"):
    lt = lmc(["get", "layer", lid]); o = f_ids(lt, "surface render order:")
    if o:
        LW, LH = f_xy(lt, "original size:") or (SW, SH); order = o; break
if not order:
    LW, LH = SW, SH

hmi = None; cands = []
for sid in order:
    st = lmc(["get", "surface", sid]); v = re.search(r"visibility:\s*(\d+)", st)
    if v and v.group(1) == "0":
        continue
    src = f_reg(st, "source region:"); dst = f_reg(st, "destination region:")
    osz = f_xy(st, "original size:")
    if not src or not dst:
        continue
    if osz and osz[0] * osz[1] < 64 * 64:   # cursor/system surface 제외
        continue
    if dst[0] == 0 and dst[1] == 0 and dst[2] >= LW * 0.95 and dst[3] >= LH * 0.95:
        hmi = (sid, src)                      # 전체화면 = HMI 크롬
    else:
        cands.append((sid, src, dst))
mapsel = None; best = -1
for sid, src, dst in cands:
    bm = dump(sid, "/tmp/_mlsel.bmp")
    if bm is None or mostly_black(parse(bm)):
        continue
    area = dst[2] * dst[3]
    if area > best:
        best = area; mapsel = (sid, src, dst)

if mapsel:
    _, _, md = mapsel
    mx = md[0] * TW // LW; my = md[1] * TH // LH
    mw = max(1, md[2] * TW // LW); mh = max(1, md[3] * TH // LH)
else:
    mx = my = mw = mh = 0

sys.stderr.write("LIVE hmi=%s map=%s TW=%d TH=%d LW=%d LH=%d map_dst=%d,%d,%d,%d\n"
                 % (hmi[0] if hmi else None, mapsel[0] if mapsel else None,
                    TW, TH, LW, LH, mx, my, mw, mh))
sys.stderr.flush()

out = sys.stdout.buffer
HMI_BMP, MAP_BMP = "/tmp/_mlhmi.bmp", "/tmp/_mlmap.bmp"
while True:
    try:
        if hmi is None:
            time.sleep(0.2); continue
        hb = dump(hmi[0], HMI_BMP)
        if hb is None:
            time.sleep(0.05); continue
        hp = parse(hb); hs = hmi[1]
        hbytes = b"".join(region(hp, hs[0], hs[1], hs[2], hs[3], TW, TH))
        mbytes = b""
        if mapsel and mw > 0 and mh > 0:
            mb = dump(mapsel[0], MAP_BMP)
            if mb is not None:
                mp = parse(mb); ms = mapsel[1]
                mbytes = b"".join(region(mp, ms[0], ms[1], ms[2], ms[3], mw, mh))
        cmw, cmh = (mw, mh) if mbytes else (0, 0)
        # SW,SH(실제 화면 해상도)도 보고 → 백엔드가 등록 해상도를 보정(터치 좌표 일치).
        out.write(b"MIBF" + struct.pack("<HHhhHHHH", TW, TH, mx, my, cmw, cmh, SW, SH) + hbytes + mbytes)
        out.flush()
    except (BrokenPipeError, IOError):
        break
    except Exception:
        time.sleep(0.05)
'''


class LiveStreamMixin:
    """surface 합성 라이브 스트리밍 — 캡처/디코드/합성 공용. 터치/해상도 정책은 서비스별."""

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
        # 타깃 가로(다운스케일). 세로=0이면 화면비율 자동(인치별 적응). LIVE_*(공용)/MIB_LIVE_*(호환).
        self._live_w = _ei(["LIVE_W", "MIB_LIVE_W"], 640)
        self._live_h = _ei(["LIVE_H", "MIB_LIVE_H"], 0)
        self._live_jpeg_q = _ei(["LIVE_JPEG_Q", "MIB_LIVE_JPEG_Q"], 60)
        # 맵 합성 게이트: HMI 맵-영역이 "대부분 구멍(검정)"일 때만 맵을 합성한다.
        # 알파가 없어 '투명 구멍'과 '불투명 어두운 콘텐츠'를 픽셀값으로 근사 —
        # 홈은 진짜 구멍이라 ≤8 비율 ~98%, Dial/차량뷰는 ~5% 이하라 게이트로 분리됨.
        self._map_key_t = _ei(["MAP_KEY_T"], 8)          # 검정 판정 임계
        try:
            self._map_hole_gate = float(os.environ.get("MAP_HOLE_GATE") or 0.5)
        except Exception:
            self._map_hole_gate = 0.5                      # 맵-영역 검정비율 컷오프

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
            script = (_LIVE_STREAMER
                      .replace("__TW__", str(self._live_w))
                      .replace("__TH__", str(self._live_h)))
            chan.sendall(script.encode("utf-8"))
            chan.shutdown_write()  # 스트리머는 stdin을 읽지 않음 → 즉시 EOF
            t = threading.Thread(target=self._live_reader, args=(chan,), daemon=True)
            with self._live_lock:
                self._live_ssh = ssh
                self._live_chan = chan
                self._live_thread = t
            t.start()
            logger.info("%s live stream started (%dx%d, q=%d)",
                        self._live_label, self._live_w, self._live_h, self._live_jpeg_q)
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
        """채널에서 MIBF 프레임(HMI + 맵 따로)을 파싱 → numpy black-key 합성 → JPEG.

        프레임: b"MIBF" + struct('<HHhhHHHH', tw,th,mx,my,mw,mh,sw,sh) + HMI(BGR) + MAP(BGR)
        합성: HMI 베이스, 맵-사각형 내 HMI가 (거의) 검정인 곳(=투명 구멍)에만 맵.
              팝업/메뉴가 맵 위로 뜨면 그 영역은 HMI(불투명)라 맵이 가리지 않는다.
        """
        from PIL import Image
        import numpy as np
        HDR = 20  # b"MIBF"(4) + '<HHhhHHHH'(16): tw,th,mx,my,mw,mh,sw,sh
        buf = b""
        try:
            chan.settimeout(5.0)
        except Exception:
            pass
        while not self._live_stop.is_set():
            try:
                data = chan.recv(262144)
            except Exception:
                break
            if not data:
                break  # 채널 닫힘 → 스트리머 종료
            buf += data
            while True:
                idx = buf.find(b"MIBF")
                if idx < 0:
                    if len(buf) > (1 << 23):
                        buf = buf[-8:]
                    break
                if len(buf) < idx + HDR:
                    break
                tw, th, mx, my, mw, mh, sw, sh = struct.unpack("<HHhhHHHH", buf[idx + 4: idx + HDR])
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
                hmi_len = tw * th * 3
                map_len = mw * mh * 3
                total = idx + HDR + hmi_len + map_len
                if len(buf) < total:
                    break
                hmi_bytes = buf[idx + HDR: idx + HDR + hmi_len]
                map_bytes = buf[idx + HDR + hmi_len: total]
                buf = buf[total:]
                if len(hmi_bytes) != hmi_len:
                    continue
                try:
                    comp = np.frombuffer(hmi_bytes, np.uint8).reshape(th, tw, 3).copy()
                    if mw > 0 and mh > 0 and len(map_bytes) == map_len:
                        x0 = max(0, mx); y0 = max(0, my)
                        reg = comp[y0:y0 + mh, x0:x0 + mw]
                        rh, rw = reg.shape[0], reg.shape[1]
                        if rh > 0 and rw > 0:
                            mp = np.frombuffer(map_bytes, np.uint8).reshape(mh, mw, 3)[:rh, :rw]
                            hole = reg.max(axis=2) <= self._map_key_t  # HMI가 (거의) 검정 = 투명 구멍
                            # 맵-영역이 대부분 구멍일 때만 맵 합성(홈). Dial/차량뷰처럼 불투명
                            # 콘텐츠가 채운 화면은 검정비율이 낮아 맵을 안 깐다(비침 방지).
                            if hole.mean() > self._map_hole_gate:
                                reg[hole] = mp[hole]
                    img = Image.fromarray(comp[:, :, ::-1], "RGB")  # BGR→RGB
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
