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

## 1. ReplayKit 모듈 연결 흐름 (사용자 관점)

ReplayKit 의 다른 플러그인과 동일한 패턴을 따른다. 코드를 직접 import 해서 쓰는 게 아니라
**디바이스로 등록** → 시나리오에서 **모듈 호출 노드**로 부른다.

### 1.1 한 번만 — `requirements.txt` 설치 / 실행

```bash
./setup.sh                # venv + PySide6 + requests + robotframework 설치
./ReplayKit.sh            # 백엔드 + 프론트엔드 기동
# 또는 dev 모드
./dev-run.sh
```

기동 후 브라우저로 `http://localhost:5173` (dev) 또는 `http://localhost:8000` (prod) 접속.

### 1.2 디바이스 등록 (UI)

> Linux 에서 ReplayKit 을 띄우면 `module_service` 가 `plugins/linux/` 서브폴더를 자동 스캔해서
> 디바이스 추가 폼의 모듈 드롭다운에 **TH**, **SCAR** 가 노출된다.

**TH 디바이스 추가 — radmoon 스캔 경로 (권장)**

`Reference/TH/connect_th.sh` 기준의 셋업을 ReplayKit 폼에 옮긴 형태. 호스트 PC 에 USB Ethernet 어댑터
(통칭 *radmoon*) 가 꽂혀 있으면 자동 발견된다.

1. 보조 디바이스 추가 → **스캔** 실행
2. 결과에서 `radmoon (TH)` 탭 선택 — `enx<mac>` 형태의 USB Ethernet 어댑터들이 나열됨
3. 사용할 어댑터의 **연결** 버튼 → TH 모듈 폼으로 자동 전환되고 아래 필드가 채워진 상태로 열림

| 필드 | 자동 채움 값 | 비고 |
|---|---|---|
| eth_if | (스캔 결과 인터페이스, 예: `enx00e04c68b2c8`) | radmoon 행에서 결정 |
| th_root | `/home/cdc/Desktop/TH` | `host_ends_setup.sh` / `ensure-adb.sh` 가 있는 폴더 |
| host_ip | `192.168.1.152/24` | `connect_th.sh` 기본값 |
| cvd_br | `cvd-ebr` | bridge 이름 |
| rbvm_ip | `192.168.140.1:5555` | RBVM ADB target |
| th_adb | `0.0.0.0:6520` | CVD ADB host:port |
| grpc_ip | `192.168.1.99:50051` | gRPC broker, client.py `--ip_address` |
| python_bin | `python3` | client.py 실행 인터프리터 |
| panel | `True` | PySide6 시각화 패널 사용 |
| panel_trigger | `GEAR_LEVER_ACCEPTED_T_REVERSE` | 패널 점등 토큰 |
| auto_setup | `True` | 등록 시 자동으로 `Setup()` 실행 |

4. **th_home 만 수동 입력** — `connect_th.sh` 의 `select_th_gui.py` 가 하던 역할. TH 버전별 압축을 푼 디렉터리
   절대경로 (예: `/home/cdc/Desktop/TH/TH_0.60.12`). `<th_home>/bin/launch_cvd` 와
   `<th_home>/harness/harness/grpc_client/src/client.py` 가 존재해야 한다.
