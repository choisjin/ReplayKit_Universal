# ReplayKit - Linux Port

Windows 원본(`E:/Project/Recording_Test`)을 Linux 에서도 빌드/실행할 수 있게 포팅한 사본입니다.

## 0. 한눈에

```bash
# Ubuntu/Debian 기준
sudo apt update
sudo apt install -y python3.10 python3.10-venv python3-tk \
                    android-tools-adb ffmpeg scrcpy \
                    nodejs npm git
./setup.sh           # venv + 패키지 + frontend npm install
./ReplayKit.sh       # 동기화 + 서버 시작 (GUI 또는 헤드리스)
```

브라우저에서 http://localhost:5173 (dev) 또는 http://localhost:8000 (prod 빌드) 접속.

## 1. 시스템 요구사항

| 항목 | 권장 |
| --- | --- |
| OS | Ubuntu 22.04 LTS / Debian 12 / Fedora 38 |
| Python | 3.10 또는 3.11 (3.12 도 동작은 함) |
| Node.js | 18+ (LTS) |
| 디스크 | ~3GB (venv + node_modules + OCR 모델) |

## 2. 의존성 설치

### 2-1. 시스템 패키지

```bash
sudo apt install -y \
    python3.10 python3.10-venv python3-pip python3-tk \
    android-tools-adb \
    ffmpeg \
    scrcpy \
    nodejs npm \
    git \
    libgl1 libglib2.0-0   # opencv-python-headless 실행 의존성
```

> **DPI/Wayland 주의**: `server.py` (Tkinter GUI) 는 X11 환경에서 가장 안정적입니다.
> Wayland 환경이라면 `GDK_BACKEND=x11 ./ReplayKit.sh` 로 실행하거나, GUI 없이
> 자동으로 헤드리스 백엔드 단독 실행이 선택됩니다 (DISPLAY 변수 없을 때).

### 2-2. Python venv & 패키지

```bash
./setup.sh
```

`setup.sh` 의 Python 우선순위 (Windows ReplayKit.bat 등가):

| 우선순위 | 조건 | 사용 경로 | 모드 |
| --- | --- | --- | --- |
| 1 | `python/bin/python3` 존재 | embedded (자기완결) | `embedded` |
| 2 | `venv/bin/python` 존재 | 기존 venv 재사용 | `venv` |
| 3 | 시스템 `python3.10~3.12` | 새 venv 생성 후 사용 | `venv` |

내부 수행:
1. 위 우선순위로 Python 선택 (embedded 면 venv 생략)
2. `<PY> -m pip install -r requirements.txt`
3. `lge.auto-*.whl` 이 있으면 자동 설치
4. `frontend && npm install` (dev 모드)

#### 2-2-a. (옵션) Embedded Python — 시스템 의존 없는 배포용

