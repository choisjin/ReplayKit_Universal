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

# github.com/<owner>/<repo>/releases/latest 의 HTTP 302 Location 헤더를 긁어
# 최신 릴리스 태그를 알아낸다. api.github.com 의 시간당 60회 비인증 rate limit
# (HTTP 403) 을 우회. github.com 본 도메인은 사람이 보는 페이지라 사실상 제한 없음.
fetch_latest_tag_via_redirect() {
    local html_url="https://github.com/astral-sh/python-build-standalone/releases/latest"
    if [ "$DOWNLOAD_TOOL" = "curl" ]; then
        # curl -sI 는 HEAD 요청, redirect 따라가지 않고 Location 헤더만 반환
        curl -sI "$html_url" \
            | grep -i '^location:' | tail -1 \
            | sed -E 's|.*/tag/([^[:space:]/]+).*|\1|' \
            | tr -d '\r\n'
    else
        # wget --max-redirect=0 으로 첫 응답만 받고 Location 헤더 파싱
        wget --max-redirect=0 --server-response -q -O /dev/null "$html_url" 2>/tmp/.pbs_redirect.$$ || true
        grep -i 'Location:' /tmp/.pbs_redirect.$$ 2>/dev/null \
            | tail -1 \
            | sed -E 's|.*/tag/([^[:space:]/]+).*|\1|' \
            | tr -d '\r\n'
        rm -f /tmp/.pbs_redirect.$$
    fi
}

fetch_latest_tag_via_api() {
    local api_url="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
    fetch_text "$api_url" 2>/dev/null \
        | grep '"tag_name"' | head -1 \
        | sed -E 's/.*"tag_name": *"([^"]+)".*/\1/'
}

# --release 비어있으면 자동 조회 (redirect 우선, API 폴백)
if [ -z "$RELEASE" ]; then
    echo "[INFO] python-build-standalone 최신 릴리스 조회 중..."

    # 1순위: github.com redirect (rate limit 없음)
    RELEASE=$(fetch_latest_tag_via_redirect || true)
    if [ -n "$RELEASE" ]; then
        echo "[INFO] 최신 릴리스 (via redirect): ${RELEASE}"
    else
        # 2순위: GitHub API (rate limit 적용)
        echo "[INFO] redirect 실패, API 폴백 시도..."
        RELEASE=$(fetch_latest_tag_via_api || true)
        if [ -n "$RELEASE" ]; then
            echo "[INFO] 최신 릴리스 (via API): ${RELEASE}"
        fi
    fi

    if [ -z "$RELEASE" ]; then
        echo "[ERROR] 최신 릴리스 태그 조회 실패 (redirect + API 모두 실패)."
        echo "        --release YYYYMMDD 로 직접 지정해주세요."
        echo "        예: $0 --release 20240814"
        echo "        목록: https://github.com/astral-sh/python-build-standalone/releases"
        exit 1
    fi
fi

# 요청한 VERSION 이 릴리스에 없으면 같은 minor (예: 3.10.x) 의 다른 patch 자동 probe.
# python-build-standalone 은 오래된 patch 버전을 새 릴리스에서 빼는 경향이 있어
# --version 3.10.14 가 최신 release 에는 없을 수 있다.
probe_version_exists() {
    local v="$1"
    # tar.gz 자체를 HEAD 로 probe. 신/구 릴리스 모두 호환.
    # (예전 PBS 는 .tar.gz.sha256 sibling 이 있었지만 20260510 이후로는 SHA256SUMS 통합 파일로 변경됨)
    local probe_url="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/cpython-${v}+${RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz"
    if [ "$DOWNLOAD_TOOL" = "curl" ]; then
        curl -fsI -o /dev/null "$probe_url"
    else
        wget --spider -q "$probe_url"
    fi
}

MAJOR_MINOR=$(echo "$VERSION" | sed -E 's/^([0-9]+\.[0-9]+).*/\1/')
if ! probe_version_exists "$VERSION"; then
    echo "[INFO] cpython-${VERSION}+${RELEASE} 가 이 릴리스에 없습니다."
    echo "[INFO] 같은 minor (${MAJOR_MINOR}.x) 의 다른 patch 시도..."
    FOUND_VERSION=""
    # patch 시리즈를 높은 → 낮은 순서로 probe (최신 보안 패치 우선)
    for p in 20 19 18 17 16 15 14 13 12 11 10 9 8 7 6 5 4 3 2 1 0; do
        cand="${MAJOR_MINOR}.${p}"
        if [ "$cand" = "$VERSION" ]; then continue; fi
        if probe_version_exists "$cand"; then
            FOUND_VERSION="$cand"
            break
        fi
    done
    if [ -z "$FOUND_VERSION" ]; then
        echo "[ERROR] 릴리스 ${RELEASE} 에 ${MAJOR_MINOR}.x 시리즈 빌드가 없습니다."
        echo "        다른 --release 로 재시도하거나 페이지 직접 확인:"
        echo "        https://github.com/astral-sh/python-build-standalone/releases/tag/${RELEASE}"
        echo
        echo "        예: $0 --release 20240814   # 3.10.14 보장"
        exit 1
    fi
    echo "[INFO] 사용 가능한 patch 발견: ${FOUND_VERSION} (요청: ${VERSION})"
    VERSION="$FOUND_VERSION"
fi

FILENAME="cpython-${VERSION}+${RELEASE}-${ARCH}-unknown-linux-gnu-install_only.tar.gz"
URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/${FILENAME}"
TMPFILE="/tmp/${FILENAME}"

echo "[DOWNLOAD] ${URL}"
fetch_file "$URL" "$TMPFILE"

# SHA256 검증 - 두 가지 형식 지원
#   (1) 구식 (20240814 등): tar.gz 와 같은 위치에 tar.gz.sha256 sibling
#   (2) 신식 (20260510~): release 페이지 루트의 통합 SHA256SUMS 파일
SHA_EXPECTED=""
if RAW=$(fetch_text "${URL}.sha256" 2>/dev/null); then
    SHA_EXPECTED=$(echo "$RAW" | awk '{print $1}')
fi
if [ -z "$SHA_EXPECTED" ]; then
    SUMS_URL="https://github.com/astral-sh/python-build-standalone/releases/download/${RELEASE}/SHA256SUMS"
    if RAW=$(fetch_text "$SUMS_URL" 2>/dev/null); then
        # SHA256SUMS 포맷: "<hash>  <filename>"
        SHA_EXPECTED=$(echo "$RAW" | grep -F " ${FILENAME}" | awk '{print $1}' | head -1)
    fi
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
    echo "[WARN] SHA256 검증을 건너뜁니다 (sha256sum 없음 또는 해시 게시 위치 변경)."
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
