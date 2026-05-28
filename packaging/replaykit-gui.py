#!/usr/bin/env python3
"""ReplayKit GUI Launcher — Linux (PySide6).

Windows server.py 의 borderless 미니멀 위젯 디자인 (360x70 우하단 floating) 을
PySide6 로 포팅. embedded Python (/opt/ReplayKit/python/bin/python3) 에 설치된
PySide6 로 실행 — 시스템 Python 의존 없음.

기능:
  ┌─ ReplayKit ──────────────  ━  ✕ ┐
  │  ▶  ↻  🌐         실행 중 PID    │
  └─────────────────────────────────┘

- ▶/■ 토글 (시작/종료)
- ↻ 재시작
- 🌐 브라우저 열기
- ━ 화면 모서리로 이동
- ✕ 창 닫기 (서버는 살려둠 — 명시적 종료는 ■)
- 타이틀 클릭+드래그로 윈도우 이동
- backend/settings.json theme 따라 dark/light 적용

PyQt 가 아닌 PySide6 (LGPL, Qt for Python) 를 사용하여 .deb 배포 가능.
embedded Python 의 bundled Tkinter 가 libxcb 와 ABI 충돌하는 문제 회피.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtGui import QFont, QCursor, QMouseEvent
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton,
    QHBoxLayout, QVBoxLayout, QFrame,
)


# ─── 경로 / 환경 ───────────────────────────────────────────
APP_DIR = Path(os.environ.get("REPLAYKIT_APP_DIR", "/opt/ReplayKit"))
USER_DATA = Path(
    os.environ.get(
        "REPLAYKIT_USER_DATA",
        os.path.join(
            os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")),
            "ReplayKit",
        ),
    )
)
PY = APP_DIR / "python" / "bin" / "python3"
PORT = int(os.environ.get("REPLAYKIT_PORT", "8000"))
SERVER_URL = f"http://localhost:{PORT}"
PID_FILE = USER_DATA / "replaykit.pid"
# 백엔드가 자가 업데이트 후 작성하는 플래그. 본 launcher 가 2초 polling 으로 감지해서
# 자식 (uvicorn) 을 죽이고 재시작 → 새로 동기화된 backend 코드를 로드.
# uvicorn 자체적으로 os.execv 하면 listening socket EADDRINUSE 로 재바인드 실패하므로
# 반드시 launcher 가 자식 교체 방식으로 재시작해야 함 (Windows server.py 와 동일 패턴).
RESTART_FLAG = USER_DATA / ".restart"


# ─── 테마 (Windows server.py 와 동일 팔레트) ──────────────
def _read_theme() -> str:
    try:
        with open(USER_DATA / "backend" / "settings.json", encoding="utf-8") as f:
            return json.load(f).get("theme", "light")
    except Exception:
        return "dark"


_DARK = {"BG": "#1e1e2e", "BG_CARD": "#2a2a3d", "FG": "#cdd6f4", "FG_DIM": "#6c7086",
         "GREEN": "#a6e3a1", "RED": "#f38ba8", "YELLOW": "#f9e2af",
         "ACCENT": "#cba6f7"}
_LIGHT = {"BG": "#f5f5f5", "BG_CARD": "#ffffff", "FG": "#1f1f1f", "FG_DIM": "#888888",
          "GREEN": "#389e0d", "RED": "#cf1322", "YELLOW": "#d48806",
          "ACCENT": "#722ed1"}
_C = _DARK if _read_theme() == "dark" else _LIGHT


# ─── PID 추적 ──────────────────────────────────────────────
def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read()
        return b"uvicorn" in cmdline and b"backend.app.main" in cmdline
    except OSError:
        return False


def get_running_pid() -> int | None:
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    return None


# ─── GUI ───────────────────────────────────────────────────
class Launcher(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ReplayKit")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setFixedSize(360, 70)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self._server_proc: subprocess.Popen | None = None
        self._drag_pos: QPoint | None = None
        # 브라우저 자동 오픈: _start() 호출 시 True, ready 후 또는 timeout 시 False
        self._pending_browser_open = False
        self._browser_check_attempts = 0

        self._build_ui()
        self._position_bottom_right()

        # 폴링 타이머 (1초)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._poll_status)
        self.timer.start(1000)
        self._poll_status()

        # 재시작 플래그 감시 타이머 — backend 가 .restart 플래그 작성 시 자식 재시작.
        # Windows server.py 의 _check_restart_flag 와 동일 역할.
        self._restart_in_progress = False
        self.restart_timer = QTimer(self)
        self.restart_timer.timeout.connect(self._check_restart_flag)
        self.restart_timer.start(2000)

        # 자동 시작 (REPLAYKIT_NO_AUTOSTART=1 로 비활성화 가능).
        # 윈도우가 화면에 그려진 후 시작하도록 500ms 지연 — UX 향상.
        if not os.environ.get("REPLAYKIT_NO_AUTOSTART"):
            QTimer.singleShot(500, self._auto_start)

    # ----- UI 빌드 -----
    def _build_ui(self) -> None:
        # 외곽선 프레임 (1px FG_DIM)
        self.setStyleSheet(f"""
            QWidget {{ background-color: {_C['BG']}; color: {_C['FG']}; font-family: 'DejaVu Sans'; }}
            QFrame#outer {{ border: 1px solid {_C['FG_DIM']}; }}
        """)

        outer = QFrame(self)
        outer.setObjectName("outer")
        outer.setGeometry(0, 0, 360, 70)

        v = QVBoxLayout(outer)
        v.setContentsMargins(1, 1, 1, 1)
        v.setSpacing(0)

        # ── 타이틀 행 ───────────────────────────────────
        title_row = QHBoxLayout()
        title_row.setContentsMargins(10, 4, 4, 0)
        title_row.setSpacing(0)

        self.title_lbl = QLabel("ReplayKit")
        f = QFont("DejaVu Sans", 11, QFont.Weight.Bold)
        self.title_lbl.setFont(f)
        self.title_lbl.setStyleSheet(f"color: {_C['ACCENT']}; background: transparent;")
        self.title_lbl.setCursor(QCursor(Qt.CursorShape.SizeAllCursor))
        # 드래그용 — installEventFilter 대신 mouse* 이벤트 직접 처리.
        # 라벨에서 시작된 클릭은 self 의 mousePressEvent 에 자동 전달되도록
        # 라벨에 mouseTracking 활성화.
        title_row.addWidget(self.title_lbl)
        title_row.addStretch()

        # 최소화 / 종료 버튼
        for text, color, slot in [("━", _C['FG_DIM'], self._minimize),
                                  ("✕", _C['RED'], self._quit)]:
            btn = self._mk_ctrl_btn(text, color, slot)
            title_row.addWidget(btn)

        v.addLayout(title_row)

        # ── 컨트롤 행 ───────────────────────────────────
        ctrl_row = QHBoxLayout()
        ctrl_row.setContentsMargins(10, 2, 10, 2)
        ctrl_row.setSpacing(4)

        # ▶/■ 토글
        self.btn_toggle = QPushButton("▶")
        self.btn_toggle.setFixedSize(36, 30)
        self.btn_toggle.setFont(QFont("DejaVu Sans", 14, QFont.Weight.Bold))
        self.btn_toggle.setStyleSheet(self._btn_style(_C['GREEN']))
        self.btn_toggle.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self.btn_toggle.clicked.connect(self._toggle)
        ctrl_row.addWidget(self.btn_toggle)

        # ↻ 재시작
        btn_restart = QPushButton("↻")
        btn_restart.setFixedSize(32, 30)
        btn_restart.setFont(QFont("DejaVu Sans", 12, QFont.Weight.Bold))
        btn_restart.setStyleSheet(self._btn_style(_C['YELLOW']))
        btn_restart.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_restart.clicked.connect(self._restart)
        ctrl_row.addWidget(btn_restart)

        # 🌐 브라우저
        btn_web = QPushButton("🌐")
        btn_web.setFixedSize(32, 30)
        btn_web.setFont(QFont("DejaVu Sans", 11))
        btn_web.setStyleSheet(self._btn_style(_C['ACCENT']))
        btn_web.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        btn_web.clicked.connect(self._open_web)
        ctrl_row.addWidget(btn_web)

        ctrl_row.addStretch()

        # 상태 텍스트
        self.status_lbl = QLabel("확인 중")
        self.status_lbl.setFont(QFont("DejaVu Sans", 9))
        self.status_lbl.setStyleSheet(f"color: {_C['FG_DIM']}; background: transparent;")
        self.status_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        ctrl_row.addWidget(self.status_lbl)

        v.addLayout(ctrl_row)

    def _mk_ctrl_btn(self, text: str, color: str, slot) -> QLabel:
        """타이틀바 우측의 최소화/종료 버튼 (Label 로 만들어 hover 효과)."""
        lbl = QLabel(text)
        lbl.setFont(QFont("DejaVu Sans", 10))
        lbl.setStyleSheet(
            f"color: {color}; background: transparent; padding: 0 6px;"
        )
        lbl.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lbl.mousePressEvent = lambda e, s=slot: s()  # type: ignore[assignment]
        # Hover effect
        orig_style = lbl.styleSheet()
        hover_style = orig_style.replace("background: transparent", f"background-color: {_C['BG_CARD']}")
        lbl.enterEvent = lambda e, l=lbl, s=hover_style: l.setStyleSheet(s)  # type: ignore[assignment]
        lbl.leaveEvent = lambda e, l=lbl, s=orig_style: l.setStyleSheet(s)  # type: ignore[assignment]
        return lbl

    def _btn_style(self, color: str) -> str:
        return f"""
            QPushButton {{
                background-color: {_C['BG_CARD']};
                color: {color};
                border: none;
                border-radius: 3px;
            }}
            QPushButton:hover {{
                background-color: {_C['BG']};
                border: 1px solid {color};
            }}
            QPushButton:pressed {{
                background-color: {_C['FG_DIM']};
            }}
        """

    def _position_bottom_right(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.move(screen.right() - self.width() - 20, screen.bottom() - self.height() - 20)

    # ----- 드래그 이동 -----
    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_pos = None

    # ----- 상태 폴링 -----
    def _poll_status(self) -> None:
        pid = get_running_pid()
        if pid is None and self._server_proc and self._server_proc.poll() is None:
            pid = self._server_proc.pid

        if pid:
            self.btn_toggle.setText("■")
            self.btn_toggle.setStyleSheet(self._btn_style(_C['RED']))
            self.status_lbl.setText(f"실행 중  PID {pid}")
            self.status_lbl.setStyleSheet(f"color: {_C['GREEN']}; background: transparent;")
            self.setWindowTitle("ReplayKit — 실행 중")
        else:
            self.btn_toggle.setText("▶")
            self.btn_toggle.setStyleSheet(self._btn_style(_C['GREEN']))
            self.status_lbl.setText("정지")
            self.status_lbl.setStyleSheet(f"color: {_C['FG_DIM']}; background: transparent;")
            self.setWindowTitle("ReplayKit — 정지")

    # ----- 자동 시작 / 브라우저 자동 오픈 -----
    def _auto_start(self) -> None:
        """앱 시작 시 자동 호출. 서버가 안 떠 있으면 시작, 떠 있으면 브라우저만."""
        if get_running_pid() is None:
            self._start()
        else:
            # 이미 실행 중 → 브라우저만 오픈 (사용자가 아이콘 두 번 클릭한 경우 등)
            if not os.environ.get("REPLAYKIT_NO_BROWSER"):
                self._open_web()

    def _check_and_open_browser(self) -> None:
        """서버 ready 감지 후 브라우저 자동 오픈. 최대 30초 시도."""
        if not self._pending_browser_open:
            return
        self._browser_check_attempts += 1
        if self._browser_check_attempts > 30:
            # 30초 timeout — 포기
            self._pending_browser_open = False
            self.status_lbl.setText("서버 ready 대기 timeout")
            self.status_lbl.setStyleSheet(f"color: {_C['RED']}; background: transparent;")
            return
        try:
            urllib.request.urlopen(f"{SERVER_URL}/openapi.json", timeout=0.5)
            # ready! 브라우저 오픈
            self._pending_browser_open = False
            if not os.environ.get("REPLAYKIT_NO_BROWSER"):
                self._open_web()
        except (urllib.error.URLError, OSError):
            # 아직 ready 아님 → 1초 후 재시도
            QTimer.singleShot(1000, self._check_and_open_browser)

    # ----- 서버 제어 -----
    def _toggle(self) -> None:
        if get_running_pid():
            self._stop()
        else:
            self._start()

    def _start(self) -> None:
        if get_running_pid() is not None:
            return
        if not PY.exists():
            self.status_lbl.setText("Python 없음")
            return

        env = os.environ.copy()
        for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(k, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["REPLAYKIT_USER_DATA"] = str(USER_DATA)
        env["RECORDING_PROJECT_ROOT"] = str(USER_DATA)
        env["REPLAYKIT_INSTALLED"] = "1"

        USER_DATA.mkdir(parents=True, exist_ok=True)
        (USER_DATA / "logs").mkdir(parents=True, exist_ok=True)

        try:
            self._server_proc = subprocess.Popen(
                [str(PY), "-m", "uvicorn", "backend.app.main:app",
                 "--host", "0.0.0.0", "--port", str(PORT)],
                cwd=str(USER_DATA), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            PID_FILE.write_text(str(self._server_proc.pid))
            self.status_lbl.setText("시작 중...")
            self.status_lbl.setStyleSheet(f"color: {_C['YELLOW']}; background: transparent;")
            # ready 감지 → 브라우저 자동 오픈 시퀀스 시작
            self._pending_browser_open = True
            self._browser_check_attempts = 0
            QTimer.singleShot(1500, self._check_and_open_browser)  # 1.5s 후 첫 체크
        except Exception as e:
            self.status_lbl.setText(f"오류: {e}"[:40])
            self.status_lbl.setStyleSheet(f"color: {_C['RED']}; background: transparent;")

    def _stop(self) -> None:
        pid = get_running_pid()
        if pid is None:
            return
        # 종료 중에 브라우저 자동 오픈 시퀀스가 돌고 있으면 취소
        self._pending_browser_open = False
        try:
            os.kill(pid, signal.SIGTERM)
            self.status_lbl.setText("종료 중...")
            self.status_lbl.setStyleSheet(f"color: {_C['YELLOW']}; background: transparent;")
            QTimer.singleShot(5000, lambda p=pid: self._force_kill(p))
        except OSError as e:
            self.status_lbl.setText(f"오류: {e}"[:40])

    def _force_kill(self, pid: int) -> None:
        if is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        try:
            PID_FILE.unlink()
        except OSError:
            pass

    def _restart(self) -> None:
        if get_running_pid():
            self._stop()
            QTimer.singleShot(2500, self._start)
        else:
            self._start()

    def _check_restart_flag(self) -> None:
        """backend 가 자가 업데이트 후 .restart 플래그 작성 시 호출됨.

        flag 를 제거하고 자식 (uvicorn) 을 죽인 후 재시작 → 새 코드 로드.
        _restart 와 거의 동일하지만 사용자 트리거가 아닌 backend 트리거이므로 별도 함수.
        """
        try:
            if not RESTART_FLAG.exists():
                return
        except OSError:
            return
        if self._restart_in_progress:
            return  # 이미 처리 중 — 중복 호출 방지
        self._restart_in_progress = True
        try:
            RESTART_FLAG.unlink(missing_ok=True)
        except Exception:
            pass
        self.status_lbl.setText("업데이트 적용 중...")
        self.status_lbl.setStyleSheet(f"color: {_C['YELLOW']}; background: transparent;")
        # 자식 종료 후 약간 기다린 다음 새로 시작 (포트 해제 대기)
        if get_running_pid():
            self._stop()
            QTimer.singleShot(3000, self._post_restart_start)
        else:
            QTimer.singleShot(500, self._post_restart_start)

    def _post_restart_start(self) -> None:
        """_check_restart_flag 에서 stop 후 호출 — 재시작 + 플래그 해제."""
        self._start()
        self._restart_in_progress = False

    def _open_web(self) -> None:
        try:
            webbrowser.open(SERVER_URL)
        except Exception:
            try:
                subprocess.Popen(["xdg-open", SERVER_URL])
            except FileNotFoundError:
                pass

    def _minimize(self) -> None:
        # FramelessWindowHint + Tool 윈도우는 일부 DE 에서 iconify 가 잘 안 됨.
        # 안전하게 좌상단으로 이동 (사용자가 다시 드래그해 가져올 수 있게).
        self.move(0, 0)

    def _quit(self) -> None:
        # 서버는 살려두고 윈도우만 종료 (서버 종료는 ■ 버튼)
        QApplication.quit()


def main() -> int:
    if not APP_DIR.exists():
        sys.stderr.write(f"[ERROR] {APP_DIR} 없음.\n")
        return 1
    app = QApplication(sys.argv)
    # X11 에서 Qt 가 자체 멀티스레딩 처리 → libxcb 충돌 없음
    launcher = Launcher()
    launcher.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
