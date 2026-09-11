#!/usr/bin/env python3
"""WebOS(Connect Wide) 화면/터치 경로 단독 테스트 CLI.

빌드·백엔드 기동 없이 실기에서 바로 돌려보려고 만든 도구다. **백엔드와 같은
`WebOSStreamService` 를 그대로 import** 하므로 코드와 항상 일치한다(복사본 아님).

사용 (레포 루트 또는 배포 루트에서)::

    python tools/webos_test.py probe                  # 단계별 진단(adb→ssh→VM→linuxStream)
    python tools/webos_test.py raw                    # 스트림 원시 바이트 덤프(파싱 전)
    python tools/webos_test.py frames --count 5       # 프레임 수신 + PNG 저장
    python tools/webos_test.py watch --seconds 20     # 초당 수신량/프레임 추이
    python tools/webos_test.py tap 1920 720           # uinput 터치
    python tools/webos_test.py tap 1920 720 --via adb # ADB input tap
    python tools/webos_test.py swipe 3000 700 1000 700

공통 옵션: --serial/--ip/--user/--password/--scale/--quality/--fps
(--serial 을 안 주면 `adb devices` 의 첫 기기를 쓴다)

좌표는 **패널 좌표(예: 3840x1440)** 기준이다 — 백엔드가 프론트에서 받는 좌표계와 같다.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

# Windows 콘솔(cp949)에서 한글/대시 출력이 UnicodeEncodeError 로 죽지 않게.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

def _load_service():
    """webos_stream_service 모듈 로드.

    배포본(dist)은 Cython 컴파일본(.pyd)이라 .py 를 고쳐 넣어도 import 가 .pyd 를
    집는다. 그래서 **같은 경로에 .py 소스가 있으면 그쪽을 우선 로드**한다 —
    수정한 파일 하나만 복사하면 빌드 없이 바로 검증할 수 있다.
    """
    import importlib
    import importlib.util

    importlib.import_module("app.services")   # 부모 패키지 먼저(상대 import 해석용)
    src = ROOT / "backend" / "app" / "services" / "webos_stream_service.py"
    if src.is_file():
        spec = importlib.util.spec_from_file_location(
            "app.services.webos_stream_service", src)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod, f"소스 {src}"
    mod = importlib.import_module("app.services.webos_stream_service")
    return mod, f"컴파일본 {getattr(mod, '__file__', '(unknown)')}"


_svc_mod, _svc_origin = _load_service()
WebOSStreamService = _svc_mod.WebOSStreamService
REMOTE_FIFO = _svc_mod.REMOTE_FIFO
REMOTE_LOG = _svc_mod.REMOTE_LOG
REMOTE_BIN = _svc_mod.REMOTE_BIN

from app.services.adb_path import resolve_adb_path  # noqa: E402


def _setup_log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _first_serial() -> str:
    """`adb devices` 의 첫 device 시리얼."""
    out = subprocess.run([resolve_adb_path(), "devices"],
                         capture_output=True, text=True, timeout=20).stdout
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            return parts[0]
    return ""


def _make(args) -> WebOSStreamService:
    serial = args.serial or _first_serial()
    if not serial:
        print("[!] ADB 디바이스를 찾을 수 없습니다 (--serial 로 지정하세요)")
        sys.exit(2)
    print(f"[i] 로드: {_svc_origin}")
    print(f"[i] serial={serial} linux={args.user}@{args.ip} "
          f"scale={args.scale} q={args.quality} fps={args.fps}")
    return WebOSStreamService(
        adb_serial=serial, linux_ip=args.ip, linux_user=args.user,
        linux_password=args.password, scale=args.scale, quality=args.quality,
        fps=args.fps, idle_timeout=0, device_id="webos_test",
    )


def _geometry(svc: WebOSStreamService) -> None:
    print(f"[i] 미러(스트림) 크기 : {svc.size}")
    print(f"[i] native(패널) 크기 : {svc.native_size}")
    print(f"[i] uinput 좌표계     : {svc.touch_size}"
          + ("  ← 0x0 이면 uinput 미활성" if not all(svc.touch_size) else ""))


# ----------------------------------------------------------------------
# 명령들
# ----------------------------------------------------------------------

def cmd_probe(args) -> int:
    svc = _make(args)
    result = svc.probe()
    print(json.dumps(result["info"], ensure_ascii=False, indent=2))
    for st in result["steps"]:
        print(f"\n--- {st['step']} ---\n{st['result']}")
    return 0


def cmd_raw(args) -> int:
    """스트림 원시 바이트를 그대로 덤프 — 프레임 파싱 전에 무엇이 오는지 본다.

    배포/배선 문제로 텍스트(ssh 경고·adb 에러)가 흘러오면 여기서 바로 보인다.
    정상이면 `00 00 xx xx 4c 01 ff d8 ff ...` (len + 'L' + N + JPEG SOI).
    """
    svc = _make(args)
    svc._deploy()
    svc._ssh(f"pkill -x linuxStream 2>/dev/null; rm -f {REMOTE_FIFO}; "
             f"mkfifo {REMOTE_FIFO} 2>/dev/null; true", timeout=20.0)
    remote = (f"( if [ -p {REMOTE_FIFO} ]; then exec 0<>{REMOTE_FIFO}; fi; "
              f"exec {REMOTE_BIN} -scale={args.scale} -quality={args.quality} "
              f"-fps={args.fps} ) 2>{REMOTE_LOG}")
    cmd = svc._adb_args("exec-out", "sh", "-c", svc._ssh_wrap(remote) + " 2>/dev/null")
    print(f"[i] 실행: {' '.join(cmd[:4])} ... (원격: {remote})")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        data = b""
        deadline = time.time() + args.seconds
        while len(data) < args.bytes and time.time() < deadline:
            chunk = proc.stdout.read(min(4096, args.bytes - len(data)))
            if not chunk:
                break
            data += chunk
    finally:
        proc.kill()
    if not data:
        print("[!] 수신 0바이트 — 원격 로그를 확인하세요:")
        print(svc._read_remote_log())
        return 1
    print(f"[i] {len(data)} bytes 수신")
    head = data[:args.show]
    ascii_ = "".join(chr(c) if 32 <= c < 127 else "." for c in head)
    print(f"hex   : {head.hex()}")
    print(f"ascii : {ascii_}")
    if len(data) >= 6:
        size = int.from_bytes(data[:4], "big")
        print(f"해석  : 첫 프레임 len={size} type={chr(data[4])!r} "
              f"({'풀' if data[4:5] == b'L' else 'skip' if data[4:5] == b'S' else '델타' if data[4:5] == b'D' else '알 수 없음'})")
    return 0


def cmd_frames(args) -> int:
    svc = _make(args)
    t0 = time.time()
    svc.start(timeout=args.timeout)
    print(f"[i] 첫 프레임까지 {time.time() - t0:.1f}s")
    _geometry(svc)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for i in range(args.count):
        data = svc.screencap_bytes(fmt="png")
        path = out_dir / f"webos_{i + 1:02d}.png"
        path.write_bytes(data)
        print(f"[i] {path}  ({len(data)} bytes, frames={svc._frame_count})")
        time.sleep(args.interval)
    svc.stop()
    return 0


def cmd_watch(args) -> int:
    svc = _make(args)
    svc.start(timeout=args.timeout)
    _geometry(svc)
    prev = 0
    end = time.time() + args.seconds
    while time.time() < end:
        time.sleep(1.0)
        now = svc._frame_count
        print(f"[i] +{now - prev:4d} frames/s   총 {now}   running={svc.is_running}"
              + (f"   err={svc.last_error}" if svc.last_error else ""))
        prev = now
    svc.stop()
    return 0


def _touch(args, fn_uinput, fn_adb) -> int:
    svc = _make(args)
    if args.via == "uinput":
        svc.start(timeout=args.timeout)
        _geometry(svc)
        fn_uinput(svc)
        print("[i] uinput 패킷 전송 완료 — 화면 반응을 확인하세요")
        print("[i] 원격 로그 tail:")
        print(svc._read_remote_log())
        svc.stop()
    else:
        fn_adb(svc)
    return 0


def cmd_tap(args) -> int:
    def _u(svc):
        print(f"[i] uinput tap 패널({args.x},{args.y}) -> {svc._map_touch(args.x, args.y)}")
        svc.tap(args.x, args.y)

    def _a(svc):
        cmd = svc._adb_args("shell", f"input tap {args.x} {args.y}")
        print(f"[i] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        print(f"[i] rc={r.returncode} out={(r.stdout + r.stderr).strip()!r}")

    return _touch(args, _u, _a)


def cmd_swipe(args) -> int:
    def _u(svc):
        print(f"[i] uinput swipe ({args.x1},{args.y1})->({args.x2},{args.y2}) {args.duration}ms")
        svc.swipe(args.x1, args.y1, args.x2, args.y2, duration_ms=args.duration)

    def _a(svc):
        cmd = svc._adb_args(
            "shell", f"input swipe {args.x1} {args.y1} {args.x2} {args.y2} {args.duration}")
        print(f"[i] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"[i] rc={r.returncode} out={(r.stdout + r.stderr).strip()!r}")

    return _touch(args, _u, _a)


def main() -> int:
    # 공통 옵션은 부모 파서로 둬서 하위 명령 앞/뒤 어느 쪽에 써도 먹게 한다.
    #   ⚠ parents= 로 재사용하면 **하위 파서의 기본값이 상위에서 준 값을 덮어쓴다**.
    #     (`--serial X tap ...` 이 무시되는 함정) → 기본값을 SUPPRESS 로 두고
    #     파싱 후에 빠진 것만 채운다.
    DEFAULTS = {
        "serial": "", "ip": "172.16.4.1", "user": "root", "password": "root",
        "scale": 2, "quality": 60, "fps": 30, "timeout": 60.0, "verbose": False,
    }
    S = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--serial", default=S, help="ADB 시리얼 (생략 시 첫 기기)")
    common.add_argument("--ip", default=S, help="Linux VM IP (기본 172.16.4.1)")
    common.add_argument("--user", default=S)
    common.add_argument("--password", default=S)
    common.add_argument("--scale", type=int, default=S)
    common.add_argument("--quality", type=int, default=S)
    common.add_argument("--fps", type=int, default=S)
    common.add_argument("--timeout", type=float, default=S, help="첫 프레임 대기(초)")
    common.add_argument("-v", "--verbose", action="store_true", default=S)

    ap = argparse.ArgumentParser(
        description="WebOS(Connect Wide) 화면/터치 경로 단독 테스트",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="단계별 진단",
                   parents=[common]).set_defaults(func=cmd_probe)

    p = sub.add_parser("raw", help="스트림 원시 바이트 덤프", parents=[common])
    p.add_argument("--bytes", type=int, default=4096)
    p.add_argument("--show", type=int, default=64, help="출력할 앞부분 길이")
    p.add_argument("--seconds", type=float, default=10.0)
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("frames", help="프레임 수신 + PNG 저장", parents=[common])
    p.add_argument("--count", type=int, default=3)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--out", default="webos_frames")
    p.set_defaults(func=cmd_frames)

    p = sub.add_parser("watch", help="초당 프레임 추이", parents=[common])
    p.add_argument("--seconds", type=float, default=20.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("tap", help="터치 1회", parents=[common])
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--via", choices=["uinput", "adb"], default="uinput")
    p.set_defaults(func=cmd_tap)

    p = sub.add_parser("swipe", help="스와이프", parents=[common])
    p.add_argument("x1", type=int)
    p.add_argument("y1", type=int)
    p.add_argument("x2", type=int)
    p.add_argument("y2", type=int)
    p.add_argument("--duration", type=int, default=300)
    p.add_argument("--via", choices=["uinput", "adb"], default="uinput")
    p.set_defaults(func=cmd_swipe)

    args = ap.parse_args()
    for _k, _v in DEFAULTS.items():
        if not hasattr(args, _k):
            setattr(args, _k, _v)
    _setup_log(args.verbose)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(f"[!] 실패: {type(e).__name__}: {e}")
        if args.verbose:
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
