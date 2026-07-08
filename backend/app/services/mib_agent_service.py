"""MIB Agent Service — SSH 기반 VW MIB(Modular Infotainment Building Block) HU 제어.

ICAS Agent와 비슷한 SSH+ksend 기반이지만 다른 ksend 변종을 사용:
  - dst/src를 작은 정수(1-63)로 받음 (bit-position form)
  - LayerManagerControl로 화면 dump (Wayland/weston 환경)
  - /vw/hmi 디렉토리 구조 (Java HMI + mcu-interpreter + vtee)

지원 범위:
  - Touch: tap / swipe / long_press / repeat_tap
  - Hardkey: VOLUME_UP, VOLUME_DOWN, MUTE, HOME, POWER
  - Screenshot: HU (LayerManagerControl dump + SCP pull)

좌표 인코딩 (참조 구현 동일):
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
import struct
import threading
import time
from typing import Optional, Callable

logger = logging.getLogger(__name__)

# ── 라이브 미러 device-side 스트리머 (인치 무관 동적 합성) ──
# NOTE: 이 스트리머/스트리밍 메서드는 live_stream_mixin.LiveStreamMixin 과 동일 구현이다.
#   ICAS는 mixin을 상속해 쓴다(중복 제거). MIB은 검증·동작 중이라 인라인 유지 중 —
#   추후 MIB도 mixin 상속으로 이관 예정(그때 아래 상수/메서드 삭제). 수정 시 양쪽 동기화 필요.
# weston screen dump은 PNG 인코딩 때문에 0.63s/frame(1.6fps 천장)이라 라이브에 부적합.
# 대신 surface를 무압축 BMP로 dump(~20ms)해서, 연결 시 LayerManagerControl get scene을
# 파싱해 HMI(전체화면 서피스) + MAP(최대 면적의 비검정 서브 서피스)을 자동 식별한다
# (하드코딩 좌표/ID 없음 → 10"/12.9"/15"/8" 등 모든 인치 자동 적응).
# device엔 PIL/numpy 없어 순수 stdlib로 nearest 다운스케일만 하고, HMI/MAP을 따로 송출:
#   b"MIBF" + struct('<HHhhHH', tw, th, mx, my, mw, mh) + HMI(tw*th*3 BGR) + MAP(mw*mh*3 BGR)
# 백엔드가 numpy black-key 합성(HMI가 검정=투명인 곳에만 MAP) 후 JPEG.
# __TW__ / __TH__(0=화면비율 자동)는 start_live_stream에서 .replace()로 주입.
_MIB_LIVE_STREAMER = r'''
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

def clip_to_layer(src, dst):
    # dst가 레이어를 벗어나는 서피스(예: 800x480 레이어에 1280x640 backdrop 맵)는
    # 보이는 부분만 유효 — dst를 레이어로 클립하고 src도 비례 보정.
    x0, y0, dw, dh = dst
    cx0 = max(0, x0); cy0 = max(0, y0)
    cx1 = min(LW, x0 + dw); cy1 = min(LH, y0 + dh)
    cw = cx1 - cx0; ch = cy1 - cy0
    if cw <= 0 or ch <= 0:
        return None
    if cw == dw and ch == dh:
        return src, dst
    sx, sy, sw, sh = src
    return ((sx + sw * (cx0 - x0) // dw, sy + sh * (cy0 - y0) // dh,
             max(1, sw * cw // dw), max(1, sh * ch // dh)),
            (cx0, cy0, cw, ch))

hmi = None; cands = []; hmi_cands = []
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
    # HMI = dst가 레이어와 '정확히' 일치(±5%)하는 풀스크린만. 레이어보다 큰 dst
    # (MQB 800x480처럼 backdrop 맵이 1280x640으로 깔리는 모델)는 맵 후보 — 클립해서 cands로.
    if (dst[0] == 0 and dst[1] == 0 and LW * 0.95 <= dst[2] <= LW * 1.05
            and LH * 0.95 <= dst[3] <= LH * 1.05):
        hmi_cands.append((sid, src))
    else:
        c = clip_to_layer(src, dst)
        if c:
            cands.append((sid, c[0], c[1]))
# HMI 후보가 여럿이면(black 베이스 서피스 + 진짜 크롬 공존 모델) 비검정 중 마지막(최상위) 선택.
for sid, src in hmi_cands:
    b = dump(sid, "/tmp/_mlhpick.bmp")
    if b is not None and not mostly_black(parse(b)):
        hmi = (sid, src)
if hmi is None and hmi_cands:
    hmi = hmi_cands[-1]
# MAP = 최대 면적의 "비검정" 후보 (보조 서피스/빈 서피스 자동 배제)
def hole_frac(hp, dst, T=2):
    # 0x10(HMI)의 dst(layer좌표) 영역에서 검정(=투명 구멍) 픽셀 비율. 맵은 이 비율이 높은 자리.
    data, w, h, off, stride, px = hp
    x0, y0, rw, rh = dst
    gx = max(1, rw // 120); gy = max(1, rh // 120)
    cnt = tot = 0; yy = y0
    while yy < y0 + rh:
        if 0 <= yy < h:
            rb = off + (h - 1 - yy) * stride; xx = x0
            while xx < x0 + rw:
                if 0 <= xx < w:
                    o = rb + xx * px
                    if o + 3 <= len(data):
                        tot += 1
                        if max(data[o], data[o + 1], data[o + 2]) <= T:
                            cnt += 1
                xx += gx
        yy += gy
    return (cnt / tot) if tot else 0.0

def select_map():
    # 맵 = 0x10의 '구멍(검정)'이 가장 많이 겹치는 후보(투명 뒤 backdrop). 면적 휴리스틱 폐기.
    # 구멍 비율이 낮으면(맵 없는 화면) None → 맵 합성 안 함(비침 방지). 화면 전환마다 재선택.
    if hmi is None:
        return None
    hb = dump(hmi[0], "/tmp/_mlhsel.bmp")
    if hb is None:
        return None
    hp = parse(hb); best = None; bestf = 0.55
    for sid, src, dst in cands:
        bm = dump(sid, "/tmp/_mlsel.bmp")
        if bm is None or mostly_black(parse(bm)):
            continue
        f = hole_frac(hp, dst)
        if f > bestf:
            bestf = f; best = (sid, src, dst)
    return best

def map_rect(ms):
    if not ms:
        return 0, 0, 0, 0
    md = ms[2]
    return (md[0] * TW // LW, md[1] * TH // LH,
            max(1, md[2] * TW // LW), max(1, md[3] * TH // LH))

sys.stderr.write("MIBLIVE hmi=%s hmi_cands=%s cands=%s TW=%d TH=%d LW=%d LH=%d\n"
                 % (hmi[0] if hmi else None, [c[0] for c in hmi_cands],
                    [(c[0], c[2]) for c in cands], TW, TH, LW, LH))
sys.stderr.flush()

out = sys.stdout.buffer
HMI_BMP, MAP_BMP = "/tmp/_mlhmi.bmp", "/tmp/_mlmap.bmp"
mapsel = None; sel_t = -999.0
while True:
    try:
        if hmi is None:
            time.sleep(0.2); continue
        now = time.time()
        if now - sel_t > 2.0:   # 2초마다 맵 재선택(화면 전환 적응; 맵 없는 화면이면 None)
            mapsel = select_map(); sel_t = now
        hb = dump(hmi[0], HMI_BMP)
        if hb is None:
            time.sleep(0.05); continue
        hp = parse(hb); hs = hmi[1]
        hbytes = b"".join(region(hp, hs[0], hs[1], hs[2], hs[3], TW, TH))
        mx, my, mw, mh = map_rect(mapsel)
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

# ── 하드키 서브 커맨드 (HKMC6thService API 호환용 — 내부적으로 press/release 구분) ──
SHORT_KEY = 0x43
LONG_KEY = 0x44
PRESS_KEY = 0x41
RELEASE_KEY = 0x42


# ── MIB 하드키 테이블 ──
# class: "short" (13B) / "long" (15B) / "volume" (15B, signed delta)
# key: KEY_CODE 바이트 (short/long) 또는 signed delta (volume: +1=UP, -1=DOWN)
# category: long frame 전용 — byte 9 카테고리 (0x30=power/home, 0x48=volume)
#
# CAN 분석 (arbitration_id=0x17f8f173):
#   POWER press:     04 30 38 01 04           → long frame, category=0x30, key=0x38
#   HOME press:      04 30 66 01 04           → long frame, category=0x30, key=0x66
#   MUTE:            short frame (key=0x20, 동작 확인됨)
#   VOLUME_UP press: 06 48 01 01 00 82        → volume frame, delta=+1 (0x01)
#   VOLUME_DOWN press: 06 48 01 ff 00 82      → volume frame, delta=-1 (0xFF signed)
#   VOLUME release:  06 48 01 00 [delta] 82   → delta가 byte 4로 이동 (swap)
MIB_KEYS: dict[str, dict] = {
    # VOLUME 전용 frame: byte 11=delta(press)/0(release), byte 12=0(press)/delta(release)
    "VOLUME_UP":   {"class": "volume", "key": 0x01},   # +1
    "VOLUME_DOWN": {"class": "volume", "key": 0xFF},   # -1 (signed)
    "MUTE":        {"class": "short",  "key": 0x20},
    "HOME":        {"class": "long",   "key": 0x66, "category": 0x30},
    "POWER":       {"class": "long",   "key": 0x38, "category": 0x30},
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


def _encode_image(pil_image, fmt: str) -> bytes:
    """PIL Image → PNG/JPEG 바이트."""
    buf = io.BytesIO()
    if (fmt or "png").lower() == "jpeg":
        pil_image.convert("RGB").save(buf, format="JPEG", quality=85)
    else:
        pil_image.save(buf, format="PNG")
    return buf.getvalue()


def _validate_png_file(path: str) -> bool:
    """PNG 파일이 시그니처 + IEND chunk를 모두 갖춘 완전한 파일인지 빠르게 검증.

    PIL.Image.open의 lazy load는 IEND 부재 등 일부 손상에 무관심하지만, .convert('RGBA')에서
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
            # (정확히 마지막 12바이트만 보면 멀쩡한 PNG도 truncated로 오판 → 캡처 전체 폐기됨).
            scan = min(size, 4096)
            f.seek(-scan, 2)
            tail = f.read(scan)
            if b"IEND" not in tail:
                return False
        return True
    except Exception:
        return False


