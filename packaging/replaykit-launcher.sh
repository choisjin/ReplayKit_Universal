#!/usr/bin/env bash
# /usr/bin/ReplayKit — installed-mode launcher (placed by replaykit.deb).
#
# 전략:
#   1. 진짜 immutable 자산 (python/, frontend/dist/, tools/, docs/, scripts/)
#      → ~/.local/share/ReplayKit/<item> 으로 symlink (대용량, 변경 없음).
#   2. backend Python 소스 + server.py 등 → user dir 에 "복사" (실시간 쓰기 필요).
#      backend 코드가 Path(__file__).resolve().parent.parent.parent 로 PROJECT_ROOT
#      를 구하는데 .resolve() 가 symlink 를 따라가서 /opt (read-only) 로 가버리는
#      문제를 회피.
#   3. 사용자 쓰기 가능 디렉토리 (scenarios/screenshots/results/logs) 생성.
#   4. apt 업그레이드 후 /opt 의 version.txt 가 user dir 의 .installed-version 과
#      다르면 backend/ 등을 자동 재복사 (코드 stale 방지).
#   5. 임베디드 Python 으로 server.py (GUI) 또는 uvicorn (headless) 실행.

set -u

APP_DIR="/opt/ReplayKit"
USER_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ReplayKit"

if [ ! -d "$APP_DIR" ]; then
    echo "[ERROR] $APP_DIR 가 없습니다. 패키지가 손상되었거나 제거되었습니다." >&2
    exit 1
fi

mkdir -p "$USER_DATA"

# ---- 1. 사용자 쓰기 가능 디렉토리 시드 ----
for d in scenarios screenshots results logs; do
    mkdir -p "$USER_DATA/$d"
done

# ---- 2. read-only 대용량 자산: symlink ----
# (python: ~150MB embedded, frontend/dist: ~5MB built, tools: ~3MB, docs/scripts: 작음)
link_if_missing() {
    local item="$1"
    local src="$APP_DIR/$item"
    local dst="$USER_DATA/$item"
    if [ ! -e "$src" ]; then
        return
    fi
    # 기존 dst 가 일반 파일/디렉토리면 보존, symlink 면 갱신
    if [ -L "$dst" ] || [ ! -e "$dst" ]; then
        ln -sfn "$src" "$dst"
    fi
}
for item in python frontend tools docs scripts README_LINUX.md; do
    link_if_missing "$item"
done

# ---- 3. backend Python 소스: 복사 (쓰기 가능해야 함) ----
# 버전 변경 시 재동기화. /opt 의 version.txt 가 갱신되면 (apt upgrade) user dir 의
# backend 도 따라서 새로 복사.
INSTALLED_VERSION_FILE="$USER_DATA/.installed-version"
APP_VERSION_FILE="$APP_DIR/version.txt"
NEEDS_SYNC=0

if [ ! -d "$USER_DATA/backend" ] || [ -L "$USER_DATA/backend" ]; then
    # 처음이거나, 이전 launcher 가 symlink 로 만들어둔 경우
    NEEDS_SYNC=1
elif [ -f "$APP_VERSION_FILE" ] && [ -f "$INSTALLED_VERSION_FILE" ]; then
    if ! cmp -s "$APP_VERSION_FILE" "$INSTALLED_VERSION_FILE"; then
        NEEDS_SYNC=1
        echo "[SYNC] /opt 의 버전이 갱신됨 — backend 재복사."
    fi
elif [ -f "$APP_VERSION_FILE" ] && [ ! -f "$INSTALLED_VERSION_FILE" ]; then
    # 첫 launcher run with new versioning scheme
    NEEDS_SYNC=1
fi

if [ "$NEEDS_SYNC" = "1" ]; then
    echo "[INIT] backend 코드를 $USER_DATA 에 복사 중 (최초/업그레이드)..."

    # 기존 symlink 또는 사용자 수정사항 정리 (단, settings.json 같은 사용자 데이터는 보존)
    BACKUP_DIR=""
    if [ -d "$USER_DATA/backend" ] && [ ! -L "$USER_DATA/backend" ]; then
        # 사용자가 만들었을 수 있는 데이터 파일 백업
        BACKUP_DIR=$(mktemp -d -t replaykit-backup.XXXXXX)
        for keep in settings.json auxiliary_devices.json compositor_presets.json scan_settings.json device_catalog.json; do
            if [ -f "$USER_DATA/backend/$keep" ]; then
                cp -p "$USER_DATA/backend/$keep" "$BACKUP_DIR/$keep" || true
            fi
        done
    fi

    rm -rf "$USER_DATA/backend"
    cp -r "$APP_DIR/backend" "$USER_DATA/backend"

    # 백업된 사용자 데이터 복원 (apt 가 새로 제공한 것보다 우선)
    if [ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ]; then
        for keep in settings.json auxiliary_devices.json compositor_presets.json scan_settings.json device_catalog.json; do
            if [ -f "$BACKUP_DIR/$keep" ]; then
                cp -p "$BACKUP_DIR/$keep" "$USER_DATA/backend/$keep"
            fi
        done
        rm -rf "$BACKUP_DIR"
    fi

    # 단일 파일들도 복사 (server.py 가 PROJECT_ROOT 를 자기 위치 기준으로 잡음)
    for f in server.py _launcher.py requirements.txt version.txt; do
        if [ -e "$APP_DIR/$f" ]; then
            rm -f "$USER_DATA/$f"
            cp -p "$APP_DIR/$f" "$USER_DATA/$f"
        fi
    done

    # 버전 마커 갱신
    if [ -f "$APP_VERSION_FILE" ]; then
        cp -p "$APP_VERSION_FILE" "$INSTALLED_VERSION_FILE"
    fi

    echo "[INIT] 완료."
fi

cd "$USER_DATA"

# ---- 4. Python 환경 격리 ----
unset PYTHONHOME PYTHONPATH PYTHONSTARTUP
export PYTHONNOUSERSITE=1
export REPLAYKIT_INSTALLED=1
export REPLAYKIT_USER_DATA="$USER_DATA"
# main.py / settings.py 가 이미 인식하는 변수. .resolve() 가 우회하더라도 우선 사용.
export RECORDING_PROJECT_ROOT="$USER_DATA"

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