5. 디바이스 이름 (예: `TH_main`) → **연결**
6. **연결 직후 자동 Setup 실행** (`auto_setup=True` 일 때).

   `connect_th.sh` step 1-3 (네트워크 셋업 + ADB ensure) 와 동일한 순서로 진행:

   ```
   [1] bridge network    sudo ip addr del <old> dev cvd-ebr (있을 때)
                         sudo ip addr add 192.168.1.152/24 dev cvd-ebr
                         sudo ip link set enx... master cvd-ebr
   [2] host_ends_setup.sh <eth_if>     (th_root 에 있을 때만)
   [3] ensure-adb.sh <eth_if>          (th_root 에 있을 때만)
   [4] adb devices                     RBVM 확인 + 디바이스 ≥ 2 검증
   ```

   결과는 등록 응답 메시지에 그대로 노출되어 GUI 토스트에 표시된다:
   - 성공: `Module device TH added (ID: TH_main) — ok\n[1] bridge network: ...`
   - 실패: `Module device TH added (ID: TH_main) — FAIL: bridge network setup\n[1] bridge network:\n  add 192.168.1.152/24 failed: sudo: a password is required`

   `IsConnected()` 가 True 가 되어 디바이스 상태 "connected" 로 표시된다.
   실패하면 디바이스는 등록되지만 상태는 disconnected — `Setup()` 메서드를 수동 재호출하거나 설정을
   수정 후 재연결.

   **sudo 요구**: `_setup_bridge_network` 와 `host_ends_setup.sh` / `ensure-adb.sh` 가 모두 `sudo -n`
   (비대화형) 으로 호출되므로, ReplayKit 백엔드를 실행하는 계정이 `ip` 와 두 스크립트에 대해
   **passwordless sudo** 권한을 가져야 한다. 예시 `/etc/sudoers.d/replaykit-th`:
   ```
   cdc ALL=(root) NOPASSWD: /usr/sbin/ip
   cdc ALL=(root) NOPASSWD: /home/cdc/Desktop/TH/host_ends_setup.sh
   cdc ALL=(root) NOPASSWD: /home/cdc/Desktop/TH/ensure-adb.sh
   ```
   설정 안 되어 있으면 step 1 에서 `sudo: a password is required` 로 즉시 FAIL.

7. **자동 Setup 비활성화** — `auto_setup=False` 로 등록하면 연결 시 setup 을 건너뛴다.
   네트워크/ADB 가 이미 다른 방식으로 준비된 경우, 또는 수동으로 시나리오에서 `TH.Setup()` 노드를
   호출하고 싶을 때 사용.

> 등록 시점에 실행되는 건 step 1-3 까지. **launch_cvd / microservice run** (`connect_th.sh` step 4-5)
> 은 ReplayKit 이 자동화하지 않는다 — 별도 터미널에서 직접 실행하거나, 후속 단계에서 `Launch()` 메서드를
> 추가할 계획.

**SCAR 디바이스 추가**

SCAR 도 스캔 가능 (`docker inspect scar` + `GET http://localhost:8081/`). 결과 행의 **연결** 버튼이면 충분.

스캔이 후보를 못 찾는 경우 수동 추가:

| 폼 필드 | 입력값 예시 | 비고 |
|---|---|---|
| Module | `SCAR (SDV Control, Linux)` | 드롭다운 선택 |
| SCAR REST URL | `http://localhost:8081` | 기본값 그대로 두면 됨 |
| Docker container 이름 | `scar` | 기본값 |
| 재기동 스크립트 (절대경로) | `/home/scar/scar.sh` | 빈 칸이면 `Reconnect()` 호출 시 FAIL |
| 재기동 스크립트 인자 | `-t 2.2.0 --ui --arti tls` | 공백 구분 |
| 재기동 스크립트 cwd | `/home/scar` | 스크립트 실행 working dir |
| 재기동 후 대기 (초) | `20` | 원본 Robot `Wait 20` 보존 |

### 1.3 시나리오에서 호출

ReplayKit 시나리오 편집기의 "모듈 명령" 노드에서:

```
device: <위에서 등록한 TH 디바이스>
module : TH
function: SendAndUpdate
args:
  topic_name = some_ip_broker_com_sdv_ampere_common_fnd_sdv_powertrain_gear_lever_position_accepted_unique
  json_path  = /home/cdc/0.scripts/runJSON/GearLeverPositionAccepted.json
  timeout    = 10
```

반환문자열이 `"FAIL:"` 로 시작하면 ReplayKit 이 해당 step 을 자동으로 실패 처리하고,
그렇지 않으면 통과로 기록한다 (SHELL/CMD 와 동일 규약).

### 1.4 (디버깅용) 코드에서 직접 호출

위 디바이스 등록 없이 단위 검증만 하고 싶을 때:

