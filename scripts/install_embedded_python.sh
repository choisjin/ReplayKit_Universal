#!/usr/bin/env bash
# install_embedded_python.sh
# Linux 용 embedded Python 다운로드/설치 헬퍼.
#
# python-build-standalone (https://github.com/astral-sh/python-build-standalone)
# 의 "install_only" tarball 을 받아 프로젝트 루트의 python/ 디렉토리에 배치한다.
# 압축 해제 후 venv 없이 python/bin/python3 그대로 사용 가능.
#
# Windows 원본이 python-3.10.4-embed-amd64.zip 을 함께 배포해
# "OS Python 무관 자기 완결" 동작했던 것과 동일한 목적.

set -e

VERSION="3.10.14"
ARCH="$(uname -m)"
RELEASE=""
FORCE=0

usage() {
    cat <<EOF
Usage: $0 [--version 3.10.14] [--arch x86_64|aarch64] [--release YYYYMMDD] [--force]

  --version  Python 버전 (기본: ${VERSION}). python-build-standalone 이 지원하는 3.x.y.
  --arch     CPU 아키텍처 (기본: 자동감지 = $(uname -m)).
             x86_64 또는 aarch64.
  --release  python-build-standalone 릴리스 태그 (예: 20240814).
             미지정 시 GitHub API 로 latest 조회.
  --force    기존 python/ 디렉토리가 있어도 덮어쓴다.

다운로드 URL 패턴:
  https://github.com/astral-sh/python-build-standalone/releases/download/\${RELEASE}/
    cpython-\${VERSION}+\${RELEASE}-\${ARCH}-unknown-linux-gnu-install_only.tar.gz

압축 해제 결과:
  python/bin/python3       <- venv 없이 바로 사용
  python/lib/python3.10/   <- site-packages 도 여기

이후 ./setup.sh 가 python/ 을 자동 감지하여 requirements.txt 를 설치합니다.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="$2"; shift 2;;
        --arch)    ARCH="$2"; shift 2;;
        --release) RELEASE="$2"; shift 2;;
        --force)   FORCE=1; shift;;
        -h|--help) usage; exit 0;;
        *) echo "Unknown arg: $1"; usage; exit 1;;
    esac
done

# 스크립트 위치 → 프로젝트 루트로 이동
cd "$(dirname "$(readlink -f "$0")")/.."
PROJECT_ROOT="$PWD"

case "$ARCH" in
    x86_64|aarch64) ;;
    *) echo "[ERROR] 지원하지 않는 arch: $ARCH (x86_64 또는 aarch64 만 지원)"; exit 1;;
esac

# 기존 python/ 처리
if [ -d "python" ] && [ "$FORCE" -ne 1 ]; then
    if [ -x "python/bin/python3" ]; then
        existing_ver=$(python/bin/python3 --version 2>/dev/null || echo "unknown")
        echo "[INFO] python/ 이미 존재합니다 (${existing_ver})."
        echo "       재설치하려면 --force 사용."
        exit 0
    else
        echo "[WARN] python/ 디렉토리는 있지만 실행 가능한 python3 가 없습니다. 정리 후 진행..."
        rm -rf python
    fi
fi

# 다운로드 도구 확인
DOWNLOAD_TOOL=""
if command -v curl >/dev/null 2>/dev/null; then
    DOWNLOAD_TOOL="curl"
elif command -v wget >/dev/null 2>/dev/null; then
    DOWNLOAD_TOOL="wget"
else
    echo "[ERROR] curl 또는 wget 이 필요합니다."
    exit 1
fi

fetch_text() {
    # $1: URL → stdout
    if [ "$DOWNLOAD_TOOL" = "curl" ]; then
        curl -fsSL "$1"
    else
        wget -qO- "$1"
    fi
}

fetch_file() {
    # $1: URL, $2: out path
    if [ "$DOWNLOAD_TOOL" = "curl" ]; then
        curl -fL --progress-bar -o "$2" "$1"
    else
        wget --show-progress -O "$2" "$1"
    fi
}

# --release 비어있으면 GitHub API 에서 최신 태그 조회
if [ -z "$RELEASE" ]; then
    echo "[INFO] python-build-standalone 최신 릴리스 조회 중..."
    api_url="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    RELEASE=$(fetch_text "$api_url" | grep '"tag_name"' | head -1 | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/')
    if [ -z "$RELEASE" ]; then
        echo "[ERROR] 최신 릴리스 태그 조회 실패."
        echo "        --release YYYYMMDD 로 직접 지정해주세요."
        echo "        예: $0 --release 20240814"
        exit 1
    fi
    echo "[INFO] 최신 릴리스: ${RELEASE}"
fi

FILENAME="cpython-${VERSION}+${RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${FILENAME}"
TMPFILE="/tmp/${FILENAME}"

echo "[DOWNLOAD] ${URL}"
fetch_file "$URL" "$TMPFILE"

# SHA256 검증 (같은 릴리스 페이지에 .sha256 함께 게시됨)
SHA_URL="${URL}.sha256"
SHA_EXPECTED=""
if SHA_EXPECTED=$(fetch_text "$SHA_URL" 2>/dev/null); then
    SHA_EXPECTED=$(echo "$SHA_EXPECTED" | awk '{print $1}')
fi
if [ -n "$SHA_EXPECTED" ] && command -v sha256sum >/dev/null 2>/dev/null; then
    SHA_ACTUAL=$(sha256sum "$TMPFILE" | awk '{print $1}')
    if [ "$SHA_EXPECTED" != "$SHA_ACTUAL" ]; then
        echo "[ERROR] SHA256 mismatch:"
        echo "  expected: $SHA_EXPECTED"
        echo "  actual:   $SHA_ACTUAL"
        rm -f "$TMPFILE"
        exit 1
    fi
    echo "[VERIFY] sha256 OK"
else
    echo "[WARN] SHA256 검증을 건너뜁니다 (sha256sum 없음 또는 .sha256 미게시)."
fi

echo "[EXTRACT] ${TMPFILE} -> ${PROJECT_ROOT}/python/"
[ "$FORCE" -eq 1 ] && rm -rf python
tar -xzf "$TMPFILE" -C "$PROJECT_ROOT"
rm -f "$TMPFILE"

if [ ! -x "python/bin/python3" ]; then
    echo "[ERROR] 압축 해제는 됐지만 python/bin/python3 가 없습니다."
    echo "        tarball 구조를 확인하거나 --release 값을 다시 확인해주세요."
    exit 1
fi

# pip 확인 (install_only 빌드는 기본 포함이지만 안전하게)
if ! python/bin/python3 -m pip --version >/dev/null 2>/dev/null; then
    echo "[INFO] pip 부트스트랩..."
    python/bin/python3 -m ensurepip --upgrade || {
        echo "[ERROR] pip 부트스트랩 실패."
        exit 1
    }
fi

echo
echo "============================================"
echo "  Embedded Python 설치 완료"
echo "============================================"
echo "  Version : $(python/bin/python3 --version)"
echo "  Pip     : $(python/bin/python3 -m pip --version | awk '{print $1, $2}')"
echo "  Path    : ${PROJECT_ROOT}/python/bin/python3"
echo
echo "다음 단계:"
echo "  ./setup.sh           # python/ 자동 감지, requirements.txt 설치"
echo "  ./ReplayKit.sh       # 실행"
echo "============================================"