def _read_png_ihdr_size(path: str) -> Optional[tuple[int, int]]:
    """PNG IHDR(첫 청크)에서 실제 width/height를 읽는다.

    파일 끝이 truncated/corrupt 되어 IEND가 없어도 IHDR은 파일 맨 앞(byte 16~24)에
    위치하므로 디바이스 실제 화면 해상도를 신뢰성 있게 얻을 수 있다. 캡처 디코딩 성공
    여부와 무관하게 터치 좌표 스케일링(_x_mult/_y_mult)을 보정하기 위함.
    반환: (width, height) 또는 실패 시 None.
    """
    try:
        with open(path, "rb") as f:
            header = f.read(24)
        if len(header) < 24:
            return None
        if header[:8] != b"\x89PNG\r\n\x1a\n":
            return None
        if header[12:16] != b"IHDR":
            return None
        width = int.from_bytes(header[16:20], "big")
        height = int.from_bytes(header[20:24], "big")
        if width <= 0 or height <= 0:
            return None
        return width, height
    except Exception:
        return None


def _png_partially_decodable(path: str) -> bool:
    """IEND까지 완전하진 않아도 LOAD_TRUNCATED_IMAGES로 디코딩 가능한 PNG인지 확인.

    디바이스 LayerManagerControl dump가 PNG를 끝까지 못 쓰는 환경(예: /tmp tmpfs 부족,
    dump 중단)에서 파일이 truncated 되어도, 첫 IDAT 일부만 온전하면 PIL이 하단을 회색으로
    채워 디코딩한다. 화면 표시·해상도 감지·터치 좌표 스케일링에는 충분하므로, 캡처를 통째로
    버려 화면이 영영 안 뜨는 것보다 부분 이미지라도 사용하는 편이 낫다.
    """
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True
        with Image.open(path) as im:
            im.load()  # 실제 디코딩 강제 — truncated면 LOAD_TRUNCATED_IMAGES가 하단을 채움
            w, h = im.size
        return w > 0 and h > 0
    except Exception:
        return False


def _rm_tree(path: str) -> None:
    try:
        import shutil
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


