# 디바이스 점검 스크립트 요청 양식 (AI에게 줄 컨텍스트)

이 문서는 **코딩을 몰라도** `Check_Battery.sh` 같은 "디바이스에서 명령을 실행하고
PASS/FAIL을 판정하는 셸 스크립트"를 AI에게 정확하게 만들게 하기 위한 양식입니다.

> 사용법: 아래 **[복사용 양식]** 블록을 복사 → 빈칸(`<< >>`)을 채워서 AI에게 그대로 붙여넣기 →
> AI가 규칙에 맞는 `.sh` 스크립트를 만들어 줍니다.

---

## [복사용 양식]

```
아래 "AI 작성 규칙"을 지켜서 디바이스 점검용 bash 스크립트를 하나 만들어줘.

1) 스크립트 파일명:
   << 예: Check_Battery.sh >>

2) 무엇을 확인하고 싶은가 (평범한 말로):
   << 예: 차량 배터리 잔량이 70 이상인지 확인하고 싶다 >>

3) 디바이스에서 실제로 실행할 명령 (알면 그대로, 모르면 원하는 동작 설명):
   << 예: dbus-send --system --print-reply --dest=vehicle.network.service /Chassis vehicle.network.service.Chassis.getRemainBattery >>

4) 결과를 어떻게 성공/실패로 가르나 (아래 중 하나 골라 채우기):
   [ ] (A) 특정 단어가 나오면 성공      → 성공 단어: << 예: ready >>
   [ ] (B) 숫자가 기준과 비교해서 성공   → 결과에서 읽을 값: << 예: 'byte' 뒤의 숫자 >>
                                          기준값: << 예: 70 >>  방향: << 이상 / 이하 >>
   [ ] (C) 명령이 그냥 오류 없이 끝나면 성공 (별도 값 비교 없음)

5) 판정 결과로 출력할 단어 (기본 PASS / FAIL):
   성공 시: << PASS >>   실패 시: << FAIL >>

6) (선택) 명령이 막히거나 실패했을 때 먼저 1회 실행할 복구 명령이 있나?:
   << 예: /app/bin/send_dbus.sh  (없으면 "없음") >>

7) (선택) 특이사항:
   << 예: root로 실행됨 / dbus-send는 /usr/bin 절대경로 필요 등. 없으면 비워둠 >>
```

---

## 작성 예시 (위 양식을 Check_Battery로 채운 모습)

```
1) 파일명: Check_Battery.sh
2) 확인할 것: 차량 배터리 잔량이 70 이상인지
3) 실행할 명령: dbus-send --system --print-reply --dest=vehicle.network.service /Chassis vehicle.network.service.Chassis.getRemainBattery
4) 판정: (B) 숫자 비교 / 읽을 값: 'byte' 뒤의 숫자 / 기준 70 / 방향: 이상
5) 출력: 성공 PASS, 실패 FAIL
6) 복구 명령: /app/bin/send_dbus.sh
7) 특이사항: root 실행
```

---

## AI 작성 규칙 (생성되는 스크립트가 반드시 지켜야 할 것)

이 환경(ReplayKit + SSHManager `bash -s`로 차량/리눅스 디바이스에서 원격 실행)에 맞추기 위한 규칙.

1. **`#!/bin/bash` 셰뱅으로 시작**, 평이한 문법만 사용.
2. **줄바꿈은 LF(Unix)**. (백엔드가 CRLF→LF 정규화하지만, 생성 시에도 LF 권장)
3. **출력은 판정 단어 한 줄**만 명확히: 성공 시 지정한 성공 단어(예 `PASS`), 실패 시 실패 단어(예 `FAIL`).
   - ⚠️ 이 단어는 `SSHManager::Check`의 `expected`와 **대소문자까지 정확히** 일치해야 함
     (`contains`도 대소문자 구분). → 성공 단어는 대문자 `PASS`로 통일 권장.
4. **명령 결과가 비어 있으면**(권한/디바이스 문제로 막힘) 곧장 실패로 처리하지 말고:
   - 6번 복구 명령이 있으면 **1회 실행 후 재시도**, 그래도 비면 `Error: ...` 출력 + `exit 1`.
   - 복구 명령이 없으면 바로 `Error: ...` + `exit 1`.
5. **숫자 비교(B형)** 는 결과에서 값을 추출(`awk` 등) → 비어있는지 먼저 확인 → 기준과 `-ge`/`-le`로 비교.
6. **절대경로 권장**: 핵심 명령이 비대화형 셸 PATH에서 안 잡힐 수 있으므로, 위치를 알면 절대경로
   (예 `/usr/bin/dbus-send`)로 호출.
7. **`2>&1` 금지** — stderr를 stdout과 합치지 말 것. 에러를 버릴 땐 `2>/dev/null`로 분리 유지.
8. 복구 명령 호출은 **직접 실행 후 `sh` 폴백** 형태로(실행권한/셰뱅 차이 대비):
   `/app/bin/foo.sh >/dev/null 2>/dev/null || sh /app/bin/foo.sh >/dev/null 2>/dev/null`

---

## 생성된 스크립트 실행/판정 방법

저장 위치: 백엔드가 읽는 경로(배포본 `C:\ReplayKit\scripts\`)에 둘 것.
(개발 repo `E:\...\scripts\`만 고치면 배포본엔 반영 안 됨 — 두 곳을 동기화)

- **그냥 실행(결과 보기):**
  ```
  SSHManager::send_command(bash -s < "C:\ReplayKit\scripts\<파일명>.sh")
  ```
- **합부 판정(PASS 검사):**
  ```
  SSHManager::Check(command=bash -s < "C:\ReplayKit\scripts\<파일명>.sh", expected=PASS, match_mode=contains, timeout=60)
  ```
  주의: `command=` 값을 **바깥 따옴표로 또 감싸지 말 것**. 경로만 `"..."`로 감쌈.

- **여러 단어 and/or 판정:**
  ```
  SSHManager::Check_Logic(command=bash -s < "...\<파일명>.sh", keywords=PASS, logic=and, timeout=60)
  ```
