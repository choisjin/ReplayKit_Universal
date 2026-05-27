#!/usr/bin/env python3
"""LinControl 좌표/캡처 어긋남 진단 스크립트.

사용법:
    ./python/bin/python3 scripts/diag_lincontrol.py
    # 또는 특정 윈도우 ID 지정:
    ./python/bin/python3 scripts/diag_lincontrol.py --hwnd 0x4400003

출력에 포함되는 정보:
  - 세션 타입 (X11 / Wayland / XWayland)
  - 모니터 구성 (xrandr / mss)
  - 윈도우 매니저
  - 대상 윈도우의 LinControlService 측 geometry / frame_extents
  - 같은 윈도우에 대한 xdotool / wmctrl 의 geometry (있을 경우)
  - client_to_screen 좌표 변환 결과 (중앙 픽셀 기준)
  - 캡처 결과 비트맵 크기 및 첫 픽셀

어긋남이 있는 부분을 찾기 위한 진단 — 이 출력을 그대로 공유하면 됩니다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# project root 를 sys.path 에 추가 — scripts/ 에서 직접 실행 가능하게
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))


def hdr(s: str) -> None:
    print()
    print("=" * 70)
    print(s)
    print("=" * 70)


def run(cmd: list[str]) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"<failed: {e}>"


def main() -> int:
    target_hwnd: int | None = None
    if len(sys.argv) >= 3 and sys.argv[1] == "--hwnd":
        try:
            target_hwnd = int(sys.argv[2], 0)
        except ValueError:
            print("invalid --hwnd value:", sys.argv[2])
            return 2

    hdr("Session / Display")
    print("XDG_SESSION_TYPE  :", os.environ.get("XDG_SESSION_TYPE", "<unset>"))
    print("DISPLAY           :", os.environ.get("DISPLAY", "<unset>"))
    print("WAYLAND_DISPLAY   :", os.environ.get("WAYLAND_DISPLAY", "<unset>"))
    print("XDG_CURRENT_DESKTOP:", os.environ.get("XDG_CURRENT_DESKTOP", "<unset>"))
    print("GDK_SCALE         :", os.environ.get("GDK_SCALE", "<unset>"))
    print("GDK_DPI_SCALE     :", os.environ.get("GDK_DPI_SCALE", "<unset>"))
    print("QT_SCALE_FACTOR   :", os.environ.get("QT_SCALE_FACTOR", "<unset>"))

    hdr("Monitors / DPI (xrandr / xdpyinfo)")
    if shutil.which("xrandr"):
        print(run(["xrandr", "--listmonitors"]))
    else:
        print("xrandr: not installed")
    if shutil.which("xdpyinfo"):
        out = run(["xdpyinfo"])
        for line in out.splitlines():
            if "dimensions" in line or "resolution" in line:
                print(line.strip())
    else:
        print("xdpyinfo: not installed")

    hdr("Window manager")
    if shutil.which("wmctrl"):
        print(run(["wmctrl", "-m"]))
    else:
        print("wmctrl: not installed (apt install wmctrl 권장 — 비교용)")

    hdr("LinControlService 가용성")
    try:
        from backend.app.services.lincontrol_service import LinControlService
    except Exception as e:
        print("LinControlService import FAILED:", e)
        return 1
    svc = LinControlService()
    print("available    :", svc.is_available())
    print("import_error :", svc.import_error())
    if not svc.is_available():
        return 1

    hdr("윈도우 목록 (LinControlService.list_processes)")
    procs = svc.list_processes()
    for i, p in enumerate(procs):
        marker = "  "
        if target_hwnd is not None and int(p["hwnd"]) == target_hwnd:
            marker = "→ "
        print(f"{marker}[{i:2}] hwnd=0x{p['hwnd']:08x} pid={p['pid']:>6} "
              f"{p['width']:>5}x{p['height']:<5} {(p['name'] or '?'):<20} "
              f"class={p['class_name']!r} title={p['title']!r}")
    if not procs:
        print("  (no windows — Wayland 세션이거나 X11 미연결)")
        return 1

    # 대상 선택 — 사용자 지정 우선, 없으면 첫 번째
    target = None
    if target_hwnd is not None:
        for p in procs:
            if int(p["hwnd"]) == target_hwnd:
                target = p
                break
        if target is None:
            print(f"\n--hwnd 0x{target_hwnd:x} 일치하는 윈도우 없음. 첫 번째 사용.")
            target = procs[0]
    else:
        target = procs[0]
    print(f"\n[대상] hwnd=0x{target['hwnd']:08x} title={target['title']!r}")

    hdr("LinControlService.attach + status")
    info = svc.attach(int(target["hwnd"]))
    for k, v in info.items():
        print(f"  {k:<20}: {v!r}")

    hdr("좌표/Frame 상세")
    hwnd = int(target["hwnd"])
    print("get_window_size      :", svc.get_window_size(), "  ← visible (GTK 그림자 제외)")
    if hasattr(svc, "_get_raw_window_size"):
        print("_get_raw_window_size :", svc._get_raw_window_size(), "  ← X 가 보는 raw 크기 (그림자 포함)")
    print("get_outer_size       :", svc.get_outer_size())
    print("get_client_offset    :", svc.get_client_offset())
    print("frame_extents (WM)   :", svc._get_frame_extents(hwnd), "  ← WM frame 두께")
    print("gtk_frame_extents    :", svc._get_gtk_frame_extents(hwnd), "  ← GTK CSD 그림자 (raw 크기에서 차감)")
    print("window_root_pos (raw):", svc._get_window_root_pos(hwnd))

    # 현재 활성 윈도우와 대상 일치 여부
    try:
        active = svc._get_active_window()
        match = (active == hwnd) if active else None
        print(f"_NET_ACTIVE_WINDOW   : 0x{active:08x} (대상과 일치={match})" if active
              else "_NET_ACTIVE_WINDOW   : <none>")
    except Exception as e:
        print("_NET_ACTIVE_WINDOW   :", "error:", e)

    hdr("외부 도구 비교")
    if shutil.which("xdotool"):
        print("--- xdotool getwindowgeometry ---")
        print(run(["xdotool", "getwindowgeometry", str(hwnd)]))
        print("--- xdotool getactivewindow ---")
        print(run(["xdotool", "getactivewindow"]))
    else:
        print("xdotool: not installed")
    if shutil.which("wmctrl"):
        print("\n--- wmctrl -lG (그리드 형식 — 매칭 윈도우만) ---")
        for line in run(["wmctrl", "-lG"]).splitlines():
            if f"0x{hwnd:08x}" in line.lower() or f"0x{hwnd:07x}" in line.lower():
                print(line)

    hdr("client_to_screen 좌표 변환 (중앙 픽셀 기준)")
    w, h = svc.get_window_size()
    if w > 0 and h > 0:
        cx, cy = w // 2, h // 2
        sx, sy = svc._client_to_screen(cx, cy)
        print(f"client (cx, cy)  = ({cx}, {cy})  ← '윈도우 중앙' 의도")
        print(f"screen (sx, sy)  = ({sx}, {sy})  ← XTest 가 이 root 좌표로 가서 클릭")
        print()
        print("[검증] 위 (sx, sy) 가 정말 윈도우 중앙인지 xdotool 로 확인:")
        print(f"  xdotool mousemove {sx} {sy}")
        print("  → 마우스가 대상 윈도우 정중앙으로 가야 정상.")
        print("  → 어긋나면 frame_extents 또는 DPI scaling 문제 의심.")
    else:
        print("get_window_size 가 0 — 윈도우가 invalidated.")

    hdr("캡처 결과 — mss 단독 시도 (raw 예외 노출)")
    # mss 가 None 반환하는 진짜 원인을 보기 위해 _capture_via_screen 의 mss 부분만 직접 실행.
    try:
        import mss as _mss
        ow, oh = svc.get_outer_size()
        rx, ry = svc._get_window_root_pos(hwnd) or (0, 0)
        l, r, t, b = svc._get_frame_extents(hwnd)
        # outer 영역 (frame 포함)
        x = rx - l
        y = ry - t
        w = ow
        h = oh
        screen_w, screen_h = svc._get_screen_size()
        print(f"requested region : x={x} y={y} w={w} h={h}")
        print(f"screen size      : {screen_w}x{screen_h}")
        print(f"region rhs       : x+w={x+w} y+h={y+h}  ({'OK' if x+w<=screen_w and y+h<=screen_h else 'OUT-OF-SCREEN'})")
        try:
            with _mss.mss() as sct:
                print(f"mss monitors     : {sct.monitors}")
                monitor = {"left": x, "top": y, "width": w, "height": h}
                sct_img = sct.grab(monitor)
                print(f"mss grab OK      : size={sct_img.size}")
        except Exception as e:
            print(f"mss grab FAILED  : {type(e).__name__}: {e}")
    except ImportError:
        print("mss not installed")
    except Exception as e:
        print("mss diagnosis exception:", e)

    hdr("캡처 결과 — LinControlService._capture_via_screen (mss + Xlib 폴백)")
    try:
        img = svc._capture_via_screen(hwnd)
        if img is None:
            print("capture failed (returned None — 두 경로 모두 실패)")
        else:
            print(f"PIL image size   : {img.size}  (mode={img.mode})")
            print(f"expected outer   : {svc.get_outer_size()}  ← status() 값")
            if img.size != svc.get_outer_size():
                print("⚠️  size mismatch — 캡처 영역과 status outer 가 다름")
            # 첫 픽셀 (좌상단) 과 중앙 픽셀 RGB
            print(f"pixel (0,0)      : {img.getpixel((0, 0))}")
            cw, ch = img.size
            print(f"pixel center     : {img.getpixel((cw // 2, ch // 2))}")
            # 빈 화면 (전부 검정 / 흰색) 감지 — 진단 목적
            extrema = img.getextrema()
            if isinstance(extrema[0], tuple):
                all_blank = all((mx - mn) <= 4 for mn, mx in extrema)
            else:
                all_blank = (extrema[1] - extrema[0]) <= 4
            print(f"all blank?       : {all_blank}  (True 면 캡처는 됐지만 내용이 단색 — GL/composited 윈도우 가능성)")
    except Exception as e:
        print("capture exception:", type(e).__name__, e)

    print()
    print("진단 끝 — 이 출력 전체를 그대로 공유해 주세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
