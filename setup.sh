#!/usr/bin/env bash
# ReplayKit - Linux Setup
set -e
cd "$(dirname "$(readlink -f "$0")")"

echo "============================================"
echo "  ReplayKit - Setup (Linux)"
echo "============================================"

# Production 감지: dist 가 있고 frontend 소스가 없으면 production
PRODUCTION=0
if [ -f "frontend/dist/index.html" ] && [ ! -f "frontend/package.json" ]; then
    PRODUCTION=1
fi

# Offline 모드
OFFLINE_MODE=0
if [ -f ".offline_mode" ]; then
    OFFLINE_MODE=1
    echo "      [OFFLINE] .offline_mode detected - skipping network operations."
fi

# -------------------------------------------------------
# [1/5] Python (embedded > venv > system 우선순위)
# -------------------------------------------------------
echo "[1/5] Detecting Python..."

PY=""
PY_MODE=""
VENV_CREATED=0

if [ -x "python/bin/python3" ]; then
    # 1순위: scripts/install_embedded_python.sh 로 설치된 embedded Python
    PY="python/bin/python3"
    PY_MODE="embedded"
    echo "      [embedded] $(python/bin/python3 --version 2>/dev/null)"
elif [ -x "venv/bin/python" ]; then
    # 2순위: 기존 venv 재사용
    PY="venv/bin/python"
    PY_MODE="venv"
    echo "      [venv] reused ($(venv/bin/python --version 2>/dev/null))"
else
    # 3순위: 시스템 Python 으로 venv 새로 생성
    SYSTEM_PYTHON=""
    for cand in python3.10 python3.11 python3.12 python3; do
        if command -v "$cand" >/dev/null 2>/dev/null; then
            ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)
            case "$ver" in
                3.10|3.11|3.12) SYSTEM_PYTHON="$cand"; break;;
            esac
        fi
    done

    if [ -z "$SYSTEM_PYTHON" ]; then
        echo "      [ERROR] Python 3.10+ not found."
        echo "      해결책 (택1):"
        echo "        a) sudo apt install python3.10 python3.10-venv python3-pip"
        echo "        b) ./scripts/install_embedded_python.sh   # 자기완결 embedded Python"
        exit 1
    fi
    echo "      Using system ${SYSTEM_PYTHON} ($(${SYSTEM_PYTHON} --version))"

    "$SYSTEM_PYTHON" -m venv venv
    if [ ! -x "venv/bin/python" ]; then
        echo "      [ERROR] venv creation failed (python3-venv 패키지 필요)"
        echo "      Install with: sudo apt install python3-venv"
        exit 1
    fi
    PY="venv/bin/python"
    PY_MODE="venv"
    VENV_CREATED=1
fi

# -------------------------------------------------------
# [2/5] venv (embedded 면 생략)
# -------------------------------------------------------
case "$PY_MODE" in
    embedded) echo "[2/5] venv skipped (embedded Python 직접 사용)";;
    venv)
        if [ "$VENV_CREATED" -eq 1 ]; then
            echo "[2/5] venv created"
        else
            echo "[2/5] venv ready (reused)"
        fi
        ;;
esac

# -------------------------------------------------------
# [3/5] Python packages
# -------------------------------------------------------
echo "[3/5] Installing Python packages..."
if [ "$OFFLINE_MODE" = "1" ]; then
    echo "      [OFFLINE] PyPI install skipped."
else
    "$PY" -m pip install --upgrade pip -q
    "$PY" -m pip install -r requirements.txt -q
fi

# lge.auto 로컬 wheel (Linux 휠만 — win_amd64 휠이 함께 있어도 거름).
# 아키텍처별 wheel 파일명: lge.auto-<ver>-cp310-cp310-linux_x86_64.whl 또는 linux_aarch64.
shopt -s nullglob
whl_files=(lge.auto-*-linux_*.whl)
if [ ${#whl_files[@]} -gt 0 ]; then
    "$PY" -m pip install "${whl_files[0]}"
    echo "      lge.auto installed: ${whl_files[0]}"
else
    echo "      [Note] lge.auto linux wheel not found"
fi
shopt -u nullglob

# -------------------------------------------------------
# [4/5] 시스템 의존성 안내 (자동 설치 안 함)
# -------------------------------------------------------
echo "[4/5] Checking system dependencies..."
need_install=()
command -v adb >/dev/null 2>&1   || need_install+=("android-tools-adb")
command -v ffmpeg >/dev/null 2>&1 || need_install+=("ffmpeg")
command -v scrcpy >/dev/null 2>&1 || need_install+=("scrcpy")

if [ ${#need_install[@]} -gt 0 ]; then
    echo "      [Note] 다음 시스템 패키지가 필요합니다:"
    echo "        sudo apt install ${need_install[*]}"
else
    echo "      adb / ffmpeg / scrcpy OK"
fi

# Tkinter 확인 (server.py GUI 용)
if ! "$PY" -c "import tkinter" >/dev/null 2>/dev/null; then
    echo "      [Note] tkinter 없음 - GUI server.py 사용 불가"
    if [ "$PY_MODE" = "embedded" ]; then
        echo "        embedded Python 은 tkinter 미포함입니다."
        echo "        GUI 가 필요하면 시스템 Python 사용 또는 헤드리스 모드 (DISPLAY= ./ReplayKit.sh)"
    else
        echo "        sudo apt install python3-tk"
    fi
fi

# -------------------------------------------------------
# [5/5] Node.js (dev mode 전용)
# -------------------------------------------------------
if [ "$PRODUCTION" = "1" ]; then
    echo "[5/5] Production mode - skipping Node.js"
elif [ "$OFFLINE_MODE" = "1" ]; then
    echo "[5/5] Offline mode - skipping npm install"
elif ! command -v npm >/dev/null 2>&1; then
    echo "[5/5] Node.js not found - install with: sudo apt install nodejs npm"
else
    echo "[5/5] Node.js $(node --version) detected"
    if [ -f "frontend/package.json" ]; then
        echo "      Installing frontend packages..."
        (cd frontend && npm install)
        echo "      npm install done"
    fi
fi

echo
echo "============================================"
echo "  Setup complete! (Python mode: ${PY_MODE})"
if [ "$PRODUCTION" = "1" ]; then
    echo "  Run: ./ReplayKit.sh"
else
    echo "  Run: ./ReplayKit.sh    or    ${PY} server.py"
fi
echo "============================================"
