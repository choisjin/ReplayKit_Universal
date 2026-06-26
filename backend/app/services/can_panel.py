"""CAN 반응속도 측정용 시각화 패널 (CANAT.CAN_PANEL 전용).

목적:
  CAN 신호를 보내는 '바로 그 순간' 화면 좌하단 패널을 검정→노랑으로 점등시켜,
  카메라로 (노랑 점등 프레임 ~ 디바이스 화면 반응 프레임) 사이를 세어 반응속도를
  측정하기 위한 시각 마커. 점등과 send_can 사이 지연이 측정 오차로 직결되므로
  '지연 없이 즉시' 점등하는 것이 핵심 요구사항이다.

설계 (반응속도 최우선):
  - 패널은 **별도 프로세스**(tkinter)로 미리 띄워둔다. uvicorn(asyncio) 인터프리터에서
    GUI mainloop 를 직접 돌리면 충돌하므로 분리. 원본 Renault TH_Lib.py 도 tkinter 사용.
  - tkinter 는 stdlib → 추가 의존성 0. PySide6 패널(plugins/linux/th_panel_*)은
    PySide6 + AF_UNIX 라 Linux 전용이라 Windows CANAT 에 못 쓴다. 이쪽은 TCP(localhost)라
    Windows/Linux 모두 동작.
  - 컨트롤러(백엔드)는 호스트에 **소켓을 미리 연결**해 둔다. 점등은 그냥 1바이트 send →
    마이크로초 단위. send_can_message 직전에 이 1바이트만 쏘면 사실상 동시 점등.
  - 호스트는 Tk 이벤트 루프에서 1ms 간격으로 소켓을 폴링하고, 점등 시 색을 먼저 칠한 뒤
    update_idletasks() 로 즉시 repaint 한다.

IPC: localhost TCP, 1바이트 opcode
  0x01 highlight (배경 노랑 + 현재 시각 라벨)
  0x02 reset     (배경 검정 + 라벨 숨김)
  0x03 shutdown  (호스트 종료)

호스트 실행: python -m backend.app.services.can_panel --host --port <p> [--width W --height H]
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import threading
import time
from typing import Optional


OP_HIGHLIGHT = 0x01
OP_RESET = 0x02
OP_SHUTDOWN = 0x03

_HOST_MODULE = "backend.app.services.can_panel"

# 호스트 startup(bind+listen) 대기용 connect 재시도
_CONNECT_RETRY_TIMES = 50          # ~5초
_CONNECT_RETRY_INTERVAL_S = 0.1

# 1분 window 내 호스트 재기동 한도 (죽었을 때 무한 spawn 방지)
_RESPAWN_WINDOW_S = 60.0
_RESPAWN_MAX = 3


# ──────────────────────────────────────────────────────────────────────────
# 호스트 (별도 프로세스) — tkinter 패널
# ──────────────────────────────────────────────────────────────────────────
def _run_host(port: int, width: int, height: int) -> int:
    """tkinter 패널 호스트. localhost:port 로 listen, 1바이트 opcode 수신.

    stdlib(tkinter) 만 사용 — `-m` 로 실행될 때 패키지 상대 import 불필요.
    """
    import tkinter as tk  # 호스트에서만 import (백엔드 인터프리터 오염 방지)
    from datetime import datetime

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", port))
    server.listen(1)
    server.setblocking(False)

    BLACK = "#000000"
    YELLOW = "#FFFF00"

    root = tk.Tk()
    root.overrideredirect(True)          # 타이틀바/테두리 제거
    root.attributes("-topmost", True)    # 항상 위
    # 좌하단 배치 (원본 TH_Lib: x=0, y=screen_height - h)
    screen_h = root.winfo_screenheight()
    root.geometry(f"{width}x{height}+0+{max(0, screen_h - height)}")
    root.configure(bg=BLACK)

    label = tk.Label(root, text="", font=("Arial", 9), fg="black", bg=BLACK)
    label.place(relx=0.5, y=4, anchor="n")

    state = {"conn": None}

    def _set_yellow() -> None:
        # 색을 먼저 칠하고 즉시 repaint → 라벨 계산이 점등을 지연시키지 않게.
        root.configure(bg=YELLOW)
        label.configure(bg=YELLOW)
        root.update_idletasks()
        label.configure(text=datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3])

    def _set_black() -> None:
        root.configure(bg=BLACK)
        label.configure(bg=BLACK, text="")
        root.update_idletasks()

    def _poll() -> None:
        conn = state["conn"]
        if conn is None:
            try:
                c, _ = server.accept()
                c.setblocking(False)
                state["conn"] = c
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                pass
        else:
            try:
                data = conn.recv(64)
                if data == b"":
                    try:
                        conn.close()
                    except OSError:
                        pass
                    state["conn"] = None
                else:
                    for b in data:
                        if b == OP_HIGHLIGHT:
                            _set_yellow()
                        elif b == OP_RESET:
                            _set_black()
                        elif b == OP_SHUTDOWN:
                            root.destroy()
                            return
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                try:
                    conn.close()
                except OSError:
                    pass
                state["conn"] = None
        root.after(1, _poll)

    root.after(1, _poll)
    try:
        root.mainloop()
    finally:
        c = state.get("conn")
        if c is not None:
            try:
                c.close()
            except OSError:
                pass
        try:
            server.close()
        except OSError:
            pass
    return 0


# ──────────────────────────────────────────────────────────────────────────
# 컨트롤러 (백엔드) — 호스트 spawn + 사전 연결된 소켓으로 opcode 송신
# ──────────────────────────────────────────────────────────────────────────
class CanPanelController:
    """패널 호스트 1개와 1:1 통신. 점등=1바이트 send (사전 연결 유지)."""

    def __init__(self, width: int = 300, height: int = 300):
        self._width = width
        self._height = height
        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[socket.socket] = None
        self._port: Optional[int] = None
        self._lock = threading.Lock()
        self._respawn_ts: list[float] = []

    # ── public ──────────────────────────────────────────
    def show_black(self) -> None:
        """검은 패널을 띄운다(이미 떠 있으면 검정으로 리셋)."""
        with self._lock:
            self._ensure_connected_locked()
            self._send_locked(OP_RESET)

    def highlight(self) -> None:
        """패널을 노랑으로 점등. send_can 직전에 호출 — 사전 연결돼 있어 즉시."""
        with self._lock:
            self._ensure_connected_locked()
            self._send_locked(OP_HIGHLIGHT)

    def close(self) -> None:
        """패널 호스트 종료."""
        with self._lock:
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

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── internal ────────────────────────────────────────
    def _send_locked(self, opcode: int) -> None:
        if self._conn is None:
            return
        try:
            self._conn.sendall(bytes([opcode]))
        except OSError:
            # 호스트가 죽었거나 끊김 — 한 번 재연결 후 재시도.
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
        # 호스트가 bind 할 때까지 잠깐 대기하며 연결
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
        # 1분 window 내 spawn 횟수 제한
        now = time.monotonic()
        self._respawn_ts = [t for t in self._respawn_ts if now - t < _RESPAWN_WINDOW_S]
        if len(self._respawn_ts) >= _RESPAWN_MAX:
            return False
        self._respawn_ts.append(now)

        # 빈 포트 확보 (OS 할당) → 호스트에 전달. 미세한 TOCTOU 는 localhost 도구라 허용.
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(("127.0.0.1", 0))
            self._port = s.getsockname()[1]
            s.close()
        except OSError:
            return False

        argv = [
            sys.executable, "-m", _HOST_MODULE,
            "--host",
            "--port", str(self._port),
            "--width", str(self._width),
            "--height", str(self._height),
        ]
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


def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CAN reaction-time visualization panel host")
    parser.add_argument("--host", action="store_true", help="호스트(GUI) 모드로 실행")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--width", type=int, default=300)
    parser.add_argument("--height", type=int, default=300)
    args = parser.parse_args(argv)
    if args.host:
        return _run_host(args.port, args.width, args.height)
    parser.error("--host 가 필요합니다")
    return 2


if __name__ == "__main__":
    sys.exit(_main())
