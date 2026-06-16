#!/bin/sh
# MIB 터치 패널(디지타이저) 실제 좌표 범위 확인 — fcntl 없는 임베디드 python3 대응(ctypes libc.ioctl 직접 호출).
# /dev/input/event* 의 ABS_X/Y, ABS_MT_POSITION_X/Y min/max 를 EVIOCGABS ioctl로 읽는다.
# 이 max 가 ksend 디지타이저 좌표공간의 실제 상한(화면해상도÷divisor 추정의 정답지).
# 사용법: cmd /c "ssh root@<IP> sh -s < E:/Project/ReplayKit_Universal/scripts/mib_touch_check.sh"
python3 - <<'PY'
import glob, struct, os, ctypes

libc = ctypes.CDLL(None, use_errno=True)
libc.ioctl.restype = ctypes.c_int
libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]

ABSINFO_SIZE = 24  # struct input_absinfo: value,min,max,fuzz,flat,resolution = 6*int32
def EVIOCGABS(a):  return (2 << 30) | (ABSINFO_SIZE << 16) | (0x45 << 8) | (0x40 + a)
def EVIOCGNAME(l): return (2 << 30) | (l << 16) | (0x45 << 8) | 0x06

CODES = [("ABS_X",0x00), ("ABS_Y",0x01), ("ABS_MT_POSITION_X",0x35), ("ABS_MT_POSITION_Y",0x36)]

for dev in sorted(glob.glob("/dev/input/event*")):
    try:
        fd = os.open(dev, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        print("%s: open fail (%s)" % (dev, e)); continue
    try:
        nb = ctypes.create_string_buffer(256)
        name = "?"
        if libc.ioctl(fd, EVIOCGNAME(256), nb) >= 0:
            name = nb.value.decode("utf-8", "replace")
        ranges = []
        for label, code in CODES:
            b = ctypes.create_string_buffer(ABSINFO_SIZE)
            if libc.ioctl(fd, EVIOCGABS(code), b) == 0:
                value, mn, mx, fuzz, flat, res = struct.unpack("iiiiii", b.raw)
                if mn != 0 or mx != 0:
                    ranges.append((label, mn, mx, res))
        if ranges:
            print("%s  name=%r" % (dev, name))
            for label, mn, mx, res in ranges:
                print("    %-20s min=%-6d max=%-6d resolution=%d" % (label, mn, mx, res))
    finally:
        os.close(fd)
print("DONE")
PY
