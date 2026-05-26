"""서버 관리 GUI — 동기화 + 백엔드 + 프론트엔드 동시 관리.

PyInstaller exe로 컴파일 가능:
  pyinstaller --onefile --noconsole --name ReplayKit server.py
"""

import re
import subprocess
import sys
import os
import threading
import time
import webbrowser
import tkinter as tk
from tkinter import scrolledtext, ttk
from datetime import datetime

import psutil

try:
    import pystray
    from PIL import Image, ImageDraw
    _HAS_TRAY = True
except ImportError:
    _HAS_TRAY = False

# PyInstaller exe: use exe's directory; normal script: use __file__'s directory
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
RESTART_FLAG = os.path.join(PROJECT_ROOT, ".restart")

# 임베디드 Python 격리 — 시스템 Python의 환경변수가 자식 subprocess로 전파되지
# 않도록 server.py 시작 시점에 제거. ReplayKit.bat에서도 정리하지만, server.py를
# 직접 실행하는 개발 시나리오도 보호.
for _var in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
    os.environ.pop(_var, None)
os.environ.setdefault("PYTHONNOUSERSITE", "1")

# Backend Python: embedded > venv > system (self)
# Windows embedded: python/python.exe (python-3.10.4-embed-amd64)
# Linux embedded:   python/bin/python3 (python-build-standalone install_only)
if sys.platform == "win32":
    _embed_python = os.path.join(PROJECT_ROOT, "python", "python.exe")
else:
    _embed_python = os.path.join(PROJECT_ROOT, "python", "bin", "python3")
_venv_python = os.path.join(PROJECT_ROOT, "venv", "Scripts", "python.exe")
if not os.path.exists(_venv_python):
    _venv_python = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
if os.path.exists(_embed_python):
    VENV_PYTHON = _embed_python
elif os.path.exists(_venv_python):
    VENV_PYTHON = _venv_python
elif not getattr(sys, "frozen", False):
    VENV_PYTHON = sys.executable
else:
    VENV_PYTHON = None

NPM_CMD = "npm.cmd" if sys.platform == "win32" else "npm"

# ── 색상 테마 (설정 파일에서 읽기) ──
_SETTINGS_FILE = os.path.join(PROJECT_ROOT, "backend", "settings.json")


def _read_theme() -> str:
    """backend/settings.json에서 theme 읽기. 기본값 dark."""
    try:
        import json
        with open(_SETTINGS_FILE, encoding="utf-8") as f:
            return json.load(f).get("theme", "dark")
    except Exception:
        return "dark"


_THEME = _read_theme()

# 다크 모드
_DARK = {
    "BG": "#1e1e2e", "BG_CARD": "#2a2a3d", "FG": "#cdd6f4", "FG_DIM": "#6c7086",
    "GREEN": "#a6e3a1", "RED": "#f38ba8", "YELLOW": "#f9e2af", "BLUE": "#89b4fa",
    "ACCENT": "#cba6f7", "LOG_BG": "#181825",
}
# 라이트 모드
_LIGHT = {
    "BG": "#f5f5f5", "BG_CARD": "#ffffff", "FG": "#1f1f1f", "FG_DIM": "#888888",
    "GREEN": "#389e0d", "RED": "#cf1322", "YELLOW": "#d48806", "BLUE": "#1677ff",
    "ACCENT": "#722ed1", "LOG_BG": "#fafafa",
}
_C = _DARK if _THEME == "dark" else _LIGHT
BG = _C["BG"]
BG_CARD = _C["BG_CARD"]
FG = _C["FG"]
FG_DIM = _C["FG_DIM"]
GREEN = _C["GREEN"]
RED = _C["RED"]
YELLOW = _C["YELLOW"]
BLUE = _C["BLUE"]
ACCENT = _C["ACCENT"]
LOG_BG = _C["LOG_BG"]

# 로그에서 필터링할 패턴
_LOG_FILTER_RE = re.compile(
    r'"GET /api/device/screenshot/|'
    r'"GET /api/device/info/'
)


def _is_system_python_dir(path: str, embed_dir: str) -> bool:
    """PATH 항목이 임베디드 Python이 아닌 시스템 Python 디렉토리인지 판별.

    임베디드 Python(self.cmd[0]) 디렉토리는 유지하고,
    C:\\Python310, C:\\Python311 등 시스템 Python 경로만 제외한다.
    """
    if not path:
        return False
    try:
        norm = os.path.normcase(os.path.abspath(path))
        embed_norm = os.path.normcase(os.path.abspath(embed_dir))
        if norm == embed_norm or norm.startswith(embed_norm + os.sep):
            return False
        # python.exe 또는 python3X.dll 이 있으면 Python 설치 디렉토리로 간주
        if os.path.isdir(norm):
            for name in os.listdir(norm):
                low = name.lower()
                if low == "python.exe" or (low.startswith("python3") and low.endswith(".dll")):
                    return True
    except OSError:
        pass
    return False