```python
# Linux 셸에서 ReplayKit venv 활성화 후
python -c "
import sys; sys.path.insert(0, '.')
from backend.app.plugins.linux.TH import TH
th = TH(
    eth_if='enx00e04c68b2c8',
    th_home='/home/cdc/Desktop/TH/TH_0.60.12',
    panel=False,
)
print(th.Info())                                    # 현재 설정 + client.py 존재여부 요약
print(th.Send('topic_name_here', '/path/to/payload.json', timeout=5))
"
```

`panel=False` 로 두면 PySide6 호스트가 spawn 되지 않아 headless 환경에서도 동작한다.
나머지 네트워크 디폴트(`host_ip`/`grpc_ip` 등) 는 `connect_th.sh` 와 동일 값이 자동 적용.

---

## 2. TH 플러그인 메서드 상세

### 2.1 인스턴스 생성 (코드 직접 호출 시)

```python
from backend.app.plugins.linux.TH import TH

th = TH(
    # ── 필수 (radmoon scan + 수동 선택) ──
    eth_if="enx00e04c68b2c8",                                  # USB Ethernet (radmoon)
    th_home="/home/cdc/Desktop/TH/TH_0.60.12",                 # 압축 푼 TH 버전 디렉터리
    # ── connect_th.sh USER CONFIG 디폴트 (override 가능) ──
    host_ip="192.168.1.152/24",
    cvd_br="cvd-ebr",
    rbvm_ip="192.168.140.1:5555",
    th_adb="0.0.0.0:6520",
    grpc_ip="192.168.1.99:50051",                              # client.py --ip_address
    # ── 플러그인 동작 ──
    python_bin="python3",
    panel=True,
    panel_trigger="GEAR_LEVER_ACCEPTED_T_REVERSE",
)
```

내부 도출:
- `self.client_dir = <th_home>/harness/harness/grpc_client/src`
- `self.th_addr   = grpc_ip`

### 2.2 메서드

| 메서드 | 설명 | 반환 |
|---|---|---|
| `Send(topic_name, json_path, timeout=10)` | 신호만 전송. 패널 갱신 없음. | `"rc=<n>\n<stdout 끝부분>"` |
| `SendAndUpdate(topic_name, json_path, timeout=10, trigger=None)` | trigger 토큰 감지 시 패널 점등. `trigger=None` 이면 생성자의 `panel_trigger` 사용. | `"rc=<n> trigger_hit=<tok> e2e_ms=<x.xx>\n..."` 또는 `"FAIL: trigger '...' not detected ..."` |
| `PanelShow()` | 빈 검정 패널만 띄움 (원본 `Show TK Panel` 등가). | `"ok"` |
| `PanelReset()` | 노란색 → 검정. 라벨 숨김. | `"ok"` |
| `PanelClose()` | 패널 호스트 프로세스 종료. | `"ok"` |
| `Setup()` | `connect_th.sh` step 1-3 (네트워크 + ADB) 수동 재실행. 등록 시 자동 호출되는 것과 동일. | `"ok\n<log>"` 또는 `"FAIL: ...\n<log>"` |
| `Connect()` | device_manager 가 자동 호출. `auto_setup=True` 면 `Setup()` 위임. | Setup 결과 |
| `IsConnected()` | device_manager 가 상태 확인용으로 호출. Setup 한 번 성공이면 True. | bool |
| `Info()` | 현재 설정 + Setup 상태 + 스크립트 존재여부 요약. 디버그용. | 여러 줄 텍스트 |

### 2.3 시나리오 예시

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

### 2.4 패널 동작 방식

원본 `TH_Lib.py` 가 사용하던 tkinter 는 ReplayKit 메인 루프와 충돌하는 사례가 있어 **별도 프로세스**의 PySide6 위젯으로 격리한다.

- 첫 번째 패널 메서드 호출 시 `PanelClient` 가 `python -m backend.app.plugins.linux.common.th_panel_host --socket /tmp/replaykit-th-panel-<pid>.sock --width 300 --height 300` 으로 호스트를 spawn.
- 통신: Unix domain socket, 1바이트 opcode.
  - `0x01` = highlight (배경 노란색 + 현재 wall-clock 라벨)
  - `0x02` = reset    (검정 + 라벨 hidden)
  - `0x03` = shutdown
