# Linux 전용 플러그인 — TH / SCAR 사용 가이드

> 이 패키지는 Linux 환경에서만 동작한다 (`backend.app.plugins.linux.__init__` 에서 platform guard).
> Windows 빌드에서는 `module_service` 의 플러그인 발견 로직이 이 폴더 자체를 건너뛴다.

원본 reference: `Reference/Renault_CDC_Plugin/` (Robot Framework 키워드 모음)
설계 배경: 같은 폴더의 `../README.md` 가 아닌 프로젝트 플랜 파일 참조.

---

## 0. 사전 준비

- OS: Ubuntu 22.04 LTS 권장 (Wayland 환경이면 PySide6 패널이 X11 fallback 필요할 수 있음)
- Python 3.10 / 3.11
- `requirements.txt` 의 PySide6-Essentials, requests 이미 포함 — `./setup.sh` 로 venv 세팅
- TH 사용 시: `client.py` 가 있는 디렉터리 + TH 브로커 IP
- SCAR 사용 시: `docker` CLI 가 PATH 에, `scar` 컨테이너 또는 REST 서버(`http://localhost:8081`)가 떠 있어야 함
- (선택) SCAR reconnect 사용 시: `scar.sh` 의 절대경로

---

## 1. TH 플러그인 사용

### 1.1 인스턴스 생성

```python
from backend.app.plugins.linux.TH import TH

th = TH(
    client_dir="/home/cdc/0.scripts/th_client",        # client.py 위치
    th_addr="192.168.1.50",                             # TH 브로커 IP
    python_bin="python3",                               # client.py 인터프리터
    panel=True,                                         # PySide6 패널 사용 여부
    panel_trigger="GEAR_LEVER_ACCEPTED_T_REVERSE",      # stdout 에서 찾을 토큰
)
```

> ReplayKit 시나리오 편집기에서 디바이스로 등록하면 `connect_type="none"` 이라 호스트 입력란 없이
> 위 생성자 인자들이 module config 에서 채워진다 (`module_service` 자동 인식).

### 1.2 메서드

| 메서드 | 설명 | 반환 |
|---|---|---|
| `Send(topic_name, json_path, timeout=10)` | 신호만 전송. 패널 갱신 없음. | `"rc=<n>\n<stdout 끝부분>"` |
| `SendAndUpdate(topic_name, json_path, timeout=10, trigger=None)` | trigger 토큰 감지 시 패널 점등. `trigger=None` 이면 생성자의 `panel_trigger` 사용. | `"rc=<n> trigger_hit=<tok> e2e_ms=<x.xx>\n..."` 또는 `"FAIL: trigger '...' not detected ..."` |
| `PanelShow()` | 빈 검정 패널만 띄움 (원본 `Show TK Panel` 등가). | `"ok"` |
| `PanelReset()` | 노란색 → 검정. 라벨 숨김. | `"ok"` |
| `PanelClose()` | 패널 호스트 프로세스 종료. | `"ok"` |

### 1.3 시나리오 예시

```python
# RVC_Performance 의 한 사이클 핵심 부분
th.Send(
    "some_ip_broker_com_sdv_ampere_common_fnd_sdv_energy_power_distribution_vehicle_power_mode_unique",
    "/home/cdc/0.scripts/runJSON/PowerOn.json",
)
# 1초 대기
th.Send(
    "some_ip_broker_com_sdv_ampere_common_fnd_sdv_vehicle_dynamics_speed_info_timestamped_unique",
    "/home/cdc/0.scripts/runJSON/SpeedInfoTimestampedRVC.json",
)
# delay ms 후
result = th.SendAndUpdate(
    "some_ip_broker_com_sdv_ampere_common_fnd_sdv_powertrain_gear_lever_position_accepted_unique",
    "/home/cdc/0.scripts/runJSON/com_sdv_ampere_common_fnd_sdv_powertrain_gear_lever_PositionAccepted.json",
    timeout=10,
)
# result 예: "rc=0 trigger_hit=GEAR_LEVER_ACCEPTED_T_REVERSE e2e_ms=23.41\n..."
```

### 1.4 패널 동작 방식

원본 `TH_Lib.py` 가 사용하던 tkinter 는 ReplayKit 메인 루프와 충돌하는 사례가 있어 **별도 프로세스**의 PySide6 위젯으로 격리한다.

