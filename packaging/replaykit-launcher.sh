#!/usr/bin/env bash
# /usr/bin/ReplayKit — installed-mode launcher (placed by replaykit.deb).
#
# 서브커맨드:
#   ReplayKit            # 기본: DISPLAY 있으면 GUI launcher, 없으면 headless uvicorn
#   ReplayKit gui        # 명시적 GUI launcher (Tkinter 윈도우)
#   ReplayKit headless   # GUI 안 띄우고 uvicorn 직접 실행
#   ReplayKit stop       # graceful 종료 (SIGTERM → 5초 대기 → SIGKILL)
#   ReplayKit restart    # stop 후 start
#   ReplayKit status     # 실행 상태 확인
#
# 전략:
#   1. 진짜 immutable 자산 (python/, frontend/dist/, tools/, docs/, scripts/)
#      → ~/.local/share/ReplayKit/<item> 으로 symlink (대용량, 변경 없음).
#   2. backend Python 소스 + server.py 등 → user dir 에 "복사" (실시간 쓰기 필요).
#   3. 사용자 쓰기 가능 디렉토리 (scenarios/screenshots/results/logs) 생성.
#   4. apt 업그레이드 후 /opt 의 version.txt 가 user dir 의 .installed-version 과
#      다르면 backend/ 등을 자동 재복사 (코드 stale 방지).
#   5. 임베디드 Python + uvicorn 으로 backend.app.main:app 실행.
#   6. PID 파일 (~/.local/share/ReplayKit/replaykit.pid) 로 종료 명령 지원.

set -u

APP_DIR="/opt/ReplayKit"
USER_DATA="${XDG_DATA_HOME:-$HOME/.local/share}/ReplayKit"
PID_FILE="$USER_DATA/replaykit.pid"
PORT="${REPLAYKIT_PORT:-8000}"
SERVER_URL="http://localhost:${PORT}"

if [ ! -d "$APP_DIR" ]; then
    echo "[ERROR] $APP_DIR 가 없습니다. 패키지가 손상되었거나 제거되었습니다." >&2
    exit 1
fi

mkdir -p "$USER_DATA"

# ============================================================
# 서브커맨드 헬퍼 함수
# ============================================================

# PID 파일에서 PID 읽기 — 살아있고 우리 프로세스 (embedded python uvicorn) 면 echo, 아니면 빈값
get_running_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE" 2>/dev/null)
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            # 실제로 우리 launcher 가 띄운 uvicorn 인지 검증 (PID 재사용 방지)
            if grep -qE "/opt/ReplayKit/python|backend\.app\.main" "/proc/$pid/cmdline" 2>/dev/null; then
                echo "$pid"
                return 0
            fi
        fi
    fi
    # PID 파일 없거나 잘못됐으면 pgrep 으로 fallback 탐색
    if command -v pgrep >/dev/null 2>/dev/null; then
        pgrep -f "${APP_DIR}/python/bin/python3.*uvicorn.*backend.app.main" 2>/dev/null | head -1
    fi
}

do_stop() {
    local pid
    pid=$(get_running_pid)
    if [ -z "$pid" ]; then
        echo "ReplayKit 가 실행 중이지 않습니다."
        rm -f "$PID_FILE"
        return 0
    fi

    echo "ReplayKit 종료 중 (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null || true

    # 최대 5초 대기 (graceful)
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
        if ! kill -0 "$pid" 2>/dev/null; then
            echo "ReplayKit 정상 종료됨."
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 0.5
    done

    # 안 죽으면 SIGKILL
    echo "응답 없음 — 강제 종료 (SIGKILL)..."
    kill -KILL "$pid" 2>/dev/null || true
    sleep 0.3
    rm -f "$PID_FILE"
    echo "ReplayKit 강제 종료됨."
}

do_status() {
    local pid
    pid=$(get_running_pid)
    if [ -n "$pid" ]; then
        echo "ReplayKit 실행 중 (PID $pid)"
        echo "  URL:      $SERVER_URL"
        echo "  PID file: $PID_FILE"
        if command -v curl >/dev/null 2>/dev/null; then
            if curl -fsS "$SERVER_URL/openapi.json" >/dev/null 2>/dev/null; then
                echo "  Health:   응답 정상"
            else
                echo "  Health:   포트는 열렸지만 응답 없음 (startup 중일 수 있음)"
            fi
        fi
        return 0
    else
        echo "ReplayKit 종료 상태"
        return 1
    fi
}

# ============================================================
# 서브커맨드 디스패치
# ============================================================
# 기본 모드 결정: DISPLAY/WAYLAND_DISPLAY 있으면 gui, 없으면 headless.
# REPLAYKIT_FORCE_HEADLESS=1 로 강제 headless 가능.
if [ -n "${REPLAYKIT_FORCE_HEADLESS:-}" ]; then
    DEFAULT_MODE="headless"
