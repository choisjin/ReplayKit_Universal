#!/usr/bin/env bash
# ReplayKit - Linux Launcher (ReplayKit.bat 의 Linux 등가)
set -u
cd "$(dirname "$(readlink -f "$0")")"

# 시스템 Python 환경 변수 격리 (cv2/.so 로딩 충돌 방지)
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
export PYTHONNOUSERSITE=1

# ---- Offline 모드 ----
OFFLINE_MODE=0
if [ -f ".offline_mode" ]; then
    OFFLINE_MODE=1
    echo "[OFFLINE] .offline_mode detected - network access disabled."
fi

# ---- --home 옵션 ----
GIT_REMOTE_FILE="git_remote.txt"
if [ "${1:-}" = "--home" ]; then
    if [ -f "git_remote_home.txt" ]; then
        GIT_REMOTE_FILE="git_remote_home.txt"
        echo "[GIT] Using home remote: git_remote_home.txt"
    else
        echo "[GIT] git_remote_home.txt not found - using default."
    fi
fi

CANONICAL_REMOTE="http://mod.lge.com/hub/dqa_replay_kit/replay_kit.git"

# ---- 기존 서버 종료 ----
stop_existing_server() {
    echo "[STOP] Checking for running server..."
    # 1) backend (port 8000) / frontend (port 5173)
    for port in 8000 5173; do
        if command -v fuser >/dev/null 2>&1; then
            fuser -k "${port}/tcp" >/dev/null 2>&1 && echo "[STOP] Killed process on port ${port}" || true
        elif command -v lsof >/dev/null 2>&1; then
            pids=$(lsof -ti tcp:${port} 2>/dev/null || true)
            if [ -n "$pids" ]; then
                kill -9 $pids 2>/dev/null && echo "[STOP] Killed PID(s) ${pids} on port ${port}" || true
            fi
        fi
    done
    # 2) server.py / _launcher.py 잔여 프로세스
    pkill -f "python.*\(server\.py\|_launcher\.py\)" 2>/dev/null && echo "[STOP] Killed Python launcher" || true
    sleep 1
}

git_init() {
    echo "[GIT] Initializing repository..."
    local remote
    remote=$(cat "$GIT_REMOTE_FILE")
    git init -b main
    git config --global --add safe.directory "$PWD"
    git remote add origin "$remote"
    if ! git fetch --depth 1 origin main; then
        echo "[GIT] Fetch failed - check network."
        return 1
    fi
    git branch --set-upstream-to=origin/main main
    git reset origin/main
    git checkout origin/main -- .gitignore
    echo "[GIT] Initialized: $remote"
}

fix_remote() {
    local cur
    cur=$(git -c "safe.directory=$PWD" remote get-url origin 2>/dev/null || true)
    if [ "$cur" != "$CANONICAL_REMOTE" ]; then
        git -c "safe.directory=$PWD" remote set-url origin "$CANONICAL_REMOTE"
        echo "[GIT] Remote corrected: $cur -> $CANONICAL_REMOTE"
    fi
}

git_pull() {
    git -c "safe.directory=$PWD" fetch origin main
    git -c "safe.directory=$PWD" reset --hard origin/main
    echo "[GIT] Updated."
}

update_deps() {
    local req_hash_file="venv/.req_hash"
    local old_hash=""
    local new_hash
    [ -f "$req_hash_file" ] && old_hash=$(cat "$req_hash_file")
    new_hash=$(sha256sum requirements.txt | awk '{print $1}')

    local need_install=0
    [ "$new_hash" != "$old_hash" ] && need_install=1

    # critical modules 체크
    local critical_missing=0
    if ! venv/bin/python -c "import rapidocr_onnxruntime, rapidfuzz" >/dev/null 2>&1; then
        need_install=1
        critical_missing=1
    fi
    [ $need_install -eq 0 ] && return

    echo "[DEPS] Installing/updating packages..."
    if ! venv/bin/python -m pip install -r requirements.txt -q; then
        echo "[DEPS] Install failed - continuing with existing packages."
        return
    fi
    if [ $critical_missing -eq 1 ]; then
        if ! venv/bin/python -c "import rapidocr_onnxruntime, rapidfuzz" >/dev/null 2>&1; then
            echo "[DEPS] Critical modules still missing - installing directly..."
            venv/bin/python -m pip install rapidocr-onnxruntime rapidfuzz -q
        fi
    fi
    echo "$new_hash" > "$req_hash_file"
    echo "[DEPS] Dependencies updated."
}

update_ocr_models() {
    local sentinel="backend/app/services/ocr_models/korean/rec_infer.onnx"
    [ -f "$sentinel" ] && return
    echo "[OCR] Multilingual models not found - first-time setup..."
    if ! venv/bin/python -m paddle2onnx.command --version >/dev/null 2>&1; then
        echo "[OCR] Installing paddle2onnx + paddlepaddle (one-time, ~160MB)..."
        if ! venv/bin/python -m pip install paddle2onnx paddlepaddle -q; then
            echo "[OCR] Install failed - Korean OCR will fall back to bundled Chinese model."
            return
        fi
    fi
    venv/bin/python scripts/download_ocr_models.py || echo "[OCR] Model download partially failed."
}

stop_existing_server

# ---- Git 동기화 ----
if [ "$OFFLINE_MODE" = "1" ]; then
    echo "[GIT] Skipped (offline mode)."
elif [ ! -d ".git" ]; then
    if [ -f "$GIT_REMOTE_FILE" ] && command -v git >/dev/null 2>&1; then
        git_init || true
    fi
elif command -v git >/dev/null 2>&1; then
    [ "${1:-}" != "--home" ] && fix_remote
    git_pull
fi

# ---- 의존성 자동 업데이트 ----
if [ "$OFFLINE_MODE" = "1" ]; then
    echo "[DEPS] Skipped (offline mode)."
elif [ -x "venv/bin/python" ] && [ -f "requirements.txt" ]; then
    update_deps
fi

# ---- OCR 모델 (최초 부팅) ----
if [ "$OFFLINE_MODE" = "1" ]; then
    echo "[OCR] Skipped (offline mode - models must be bundled)."
elif [ -x "venv/bin/python" ] && [ -f "scripts/download_ocr_models.py" ]; then
    update_ocr_models
fi

# ---- 서버 시작 ----
ENTRY="server.py"
[ -f "_launcher.py" ] && ENTRY="_launcher.py"

if [ ! -x "venv/bin/python" ]; then
    echo "[ERROR] Python not found. Run ./setup.sh first."
    exit 1
fi

# DISPLAY 없으면 (헤드리스) GUI 없이 backend/frontend 만 직접 실행
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "[START] Headless mode - starting backend (uvicorn) directly"
    exec venv/bin/python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000
else
    echo "[START] venv/bin/python $ENTRY"
    exec venv/bin/python "$ENTRY"
fi
