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
    return WebOSStreamService(
        adb_serial=serial, linux_ip=args.ip, linux_user=args.user,
        linux_password=args.password, scale=args.scale, quality=args.quality,
        fps=args.fps, idle_timeout=0, device_id="webos_test",
    )


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
