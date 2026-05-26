"""디바이스에서 두 번 연속 screencap → 둘 다 (0,0,200,200) 크롭 → 비교 통계 출력.

목적:
  - 같은 정지 화면에서 두 캡처가 픽셀 단위로 동일한지 확인
  - 동일하지 않다면 어떤 패턴으로 다른지 (균일 노이즈 vs 에지 집중)
  - SSIM blur on/off에 따른 점수 차이

사용:
  venv/bin/python scripts/diag_crop_compare.py <serial>
  venv/bin/python scripts/diag_crop_compare.py <serial> --display 131
  venv/bin/python scripts/diag_crop_compare.py <serial> --x 0 --y 0 --w 200 --h 200 --gap 1.0

디바이스가 결정적이면 identical=True, ssim=1.0이 나와야 정상.
"""
from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import cv2  # type: ignore
import numpy as np  # type: ignore
from skimage.metrics import structural_similarity as ssim  # type: ignore


def adb_screencap(serial: str, display: int | None) -> bytes:
    """adb shell screencap -p 결과 PNG 바이트 반환. display가 주어지면 -d 사용."""
    cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
    if display is not None:
        cmd = ["adb", "-s", serial, "exec-out", "screencap", "-d", str(display), "-p"]
    result = subprocess.run(cmd, capture_output=True, check=True)
    return result.stdout


def crop_region(png_bytes: bytes, x: int, y: int, w: int, h: int) -> tuple[np.ndarray, np.ndarray]:
    """PNG → BGR ndarray → 지정 영역 crop. (full, cropped) 반환."""
    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError("cv2.imdecode failed")
    h_full, w_full = img.shape[:2]
    if x + w > w_full or y + h > h_full:
        raise RuntimeError(
            f"crop region ({x},{y},{w}x{h}) exceeds image size {w_full}x{h_full}"
        )
    return img, img[y:y + h, x:x + w].copy()


def stats(a: np.ndarray, b: np.ndarray, label: str) -> None:
    """두 ndarray 차이 통계 + SSIM (blur on/off) 출력."""
    if a.shape != b.shape:
        print(f"[{label}] shape mismatch: {a.shape} vs {b.shape}")
        return
    d = cv2.absdiff(a, b)
    identical = bool((d == 0).all())
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)
    ssim_raw, _ = ssim(ga, gb, full=True)
    ga_b = cv2.GaussianBlur(ga, (3, 3), 0.5)
    gb_b = cv2.GaussianBlur(gb, (3, 3), 0.5)
    ssim_blur, _ = ssim(ga_b, gb_b, full=True)
    ga_b2 = cv2.GaussianBlur(ga, (5, 5), 1.0)
    gb_b2 = cv2.GaussianBlur(gb, (5, 5), 1.0)
    ssim_blur2, _ = ssim(ga_b2, gb_b2, full=True)
    gd = cv2.absdiff(ga, gb)
    print(
        f"[{label}] shape={a.shape} identical={identical}\n"
        f"  bgr_diff: max={int(d.max())} mean={float(d.mean()):.4f} "
        f">5={100.0 * float((d > 5).sum()) / d.size:.2f}% "
        f">2={100.0 * float((d > 2).sum()) / d.size:.2f}% "
        f">0={100.0 * float((d > 0).sum()) / d.size:.2f}%\n"
        f"  gray_diff: max={int(gd.max())} mean={float(gd.mean()):.4f} "
        f">5={100.0 * float((gd > 5).sum()) / gd.size:.2f}%\n"
        f"  ssim_raw={float(ssim_raw):.6f} "
        f"ssim_blur(3,0.5)={float(ssim_blur):.6f} "
        f"ssim_blur(5,1.0)={float(ssim_blur2):.6f}"
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("serial", help="ADB device serial (e.g. dc2ca56d)")
    ap.add_argument("--display", type=int, default=None, help="SF display id (screencap -d)")
    ap.add_argument("--x", type=int, default=0)
    ap.add_argument("--y", type=int, default=0)
    ap.add_argument("--w", type=int, default=200)
    ap.add_argument("--h", type=int, default=200)
    ap.add_argument("--gap", type=float, default=1.0, help="seconds between captures")
    ap.add_argument("--rounds", type=int, default=3, help="number of capture pairs")
    ap.add_argument("--save", action="store_true", help="save c1.png/c2.png/crop1.png/crop2.png to CWD")
    args = ap.parse_args()

    print(f"=== diag_crop_compare ===")
    print(f"device={args.serial} display={args.display}")
    print(f"crop=({args.x},{args.y}) {args.w}x{args.h} gap={args.gap}s rounds={args.rounds}")
    print()

    for r in range(1, args.rounds + 1):
        print(f"--- Round {r} ---")
        t0 = time.monotonic()
        png1 = adb_screencap(args.serial, args.display)
        t1 = time.monotonic()
        time.sleep(args.gap)
        png2 = adb_screencap(args.serial, args.display)
        t2 = time.monotonic()

        h1 = hashlib.sha256(png1).hexdigest()[:16]
        h2 = hashlib.sha256(png2).hexdigest()[:16]
        print(
            f"  png_bytes_len: {len(png1)} vs {len(png2)} "
            f"sha256[:16]={h1} vs {h2} png_identical={h1 == h2}"
        )
        print(f"  capture_ms: {1000*(t1-t0):.0f} {1000*(t2-t1):.0f}")

        full1, crop1 = crop_region(png1, args.x, args.y, args.w, args.h)
        full2, crop2 = crop_region(png2, args.x, args.y, args.w, args.h)

        stats(full1, full2, "full_screen")
        stats(crop1, crop2, f"crop_({args.x},{args.y})_{args.w}x{args.h}")

        if args.save and r == 1:
            Path("c1.png").write_bytes(png1)
            Path("c2.png").write_bytes(png2)
            cv2.imwrite("crop1.png", crop1)
            cv2.imwrite("crop2.png", crop2)
            print("  saved: c1.png c2.png crop1.png crop2.png")

        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
