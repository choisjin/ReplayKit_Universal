#!/bin/sh
# 터치 디지타이저 실제 좌표 범위 확인 — display→digitizer 정확 스케일 도출용.
# 사용법: cmd /c "ssh root@192.168.1.4 sh -s < C:\ReplayKit\scripts\mib_touch_caps.sh > mib_caps.txt"
S(){ echo; echo "===== $1 ====="; }
export XDG_RUNTIME_DIR=/run/platform/weston
LMC=LayerManagerControl

S "touch 입력 디바이스 목록"
$LMC get input devices with touch 2>&1
$LMC get input devices with all 2>&1

S "각 touch 디바이스 capabilities (좌표 min/max 범위)"
# 이름이 위에서 나오면 그걸로 조회. 흔한 이름들도 시도.
for n in $($LMC get input devices with touch 2>/dev/null | grep -oE '[A-Za-z0-9_./ -]+' | sed 's/^ *//;s/ *$//' | grep -iE 'touch|screen|ts|input' | head -n 8); do
  echo "-- device: [$n]"
  $LMC get input device "$n" capabilities 2>&1
done

S "evdev 레벨 절대축 범위 (ABS_X/ABS_Y max = 디지타이저 네이티브 해상도)"
# /dev/input/event* 중 터치를 찾아 절대축 범위 출력 (evtest 있으면)
command -v evtest 2>/dev/null
for e in /dev/input/event0 /dev/input/event1 /dev/input/event2 /dev/input/event3 /dev/input/event4 /dev/input/event5; do
  [ -e "$e" ] || continue
  echo "-- $e: $(cat /sys/class/input/$(basename $e)/device/name 2>/dev/null)"
done
echo "-- /proc/bus/input/devices (ABS bitmap / 이름):"
cat /proc/bus/input/devices 2>/dev/null | grep -iE 'Name|Handlers|ABS' | head -n 40

echo
echo "===== CAPS DONE ====="
