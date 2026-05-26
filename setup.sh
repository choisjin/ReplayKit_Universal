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
# [1/5] Python
# -------------------------------------------------------
echo "[1/5] Setting up Python..."

PYTHON=""
for cand in python3.10 python3.11 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        ver=$("$cand" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || true)
        case "$ver" in
            3.10|3.11|3.12) PYTHON="$cand"; break;;
        esac
    fi
done

if [ -z "$PYTHON" ]; then
    echo "      [ERROR] Python 3.10+ not found."
    echo "      Install with: sudo apt install python3.10 python3.10-venv python3-pip"
    exit 1
fi
echo "      Using ${PYTHON} ($(${PYTHON} --version 2>&1))"

# -------------------------------------------------------
# [2/5] venv
# -------------------------------------------------------
echo "[2/5] Creating venv..."
if [ ! -d "venv" ]; then
    "$PYTHON" -m venv venv
    if [ ! -x "venv/bin/python" ]; then
        echo "      [ERROR] venv creation failed (python3-venv 패키지 필요)"
        echo "      Install with: sudo apt install python3-venv"
        exit 1
    fi
    echo "      venv created"
else
    echo "      venv already exists - skipped"
fi
PY="venv/bin/python"
PIP="venv/bin/pip"

# -------------------------------------------------------
# [3/5] Python packages
# -------------------------------------------------------
echo "[3/5] Installing Python packages..."
if [ "$OFFLINE_MODE" = "1" ]; then
    echo "      [OFFLINE] PyPI install skipped."
else
    "$PY" -m pip install --upgrade pip -q
    "$PIP" install -r requirements.txt -q
fi

# lge.auto 로컬 wheel (있으면 설치)
shopt -s nullglob
whl_files=(lge.auto-*.whl)
if [ ${#whl_files[@]} -gt 0 ]; then
    "$PIP" install "${whl_files[0]}"
    echo "      lge.auto installed"
else
    echo "      [Note] lge.auto .whl not found"
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
if ! "$PY" -c "import tkinter" >/dev/null 2>&1; then
    echo "      [Note] tkinter 없음 - GUI server.py 사용 불가"
    echo "        sudo apt install python3-tk"
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
echo "  Setup complete!"
if [ "$PRODUCTION" = "1" ]; then
    echo "  Run: ./ReplayKit.sh"
else
    echo "  Run: ./ReplayKit.sh    or    venv/bin/python server.py"
fi
echo "============================================"