elif [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
    DEFAULT_MODE="gui"
else
    DEFAULT_MODE="headless"
fi

LAUNCH_MODE="${1:-$DEFAULT_MODE}"

case "$LAUNCH_MODE" in
    stop)
        do_stop
        exit 0
        ;;
    status)
        do_status
        exit $?
        ;;
    restart)
        do_stop
        shift || true
        echo "재시작 중..."
        # DISPLAY 상황 다시 평가
        if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
            LAUNCH_MODE="gui"
        else
            LAUNCH_MODE="headless"
        fi
        ;;
    gui)
        shift || true
        ;;
    headless|start|--start)
        # start 는 legacy alias — 기본 모드 사용
        if [ "$LAUNCH_MODE" = "start" ] || [ "$LAUNCH_MODE" = "--start" ]; then
            LAUNCH_MODE="$DEFAULT_MODE"
        fi
        shift || true
        ;;
    -h|--help|help)
        sed -n '2,15p' "$0" | sed 's/^# \?//'
        exit 0
        ;;
    -*)
        # 옵션 — 기본 모드로 동작, 옵션은 그대로 $@
        LAUNCH_MODE="$DEFAULT_MODE"
        ;;
    *)
        echo "알 수 없는 명령: $LAUNCH_MODE" >&2
        echo "사용법: ReplayKit [gui|headless|stop|restart|status]" >&2
        exit 1
        ;;
esac

# ============================================================
# 여기서부터 start 로직 (gui 또는 headless 분기는 아래 exec 직전)
# ============================================================

# ---- 1. 사용자 쓰기 가능 디렉토리 시드 ----
for d in scenarios screenshots results logs; do
    mkdir -p "$USER_DATA/$d"
done

# ---- TTY 미연결 (아이콘 클릭 등) → launcher.log 로 출력 리다이렉트 ----
# stdout 이 terminal 이 아니면 (icon click / desktop entry / systemd) 모든 출력을
# 파일에 기록. 사용자가 'tail -f ~/.local/share/ReplayKit/logs/launcher.log' 로
# 진단 가능. 터미널에서 실행 시에는 평소처럼 화면에 출력.
LAUNCHER_LOG="$USER_DATA/logs/launcher.log"
if ! [ -t 1 ]; then
    # 로그 회전: 너무 커지면 잘라냄 (마지막 1MB만 유지)
    if [ -f "$LAUNCHER_LOG" ] && [ "$(stat -c %s "$LAUNCHER_LOG" 2>/dev/null || echo 0)" -gt 1048576 ]; then
        tail -c 524288 "$LAUNCHER_LOG" > "${LAUNCHER_LOG}.tmp" && mv "${LAUNCHER_LOG}.tmp" "$LAUNCHER_LOG"
    fi
    echo "" >> "$LAUNCHER_LOG"
    echo "===== $(date '+%Y-%m-%d %H:%M:%S') ReplayKit start =====" >> "$LAUNCHER_LOG"
    exec >> "$LAUNCHER_LOG" 2>> "$LAUNCHER_LOG"
fi

# 데스크탑 알림 헬퍼 (있을 때만)
notify_user() {
    local title="$1"
    local body="$2"
    if command -v notify-send >/dev/null 2>/dev/null && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then
        notify-send -i replaykit "$title" "$body" 2>/dev/null || true
    fi
}

# 치명적 오류 처리: 사용자 알림 + 로그 안내
fatal() {
    local msg="$1"
    echo "[FATAL] $msg" >&2
    notify_user "ReplayKit 시작 실패" "$msg
자세한 내용: $LAUNCHER_LOG"
    exit 1
}

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

# ---- 포트 충돌 감지 / 이미 실행 중 처리 ----
# 같은 포트에 이미 다른 프로세스 (또는 우리 인스턴스) 가 바인드 중이면
# uvicorn 이 OSError: address already in use 로 즉시 죽음.
# 우리 인스턴스면 그냥 브라우저만 오픈 (UX 향상), 다른 프로세스면 친절히 안내.

port_in_use() {
    if command -v ss >/dev/null 2>/dev/null; then
        ss -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${PORT}\$"
    elif command -v netstat >/dev/null 2>/dev/null; then
        netstat -tln 2>/dev/null | awk '{print $4}' | grep -qE ":${PORT}\$"
    elif command -v lsof >/dev/null 2>/dev/null; then
        lsof -iTCP:${PORT} -sTCP:LISTEN >/dev/null 2>/dev/null
    else
        return 1   # 검사 도구 없으면 그냥 시도
    fi
}