def _run_cmd(cmd, cwd=PROJECT_ROOT, timeout=120):
    """subprocess 실행 후 (returncode, stdout) 반환."""
    try:
        r = subprocess.run(
            cmd, cwd=cwd, capture_output=True, timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        stdout = r.stdout.decode("utf-8", errors="replace").strip() if r.stdout else ""
        stderr = r.stderr.decode("utf-8", errors="replace").strip() if r.stderr else ""
        return r.returncode, (stdout + "\n" + stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "Timeout"
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


class ServerProcess:
    """하나의 서버 프로세스를 관리."""

    def __init__(self, name: str, cmd: list[str], cwd: str, url: str):
        self.name = name
        self.cmd = cmd
        self.cwd = cwd
        self.url = url
        self.port = int(url.rsplit(":", 1)[-1].split("/")[0])
        self.proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        if self.proc is None:
            return False
        return self.proc.poll() is None

    def start(self, log_callback) -> bool:
        if self.running:
            log_callback(f"[{self.name}] 이미 실행 중입니다")
            return False
        if not self.cmd or self.cmd[0] is None:
            log_callback(f"[{self.name}] venv가 없어 시작할 수 없습니다. setup.bat을 먼저 실행하세요")
            return False
        env = os.environ.copy()
        env["RECORDING_PROJECT_ROOT"] = PROJECT_ROOT
        # 임베디드 Python 격리: 시스템 Python의 환경변수가 상속되어
        # C:\PythonXX\Lib 등이 sys.path에 끼어드는 문제 차단.
        # python.exe -E -s 플래그와 함께 사용해야 완전 격리됨.
        for var in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
            env.pop(var, None)
        env["PYTHONNOUSERSITE"] = "1"
        # PATH에서 시스템 Python 디렉토리 제거 — DLL 검색 시 시스템 Python의
        # python3X.dll 등이 먼저 잡히는 것을 방지.
        if sys.platform == "win32" and self.cmd and self.cmd[0]:
            embed_dir = os.path.dirname(os.path.abspath(self.cmd[0]))
            path_parts = env.get("PATH", "").split(os.pathsep)
            filtered = [p for p in path_parts if not _is_system_python_dir(p, embed_dir)]
            env["PATH"] = os.pathsep.join(filtered)
        try:
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=self.cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if sys.platform == "win32" else 0,
                bufsize=0,
            )
            log_callback(f"[{self.name}] 시작됨 (PID {self.proc.pid}) — {self.url}")
            self._reader_thread = threading.Thread(
                target=self._read_output, args=(log_callback,), daemon=True,
            )
            self._reader_thread.start()
            return True
        except Exception as e:
            log_callback(f"[{self.name}] 시작 실패: {e}")
            return False

    def stop(self, log_callback) -> None:
        if not self.running or self.proc is None:
            log_callback(f"[{self.name}] 실행 중이 아닙니다")
            return
        pid = self.proc.pid
        try:
            parent = psutil.Process(pid)
            children = parent.children(recursive=True)
            for child in children:
                child.terminate()
            parent.terminate()
            gone, alive = psutil.wait_procs([parent] + children, timeout=5)
            for p in alive:
                p.kill()
            log_callback(f"[{self.name}] 정지 완료 (PID {pid})")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            log_callback(f"[{self.name}] 이미 정지됨")
        self.proc = None
        self._kill_port_listeners(log_callback)

    def _kill_port_listeners(self, log_callback) -> None:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == self.port and conn.status == "LISTEN" and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    for child in p.children(recursive=True):
                        child.kill()
                    p.kill()
                    log_callback(f"[{self.name}] 포트 {self.port} 고아 프로세스 정리 (PID {conn.pid})")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

    def _read_output(self, log_callback) -> None:
        try:
            assert self.proc and self.proc.stdout
            for raw_line in self.proc.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                if line:
                    log_callback(f"[{self.name}] {line}")
        except Exception:
            pass


class ServerManagerApp:
    def __init__(self):
        self._log_buffer: list[str] = []
        # 날짜별 로그 디렉토리
        self._log_dir = os.path.join(PROJECT_ROOT, "logs")
        os.makedirs(self._log_dir, exist_ok=True)
        self._log_date = datetime.now().strftime("%Y-%m-%d")
        # 7일 지난 로그 파일 삭제
        self._cleanup_old_logs(7)

        self.root = tk.Tk()
        self.root.title("ReplayKit")
        self.root.configure(bg=BG)

        # 배포 모드: frontend/dist 존재하면 백엔드가 정적 파일 서빙 (프론트엔드 서버 불필요)
        fe_dist = os.path.join(FRONTEND_DIR, "dist", "index.html")
        self._production = os.path.exists(fe_dist)

        # --reload 제거: uvicorn reload가 CREATE_NO_WINDOW 없이 자식 프로세스를 생성하여
        # cmd 창이 나타나는 문제 방지. GUI의 재시작 버튼으로 대체.
        self.backend = ServerProcess(
            "백엔드",
            # -E: PYTHON* 환경변수 무시 (PYTHONHOME, PYTHONPATH 등)
            # -s: 사용자 site-packages 비활성화 (%APPDATA%\Python\PythonXX\site-packages)
            # 시스템 Python이 설치된 환경에서도 임베디드 Python만 사용하도록 격리.
            [VENV_PYTHON, "-E", "-s", "-m", "uvicorn", "backend.app.main:app",
             "--host", "0.0.0.0", "--port", "8000",
             # 장시간 재생 중 일시적 event loop 지연이 있어도 WS가 끊기지 않도록
             # ping interval/timeout을 기본값(20s) → 60s로 상향.
             "--ws-ping-interval", "60", "--ws-ping-timeout", "60"],
            PROJECT_ROOT,
            "http://localhost:8000",
        )
        self.frontend = ServerProcess(
            "프론트엔드",
            [NPM_CMD, "run", "dev"],
            FRONTEND_DIR,
            "http://localhost:5173",
        )

        self._build_ui()
        self._update_status()
        self._check_restart_flag()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ── UI 구성 ──

    def _build_ui(self):
        self.root.overrideredirect(True)  # 윈도우 타이틀바 제거
        self.root.geometry("360x70")
        self.root.resizable(False, False)
        # 화면 우하단 (작업표시줄 위)
        self.root.update_idletasks()
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.root.geometry(f"+{sw - 380}+{sh - 120}")

        # 드래그 이동 지원
        self._drag_data = {"x": 0, "y": 0}

        outer = tk.Frame(self.root, bg=FG_DIM, bd=1, relief="solid")
        outer.pack(fill="both", expand=True)

        main = tk.Frame(outer, bg=BG)
        main.pack(fill="both", expand=True, padx=1, pady=1)

        # 상단: 타이틀 + 윈도우 컨트롤
        title_bar = tk.Frame(main, bg=BG)
        title_bar.pack(fill="x")

        title_lbl = tk.Label(
            title_bar, text="RePlayKit", bg=BG, fg=ACCENT,
            font=("Segoe UI", 11, "bold"), cursor="fleur",
        )
        title_lbl.pack(side="left", padx=(10, 0), pady=(4, 0))
        title_lbl.bind("<Button-1>", lambda e: self._drag_data.update(x=e.x, y=e.y))
        title_lbl.bind("<B1-Motion>", lambda e: self.root.geometry(
            f"+{self.root.winfo_x() + e.x - self._drag_data['x']}+{self.root.winfo_y() + e.y - self._drag_data['y']}"))

        # 윈도우 컨트롤 버튼 (트레이 / 종료)
        ctrl = tk.Frame(title_bar, bg=BG)
        ctrl.pack(side="right", padx=(0, 4), pady=(4, 0))

        for text, color, cmd in [
            ("━", FG_DIM, self._to_tray),
            ("✕", RED, self._quit),
        ]:
            b = tk.Label(ctrl, text=text, bg=BG, fg=color,
                         font=("Segoe UI", 10), cursor="hand2", padx=6)
            b.pack(side="left")
            b.bind("<Button-1>", lambda e, c=cmd: c())
            b.bind("<Enter>", lambda e, b=b: b.configure(bg=BG_CARD))
            b.bind("<Leave>", lambda e, b=b: b.configure(bg=BG))

        # 하단: 서버 컨트롤 + 상태
        bottom = tk.Frame(main, bg=BG)
        bottom.pack(fill="x", padx=10, pady=(2, 0))

        # ▶/■ 토글
        self._toggle_btn = tk.Button(
            bottom, text="▶", bg=BG_CARD, fg=GREEN,
            activebackground=BG, activeforeground=GREEN,
            font=("Segoe UI", 14, "bold"), relief="flat", bd=0,
            cursor="hand2", width=2, command=self._toggle_server,
        )
        self._toggle_btn.pack(side="left", padx=(0, 4))

        # ↻ 재시작
        self._make_btn(bottom, "↻", YELLOW, self._restart_all).pack(side="left", padx=(0, 4))

        # 🌐 웹
        self._make_btn(bottom, "🌐", ACCENT, self._open_web).pack(side="left")

        # 상태 표시줄 (버튼 오른쪽)
        self.statusbar = tk.Label(
            bottom, text="준비", bg=BG, fg=FG_DIM,
            font=("Segoe UI", 8), anchor="e",
        )
        self.statusbar.pack(side="right", padx=(0, 4))

        # 더미 (호환용)
        self.be_card = {"indicator": tk.Label(), "status_lbl": tk.Label(), "start_btn": tk.Button(), "stop_btn": tk.Button()}
        self.fe_card = {"indicator": tk.Label(), "status_lbl": tk.Label(), "start_btn": tk.Button(), "stop_btn": tk.Button()}
        self.log_tabs: dict[str, scrolledtext.ScrolledText] = {}

        # ── 시스템 트레이 ──
        self._tray_icon = None
        if _HAS_TRAY:
            self._create_tray()
            threading.Thread(target=self._tray_icon.run, daemon=True).start()

    def _toggle_server(self):
        if self.backend.running:
            self._stop_all()
        else:
            self._start_all()

    def _to_tray(self):
        if _HAS_TRAY and self._tray_icon:
            self.root.withdraw()
        else:
            self.root.iconify()

    def _make_btn(self, parent, text: str, color: str, command) -> tk.Button:
        btn = tk.Button(
            parent, text=text, bg=BG_CARD, fg=color,
            activebackground=BG, activeforeground=color,
            font=("Segoe UI", 16, "bold"), relief="flat", bd=0,
            cursor="hand2", command=command, width=2,
        )
        btn.bind("<Enter>", lambda e, b=btn, c=color: b.configure(bg="#e0e0e0" if _THEME == "light" else "#363650"))
        btn.bind("<Leave>", lambda e, b=btn: b.configure(bg=BG_CARD))
        return btn

    # ── 로그 ──

    def _log(self, msg: str):
        self.root.after(0, self._append_log, msg)

    def _set_status(self, text: str):
        self.root.after(0, lambda: self.statusbar.configure(text=text))

    def _append_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        if "[백엔드]" in msg and _LOG_FILTER_RE.search(msg):
            return
        entry = f"[{ts}] {msg}"
        self._log_buffer.append(entry)
        if len(self._log_buffer) > 2000:
            self._log_buffer = self._log_buffer[-1500:]
        # 날짜별 로그 파일에 기록
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if today != self._log_date:
                self._log_date = today
                self._cleanup_old_logs(7)
            log_file = os.path.join(self._log_dir, f"{today}.log")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass

    def _cleanup_old_logs(self, keep_days: int):
        """keep_days일 이전 로그 파일 삭제."""
        try:
            import glob as _g
            cutoff = time.time() - keep_days * 86400
            for f in _g.glob(os.path.join(self._log_dir, "*.log")):
                if os.path.getmtime(f) < cutoff:
                    os.remove(f)
        except Exception:
            pass

    # ── 동기화 (git pull + 의존성) ──

    def _sync(self, log_callback):
        """git pull + pip install + npm install. 메인 스레드가 아닌 곳에서 호출."""
        try:
            return self._sync_impl(log_callback)
        except Exception as e:
            log_callback(f"[동기화] 예외 발생: {e}")
            return True  # 동기화 실패해도 서버 시작은 진행

    def _sync_impl(self, log_callback):
        self._set_status("동기화 중...")

        # 1) Git pull은 ReplayKit.bat이 담당 (서버 실행 전에 완료됨)
        # 개발 환경: .pyd 캐시 삭제 / 배포 환경: .py 소스 삭제 (.pyd 보호)
        _main_py = os.path.join(PROJECT_ROOT, "backend", "app", "main.py")
        _is_dev = os.path.exists(_main_py)
        if _is_dev:
            import glob as _glob
            _backend_dir = os.path.join(PROJECT_ROOT, "backend")
            for _pyd in _glob.glob(os.path.join(_backend_dir, "**", "*.pyd"), recursive=True):
                try:
                    os.remove(_pyd)
                except Exception:
                    pass
        else:
            import glob as _glob
            _backend_app = os.path.join(PROJECT_ROOT, "backend", "app")
            _skip_py = {"__init__.py", "dependencies.py"}
            removed = 0
            for _py in _glob.glob(os.path.join(_backend_app, "**", "*.py"), recursive=True):
                fname = os.path.basename(_py)
                if fname in _skip_py:
                    continue
                stem = os.path.splitext(fname)[0]
                pyd_exists = any(_glob.glob(os.path.join(os.path.dirname(_py), f"{stem}.*.pyd")))
                if pyd_exists:
                    try:
                        os.remove(_py)
                        removed += 1
                    except Exception:
                        pass
            if removed:
                log_callback(f"[동기화] 배포 보호: .py 소스 {removed}개 삭제 (.pyd 우선)")

        # 2) pip install (requirements.txt 변경 또는 핵심 모듈 누락 시)
        log_callback("[동기화] Python 의존성 확인 중...")
        try:
            import hashlib
            req_file = os.path.join(PROJECT_ROOT, "requirements.txt")
            req_hash_file = os.path.join(PROJECT_ROOT, ".req_hash")
            req_hash = hashlib.md5(open(req_file, "rb").read()).hexdigest() if os.path.exists(req_file) else ""
            old_hash = open(req_hash_file).read().strip() if os.path.exists(req_hash_file) else ""

            # 핵심 모듈 import 체크 — requirements.txt 동기화 누락에 대비한 안전장치.
            # 해시 비교만 의지하면 배포 환경 requirements.txt 가 갱신되지 않은 경우
            # 누락된 패키지를 영영 발견하지 못하므로 import 가능 여부로 직접 확인.
            CRITICAL_MODULES = ["rapidocr_onnxruntime", "rapidfuzz"]
            critical_missing: list[str] = []
            if VENV_PYTHON and os.path.exists(VENV_PYTHON):
                for mod in CRITICAL_MODULES:
                    try:
                        check_code, _ = _run_cmd([VENV_PYTHON, "-c", f"import {mod}"], timeout=15)
                        if check_code != 0:
                            critical_missing.append(mod)
                    except Exception:
                        critical_missing.append(mod)

            if req_hash != old_hash:
                log_callback("[동기화] Python 의존성 설치 중... (requirements.txt 변경 감지)")
                code, out = _run_cmd([VENV_PYTHON, "-E", "-s", "-m", "pip", "install", "-r", "requirements.txt", "-q"], timeout=180)
                if code != 0:
                    log_callback(f"[동기화] pip install 실패: {out[:200]}")
                try:
                    with open(req_hash_file, "w") as f:
                        f.write(req_hash)
                except Exception:
                    pass
            else:
                log_callback("[동기화] Python 의존성 변경 없음 — 건너뜀")

            # 핵심 모듈이 여전히 누락된 경우 직접 설치 (requirements.txt 동기화 무관)
            if critical_missing:
                # 위에서 -r requirements.txt 가 실행됐어도 여기 누락이면 requirements 자체에
                # 빠진 패키지이므로 명시적으로 설치.
                still_missing = []
                for mod in critical_missing:
                    check_code, _ = _run_cmd([VENV_PYTHON, "-c", f"import {mod}"], timeout=15)
                    if check_code != 0:
                        still_missing.append(mod)
                if still_missing:
                    log_callback(f"[동기화] 핵심 모듈 누락 — 직접 설치: {still_missing}")
                    # 패키지 이름은 hyphen 형태로 통일
                    pkg_names = [m.replace("_", "-") for m in still_missing]
                    code, out = _run_cmd(
                        [VENV_PYTHON, "-E", "-s", "-m", "pip", "install", *pkg_names, "-q"],
                        timeout=180,
                    )
                    if code != 0:
                        log_callback(f"[동기화] 핵심 모듈 설치 실패: {out[:200]}")
                    else:
                        log_callback(f"[동기화] 핵심 모듈 설치 완료: {pkg_names}")
        except Exception as e:
            log_callback(f"[동기화] Python 의존성 확인 오류: {e}")

        # 3) npm install (개발 모드 + package.json 변경 시에만)
        if not self._production:
            log_callback("[동기화] Node 의존성 확인 중...")
            try:
                pkg_file = os.path.join(FRONTEND_DIR, "package.json")
                pkg_hash_file = os.path.join(FRONTEND_DIR, ".pkg_hash")
                pkg_hash = hashlib.md5(open(pkg_file, "rb").read()).hexdigest() if os.path.exists(pkg_file) else ""
                old_pkg_hash = open(pkg_hash_file).read().strip() if os.path.exists(pkg_hash_file) else ""
                if pkg_hash != old_pkg_hash:
                    log_callback("[동기화] Node 의존성 설치 중...")
                    code, out = _run_cmd([NPM_CMD, "install", "--silent"], cwd=FRONTEND_DIR, timeout=120)
                    if code != 0:
                        log_callback(f"[동기화] npm install 실패: {out[:200]}")
                    try:
                        with open(pkg_hash_file, "w") as f:
                            f.write(pkg_hash)
                    except Exception:
                        pass
                else:
                    log_callback("[동기화] Node 의존성 변경 없음 — 건너뜀")
            except Exception as e:
                log_callback(f"[동기화] Node 의존성 확인 오류: {e}")

        # 4) 고아 screenshots 폴더 정리 (시나리오 없는 스크린샷 폴더 삭제)
        try:
            import shutil as _shutil
            _sc_dir = os.path.join(PROJECT_ROOT, "backend", "screenshots")
            _scen_dir = os.path.join(PROJECT_ROOT, "backend", "scenarios")
            if os.path.isdir(_sc_dir) and os.path.isdir(_scen_dir):
                _existing = {os.path.splitext(f)[0] for f in os.listdir(_scen_dir) if f.endswith(".json")}
                _cleaned = 0
                for d in os.listdir(_sc_dir):
                    dp = os.path.join(_sc_dir, d)
                    if os.path.isdir(dp) and d not in _existing:
                        _shutil.rmtree(dp, ignore_errors=True)
                        _cleaned += 1
                if _cleaned:
                    log_callback(f"[동기화] 고아 스크린샷 폴더 {_cleaned}개 삭제")
        except Exception:
            pass

        # 5) 고아 results 정리 (result.json 없는 폴더 + 레거시 플랫 파일 삭제)
        try:
            _res_dir = os.path.join(PROJECT_ROOT, "backend", "results")
            if os.path.isdir(_res_dir):
                _cleaned = 0
                for item in os.listdir(_res_dir):
                    fp = os.path.join(_res_dir, item)
                    if os.path.isdir(fp):
                        if not os.path.isfile(os.path.join(fp, "result.json")):
                            _shutil.rmtree(fp, ignore_errors=True)
                            _cleaned += 1
                    elif os.path.isfile(fp) and item.endswith((".json", ".xlsx")):
                        # 레거시 플랫 파일 — 대응하는 런 폴더가 없으면 삭제
                        _cleaned += 1
                        os.remove(fp)
                if _cleaned:
                    log_callback(f"[동기화] 고아 결과 {_cleaned}개 삭제")
        except Exception:
            pass

        log_callback("[동기화] 완료")
        self._set_status("동기화 완료")
        return True

    # ── 재시작 플래그 감시 ──

    def _check_restart_flag(self):
        """백엔드가 .restart 파일을 생성하면 서버를 재시작."""
        if os.path.exists(RESTART_FLAG):
            try:
                os.remove(RESTART_FLAG)
            except Exception:
                pass
            self._log("[시스템] 재시작 요청 감지됨")
            self._full_restart()
        self.root.after(2000, self._check_restart_flag)

    def _full_restart(self):
        """서버 종료 → git pull → 서버 재시작 (런처는 유지)."""
        def _do():
            self._log("[시스템] 서버 종료 중...")
            self.backend.stop(self._log)
            if not self._production:
                self.frontend.stop(self._log)
            time.sleep(1)
            _kill_existing_servers()
            time.sleep(1)

            # git pull (서버 프로세스가 죽은 상태이므로 .pyd 잠금 없음)
            _git_paths = [r"C:\Program Files\Git\cmd", r"C:\Program Files (x86)\Git\cmd"]
            for _gp in _git_paths:
                if os.path.isdir(_gp) and _gp not in os.environ.get("PATH", ""):
                    os.environ["PATH"] = _gp + ";" + os.environ.get("PATH", "")
            git_dir = os.path.join(PROJECT_ROOT, ".git")
            if os.path.isdir(git_dir):
                safe_dir = PROJECT_ROOT.replace("\\", "/")
                git = ["git", "-c", f"safe.directory={safe_dir}"]
                if _run_cmd(["git", "--version"], timeout=5)[0] == 0:
                    self._log("[업데이트] git pull 중...")
                    _run_cmd(git + ["fetch", "origin", "main"], timeout=30)
                    # 런처 .pyd는 현재 프로세스가 잠그고 있으므로 제외
                    import glob as _g
                    for _pyd in _g.glob(os.path.join(PROJECT_ROOT, "server*.pyd")):
                        rel = os.path.relpath(_pyd, PROJECT_ROOT).replace("\\", "/")
                        _run_cmd(git + ["update-index", "--assume-unchanged", rel], timeout=5)
                    code, out = _run_cmd(git + ["reset", "--hard", "origin/main"], timeout=15)
                    # 제외 복원 (다음 전체 재시작 시 업데이트)
                    for _pyd in _g.glob(os.path.join(PROJECT_ROOT, "server*.pyd")):
                        rel = os.path.relpath(_pyd, PROJECT_ROOT).replace("\\", "/")
                        _run_cmd(git + ["update-index", "--no-assume-unchanged", rel], timeout=5)
                    if code == 0:
                        self._log(f"[업데이트] 완료: {out.strip()}")
                    else:
                        self._log(f"[업데이트] git pull 실패: {out.strip()}")

            # 서버 재시작 (웹은 프론트엔드 폴링이 자동 새로고침하므로 열지 않음)
            self._log("[시스템] 서버 재시작 중...")
            self._start_all_sync(auto_open_web=False)
        threading.Thread(target=_do, daemon=True).start()

    # ── 상태 업데이트 (주기적) ──

    def _update_status(self):
        if self.backend.running:
            self._toggle_btn.configure(text="■", fg=RED)
            self.root.title("ReplayKit — 실행 중")
            status = self.statusbar.cget("text")
            if "동기화" not in status and "업데이트" not in status:
                self.statusbar.configure(text="서버 실행 중")
        else:
            self._toggle_btn.configure(text="▶", fg=GREEN)
            self.root.title("ReplayKit — 정지됨")
            status = self.statusbar.cget("text")
            if "동기화" not in status and "업데이트" not in status and "준비" not in status:
                self.statusbar.configure(text="서버 정지됨")

        if self._tray_icon:
            self._tray_icon.title = "ReplayKit — " + ("실행 중" if self.backend.running else "정지됨")

        self.root.after(1000, self._update_status)

    # ── 명령 ──

    def _get_server(self, name: str) -> ServerProcess:
        return self.backend if name == "백엔드" else self.frontend

    def _start_one(self, name: str):
        threading.Thread(target=self._get_server(name).start, args=(self._log,), daemon=True).start()

    def _stop_one(self, name: str):
        threading.Thread(target=self._get_server(name).stop, args=(self._log,), daemon=True).start()

    def _restart_one(self, name: str):
        def _do():
            srv = self._get_server(name)
            srv.stop(self._log)
            time.sleep(1)
            srv.start(self._log)
        threading.Thread(target=_do, daemon=True).start()

    def _start_all_sync(self, auto_open_web=False):
        """서버 시작 (현재 스레드에서 실행, 다른 스레드에서 호출할 때 사용)."""
        self.backend.start(self._log)
        if not self._production:
            self.frontend.start(self._log)
        else:
            self._log("[시스템] 프로덕션 모드 — 프론트엔드는 백엔드가 서빙합니다")
        if auto_open_web:
            self._wait_and_open_web()

    def _start_all(self):
        threading.Thread(target=lambda: self._start_all_sync(auto_open_web=True), daemon=True).start()

    def _open_web(self):
        url = self.backend.url if self._production else self.frontend.url
        webbrowser.open(url)

    def _wait_and_open_web(self):
        """백엔드 HTTP 응답이 올 때까지 대기 후 브라우저 자동 오픈."""
        import urllib.request
        url = self.backend.url if self._production else self.frontend.url
        check_url = self.backend.url + "/api/health"
        for _ in range(15):  # 최대 15초 대기
            time.sleep(1)
            if not self.backend.running:
                return
            try:
                urllib.request.urlopen(check_url, timeout=2)
                self._log(f"[시스템] 백엔드 준비 완료 — 브라우저를 엽니다")
                webbrowser.open(url)
                return
            except Exception:
                pass
        self._log("[시스템] 백엔드 응답 대기 시간 초과 — 수동으로 웹을 열어주세요")

    def _stop_all(self):
        def _do():
            self.backend.stop(self._log)
            if not self._production:
                self.frontend.stop(self._log)
        threading.Thread(target=_do, daemon=True).start()

    def _restart_all(self):
        def _do():
            self.backend.stop(self._log)
            if not self._production:
                self.frontend.stop(self._log)
            time.sleep(1)
            self._start_all_sync(auto_open_web=True)
        threading.Thread(target=_do, daemon=True).start()

    def _sync_and_start(self):
        """동기화 후 서버 시작."""
        def _do():
            self.backend.stop(self._log)
            if not self._production:
                self.frontend.stop(self._log)
            time.sleep(1)
            if self._sync(self._log):
                self._start_all_sync(auto_open_web=True)
        threading.Thread(target=_do, daemon=True).start()

    def _on_close(self):
        """창 닫기 → 트레이로 최소화 (트레이 없으면 종료)."""
        if _HAS_TRAY and self._tray_icon:
            self.root.withdraw()
        else:
            self._quit()

    def _quit(self):
        """완전 종료."""
        if self.backend.running or self.frontend.running:
            self.backend.stop(self._log)
            if not self._production:
                self.frontend.stop(self._log)
        self._kill_adb()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.root.destroy()

    def _show_window(self):
        """트레이에서 창 복원."""
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _create_tray(self):
        """시스템 트레이 아이콘 + 우클릭 메뉴 객체 생성."""
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse([8, 8, 56, 56], fill=(68, 114, 196))
        draw.text((20, 18), "R", fill="white")

        menu = pystray.Menu(
            pystray.MenuItem("열기", lambda: self.root.after(0, self._show_window), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("서버 시작", lambda: self.root.after(0, self._start_all)),
            pystray.MenuItem("서버 재시작", lambda: self.root.after(0, self._restart_all)),
            pystray.MenuItem("서버 정지", lambda: self.root.after(0, self._stop_all)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("웹 열기", lambda: self.root.after(0, self._open_web)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("종료", lambda: self.root.after(0, self._quit)),
        )
        self._tray_icon = pystray.Icon("ReplayKit", img, "ReplayKit", menu)

    @staticmethod
    def _kill_adb():
        """시스템에서 실행 중인 adb.exe 프로세스를 모두 종료."""
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() == "adb.exe":
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

    def run(self):
        self.root.mainloop()


def _hide_console():
    """콘솔 창 숨기기 (Windows)."""
    if sys.platform == "win32":
        try:
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def _kill_existing_servers():
    """기존 백엔드/프론트엔드 포트를 점유한 프로세스 종료."""
    fe_dist = os.path.join(PROJECT_ROOT, "frontend", "dist", "index.html")
    ports = [8000] if os.path.exists(fe_dist) else [8000, 5173]
    for port in ports:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    for child in p.children(recursive=True):
                        child.kill()
                    p.kill()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass


def _check_existing_server() -> bool:
    """기존 서버가 실행 중인지 확인. 실행 중이면 사용자에게 처리 방법을 묻는다.
    Returns: True=계속 진행, False=종료"""
    ports_to_check = [8000]
    occupied = []
    for port in ports_to_check:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr.port == port and conn.status == "LISTEN" and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    occupied.append((port, conn.pid, p.name()))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    occupied.append((port, conn.pid, "?"))
    if not occupied:
        return True

    # Tkinter 대화상자로 선택
    import tkinter.messagebox as mbox
    _root = tk.Tk()
    _root.withdraw()
    msg = "ReplayKit 서버가 이미 실행 중입니다.\n\n"
    for port, pid, name in occupied:
        msg += f"  포트 {port} — PID {pid} ({name})\n"
    msg += "\n기존 서버를 종료하고 새로 시작하시겠습니까?"
    result = mbox.askyesnocancel("ReplayKit", msg,
                                  icon="warning",
                                  default=mbox.NO)
    _root.destroy()
    if result is True:
        # Yes: 기존 서버 종료 후 계속
        _kill_existing_servers()
        time.sleep(1)
        return True
    elif result is False:
        # No: 기존 서버 유지, 새 인스턴스 종료
        return False
    else:
        # Cancel: 종료
        return False


def main():
    # pythonw.exe에서 stdout/stderr가 None인 경우 처리 (print 오류 방지)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, 'w')
    if sys.stderr is None:
        sys.stderr = open(os.devnull, 'w')

    _hide_console()

    is_restart = "--restart" in sys.argv
    do_sync = "--no-sync" not in sys.argv and not is_restart

    if is_restart:
        time.sleep(2)
        _kill_existing_servers()
        time.sleep(1)
    else:
        # 일반 시작: 기존 서버 확인
        if not _check_existing_server():
            return

    app = ServerManagerApp()

    if do_sync:
        # 일반 시작: 동기화 후 자동 시작 + 웹 오픈
        def _auto_sync_and_start():
            if app._sync(app._log):
                app._start_all_sync(auto_open_web=True)
            else:
                app._log("[시스템] 동기화 실패 — 수동으로 시작하세요")
        threading.Thread(target=_auto_sync_and_start, daemon=True).start()
    elif is_restart:
        # 재시작: 동기화 없이 바로 시작 + 웹 오픈
        def _restart_and_open():
            app._start_all_sync(auto_open_web=True)
        threading.Thread(target=_restart_and_open, daemon=True).start()

    app.run()


if __name__ == "__main__":
    main()