- 첫 번째 패널 메서드 호출 시 `PanelClient` 가 `python -m backend.app.plugins.linux.common.th_panel_host --socket /tmp/replaykit-th-panel-<pid>.sock --width 300 --height 300` 으로 호스트를 spawn.
- 통신: Unix domain socket, 1바이트 opcode.
  - `0x01` = highlight (배경 노란색 + 현재 wall-clock 라벨)
  - `0x02` = reset    (검정 + 라벨 hidden)
  - `0x03` = shutdown
- 호스트 프로세스는 좌하단 (스크린 좌표 `(0, screen.height-300)`) 300x300, frameless, top-most, `WA_ShowWithoutActivating` (포커스 도둑 방지).
- 호스트가 죽으면 다음 호출에서 자동 재시작 — 1분 윈도우 내 최대 3회까지 (`PanelClient._RESPAWN_MAX`).

### 1.5 지연 경로 (`SendAndUpdate`)

```
client.py stdout 1 byte 도착
  → proc.scan_until 의 os.read() 즉시 깨어남
  → rolling buffer 에서 trigger 검출 → on_match 콜백
  → PanelClient.highlight() → socket.sendall(b'\x01')  (1 syscall)
  → 호스트의 QSocketNotifier activated 시그널
  → PanelWindow.highlight() → setStyleSheet + repaint
```

같은 머신에서 측정 시 검출-패널 점등 사이 **수 ms 이내**가 목표. `e2e_ms` 는 client.py spawn 시점부터 trigger 검출까지 총 시간.

---

## 2. SCAR 플러그인 사용

### 2.1 인스턴스 생성

```python
from backend.app.plugins.linux.SCAR import SCAR

scar = SCAR(
    api_base="http://localhost:8081",                  # SCAR REST 서버
    container="scar",                                  # docker container 이름
    reconnect_script="/home/scar/scar.sh",             # (선택) 재기동 스크립트 절대경로
    reconnect_args="-t 2.2.0 --ui --arti tls",         # (선택) 공백 구분 인자 문자열
    reconnect_cwd="/home/scar",                        # (선택) 스크립트 cwd
    reconnect_wait_s=20.0,                             # 재기동 후 대기 (원본 'Wait 20')
)
```

### 2.2 메서드

| 메서드 | 설명 | 반환 |
|---|---|---|
| `Ready(max_retry=3)` | API → DOCKER → reconnect 순으로 모드 자동 판별. | `"API"` / `"DOCKER"` / `"NONE"` |
| `SendApi(url, headers, data, max_retry=3)` | REST POST. headers/data 는 JSON 문자열. | `"status=<n>\n<body 512자>"` 또는 `"FAIL: ..."` |
| `Exec(cmd, timeout=300, max_retry=3)` | `docker exec scar bash -c <cmd>`. | `"rc=<n>\n<stdout 2KB>"` 또는 `"FAIL: ..."` |
| `Reconnect()` | `setsid <reconnect_script> ... &` + 대기. | `"DOCKER"` / `"FAIL: ..."` |

### 2.3 자동 판별 동작

`Ready()` 의 흐름은 원본 `Ensure SCAR Is Ready` 와 동일:

```
for attempt in 0..max_retry-1:
  if force_docker_mode:   return "DOCKER"
  if api.is_alive():      return "API"
  if docker.is_running(): return "DOCKER"
  reconnect()             # force_docker_mode = True
return "NONE"
```

핵심: `force_docker_mode` 는 인스턴스 상태로 유지된다. 한 번이라도 reconnect 가 일어났다면 같은 SCAR 인스턴스에서는
**더 이상 API 를 시도하지 않고 바로 DOCKER 로 직행**. 시나리오 중간에 SCAR 컨테이너가 다운돼서 재기동된 경우를
보호하려는 원본 의도이다.

`SendApi` 는 모드가 `API` 가 아니면 명시적으로 `FAIL` — Docker 동등 동작이 필요한 호출은 `Exec()` 로 직접 부른다.

### 2.4 시나리오 예시