class MIBAgentService:
    """SSH 기반 MIB HU 제어 서비스.

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
                 key_overrides: Optional[dict[str, dict]] = None,
                 on_resolution_changed: Optional[Callable[[str], None]] = None,
                 on_addr_changed: Optional[Callable[[str, str], None]] = None,
                 screen_indices: Optional[list[int]] = None):
        self.host = host
        self.port = int(port)
        self.device_id = device_id or f"MIB_{host}"
        self.username = username
        self.password = password or ""
        self._resolution = resolution.upper()
        self._parse_resolution()
        # market 분기 (RemoteController.py 라인 63-75 참조)
        # EU/NAR/CN: legacy 주소 + IPv6 private server
        # GP(KR): 숫자 주소 + IPv4 private server
        self.market = (market or "EU").upper()
        self._apply_market_defaults(self.market, private_server_ip)
        self.private_server_password = private_server_password
        self.iid_display = str(iid_display or "10")
        self.hud_display = str(hud_display or "11")

        self._connected = False
        self.agent_version = "MIB Agent"
        # 마지막 입력(터치/하드키) 시각 (time.monotonic). 화면 미러 루프가 이 값을 읽어
        # 적응형 리프레시(입력 없으면 10s, 입력 직후 2s×5회 burst)를 결정한다. 입력은 모두
        # _ksend/_ksend_many를 거치므로 그 진입점에서만 갱신하면 됨(캡처는 ksend를 안 씀).
        self.last_input_ts = 0.0
        # 터치 좌표 보정 오프셋 (디바이스 좌표 공간, px). 일부 MIB 유닛은 dump 이미지의 원점과
        # 터치 디지타이저의 원점이 일정하게 어긋난다(예: 상단 상태바 ~70px). 디바이스별로
        # 캘리브레이션해 _touch_frame에서 모든 터치(tap/swipe/long_press)에 일괄 적용.
        # 환경변수(MIB_TOUCH_X_OFFSET/MIB_TOUCH_Y_OFFSET)가 초기 기본값, set_touch_offsets()가 재정의.
        def _env_int(name: str) -> int:
            try:
                return int(os.environ.get(name, "0") or 0)
            except Exception:
                return 0
        self._touch_x_offset = _env_int("MIB_TOUCH_X_OFFSET")
        self._touch_y_offset = _env_int("MIB_TOUCH_Y_OFFSET")
        # 터치 디지타이저 좌표 스케일.
        # MIB 터치 디지타이저는 "화면 픽셀"이 아니라 화면의 ~절반 해상도(양축)를 좌표공간으로 쓴다
        # (검증: 10.4" Y 720→350 ≈ ÷2). → sent = 화면좌표 × 0.5, mult=1로 직접 인코딩.
        # (구버전은 scale=mult/2 후 ÷mult였는데, 폭2240(15") mult=3에서 사전클램프 버그 → 직접 ÷2로 변경.)
        # 등록 해상도(res)는 풀해상도 screencap crop·프론트 매핑용으로 화면 그대로 유지.
        # 기본은 None(=0.5); 패널이 다르면 env로 절대 스케일 override.
        def _env_float_opt(name: str):
            v = os.environ.get(name)
            if not v:
                return None
            try:
                return float(v)
            except Exception:
                return None
        self._touch_x_scale = _env_float_opt("MIB_TOUCH_X_SCALE")  # None → x_mult/2
        self._touch_y_scale = _env_float_opt("MIB_TOUCH_Y_SCALE")  # None → y_mult/2
        # 캡처 전용 SSH 세션 — LayerManagerControl dump + SCP pull 용. 캡처 한 사이클은
        # 1초 가까이 걸리므로, 같은 락에 묶이면 그 사이 입력(ksend)이 블록됨.
        # 따라서 터치/하드키는 별도 _input_ssh_*에서 보내 캡처와 병렬화.
        self._ssh_client = None
        self._ssh_shell = None  # (legacy, 더 이상 사용하지 않음 — 입력은 _input_ssh_shell이 담당)
        self._ssh_lock = threading.RLock()
        self._ssh_keepalive_interval = 30  # seconds; transport.set_keepalive로 TCP idle 방지
        # 입력(터치/하드키) 전용 SSH 세션 — 캡처 락과 독립이라 SCP가 바빠도 ksend는 즉시 송신.
        # invoke_shell도 이 connection 위에서 유지하여 fire-and-forget 패턴 그대로 사용.
        self._input_ssh_client = None
        self._input_ssh_shell = None
        self._input_ssh_lock = threading.RLock()
        # IID/HUD 캡처 — private_server로의 direct-tcpip 터널 + SSH 클라이언트도 장수명 캐시.
        # 매 프레임마다 paramiko.connect() 인증(~300-500ms)을 반복하지 않도록.
        self._ps_ssh = None
        self._ps_tunnel_chan = None
        self._ps_lock = threading.RLock()
        # ── 라이브 미러 스트리밍 상태 ──
        # 전용 SSH exec 채널에서 device 스트리머(python3)를 상주시키고, 리더 스레드가
        # MIBF 프레임을 받아 BGR→JPEG로 변환해 최신 1장을 보관. main.py WS 루프는
        # get_live_frame()으로 최신본을 꺼내 보낸다(프레임당 SCP/페이싱 제거).
        # 라이브는 저해상도/저화질로 충분(조작용); 풀해상도 _screencap_hu는 이미지비교용으로 유지.
        # 라이브는 전용 SSH 연결을 쓴다 — 풀해상도 screencap이 공유 SSH를 리셋해도
        # 주 화면(라이브)이 끊기지 않도록 격리.
        self._live_ssh = None
        self._live_chan = None
        self._live_thread = None
        self._live_stop = threading.Event()
        self._live_lock = threading.Lock()
        self._latest_live_jpeg: Optional[bytes] = None
        self._live_frame_id = 0
        self._live_res_synced = False  # 스트림당 1회 등록 해상도 보정 가드

        def _env_int_def(name: str, default: int) -> int:
            try:
                return int(os.environ.get(name) or default)
            except Exception:
                return default
        # 타깃 가로 해상도(다운스케일). 세로(_live_h)는 0=화면비율 자동(인치별 자동 적응).
        # 작을수록 device python 다운스케일이 빨라져 fps↑(해상도↑=fps↓ 트레이드오프).
        self._live_w = _env_int_def("MIB_LIVE_W", 832)  # 640*1.3 (가독성↑, fps 약간↓)
        self._live_h = _env_int_def("MIB_LIVE_H", 0)  # 0 → TH = TW*SH/SW 자동
        self._live_jpeg_q = _env_int_def("MIB_LIVE_JPEG_Q", 60)
        # 맵 합성 게이트 (live_stream_mixin과 동일) — 맵-영역이 "대부분 구멍(검정)"일 때만
        # 맵 합성. 홈은 진짜 구멍이라 ≤8 비율 ~98%, Dial/차량뷰는 ~5%↓라 게이트로 분리.
        self._map_key_t = _env_int_def("MAP_KEY_T", 2)
        try:
            self._map_hole_gate = float(os.environ.get("MAP_HOLE_GATE") or 0.8)
        except Exception:
            self._map_hole_gate = 0.8
        self._key_overrides: dict[str, dict] = dict(key_overrides or {})
        # 캡처에서 PNG 실제 크기와 _res_x/_res_y가 다를 때 자동 정정 + 영구 저장 콜백.
        # 시그니처: callback("WxH"). DeviceManager가 dev.info 갱신과 파일 저장을 담당.
        self._on_resolution_changed = on_resolution_changed
        # 콜백 폭주 방지 — 동일 해상도면 호출 안 함, 락으로 직렬화.
        self._res_callback_lock = threading.Lock()
        # ksend src/dst가 디바이스 ksend 변종에서 거부될 때(예: bit-position form만 허용)
        # 자동 보정 후 영구 저장하기 위한 콜백. 시그니처: callback(src, dst).
        self._on_addr_changed = on_addr_changed
        # LayerManagerControl로 dump할 screen 인덱스 — 디바이스마다 가용한 layer가 다름.
        # 기본값 [0, 2]은 일반적인 IVI 환경 추정치. 일부 단일 디스플레이 MIB는 [0]만 존재.
        # 캡처 실패가 누적되면 해당 인덱스를 자동 비활성화하고, 첫 연결 시 진단 명령으로 가용 레이어를 학습.
        if screen_indices is None or not screen_indices:
            self._screen_indices: list[int] = [0, 2]
        else:
            self._screen_indices = [int(i) for i in screen_indices]
        # 인덱스별 연속 실패 카운트. 이 임계치 이상이면 비활성화.
        self._screen_fail_count: dict[int, int] = {i: 0 for i in self._screen_indices}
        self._screen_disabled: set[int] = set()
        self._screen_fail_threshold = 3  # 3회 연속 실패하면 해당 인덱스 dump 시도 중단

    # ------------------------------------------------------------------
    # Basic accessors
    # ------------------------------------------------------------------
    def _parse_resolution(self) -> None:
        try:
            rx, ry = self._resolution.upper().split("X")
            self._res_x = int(rx)
            self._res_y = int(ry)
        except Exception:
            self._res_x, self._res_y = 1560, 700
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

    def _maybe_autoupdate_resolution(self, width: int, height: int) -> bool:
        """캡처된 PNG 크기와 현재 _res_x/_res_y 비교 후 다르면 자동 갱신.

        반환값: 실제로 갱신되었는지 여부. 콜백은 DeviceManager가 주입하며
        dev.info dict + 파일 저장을 담당. 동일 해상도면 no-op.
        """
        if width <= 0 or height <= 0:
            return False
        if width == self._res_x and height == self._res_y:
            return False
        with self._res_callback_lock:
            new_res = f"{width}x{height}"
            self._resolution = new_res.upper()
            self._parse_resolution()
            cb = self._on_resolution_changed
        if cb is not None:
            try:
                cb(new_res)
            except Exception as e:
                logger.warning("MIB on_resolution_changed callback failed: %s", e)
        else:
            logger.info("MIB resolution auto-detected (no persistence callback): %s", new_res)
        return True

    def detect_resolution(self) -> tuple[int, int]:
        """1회 캡처를 트리거해 디바이스 실제 해상도를 반환 + 자동 갱신.

        호출자: 자동 감지 버튼 / 등록 직후 1회 보정.
        반환: (width, height). 캡처 실패 시 RuntimeError 전파.
        """
        # _screencap_hu 내부에서 _maybe_autoupdate_resolution이 호출되므로
        # 캡처 후 self._res_x/_res_y가 곧 디바이스 실제 해상도가 됨.
        self._screencap_hu(fmt="png")
        return self._res_x, self._res_y

    def set_addr(self, src: str, dst: str) -> None:
        """src/dst ksend 주소 변경 (EU/NAR/CN/GP 분기)."""
        self.src_addr = src
        self.dst_addr = dst

    def _apply_market_defaults(self, market: str, private_server_ip_override: str = "") -> None:
        """market 값에 따라 ksend src/dst 주소 + private_server_ip 기본값 설정.

        RemoteController.py 라인 63-75 참조:
          EU/NAR/CN (legacy): src=0x200000000000000, dst=0x80000000000, private=IPv6
          GP/KR (bit-position): src=57, dst=43, private=IPv4 192.168.0.2
        private_server_ip_override가 비어있지 않으면 그 값을 그대로 사용.
        """
        m = (market or "EU").upper()
        if m in ("EU", "NAR", "CN"):
            self.src_addr = "0x200000000000000"
            self.dst_addr = "0x80000000000"
            default_private = "fd53:7cb8:383:3::73"
            ksend_form = "legacy_hex"
        else:
            # GP/KR: bit-position decimal form
            self.src_addr = "57"
            self.dst_addr = "43"
            default_private = "192.168.0.2"
            ksend_form = "bit_position_decimal"
        self.private_server_ip = private_server_ip_override or default_private
        logger.debug(
            "MIB market defaults applied: market=%s ksend_form=%s "
            "src_addr=%s dst_addr=%s private_server_ip=%s",
            m, ksend_form, self.src_addr, self.dst_addr, self.private_server_ip
        )

    def set_market(self, market: str, private_server_ip_override: str = "") -> None:
        """런타임 market 전환 (addr + private_server_ip 동시 갱신).

        market 변경 시 모든 hardkey 동작(특히 POWER)의 추가 메시지 주소가 자동으로 갱신됨.
        """
        old_market = self.market
        old_src = self.src_addr
        old_dst = self.dst_addr

        self.market = (market or "EU").upper()
        self._apply_market_defaults(self.market, private_server_ip_override)

        logger.info(
            "MIB market switched: %s → %s (src: %s → %s, dst: %s → %s)",
            old_market, self.market, old_src, self.src_addr, old_dst, self.dst_addr
        )

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
        ssh.connect(self.host, username=self.username, port=self.port,
                    password=self.password, timeout=10)
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

    def _get_input_ssh(self):
        """입력 전용 SSH 클라이언트 반환 — 끊어졌으면 재연결.

        _input_ssh_lock 안에서 호출해야 함. 캡처 SSH(_ssh_client)와는 완전히 독립된
        TCP 세션이라 한쪽이 바빠도 다른 쪽은 영향 없음.
        """
        if self._is_ssh_alive(self._input_ssh_client):
            return self._input_ssh_client
        if self._input_ssh_client is not None:
            try:
                self._input_ssh_client.close()
            except Exception:
                pass
            self._input_ssh_client = None
        if self._input_ssh_shell is not None:
            try:
                self._input_ssh_shell.close()
            except Exception:
                pass
            self._input_ssh_shell = None
        ssh = self._new_ssh()
        try:
            t = ssh.get_transport()
            if t is not None:
                t.set_keepalive(self._ssh_keepalive_interval)
        except Exception:
            pass
        self._input_ssh_client = ssh
        return ssh

    def _get_input_shell(self):
        """입력 전용 invoke_shell 채널 반환 — ksend 등 fire-and-forget 명령용."""
        ssh = self._get_input_ssh()
        if self._input_ssh_shell is not None:
            try:
                if not self._input_ssh_shell.closed:
                    return self._input_ssh_shell
            except Exception:
                pass
            try:
                self._input_ssh_shell.close()
            except Exception:
                pass
            self._input_ssh_shell = None
        shell = ssh.invoke_shell()
        shell.settimeout(0.5)
        # 초기 프롬프트 드레인 — 최대 1초
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                if shell.recv_ready():
                    shell.recv(65536)
                else:
                    time.sleep(0.05)
            except Exception:
                break
        self._input_ssh_shell = shell
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
        """입력 전용 shell 채널로 명령 송신 + drain. transport/shell dead면 1회 리셋 재시도.

        캡처 SSH 락(_ssh_lock)과 독립된 _input_ssh_lock에서 실행되므로,
        스크린샷 SCP가 진행 중이어도 터치/하드키는 즉시 송신됨.
        """
        def _do(shell) -> None:
            for c in commands:
                shell.send(c + "\n")
                if post_sleep_s > 0:
                    time.sleep(post_sleep_s)
                self._drain_shell(shell)

        with self._input_ssh_lock:
            try:
                shell = self._get_input_shell()
                _do(shell)
                return
            except Exception as e:
                logger.warning("MIB input shell exec failed, retrying: %s", e)
                # shell 리셋 → 다시 시도 (transport가 살아있으면 재사용, 죽었으면 재연결)
                if self._input_ssh_shell is not None:
                    try:
                        self._input_ssh_shell.close()
                    except Exception:
                        pass
                    self._input_ssh_shell = None
            shell = self._get_input_shell()
            _do(shell)

    def connect(self, timeout: float = 10.0) -> bool:
        """캡처/입력 SSH 세션을 모두 확보. 두 세션은 독립이라 한쪽이 바빠도 다른쪽 영향 없음."""
        try:
            with self._ssh_lock:
                self._get_shared_ssh()  # 캡처용 SSH 사전 확보
            with self._input_ssh_lock:
                self._get_input_ssh()   # 입력용 SSH 사전 확보 (첫 ksend 지연 제거)
            self._connected = True
            logger.info(
                "MIB connected to %s:%d (market=%s, src_addr=%s, dst_addr=%s) "
                "[capture+input sessions]",
                self.host, self.port, self.market, self.src_addr, self.dst_addr
            )
            # 캡처 layer 진단: 가용 screen/layer 인덱스를 알면 사용자에게 가이드 제공.
            # 실패해도 연결 자체에는 영향 없음 (best-effort, 5초 타임아웃).
            try:
                self._probe_layer_info()
            except Exception as e:
                logger.debug("MIB layer probe skipped: %s", e)
            # ksend 입력 경로 진단: 바이너리 존재 + src/dst addr로 더미 프레임 송신 결과 확인.
            try:
                self._probe_ksend()
            except Exception as e:
                logger.debug("MIB ksend probe skipped: %s", e)
            # ksend가 현재 src/dst를 거부하면 bit-position form으로 자동 변환 + 영구 저장.
            try:
                self._try_autocorrect_addr()
            except Exception as e:
                logger.debug("MIB addr auto-correct skipped: %s", e)
            return True
        except Exception as e:
            logger.error("MIB connect failed %s:%d: %s", self.host, self.port, e)
            self._connected = False
            return False

    def _probe_ksend(self) -> None:
        """ksend 입력 경로의 가용성을 진단. 입력 전용 SSH 세션에서 실행.

        여러 진단 명령을 별도 라인으로 출력 (단일 라인이 길이 제한에 잘리지 않도록).
        """
        # 더미 송신: 현재 src/dst로 짧은 binary 메시지 1회. -v로 verbose 출력 활성.
        # 좌표 0,0 + end byte 0xFF(release)로 실 영향 최소화.
        dummy_data = (
            "0x83 0x50 0x20 0x0b 0x00 0x00 0x00 0x00 0x00 0xa0 0x01 0x11 "
            "0x10 0x00 0x00 0xff"
        )
        # ksend usage가 작은 정수 PID(`-s 10 -d 11`)를 사용하는 점, 기본 0x80000000000이
        # 32-bit overflow로 보이는 점을 근거로 다양한 dst 후보를 자동 시도.
        # 각 후보를 ksend -v로 송신 → "empty address data" 같은 파싱 에러 vs 정상 송신 식별.
        # 송신 자체는 메시지가 디바이스 KIPC 큐에 들어가도 처리될 보장은 없지만,
        # 최소한 ksend 파서가 받아들이는 형식을 좁힐 수 있음.
        candidates = [
            "0", "1", "2", "5", "10", "11", "16", "32", "43", "57", "63", "64",
            "100", "127", "128", "200", "255",
            "0x10", "0x20", "0x40", "0x80",
            # 32-bit/16-bit hex 변종 — overflow 안 되는 범위
            "0x800", "0x8000", "0x80000",
            "8796093022208",  # 0x80000000000을 decimal로
        ]
        ksend_sweep_lines = [
            f"echo '==dst={d}==' ; "
            f"/lge/app_ro/bin/ksend -v -s 0 -d {d} -b \"0x00 0x01\" 2>&1 | head -n 3 ; "
            f"echo \"exit=$?\""
            for d in candidates
        ]
        # (label, command, max_chars)
        probes: list[tuple[str, str, int]] = [
            ("ksend bin", "ls -la /lge/app_ro/bin/ksend 2>&1", 200),
            ("ksend usage", "/lge/app_ro/bin/ksend 2>&1 | head -n 25", 1500),
            ("input nodes", "ls -la /dev/input/ 2>&1 | head -n 30", 800),
            ("uinput", "ls -la /dev/uinput 2>&1", 200),
            ("KIPC procs",
             "(ps -ef 2>/dev/null || ps 2>/dev/null) | "
             "grep -iE '(touch|input|hmi|kipc|hardkey|remote|mcu|vtee)' | "
             "grep -v grep | head -n 25", 4000),
            ("KIPC proc dir", "ls -la /proc/lge_kipc/ 2>&1 | head -n 30", 1500),
            ("KIPC list",
             "for f in /proc/lge_kipc/list /proc/kipc/list "
             "/proc/lge_kipc/proc /sys/kernel/kipc/list ; do "
             "  if [ -e \"$f\" ]; then echo \"==$f==\"; cat \"$f\" 2>&1 | head -n 60 ; fi ; "
             "done", 4000),
            ("KIPC any",
             "find /proc -maxdepth 3 -name 'kipc*' 2>/dev/null | head -n 20", 1500),
            # 디바이스의 KIPC 설정 파일 찾기 — vw/hmi/lge 디렉토리에서 kipc 관련 conf
            ("KIPC conf",
             "( find /etc /vw /lge -maxdepth 6 -type f \\( -name '*kipc*' -o -name '*KIPC*' \\) 2>/dev/null ; "
             "  grep -rl -i 'kipc' /vw/hmi/config 2>/dev/null | head -n 10 ; "
             "  grep -rl -i 'kipc' /etc 2>/dev/null | head -n 10 ) | head -n 30", 2000),
            ("addr defaults",
             f"echo 'src={self.src_addr} dst={self.dst_addr} market={self.market}'",
             400),
            ("ksend -v dummy (current)",
             f"/lge/app_ro/bin/ksend -v -s {self.src_addr} -d {self.dst_addr} "
             f'-b "{dummy_data}" 2>&1 ; echo "exit=$?"', 2000),
            # 다양한 dst 후보 sweep — 어느 값이 ksend 파서를 통과하는지 식별
            ("ksend dst sweep", " ; ".join(ksend_sweep_lines), 6000),
        ]
        with self._input_ssh_lock:
            ssh = self._get_input_ssh()
            for label, cmd, limit in probes:
                try:
                    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=8)
                    try:
                        stdin.close()
                    except Exception:
                        pass
                    out = stdout.read().decode("utf-8", errors="replace")
                    err = stderr.read().decode("utf-8", errors="replace")
                    combined = out
                    if err.strip():
                        combined += "\n[stderr] " + err
                    snippet = combined.strip().replace("\r", " ").replace("\n", " | ")[:limit]
                    logger.info("MIB probe[%s] → %s", label, snippet or "(empty)")
                except Exception as e:
                    logger.debug("MIB probe[%s] failed: %s", label, e)

    def _wait_remote_files_stable(self, ssh, items: list[tuple[int, str]],
                                  max_wait_s: float = 1.0,
                                  poll_interval_s: float = 0.05,
                                  stable_iters: int = 2) -> None:
        """디바이스 쪽 파일 크기가 stable_iters회 연속 동일할 때까지 폴링.

        LayerManagerControl이 비동기로 PNG를 쓰는 환경에서 SCP가 partial 파일을 가져가는
        race를 막기 위함. items: [(idx, remote_path), ...]. 실패해도 silent — 안정성은
        PNG 무결성 검증(_validate_png_file)에서 한 번 더 거름.
        """
        if not items:
            return
        deadline = time.monotonic() + max_wait_s
        # 단일 SSH 명령으로 모든 파일 크기를 한 번에 조회 (왕복 비용 절감).
        size_cmd = " ; ".join([f"wc -c < {rp} 2>/dev/null || echo 0" for _, rp in items])
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
                # 모든 파일이 양수 + 직전과 동일하면 stable streak 증가
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

    def _wait_remote_png_complete(self, ssh, remote_paths: list[str],
                                  max_wait_s: float = 3.0,
                                  poll_interval_s: float = 0.05) -> None:
        """디바이스 PNG가 IEND(완성)까지 쓰여질 때까지 폴링한 뒤 SCP하도록 대기.

        LayerManagerControl dump가 파일을 비동기로 쓰는 환경에서는 exec_command가 끝나도
        writer가 1024-block 단위로 계속 append 중일 수 있다. 단순 size-stable 폴링은 writer가
        블록 경계에서 잠깐 멈춘 순간을 'stable'로 오판해 partial을 SCP하게 되고, 전송 크기가
        정확히 1024 배수로 잘린다(디바이스 파일은 완전한데 우리가 미완성 시점을 캡처 → 하단 row
        손실로 화면 하단/홈·apps 바가 깜빡임). 파일 끝 IEND가 보일 때까지 기다린다.

        busybox 호환: `tail -c 64 <f> | grep -q IEND`. 파일 상태를 한 SSH 호출로 일괄 조회:
          Y=완성(IEND有) / P=존재하나 미완성 / X=파일없음(기다려도 무의미).
        타임아웃 시 silent 진행 — partial은 상위에서 truncated-but-decodable로 수용된다.
        """
        if not remote_paths:
            return
        deadline = time.monotonic() + max_wait_s
        pending = list(remote_paths)
        while pending and time.monotonic() < deadline:
            checks = " ; ".join(
                f"if [ -s {rp} ]; then tail -c 64 {rp} 2>/dev/null | grep -q IEND "
                f"&& echo Y || echo P; else echo X; fi"
                for rp in pending
            )
            statuses: list[str] = []
            try:
                stdin, stdout, stderr = ssh.exec_command(checks, timeout=3)
                try:
                    stdin.close()
                except Exception:
                    pass
                out = stdout.read().decode("utf-8", errors="replace")
                statuses = [l.strip() for l in out.splitlines() if l.strip() in ("Y", "P", "X")]
            except Exception:
                statuses = []
            if len(statuses) != len(pending):
                time.sleep(poll_interval_s)
                continue
            # 아직 미완성(P)인 파일만 다음 라운드로
            new_pending = [rp for rp, st in zip(pending, statuses) if st == "P"]
            if not new_pending:
                return
            pending = new_pending
            time.sleep(poll_interval_s)

    @staticmethod
    def _bitmask_to_bit_position(addr: str) -> Optional[str]:
        """0x80000000000 (=1<<43) 같은 단일-비트 bitmask를 '43'(bit position)으로 변환.

        - addr이 power-of-two가 아니거나 0이면 None
        - 1 ≤ bit_position ≤ 63 범위 내일 때만 반환 (ksend 6-bit 한계)
        """
        try:
            v = int(addr, 0) if isinstance(addr, str) else int(addr)
            if v <= 0 or (v & (v - 1)) != 0:
                return None
            bp = v.bit_length() - 1
            if 0 < bp <= 63:
                return str(bp)
        except Exception:
            pass
        return None

    def _ksend_test_addr(self, src: str, dst: str, ssh) -> bool:
        """주어진 src/dst로 더미 ksend를 1회 송신해 파서가 받아들이는지 확인.

        반환: True = 정상 송신("Sending data via ksend..." 출력), False = 거부.
        호출자가 _input_ssh_lock을 잡고 있어야 함.
        """
        try:
            cmd = (
                f"/lge/app_ro/bin/ksend -v -s {src} -d {dst} -b \"0x00\" 2>&1"
            )
            stdin, stdout, _ = ssh.exec_command(cmd, timeout=4)
            try:
                stdin.close()
            except Exception:
                pass
            out = stdout.read().decode("utf-8", errors="replace")
            return "Sending data" in out and "empty address data" not in out
        except Exception:
            return False

    def _try_autocorrect_addr(self) -> bool:
        """현재 src/dst가 ksend에서 거부되면 bit-position form으로 변환.

        반환: 변환 적용 여부. 콜백이 등록되어 있으면 그것도 호출 (영구 저장).
        """
        with self._input_ssh_lock:
            ssh = self._get_input_ssh()
            # 1) 현재 addr 검증
            if self._ksend_test_addr(self.src_addr, self.dst_addr, ssh):
                return False  # 이미 동작
            # 2) bit-position form 후보
            new_src = self._bitmask_to_bit_position(self.src_addr) or self.src_addr
            new_dst = self._bitmask_to_bit_position(self.dst_addr) or self.dst_addr
            if new_src == self.src_addr and new_dst == self.dst_addr:
                logger.warning(
                    "MIB ksend addr rejected (src=%s dst=%s) and no bit-position fallback available. "
                    "Manual override may be required.",
                    self.src_addr, self.dst_addr,
                )
                return False
            # 3) 변환 후 검증
            if not self._ksend_test_addr(new_src, new_dst, ssh):
                logger.warning(
                    "MIB ksend addr rejected and bit-position form (src=%s dst=%s) also failed.",
                    new_src, new_dst,
                )
                return False
            # 4) 적용 + 영구 저장
            old_src, old_dst = self.src_addr, self.dst_addr
            self.src_addr = new_src
            self.dst_addr = new_dst
            logger.info(
                "MIB addr auto-corrected to bit-position form: src=%s→%s dst=%s→%s",
                old_src, new_src, old_dst, new_dst,
            )
        cb = self._on_addr_changed
        if cb is not None:
            try:
                cb(self.src_addr, self.dst_addr)
            except Exception as e:
                logger.warning("MIB on_addr_changed callback failed: %s", e)
        return True

    def _maybe_disable_screen(self, idx: int) -> None:
        """연속 실패가 임계치를 넘으면 해당 screen 인덱스를 비활성화 (이후 dump 시도 안 함)."""
        if idx in self._screen_disabled:
            return
        if self._screen_fail_count.get(idx, 0) >= self._screen_fail_threshold:
            self._screen_disabled.add(idx)
            logger.warning(
                "MIB HU screen %d auto-disabled after %d consecutive failures — "
                "this layer is likely not available on this device. Active indices now: %s",
                idx, self._screen_fail_threshold,
                [i for i in self._screen_indices if i not in self._screen_disabled] or "[fallback: 0]",
            )

    def _log_dump_diagnostics(self, ssh, idx: int, local_path: Optional[str]) -> None:
        """MIB_DEBUG_DUMP=1 일 때 PNG 검증/SCP 실패 원인을 로깅.

        디바이스 측: lmc_idx{idx}.err(LayerManagerControl stderr) 내용과 ls -la 결과.
        로컬 측: 받은 파일의 첫 16/끝 16바이트 hex (시그니처/IEND 위치 확인).
        """
        try:
            cmd = (
                f"echo '=== ls /tmp/screen_idx{idx}.png ==='; "
                f"ls -la /tmp/screen_idx{idx}.png 2>&1; "
                f"echo '=== head -c 16 hex ==='; "
                f"head -c 16 /tmp/screen_idx{idx}.png 2>/dev/null | od -An -tx1 -N16; "
                f"echo '=== tail -c 16 hex ==='; "
                f"tail -c 16 /tmp/screen_idx{idx}.png 2>/dev/null | od -An -tx1 -N16; "
                f"echo '=== lmc_idx{idx}.err ==='; "
                f"cat /tmp/lmc_idx{idx}.err 2>/dev/null || echo '(no err file)'"
            )
            stdin, stdout, _ = ssh.exec_command(cmd, timeout=5)
            try:
                stdin.close()
            except Exception:
                pass
            out = stdout.read().decode("utf-8", errors="replace")
            snippet = out.strip().replace("\r", " ").replace("\n", " | ")[:1000]
            logger.warning("MIB HU dump diag idx=%d device → %s", idx, snippet or "(empty)")
        except Exception as e:
            logger.debug("MIB HU dump diag idx=%d device probe failed: %s", idx, e)

        if local_path and os.path.exists(local_path):
            try:
                size = os.path.getsize(local_path)
                with open(local_path, "rb") as f:
                    head = f.read(16)
                    if size > 32:
                        f.seek(-16, 2)
                        tail = f.read(16)
                    else:
                        tail = b""
                logger.warning(
                    "MIB HU dump diag idx=%d local size=%d head=%s tail=%s",
                    idx, size, head.hex(" "), tail.hex(" ") if tail else "(too small)",
                )
            except Exception as e:
                logger.debug("MIB HU dump diag idx=%d local hex failed: %s", idx, e)

    def _probe_layer_info(self) -> None:
        """LayerManagerControl get screens/layers를 실행해 진단 정보를 로깅.

        실제 디바이스마다 가용 layer 인덱스가 다르므로, 사용자가 올바른 값을 설정하도록
        로그로 안내. 명령 실패는 무시 (LayerManagerControl 자체가 없을 수도 있음).
        """
        with self._ssh_lock:
            ssh = self._get_shared_ssh()
            for label, cmd in (
                ("screens", "export XDG_RUNTIME_DIR=/run/platform/weston ; LayerManagerControl get screens"),
                ("layers",  "export XDG_RUNTIME_DIR=/run/platform/weston ; LayerManagerControl get layers"),
            ):
                try:
                    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=5)
                    try:
                        stdin.close()
                    except Exception:
                        pass
                    out = stdout.read().decode("utf-8", errors="replace")
                    err = stderr.read().decode("utf-8", errors="replace")
                    snippet = (out or err).strip().replace("\r", " ").replace("\n", " | ")[:500]
                    logger.info("MIB LayerManagerControl get %s → %s", label, snippet or "(empty)")
                except Exception as e:
                    logger.debug("MIB LayerManagerControl get %s failed: %s", label, e)

    def disconnect(self) -> None:
        self._connected = False
        # 라이브 스트림(전용 채널 + 리더 스레드) 먼저 정리 — 공유 SSH를 닫기 전에.
        try:
            self.stop_live_stream()
        except Exception:
            pass
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
        # 입력 전용 세션도 정리
        with self._input_ssh_lock:
            if self._input_ssh_shell is not None:
                try:
                    self._input_ssh_shell.close()
                except Exception:
                    pass
                self._input_ssh_shell = None
            if self._input_ssh_client is not None:
                try:
                    self._input_ssh_client.close()
                except Exception:
                    pass
                self._input_ssh_client = None
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
            raise RuntimeError("MIB shared HU transport not active")
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
                logger.warning("MIB shared SSH exec failed, retrying: %s", e)
                if self._ssh_client is not None:
                    try:
                        self._ssh_client.close()
                    except Exception:
                        pass
                    self._ssh_client = None
            ssh = self._get_shared_ssh()
            _run_all(ssh, commands)

    def _ksend(self, data_bytes: str) -> None:
        """ksend 명령 1회 송신.

        기본 모드(invoke_shell): 빠르지만 stderr/exit를 알 수 없어 silent fail 가능.
        MIB_KSEND_VERBOSE=1 환경변수: exec_command 모드 + ksend -v 옵션 + 결과 로깅.
        """
        self.last_input_ts = time.monotonic()
        verbose = os.environ.get("MIB_KSEND_VERBOSE", "").strip() in ("1", "true", "yes")
        v_flag = " -v " if verbose else " "
        cmd = f'/lge/app_ro/bin/ksend{v_flag}-s {self.src_addr} -d {self.dst_addr} -b "{data_bytes}"'
        if verbose:
            self._ksend_exec_verbose(cmd)
        else:
            self._shell_run([cmd])

    def _ksend_many(self, data_list: list[str], interval_s: float = 0.1) -> None:
        """ksend 명령 여러 개를 공유 shell 채널에서 순차 송신."""
        self.last_input_ts = time.monotonic()
        verbose = os.environ.get("MIB_KSEND_VERBOSE", "").strip() in ("1", "true", "yes")
        v_flag = " -v " if verbose else " "
        cmds = [
            f'/lge/app_ro/bin/ksend{v_flag}-s {self.src_addr} -d {self.dst_addr} -b "{data}"'
            for data in data_list
        ]
        if verbose:
            for c in cmds:
                self._ksend_exec_verbose(c)
                if interval_s > 0:
                    time.sleep(interval_s)
            return
        # 각 cmd 사이 간격은 shell_run의 post_sleep_s로 들어감 — interval_s 우선
        self._shell_run(cmds, post_sleep_s=max(0.02, interval_s))

    def _ksend_exec_verbose(self, cmd: str) -> None:
        """진단 모드: exec_command로 ksend 실행하고 stderr/exit 결과를 로깅.

        성능 영향 있음 (매 명령당 SSH channel 1회). 디버깅 후 환경변수 해제 권장.
        입력 전용 SSH 세션 사용 — 캡처와 독립.
        """
        with self._input_ssh_lock:
            ssh = self._get_input_ssh()
            try:
                stdin, stdout, stderr = ssh.exec_command(cmd, timeout=5)
                try:
                    stdin.close()
                except Exception:
                    pass
                out = stdout.read().decode("utf-8", errors="replace")
                err = stderr.read().decode("utf-8", errors="replace")
                ec = stdout.channel.recv_exit_status()
                if ec != 0 or err.strip():
                    logger.warning(
                        "ksend exit=%d stderr=%r stdout=%r cmd=%r",
                        ec, err.strip()[:200], out.strip()[:200], cmd,
                    )
                else:
                    logger.info("ksend ok: %r", cmd[:160])
            except Exception as e:
                logger.warning("ksend exec failed: type=%s repr=%r cmd=%r",
                               type(e).__name__, e, cmd)

    # ------------------------------------------------------------------
    # Touch (press/drag/release) — ref RemoteController.excutecmdTouch*
    # ------------------------------------------------------------------
    def _touch_frame(self, x: int, y: int, end_byte: int) -> str:
        # MIB 터치 디지타이저 = 화면 / max(2, mult),  mult = int(res/1023)+1.
        # 실측: 폭<2046(mult≤2)은 ÷2, 폭 2240(15", mult=3)은 ÷3 (frontend 1420→screen 2160 데이터).
        #       높이<1023(mult=1)도 floor 2로 ÷2 (10.4" 878 검증). mult=1을 그대로 ÷1하면 2배 어긋남.
        # 화면좌표를 디지타이저 좌표로 변환 후 mult=1로 인코딩(참조의 ÷mult 사전클램프 버그 회피).
        dvx = max(2, self._x_mult)
        dvy = max(2, self._y_mult)
        xs = self._touch_x_scale if self._touch_x_scale is not None else (1.0 / dvx)
        ys = self._touch_y_scale if self._touch_y_scale is not None else (1.0 / dvy)
        dx = int(round(int(x) * xs)) + self._touch_x_offset
        dy = int(round(int(y) * ys)) + self._touch_y_offset
        # 디지타이저 좌표 범위로 클램프(= 화면×scale). 음수/초과 방지.
        ax = min(max(0, dx), max(1, int(self._res_x * xs)))
        ay = min(max(0, dy), max(1, int(self._res_y * ys)))
        p1, p2, p3 = _encode_touch_xy(ax, ay, 1, 1)
        return (
            f"0x83 0x50 0x20 0x0b 0x00 0x00 0x00 0x00 0x00 0xa0 0x01 0x11 "
            f"0x{p1:02x} 0x{p2:02x} 0x{p3:02x} 0x{end_byte:02x}"
        )

    def set_touch_offsets(self, x_offset: int = 0, y_offset: int = 0) -> None:
        """터치 좌표 보정 오프셋 설정 (디바이스 좌표 px). 라이브 캘리브레이션용."""
        try:
            self._touch_x_offset = int(x_offset)
        except Exception:
            self._touch_x_offset = 0
        try:
            self._touch_y_offset = int(y_offset)
        except Exception:
            self._touch_y_offset = 0
        logger.info("MIB touch offsets set: x=%d y=%d",
                    self._touch_x_offset, self._touch_y_offset)

    def get_touch_offsets(self) -> tuple[int, int]:
        return (self._touch_x_offset, self._touch_y_offset)

    def set_touch_scale(self, x_scale=None, y_scale=None) -> None:
        """터치 디지타이저 절대 스케일 override (디바이스별 라이브 캘리브레이션용).

        None/빈값/0이하 = 기본(축별 1/max(2,mult), 보통 0.5) 사용.
        일부 패널은 디지타이저 좌표공간이 화면의 1/2이 아니다. 예: 13.1" 1920x1080은
        Y 디지타이저가 화면의 ~1/4이라 기본 ÷2(화면×0.5)로 보내면 터치가 Y로 2배 늘어남
        → y_scale=0.25 로 보정. 해상도 공식으로는 도출 불가한 패널 펌웨어 고유값이라
        디바이스 info(touch_x_scale/touch_y_scale)에 저장해 디바이스별로만 적용한다.
        """
        def _f(v):
            if v is None or v == "":
                return None
            try:
                fv = float(v)
            except Exception:
                return None
            return fv if fv > 0 else None
        self._touch_x_scale = _f(x_scale)
        self._touch_y_scale = _f(y_scale)
        logger.info("MIB touch scale set: x=%s y=%s",
                    self._touch_x_scale, self._touch_y_scale)

    def get_touch_scale(self) -> tuple:
        return (self._touch_x_scale, self._touch_y_scale)

    def _touch_press(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFD))

    def _touch_drag(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFE))

    def _touch_release(self, x: int, y: int) -> None:
        self._ksend(self._touch_frame(x, y, 0xFF))

    def tap(self, x: int, y: int, screen_type: str = "HU",
            dp: float = 0.2, dr: float = 0.0) -> None:
        """단일 탭. press → (dp초 대기) → release.

        진단: MIB_TOUCH_DST_SWEEP=1 이면 1회 탭이 후보 dst(1-63)를 차례로 훑는
        sweep으로 동작한다. 비표준 ksend variant에서 터치 입력 핸들러의 KIPC id를
        모를 때, 알려진 버튼 위를 한 번 탭하고 화면이 반응하는 순간의 dst를 로그에서
        찾기 위함. (정상 동작 시에는 이 환경변수를 끌 것)
        """
        if os.environ.get("MIB_TOUCH_DST_SWEEP", "").strip() in ("1", "true", "yes"):
            self.sweep_touch_dst(x, y)
            return
        self._touch_press(x, y)
        if dp > 0:
            time.sleep(dp)
        self._touch_release(x, y)
        if dr > 0:
            time.sleep(dr)

    def sweep_touch_dst(self, x: int, y: int, dwell_s: Optional[float] = None,
                        candidates: Optional[list[str]] = None) -> None:
        """진단용 터치 dst 스윕 — 동일 좌표에 후보 dst마다 1회씩 탭을 보낸다.

        사용법: MIB_TOUCH_DST_SWEEP=1 로 백엔드를 띄우고, 화면에서 '눌리면 확실히
        바뀌는' 버튼(예: 메뉴 진입) 위를 한 번 탭한다. 그러면 dst=1..63 을 순서대로
        시도하며 각 시도를 WARNING 로그로 남기므로, 화면이 반응한 시점의 dst를 찾을 수 있다.
        찾은 값은 dev.info 의 ksend_dst 로 고정하면 된다.

        dwell_s: 각 dst 사이 대기(초). 기본 1.2s (MIB_TOUCH_DST_SWEEP_DWELL 로 조정).
        """
        if dwell_s is None:
            try:
                dwell_s = float(os.environ.get("MIB_TOUCH_DST_SWEEP_DWELL", "1.2") or 1.2)
            except Exception:
                dwell_s = 1.2
        if candidates is None:
            candidates = [str(i) for i in range(1, 64)]
        saved_dst = self.dst_addr
        logger.warning(
            "MIB TOUCH DST SWEEP start: (%d,%d) candidates=%d dwell=%.1fs "
            "— watch the screen; note the dst when it reacts",
            x, y, len(candidates), dwell_s,
        )
        try:
            for d in candidates:
                self.dst_addr = d
                logger.warning("MIB TOUCH DST SWEEP: dst=%s  tap(%d,%d)", d, x, y)
                self._touch_press(x, y)
                time.sleep(0.15)
                self._touch_release(x, y)
                time.sleep(max(0.2, dwell_s))
        finally:
            self.dst_addr = saved_dst
            logger.warning("MIB TOUCH DST SWEEP done — dst restored to %s", saved_dst)

    def long_press(self, x: int, y: int, duration_ms: int = 3000,
                   screen_type: str = "HU") -> None:
        self._touch_press(x, y)
        time.sleep(duration_ms / 1000.0)
        self._touch_release(x, y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              screen_type: str = "HU", duration_ms: int = 300,
              hold_ms: int = 0) -> None:
        """press(x1,y1) → [hold_ms 누름 유지] → drag(보간) → release(x2,y2).

        hold_ms>0이면 드래그앤드롭(앱카드 이동) — 시작점을 hold_ms 동안 눌러 항목을
        "집어 올린" 뒤 드래그한다.
        """
        # 보간 스텝 수: duration 기반 (각 스텝 ~20ms 목표, 최소 3 최대 20)
        target_interval_ms = 20
        steps = max(3, min(20, max(1, duration_ms // target_interval_ms)))
        dx = (x2 - x1) / steps
        dy = (y2 - y1) / steps

        # press 프레임 — hold_ms가 있으면 먼저 눌러서 유지한 뒤 drag 프레임을 보낸다.
        press = self._touch_frame(x1, y1, 0xFD)
        drag_frames: list[str] = []
        for i in range(1, steps):
            ix = int(round(x1 + dx * i))
            iy = int(round(y1 + dy * i))
            drag_frames.append(self._touch_frame(ix, iy, 0xFE))  # drag
        drag_frames.append(self._touch_frame(x2, y2, 0xFF))  # release

        if hold_ms and hold_ms > 0:
            self._ksend_many([press], interval_s=0)
            time.sleep(hold_ms / 1000.0)
            interval_s = max(0.01, (duration_ms / 1000.0) / max(1, len(drag_frames)))
            self._ksend_many(drag_frames, interval_s=interval_s)
            return

        # 동일 SSH 세션으로 일괄 송신 — 오버헤드 최소화
        frames = [press, *drag_frames]
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

    def _hkey_long_frame(self, key_code: int, state: int, category: int = 0x30) -> str:
        """Long 클래스(Home/Power) — 15 bytes.

        byte 9: category (0x30 = power/home)
        byte 10: key_code
        byte 11: state (press=0x01, release=0x00)
        byte 12: tail — 0x10(press) / 0xD9(release)
        """
        tail = 0x10 if state else 0xD9
        return (
            f"0x83 0x50 0x20 0x0B 0x17 0xF8 0xF1 0x73 0x00 0x{category:02X} "
            f"0x{key_code:02X} 0x{state:02X} 0x{tail:02X} 0x00 0x00"
        )

    def _hkey_volume_frame(self, delta: int, press: bool) -> str:
        """Volume 전용 frame — 15 bytes (CAN: arb_id=0x17F8F173, data=06 48 01 ...).

        byte 9: 0x48 (volume category)
        byte 10: 0x01 (sub-id, 고정)
        byte 11: press 시 delta(+1/-1), release 시 0x00
        byte 12: press 시 0x00, release 시 delta(+1/-1) ← byte 11과 swap
        byte 13: 0x82 (volume tail)

        delta: +1 (UP, 0x01) / -1 (DOWN, 0xFF signed)
        """
        d = delta & 0xFF  # signed → unsigned byte
        if press:
            b11, b12 = d, 0x00
        else:
            b11, b12 = 0x00, d
        return (
            f"0x83 0x50 0x20 0x0B 0x17 0xF8 0xF1 0x73 0x00 0x48 0x01 "
            f"0x{b11:02X} 0x{b12:02X} 0x82 0x00"
        )

    def resolve_key(self, key_name: str) -> Optional[dict]:
        """키 스펙 반환 (override 병합).

        반환 dict 필드: class("short"|"long"), key(int), category(int, long 전용)
        """
        base = MIB_KEYS.get(key_name)
        if not base:
            return None
        merged = dict(base)
        ov = self._key_overrides.get(key_name) or {}
        for k in ("class", "key", "category"):
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

        MIB는 press→release 시퀀스가 기본. LONG은 press→대기→release 패턴으로 처리.
        market별 추가 동작(POWER 메시지 등)도 함께 처리.
        hold_ms: LONG_KEY일 때 press↔release 사이 hold 시간(ms). None이면 기본 1000ms.
        """
        info = self.resolve_key(key_name)
        if not info:
            raise ValueError(f"Unknown MIB key: {key_name}")
        key_code = int(info["key"])
        klass = info.get("class", "short")
        # long frame의 category(byte 9): HOME/POWER=0x30
        category = int(info.get("category", 0x30))

        # frame 빌드: class별 분기
        #   short: 13B (MUTE) — release 시 key=0x00, state=0x00
        #   long:  15B (HOME/POWER) — key_code 유지, state 0x00 + tail 변경
        #   volume: 15B (VOLUME_UP/DOWN) — signed delta가 byte11/byte12 swap
        if klass == "short":
            press = self._hkey_short_frame(key_code, 0x01)
            release = self._hkey_short_frame(0x00, 0x00)
        elif klass == "volume":
            press = self._hkey_volume_frame(key_code, press=True)
            release = self._hkey_volume_frame(key_code, press=False)
        else:  # long
            press = self._hkey_long_frame(key_code, 0x01, category)
            release = self._hkey_long_frame(key_code, 0x00, category)

        if sub_cmd == LONG_KEY:
            hold_s = max(0.05, (hold_ms / 1000.0)) if hold_ms is not None else 1.0
        else:
            hold_s = 0.1
        self._ksend_many([press], interval_s=0)
        time.sleep(hold_s)
        self._ksend_many([release], interval_s=0)

        # market별 추가 동작 분기
        if key_name == "POWER":
            # POWER 전용 추가 커맨드 (ref ABTpower: command03~05)
            # HU의 power state 전환을 위한 별도 주소(src2/dst2) 메시지
            # market: EU/NAR/CN은 legacy hex addr, else는 bit-position form
            self._ksend_power_extra()
            logger.debug(
                "MIB send_key_by_name(POWER) complete [market=%s src=%s dst=%s]",
                self.market, self.src_addr, self.dst_addr
            )


    def _ksend_power_extra(self) -> None:
        """ABTpower의 command03~05에 해당하는 추가 ksend 송신.
        market에 따라 src2/dst2 주소가 다름 (ref RemoteController.ABTpower).

        EU/NAR/CN (legacy): hex bitmask (0x40000000000 = bit42, 0x8000000000000000 = bit63)
        GP/KR (bit-position): decimal bit position (42, 63)

        주의: 디바이스 ksend variant가 bit-position form만 받는 경우(_try_autocorrect_addr이
        self.src_addr을 변환한 경우), src2/dst2도 동일하게 bit-position으로 자동 변환.
        EU 모드의 hex 값은 의미상 bit-position 42/63과 동일하므로 변환은 안전.
        """
        # market 기반 기본값
        if self.market in ("EU", "NAR", "CN"):
            src2 = "0x40000000000"      # = 1 << 42
            dst2 = "0x8000000000000000"  # = 1 << 63
            addr_form = "legacy_hex"
        else:
            # GP/KR: bit-position form (42, 63 = src/dst bit positions)
            src2 = "42"
            dst2 = "63"
            addr_form = "bit_position"

        # ksend variant가 bit-position form만 받는 경우 자동 변환.
        # self.src_addr이 decimal 문자열(0-63)이면 이 디바이스의 ksend는 bit-position variant.
        # 그럼 src2/dst2도 bit-position form으로 강제 변환해야 함 (EU hex는 거부됨).
        if addr_form == "legacy_hex" and self._is_bit_position_form(self.src_addr):
            new_src2 = self._bitmask_to_bit_position(src2) or src2
            new_dst2 = self._bitmask_to_bit_position(dst2) or dst2
            logger.info(
                "MIB _ksend_power_extra: device uses bit-position ksend variant "
                "(self.src=%s), auto-converting src2/dst2: %s→%s, %s→%s",
                self.src_addr, src2, new_src2, dst2, new_dst2,
            )
            src2 = new_src2
            dst2 = new_dst2
            addr_form = "legacy_hex_auto→bit_position"

        payloads = [
            "0x01 0x91 0xF0 0x01 0x4C 0x00 0x00",  # command03
            "0x01 0x91 0xF0 0x02 0x38 0x00 0x00",  # command04 (key code 0x38 = POWER)
            "0x01 0x91 0xF0 0x01 0x01 0x00 0x00",  # command05
        ]
        cmds = [
            f'/lge/app_ro/bin/ksend -s {src2} -d {dst2} -b "{p}"'
            for p in payloads
        ]
        logger.debug(
            "MIB _ksend_power_extra: market=%s addr_form=%s src2=%s dst2=%s payloads=%d",
            self.market, addr_form, src2, dst2, len(payloads)
        )
        self._shell_run(cmds, post_sleep_s=0.1)

    @staticmethod
    def _is_bit_position_form(addr: str) -> bool:
        """addr이 bit-position decimal form인지 확인 (0-63 범위 정수 문자열)."""
        try:
            v = int(addr)
            return 0 <= v <= 63
        except (ValueError, TypeError):
            return False

    def send_key(self, cmd: int, sub_cmd: int, key_data: int,
                 monitor: int = 0x00, direction: Optional[int] = None,
                 hold_ms: Optional[int] = None) -> None:
        """HKMC 호환용 raw send_key. key_data를 KEY_CODE로 해석해 single press/release 수행.

        MIB는 cmd 분류가 하나라, 별도 분기 없이 short 프레임을 기본으로 사용.
        long class가 필요하면 key_data 범위로 자동 판별 (POWER=0x38, HOME=0x66).
        hold_ms: LONG_KEY일 때 press↔release 사이 hold 시간(ms). None이면 기본 1000ms.

        주의: 이 경로는 market별 추가 메시지(POWER)를 송신하지 않음.
        POWER를 사용할 때는 send_key_by_name("POWER")을 권장.
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

        key_name_hint = {0x10: "VOLUME_UP", 0x11: "VOLUME_DOWN", 0x20: "MUTE",
                         0x38: "POWER", 0x66: "HOME"}.get(key_data, f"0x{key_data:02X}")
        logger.debug(
            "MIB send_key(raw): key_data=0x%02X (%s) class=%s hold_s=%.1f [market=%s]",
            key_data, key_name_hint, klass, hold_s, self.market
        )

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
    def _screencap_hu(self, fmt: str = "png") -> bytes:
        import tempfile
        import os
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True

        # HU sshd는 SFTP 서브시스템 미지원 → SCP(paramiko-scp)로 pull.
        try:
            from scp import SCPClient
        except ImportError as e:
            raise RuntimeError("scp module required: pip install scp") from e

        # 진단/완화 토글 (환경변수)
        # - MIB_DEBUG_TIMING=1 : dump/wait/scp/compose 단계별 소요시간 로깅
        # - MIB_DEBUG_DUMP=1   : PNG 검증 실패 시 lmc_idx{idx}.err / 원격 ls -la /
        #   로컬 파일 헤더·테일 hex를 로깅 (corrupt 원인 진단).
        # - MIB_DUMP_TIMEOUT_S : exec_command stdout 폴링 타임아웃(초). 기본 8.
        #   터치 직후 weston 리페인트와 dump 경합으로 hang 시 stall 길이를 제한.
        # - MIB_CROP_TO_REGISTERED=1 : 캡처 PNG를 사용자 등록 해상도로 top-left crop,
        #   _maybe_autoupdate_resolution 비활성화. surface 버퍼 > visible 인 환경
        #   (예: 13.1" 1920x1080 등록인데 dump가 1920x1280로 떨어지는 케이스)에서
        #   하단 검정 padding 영역을 제거.
        debug_timing = os.environ.get("MIB_DEBUG_TIMING", "").strip() in ("1", "true", "yes")
        debug_dump = os.environ.get("MIB_DEBUG_DUMP", "").strip() in ("1", "true", "yes")
        try:
            dump_timeout = float(os.environ.get("MIB_DUMP_TIMEOUT_S", "8") or 8)
        except Exception:
            dump_timeout = 8.0
        # 기본값 ON: 등록 해상도(= 디바이스 실제 화면/터치 좌표 공간)를 권위로 삼아 dump 버퍼의
        # 우/하단 black margin을 잘라낸다. LayerManagerControl dump는 compositor가 할당한 layer
        # 버퍼 전체(예: 1560x878)를 뜨는데, 실제 렌더된 UI viewport(예: 1560x700)보다 커서 우/하단에
        # 미사용 black 영역이 남는다. 이 버퍼 크기로 해상도를 자동 갱신하면 프론트 좌표 매핑이 어긋나
        # 터치가 틀어진다(regression 4dd181c). 따라서 등록 해상도를 신뢰하고 자동 갱신은 하지 않는 게
        # 기본. 자동 해상도 감지로 회귀하려면 MIB_CROP_TO_REGISTERED=0.
        _crop_env = os.environ.get("MIB_CROP_TO_REGISTERED", "").strip().lower()
        crop_to_registered = _crop_env not in ("0", "false", "no")
        registered_w, registered_h = self._res_x, self._res_y

        def _phase_log(label: str, t0: float) -> None:
            if debug_timing:
                logger.info("MIB cap.%s: %.0fms", label, (time.monotonic() - t0) * 1000)

        tmp_dir = tempfile.mkdtemp(prefix="mib_cap_")
        try:
            # 공유 SSH 세션에서 dump + SCP pull 을 일괄 수행 (매 프레임마다 재인증 방지).
            # - 활성화된 screen 인덱스만 dump 시도 (디바이스마다 가용 layer 다름)
            # - 각 dump를 ;로 분리: 한쪽 실패가 다른쪽을 막지 않음
            # - SCPClient 하나로 모든 파일 연속 get (subsystem 1회)
            def _do_capture(ssh) -> list[str]:
                active_indices = [i for i in self._screen_indices if i not in self._screen_disabled]
                if not active_indices:
                    # 모든 인덱스가 비활성화됨 — 안전 fallback: screen 0만 다시 시도
                    active_indices = [0]
                # 인덱스 → 로컬 파일명 매핑 (사람 가독 위해 1-base)
                file_map = [(idx, f"screen_idx{idx}.png") for idx in active_indices]
                # 매 프레임 시작 시 stale 파일을 제거해야 SCP가 이전 프레임의 partial 파일을 가져오지 않음.
                # LayerManagerControl dump는 IVI 그래픽 파이프라인을 통해 비동기로 PNG를 쓰는 구현체가
                # 있어 exec_command가 끝나도 파일이 완성 전일 수 있음 → rm + dump + sync 순으로 처리.
                rm_parts = [f"rm -f /tmp/{fname}" for _, fname in file_map]
                dump_parts = [
                    f"LayerManagerControl dump screen {idx} to /tmp/{fname} 2>/tmp/lmc_idx{idx}.err"
                    for idx, fname in file_map
                ]
                dump_cmd = (
                    "export XDG_RUNTIME_DIR=/run/platform/weston ; "
                    + " ; ".join(rm_parts)
                    + " ; "
                    + " ; ".join(dump_parts)
                    + " ; sync"
                )
                t_dump = time.monotonic()
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
                            logger.warning("MIB HU dump timeout %.1fs — abandoning cycle", dump_timeout)
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
                    logger.warning("MIB HU dump exit=%d stderr=%r — skipping SCP, will reset SSH",
                                   exit_status, snippet)
                _phase_log("dump", t_dump)

                # dump 자체가 실패/타임아웃이면 SCP 단계는 skip — 죽은 dump 뒤의 SCP 시도가
                # 추가 stall을 누적시키고, partial 파일을 가져와 _screen_fail_count를 잘못
                # 증가시키는 부작용 방지. SSH 채널이 wedged 상태일 가능성이 높아 호출자가 리셋하도록 신호.
                if dump_failed:
                    raise RuntimeError(f"MIB HU dump failed (exit={exit_status})")

                # LayerManagerControl이 비동기 처리하는 경우 dump_cmd 종료 후에도 파일 쓰기가 진행 중일 수 있음.
                # 1차로 크기 안정화 폴링, 2차로 IEND(PNG 완성) 폴링. 디바이스 파일은 완전한데
                # writer가 1024-block 단위로 append 중인 순간에 SCP하면 정확히 1024 배수로 잘린
                # partial을 가져오는 증상이 있어, IEND가 보일 때까지 기다린 뒤 SCP한다.
                t_wait = time.monotonic()
                self._wait_remote_files_stable(ssh, [(idx, f"/tmp/{fname}") for idx, fname in file_map])
                self._wait_remote_png_complete(ssh, [f"/tmp/{fname}" for _, fname in file_map])
                _phase_log("wait_stable", t_wait)

                files: list[str] = []
                t_scp = time.monotonic()
                try:
                    with SCPClient(ssh.get_transport()) as scp:
                        for idx, fname in file_map:
                            remote = f"/tmp/{fname}"
                            local = os.path.join(tmp_dir, fname)
                            try:
                                scp.get(remote, local)
                                ok = False
                                if os.path.exists(local) and os.path.getsize(local) > 0:
                                    # PNG 무결성 1차 검증 — 시그니처 + IEND chunk 존재 여부
                                    # 완전한 PNG(IEND 존재)면 그대로, 아니면 truncated여도 PIL이
                                    # 디코딩 가능하면 부분 이미지로 수용한다. 디바이스 dump가 PNG를
                                    # 끝까지 안 써주는 환경에서 캡처를 통째로 버리지 않기 위함.
                                    usable = _validate_png_file(local) or _png_partially_decodable(local)
                                    if usable:
                                        files.append(local)
                                        self._screen_fail_count[idx] = 0
                                        ok = True
                                        # truncated 수용 시에는 원인 추적용으로 1회 안내 (성공 카운트는 유지).
                                        if not _validate_png_file(local):
                                            logger.warning(
                                                "MIB HU scp %s: PNG truncated but decodable, using partial "
                                                "(size=%d; device dump likely not writing full PNG — check /tmp space)",
                                                remote, os.path.getsize(local),
                                            )
                                    else:
                                        logger.warning(
                                            "MIB HU scp %s: PNG corrupt/undecodable (size=%d)",
                                            remote, os.path.getsize(local),
                                        )
                                    # 디코딩 성공 여부와 무관하게 IHDR(파일 앞부분)은 대개 온전 →
                                    # 실제 해상도를 추출해 터치 좌표 스케일링(_x_mult/_y_mult)을 보정.
                                    # crop 모드에서는 등록 해상도 우선이므로 자동 갱신 skip.
                                    if not crop_to_registered:
                                        dims = _read_png_ihdr_size(local)
                                        if dims:
                                            try:
                                                self._maybe_autoupdate_resolution(dims[0], dims[1])
                                            except Exception as _re:
                                                logger.debug(
                                                    "MIB IHDR resolution auto-correct skipped: %s", _re
                                                )
                                    if not usable and debug_dump:
                                        self._log_dump_diagnostics(ssh, idx, local)
                                if not ok:
                                    self._screen_fail_count[idx] = self._screen_fail_count.get(idx, 0) + 1
                                    self._maybe_disable_screen(idx)
                            except Exception as ee:
                                self._screen_fail_count[idx] = self._screen_fail_count.get(idx, 0) + 1
                                # 임계치 도달 직전까지만 warn — 그 후엔 auto-disable되어 시도 안 함
                                if self._screen_fail_count[idx] <= self._screen_fail_threshold:
                                    logger.warning(
                                        "MIB HU scp %s failed (%d/%d): type=%s repr=%r",
                                        remote, self._screen_fail_count[idx],
                                        self._screen_fail_threshold,
                                        type(ee).__name__, ee,
                                    )
                                if debug_dump:
                                    self._log_dump_diagnostics(ssh, idx, local if os.path.exists(local) else None)
                                self._maybe_disable_screen(idx)
                except Exception as ee:
                    logger.warning(
                        "MIB HU SCPClient failed: type=%s repr=%r",
                        type(ee).__name__, ee, exc_info=True,
                    )
                _phase_log("scp", t_scp)
                return files

            local_files: list[str] = []
            with self._ssh_lock:
                try:
                    ssh = self._get_shared_ssh()
                    local_files = _do_capture(ssh)
                except Exception as e:
                    logger.warning(
                        "MIB HU capture failed on shared SSH, retrying: type=%s repr=%r",
                        type(e).__name__, e,
                    )
                    if self._ssh_client is not None:
                        try:
                            self._ssh_client.close()
                        except Exception:
                            pass
                        self._ssh_client = None
                    ssh = self._get_shared_ssh()
                    local_files = _do_capture(ssh)

            if not local_files:
                raise RuntimeError(
                    f"No HU screenshot captured (LayerManagerControl dump may have failed; "
                    f"check 'MIB HU dump' / 'MIB HU scp' / 'MIB HU SCPClient' warnings above)"
                )

            # _validate_png_file이 1차로 거르지만, IDAT 내부 손상은 .convert에서야 드러남.
            # 손상된 파일은 무시하고 정상 파일만 사용. 모두 실패 시 RuntimeError로 외부 재시도.
            t_compose = time.monotonic()
            images: list[Image.Image] = []
            corrupt_paths: list[tuple[str, int, str]] = []
            for p in local_files:
                try:
                    img = Image.open(p)
                    img = img.convert("RGBA")
                    images.append(img)
                except Exception as ie:
                    sz = os.path.getsize(p) if os.path.exists(p) else -1
                    corrupt_paths.append((p, sz, f"{type(ie).__name__}: {ie!r}"))
            if not images:
                raise RuntimeError(
                    f"PIL Image.open failed for all captures (likely truncated PNG): {corrupt_paths}"
                )
            if corrupt_paths:
                logger.debug("MIB HU partial composite — corrupt skipped: %s", corrupt_paths)
            base = images[0]
            for over in images[1:]:
                if over.size != base.size:
                    over = over.resize(base.size)
                base = Image.alpha_composite(base, over)
            # 등록 해상도 crop 모드: surface 버퍼가 visible content 보다 큰 디바이스(13.1" 등)
            # 에서 하단/우측에 들어가는 미사용 padding 영역을 제거. 등록 해상도 보다 작으면 그대로.
            if crop_to_registered and registered_w > 0 and registered_h > 0:
                cw = min(int(base.size[0]), int(registered_w))
                ch = min(int(base.size[1]), int(registered_h))
                if (cw, ch) != base.size:
                    base = base.crop((0, 0, cw, ch))
            else:
                # PNG 실제 크기 == 디바이스 실제 화면 해상도. 사용자가 잘못 입력한 경우 자동 보정.
                # _x_mult/_y_mult가 어긋나면 터치 좌표 인코딩이 깨지므로 캡처가 들어올 때마다 점검.
                try:
                    self._maybe_autoupdate_resolution(int(base.size[0]), int(base.size[1]))
                except Exception as e:
                    logger.debug("MIB resolution auto-correct skipped: %s", e)
            _phase_log("compose", t_compose)
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
            raise RuntimeError("MIB IID/HUD capture: private_server_ip not configured")

        tmp_dir = tempfile.mkdtemp(prefix="mib_iid_")
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
                logger.warning("MIB IID/HUD capture via direct-tcpip failed, retrying: %s", e)
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
        marker = "__MIB_FILE_OK__"
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
            logger.debug("MIB shell send failed: %s", e)
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
    # 라이브 미러 스트리밍 (surface 합성 → device python 다운스케일 → JPEG)
    # ------------------------------------------------------------------
    def is_live_running(self) -> bool:
        t = self._live_thread
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
            script = (_MIB_LIVE_STREAMER
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
            logger.info("MIB live stream started (%dx%d, q=%d)",
                        self._live_w, self._live_h, self._live_jpeg_q)
            return True
        except Exception as e:
            logger.warning("MIB live stream start failed: %r", e)
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

        프레임 형식: b"MIBF" + struct('<HHhhHH', tw, th, mx, my, mw, mh)
                     + HMI(tw*th*3 BGR) + MAP(mw*mh*3 BGR)
        합성: HMI를 베이스로, 맵-사각형 안에서 HMI가 (거의) 검정인 곳(=투명 구멍)에만 맵을 표시.
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
                # 스트리머 stderr(scene 판정 진단: hmi/cands 등)를 로그로 회수
                while chan.recv_stderr_ready():
                    err = chan.recv_stderr(4096)
                    if err:
                        logger.info("MIB streamer: %s",
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
                idx = buf.find(b"MIBF")
                if idx < 0:
                    if len(buf) > (1 << 23):
                        buf = buf[-8:]
                    break
                if len(buf) < idx + HDR:
                    break
                tw, th, mx, my, mw, mh, sw, sh = struct.unpack("<HHhhHHHH", buf[idx + 4: idx + HDR])
                # 실제 화면 해상도로 등록 해상도 1회 보정 — 프론트 터치 좌표 매핑(deviceRes)이
                # 라이브 이미지가 나타내는 좌표 공간과 일치하도록(인치별 자동).
                if not self._live_res_synced and sw > 0 and sh > 0:
                    self._live_res_synced = True
                    try:
                        if self._maybe_autoupdate_resolution(sw, sh):
                            logger.info("MIB live: 등록 해상도 보정 → %dx%d", sw, sh)
                    except Exception as e:
                        logger.debug("MIB live resolution sync failed: %s", e)
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
                            # 단 맵이 프레임 전체를 덮는 backdrop 모델(MQB 등)은 불투명 크롬이
                            # 섞여 비율이 낮아지므로 device select_map과 같은 0.55로 완화.
                            gate = self._map_hole_gate
                            if mw * mh >= tw * th * 0.9:
                                gate = min(gate, 0.55)
                            if hole.mean() > gate:
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
        logger.info("MIB live reader exited")

    def get_live_frame(self) -> tuple[Optional[bytes], int]:
        """(최신 JPEG bytes, frame_id). 아직 없으면 (None, 0)."""
        with self._live_lock:
            return self._latest_live_jpeg, self._live_frame_id

    def stop_live_stream(self) -> None:
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
                          screen_type: str = "HU", duration_ms: int = 300,
                          hold_ms: int = 0) -> None:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, self.swipe, x1, y1, x2, y2, screen_type, duration_ms, hold_ms
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
