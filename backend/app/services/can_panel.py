"""CAN 반응속도 측정용 시각화 패널 (CANAT.CAN_PANEL 전용).

목적:
  CAN 신호를 보내는 '바로 그 순간' 화면 패널을 검정→노랑으로 점등시켜, 카메라로
  (노랑 점등 프레임 ~ 디바이스 화면 반응 프레임) 사이를 세어 반응속도를 측정하기 위한
  시각 마커. 점등과 send_can 사이 지연이 측정 오차로 직결되므로 '지연 없이 즉시' 점등이 핵심.

설계 (반응속도 최우선):
  - 패널은 **별도 프로세스**(PySide6)로 미리 띄워둔다. uvicorn(asyncio) 인터프리터에서
    GUI 이벤트 루프를 직접 돌리면 충돌하므로 분리. (tkinter 는 임베드 Python 에 _tkinter 가
    빠져 안 뜨는 사례가 있어 PySide6 로 전환 — TH 패널과 동일 토킷, 번들에 포함됨.)
  - IPC 는 localhost TCP 라 Windows/Linux 모두 동작 (TH 의 AF_UNIX 는 Linux 전용).
  - 컨트롤러(백엔드)는 호스트에 **소켓을 미리 연결**해 둔다. 점등은 그냥 1바이트 send →
    마이크로초. send_can_message 직전에 이 1바이트만 쏘면 사실상 동시 점등.
  - 호스트는 QSocketNotifier 로 소켓을 이벤트 구동 수신(폴링 지연 0) + repaint() 강제.

위치/크기:
  - x,y,width,height 를 spawn 인자로 받는다. 패널 좌표계 = Qt 전역 픽셀(고DPI 스케일 1:1 고정,
    아래 _force_no_hidpi 참고). --grab 캡처도 같은 좌표계라 크롭 결과를 그대로 setGeometry.
  - x<0 또는 y<0 이면 좌하단 기본 배치.

캡처(--grab):
  - 프론트 크롭 UI 용. 주 모니터를 PNG 로 캡처해 base64 를 stdout 으로 출력. PySide6 의
    QScreen.grabWindow(0) 사용 → 패널 배치 좌표계와 동일.

IPC: localhost TCP, 1바이트 opcode
  0x01 highlight (배경 노랑 + 현재 시각 라벨)
  0x02 reset     (배경 검정 + 라벨 숨김)
  0x03 shutdown  (호스트 종료)

호스트 실행: python -m backend.app.services.can_panel --host --port <p> [--x --y --width --height]
캡처 실행:   python -m backend.app.services.can_panel --grab
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional


OP_HIGHLIGHT = 0x01
OP_RESET = 0x02
OP_SHUTDOWN = 0x03

# 이 파일의 절대 경로. 배포 시에는 컴파일된 확장 모듈(.pyd)일 수 있다
# (예: can_panel.cp310-win_amd64.pyd).
_SELF_PATH = str(Path(__file__).resolve())


def _child_cmd(extra_args: list[str]) -> list[str]:
    """패널 호스트/캡처 서브프로세스 실행 커맨드.

    세 가지 함정을 모두 피한다:
      - `python -m backend.app.services.can_panel`: 배포(임베드 Python + python._pth)에서는
        PYTHONPATH/cwd 가 무시돼 `backend` 패키지를 못 찾음 → ModuleNotFoundError.
      - `python <파일>`: 배포본은 이 파일이 .pyd(바이너리)라 스크립트로 못 돌림
        → "Non-UTF-8 code starting with '\\x90'" SyntaxError.
    해결: importlib 로 이 파일(.py 든 .pyd 든)을 경로로 직접 로드해 _main() 실행.
    backend 패키지 import 도, sys.path 조작도 필요 없다. 확장 모듈의 init 함수명과
    맞도록 모듈명은 'can_panel' 로 고정(.pyd = PyInit_can_panel)."""
    boot = (
        "import importlib.util,sys\n"
        "s=importlib.util.spec_from_file_location('can_panel'," + repr(_SELF_PATH) + ")\n"
        "m=importlib.util.module_from_spec(s)\n"
        "s.loader.exec_module(m)\n"
        "raise SystemExit(m._main())\n"
    )
    return [sys.executable, "-c", boot, *extra_args]

# 호스트 startup(bind+listen) 대기용 connect 재시도
_CONNECT_RETRY_TIMES = 50          # ~5초
_CONNECT_RETRY_INTERVAL_S = 0.1

# 1분 window 내 호스트 재기동 한도 (죽었을 때 무한 spawn 방지)
_RESPAWN_WINDOW_S = 60.0
_RESPAWN_MAX = 3

DEFAULT_W = 300
DEFAULT_H = 300


def _force_no_hidpi() -> None:
    """고DPI 스케일을 끄고 물리=논리 1:1 좌표로 고정.

    grabWindow 픽셀 좌표와 setGeometry 논리 좌표가 1:1 로 일치해야 크롭→배치가 정확하다.
    QApplication 생성 전에 호출해야 함.
    """
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
    os.environ.setdefault("QT_SCALE_FACTOR", "1")


# ──────────────────────────────────────────────────────────────────────────
# 호스트 (별도 프로세스) — PySide6 패널
# ──────────────────────────────────────────────────────────────────────────
def _run_host(port: int, x: int, y: int, width: int, height: int) -> int:
    """PySide6 패널 호스트. localhost:port 로 listen, 1바이트 opcode 수신."""
    _force_no_hidpi()
    from datetime import datetime
    from PySide6.QtCore import Qt, QSocketNotifier, QTimer  # type: ignore
    from PySide6.QtGui import QFont, QGuiApplication  # type: ignore
    from PySide6.QtWidgets import QApplication, QLabel, QWidget  # type: ignore

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.setblocking(False)

    app = QApplication(sys.argv[:1])

    class PanelWindow(QWidget):
        def __init__(self) -> None:
            super().__init__()
            # 항상 최상위 — Qt.Tool 은 다른 앱이 활성화되면 자동으로 숨는 부작용이 있어 제외.
            # WindowDoesNotAcceptFocus + WA_ShowWithoutActivating 으로 포커스는 안 뺏는다.
            self.setWindowFlags(
                Qt.FramelessWindowHint
                | Qt.WindowStaysOnTopHint
                | Qt.WindowDoesNotAcceptFocus
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            w = width if width > 0 else DEFAULT_W
            h = height if height > 0 else DEFAULT_H
            if x >= 0 and y >= 0:
                px, py = x, y
            else:
                # 좌하단 기본 배치
                screens = QGuiApplication.screens()
                geom = screens[0].geometry() if screens else None
                if geom is not None:
                    px, py = geom.x(), geom.y() + geom.height() - h
                else:
                    px, py = 0, 0
            self.setGeometry(px, py, w, h)
            self._label = QLabel("", self)
            self._label.setFont(QFont("Arial", 9))
            self._label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            self._label.setGeometry(0, 4, w, 20)
            self._reset()
            self.show()
            self._force_topmost()
            # 다른 TOPMOST 창이 위로 올라와 가리지 않도록 주기적으로 최상위 재적용.
            # (소켓 점등은 별도 이벤트라 이 타이머가 점등 지연에 영향 없음)
            self._top_timer = QTimer(self)
            self._top_timer.timeout.connect(self._force_topmost)
            self._top_timer.start(700)

        def _force_topmost(self) -> None:
            self.raise_()
            if sys.platform == "win32":
                # Win32 SetWindowPos(HWND_TOPMOST) — 포커스/위치/크기는 건드리지 않음.
                try:
                    import ctypes
                    HWND_TOPMOST = -1
                    SWP_NOMOVE = 0x0002
                    SWP_NOSIZE = 0x0001
                    SWP_NOACTIVATE = 0x0010
                    ctypes.windll.user32.SetWindowPos(
                        int(self.winId()), HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                    )
                except Exception:
                    pass

        def highlight(self) -> None:
            self.setStyleSheet("background-color: #FFFF00;")
            self._label.setStyleSheet("color: black; background: transparent;")
            self._force_topmost()  # 점등 시 확실히 맨 위로
            self.repaint()  # 즉시 화면 갱신 (점등 지연 최소화)
            self._label.setText(datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])

        def _reset(self) -> None:
            self.setStyleSheet("background-color: #000000;")
            self._label.setStyleSheet("color: black; background: transparent;")
            self._label.setText("")
            self.repaint()

        def reset(self) -> None:
            self._reset()

    panel = PanelWindow()
    state = {"conn": None, "notifier": None}

    def _close_conn() -> None:
        n = state.get("notifier")
        c = state.get("conn")
        if n is not None:
            n.setEnabled(False)
            n.deleteLater()
            state["notifier"] = None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass
            state["conn"] = None

    def _on_conn_readable(_fd: int) -> None:
        c = state.get("conn")
        if c is None:
            return
        try:
            data = c.recv(64)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            _close_conn()
            return
        if not data:
            _close_conn()
            return
        for b in data:
            if b == OP_HIGHLIGHT:
                panel.highlight()
            elif b == OP_RESET:
                panel.reset()
            elif b == OP_SHUTDOWN:
                _close_conn()
                app.quit()
                return

    def _on_server_readable(_fd: int) -> None:
        try:
            conn, _ = server.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            return
        _close_conn()  # 한 번에 한 client
        conn.setblocking(False)
        state["conn"] = conn
        n = QSocketNotifier(conn.fileno(), QSocketNotifier.Read)
        n.activated.connect(_on_conn_readable)
        state["notifier"] = n

    server_notifier = QSocketNotifier(server.fileno(), QSocketNotifier.Read)
    server_notifier.activated.connect(_on_server_readable)

    try:
        rc = app.exec()
    finally:
        _close_conn()
        try:
            server.close()
        except OSError:
            pass
    return rc


def _run_grab() -> int:
    """주 모니터를 PNG 로 캡처해 base64 를 stdout 으로 출력 (프론트 크롭 UI 용)."""
    _force_no_hidpi()
    from PySide6.QtWidgets import QApplication  # type: ignore
    from PySide6.QtGui import QGuiApplication  # type: ignore
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice  # type: ignore
    import base64

    app = QApplication(sys.argv[:1])  # noqa: F841 (Qt 초기화 필요)
    screen = QGuiApplication.primaryScreen()
    if screen is None:
        sys.stderr.write("no primary screen\n")
        return 1
    pixmap = screen.grabWindow(0)
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.WriteOnly)
    pixmap.save(buf, "PNG")
    geom = screen.geometry()
    # 첫 줄: 스크린 origin/size (전역 좌표 매핑용), 둘째 줄부터: base64 PNG
    sys.stdout.write(f"{geom.x()} {geom.y()} {geom.width()} {geom.height()}\n")
    sys.stdout.write(base64.b64encode(bytes(ba)).decode("ascii"))
    sys.stdout.flush()
    return 0


# ──────────────────────────────────────────────────────────────────────────
# 컨트롤러 (백엔드) — 호스트 spawn + 사전 연결된 소켓으로 opcode 송신
# ──────────────────────────────────────────────────────────────────────────
class CanPanelController:
    """패널 호스트 1개와 1:1 통신. 점등=1바이트 send (사전 연결 유지).

    위치/크기는 spawn 인자라, 변경 시 호스트를 재기동한다 (점등 fast-path 는 그대로 1바이트).
    """

    def __init__(self) -> None:
        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._geom: tuple[int, int, int, int] = (-1, -1, DEFAULT_W, DEFAULT_H)
        self._lock = threading.Lock()
        self._respawn_ts: list[float] = []

    # ── public ──────────────────────────────────────────
    def show_black(self, x: int = -1, y: int = -1,
                   width: int = DEFAULT_W, height: int = DEFAULT_H) -> None:
        """검은 패널을 지정 위치/크기로 띄운다. 지오메트리가 바뀌면 재기동."""
        with self._lock:
            new_geom = (x, y, width, height)
            if self.is_running() and new_geom != self._geom:
                self._close_locked()
            self._geom = new_geom
            self._ensure_connected_locked()
            self._send_locked(OP_RESET)

    def highlight(self) -> None:
        """패널을 노랑으로 점등. send_can 직전 호출 — 사전 연결돼 있어 즉시."""
        with self._lock:
            self._ensure_connected_locked()
            self._send_locked(OP_HIGHLIGHT)

    def close(self) -> None:
        """패널 호스트 종료."""
        with self._lock:
            self._close_locked()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── internal ────────────────────────────────────────
    def _close_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.sendall(bytes([OP_SHUTDOWN]))
            except OSError:
                pass
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._proc is not None:
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
            self._proc = None
        self._port = None

    def _send_locked(self, opcode: int) -> None:
        if self._conn is None:
            return
        try:
            self._conn.sendall(bytes([opcode]))
        except OSError:
            self._cleanup_conn_locked()
            self._ensure_connected_locked(force_respawn=True)
            if self._conn is not None:
                try:
                    self._conn.sendall(bytes([opcode]))
                except OSError:
                    self._cleanup_conn_locked()

    def _ensure_connected_locked(self, force_respawn: bool = False) -> None:
        if self._conn is not None and not force_respawn:
            return
        if not self.is_running() or force_respawn:
            if not self._spawn_host_locked():
                return
        for _ in range(_CONNECT_RETRY_TIMES):
            if not self.is_running():
                break
            try:
                c = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                c.connect(("127.0.0.1", self._port))
                c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # 점등 지연 최소화
                self._conn = c
                return
            except OSError:
                time.sleep(_CONNECT_RETRY_INTERVAL_S)

    def _spawn_host_locked(self) -> bool:
        now = time.monotonic()
        self._respawn_ts = [t for t in self._respawn_ts if now - t < _RESPAWN_WINDOW_S]
        if len(self._respawn_ts) >= _RESPAWN_MAX:
            return False
        self._respawn_ts.append(now)

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            self._port = s.getsockname()[1]
            s.close()
        except OSError:
            return False

        x, y, w, h = self._geom
        argv = _child_cmd([
            "--host",
            "--port", str(self._port),
            "--x", str(x), "--y", str(y),
            "--width", str(w), "--height", str(h),
        ])
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except OSError:
            self._proc = None
            return False

    def _cleanup_conn_locked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None


# ── 모듈 싱글톤 ───────────────────────────────────────────
_panel_singleton: Optional[CanPanelController] = None
_panel_singleton_lock = threading.Lock()


def get_can_panel() -> CanPanelController:
    """CAN_PANEL 전용 싱글톤 컨트롤러."""
    global _panel_singleton
    with _panel_singleton_lock:
        if _panel_singleton is None:
            _panel_singleton = CanPanelController()
        return _panel_singleton


def grab_monitor() -> dict:
    """주 모니터를 캡처해 {image(base64 PNG), x, y, width, height} 반환 (크롭 UI 용).

    별도 프로세스로 PySide6 캡처 — 백엔드 인터پری터에 Qt app 을 띄우지 않는다.
    """
    # importlib 로 이 파일(.py/.pyd)을 직접 로드해 실행 (패키지 import·sys.path 불필요).
    argv = _child_cmd(["--grab"])
    proc = subprocess.run(argv, capture_output=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            "monitor grab failed: " + (proc.stderr.decode("utf-8", "replace").strip() or "(no stderr)")
        )
    out = proc.stdout.decode("ascii", "replace")
    first_nl = out.find("\n")
    if first_nl < 0:
        raise RuntimeError("monitor grab: malformed output")
    header = out[:first_nl].split()
    b64 = out[first_nl + 1:].strip()
    gx, gy, gw, gh = (int(header[0]), int(header[1]), int(header[2]), int(header[3]))
    return {"image": b64, "format": "png", "x": gx, "y": gy, "width": gw, "height": gh}


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CAN reaction-time visualization panel")
    parser.add_argument("--host", action="store_true", help="호스트(GUI) 모드")
    parser.add_argument("--grab", action="store_true", help="주 모니터 캡처 모드 (base64 PNG stdout)")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--x", type=int, default=-1)
    parser.add_argument("--y", type=int, default=-1)
    parser.add_argument("--width", type=int, default=DEFAULT_W)
    parser.add_argument("--height", type=int, default=DEFAULT_H)
    args = parser.parse_args(argv)
    if args.grab:
        return _run_grab()
    if args.host:
        if not args.port:
            parser.error("--host 에는 --port 가 필요합니다")
        return _run_host(args.port, args.x, args.y, args.width, args.height)
    parser.error("--host 또는 --grab 가 필요합니다")
    return 2


if __name__ == "__main__":
    sys.exit(_main())
