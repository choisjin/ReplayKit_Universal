#!/usr/bin/env bash
# build_deb.sh — Linux 빌드 스테이징 + 선택적 .deb 패키지 생성.
#
# 결과물:
#   dist/ReplayKit/                          # 항상 생성 (Windows dist/ReplayKit 와 등가).
#                                            # build_dist.py 의 _deploy_force_push 가
#                                            # 이 디렉토리를 LGE git 으로 force push.
#                                            # 사용자 머신의 ReplayKit.sh 가 git pull 로 sync.
#   dist/replaykit_<VERSION>_<ARCH>.deb      # --no-deb 가 아닐 때만 생성.
#                                            # 최초 설치/시스템 패키지 매니저 사용자용.
#
# 사전 요구:
#   - Linux (또는 WSL/Docker) 환경
#   - tar (필수), npm (선택, --no-frontend 시 불필요)
#   - dpkg-deb (선택, --no-deb 시 불필요)
#   - 선택: imagemagick (replaykit.ico → .png 자동 변환용)
#
# 사용:
#   ./scripts/build_deb.sh                    # 기본 (dist/ReplayKit/ + .deb)
#   ./scripts/build_deb.sh --no-deb           # .deb 생성 스킵, dist/ReplayKit/ 만
#   ./scripts/build_deb.sh --arch arm64       # 크로스 빌드
#   ./scripts/build_deb.sh --keep-build       # build/ 정리 안 함
#   ./scripts/build_deb.sh --no-frontend      # frontend npm build 생략 (이미 dist 있을 때)

set -e

cd "$(dirname "$(readlink -f "$0")")/.."
PROJECT_ROOT="$PWD"

# ---- 인자 파싱 ----
ARCH=""
KEEP_BUILD=0
NO_FRONTEND=0
NO_DEB=0
while [ $# -gt 0 ]; do
    case "$1" in
        --arch)        ARCH="$2"; shift 2;;
        --keep-build)  KEEP_BUILD=1; shift;;
        --no-frontend) NO_FRONTEND=1; shift;;
        --no-deb)      NO_DEB=1; shift;;
        -h|--help)
            sed -n '2,28p' "$0"
            exit 0;;
        *) echo "Unknown arg: $1"; exit 1;;
    esac
done

# ---- 환경 검증 ----
require() {
    command -v "$1" >/dev/null 2>/dev/null || {
        echo "[ERROR] '$1' 명령이 필요합니다. ($2)"
        exit 1
    }
}
[ "$NO_DEB" -eq 1 ] || require dpkg-deb "sudo apt install dpkg-dev"
require tar "보통 시스템 기본"
[ "$NO_FRONTEND" -eq 1 ] || require npm "NodeSource 또는 nvm 으로 Node.js 설치 (Ubuntu 24.04 의 apt npm 패키지는 의존성 충돌)"

# python-build-standalone 의 Python 은 clang 으로 빌드되어 sysconfig 의 CC 가 clang.
# PyAudio 같은 sdist-only C 확장 패키지가 빌드될 때 시스템에 clang 이 없으면 실패.
missing_apt=()
command -v clang >/dev/null 2>/dev/null || missing_apt+=(clang)
[ -f /usr/include/portaudio.h ] || missing_apt+=(portaudio19-dev)
command -v gcc >/dev/null 2>/dev/null   || missing_apt+=(build-essential)

if [ ${#missing_apt[@]} -gt 0 ]; then
    echo "[ERROR] Python C 확장 빌드에 필요한 시스템 패키지가 부족합니다:"
    echo "        ${missing_apt[*]}"
    echo ""
    echo "  해결: sudo apt install -y ${missing_apt[*]}"
    exit 1
fi

# ---- 메타데이터 ----
if [ ! -f version.txt ]; then
    echo "[ERROR] version.txt 가 없습니다."
    exit 1
fi
VERSION=$(tr -d 'v\r\n ' < version.txt)
if [ -z "$VERSION" ]; then
    echo "[ERROR] version.txt 가 비어있거나 형식 오류."
    exit 1
fi

if [ -z "$ARCH" ]; then
    if command -v dpkg >/dev/null 2>/dev/null; then
        ARCH=$(dpkg --print-architecture)
    else
        case "$(uname -m)" in
            x86_64)  ARCH="amd64";;
            aarch64) ARCH="arm64";;
            *) echo "[ERROR] uname=$(uname -m) — --arch 로 명시해주세요."; exit 1;;
        esac
    fi