```python
mode = scar.Ready()                       # "API" / "DOCKER" / "NONE"

if mode == "API":
    scar.SendApi(
        "http://localhost:8081/utc_time/start",
        headers='{"Content-Type":"application/json"}',
        data='{"value":"now"}',
    )
elif mode == "DOCKER":
    scar.Exec(
        "cd /home/scar && ENDS=2025_r10 ./scripts/test_sequences/pnp/sleep_power_sequence.sh off --frontend",
        timeout=60,
    )
# NONE 이면 SCAR 부분 건너뛰기
```

---

## 3. 사전 점검 체크리스트

배포 전에 한 번씩 확인해야 할 항목.

### 3.1 환경 의존성

- [ ] `python3 -c "from PySide6.QtWidgets import QApplication; print('ok')"` 이 OK 인지
- [ ] `docker version` 이 정상 응답하는지 (SCAR 사용 시)
- [ ] `requests.get("http://localhost:8081/")` 가 200/404 등 응답을 받는지 (SCAR API 사용 시)
- [ ] TH `client.py` 가 있는 디렉터리 권한 + 실행 권한
- [ ] Wayland 세션이면 `XDG_SESSION_TYPE=x11` 또는 `GDK_BACKEND=x11` 로 ReplayKit 실행 (PySide6 frameless top-most 동작 보장)

### 3.2 자동 테스트 (pytest, Linux 머신에서)

```bash
cd /path/to/ReplayKit_Universal
. .venv/bin/activate
pip install pytest                        # 아직 없다면
pytest test/plugins/linux -v
```

검증되는 시나리오:
- `test_proc.py` — `scan_until` 이 줄바꿈 없이 토큰을 감지하는지, timeout 동작, process group 종료
- `test_th_signal.py` — 더미 client 로 trigger 즉시 감지 + e2e_ms 측정 (목표 1500ms 이내)
- `test_scar_health.py` — API/DOCKER/NONE 상태 전이 4가지 케이스 mock 기반 검증

> `test_th_signal.py` 는 sub-shell 로 dummy client.py 를 실행하므로 SCAR/TH 실서버 없이도 통과.

### 3.3 수동 통합 테스트

순서대로 확인:

1. **module 등록 확인**
   ```python
   from backend.app.services import module_service
   names = [m["name"] for m in module_service.list_available_modules()]
   assert "TH" in names and "SCAR" in names
   ```

2. **TH 패널 띄우기만**
   ```python
   from backend.app.plugins.linux.TH import TH
   th = TH(client_dir="/tmp", th_addr="127.0.0.1", panel=True)
   th.PanelShow()                          # 좌하단 검정 300x300 패널 등장
   th.PanelReset()
   th.PanelClose()
   ```
   기대: 패널이 떴다가 사라짐. 콘솔에 에러 없음. Wayland 면 X11 fallback 필요할 수 있음.

3. **TH 패널 점등 latency 측정**
   - dummy client.py 를 만들어 `sleep 0.5 && echo GEAR_LEVER_ACCEPTED_T_REVERSE` 만 출력하게 함
   - `th.SendAndUpdate("dummy", "/dev/null", timeout=3)` 호출
   - 기대: 패널이 노란색으로 점등 + 현재 시각 라벨. 반환 문자열의 `e2e_ms` 가 약 500ms 근처 + 작은 오버헤드 (가능하면 < 50ms 추가).

4. **SCAR API 경로**
   - `scar` 컨테이너가 떠있고 `http://localhost:8081/` 가 응답하는 상태
   - `scar.Ready()` → `"API"`
   - `scar.SendApi("http://localhost:8081/<endpoint>", headers='{}', data='{}')` → `status=200` 또는 서버가 주는 코드

5. **SCAR DOCKER 경로**
   - REST 서버를 죽이거나 응답하지 않게 만들고 컨테이너만 살려둠
   - `scar.Ready()` → `"DOCKER"`
   - `scar.Exec("echo hello")` → `rc=0\nhello`

6. **SCAR reconnect 경로**
   - 컨테이너 중지: `docker stop scar`
   - `scar.Ready()` 호출 → reconnect_script 실행 시도, 20초 대기 후
     - 스크립트가 정상 동작했으면 `"DOCKER"`
     - 스크립트가 없거나 실패하면 force_docker_mode=True 상태로 `"DOCKER"` 반환되지만 실제 `Exec` 는 `FAIL: SCAR container not running` 으로 떨어짐
   - 같은 인스턴스로 `scar.Ready()` 재호출 → 무조건 `"DOCKER"` (API 재시도 안 함)

