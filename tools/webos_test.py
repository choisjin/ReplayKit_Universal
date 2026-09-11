#!/usr/bin/env python3
"""WebOS(Connect Wide) 화면/터치 경로 단독 테스트 CLI.

빌드·백엔드 기동 없이 실기에서 바로 돌려보려고 만든 도구다. **백엔드와 같은
`WebOSStreamService` 를 그대로 import** 하므로 코드와 항상 일치한다(복사본 아님).

사용 (레포 루트 또는 배포 루트에서)::

    python tools/webos_test.py probe                  # 단계별 진단(adb→ssh→VM→linuxStream)
    python tools/webos_test.py raw                    # 스트림 원시 바이트 덤프(파싱 전)
    python tools/webos_test.py frames --count 5       # 프레임 수신 + PNG 저장
    python tools/webos_test.py watch --seconds 20     # 초당 수신량/프레임 추이
    python tools/webos_test.py tap 1920 720           # evdev 직접 주입(기본)
    python tools/webos_test.py tap 1920 720 --via adb # ADB input tap(참고용)
    python tools/webos_test.py swipe 3000 700 1000 700

공통 옵션: --serial/--ip/--user/--password/--scale/--quality/--fps
(--serial 을 안 주면 `adb devices` 의 첫 기기를 쓴다)

좌표는 **패널 좌표(예: 3840x1440)** 기준이다 — 백엔드가 프론트에서 받는 좌표계와 같다.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import struct
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
    svc = WebOSStreamService(
        adb_serial=serial, linux_ip=args.ip, linux_user=args.user,
        linux_password=args.password, scale=args.scale, quality=args.quality,
        fps=args.fps, idle_timeout=0, device_id="webos_test",
    )
    if getattr(args, "no_kill", False):
        svc.kill_existing = False
        print("[i] --no-kill: 기존 linuxStream 을 죽이지 않고 추가로 띄웁니다")
    return svc


def _to_panel(svc: WebOSStreamService, x: int, y: int, space: str) -> tuple:
    """입력 좌표를 패널 좌표로 변환.

    --space mirror : 저장한 PNG(스트림 출력, 예 1920x720) 픽셀 좌표
    --space panel  : 패널/Android 좌표 (예 3840x1440) — 백엔드가 쓰는 기준
    """
    if space == "panel":
        return int(x), int(y)
    sw, sh = svc.size
    nw, nh = svc.native_size
    if sw and nw:
        x = x * nw / sw
    if sh and nh:
        y = y * nh / sh
    return int(round(x)), int(round(y))


def _warn_if_contended(svc) -> None:
    """스트림이 금방 끊기면 같은 HU 를 보는 다른 주체가 있을 가능성을 알린다.

    스트림 기동 시 디바이스에서 `pkill -x linuxStream` 을 하므로, 백엔드 미러가
    같은 HU 를 보고 있으면 서로 끄는 핑퐁이 된다.
    """
    if "WebOS stream ended" in (svc.last_error or ""):
        print("[!] 스트림이 끊겼습니다 — 백엔드 미러가 같은 HU 의 WebOS 화면을 보고 있으면"
              " 서로의 linuxStream 을 종료시킵니다. 브라우저에서 미러를 다른 화면/디바이스로"
              " 돌린 뒤 다시 실행해 보세요.")


def _geometry(svc: WebOSStreamService) -> None:
    print(f"[i] 미러(스트림) 크기 : {svc.size}")
    print(f"[i] native(패널) 크기 : {svc.native_size}   ← 터치 좌표 기준")
    print(f"[i] 터치 주입 노드    : {svc.evdev_path or '(첫 터치 때 자동 탐색)'}")
    print(f"[i] linuxStream uinput: {svc.touch_size} (미사용 — 참고용)")


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
    svc._ssh("pkill -x linuxStream 2>/dev/null; true", timeout=20.0)
    remote = (f"{REMOTE_BIN} -scale={args.scale} -quality={args.quality} "
              f"-fps={args.fps} -verbose 2>{REMOTE_LOG}")
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
        if i == 0:
            print("[i] 이 PNG 의 픽셀 좌표로 터치하려면 --space mirror 를 쓰세요 "
                  "(기본 --space panel 은 3840x1440 기준)")
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
    if args.via == "evdev":
        svc.start(timeout=args.timeout)
        _geometry(svc)
        fn_uinput(svc)
        print("[i] 전송 완료 — 화면 반응을 확인하세요 "
              "(물리 터치와 동일한 evdev 이벤트입니다)")
        svc.stop()
    else:
        # --space mirror 면 스트림 기하(미러/패널 크기)가 필요해 스트림을 잠깐 띄운다.
        if args.space == "mirror":
            svc.start(timeout=args.timeout)
            _geometry(svc)
        fn_adb(svc)
        if args.space == "mirror":
            svc.stop()
    return 0


def cmd_tap(args) -> int:
    def _u(svc):
        px, py = _to_panel(svc, args.x, args.y, args.space)
        print(f"[i] 입력({args.x},{args.y}, {args.space}) -> 패널({px},{py})  "
              f"hold={args.hold}ms")
        svc.tap(px, py, hold_ms=args.hold)

    def _a(svc):
        px, py = _to_panel(svc, args.x, args.y, args.space)
        print(f"[i] 입력({args.x},{args.y}, {args.space}) -> 패널({px},{py})")
        cmd = svc._adb_args("shell", f"input tap {px} {py}")
        print(f"[i] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        print(f"[i] rc={r.returncode} out={(r.stdout + r.stderr).strip()!r}")

    return _touch(args, _u, _a)


def cmd_swipe(args) -> int:
    def _u(svc):
        ax, ay = _to_panel(svc, args.x1, args.y1, args.space)
        bx, by = _to_panel(svc, args.x2, args.y2, args.space)
        print(f"[i] uinput swipe 패널({ax},{ay})->({bx},{by}) {args.duration}ms")
        svc.swipe(ax, ay, bx, by, duration_ms=args.duration)

    def _a(svc):
        ax, ay = _to_panel(svc, args.x1, args.y1, args.space)
        bx, by = _to_panel(svc, args.x2, args.y2, args.space)
        print(f"[i] 패널({ax},{ay})->({bx},{by})")
        cmd = svc._adb_args(
            "shell", f"input swipe {ax} {ay} {bx} {by} {args.duration}")
        print(f"[i] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"[i] rc={r.returncode} out={(r.stdout + r.stderr).strip()!r}")

    return _touch(args, _u, _a)


def cmd_android(args) -> int:
    """Android 쪽 상태 점검 — 터치를 어디로 보내야 하는지 판단용.

    webOS 영역은 Android 레이어의 hole 이라, 그 영역 터치를 Android 가 받는지
    (= 투사 앱이 forward 하는 구조인지) 아니면 Linux VM 이 직접 받는지에 따라
    써야 할 경로가 갈린다. 포커스 앱/디스플레이 목록이 그 단서다.
    """
    svc = _make(args)

    def _sh(label: str, cmd: str, timeout: float = 20.0) -> str:
        r = subprocess.run(svc._adb_args("shell", cmd),
                           capture_output=True, text=True, timeout=timeout)
        out = (r.stdout + r.stderr).strip()
        print(f"\n--- {label} ---\n{out[:1500] or '(빈 응답)'}")
        return out

    _sh("wm size / density", "wm size; wm density")
    _sh("디스플레이 목록", "dumpsys display | grep -E 'mDisplayId|displayId=|uniqueId' | head -40")
    _sh("현재 포커스 창", "dumpsys window | grep -E 'mCurrentFocus|mFocusedApp' | head -10")
    _sh("최상위 액티비티", "dumpsys activity activities | grep -E 'topResumedActivity|ResumedActivity' | head -10")
    _sh("webOS 투사 앱", "dumpsys window | grep -i webos | head -20")

    if args.keyevent:
        print(f"\n[i] keyevent {args.keyevent} 주입 — Android UI 가 반응하면 "
              f"input 주입 자체는 동작하는 것입니다")
        _sh(f"input keyevent {args.keyevent}", f"input keyevent {args.keyevent}")
    return 0


def cmd_touch(args) -> int:
    """터치 패킷 1개만 전송 — press/move/release 를 따로 쏴서 유실 지점을 찾는다."""
    svc = _make(args)
    svc.start(timeout=args.timeout)
    _geometry(svc)
    px, py = _to_panel(svc, args.x, args.y, args.space)
    builders = {
        "press": lambda: _svc_mod._mt_down(px, py, slot=args.id, tracking=args.id + 1),
        "move": lambda: _svc_mod._mt_move(px, py, slot=args.id),
        "release": lambda: _svc_mod._mt_up(slot=args.id),
    }
    print(f"[i] {args.type} 패널({px},{py}) slot={args.id} -> {svc.evdev_path or '(자동 탐색)'}")
    svc._write_ev(builders[args.type]())
    time.sleep(args.wait)
    print(f"[i] 전송 완료 (노드: {svc.evdev_path})")
    svc.stop()
    return 0


def cmd_sh(args) -> int:
    """Linux VM(webOS) 또는 Android HU 에서 임의 명령 실행.

    왕복 비용을 줄이려고 둔 탈출구. 예::

        webos_test.py sh "cat /proc/bus/input/devices"
        webos_test.py sh --android "dumpsys window | grep mCurrentFocus"
    """
    svc = _make(args)
    cmd = " ".join(args.command)
    if args.android:
        print(f"[i] (Android) {cmd}")
        r = subprocess.run(svc._adb_args("shell", cmd),
                           capture_output=True, text=True, timeout=args.timeout)
        out = (r.stdout + r.stderr).strip()
    else:
        print(f"[i] (Linux VM {svc.linux_user}@{svc.linux_ip}) {cmd}")
        svc._deploy()          # ssh_ 배포 보장
        r = svc._ssh(cmd, timeout=args.timeout)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
    print(out or "(빈 응답)")
    return 0


def cmd_inputinfo(args) -> int:
    """webOS(Linux VM) 입력 장치 상태 — uinput 터치가 왜 안 먹는지 판단용.

    linuxStream 이 만드는 장치 이름은 'linuxStream-touch' 다. 이 장치가
    /proc/bus/input/devices 에 보이는지, 어떤 핸들러(eventN)가 붙었는지,
    ABS_MT_* 멀티터치 축을 갖는지가 핵심 단서다 — libinput 계열은 MT 축이나
    INPUT_PROP_DIRECT 가 없으면 터치스크린으로 인식하지 않는다.
    """
    svc = _make(args)
    svc._deploy()
    # 스트림을 띄워 uinput 장치가 만들어진 상태로 본다
    svc.start(timeout=args.timeout)
    _geometry(svc)
    try:
        for label, cmd in [
            ("입력 장치 목록", "cat /proc/bus/input/devices"),
            ("/dev/input", "ls -l /dev/input/ 2>&1"),
            ("linuxStream 프로세스", "ps | grep -i linuxstream | grep -v grep"),
            ("libinput 인식", "libinput list-devices 2>&1 | head -60"),
        ]:
            r = svc._ssh(cmd, timeout=30.0)
            out = ((r.stdout or "") + (r.stderr or "")).strip()
            print(f"\n--- {label} ---\n{out[:2500] or '(빈 응답)'}")
    finally:
        svc.stop()
    return 0


def cmd_evtest(args) -> int:
    """uinput 장치의 **커널 이벤트**를 캡처하면서 press/release 를 쏜다.

    linuxStream 로그는 press 만 찍히는데, 실제로 release(BTN_TOUCH=0 / SYN)가
    커널까지 나가는지는 로그로 알 수 없다. 여기서 확정한다.

    --device 를 안 주면 /proc/bus/input/devices 에서 'linuxStream-touch' 의
    eventN 을 자동으로 찾는다. evtest 가 없으면 hexdump 폴백을 쓴다.
    """
    svc = _make(args)
    svc.start(timeout=args.timeout)
    _geometry(svc)
    try:
        dev = args.device
        if not dev:
            r = svc._ssh(
                "awk '/linuxStream-touch/,/^$/' /proc/bus/input/devices "
                "| grep -o 'event[0-9]*' | head -1", timeout=20.0)
            name = ((r.stdout or "") + (r.stderr or "")).strip().split()
            if name:
                dev = f"/dev/input/{name[-1]}"
        if not dev:
            print("[!] linuxStream-touch 의 event 장치를 찾지 못했습니다. "
                  "`inputinfo` 로 확인 후 --device 로 직접 지정하세요.")
            return 1
        print(f"[i] 캡처 장치: {dev}")

        cap = ("(command -v evtest >/dev/null && timeout %d evtest %s "
               "|| timeout %d hexdump -v -e '24/1 \"%%02x \" \"\\n\"' %s) > /tmp/evcap.txt 2>&1 &"
               % (args.seconds, dev, args.seconds, dev))
        svc._ssh(cap, timeout=20.0)
        time.sleep(1.0)

        px, py = _to_panel(svc, args.x, args.y, args.space)
        print(f"[i] press/release 전송 → 패널({px},{py})  hold={args.hold}ms")
        svc.tap(px, py, hold_ms=args.hold)

        time.sleep(args.seconds + 1)
        r = svc._ssh("cat /tmp/evcap.txt 2>&1 | tail -n 60", timeout=30.0)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        print(f"\n--- 커널 입력 이벤트 ---\n{out or '(없음 — 이벤트가 커널까지 안 갔습니다)'}")
        print("\n--- linuxStream 로그 ---")
        print(svc._read_remote_log())
    finally:
        svc.stop()
    return 0


# --- evdev 주입 (실제 터치스크린 노드에 직접 write) ---------------------------
#
# linuxStream 이 만드는 uinput 장치는 INPUT_PROP_DIRECT 도 없고 ABS_MT_* 축도 없는
# 구형 단일터치 스펙이라 webOS 입력 스택이 터치스크린으로 인식하지 않는다.
# evdev 노드는 write() 로 이벤트 주입이 되므로, **실제 터치스크린 장치**에 물리 터치와
# 동일한 멀티터치 프로토콜 B 시퀀스를 써 넣는다.

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0x00
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X, ABS_MT_POSITION_Y = 0x35, 0x36
ABS_MT_TRACKING_ID = 0x39


def _ev(etype: int, code: int, value: int) -> bytes:
    """struct input_event (aarch64: timeval 16B + type/code/value = 24B)."""
    return struct.pack("<qqHHi", 0, 0, etype, code, value)


def _mt_down(x: int, y: int, slot: int = 0, tracking: int = 1) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_TRACKING_ID, tracking),
        _ev(EV_ABS, ABS_MT_POSITION_X, x),
        _ev(EV_ABS, ABS_MT_POSITION_Y, y),
        _ev(EV_KEY, BTN_TOUCH, 1),
        _ev(EV_ABS, ABS_X, x),
        _ev(EV_ABS, ABS_Y, y),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])


def _mt_move(x: int, y: int, slot: int = 0) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_POSITION_X, x),
        _ev(EV_ABS, ABS_MT_POSITION_Y, y),
        _ev(EV_ABS, ABS_X, x),
        _ev(EV_ABS, ABS_Y, y),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])


def _mt_up(slot: int = 0) -> bytes:
    return b"".join([
        _ev(EV_ABS, ABS_MT_SLOT, slot),
        _ev(EV_ABS, ABS_MT_TRACKING_ID, -1),
        _ev(EV_KEY, BTN_TOUCH, 0),
        _ev(EV_SYN, SYN_REPORT, 0),
    ])


def _write_events(svc, device: str, payload: bytes, timeout: float = 30.0) -> str:
    """base64 로 실어 보내 디바이스 노드에 write — 따옴표/이스케이프 사고를 피한다."""
    b64 = base64.b64encode(payload).decode("ascii")
    r = svc._ssh(f"echo {b64} | base64 -d > {device} && echo INJECT_OK", timeout=timeout)
    return ((r.stdout or "") + (r.stderr or "")).strip()


def _find_touch_device(svc) -> str:
    """실제 터치스크린 evdev 노드 탐색 — MT 축(ABS bit 53/54)을 가진 첫 장치.

    linuxStream-touch 는 제외한다(우리가 만든 가짜 장치).
    """
    r = svc._ssh("cat /proc/bus/input/devices", timeout=20.0)
    text = (r.stdout or "") + (r.stderr or "")
    name, handlers, absbits = "", "", ""
    best = ""
    for line in text.splitlines() + [""]:
        line = line.strip()
        if line.startswith("N: Name="):
            name = line.split("=", 1)[1].strip('"')
        elif line.startswith("H: Handlers="):
            handlers = line.split("=", 1)[1]
        elif line.startswith("B: ABS="):
            absbits = line.split("=", 1)[1].replace(" ", "")
        elif not line:
            if name and "linuxStream" not in name and absbits and handlers:
                try:
                    mask = int(absbits, 16)
                except ValueError:
                    mask = 0
                # ABS_MT_POSITION_X(53) / Y(54) 보유 = 진짜 멀티터치 터치스크린
                if mask >> 53 & 1 and mask >> 54 & 1:
                    ev = [h for h in handlers.split() if h.startswith("event")]
                    if ev and not best:
                        best = f"/dev/input/{ev[0]}"
                        print(f"[i] 터치스크린 감지: {name} -> {best}")
            name, handlers, absbits = "", "", ""
    return best


def cmd_evinject(args) -> int:
    """실제 터치스크린 노드에 MT 이벤트를 직접 주입한다(탭/스와이프)."""
    svc = _make(args)
    svc._deploy()
    if args.space == "mirror":
        svc.start(timeout=args.timeout)      # 좌표 환산에 스트림 기하 필요
    try:
        device = args.device or _find_touch_device(svc)
        if not device:
            print("[!] 멀티터치 터치스크린 노드를 찾지 못했습니다 (--device 로 지정)")
            return 1
        x1, y1 = _to_panel(svc, args.x, args.y, args.space)
        if args.to is not None:
            x2, y2 = _to_panel(svc, args.to[0], args.to[1], args.space)
        else:
            x2, y2 = x1, y1
        print(f"[i] {device} 에 주입: ({x1},{y1})"
              + (f" -> ({x2},{y2}) 스와이프" if (x1, y1) != (x2, y2) else " 탭")
              + f"  hold={args.hold}ms")

        payload = _mt_down(x1, y1)
        if (x1, y1) != (x2, y2):
            steps = max(2, args.steps)
            for i in range(1, steps + 1):
                t = i / steps
                payload += _mt_move(int(x1 + (x2 - x1) * t), int(y1 + (y2 - y1) * t))
        payload += _mt_up()

        out = _write_events(svc, device, payload)
        print(f"[i] 결과: {out or '(응답 없음)'}")
        print("[i] 화면이 반응하는지 확인하세요 "
              "(물리 터치와 동일한 이벤트 시퀀스입니다)")
    finally:
        if args.space == "mirror":
            svc.stop()
    return 0


def cmd_edge(args) -> int:
    """가장자리 → 안쪽 스와이프 (리모콘 열기 같은 엣지 제스처 검증).

    시작점을 화면 끝(0 또는 max-1)에 정확히 맞춰 보낸다 — 미러에서 손으로 드래그하면
    몇 px 안쪽에서 시작해 인식이 안 되는 경우가 많아서, 그 변수를 제거한 테스트다.
    """
    svc = _make(args)
    svc.start(timeout=args.timeout)
    _geometry(svc)
    w, h = svc.native_size
    if not (w and h):
        print("[!] 패널 크기를 알 수 없습니다")
        return 1
    ratio = max(0.05, min(0.95, args.ratio))
    if args.side == "right":
        x1, y1 = w - 1, int(h * ratio)
        x2, y2 = max(0, w - 1 - args.length), y1
    elif args.side == "left":
        x1, y1 = 0, int(h * ratio)
        x2, y2 = min(w - 1, args.length), y1
    elif args.side == "bottom":
        x1, y1 = int(w * ratio), h - 1
        x2, y2 = x1, max(0, h - 1 - args.length)
    else:  # top
        x1, y1 = int(w * ratio), 0
        x2, y2 = x1, min(h - 1, args.length)
    print(f"[i] {args.side} 엣지: ({x1},{y1}) -> ({x2},{y2})  "
          f"{args.duration}ms hold={args.hold}ms  via={args.via}")
    if args.via == "adb":
        # Android 레이어(투사 앱/시스템 UI)가 제스처를 가로채는지 확인용.
        cmd = svc._adb_args(
            "shell", f"input swipe {x1} {y1} {x2} {y2} {args.duration}")
        print(f"[i] {' '.join(cmd)}")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        print(f"[i] rc={r.returncode} out={(r.stdout + r.stderr).strip()!r}")
    else:
        svc.swipe(x1, y1, x2, y2, duration_ms=args.duration, hold_ms=args.hold)
    print("[i] 전송 완료 — 리모콘/패널이 떴는지 확인하세요")
    svc.stop()
    return 0


# 참조본 01_monitor_up.bat / 02_monitor_down.bat 의 vcs_simulator_rx 프레임.
# 디스플레이 팝업(Extended) 상태를 바꾼다 — 이 상태에 따라 Android 논리 크기가
# 3840x850 ↔ 3840x1440 으로 달라지고, 화면 레이아웃도 함께 바뀐다.
_MONITOR_FRAMES = {
    "up": [
        "\\x83\\x50\\x10\\x00\\x00\\x01\\x98\\x00\\x00\\x00\\x02\\x01"
        "\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00",
        "\\x83\\x50\\x10\\x00\\x00\\x01\\x98\\x00\\x00\\x00\\x01\\x01"
        "\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00",
    ],
    "down": [
        "\\x83\\x50\\x10\\x00\\x00\\x01\\x98\\x00\\x00\\x00\\x03\\x01"
        "\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00",
        "\\x83\\x50\\x10\\x00\\x00\\x01\\x98\\x00\\x00\\x00\\x04\\x01"
        "\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00",
    ],
}


def cmd_monitor(args) -> int:
    """디스플레이 팝업 up/down (참조본 01/02_monitor_*.bat 와 동일 프레임).

    이 상태에 따라 Android 논리 크기(wm size)가 바뀐다 — 기본 화면이 작게 보이거나
    터치가 어긋날 때 상태를 되돌려 보는 용도.
    """
    svc = _make(args)
    frames = _MONITOR_FRAMES[args.state]
    for i, fr in enumerate(frames):
        cmd = svc._adb_args("shell", f"echo -e -n '{fr}' > /dev/vcs_simulator_rx")
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        print(f"[i] frame{i + 1} rc={r.returncode} {(r.stdout + r.stderr).strip()!r}")
        if i == 0:
            time.sleep(3.0)
    r = subprocess.run(svc._adb_args("shell", "wm size"),
                       capture_output=True, text=True, timeout=20)
    print(f"[i] 이후 wm size: {(r.stdout + r.stderr).strip()}")
    return 0


def _adb_text(svc, *a, timeout=60) -> str:
    r = subprocess.run(svc._adb_args(*a), capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def _screencap(svc, display_id=None, timeout=60):
    """Android 화면을 base64 경유로 받아 BGR 로 디코드.

    exec-out 의 raw 바이너리는 PC/adb 조합에 따라 깨지므로 base64 를 쓴다(알려진 이슈).
    """
    import base64 as _b64

    import cv2
    import numpy as np

    d = f"-d {display_id} " if display_id is not None else ""
    r = subprocess.run(svc._adb_args("shell", f"screencap {d}-p | base64"),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 or not r.stdout.strip():
        return None, f"rc={r.returncode} {r.stderr.strip()[:160]}"
    try:
        raw = _b64.b64decode("".join(r.stdout.split()))
    except Exception as e:
        return None, f"base64 디코딩 실패: {e}"
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None, f"PNG 디코딩 실패 ({len(raw)}B)"
    return img, ""


def cmd_dual(args) -> int:
    """스트림 2개를 **서로 죽이지 않게** 동시에 띄워 공존 가능 여부를 본다.

    둘 다 30fps 근처를 유지하면 pkill 을 제거해도 된다는 뜻이다.
    한쪽이 0fps 로 떨어지면 디바이스에서 한 인스턴스만 살 수 있다는 뜻.
    """
    a = _make(args)
    a.device_id, a.kill_existing = "dual_A", False
    b = _make(args)
    b.device_id, b.kill_existing = "dual_B", False

    # A 를 먼저 띄우고, 안정된 뒤 B 를 붙인다 — B 기동이 A 를 끊는지가 핵심.
    print("\n[A] 기동")
    a.start(timeout=args.timeout)
    time.sleep(2.0)
    print(f"[A] 단독 안정화 완료 (프레임 {a._frame_count})")

    print("[B] 기동 — 여기서 A 가 끊기면 공존 불가")
    try:
        b.start(timeout=args.timeout)
    except Exception as e:
        print(f"[!] B 기동 실패: {e}")

    pa, pb = a._frame_count, b._frame_count
    ok = True
    for i in range(args.seconds):
        time.sleep(1.0)
        ca, cb = a._frame_count, b._frame_count
        fa, fb = ca - pa, cb - pb
        pa, pb = ca, cb
        if fa == 0 or fb == 0:
            ok = False
        print(f"  {i + 1:2d}s   A {fa:3d} fps (총 {ca}, running={a.is_running})"
              f"   B {fb:3d} fps (총 {cb}, running={b.is_running})")

    print("\n=== 디바이스 프로세스 ===")
    r = a._ssh("pgrep -x linuxStream | tr '\\n' ' '; echo", timeout=25.0)
    pids = (r.stdout or "").strip()
    print(f"  linuxStream pid: {pids or '(없음)'}")

    a.stop()
    b.stop()
    print()
    if ok:
        print("[결론] 두 인스턴스 공존 OK → 기동 시 pkill 을 제거해도 됩니다.")
        print("       (iSAP·ADB·테스트툴이 같은 HU 를 동시에 봐도 서로 안 끊김)")
    else:
        print("[결론] 한 번에 한 인스턴스만 가능합니다 → pkill 유지 + 프로세스 내 공유로 갑니다.")
        print("       (테스트툴을 쓸 때는 백엔드 미러를 다른 화면으로 돌려야 합니다)")
    return 0


def cmd_diag(args) -> int:
    """스트림이 끊길 때까지 기다렸다가 **디바이스 쪽 상태**를 그대로 찍는다.

    linuxStream 이 죽었는지(=스스로 종료) 살아있는지(=파이프만 끊김)가 갈림길이다.
    """
    svc = _make(args)
    try:
        svc.start(timeout=args.timeout)
    except Exception as e:
        print(f"[!] 기동 실패: {e}")

    t0 = time.time()
    while time.time() - t0 < args.wait:
        if not svc.is_running:
            break
        time.sleep(0.2)
    dead = not svc.is_running
    print(f"\n[i] {time.time() - t0:.2f}s 경과, 프레임 {svc._frame_count}개, "
          f"running={svc.is_running}")
    if svc.last_error:
        print(f"[i] last_error: {svc.last_error}")
    if not dead:
        print("[i] 살아있습니다 — 이 구성에서는 재현 안 됨")

    # 이 시점의 디바이스 상태. ssh 동시 세션은 정상임을 sshtest 로 확인했다.
    print("\n=== linuxStream 프로세스 ===")
    r = svc._ssh("pgrep -x linuxStream || echo NO_PROC", timeout=25.0)
    alive = "NO_PROC" not in (r.stdout or "")
    print((r.stdout or r.stderr).strip()[:200])
    print(f"  → linuxStream {'살아있음 (파이프만 끊긴 것)' if alive else '죽음 (스스로 종료)'}")

    print("\n=== /tmp/linuxStream.log (마지막 40줄) ===")
    r = svc._ssh(f"tail -n 40 /tmp/linuxStream.log 2>/dev/null || echo NO_LOG", timeout=25.0)
    print((r.stdout or r.stderr).strip()[:3000])

    print("\n=== dmesg 마지막 20줄 (OOM/세그폴트 확인) ===")
    r = svc._ssh("dmesg 2>/dev/null | tail -n 20 || echo NO_DMESG", timeout=25.0)
    print((r.stdout or r.stderr).strip()[:2000])

    print("\n=== 메모리 ===")
    r = svc._ssh("head -n 3 /proc/meminfo", timeout=25.0)
    print((r.stdout or r.stderr).strip()[:300])

    print("\n=== adb 상태 ===")
    print(_adb_text(svc, "devices", "-l").strip()[:400])

    svc.stop()
    print("\n[i] linuxStream 이 '죽음' 이면 로그/dmesg 의 마지막 줄이 사유입니다.")
    print("[i] '살아있음' 이면 adb exec-out 전송이 끊긴 것이라 전송 경로를 바꿔야 합니다.")
    return 0


def cmd_sshtest(args) -> int:
    """Linux VM ssh 세션이 **배타적인지** 실측한다.

    긴 ssh(1초마다 TICK)를 띄워 두고 중간에 (1) Android adb shell, (2) 두 번째 ssh 를
    붙여 첫 세션이 끊기는지 본다. 끊긴다면 스트림이 사는 동안 어떤 ssh 도 쓸 수 없다는
    뜻이고, 터치/노드탐색을 전부 한 세션으로 다중화해야 한다.
    """
    import threading

    svc = _make(args)
    svc._deploy()

    ticks, dead_at = [], [None]
    remote = "i=0; while [ $i -lt 20 ]; do i=$((i+1)); echo TICK$i; sleep 1; done"
    cmd = svc._adb_args("exec-out", "sh", "-c", svc._ssh_wrap(remote) + " 2>/dev/null")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def _rx():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line:
                ticks.append((time.time(), line))
        dead_at[0] = time.time()

    threading.Thread(target=_rx, daemon=True).start()

    t0 = time.time()
    time.sleep(3.0)
    n1 = len(ticks)
    print(f"[1] 단독 3초: TICK {n1}개 (살아있음={dead_at[0] is None})")
    if n1 == 0:
        print("[!] 첫 세션이 아예 안 뜹니다 — ssh/배포 문제입니다")
        proc.kill()
        return 1

    print("[2] Android adb shell 한 번 실행 (ssh 아님)")
    subprocess.run(svc._adb_args("shell", "echo ANDROID_OK"),
                   capture_output=True, text=True, timeout=20)
    time.sleep(2.0)
    n2 = len(ticks)
    alive2 = dead_at[0] is None
    print(f"    → TICK {n2}개(+{n2 - n1}) 살아있음={alive2}")

    print("[3] 두 번째 ssh 세션 실행")
    r = svc._ssh("echo SECOND_OK", timeout=25.0)
    print(f"    두 번째 ssh 결과: {(r.stdout or r.stderr).strip()[:80]!r}")
    time.sleep(2.0)
    n3 = len(ticks)
    alive3 = dead_at[0] is None
    print(f"    → TICK {n3}개(+{n3 - n2}) 살아있음={alive3}")

    proc.kill()
    print()
    if not alive2:
        print("[결론] adb shell 한 번에 첫 세션이 죽습니다 — ssh 가 아니라 **adb 링크** 문제입니다.")
    elif not alive3 or n3 == n2:
        print("[결론] ssh 세션은 **배타적**입니다. 스트림이 도는 동안 별도 ssh 를 열면 안 됩니다")
        print("       → 터치/노드탐색을 스트림과 한 세션으로 다중화해야 합니다.")
    else:
        print("[결론] ssh 동시 세션 정상. 스트림이 죽는 원인은 다른 데 있습니다.")
        print(f"       (경과 {time.time() - t0:.1f}s, 총 TICK {n3}개)")
    return 0


def cmd_displays(args) -> int:
    """Android 디스플레이를 **전부** 캡처해 어디에 UI 가 그려지는지 찾는다.

    webOS 가 떠 있을 때 기본 디스플레이 캡처가 전부 검정이면, 실제 패널에 보이는
    사이드바/시계는 (a) 다른 display id 에 있거나 (b) Android 가 아닌 QNX 컴포지터
    레이어다. (b) 라면 Android 합성으로는 재현할 수 없다.
    """
    import cv2

    svc = _make(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("=== dumpsys SurfaceFlinger --display-id ===")
    print(_adb_text(svc, "shell", "dumpsys SurfaceFlinger --display-id").strip()[:1500])

    print("\n=== 현재 포커스 ===")
    print(_adb_text(
        svc, "shell",
        "dumpsys activity activities | grep -iE 'Display #|topResumedActivity'"
    ).strip()[:1200])

    # 캡처 대상: 기본(-d 없음) + dumpsys 에서 찾은 물리 display id + 논리 id 0..3
    ids = []
    for line in _adb_text(svc, "shell", "dumpsys SurfaceFlinger --display-id").splitlines():
        for tok in line.replace(":", " ").replace(",", " ").split():
            if tok.isdigit() and len(tok) >= 3 and tok not in ids:
                ids.append(tok)
    targets = [None] + ids + [t for t in ("0", "1", "2", "3") if t not in ids]

    print(f"\n=== 캡처 ({len(targets)}개) ===")
    for t in targets:
        img, err = _screencap(svc, t)
        name = "default" if t is None else f"d{t}"
        if img is None:
            print(f"  {name:<16} 실패 — {err}")
            continue
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        opaque = float((gray > args.threshold).mean()) * 100
        path = out / f"android_{name}.png"
        cv2.imwrite(str(path), img)
        mark = "  ← UI 있음" if opaque > 0.5 else ""
        print(f"  {name:<16} {img.shape[1]}x{img.shape[0]}  "
              f"불투명 {opaque:5.1f}%  -> {path.name}{mark}")

    print(f"\n[i] {out.resolve()} 확인")
    print("[i] 어느 것도 UI 가 없으면 사이드바는 Android 가 아닌 QNX 레이어입니다"
          " (Android 합성으로 재현 불가).")
    return 0


def cmd_composite(args) -> int:
    """webOS(Linux) 프레임 + Android 화면을 받아 **합성 미리보기**를 만든다.

    실제 패널은 webOS 위에 Android UI(좌측 사이드바, 상태 아이콘, 팝업 등)가 얹힌
    모습인데, 우리 WebOS 미러는 Linux 레이어만 보여준다. Android 캡처에서 webOS 가
    비쳐 보이는 영역이 **검정으로 찍히는지**(= 검정을 투명으로 보고 합성 가능한지)를
    눈으로 확인하려는 도구다.

    저장물: webos.png / android.png / composite.png / android_mask.png
    """
    import numpy as np
    import cv2

    svc = _make(args)
    svc.start(timeout=args.timeout)
    _geometry(svc)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # webOS(Linux VM) 프레임
    web = cv2.imdecode(np.frombuffer(svc.screencap_bytes(fmt="png"), np.uint8),
                       cv2.IMREAD_COLOR)
    cv2.imwrite(str(out / "webos.png"), web)
    print(f"[i] webos.png  {web.shape[1]}x{web.shape[0]}")

    # Android 화면 — exec-out 의 raw 바이너리는 PC/adb 조합에 따라 깨진다(알려진 이슈).
    # 백엔드와 동일하게 **base64 경유**로 받는다.
    import base64 as _b64
    cmd = svc._adb_args("shell", "screencap -p | base64")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not r.stdout.strip():
        print(f"[!] Android 캡처 실패 rc={r.returncode} {r.stderr[:200]!r}")
        svc.stop()
        return 1
    try:
        raw = _b64.b64decode("".join(r.stdout.split()))
    except Exception as e:
        print(f"[!] base64 디코딩 실패: {e}")
        svc.stop()
        return 1
    andr = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if andr is None:
        print("[!] Android 캡처 디코딩 실패")
        svc.stop()
        return 1
    cv2.imwrite(str(out / "android.png"), andr)
    print(f"[i] android.png {andr.shape[1]}x{andr.shape[0]}")

    # 두 레이어 크기를 맞춘다(webOS 스트림은 -scale 축소본)
    h, w = web.shape[:2]
    if (andr.shape[1], andr.shape[0]) != (w, h):
        andr_r = cv2.resize(andr, (w, h), interpolation=cv2.INTER_AREA)
        print(f"[i] Android 를 {w}x{h} 로 리사이즈")
    else:
        andr_r = andr

    # 검정(=hole, webOS 가 비치는 영역)을 투명으로 보고 합성
    gray = cv2.cvtColor(andr_r, cv2.COLOR_BGR2GRAY)
    mask = (gray > args.threshold).astype(np.uint8)     # 1 = Android 가 그린 픽셀
    cv2.imwrite(str(out / "android_mask.png"), mask * 255)
    comp = web.copy()
    comp[mask == 1] = andr_r[mask == 1]
    cv2.imwrite(str(out / "composite.png"), comp)

    opaque = float(mask.mean()) * 100
    print(f"[i] composite.png 저장 — Android 불투명 픽셀 {opaque:.1f}% "
          f"(threshold={args.threshold})")
    if opaque > 80:
        print("[!] Android 캡처가 거의 전면 불투명합니다 — 검정 키 합성은 부적합할 수 있습니다"
              "(hole 이 검정으로 안 찍히는 경우).")
    elif opaque < 1:
        print("[!] Android 캡처가 거의 전부 검정입니다 — UI 가 안 잡혔을 수 있습니다.")
    print(f"[i] {out.resolve()} 의 4개 파일을 눈으로 비교해 보세요")
    _warn_if_contended(svc)
    svc.stop()
    return 0


def main() -> int:
    # 공통 옵션은 부모 파서로 둬서 하위 명령 앞/뒤 어느 쪽에 써도 먹게 한다.
    #   ⚠ parents= 로 재사용하면 **하위 파서의 기본값이 상위에서 준 값을 덮어쓴다**.
    #     (`--serial X tap ...` 이 무시되는 함정) → 기본값을 SUPPRESS 로 두고
    #     파싱 후에 빠진 것만 채운다.
    DEFAULTS = {
        "serial": "", "ip": "172.16.4.1", "user": "root", "password": "root",
        "scale": 2, "quality": 60, "fps": 30, "timeout": 60.0, "verbose": False,
        "no_kill": False,
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
    common.add_argument("--no-kill", action="store_true", default=S,
                        dest="no_kill",
                        help="기존 linuxStream 을 죽이지 않고 추가 기동(동시 사용 실측)")

    ap = argparse.ArgumentParser(
        description="WebOS(Connect Wide) 화면/터치 경로 단독 테스트",
        parents=[common],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="단계별 진단",
                   parents=[common]).set_defaults(func=cmd_probe)

    p = sub.add_parser("sh", help="Linux VM(기본) 또는 --android 에서 명령 실행",
                       parents=[common])
    p.add_argument("command", nargs="+")
    p.add_argument("--android", action="store_true", help="Linux VM 대신 HU(Android)에서 실행")
    p.set_defaults(func=cmd_sh)

    sub.add_parser("inputinfo", help="webOS 입력 장치 덤프(uinput 인식 여부)",
                   parents=[common]).set_defaults(func=cmd_inputinfo)

    p = sub.add_parser("evinject", help="실제 터치스크린 노드에 MT 이벤트 직접 주입",
                       parents=[common])
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--to", nargs=2, type=int, metavar=("X2", "Y2"),
                   help="지정 시 스와이프")
    p.add_argument("--space", choices=["panel", "mirror"], default="panel")
    p.add_argument("--device", default="", help="예: /dev/input/event0 (생략 시 자동 탐색)")
    p.add_argument("--hold", type=int, default=80)
    p.add_argument("--steps", type=int, default=10, help="스와이프 보간 단계")
    p.set_defaults(func=cmd_evinject)

    p = sub.add_parser("evtest", help="커널 입력 이벤트 캡처하며 터치 전송",
                       parents=[common])
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--space", choices=["panel", "mirror"], default="panel")
    p.add_argument("--device", default="", help="예: /dev/input/event5 (생략 시 자동 탐색)")
    p.add_argument("--hold", type=int, default=200, help="press~release 간격(ms)")
    p.add_argument("--seconds", type=int, default=4, help="캡처 시간(초)")
    p.set_defaults(func=cmd_evtest)

    p = sub.add_parser("android", help="Android 디스플레이/포커스/입력 확인",
                       parents=[common])
    p.add_argument("--keyevent", default="",
                   help="주입해볼 키(예: HOME, BACK) — Android 반응으로 input 동작 확인")
    p.set_defaults(func=cmd_android)

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
    p.add_argument("--via", choices=["evdev", "adb"], default="evdev")
    p.add_argument("--space", choices=["panel", "mirror"], default="panel",
                   help="좌표 기준: panel=3840x1440(기본) / mirror=저장 PNG 픽셀")
    p.add_argument("--hold", type=int, default=60,
                   help="press~release 간격(ms). 패킷 유실 의심 시 늘려본다")
    p.set_defaults(func=cmd_tap)

    p = sub.add_parser("touch", help="터치 패킷 1개만 전송(press/move/release)",
                       parents=[common])
    p.add_argument("type", choices=["press", "move", "release"])
    p.add_argument("x", type=int)
    p.add_argument("y", type=int)
    p.add_argument("--id", type=int, default=0, help="touch_id")
    p.add_argument("--space", choices=["panel", "mirror"], default="panel")
    p.add_argument("--wait", type=float, default=0.7, help="전송 후 로그 확인 대기(초)")
    p.set_defaults(func=cmd_touch)

    p = sub.add_parser("dual", help="스트림 2개 동시 기동 — 공존 가능 여부 실측",
                       parents=[common])
    p.add_argument("--seconds", type=int, default=10, help="관찰 시간(초)")
    p.set_defaults(func=cmd_dual)

    p = sub.add_parser("diag", help="스트림이 끊긴 직후 디바이스 상태 덤프",
                       parents=[common])
    p.add_argument("--wait", type=float, default=20.0, help="끊길 때까지 최대 대기(초)")
    p.set_defaults(func=cmd_diag)

    sub.add_parser("sshtest", help="Linux VM ssh 동시 세션 가능 여부 실측",
                   parents=[common]).set_defaults(func=cmd_sshtest)

    p = sub.add_parser("displays", help="Android 디스플레이 전수 캡처(UI 위치 탐색)",
                       parents=[common])
    p.add_argument("--out", default="webos_composite")
    p.add_argument("--threshold", type=int, default=16)
    p.set_defaults(func=cmd_displays)

    p = sub.add_parser("composite", help="webOS + Android 합성 미리보기 생성",
                       parents=[common])
    p.add_argument("--out", default="webos_composite")
    p.add_argument("--threshold", type=int, default=16,
                   help="이 밝기 이하를 '투명(hole)'으로 본다 (0~255)")
    p.set_defaults(func=cmd_composite)

    p = sub.add_parser("monitor", help="디스플레이 팝업 up/down (Android 논리 크기 변경)",
                       parents=[common])
    p.add_argument("state", choices=["up", "down"])
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("edge", help="가장자리→안쪽 스와이프(엣지 제스처)",
                       parents=[common])
    p.add_argument("side", choices=["left", "right", "bottom", "top"])
    p.add_argument("--ratio", type=float, default=0.85,
                   help="시작 지점의 위치 비율(좌우 엣지는 세로, 상하 엣지는 가로). 기본 0.85=하단쪽")
    p.add_argument("--length", type=int, default=900, help="쓸어내는 거리(px, 패널 기준)")
    p.add_argument("--duration", type=int, default=350)
    p.add_argument("--hold", type=int, default=0, help="시작점에서 잠시 누르고 이동")
    p.add_argument("--via", choices=["evdev", "adb"], default="evdev",
                   help="evdev=webOS(Linux VM) / adb=Android 레이어")
    p.set_defaults(func=cmd_edge)

    p = sub.add_parser("swipe", help="스와이프", parents=[common])
    p.add_argument("x1", type=int)
    p.add_argument("y1", type=int)
    p.add_argument("x2", type=int)
    p.add_argument("y2", type=int)
    p.add_argument("--duration", type=int, default=300)
    p.add_argument("--via", choices=["evdev", "adb"], default="evdev")
    p.add_argument("--space", choices=["panel", "mirror"], default="panel",
                   help="좌표 기준: panel=3840x1440(기본) / mirror=저장 PNG 픽셀")
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