fi

case "$ARCH" in
    amd64) PYBS_ARCH="x86_64";;
    arm64) PYBS_ARCH="aarch64";;
    *) echo "[ERROR] 지원하지 않는 ARCH: $ARCH (amd64 또는 arm64)"; exit 1;;
esac

DIST_DIR="$PROJECT_ROOT/dist"
# Canonical staging — git push 대상이자 사용자가 git clone 으로 받는 트리.
# Windows 의 dist/ReplayKit 와 동일한 역할.
DIST_REPLAYKIT="$DIST_DIR/ReplayKit"
# .deb 전용 staging (DEBIAN/ + usr/bin + opt/ReplayKit + usr/share/...).
BUILD_DIR="$PROJECT_ROOT/build/deb"
PKG_NAME="replaykit_${VERSION}_${ARCH}"
DEB_STAGING="$BUILD_DIR/$PKG_NAME"
OPT_DIR="$DEB_STAGING/opt/ReplayKit"

echo "============================================"
if [ "$NO_DEB" -eq 1 ]; then
    echo "  Building dist/ReplayKit/  (skip .deb)"
else
    echo "  Building dist/ReplayKit/ + $PKG_NAME.deb"
fi
echo "  Version: $VERSION"
echo "  Arch:    $ARCH (python-build-standalone: $PYBS_ARCH)"
echo "============================================"

# ---- 클린 ----
mkdir -p "$DIST_DIR"
# dist/ReplayKit/ — stale 파일 정리하고 처음부터 다시 채움.
# .git 디렉토리는 보존 (build_dist.py _ensure_dist_git 가 관리).
if [ -d "$DIST_REPLAYKIT" ]; then
    # 보존: .git
    find "$DIST_REPLAYKIT" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
else
    mkdir -p "$DIST_REPLAYKIT"
fi
# .deb staging 은 항상 새로 시작
rm -rf "$DEB_STAGING"
if [ "$NO_DEB" -ne 1 ]; then
    mkdir -p "$OPT_DIR"
    mkdir -p "$DEB_STAGING/DEBIAN"
    mkdir -p "$DEB_STAGING/usr/bin"
    mkdir -p "$DEB_STAGING/usr/share/applications"
    mkdir -p "$DEB_STAGING/usr/share/icons/hicolor/256x256/apps"
fi

# ---- [1/7] Frontend 빌드 ----
echo "[1/7] Frontend build..."
if [ "$NO_FRONTEND" -eq 1 ]; then
    echo "      Skipped (--no-frontend)"
elif [ -d frontend/dist ]; then
    echo "      Skipped (frontend/dist already exists — use --no-frontend to silence or rm to rebuild)"
else
    (cd frontend && npm install --no-audit --no-fund && npm run build)
fi
if [ ! -d frontend/dist ]; then
    echo "[ERROR] frontend/dist 가 없습니다. 빌드 필요."
    exit 1
fi

# ---- [2/7] Embedded Python ----
echo "[2/7] Embedded Python..."
if [ -x "python/bin/python3" ]; then
    echo "      Reusing existing python/ ($(python/bin/python3 --version))"
else
    ./scripts/install_embedded_python.sh --arch "$PYBS_ARCH"
fi

# ---- [3/7] Python 패키지 설치 (embedded Python 안에) ----
echo "[3/7] pip install -r requirements.txt → python/..."
./python/bin/python3 -m pip install --upgrade pip -q

# 이전 빌드 잔재 정리 — 옛 lge.auto 가 numpy (<1.25) 를 pin 하고 있으면 requirements.txt
# 의 numpy==2.2.6 (opencv-python numpy>=2 요구 충족용) 으로 못 올라감.
# pip uninstall 만으론 일부 케이스에서 site-packages 의 namespace package (lge/) 일부 파일이
# 남아 resolver 가 여전히 메타데이터를 읽는 현상이 있어 파일시스템 레벨에서 강제 제거.
SITE_PACKAGES=$(./python/bin/python3 -c "import site; print(site.getsitepackages()[0])")
echo "      site-packages: $SITE_PACKAGES"
./python/bin/python3 -m pip uninstall -y lge.auto lge-auto || true
rm -rf "$SITE_PACKAGES/lge" "$SITE_PACKAGES"/lge.auto-*.dist-info "$SITE_PACKAGES"/lge_auto-*.dist-info 2>/dev/null || true