if port_in_use; then
    OUR_PID=$(get_running_pid)
    if [ -n "$OUR_PID" ]; then
        # 우리 ReplayKit 가 이미 실행 중
        if [ "$LAUNCH_MODE" = "gui" ]; then
            # GUI 모드: GUI 윈도우를 띄우되, 서버는 기존 인스턴스에 연결
            # (replaykit-gui.py 자체가 PID 파일 보고 상태 표시함)
            echo "[INFO] ReplayKit 이미 실행 중 (PID $OUR_PID). GUI 윈도우 띄우는 중..."
            # 아래 GUI 실행 로직으로 폴쓰루
        else
            # headless 모드: 새로 안 띄우고 브라우저만 오픈
            echo "[INFO] ReplayKit 이미 실행 중 (PID $OUR_PID). 브라우저로 접속합니다."
            if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
                if command -v xdg-open >/dev/null 2>/dev/null; then
                    xdg-open "$SERVER_URL" >/dev/null 2>/dev/null &
                    exit 0
                fi
            fi
            echo "    수동 접속: $SERVER_URL"
            echo "    종료:      ReplayKit stop"
            exit 0
        fi
    else
        fatal "포트 ${PORT} 가 다른 프로세스에 의해 사용 중입니다 (ReplayKit 외부).
다른 포트로 실행:    REPLAYKIT_PORT=9000 ReplayKit
또는 점유 프로세스 확인:
    sudo fuser ${PORT}/tcp
    sudo lsof -iTCP:${PORT} -sTCP:LISTEN"
    fi
fi

# ============================================================
# 실행 분기: GUI 모드 vs Headless 모드
# ============================================================
if [ "$LAUNCH_MODE" = "gui" ]; then
    # GUI 모드: Tkinter launcher 띄움. 서버 제어는 GUI 윈도우에서.
    GUI_SCRIPT="$APP_DIR/replaykit-gui.py"
    if [ ! -f "$GUI_SCRIPT" ]; then
        echo "[WARN] $GUI_SCRIPT 없음 — headless 모드로 폴백" >&2
        LAUNCH_MODE="headless"
    else
        echo "[START] GUI launcher: $PY $GUI_SCRIPT"
        # GUI launcher 가 직접 uvicorn 을 subprocess 로 관리하므로, 여기선 서버 실행
        # 하지 않음. GUI 윈도우가 사용자에게 보임 → 종료 버튼으로 깔끔하게.
        export REPLAYKIT_APP_DIR="$APP_DIR"
        exec "$PY" "$GUI_SCRIPT" "$@"
    fi
fi

# ============================================================
# Headless 모드: uvicorn 직접 실행 + 브라우저 자동 오픈
# ============================================================
want_browser=0
if [ -z "${REPLAYKIT_NO_BROWSER:-}" ]; then
    if [ -n "${DISPLAY:-}" ] || [ -n "${WAYLAND_DISPLAY:-}" ]; then
        if command -v xdg-open >/dev/null 2>/dev/null; then
            want_browser=1
        else
            echo "[WEB] xdg-open 명령 없음 — 브라우저 자동 오픈 생략 (sudo apt install xdg-utils)"
        fi
    fi
fi

if [ "$want_browser" = "1" ]; then
    (
        for i in $(seq 1 30); do
            sleep 0.5
            if curl -fsS "${SERVER_URL}/openapi.json" >/dev/null 2>/dev/null; then
                echo "[WEB] 서버 ready → 브라우저 오픈: ${SERVER_URL}"
                xdg-open "${SERVER_URL}" >/dev/null 2>/dev/null &
                exit 0
            fi
        done
        echo "[WEB] 서버 ready 대기 15초 초과 — 브라우저는 수동으로 ${SERVER_URL} 열어주세요." >&2
    ) &
fi

if [ "$want_browser" = "1" ]; then
    echo "[START] uvicorn at ${SERVER_URL} (헤드리스 + 브라우저 자동 오픈)"
else
    if [ -n "${REPLAYKIT_NO_BROWSER:-}" ]; then
        echo "[START] uvicorn at ${SERVER_URL} (REPLAYKIT_NO_BROWSER=1, 헤드리스)"
    else
        echo "[START] uvicorn at ${SERVER_URL} (DISPLAY 없음, 헤드리스)"
    fi
fi
echo "    종료:    ReplayKit stop    또는 Ctrl+C"

# PID 파일에 자기 PID 기록. exec 가 현재 셸 프로세스를 uvicorn 으로 대체하므로
# bash 의 $$ 가 그대로 uvicorn 의 PID 가 된다.
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT INT TERM

exec "$PY" -m uvicorn backend.app.main:app --host 0.0.0.0 --port "$PORT" "$@"
