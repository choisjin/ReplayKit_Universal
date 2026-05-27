#!/usr/bin/python3
"""ReplayKit GUI Launcher — Linux.

Windows server.py 의 borderless 미니 launcher 와 동일한 스타일:
  - 360x70 우하단 floating 위젯
  - 5개 버튼: ▶/■ 토글, ↻ 재시작, 🌐 브라우저, ━ 최소화, ✕ 종료
  - 우측에 상태 텍스트
  - 타이틀 클릭+드래그로 이동

!!중요!! 이 스크립트는 **시스템 Python (/usr/bin/python3)** 으로 실행됨.
embedded Python (python-build-standalone) 의 bundled Tk 가 Linux 최신 libxcb
와 ABI 호환이 안 되어 import tkinter 만으로 'xcb_xlib_unknown_seq_number'
assertion 으로 죽기 때문. 백엔드 uvicorn 만 embedded Python 으로 subprocess
실행 — GUI 와 백엔드는 분리된 Python.

stdlib 만 사용하므로 (tkinter, subprocess, os, signal, webbrowser, pathlib, json)
시스템 python3 + python3-tk 만 있으면 동작. Ubuntu 22.04+ 기본.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

import tkinter as tk


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


# ─── 색상 (Windows server.py 의 다크 테마 그대로) ────────
def _read_theme() -> str:
    try:
        with open(USER_DATA / "backend" / "settings.json", encoding="utf-8") as f:
            return json.load(f).get("theme", "dark")
    except Exception:
        return "dark"


_DARK = {"BG": "#1e1e2e", "BG_CARD": "#2a2a3d", "FG": "#cdd6f4", "FG_DIM": "#6c7086",
         "GREEN": "#a6e3a1", "RED": "#f38ba8", "YELLOW": "#f9e2af",
         "ACCENT": "#cba6f7"}
_LIGHT = {"BG": "#f5f5f5", "BG_CARD": "#ffffff", "FG": "#1f1f1f", "FG_DIM": "#888888",
          "GREEN": "#389e0d", "RED": "#cf1322", "YELLOW": "#d48806",
          "ACCENT": "#722ed1"}
_C = _DARK if _read_theme() == "dark" else _LIGHT
BG, BG_CARD, FG, FG_DIM = _C["BG"], _C["BG_CARD"], _C["FG"], _C["FG_DIM"]
GREEN, RED, YELLOW, ACCENT = _C["GREEN"], _C["RED"], _C["YELLOW"], _C["ACCENT"]


# ─── PID 관리 ──────────────────────────────────────────────
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
class Launcher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ReplayKit")
        self._build_ui()

        self._drag = {"x": 0, "y": 0}
        self._proc: subprocess.Popen | None = None
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.after(0, self._poll_status)

    # ----- UI -----
    def _build_ui(self) -> None:
        self.root.overrideredirect(True)
        self.root.geometry("360x70")
        self.root.configure(bg=BG)

        # 우하단 위치
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"+{sw - 380}+{sh - 120}")

        # 1px 외곽선
        outer = tk.Frame(self.root, bg=FG_DIM, bd=1, relief="solid")
        outer.pack(fill="both", expand=True)
        main = tk.Frame(outer, bg=BG)
        main.pack(fill="both", expand=True, padx=1, pady=1)

        # 타이틀 행
        title_bar = tk.Frame(main, bg=BG)
        title_bar.pack(fill="x")

        title = tk.Label(title_bar, text="ReplayKit", bg=BG, fg=ACCENT,
                         font=("DejaVu Sans", 11, "bold"), cursor="fleur")
        title.pack(side="left", padx=(10, 0), pady=(4, 0))
        title.bind("<Button-1>", lambda e: self._drag.update(x=e.x, y=e.y))
        title.bind("<B1-Motion>", self._on_drag)

        # 우측: 최소화 / 종료
        ctrl = tk.Frame(title_bar, bg=BG)
        ctrl.pack(side="right", padx=(0, 4), pady=(4, 0))
        for text, color, cmd in [("━", FG_DIM, self._minimize),
                                 ("✕", RED, self._quit)]:
            b = tk.Label(ctrl, text=text, bg=BG, fg=color,
                         font=("DejaVu Sans", 10), cursor="hand2", padx=6)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, b=b: b.configure(bg=BG_CARD))
            b.bind("<Leave>", lambda e, b=b: b.configure(bg=BG))

        # 하단 컨트롤 행
        bottom = tk.Frame(main, bg=BG)
        bottom.pack(fill="x", padx=10, pady=(2, 0))

        self.btn_toggle = tk.Button(
            bottom, text="▶", bg=BG_CARD, fg=GREEN,
            activebackground=BG, activeforeground=GREEN,
            font=("DejaVu Sans", 14, "bold"), relief="flat", bd=0,
            cursor="hand2", width=2, command=self._toggle,
        )
        self.btn_toggle.pack(side="left", padx=(0, 4))

        self._mk_btn(bottom, "↻", YELLOW, self._restart).pack(side="left", padx=(0, 4))
        self._mk_btn(bottom, "🌐", ACCENT, self._open_web).pack(side="left")

        self.status_lbl = tk.Label(bottom, text="확인 중", bg=BG, fg=FG_DIM,
                                   font=("DejaVu Sans", 9), anchor="e")
        self.status_lbl.pack(side="right", padx=(0, 4))

    def _mk_btn(self, parent, text: str, color: str, command) -> tk.Button:
        btn = tk.Button(
            parent, text=text, bg=BG_CARD, fg=color,
            activebackground=BG, activeforeground=color,
            font=("DejaVu Sans", 11), relief="flat", bd=0,
            cursor="hand2", width=2, command=command,
        )
        return btn

    # ----- 드래그 -----
    def _on_drag(self, e) -> None:
        x = self.root.winfo_x() + e.x - self._drag["x"]
        y = self.root.winfo_y() + e.y - self._drag["y"]
        self.root.geometry(f"+{x}+{y}")

    # ----- 상태 폴링 -----
    def _poll_status(self) -> None:
        pid = get_running_pid()
        if pid is None and self._proc and self._proc.poll() is None:
            pid = self._proc.pid

        if pid:
            self.btn_toggle.config(text="■", fg=RED, activeforeground=RED)
            self.status_lbl.config(text=f"실행 중  PID {pid}", fg=GREEN)
            self.root.title("ReplayKit — 실행 중")
        else:
            self.btn_toggle.config(text="▶", fg=GREEN, activeforeground=GREEN)
            self.status_lbl.config(text="정지", fg=FG_DIM)
            self.root.title("ReplayKit — 정지")

        self.root.after(1000, self._poll_status)

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
            self.status_lbl.config(text="Python 없음", fg=RED)
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
            self._proc = subprocess.Popen(
                [str(PY), "-m", "uvicorn", "backend.app.main:app",
                 "--host", "0.0.0.0", "--port", str(PORT)],
                cwd=str(USER_DATA), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            PID_FILE.write_text(str(self._proc.pid))
            self.status_lbl.config(text="시작 중...", fg=YELLOW)
        except Exception as e:
            self.status_lbl.config(text=f"오류: {e}", fg=RED)

    def _stop(self) -> None:
        pid = get_running_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
            self.status_lbl.config(text="종료 중...", fg=YELLOW)
            self.root.after(5000, lambda p=pid: self._force_kill(p))
        except OSError as e:
            self.status_lbl.config(text=f"오류: {e}", fg=RED)

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
            self.root.after(2500, self._start)
        else:
            self._start()

    def _open_web(self) -> None:
        try:
            webbrowser.open(SERVER_URL)
        except Exception:
            try:
                subprocess.Popen(["xdg-open", SERVER_URL])
            except FileNotFoundError:
                pass

    def _minimize(self) -> None:
        # overrideredirect 모드는 iconify 가 잘 동작 안 함 — withdraw 후
        # 사용자가 다시 띄울 방법이 없으므로 그냥 작게 줄이는 정도로.
        # Linux 트레이는 환경 따라 다름 — 단순화를 위해 최소화 미지원,
        # 대신 화면 모서리로 이동.
        self.root.geometry("+0+0")

    def _quit(self) -> None:
        # 서버는 살려두고 윈도우만 종료 (서버 종료는 ■ 버튼으로 명시적)
        self.root.destroy()


def main() -> int:
    if not APP_DIR.exists():
        sys.stderr.write(f"[ERROR] {APP_DIR} 없음.\n")
        return 1
    root = tk.Tk()
    Launcher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
