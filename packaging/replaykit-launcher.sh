#!/usr/bin/env bash
# /usr/bin/ReplayKit — installed-mode launcher (placed by replaykit.deb).
#
# 동작:
#   1. ~/.local/share/ReplayKit/ 를 사용자 데이터 루트로 사용 (XDG 호환).
#   2. /opt/ReplayKit/ 의 immutable 자산을 사용자 데이터 루트에 심볼릭링크.
#   3. 임베디드 Python (/opt/ReplayKit/python/bin/python3) 로 server.py 또는
#      headless 모드 (DISPLAY 없을 때) uvicorn 직접 실행.
#
# 시스템 Python / 환경변수에 의존하지 않음.

set -u

APP_DIR="/opt/ReplayKit"
USER_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ReplayKit"

if [ ! -d "$APP_DIR" ]; then
    echo "[ERROR] $APP_DIR 가 없습니다. 패키지가 손상되었거나 제거되었습니다." >&2
    exit 1
fi

mkdir -p "$USER_DATA"

# 사용자 쓰기 가능 디렉토리 시드 (최초 실행 시 자동 생성)
for d in scenarios screenshots results logs; do
    mkdir -p "$USER_DATA/$d"
done

# /opt 의 immutable 자산을 사용자 데이터 루트로 심볼릭링크 (이미 있으면 건너뜀)
for item in backend frontend tools scripts python requirements.txt version.txt server.py _launcher.py README_LINUX.md; do
    src="$APP_DIR/$item"
    dst="$USER_DATA/$item"
    if [ -e "$src" ] && [ ! -e "$dst" ]; then
        ln -sf "$src" "$dst"
    fi
done

# settings.json 은 사용자별로 가져야 하므로 첫 실행 시 복사 (read-write)
if [ ! -f "$USER_DATA/backend/settings.json" ] && [ -f "$APP_DIR/backend/settings.json" ]; then
    # backend 는 심볼릭링크라 직접 쓰면 /opt 에 쓰려 함 — 사용자별 복사본을 별도 위치에
    # 두는 대신, settings.json 만 풀어서 덮어쓸 수 있게 한다.
    : # 현재 backend/ 전체가 심볼릭링크라 settings.json 변경은 read-only 환경에서 발생.
      # 추후 backend 코드를 XDG_CONFIG_HOME 사용하도록 수정하면 깔끔해짐.
fi

cd "$USER_DATA"

# Python 환경 격리
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
export PYTHONNOUSERSITE=1
export REPLAYKIT_INSTALLED=1
export REPLAYKIT_USER_DATA="$USER_DATA"

PY="$APP_DIR/python/bin/python3"
if [ ! -x "$PY" ]; then
    echo "[ERROR] 임베디드 Python 을 찾을 수 없습니다: $PY" >&2
    exit 1
fi

ENTRY="server.py"
[ -f "$USER_DATA/_launcher.py" ] && ENTRY="_launcher.py"

# DISPLAY 없으면 headless (서버 단독 실행)
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
    echo "[START] Headless mode — uvicorn at http://localhost:8000"
    exec "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 "$@"
else
    echo "[START] $PY $ENTRY (mode=installed)"
    exec "$PY" "$ENTRY" "$@"
fi