# --upgrade-strategy eager — stale numpy 1.24.4 등이 박혀 있는 dirty env 에서도 requirements
# 의 핀 (numpy==2.2.6) 으로 강제 갱신. 첫 빌드엔 영향 없고 재빌드 안정성만 향상.
./python/bin/python3 -m pip install -r requirements.txt --upgrade --upgrade-strategy eager -q

shopt -s nullglob
# Linux 휠만 (win_amd64 휠이 함께 있어도 거름) — 아키텍처별: linux_x86_64 또는 linux_aarch64.
# --no-deps: wheel METADATA 의 numpy (<1.25,>=1.21) pin 이 opencv-python numpy>=2 와 충돌하므로
# declared deps 무시. wheel 의 모든 deps 는 requirements.txt 에 이미 있음 (numpy 만 wheel 이 과도 제약).
whl_files=(lge.auto-*-linux_*.whl)
if [ ${#whl_files[@]} -gt 0 ]; then
    ./python/bin/python3 -m pip install --no-deps "${whl_files[0]}" -q
    echo "      lge.auto installed: ${whl_files[0]} (--no-deps)"
else
    echo "      [Note] lge.auto linux wheel not found — Linux 전용 모듈 (CANAT 등) 사용 불가"
fi
shopt -u nullglob

# ---- [3.5/7] PySide6 명시 설치 + 검증 ----
# .deb launcher (/usr/bin/ReplayKit gui) 가 PySide6 로 GUI 띄움. embedded Python 에 PySide6
# 가 없으면 GUI launcher 가 headless 모드로만 떨어져 사용자가 "GUI 가 안 뜸" 으로 인지.
# requirements.txt 의 PySide6-Essentials pin 이 어떤 이유든 install 안 됐을 때를 대비해
# 매 빌드마다 명시적 설치 시도 (이미 있으면 -q 로 조용히 통과).
echo "      Verifying PySide6 (GUI launcher dependency)..."
# 항상 install 시도 — 이미 설치돼 있으면 pip 가 'already satisfied' 로 빠르게 통과.
if ! ./python/bin/python3 -m pip install --upgrade "PySide6-Essentials" -q; then
    echo "      [WARN] PySide6-Essentials 실패 → PySide6 (meta) 로 재시도..."
    ./python/bin/python3 -m pip install --upgrade "PySide6" -q || true
fi
if ! ./python/bin/python3 -c "import PySide6, PySide6.QtCore; print('      OK: PySide6', PySide6.__version__)" ; then
    echo ""
    echo "[ERROR] PySide6 미설치 또는 import 실패. .deb 산출물의 GUI launcher 가 동작 안 함."
    echo "        진단:"
    echo "          ./python/bin/python3 -m pip list | grep -i pyside"
    exit 1
fi
# site-packages 에 실제로 파일이 있는지 fs 레벨 검증 — pip install 이 성공해도 다른 site
# 경로(예: user site) 로 들어가는 케이스 방어. 없으면 build 중단.
if ! ./python/bin/python3 -c "
import PySide6, pathlib
p = pathlib.Path(PySide6.__file__).parent
assert p.is_relative_to(pathlib.Path('./python').resolve()), f'PySide6 at {p} 가 embedded python/ 외부 — .deb 에 포함 안 됨'
print(f'      PySide6 path: {p}')
" 2>&1; then
    echo "[ERROR] PySide6 가 embedded python/ 외부에 설치됨 — .deb 페이로드에 포함되지 않음."
    exit 1
fi

# ---- [4/7] Staging → dist/ReplayKit/ ----
# Canonical staging. Windows dist/ReplayKit 와 같은 구조.
# 사용자가 git clone 으로 받는 트리 = 사용자 머신의 ReplayKit.sh 가 실행되는 트리.
echo "[4/7] Staging files → $DIST_REPLAYKIT"

stage_to() {
    # stage_to <item> <target_root>
    # PROJECT_ROOT/<item> 이 있으면 <target_root>/<item> 으로 cp -a.
    local item="$1"
    local root="$2"
    if [ -e "$PROJECT_ROOT/$item" ]; then
        # 디렉토리는 -a 로 재귀 복사, 단일 파일도 동일하게 처리됨
        cp -a "$PROJECT_ROOT/$item" "$root/"
    fi
}

# Mode A (git clone) 사용자가 setup.sh + ReplayKit.sh 로 실행하기 위해 필요한 파일들.
# embedded Python (python/) 은 dist/.gitignore 에서 제외 (200MB+).
# 사용자 설치 시 ./scripts/install_embedded_python.sh 또는 setup.sh 가 다운로드.
for item in \
    backend \
    scripts \
    tools \
    server.py \
    _launcher.py \
    requirements.txt \
    version.txt \
    README_LINUX.md \
    ReplayKit.sh \
    setup.sh \
    sync_and_run.sh \
    git_remote.txt \
    replaykit.ico \
    .gitattributes \
; do
    stage_to "$item" "$DIST_REPLAYKIT"
done

# lge.auto wheel — setup 단계에서 embedded Python 에 설치. Linux 휠만 dist 에 포함.
shopt -s nullglob
for f in lge.auto-*-linux_*.whl; do
    cp -a "$PROJECT_ROOT/$f" "$DIST_REPLAYKIT/"
done
shopt -u nullglob

# frontend 는 dist 만 (소스 + node_modules 제외)
mkdir -p "$DIST_REPLAYKIT/frontend"
cp -a "$PROJECT_ROOT/frontend/dist" "$DIST_REPLAYKIT/frontend/dist"

# embedded python 도 dist/ReplayKit/ 에 두지만 .gitignore 로 push 제외.
# 로컬에선 .deb 가 이 트리에서 직접 copy 하므로 필요.
if [ -d "$PROJECT_ROOT/python" ]; then
    cp -a "$PROJECT_ROOT/python" "$DIST_REPLAYKIT/python"
fi

# .deb 페이로드용 packaging 자원 — Mode A 사용자에겐 불필요해도 함께 두면 .deb 재빌드 편함.
# 작아서(<100KB) 부담 없음.
if [ -d "$PROJECT_ROOT/packaging" ]; then
    cp -a "$PROJECT_ROOT/packaging" "$DIST_REPLAYKIT/packaging"
fi

# 정리: __pycache__, .pyc, 빌드 산출물 잔재
find "$DIST_REPLAYKIT" -type d -name __pycache__ -prune -exec rm -rf {} + || true
find "$DIST_REPLAYKIT" -type f -name '*.pyc' -delete || true
[ -d "$DIST_REPLAYKIT/venv" ] && rm -rf "$DIST_REPLAYKIT/venv"

echo "      dist/ReplayKit/ 스테이징 완료"

# ---- [5/7] .deb staging (--no-deb 면 스킵) ----
if [ "$NO_DEB" -eq 1 ]; then
    echo "[5/7] .deb staging skipped (--no-deb)"
else
    echo "[5/7] Staging files → $OPT_DIR (for .deb payload)"
    # dist/ReplayKit/ 내용을 그대로 .deb 의 /opt/ReplayKit/ 에 복사.
    # .git 디렉토리는 제외 (배포본에 git 메타데이터 노출 회피).
    if command -v rsync >/dev/null 2>/dev/null; then
        rsync -a --exclude='.git' --exclude='.gitignore' "$DIST_REPLAYKIT/" "$OPT_DIR/"
    else
        # rsync 없으면 cp 후 수동 제거
        cp -a "$DIST_REPLAYKIT/." "$OPT_DIR/"
        rm -rf "$OPT_DIR/.git" "$OPT_DIR/.gitignore" 2>/dev/null || true
    fi

    # PySide6 가 staging 에 실제로 들어왔는지 fs 검증 — .deb 사용자 GUI launcher 미동작 사고 방지.
    if [ ! -d "$OPT_DIR/python/lib/python3.10/site-packages/PySide6" ] && \
       [ ! -d "$OPT_DIR/python/lib/python3.10/site-packages/PySide6_Essentials" ]; then
        echo "[ERROR] PySide6 가 $OPT_DIR/python/lib/python3.10/site-packages/ 에 없음."
        echo "        rsync 단계에서 누락된 것으로 보입니다 (권한/링크/심볼릭링크 등 의심)."
        echo "        진단:"
        echo "          ls -la $DIST_REPLAYKIT/python/lib/python3.10/site-packages/ | grep -i pyside"
        echo "          ls -la $OPT_DIR/python/lib/python3.10/site-packages/ | grep -i pyside"
        exit 1
    fi
    echo "      PySide6 fs-check OK (in $OPT_DIR/python/...)"

    # ---- [6/7] Launcher / Desktop / Icon / GUI ----
    echo "[6/7] Launcher + desktop entry + icon..."
    cp packaging/replaykit-launcher.sh "$DEB_STAGING/usr/bin/ReplayKit"
    chmod 755 "$DEB_STAGING/usr/bin/ReplayKit"

    # GUI launcher 는 dist/ReplayKit/packaging/ 에 이미 복사됨 — .deb 페이로드는 OPT_DIR 안에서 참조.
    if [ -f "$OPT_DIR/packaging/replaykit-gui.py" ]; then
        cp "$OPT_DIR/packaging/replaykit-gui.py" "$OPT_DIR/replaykit-gui.py"
        chmod 644 "$OPT_DIR/replaykit-gui.py"
    fi

    cp packaging/ReplayKit.desktop "$DEB_STAGING/usr/share/applications/ReplayKit.desktop"
    chmod 644 "$DEB_STAGING/usr/share/applications/ReplayKit.desktop"

    ICON_DST="$DEB_STAGING/usr/share/icons/hicolor/256x256/apps/replaykit.png"
    if [ -f packaging/icon-256.png ]; then
        cp packaging/icon-256.png "$ICON_DST"
        echo "      Icon: packaging/icon-256.png"
    elif [ -f replaykit.ico ] && command -v convert >/dev/null 2>/dev/null; then
        convert "replaykit.ico[0]" -resize 256x256 "$ICON_DST"
        echo "      Icon: converted from replaykit.ico (imagemagick)"
    elif [ -f replaykit.ico ] && command -v magick >/dev/null 2>/dev/null; then
        magick "replaykit.ico[0]" -resize 256x256 "$ICON_DST"
        echo "      Icon: converted from replaykit.ico (imagemagick v7)"
    else
        echo "      [WARN] 아이콘 없음. packaging/icon-256.png 추가 권장."
    fi

    # ---- [7/7] DEBIAN/control + maintainer scripts ----
    echo "[7/7] DEBIAN/control..."
    sed -e "s/@@VERSION@@/${VERSION}/g" \
        -e "s/@@ARCH@@/${ARCH}/g" \
        packaging/debian/control.in > "$DEB_STAGING/DEBIAN/control"

    for hook in postinst prerm postrm; do
        if [ -f "packaging/debian/${hook}" ]; then
            cp "packaging/debian/${hook}" "$DEB_STAGING/DEBIAN/${hook}"
            chmod 755 "$DEB_STAGING/DEBIAN/${hook}"
        fi
    done

    # Installed-Size 자동 산정
    if command -v du >/dev/null 2>/dev/null; then
        SIZE_KB=$(du -sk "$DEB_STAGING" --exclude=DEBIAN | awk '{print $1}')
        echo "Installed-Size: ${SIZE_KB}" >> "$DEB_STAGING/DEBIAN/control"
    fi

    # ---- 빌드 ----
    echo
    echo "dpkg-deb --build..."
    OUTPUT="$DIST_DIR/${PKG_NAME}.deb"
    dpkg-deb --build --root-owner-group "$DEB_STAGING" "$OUTPUT"

    if [ "$KEEP_BUILD" -ne 1 ]; then
        rm -rf "$DEB_STAGING"
    fi

    echo
    ls -lh "$OUTPUT"
fi

echo
echo "============================================"
echo "  Done"
echo "  Staging:    $DIST_REPLAYKIT/"
if [ "$NO_DEB" -ne 1 ]; then
    echo "  Installer:  $OUTPUT"
fi
echo "============================================"

# LGE git push 는 build_dist.py 의 _deploy_force_push 가 책임.
# (이전 standalone push 로직 제거 — dist/ReplayKit/ 를 push 하는 것으로 통합)

if [ "$NO_DEB" -ne 1 ]; then
    echo
    echo "Install:"
    echo "  sudo apt install $OUTPUT"
    echo "Verify:"
    echo "  dpkg -L replaykit | head"
    echo "Run:"
    echo "  ReplayKit                       # GUI 또는 헤드리스 자동"
    echo "  DISPLAY= ReplayKit              # 강제 헤드리스"
    echo "Remove:"
    echo "  sudo apt remove replaykit       # 사용자 데이터 보존"
fi