7. **RVC_Performance 1사이클 재현**
   - 원본 `Reference/Renault_CDC_Plugin/RVC_Performance.txt` 의 한 cycle 을 ReplayKit 시나리오로 작성
   - 기대: MCU 시리얼에서 `PS:SHUTDOWN_DONE->OFF. ACT_Done:0x00000040` 수신, Arduino `RVC_START` 전송, 패널 점멸 발생

### 3.4 회귀 점검 (Windows 빌드)

Windows 빌드는 이 패키지를 import 하면 즉시 ImportError. 회귀 가능성이 있는 곳:
- [ ] Windows 에서 `module_service.list_available_modules()` 호출 시 TH/SCAR 가 목록에 없는지 (있으면 안 됨)
- [ ] Windows 에서 시나리오 파일에 TH/SCAR 호출이 박혀 있어도 import 실패가 명확한 에러 메시지로 노출되는지

---

## 4. 트러블슈팅

### `ImportError: backend.app.plugins.linux is Linux-only (current platform: win32)`
정상 동작. Windows 에서는 패키지 자체가 비활성. 시나리오에 TH/SCAR 가 박혀 있다면 Linux PC 에서 재생해야 한다.

### `FAIL: TH client spawn error: ...`
`client_dir` 에 `client.py` 가 없거나 실행 권한 문제. 더 흔한 케이스는 `python_bin` 이 올바른 venv 의 인터프리터가 아닌 것.

### `FAIL: trigger '...' not detected (timeout=10s) rc=0`
- client.py 가 trigger 토큰을 stdout 에 찍지 않음. `--log_level DEBUG` 로 출력이 충분한지 client.py 측에서 확인.
- 또는 stderr 에만 찍히는 경우 — 현재 구현은 `stderr=STDOUT` 으로 합쳐 읽으므로 stderr 도 잡힘. 그래도 안 잡히면 client.py 가 line-buffer 가 아닌 block-buffer (예: `python -u` 미지정) 인지 확인.

### 패널이 안 뜸 / 검은 사각형만 나옴
- Wayland 세션 여부 확인: `echo $XDG_SESSION_TYPE`. `wayland` 면 X11 으로 강제: `GDK_BACKEND=x11 ./ReplayKit.sh`.
- `python -m backend.app.plugins.linux.common.th_panel_host --socket /tmp/test.sock --width 300 --height 300` 로 단독 실행해서 띄워보면 원인이 PySide6 자체인지 IPC 인지 분리됨.

### `FAIL: SCAR API down, use Exec() in docker mode`
원본 의도. `SendApi` 는 API 모드 전용. Docker 모드에서는 같은 결과를 `Exec` 로 명시적으로 호출.

### `FAIL: reconnect_script not configured`
`SCAR(..., reconnect_script="/abs/path/to/scar.sh", reconnect_cwd="/home/scar")` 로 생성자에서 명시.

### Docker exec 가 매우 느림
`SCARDocker.exec` 의 `timeout` 기본 300초. 짧게 끊고 싶다면 `scar.Exec(cmd, timeout=30)` 처럼 호출 단위로 조절.

---

## 5. 파일 매핑 요약

| 컴포넌트 | 파일 | 책임 |
|---|---|---|
| 시나리오 노출 | `TH.py`, `SCAR.py` | 사용자 API. `"FAIL: ..."` 반환 규약 |
| Subprocess 헬퍼 | `common/proc.py` | raw-fd 스캐너, process group terminate |
| TH 신호 | `common/th_signal.py` | client.py 실행 + trigger 검출 |
| TH 패널 호스트 | `common/th_panel_host.py` | PySide6 별도 프로세스, opcode 처리 |
| TH 패널 IPC | `common/th_panel_client.py` | lazy spawn + Unix socket |
| SCAR REST | `common/scar_api.py` | requests.Session 재사용 |
| SCAR Docker | `common/scar_docker.py` | inspect/exec/start_via_script |
| SCAR 상태머신 | `common/scar_health.py` | ensure_ready, force_docker_mode |
