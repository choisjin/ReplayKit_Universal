#!/usr/bin/env python3
"""ReplayKit GUI Launcher — Linux installed mode.

Windows server.py 의 Tkinter 윈도우 등가물. embedded Python 의 bundled tkinter
를 사용하므로 추가 의존성 없음.

핵심 설계:
- 스레드 없이 Tk 의 after() 폴링만 사용 → Tkinter + Xlib XCB 충돌 회피.
  (Windows server.py 의 threading-based 로그 캡처가 Linux 에서 xcb_io.c 의
   `!xcb_xlib_unknown_seq_number` assertion 으로 죽는 문제 회피.)
- subprocess.Popen 으로 uvicorn 관리, Popen.poll() 로 상태 확인.
- ~/.local/share/ReplayKit/replaykit.pid 로 외부 launcher.sh 와 PID 공유.

호출 흐름:
    데스크탑 아이콘 클릭
        → /usr/bin/ReplayKit (launcher.sh)
            → DISPLAY 있으면 이 파일 실행
            → DISPLAY 없으면 uvicorn 직접 실행 (헤드리스)
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox


# ============================================================
# 경로/설정
# ============================================================
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
LOG_FILE = USER_DATA / "logs" / "backend.log"
PID_FILE = USER_DATA / "replaykit.pid"

# 로그 뷰어 설정
LOG_MAX_LINES = 300       # 메모리에 유지할 최대 라인 수
LOG_POLL_MS = 500         # 로그 갱신 주기
STATUS_POLL_MS = 1000     # 상태 갱신 주기


# ============================================================
# 유틸리티
# ============================================================
def is_pid_alive(pid: int) -> bool:
    """PID 가 살아있고 우리 ReplayKit 프로세스인지 확인."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="ignore")
        return "uvicorn" in cmdline and "backend.app.main" in cmdline
    except OSError:
        return False


def get_running_pid() -> int | None:
    """PID 파일에서 실행 중인 ReplayKit PID 확인. 없거나 죽었으면 None."""
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            if is_pid_alive(pid):
                return pid
        except (ValueError, OSError):
            pass
        # stale PID 파일 정리
        try:
            PID_FILE.unlink()
        except OSError:
            pass
    return None


