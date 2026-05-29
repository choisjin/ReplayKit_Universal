"""TH 패널 호스트 IPC 클라이언트.

패널 호스트(th_panel_host) 와 Unix socket 으로 통신해서 opcode 한 바이트만 보낸다.
- lazy start: 첫 호출 시 호스트 프로세스 spawn.
- 연결이 끊기면(EPIPE/ConnectionResetError) 1분당 최대 3회까지 자동 재시작.

opcode:
  0x01 highlight, 0x02 reset, 0x03 shutdown
"""

from __future__ import annotations

import errno
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Optional

from .th_panel_host import OP_HIGHLIGHT, OP_RESET, OP_SHUTDOWN


_HOST_MODULE = "backend.app.plugins.linux.common.th_panel_host"
_RESPAWN_WINDOW_S = 60.0
_RESPAWN_MAX = 3
_CONNECT_RETRY_TIMES = 30        # ~3 초 대기 (호스트 startup 여유)
_CONNECT_RETRY_INTERVAL_S = 0.1


class PanelClient:
    """패널 호스트 1개와 1:1 통신."""

    def __init__(
        self,
        socket_path: Optional[str] = None,
        host_argv_prefix: Optional[list[str]] = None,
        width: int = 300,
        height: int = 300,
    ):
        if socket_path is None:
            socket_path = os.path.join(
                tempfile.gettempdir(),
                f"replaykit-th-panel-{os.getpid()}.sock",
            )
        self._sock_path = socket_path
        self._host_argv_prefix = host_argv_prefix or [sys.executable, "-m", _HOST_MODULE]
        self._width = width
        self._height = height

        self._proc: Optional[subprocess.Popen] = None
        self._conn: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._respawn_ts: list[float] = []  # 최근 spawn 시각 (rolling)

    # ── public ──────────────────────────────────────────
    def highlight(self) -> None:
        """패널을 노란색으로 점등하고 현재 시각 라벨을 보여준다."""
        self._send(OP_HIGHLIGHT)

    def reset(self) -> None:
        """패널을 검정으로 되돌린다."""
        self._send(OP_RESET)

    def close(self) -> None:
        """호스트 프로세스 종료. 그 후 lazy 재시작 가능."""
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
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass

    # ── internal ────────────────────────────────────────
    def _send(self, opcode: int) -> None:
        with self._lock:
            self._ensure_connected_locked()
            if self._conn is None:
                return  # 재시작 한도 초과
            try:
                self._conn.sendall(bytes([opcode]))
                return
            except (BrokenPipeError, ConnectionResetError, OSError) as e:
                # 호스트가 죽었거나 끊겼다. 한 번만 재시도.
                self._cleanup_conn_locked()
                if isinstance(e, OSError) and e.errno not in (errno.EPIPE, errno.ECONNRESET):
                    # 그 외 OSError 는 그냥 포기 — 다음 호출 때 다시 시도.
                    return
                self._ensure_connected_locked(force_respawn=True)
                if self._conn is None:
                    return
                try:
                    self._conn.sendall(bytes([opcode]))
                except OSError:
                    self._cleanup_conn_locked()

    def _ensure_connected_locked(self, force_respawn: bool = False) -> None:
        if self._conn is not None and not force_respawn:
            return
        if not self._is_host_alive_locked() or force_respawn:
            if not self._spawn_host_locked():
                return
        # 호스트가 bind 할 때까지 잠깐 대기 + 연결
        for _ in range(_CONNECT_RETRY_TIMES):
            try:
                c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                c.connect(self._sock_path)
                self._conn = c
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError):
                time.sleep(_CONNECT_RETRY_INTERVAL_S)
        # 실패 — 다음 호출에서 다시 시도.

    def _is_host_alive_locked(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn_host_locked(self) -> bool:
        # 1분 window 내 spawn 횟수 제한
        now = time.monotonic()
        self._respawn_ts = [t for t in self._respawn_ts if now - t < _RESPAWN_WINDOW_S]
        if len(self._respawn_ts) >= _RESPAWN_MAX:
            return False
        self._respawn_ts.append(now)

        try:
            os.unlink(self._sock_path)
        except OSError:
            pass

        argv = list(self._host_argv_prefix) + [
            "--socket", self._sock_path,
            "--width", str(self._width),
            "--height", str(self._height),
        ]
        try:
            self._proc = subprocess.Popen(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
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
