"""TH 시각화 패널 호스트 프로세스 (PySide6).

ReplayKit 백엔드와 같은 인터프리터에서 Qt 를 띄우면 tkinter 류와 충돌하는
사례가 보고되어, 패널은 별도 프로세스로 분리한다.

통신:
  - Unix domain socket (--socket <path>) 으로 listen
  - 한 번에 한 client 만 받음
  - 메시지 = 1바이트 opcode
      0x01: highlight (배경 노란색 + 현재 wall-clock 라벨)
      0x02: reset    (배경 검정 + 라벨 숨김)
      0x03: shutdown (QApplication.quit)

실행:
  python -m backend.app.plugins.linux.common.th_panel_host --socket /tmp/...sock
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
from datetime import datetime
from typing import Optional


OP_HIGHLIGHT = 0x01
OP_RESET = 0x02
OP_SHUTDOWN = 0x03


def _import_qt():
    """PySide6 lazy import — Linux 가 아닌 환경에서 패키지 import 자체는 막지 않는다."""
    from PySide6.QtCore import Qt, QSocketNotifier  # type: ignore
    from PySide6.QtGui import QFont, QGuiApplication  # type: ignore
    from PySide6.QtWidgets import QApplication, QLabel, QWidget, QVBoxLayout  # type: ignore
    return Qt, QSocketNotifier, QFont, QGuiApplication, QApplication, QLabel, QWidget, QVBoxLayout


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="ReplayKit TH visualization panel host")
    parser.add_argument("--socket", required=True, help="Unix socket path to listen on")
    parser.add_argument("--width", type=int, default=300)
    parser.add_argument("--height", type=int, default=300)
    args = parser.parse_args(argv)

    sock_path = args.socket
    # 기존 소켓 파일이 남아있으면 제거 — bind 실패 방지
    try:
        if os.path.exists(sock_path):
            os.unlink(sock_path)
    except OSError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(sock_path)
    server.listen(1)
    server.setblocking(False)

    (
        Qt,
        QSocketNotifier,
        QFont,
        QGuiApplication,
        QApplication,
        QLabel,
        QWidget,
        QVBoxLayout,
    ) = _import_qt()

    app = QApplication(sys.argv[:1])

    class PanelWindow(QWidget):
        def __init__(self, w: int, h: int):
            super().__init__()
            self.setWindowFlags(
                Qt.WindowStaysOnTopHint
                | Qt.FramelessWindowHint
                | Qt.Tool
            )
            self.setAttribute(Qt.WA_ShowWithoutActivating)
            self.resize(w, h)
            # 좌하단 (원본 TH_Lib 의 x=0, y=screen.height-h)
            screens = QGuiApplication.screens()
            geom = screens[0].geometry() if screens else None
            if geom is not None:
                self.move(geom.x(), geom.y() + geom.height() - h)
            self._label = QLabel("", self)
            self._label.setFont(QFont("Arial", 8))
            self._label.setStyleSheet("color: black; background: transparent;")
            self._label.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 2, 0, 0)
            layout.addWidget(self._label, alignment=Qt.AlignTop | Qt.AlignHCenter)
            self._reset()
            self.show()

        def highlight(self) -> None:
            self.setStyleSheet("background-color: #FFFF00;")
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            self._label.setText(now)
            self._label.setStyleSheet("color: black; background: transparent;")
            self.repaint()

        def _reset(self) -> None:
            self.setStyleSheet("background-color: #000000;")
            self._label.setText("")
            self.repaint()

        def reset(self) -> None:
            self._reset()

    panel = PanelWindow(args.width, args.height)

    # ─── client connection 관리 ─────────────────────────
    state = {"conn": None, "conn_notifier": None}

    def _close_conn() -> None:
        n = state.get("conn_notifier")
        c = state.get("conn")
        if n is not None:
            n.setEnabled(False)
            n.deleteLater()
            state["conn_notifier"] = None
        if c is not None:
            try:
                c.close()
            except OSError:
                pass
            state["conn"] = None

    def _on_conn_readable(_fd: int) -> None:
        c: Optional[socket.socket] = state.get("conn")
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
        for byte in data:
            if byte == OP_HIGHLIGHT:
                panel.highlight()
            elif byte == OP_RESET:
                panel.reset()
            elif byte == OP_SHUTDOWN:
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
        # 기존 conn 이 있으면 새 conn 우선. 한 번에 한 client.
        _close_conn()
        conn.setblocking(False)
        state["conn"] = conn
        notifier = QSocketNotifier(conn.fileno(), QSocketNotifier.Read)
        notifier.activated.connect(_on_conn_readable)
        state["conn_notifier"] = notifier

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
        try:
            os.unlink(sock_path)
        except OSError:
            pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
