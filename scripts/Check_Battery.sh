#!/bin/bash

# 1. dbus-send 명령어 실행 및 결과 저장
OUTPUT=$(dbus-send --system --print-reply --dest=vehicle.network.service /Chassis vehicle.network.service.Chassis.getRemainBattery 2>/dev/null)

# 2. 출력 결과에서 'byte' 뒤의 숫자 추출
BATTERY_VAL=$(echo "$OUTPUT" | awk '/byte/ {print $2}')

# 3. 예외 처리: 값을 가져오지 못했을 경우
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