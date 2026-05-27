#!/usr/bin/env bash
# build_deb.sh — Linux 용 .deb 패키지 빌드 (Windows installer.iss 등가).
#
# 결과물:
#   dist/replaykit_<VERSION>_<ARCH>.deb
#
# 사전 요구:
#   - Linux (또는 WSL/Docker) 환경
#   - dpkg-deb, npm, curl 또는 wget, tar
#   - 선택: imagemagick (replaykit.ico → .png 자동 변환용)
#
# 사용:
#   ./scripts/build_deb.sh                    # 기본 (자동 감지)
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
while [ $# -gt 0 ]; do
    case "$1" in
        --arch)        ARCH="$2"; shift 2;;
        --keep-build)  KEEP_BUILD=1; shift;;
        --no-frontend) NO_FRONTEND=1; shift;;
        -h|--help)
            sed -n '2,18p' "$0"
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
require dpkg-deb "sudo apt install dpkg-dev"
require tar "보통 시스템 기본"
[ "$NO_FRONTEND" -eq 1 ] || require npm "NodeSource 또는 nvm 으로 Node.js 설치 (Ubuntu 24.04 의 apt npm 패키지는 의존성 충돌)"

# python-build-standalone 의 Python 은 clang 으로 빌드되어 sysconfig 의 CC 가 clang.
# PyAudio 같은 sdist-only C 확장 패키지가 빌드될 때 시스템에 clang 이 없으면 실패.
# portaudio19-dev 는 PyAudio C extension 의 헤더.
# build-essential 은 다른 일부 native 패키지 (e.g., 일부 OCR backend) 가 필요로 함.
missing_apt=()
command -v clang >/dev/null 2>/dev/null || missing_apt+=(clang)
[ -f /usr/include/portaudio.h ] || missing_apt+=(portaudio19-dev)
command -v gcc >/dev/null 2>/dev/null   || missing_apt+=(build-essential)