- 호스트 프로세스는 좌하단 (스크린 좌표 `(0, screen.height-300)`) 300x300, frameless, top-most, `WA_ShowWithoutActivating` (포커스 도둑 방지).
- 호스트가 죽으면 다음 호출에서 자동 재시작 — 1분 윈도우 내 최대 3회까지 (`PanelClient._RESPAWN_MAX`).

### 2.5 지연 경로 (`SendAndUpdate`)

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

## 3. SCAR 플러그인 메서드 상세

### 3.1 인스턴스 생성 (코드 직접 호출 시)

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

### 3.2 메서드

| 메서드 | 설명 | 반환 |
|---|---|---|
| `Ready(max_retry=3)` | API → DOCKER → reconnect 순으로 모드 자동 판별. | `"API"` / `"DOCKER"` / `"NONE"` |
| `SendApi(url, headers, data, max_retry=3)` | REST POST. headers/data 는 JSON 문자열. | `"status=<n>\n<body 512자>"` 또는 `"FAIL: ..."` |
| `Exec(cmd, timeout=300, max_retry=3)` | `docker exec scar bash -c <cmd>`. | `"rc=<n>\n<stdout 2KB>"` 또는 `"FAIL: ..."` |
| `Reconnect()` | `setsid <reconnect_script> ... &` + 대기. | `"DOCKER"` / `"FAIL: ..."` |

### 3.3 자동 판별 동작

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

### 3.4 시나리오 예시

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

ReplayKit 시나리오 편집기에서는 위 분기를 모듈 명령 노드 + 조건 분기 노드의 조합으로 만든다.
`Ready` 함수의 반환값을 변수에 저장해두고 다음 노드에서 `== "API"` / `== "DOCKER"` 로 분기.

---

## 4. 사전 점검 체크리스트

배포 전에 한 번씩 확인해야 할 항목.

### 4.1 환경 의존성

- [ ] `python3 -c "from PySide6.QtWidgets import QApplication; print('ok')"` 이 OK 인지
- [ ] `docker version` 이 정상 응답하는지 (SCAR 사용 시)
- [ ] `requests.get("http://localhost:8081/")` 가 200/404 등 응답을 받는지 (SCAR API 사용 시)
- [ ] TH `client.py` 가 있는 디렉터리 권한 + 실행 권한
- [ ] Wayland 세션이면 `XDG_SESSION_TYPE=x11` 또는 `GDK_BACKEND=x11` 로 ReplayKit 실행 (PySide6 frameless top-most 동작 보장)

### 4.2 자동 테스트 (pytest, Linux 머신에서)

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

### 4.3 수동 통합 테스트 (Linux PC 에서)

순서대로 확인하면 발견하는 문제의 절반은 1~3 단계에서 잡힌다.

1. **module 등록 확인** — UI 디바이스 추가 폼에 TH/SCAR 가 노출되는지의 backend 측 검증
   ```bash
   cd /path/to/ReplayKit_Universal && . .venv/bin/activate
   python -c "
   from backend.app.services import module_service
   names = [m['name'] for m in module_service.list_available_modules()]
   print('TH:', 'TH' in names, ' SCAR:', 'SCAR' in names)
   "
   ```
   기대: `TH: True  SCAR: True`. False 면 `plugins/linux/` 서브폴더 discovery 가 깨졌거나
   `__init__.py` import 가 실패한 것 — `python -c "import backend.app.plugins.linux.TH"` 로 에러 메시지 확인.

2. **UI 드롭다운 확인** — ReplayKit GUI 의 디바이스 추가 모달에서 모듈 드롭다운에 `TH (Test Harness, Linux)`,
   `SCAR (SDV Control, Linux)` 가 나오는지 + 각각의 입력 폼 필드가 README 1.2 표와 같은지.

