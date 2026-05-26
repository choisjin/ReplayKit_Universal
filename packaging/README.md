# Packaging — .deb 빌드

Windows `installer.iss` (Inno Setup) 의 Linux 등가물. 결과적으로
`replaykit_<VERSION>_<ARCH>.deb` 파일 하나를 만들어 Ubuntu/Debian 사용자가
`sudo apt install ./replaykit_x.y.z_amd64.deb` 한 줄로 설치 가능.

## 0. 한눈에

```bash
# Linux (또는 WSL) 에서:
sudo apt install -y dpkg dpkg-dev nodejs npm imagemagick
./scripts/build_deb.sh
sudo apt install ./dist/replaykit_*.deb
ReplayKit
```

## 1. 디렉토리 구조

```
packaging/
├── README.md                       # 본 문서
├── replaykit-launcher.sh           # /usr/bin/ReplayKit 으로 설치되는 wrapper
├── ReplayKit.desktop               # 앱 메뉴 entry
├── icon-256.png                    # (옵션) 미리 만들어둔 PNG 아이콘
└── debian/
    ├── control.in                  # @@VERSION@@/@@ARCH@@ 치환되는 control 템플릿
    ├── postinst                    # 설치 후 (아이콘 캐시, 안내)
    ├── prerm                       # 제거 전 (실행 중 서버 종료)
    └── postrm                      # 제거 후 (캐시 갱신)
```

빌드 산출물:

```
build/deb/replaykit_<VERSION>_<ARCH>/     # staging (--keep-build 로 보존 가능)
dist/replaykit_<VERSION>_<ARCH>.deb       # 최종 패키지
```

## 2. 설치 레이아웃 (.deb 가 시스템에 배포하는 위치)

| 경로 | 내용 |
| --- | --- |
| `/opt/ReplayKit/` | 임베디드 Python, backend, frontend/dist, tools/, scripts/, server.py — read-only |
| `/usr/bin/ReplayKit` | 사용자가 호출하는 launcher (replaykit-launcher.sh) |
| `/usr/share/applications/ReplayKit.desktop` | 앱 메뉴 entry |
| `/usr/share/icons/hicolor/256x256/apps/replaykit.png` | 아이콘 |
| `~/.local/share/ReplayKit/` | 사용자별 쓰기 가능 데이터 (scenarios, screenshots, results, logs) — 최초 실행 시 자동 생성 |

`~/.local/share/ReplayKit/` 안에는 `/opt/ReplayKit/`의 읽기 전용 자산이 심볼릭링크로
들어가 있어, 사용자는 단일 작업 디렉토리에서 동작하는 것처럼 보입니다.
`sudo apt remove replaykit` 후에도 사용자 데이터는 보존됩니다.

## 3. 의존성

`control.in` 의 `Depends:` 는 항상 자동 설치, `Recommends:` 는 `apt` 기본 옵션에 따라.

| 종류 | 패키지 | 용도 |
| --- | --- | --- |
| Depends | `libgl1`, `libglib2.0-0`, `libxcb1`, `libxkbcommon0` | opencv-python-headless, Qt-less GUI 런타임 |
| Recommends | `scrcpy` | 안드로이드 화면 캡처/조작 |
| Recommends | `ffmpeg` | 비디오 인코딩 |
| Recommends | `android-tools-adb` | adb 명령 |
| Recommends | `python3-tk` | server.py 의 Tkinter GUI |
| Recommends | `fonts-noto-cjk` | 한글 UI 표시 |
| Recommends | `gstreamer1.0-plugins-good` | playsound (Linux) |

> embedded Python (~30MB) 과 site-packages 가 패키지 안에 들어가므로
> `python3` 시스템 의존은 명시하지 않습니다. (Tkinter GUI 만 시스템 python3-tk 필요)

## 4. 빌드 옵션

```bash
./scripts/build_deb.sh --help

./scripts/build_deb.sh                    # 자동 감지 (현재 머신 arch)
./scripts/build_deb.sh --arch arm64       # 크로스 빌드 (python-build-standalone 가 aarch64 미러 자동 다운로드)
./scripts/build_deb.sh --no-frontend      # frontend/dist 이미 빌드돼 있으면 npm 스킵
./scripts/build_deb.sh --keep-build       # build/deb/ staging 보존 (디버깅용)
```

## 5. 배포

```bash
# 검사
dpkg-deb -I dist/replaykit_*.deb         # 메타데이터
dpkg-deb -c dist/replaykit_*.deb | head  # 파일 목록

# 설치 (단일 머신)
sudo apt install ./dist/replaykit_*.deb

# 또는 GitHub Release 에 첨부 → 사용자가 다운로드 → 더블클릭 (GNOME Software / KDE Discover)
```

## 6. 아이콘 준비

세 가지 옵션 (우선순위 순):

1. **`packaging/icon-256.png` 를 미리 둔다** — 가장 권장. 256x256 PNG.
2. **`replaykit.ico` + `imagemagick`** — 빌드 시 자동으로 `convert replaykit.ico[0] -resize 256x256 ...` 실행.
3. **아이콘 없음** — `[WARN]` 표시 후 빌드는 계속 진행. 앱 메뉴에서 기본 아이콘으로 표시.

ImageMagick 설치:

```bash
sudo apt install imagemagick
```

## 7. 트러블슈팅

| 증상 | 원인 / 조치 |
| --- | --- |
| `dpkg-deb: error: archive '...' has premature member 'control.tar.gz'` | dpkg-deb 버전이 너무 옛날 — `sudo apt install dpkg` |
| 설치 후 `ReplayKit` 실행 안 됨 | `/opt/ReplayKit/python/bin/python3 --version` 으로 임베디드 Python 직접 확인 |
| `scrcpy: command not found` | `sudo apt install scrcpy` (Recommends 가 자동 설치 안 됐을 때) |
| 아이콘이 □ 또는 기본 | `gtk-update-icon-cache -f /usr/share/icons/hicolor` 수동 갱신 |
| `~/.local/share/ReplayKit/backend/settings.json` 수정 불가 | 현재 backend/ 가 /opt 로의 심볼릭링크 — settings.json 사용자별 분리는 향후 작업 |