Windows 원본은 `python-3.10.4-embed-amd64.zip` 을 함께 배포해 OS Python 과
무관하게 동작합니다. Linux 등가물은 [`python-build-standalone`](https://github.com/astral-sh/python-build-standalone)
(Astral 제공) 의 `install_only` tarball — 약 30MB, 압축 해제만으로
`python/bin/python3` 가 바로 동작합니다.

```bash
# 최신 릴리스 자동 조회 (기본: 3.10.14, 자동 arch 감지)
./scripts/install_embedded_python.sh

# 버전/아키텍처 지정
./scripts/install_embedded_python.sh --version 3.11.9 --arch aarch64

# 특정 릴리스 태그 고정 (재현성 필요할 때)
./scripts/install_embedded_python.sh --release 20240814

# 기존 python/ 덮어쓰기
./scripts/install_embedded_python.sh --force
```

설치 후:
```bash
./setup.sh           # python/ 자동 감지 → venv 생략, requirements.txt 만 설치
./ReplayKit.sh       # [PYTHON] mode=embedded, bin=python/bin/python3 로 실행
```

| 장점 | 단점 |
| --- | --- |
| 시스템 Python 의존성 0 | tkinter 미포함 (GUI 서버 사용 불가, 헤드리스만) |
| 배포본 자기완결 (OS Python 충돌 회피) | 첫 다운로드 ~30MB |
| `python/` 디렉토리 통째로 복사하면 이식 가능 | x86_64 / aarch64 만 지원 |

> tkinter GUI (`server.py`) 가 필요하면 시스템 Python + `python3-tk` 를 사용하거나
> `DISPLAY= ./ReplayKit.sh` 헤드리스 모드를 사용하세요.

### 2-3. ADB 설정

```bash
# udev 규칙 (사용자 권한으로 디바이스 접근)
sudo apt install android-sdk-platform-tools-common
sudo usermod -aG plugdev $USER
# 로그아웃 후 재로그인 또는 newgrp plugdev
```

연결 후:
```bash
adb devices    # 디바이스 목록 확인
```

## 3. 실행

### 개발 모드 (UI 변경 hot reload)

```bash
./ReplayKit.sh
# 또는 수동:
source venv/bin/activate
python -m uvicorn backend.app.main:app --reload --port 8000   # 백엔드
cd frontend && npm run dev                                     # 프론트 (5173)
```

### 프로덕션 빌드

```bash
cd frontend && npm run build    # frontend/dist/ 생성
./ReplayKit.sh                  # PRODUCTION 자동 감지
```

### 헤드리스 (서버 전용)

`DISPLAY` 가 비어있으면 `ReplayKit.sh` 가 자동으로 Tkinter GUI 를 건너뛰고
`uvicorn` 만 직접 실행합니다. 원격 서버 / 컨테이너 / CI 환경에 적합.

```bash
DISPLAY= ./ReplayKit.sh
```

## 4. Linux 에서 동작하지 않는 기능 (자동 비활성)

코드는 `sys.platform == "win32"` 가드로 안전하게 분기되어 있어, Linux 에서는
해당 기능이 **자동으로 비활성화**되고 다른 기능은 정상 동작합니다.

| 기능 | 모듈 | Linux 동작 |
| --- | --- | --- |
| **WinControl** (다른 Win32 프로세스 윈도우 캡처/조작) | `wincontrol_service.py` | 비활성 (`available=False` 반환) |
| **CANoe RBS** (Vector CANoe COM 연동) | `plugins/CANoe_RBS.py` | import 실패 시 모듈 미등록 |
| **DLT Viewer** (GENIVI DLT Viewer 자동화) | `plugins/DLTViewer.py` | Linux 빌드 가능 (별도 컴파일 필요) |
| **VisionCamera** (Allied Vision Vimba X) | `plugins/VisionCamera.py` | Vimba X Linux SDK 설치 필요 |
| **CAN Transport DLL** (`CANatTransportProcDll.dll`) | `app/modules/` | DLL 로딩 불가 — 대체 .so 필요 |
| **Windows 절전 차단** (`SetThreadExecutionState`) | `playback_service.py` | no-op (systemd-inhibit 권장) |
| **CREATE_NO_WINDOW 콘솔 숨김** | 전역 | 자동 0 으로 처리 |
| **Windows 시리얼 포트** (COMx) | `pyserial` | `/dev/ttyUSB*` `/dev/ttyACM*` 으로 매핑 |

`backend/app/modules/*.dll` 은 참조용으로 그대로 두었으며, 동일 기능의 Linux
빌드(.so)를 같은 디렉토리에 두면 `module_service._ensure_module_deps` 가
자동으로 plugin 디렉토리로 복사합니다.

## 5. ffmpeg / scrcpy 경로

- `tools/ffmpeg.exe` 는 제거됨 — 시스템 패키지 ffmpeg 를 사용합니다.
- `FFMPEG_PATH` 환경 변수로 override 가능: `export FFMPEG_PATH=/usr/local/bin/ffmpeg`
- `tools/scrcpy-server.jar` 는 유지 (cross-platform).
- `SCRCPY_SERVER_PATH` 환경 변수로도 override 가능.

배포 설치 경로는 자동으로 `/opt/ReplayKit` 또는 `~/.local/share/ReplayKit` 을 후보로 탐색.

## 6. 알려진 차이점

- **Hotkey/Tray**: `pystray` 트레이 아이콘은 GTK/AppIndicator 의존. Wayland 에서는
  표시되지 않을 수 있음 — 기능적으로는 백엔드/프론트엔드 모두 정상.
- **playsound**: Linux 에서는 `gst-plugins-good` 필요 (`sudo apt install gstreamer1.0-plugins-good`).
- **mss**: X11 우선. Wayland 에서 화면 캡처는 사용하지 않으므로 무영향.
- **fonts**: 한글 UI 가 □ 로 나오면 `sudo apt install fonts-noto-cjk` 설치.

## 7. 트러블슈팅

| 증상 | 원인 / 조치 |
| --- | --- |
| `tkinter import error` | `sudo apt install python3-tk` |
| `cv2 import error` | `sudo apt install libgl1 libglib2.0-0` |
| `adb` 권한 거부 | `sudo usermod -aG plugdev $USER` + 재로그인 |
| 한글 깨짐 | `sudo apt install fonts-noto-cjk` |
| `port 8000 already in use` | `./ReplayKit.sh` 의 stop_existing_server 가 자동 정리 |
| OCR rapidocr 미설치 | `venv/bin/pip install rapidocr-onnxruntime` |

## 8. 원본과의 차이 요약

```
원본 (Recording_Test) → Replaykit_for_linux
─────────────────────────────────────────────
+ setup.sh, ReplayKit.sh, sync_and_run.sh           (Linux 실행 스크립트)
+ scripts/install_embedded_python.sh                (Linux embedded Python 설치)
+ README_LINUX.md                                   (본 문서)
- setup.bat, ReplayKit.bat, sync_and_run.bat        (Windows 전용 제거)
- build_dist.py, installer.iss                      (Windows 빌드 전용 제거)
- tools/ffmpeg.exe                                  (Linux 는 system ffmpeg)
~ requirements.txt                                  (pywin32 등 sys_platform 분기)
~ module_service.py                                 (.dll 외 .so/.dylib glob 추가)
~ ocr_service.py, scripts/diag_*.py                 (venv/Scripts → venv/bin)
~ setup.sh / ReplayKit.sh                           (embedded > venv > system 우선순위)
```

Python 소스의 로직은 그대로 두었습니다 — 이미 `sys.platform` 가드가
잘 들어가 있기 때문입니다.
