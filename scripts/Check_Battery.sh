#!/bin/bash

# dbus-send 배터리 조회 명령 (재사용 위해 변수화)
DBUS_CALL='dbus-send --system --print-reply --dest=vehicle.network.service /Chassis vehicle.network.service.Chassis.getRemainBattery'

# 1. dbus-send 명령어 실행 및 결과 저장
OUTPUT=$($DBUS_CALL 2>/dev/null)

# 1-1. dbus-send 가 막혀(SMACK 라벨/권한 손상 → Permission denied) 빈 응답이면
#      벤더 복구 스크립트를 1회 실행하고 재시도한다.
if [ -z "$OUTPUT" ]; then
    if [ -f /app/bin/send_dbus.sh ]; then
        /app/bin/send_dbus.sh >/dev/null 2>/dev/null || sh /app/bin/send_dbus.sh >/dev/null 2>/dev/null
    fi
    OUTPUT=$($DBUS_CALL 2>/dev/null)
fi

# 2. 출력 결과에서 'byte' 뒤의 숫자 추출
BATTERY_VAL=$(echo "$OUTPUT" | awk '/byte/ {print $2}')

# 3. 예외 처리: 값을 가져오지 못했을 경우 (복구 후에도 실패)
if [ -z "$BATTERY_VAL" ]; then
    echo "Error: Failed to get battery value."
    exit 1
fi

# 4. 값 비교 (70 기준)
if [ "$BATTERY_VAL" -ge 70 ]; then
    echo "PASS"
else
    echo "FAIL"
fi
