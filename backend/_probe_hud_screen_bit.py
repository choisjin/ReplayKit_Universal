"""HKMC 6th HUD 캡처 screen bit 탐색 (실기 필요, 일회성).

레퍼런스에는 cluster(1<<0)/front(1<<3)/rear_l(1<<5)/rear_r(1<<7) 4종만 있고
HUD 비트는 문서화되어 있지 않다. 이 스크립트는 후보 비트로 CMD_GETIMG를 보내
결과 이미지를 저장하고, front_center 기준 캡처와 동일한지(=비트가 무시됨)
자동 비교해준다.

실행:
    python _probe_hud_screen_bit.py 192.168.0.10 20000
    python _probe_hud_screen_bit.py 192.168.0.10 20000 --bits 2,4,16,64 --size 1920x720

확인된 비트는 코드 수정 없이 환경변수로 적용한다:
    set HKMC_HUD_SCREEN_BIT=4
    set HKMC_HUD_RESOLUTION=1920x720
"""
import argparse
import sys
from pathlib import Path

from app.services import hkmc6th_service as H

OUT_DIR = Path(__file__).parent / "_hud_probe"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("port", type=int)
    ap.add_argument("--bits", default="2,4,16,64",
                    help="시도할 screen bit 목록 (10진/0x 혼용 가능, 기본 2,4,16,64)")
    ap.add_argument("--size", default="", help="HUD 캡처 요청 해상도 WxH (기본 1920x720)")
    ap.add_argument("--timeout", type=float, default=10.0)
    args = ap.parse_args()

    if args.size:
        w_s, h_s = args.size.lower().split("x")
        H.HKMC6thService._DEFAULT_SCREEN_SIZES["hud"] = (int(w_s), int(h_s))

    svc = H.HKMC6thService(args.host, args.port, device_id="hud-probe")
    if not svc.connect(timeout=args.timeout):
        print("connect failed")
        return 1

    OUT_DIR.mkdir(exist_ok=True)
    try:
        front = svc.screencap_bytes(screen_type="front_center", fmt="png", timeout=args.timeout)
        (OUT_DIR / "front_center.png").write_bytes(front)
        print(f"front_center: {len(front)} bytes -> {OUT_DIR / 'front_center.png'}")

        for token in args.bits.split(","):
            token = token.strip()
            if not token:
                continue
            bit = int(token, 0)
            H.SCREEN_CAPTURE_MAP["hud"] = bit
            svc._hud_bit_logged = False
            try:
                data = svc.screencap_bytes(screen_type="hud", fmt="png", timeout=args.timeout)
            except Exception as e:
                print(f"bit 0x{bit:02X}: FAIL ({e})")
                continue
            path = OUT_DIR / f"hud_bit_0x{bit:02X}.png"
            path.write_bytes(data)
            same = "front과 동일(비트 무시된 듯)" if data == front else "front과 다름 <-- 후보"
            print(f"bit 0x{bit:02X}: {len(data)} bytes, {same} -> {path}")
    finally:
        svc.disconnect()

    print("\n저장된 PNG를 눈으로 확인해 HUD 화면인 비트를 고르고, "
          "HKMC_HUD_SCREEN_BIT 환경변수에 설정하세요.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
