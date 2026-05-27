#!/usr/bin/env bash
# ReplayKit dev 모드 launcher — Tkinter 기반 server.py 우회용.
#
# 사유: embedded Python (python-build-standalone) 의 bundled Tk 가 시스템 libxcb 와
#       ABI 호환 안 됨 → ReplayKit.sh 가 띄우는 server.py 실행 시 xcb assertion
#       (xcb_xlib_unknown_seq_number) 으로 core dump.
#       .deb 배포 패키지에선 packaging/replaykit-gui.py (PySide6) 가 정상 동작하지만
#       dev 트리에는 그 PySide6 launcher 가 포함돼 있지 않아 server.py 가 실행됨.
#
# 동작: server.py(GUI 컨트롤 패널)를 건너뛰고 uvicorn 백엔드만 직접 실행.
#       LinControl/녹화/재생 등 핵심 기능은 모두 백엔드 단독으로 동작 — GUI 컨트롤
#       패널이 없을 뿐이며 브라우저로 접속해 사용.
#
# 환경 변수:
#   REPLAYKIT_NO_BROWSER=1   서버 ready 후 브라우저 자동 오픈 비활성
#   REPLAYKIT_PORT=9000      기본 8000 대신 다른 포트 사용
#
# 사용:
#   ./dev-run.sh             — 백엔드 실행 + 브라우저 자동 오픈 (http://localhost:8000)
#   Ctrl+C                   — 종료

set -u
cd "$(dirname "$(readlink -f "$0")")"

# 시스템 Python 환경 변수 격리 (cv2/.so 로딩 충돌 방지)
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
export PYTHONNOUSERSITE=1

# Python 우선순위: embedded > venv > 시스템
PY=""
if [ -x "python/bin/python3" ]; then
    PY="python/bin/python3"
elif [ -x "venv/bin/python" ]; then
    PY="venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PY="python3"
else
    echo "[ERROR] Python 을 찾을 수 없음. ./setup.sh 또는 ./scripts/install_embedded_python.sh 먼저 실행." >&2
    exit 1
fi
echo "[DEV] Python: $PY ($("$PY" --version 2>&1))"

PORT="${REPLAYKIT_PORT:-8000}"

# frontend/dist 자동 빌드 — 백엔드가 정적 파일을 서빙하려면 dist/index.html 필요.
# 없으면 npm install (node_modules 부재 시) + npm run build 1회 수행.
# 빌드된 결과는 git ignore 되어 있어 항상 로컬에서 생성됨.
if [ ! -f "frontend/dist/index.html" ]; then
    echo "[DEV] frontend/dist/index.html 없음 — 자동 빌드 시도"
    if ! command -v npm >/dev/null 2>&1; then
        echo "[ERROR] npm 미설치. 다음 중 하나로 해결:"
        echo "  a) sudo apt install -y nodejs npm"
        echo "  b) NodeSource (권장): curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - && sudo apt install -y nodejs"
        echo "  c) 별도 머신에서 빌드 후 frontend/dist 복사"
        exit 1
    fi
    if [ ! -d "frontend/node_modules" ]; then
        echo "[DEV] cd frontend && npm install  (최초 1회, ~1~3분 소요)"
        (cd frontend && npm install) || {
            echo "[ERROR] npm install 실패 — 위 로그 확인"
            exit 1
        }
    fi
    echo "[DEV] cd frontend && npm run build"
    (cd frontend && npm run build) || {
        echo "[ERROR] npm run build 실패 — 위 로그 확인"
        exit 1
    }
    if [ ! -f "frontend/dist/index.html" ]; then
        echo "[ERROR] 빌드 끝났지만 frontend/dist/index.html 가 여전히 없음 — vite.config 확인 필요"
        exit 1
    fi
    echo "[DEV] frontend/dist 생성 완료"
fi

# 기존 백엔드 종료 (8000/5173 모두 — 사용자가 ReplayKit.sh 로 띄웠던 잔재 정리)
for port in "$PORT" 5173; do
    if command -v fuser >/dev/null 2>&1; then
        fuser -k "${port}/tcp" >/dev/null 2>&1 && echo "[DEV] Killed process on port ${port}" || true
    elif command -v lsof >/dev/null 2>&1; then
        pids=$(lsof -ti tcp:"${port}" 2>/dev/null || true)
        [ -n "$pids" ] && kill -9 $pids 2>/dev/null && echo "[DEV] Killed PID(s) ${pids} on port ${port}" || true
    fi
done
sleep 1

# 브라우저 자동 오픈 — 서버가 listen 시작 직후 (REPLAYKIT_NO_BROWSER=1 로 끌 수 있음)
if [ -z "${REPLAYKIT_NO_BROWSER:-}" ] && command -v xdg-open >/dev/null 2>&1; then
    (
        # ready 감지 — TCP 8000 이 LISTEN 상태가 될 때까지 최대 30초 폴링
        for _ in $(seq 1 60); do
            sleep 0.5
            if command -v ss >/dev/null 2>&1; then
                ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN && break
            else
                (echo > "/dev/tcp/127.0.0.1/$PORT") >/dev/null 2>&1 && break
            fi
        done
        xdg-open "http://localhost:$PORT" >/dev/null 2>&1 || true
    ) &
fi

echo "[DEV] uvicorn backend.app.main:app --host 0.0.0.0 --port $PORT"
echo "[DEV] 브라우저 → http://localhost:$PORT  (Ctrl+C 로 종료)"
exec "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT"