2-b. **radmoon 스캔 확인** — TH 디바이스를 스캔으로 추가하는 경로 검증
   - 보조 디바이스 추가 → **스캔** → 결과 탭에 `radmoon (TH)` 가 노출되는지
   - USB Ethernet 어댑터가 보이는지 (예: `enx00e04c68b2c8` + MAC + `up` 태그)
   - 어댑터 행의 **연결** 버튼 → TH 폼이 자동 열리고 `eth_if` 가 채워졌는지
   - 검증용 backend 호출:
     ```bash
     python -c "
     from backend.app.services.device_manager import DeviceManager
     import asyncio
     async def main():
         dm = DeviceManager()
         print(await dm.scan_radmoon())
     asyncio.run(main())
     "
     ```

3. **TH 패널만 단독 띄우기** (PySide6 / X11 환경 검증)
   ```bash
   python -m backend.app.plugins.linux.common.th_panel_host --socket /tmp/th-test.sock --width 300 --height 300
   ```
   다른 터미널에서:
   ```bash
   python -c "
   import socket
   s = socket.socket(socket.AF_UNIX); s.connect('/tmp/th-test.sock')
   s.sendall(b'\\x01'); import time; time.sleep(2)   # highlight
   s.sendall(b'\\x02'); time.sleep(1)                # reset
   s.sendall(b'\\x03')                                # shutdown
   "
   ```
   기대: 좌하단 300x300 frameless 패널 → 노란색 점등 + 타임스탬프 라벨 → 검정 → 종료.

4. **TH 패널 라이프사이클**
   ```bash
   python - <<'PY'
   from backend.app.plugins.linux.TH import TH
   # eth_if/th_home 은 더미값 — 패널 메서드는 client.py 호출이 없어서 무관
   th = TH(eth_if="lo", th_home="/tmp", panel=True)
   print(th.PanelShow())          # 좌하단 검정 패널 등장
   print(th.PanelReset())
   print(th.PanelClose())
   PY
   ```
   기대: 모두 `ok`. Wayland 세션이면 `XDG_SESSION_TYPE=x11 GDK_BACKEND=x11 python -m ...` 형태로 강제.

5. **TH 패널 점등 latency 측정** — 실제 측정 (더미 client.py 사용)
   ```bash
   # th_home 구조 흉내 — <th_home>/harness/harness/grpc_client/src/client.py
   DUMMY=/tmp/dummy_th_home
   mkdir -p "$DUMMY/harness/harness/grpc_client/src"
   cat > "$DUMMY/harness/harness/grpc_client/src/client.py" <<'PY'
   import sys, time
   for a in sys.argv: print('arg:', a)
   time.sleep(0.5)
   sys.stdout.write('XXX GEAR_LEVER_ACCEPTED_T_REVERSE YYY\n'); sys.stdout.flush()
   time.sleep(0.1)
   PY
   python - <<PY
   from backend.app.plugins.linux.TH import TH
   th = TH(eth_if="lo", th_home="$DUMMY", panel=True)
   print(th.Info())
   print(th.SendAndUpdate("dummy_topic", "/dev/null", timeout=3))
   th.PanelClose()
   PY
   ```
   기대: `Info()` 가 `client.py exists = True` 출력 → 반환 1줄 `rc=0 trigger_hit=GEAR_LEVER_ACCEPTED_T_REVERSE e2e_ms=<숫자>`.
   e2e_ms 가 약 500ms (sleep 0.5) + 50ms 오버헤드 이내인지 확인.

6. **SCAR API 경로**
   - `scar` 컨테이너 떠있고 `http://localhost:8081/` 가 응답하는 상태
   - `python -c "from backend.app.plugins.linux.SCAR import SCAR; s=SCAR(); print(s.Ready())"` → `"API"`
   - `s.SendApi("http://localhost:8081/<endpoint>", headers='{}', data='{}')` → `status=200` 또는 서버 코드

7. **SCAR DOCKER 경로** — REST 서버만 죽이거나 응답 차단
   - `s.Ready()` → `"DOCKER"`
   - `s.Exec("echo hello")` → `rc=0\nhello`