if [ ${#missing_apt[@]} -gt 0 ]; then
    echo "[ERROR] Python C 확장 빌드에 필요한 시스템 패키지가 부족합니다:"
    echo "        ${missing_apt[*]}"
    echo ""
    echo "  해결: sudo apt install -y ${missing_apt[*]}"
    echo ""
    echo "  (embedded Python 은 clang 으로 빌드되어 있어 PyAudio 등 sdist 패키지가"
    echo "   clang 을 호출함. portaudio19-dev 는 PyAudio C extension 의 헤더.)"
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

BUILD_DIR="$PROJECT_ROOT/build/deb"
PKG_NAME="replaykit_${VERSION}_${ARCH}"
STAGING="$BUILD_DIR/$PKG_NAME"
OPT_DIR="$STAGING/opt/ReplayKit"
DIST_DIR="$PROJECT_ROOT/dist"

echo "============================================"
echo "  Building $PKG_NAME.deb"
echo "  Version: $VERSION"
echo "  Arch:    $ARCH (python-build-standalone: $PYBS_ARCH)"
echo "============================================"

# ---- 클린 ----
rm -rf "$STAGING"
mkdir -p "$OPT_DIR"
mkdir -p "$STAGING/DEBIAN"
mkdir -p "$STAGING/usr/bin"
mkdir -p "$STAGING/usr/share/applications"
mkdir -p "$STAGING/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$DIST_DIR"

# ---- [1/6] Frontend 빌드 ----
echo "[1/6] Frontend build..."
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

# ---- [2/6] Embedded Python ----
echo "[2/6] Embedded Python..."
if [ -x "python/bin/python3" ]; then
    echo "      Reusing existing python/ ($(python/bin/python3 --version))"
else
    ./scripts/install_embedded_python.sh --arch "$PYBS_ARCH"
fi

# ---- [3/6] Python 패키지 설치 (embedded Python 안에) ----
echo "[3/6] pip install -r requirements.txt → python/..."
./python/bin/python3 -m pip install --upgrade pip -q
./python/bin/python3 -m pip install -r requirements.txt -q

shopt -s nullglob
whl_files=(lge.auto-*.whl)
if [ ${#whl_files[@]} -gt 0 ]; then
    ./python/bin/python3 -m pip install "${whl_files[0]}" -q
    echo "      lge.auto installed"
fi
shopt -u nullglob

# ---- [3.5/6] PySide6 명시 설치 + 검증 ----
# GUI launcher (replaykit-gui.py) 가 PySide6 에 의존. requirements.txt 의 핀이
# 이 Python 버전에 wheel 이 없거나 install 실패해도 -q 가 가려서 사용자가
# 모르는 경우를 막기 위한 안전망.
echo "      Verifying PySide6 (GUI launcher dependency)..."
if ! ./python/bin/python3 -c "import PySide6" >/dev/null 2>/dev/null; then
    echo "      PySide6 not present after requirements.txt — installing explicitly (verbose)..."
    # 명시적 설치 — 출력 보이게 (-q 제거)
    if ./python/bin/python3 -m pip install --upgrade "PySide6-Essentials"; then
        :
    else
        echo "      [WARN] PySide6-Essentials 실패 → PySide6 (meta) 로 재시도..."
        ./python/bin/python3 -m pip install --upgrade "PySide6" || true
    fi
fi
# 최종 검증 — 없으면 빌드 중단 (배포 후 사용자가 '미설치' 메시지 보는 것 방지)
if ! ./python/bin/python3 -c "import PySide6, PySide6.QtCore; print('      OK: PySide6', PySide6.__version__)" ; then
    echo ""
    echo "[ERROR] PySide6 미설치 또는 import 실패."
    echo "        진단:"
    echo "          ./python/bin/python3 -m pip list | grep -i pyside"
    echo "          ./python/bin/python3 -c 'import PySide6'"
    echo ""
    echo "        가능한 원인:"
    echo "          - PySide6 wheel 이 이 Python 버전 ($(./python/bin/python3 --version)) 에 없음"
    echo "          - 네트워크 / 디스크 / 권한 이슈"
    echo "          - requirements.txt 의 PySide6 핀이 wheel 없는 버전"
    echo ""
    echo "        해결:"
    echo "          requirements.txt 에서 PySide6-Essentials 핀 풀거나 다른 버전 시도"
    exit 1
fi

# ---- [4/6] Staging /opt/ReplayKit ----
echo "[4/6] Staging files → $OPT_DIR"

stage() {
    local item="$1"
    if [ -e "$PROJECT_ROOT/$item" ]; then
        cp -a "$PROJECT_ROOT/$item" "$OPT_DIR/"
    fi
}

stage python
stage backend
stage scripts
stage tools
stage server.py
stage _launcher.py
stage requirements.txt
stage version.txt
stage README_LINUX.md

# frontend 는 dist 만 (소스 + node_modules 제외)
mkdir -p "$OPT_DIR/frontend"
cp -a "$PROJECT_ROOT/frontend/dist" "$OPT_DIR/frontend/dist"

# 정리: __pycache__, .pyc, .git*
find "$OPT_DIR" -type d -name __pycache__ -prune -exec rm -rf {} + || true
find "$OPT_DIR" -type f -name '*.pyc' -delete || true
find "$OPT_DIR" -type d -name '.git' -prune -exec rm -rf {} + || true
find "$OPT_DIR" -type f -name '.gitignore' -delete || true

# venv 가 우연히 staging 에 섞이지 않도록 확인
[ -d "$OPT_DIR/venv" ] && rm -rf "$OPT_DIR/venv"

# ---- [5/6] Launcher / Desktop / Icon / GUI ----
echo "[5/6] Launcher + GUI + desktop entry + icon..."
cp packaging/replaykit-launcher.sh "$STAGING/usr/bin/ReplayKit"
chmod 755 "$STAGING/usr/bin/ReplayKit"

# GUI launcher (Tkinter) — DISPLAY 있을 때 launcher.sh 가 이걸 호출
if [ -f packaging/replaykit-gui.py ]; then
    cp packaging/replaykit-gui.py "$OPT_DIR/replaykit-gui.py"
    chmod 644 "$OPT_DIR/replaykit-gui.py"
fi

cp packaging/ReplayKit.desktop "$STAGING/usr/share/applications/ReplayKit.desktop"
chmod 644 "$STAGING/usr/share/applications/ReplayKit.desktop"

ICON_DST="$STAGING/usr/share/icons/hicolor/256x256/apps/replaykit.png"
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

# ---- [6/6] DEBIAN/control + maintainer scripts ----
echo "[6/6] DEBIAN/control..."
sed -e "s/@@VERSION@@/${VERSION}/g" \
    -e "s/@@ARCH@@/${ARCH}/g" \
    packaging/debian/control.in > "$STAGING/DEBIAN/control"

for hook in postinst prerm postrm; do
    if [ -f "packaging/debian/${hook}" ]; then
        cp "packaging/debian/${hook}" "$STAGING/DEBIAN/${hook}"
        chmod 755 "$STAGING/DEBIAN/${hook}"
    fi
done

# Installed-Size 자동 산정
if command -v du >/dev/null 2>/dev/null; then
    SIZE_KB=$(du -sk "$STAGING" --exclude=DEBIAN | awk '{print $1}')
    echo "Installed-Size: ${SIZE_KB}" >> "$STAGING/DEBIAN/control"
fi

# ---- 빌드 ----
echo
echo "dpkg-deb --build..."
OUTPUT="$DIST_DIR/${PKG_NAME}.deb"
dpkg-deb --build --root-owner-group "$STAGING" "$OUTPUT"

if [ "$KEEP_BUILD" -ne 1 ]; then
    rm -rf "$STAGING"
fi

echo
echo "============================================"
echo "  Done: $OUTPUT"
echo "============================================"
ls -lh "$OUTPUT"

# ---- [7/7] LG 배포 git 자동 갱신 push ----
# 빌드 성공 시 ReplayKit.sh 의 CANONICAL_REMOTE (= 사용자 머신이 sync 받는 LG 배포본) 로
# 현재 main HEAD 를 push. 인증/네트워크 실패해도 .deb 결과는 보존 — 핵심 산출물 손실 방지.
# 비활성화하려면 SKIP_LGE_PUSH=1 환경변수로 호출:
#   SKIP_LGE_PUSH=1 ./scripts/build_deb.sh
if [ "${SKIP_LGE_PUSH:-0}" = "1" ]; then
    echo
    echo "[LGE PUSH] Skipped (SKIP_LGE_PUSH=1)."
elif [ ! -d ".git" ] || ! command -v git >/dev/null 2>&1; then
    echo
    echo "[LGE PUSH] Skipped (not a git repo 또는 git 미설치)."
else
    # CANONICAL_REMOTE 를 ReplayKit.sh 에서 추출 — 한 곳에서만 정의 유지.
    LGE_URL=$(grep -E '^CANONICAL_REMOTE=' ReplayKit.sh 2>/dev/null | head -1 | sed -e 's/^CANONICAL_REMOTE=//' -e 's/^"//' -e 's/"$//')
    if [ -z "$LGE_URL" ]; then
        echo
        echo "[LGE PUSH] Skipped (ReplayKit.sh 에서 CANONICAL_REMOTE 추출 실패)."
    else
        echo
        echo "[LGE PUSH] 배포본 sync → $LGE_URL"
        # lge remote 가 없거나 URL 이 다르면 자동 등록/갱신
        cur_lge=$(git remote get-url lge 2>/dev/null || true)
        if [ -z "$cur_lge" ]; then
            git remote add lge "$LGE_URL"
            echo "[LGE PUSH] remote 'lge' 추가됨"
        elif [ "$cur_lge" != "$LGE_URL" ]; then
            git remote set-url lge "$LGE_URL"
            echo "[LGE PUSH] remote 'lge' URL 갱신: $cur_lge → $LGE_URL"
        fi
        # force push — '기존 내용은 다 날리고 엎어쓰자' 정책. main 만 push.
        if git push --force lge main; then
            echo "[LGE PUSH] OK — 사용자 머신의 ReplayKit.sh 가 다음 실행 시 새 HEAD 로 sync."
        else
            echo "[LGE PUSH] FAILED — 인증/네트워크 확인 후 수동 재시도:"
            echo "             git push --force lge main"
            echo "             (.deb 산출물은 보존됨: $OUTPUT)"
        fi
    fi
fi

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
echo "  sudo apt purge  replaykit       # 패키지 + 설정 제거 (사용자 데이터는 ~/.local/share/ReplayKit 에 그대로)"