# ============================================================
# GUI
# ============================================================
class ReplayKitLauncher:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("ReplayKit Launcher")
        self.root.geometry("780x560")
        self.root.minsize(600, 400)

        # 내부 상태
        self.server_proc: subprocess.Popen | None = None  # 우리가 띄운 경우
        self.log_position = 0  # backend.log 의 어디까지 읽었는지 (byte offset)

        self._build_ui()
        # 윈도우 닫기 동작
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # 초기 1회 + 주기 폴링 시작
        self.root.after(0, self._refresh_status)
        self.root.after(100, self._refresh_log)

    # ----- UI 구성 -----
    def _build_ui(self) -> None:
        style = ttk.Style()
        # 시스템 기본 테마 유지 (clam/alt 강제 안 함 — DE 통합)

        # ── 상태 영역 ───────────────────────────────────────
        top = ttk.Frame(self.root, padding=(12, 10))
        top.pack(fill=tk.X)

        ttk.Label(top, text="ReplayKit", font=("", 14, "bold")).pack(side=tk.LEFT)
        self.status_label = ttk.Label(top, text="확인 중...", font=("", 10))
        self.status_label.pack(side=tk.LEFT, padx=(15, 0))

        self.url_label = ttk.Label(top, text="", font=("", 9), foreground="#555")
        self.url_label.pack(side=tk.RIGHT)

        # ── 버튼 행 ────────────────────────────────────────
        btn_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        btn_frame.pack(fill=tk.X)

        self.btn_start = ttk.Button(btn_frame, text="▶ 시작", command=self.start_server, width=10)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 4))

        self.btn_stop = ttk.Button(btn_frame, text="■ 종료", command=self.stop_server, width=10)
        self.btn_stop.pack(side=tk.LEFT, padx=4)

        self.btn_restart = ttk.Button(btn_frame, text="↻ 재시작", command=self.restart_server, width=10)
        self.btn_restart.pack(side=tk.LEFT, padx=4)

        ttk.Separator(btn_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=8)

        ttk.Button(btn_frame, text="🌐 브라우저 열기", command=self.open_browser, width=15).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📁 로그 폴더", command=self.open_log_folder, width=12).pack(side=tk.LEFT, padx=4)
        ttk.Button(btn_frame, text="📂 데이터 폴더", command=self.open_data_folder, width=12).pack(side=tk.LEFT, padx=4)

        ttk.Button(btn_frame, text="✕ 창 닫기", command=self._on_close, width=10).pack(side=tk.RIGHT)

        # ── 옵션 행 ────────────────────────────────────────
        opt_frame = ttk.Frame(self.root, padding=(12, 0, 12, 8))
        opt_frame.pack(fill=tk.X)

        self.var_stop_on_close = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame,
            text="창 닫을 때 서버도 종료 (체크 해제하면 백그라운드 유지)",
            variable=self.var_stop_on_close,
        ).pack(side=tk.LEFT)

        # ── 로그 영역 ──────────────────────────────────────
        log_frame = ttk.LabelFrame(self.root, text=f"backend.log  ({LOG_FILE})", padding=6)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self.log_text = scrolledtext.ScrolledText(
            log_frame,
            wrap=tk.NONE,
            font=("monospace", 9),
            background="#1e1e1e",
            foreground="#d4d4d4",
            insertbackground="#d4d4d4",
        )
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log_text.configure(state="disabled")

        # 컬러 태그
        self.log_text.tag_configure("error", foreground="#ff6b6b")
        self.log_text.tag_configure("warn", foreground="#ffd166")
        self.log_text.tag_configure("info", foreground="#a0c4ff")

    # ----- 상태 폴링 -----
    def _refresh_status(self) -> None:
        pid = get_running_pid()
        # 우리가 띄운 subprocess 도 함께 확인
        if pid is None and self.server_proc and self.server_proc.poll() is None:
            pid = self.server_proc.pid

        if pid:
            self.status_label.config(text=f"● 실행 중 (PID {pid})", foreground="#2d8a2d")
            self.url_label.config(text=SERVER_URL)
            self.btn_start.config(state=tk.DISABLED)
            self.btn_stop.config(state=tk.NORMAL)
            self.btn_restart.config(state=tk.NORMAL)
        else:
            self.status_label.config(text="○ 종료 상태", foreground="#888")
            self.url_label.config(text="")
            self.btn_start.config(state=tk.NORMAL)
            self.btn_stop.config(state=tk.DISABLED)
            self.btn_restart.config(state=tk.DISABLED)

        self.root.after(STATUS_POLL_MS, self._refresh_status)

    # ----- 로그 폴링 -----
    def _refresh_log(self) -> None:
        if LOG_FILE.exists():
            try:
                size = LOG_FILE.stat().st_size
                # 로그가 회전되어 작아진 경우 처음부터
                if size < self.log_position:
                    self.log_position = 0
                    self._clear_log_display()
                if size > self.log_position:
                    with open(LOG_FILE, "rb") as f:
                        f.seek(self.log_position)
                        new_data = f.read(size - self.log_position)
                    self.log_position = size
                    text = new_data.decode("utf-8", errors="replace")
                    self._append_log(text)
            except OSError:
                pass
        self.root.after(LOG_POLL_MS, self._refresh_log)

    def _append_log(self, text: str) -> None:
        if not text:
            return
        self.log_text.configure(state=tk.NORMAL)
        # 단순 추가 — 정확한 라인별 색상 매칭은 비용/효용 trade-off 라 생략
        # 단, [ERROR]/[WARNING] 포함 라인은 컬러로
        for line in text.splitlines(keepends=True):
            tag = ""
            ucase = line.upper()
            if "[ERROR]" in ucase or "ERROR:" in ucase or "TRACEBACK" in ucase:
                tag = "error"
            elif "[WARNING]" in ucase or "WARNING:" in ucase or "[WARN]" in ucase:
                tag = "warn"
            elif "[INFO]" in ucase or "INFO:" in ucase:
                tag = "info"
            if tag:
                self.log_text.insert(tk.END, line, tag)
            else:
                self.log_text.insert(tk.END, line)
        # 라인 수 제한
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > LOG_MAX_LINES:
            self.log_text.delete("1.0", f"{line_count - LOG_MAX_LINES}.0")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log_display(self) -> None:
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ----- 서버 제어 -----
    def start_server(self) -> None:
        if get_running_pid() is not None:
            return  # 이미 실행 중
        if not PY.exists():
            messagebox.showerror("오류", f"임베디드 Python 없음:\n{PY}")
            return

        env = os.environ.copy()
        # 시스템 Python 환경 격리
        for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(k, None)
        env["PYTHONNOUSERSITE"] = "1"
        env["REPLAYKIT_USER_DATA"] = str(USER_DATA)
        env["RECORDING_PROJECT_ROOT"] = str(USER_DATA)
        env["REPLAYKIT_INSTALLED"] = "1"

        USER_DATA.mkdir(parents=True, exist_ok=True)
        (USER_DATA / "logs").mkdir(parents=True, exist_ok=True)

        try:
            self.server_proc = subprocess.Popen(
                [
                    str(PY), "-m", "uvicorn", "backend.app.main:app",
                    "--host", "0.0.0.0", "--port", str(PORT),
                ],
                cwd=str(USER_DATA),
                env=env,
                stdout=subprocess.DEVNULL,   # backend 가 자체 로그 파일에 기록
                stderr=subprocess.DEVNULL,
                start_new_session=True,       # 부모 (GUI) 와 신호 분리
            )
            PID_FILE.write_text(str(self.server_proc.pid))
        except Exception as e:
            messagebox.showerror("시작 실패", f"서버 시작 실패:\n{e}")

    def stop_server(self) -> None:
        pid = get_running_pid()
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError as e:
            messagebox.showwarning("종료 경고", f"SIGTERM 전송 실패: {e}")
            return
        # 5초 후에도 살아있으면 SIGKILL — 비동기로 처리
        self.root.after(5000, lambda p=pid: self._force_kill_if_alive(p))

    def _force_kill_if_alive(self, pid: int) -> None:
        if is_pid_alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        # PID 파일 정리
        try:
            PID_FILE.unlink(missing_ok=True)
        except (OSError, TypeError):
            try:
                PID_FILE.unlink()
            except OSError:
                pass

    def restart_server(self) -> None:
        self.stop_server()
        # 종료 대기 후 재시작 — 너무 빠르면 포트 TIME_WAIT 충돌
        self.root.after(2500, self.start_server)

    def open_browser(self) -> None:
        try:
            webbrowser.open(SERVER_URL)
        except Exception:
            # webbrowser 실패하면 xdg-open 폴백
            subprocess.Popen(["xdg-open", SERVER_URL])

    def open_log_folder(self) -> None:
        log_dir = USER_DATA / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(log_dir)])
        except FileNotFoundError:
            messagebox.showinfo("로그 폴더", str(log_dir))

    def open_data_folder(self) -> None:
        USER_DATA.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(USER_DATA)])
        except FileNotFoundError:
            messagebox.showinfo("데이터 폴더", str(USER_DATA))

    # ----- 윈도우 닫기 -----
    def _on_close(self) -> None:
        if self.var_stop_on_close.get() and get_running_pid() is not None:
            self.stop_server()
            # 종료 신호만 보내고 즉시 창 닫음 — 백엔드는 자체 cleanup
        self.root.destroy()


def main() -> int:
    if not APP_DIR.exists():
        # CLI 환경에서 호출되었을 때
        sys.stderr.write(f"[ERROR] {APP_DIR} 가 없습니다. ReplayKit 패키지가 설치되었는지 확인하세요.\n")
        return 1
    root = tk.Tk()
    ReplayKitLauncher(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