8. **SCAR reconnect 경로**
   - 컨테이너 중지: `docker stop scar`
   - `s = SCAR(reconnect_script="/home/scar/scar.sh", reconnect_cwd="/home/scar")` 로 생성 후 `s.Ready()`
   - 기대: reconnect_script 실행 + 20초 대기 후 `"DOCKER"` 또는 `"NONE"` (스크립트 없거나 실패 시)
   - 같은 인스턴스로 `s.Ready()` 재호출 → API 재시도 없이 즉시 `"DOCKER"` (force_docker_mode 보존)

9. **시나리오에서 호출 (end-to-end)**
   - GUI 에서 등록한 TH 디바이스 + SCAR 디바이스를 사용하는 시나리오 1개 작성
   - 노드 1: `SCAR.SendApi(...)` (UTC time event)
   - 노드 2: `SCAR.SendApi(...)` (PnP off)
   - 노드 3: `TH.SendAndUpdate(topic, json, timeout=10)`
   - 시나리오 재생 → 패널이 점등되고 모든 step 이 pass 로 기록되는지

10. **RVC_Performance 1사이클 재현**
    - 원본 `Reference/Renault_CDC_Plugin/RVC_Performance.txt` 의 한 cycle 을 ReplayKit 시나리오로 옮김
    - 기대: MCU 시리얼에서 `PS:SHUTDOWN_DONE->OFF. ACT_Done:0x00000040` 수신, Arduino `RVC_START` 전송,
      `Send Signal And Update Panel` 시점에 패널이 노란색으로 깜빡

### 4.4 회귀 점검 (Windows 빌드)

Windows 빌드는 이 패키지를 import 하면 즉시 ImportError. 회귀 가능성이 있는 곳:
- [ ] Windows 에서 `module_service.list_available_modules()` 호출 시 TH/SCAR 가 목록에 없는지 (있으면 안 됨 —
      hardcoded 리스트에 추가되어 있지만 `_find_plugin_file` 가 None 을 반환해 fallback 단계에서 걸러져야 함)
- [ ] Windows 에서 시나리오 파일에 TH/SCAR 호출이 박혀 있어도 import 실패가 명확한 에러 메시지로 노출되는지
      (`_last_import_error` 에 사유가 들어가야 UI 토스트에 보임)

---

## 5. 트러블슈팅

### `ImportError: backend.app.plugins.linux is Linux-only (current platform: win32)`
정상 동작. Windows 에서는 패키지 자체가 비활성. 시나리오에 TH/SCAR 가 박혀 있다면 Linux PC 에서 재생해야 한다.

### `FAIL: th_home not configured ...` / `FAIL: client.py not found at ...`
TH 디바이스 등록 시 `th_home` 비워둠 또는 잘못된 경로. `<th_home>/harness/harness/grpc_client/src/client.py`
가 존재해야 한다. `python -c "from backend.app.plugins.linux.TH import TH; print(TH(eth_if='', th_home='/your/path').Info())"`
로 확인.

### `FAIL: bridge network setup` + `add 192.168.1.152/24 failed: sudo: a password is required`
백엔드 실행 계정이 `/usr/sbin/ip` (또는 `/sbin/ip`) 에 대해 passwordless sudo 권한이 없음. §1.2 step 6 의
`/etc/sudoers.d/replaykit-th` 예시 참고. 또는 임시로 `auto_setup=False` 로 등록하고 별도 터미널에서
`connect_th.sh` 를 sudo 로 직접 실행한 뒤 시나리오로 진입.

### `FAIL: ensure-adb.sh` + `exit 1` + `RBVM (192.168.140.1:5555) not connected`
ensure-adb.sh 가 ADB 인터페이스를 못 찾은 것. eth_if 가 실제 HU 연결된 어댑터인지, HU 가 부팅 완료됐는지
확인. `adb devices` 직접 실행해서 상태 검사.

### `FAIL: interface 'enxXX...' not found in /sys/class/net/`
어댑터가 분리됨 또는 재부팅 후 다른 이름으로 잡힘. radmoon 스캔 다시 실행해서 현재 이름 확인하고
디바이스 설정 업데이트.

### `FAIL: TH client spawn error: ...`
경로는 맞는데 실행 권한 문제 또는 `python_bin` 이 올바른 venv 의 인터프리터가 아님.

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

## 6. 파일 매핑 요약

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
