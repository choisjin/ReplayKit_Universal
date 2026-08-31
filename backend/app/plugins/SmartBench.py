"""SmartBench — Smart Bench TCP 제어 플러그인.

TCP 소켓으로 Smart Bench 장비에 텍스트 명령어를 전송하여
전원(Battery/ACC/IGN), 버튼, 전류 측정, LED 검증 등을 수행합니다.

통신 프로토콜 (ATS SmartBenchClient 호환):
  Connect: "CONNECT\\n" → "CONNECTED"
  Send:    "command\\n"  → "OK" 또는 "OK;data"
  Disconnect: "DISCONNECT\\n" → "DISCONNECTED"

사용 예 (시나리오 스텝):
  SmartBench.Connect()
  SmartBench.Battery("on")
  SmartBench.ACC("on")
  SmartBench.IGN("on")
  SmartBench.CheckCurrent(0.5, 2.0, 10000)  → PASS/FAIL
  SmartBench.ButtonPress(0, 3000)
  SmartBench.IGN("off")
  SmartBench.Disconnect()
"""

import logging
import socket
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Smart Bench 릴레이 번호 정의
_RELAY = {
    "battery": 17,
    "acc": 18,
    "ign": 19,
    "ign3": 20,
    "usb": 30,
}


class SmartBench:
    """Smart Bench TCP 제어 모듈.

    생성자:
        host: Smart Bench IP 주소
        port: TCP 포트 (기본 8000)
    """

    def __init__(self, host: str = "", port: int = 8000):
        self._host = host
        self._port = int(port)
        self._sock: Optional[socket.socket] = None

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """Smart Bench에 TCP 연결 후 CONNECT 핸드셰이크.

        Returns:
            연결 결과 메시지
        """
        if not self._host:
            return "ERROR: host가 설정되지 않았습니다"
        if self._sock:
            return f"이미 연결됨: {self._host}:{self._port}"
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((self._host, self._port))
            # CONNECT 핸드셰이크
            sock.send(("CONNECT\n").encode("utf-8"))
            rc = sock.recv(1024).decode("utf-8").replace("\n", "").replace(" ", "").strip()
            if rc == "CONNECTED":
                sock.settimeout(10)
                self._sock = sock
                logger.info("[SmartBench] Connected to %s:%d", self._host, self._port)
                return f"Connected to {self._host}:{self._port}"
            else:
                sock.close()
                logger.error("[SmartBench] Handshake failed: %s", rc)
                return f"ERROR: unexpected response: {rc}"
        except Exception as e:
            self._sock = None
            logger.error("[SmartBench] Connection failed: %s", e)
            return f"ERROR: {e}"

    def Disconnect(self) -> str:
        """Smart Bench DISCONNECT 핸드셰이크 후 연결 해제.

        Returns:
            결과 메시지
        """
        if self._sock:
            try:
                self._sock.send(("DISCONNECT\n").encode("utf-8"))
                self._sock.recv(1024)
                self._sock.close()
            except Exception:
                try:
                    self._sock.close()
                except Exception:
                    pass
            self._sock = None
        logger.info("[SmartBench] Disconnected")
        return "Disconnected"

    def IsConnected(self) -> bool:
        """연결 상태 확인."""
        return self._sock is not None

    # ------------------------------------------------------------------
    # 내부 통신
    # ------------------------------------------------------------------

    def _send(self, command: str, _retry: bool = True) -> str:
        """TCP 텍스트 명령 전송 후 응답 수신 (개행 종료).

        ATS SmartBenchClient.Send()와 동일한 프로토콜:
          TX: "command\\n"
          RX: "OK" 또는 "OK;data"
        """
        if not self._sock:
            if not self._host:
                return "ERROR: host가 설정되지 않았습니다"
            result = self.Connect()
            if result.startswith("ERROR"):
                return result

        try:
            self._sock.sendall((command + "\n").encode("utf-8"))
            data = self._sock.recv(4096).decode("utf-8", errors="replace").strip()
            logger.info("[SmartBench] TX: %s → RX: %s", command, data)
            return data if data else "OK"
        except Exception as e:
            logger.error("[SmartBench] Send failed: %s", e)
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            if _retry:
                logger.info("[SmartBench] Reconnecting...")
                return self._send(command, _retry=False)
            return f"ERROR: {e}"

    # ------------------------------------------------------------------
    # 전원 제어
    # ------------------------------------------------------------------

    def Battery(self, status: str = "on") -> str:
        """Battery(+B) 릴레이 제어.

        Args:
            status: 'on' 또는 'off'

        Returns:
            결과 메시지
        """
        cmd = f"relay-{_RELAY['battery']}-{status}"
        return self._send(cmd)

    def ACC(self, status: str = "on") -> str:
        """ACC 릴레이 제어.

        Args:
            status: 'on' 또는 'off'

        Returns:
            결과 메시지
        """
        cmd = f"relay-{_RELAY['acc']}-{status}"
        return self._send(cmd)

    def IGN(self, status: str = "on") -> str:
        """IGN 릴레이 제어.

        Args:
            status: 'on' 또는 'off'

        Returns:
            결과 메시지
        """
        cmd = f"relay-{_RELAY['ign']}-{status}"
        return self._send(cmd)

    def IGN3(self, status: str = "on") -> str:
        """IGN3 릴레이 제어.

        Args:
            status: 'on' 또는 'off'

        Returns:
            결과 메시지
        """
        cmd = f"relay-{_RELAY['ign3']}-{status}"
        return self._send(cmd)

    def USB(self, status: str = "on") -> str:
        """USB 릴레이 제어.

        Args:
            status: 'on' 또는 'off'

        Returns:
            결과 메시지
        """
        cmd = f"relay-{_RELAY['usb']}-{status}"
        return self._send(cmd)

    # ------------------------------------------------------------------
    # 전류 측정
    # ------------------------------------------------------------------

    def CheckCurrent(self, min_current: float = 0.0, max_current: float = 5.0, check_delay: int = 10000) -> str:
        """전류를 측정하여 범위 내 진입 시 PASS, 타임아웃 시 FAIL.

        연속 3회 범위 내 값이 측정되면 즉시 PASS 반환.

        Args:
            min_current: 최소 전류 (A)
            max_current: 최대 전류 (A)
            check_delay: 타임아웃 (ms)

        Returns:
            "PASS: {current}A" 또는 "FAIL: timeout — {values}"
        """
        pass_count = 0
        values = []
        # "current-1000" 명령은 장비 내부 ~1000ms 샘플링 + 통신 오버헤드로 1회
        # 측정에 1.0~1.5s 소요 → 연속 3회 측정 정책상 3000ms 미만 타임아웃은
        # 논리적으로 성립 불가라 최소 3000ms로 강제 상향한다.
        check_delay_ms = int(check_delay)
        if check_delay_ms < 3000:
            logger.warning(
                "[SmartBench] CheckCurrent check_delay=%dms is below minimum 3000ms; forcing 3000ms",
                check_delay_ms,
            )
            check_delay_ms = 3000
        timeout_sec = check_delay_ms / 1000.0
        start = time.time()

        while time.time() - start < timeout_sec:
            resp = self._send("current-1000")
            try:
                raw = float(resp.split(";")[1])
                current = round(raw / 1000.0, 3)
                if current < 0:
                    current = 0.0
            except (IndexError, ValueError):
                continue

            values.append(current)

            if float(min_current) <= current <= float(max_current):
                pass_count += 1
            else:
                pass_count = 0

            if pass_count >= 3:
                logger.info("[SmartBench] CheckCurrent PASS: %sA", current)
                return f"PASS: {current}A"

        logger.info("[SmartBench] CheckCurrent FAIL: timeout")
        return f"FAIL: timeout ({timeout_sec}s) — last values: {values[-5:]}"

    def CheckCurrentMoment(self, min_current: float = 0.0, max_current: float = 5.0) -> str:
        """순간 전류를 측정하여 범위 내이면 PASS.

        1회 범위 내 진입 시 PASS, 연속 3회 범위 외이면 FAIL.

        Args:
            min_current: 최소 전류 (A)
            max_current: 최대 전류 (A)

        Returns:
            "PASS: {current}A" 또는 "FAIL: {current}A"
        """
        fail_count = 0

        while True:
            resp = self._send("current-1000")
            try:
                raw = float(resp.split(";")[1])
                current = round(raw / 1000.0, 3)
            except (IndexError, ValueError):
                continue

            if float(min_current) <= current <= float(max_current):
                return f"PASS: {current}A"
            else:
                fail_count += 1

            if fail_count >= 3:
                return f"FAIL: {current}A (out of range)"

    def CheckCurrentMaintain(self, min_current: float = 0.0, max_current: float = 5.0, check_delay: int = 10000) -> str:
        """전류가 지정 시간 동안 범위 내 유지되는지 검증.

        범위를 벗어나면 5회 연속 시 즉시 FAIL.
        타임아웃까지 범위 내 유지 시 PASS.

        Args:
            min_current: 최소 전류 (A)
            max_current: 최대 전류 (A)
            check_delay: 유지 시간 (ms)

        Returns:
            "PASS: maintained {duration}" 또는 "FAIL: dropped — {values}"
        """
        in_range = False
        fail_count = 0
        values = []
        timeout_sec = int(check_delay) / 1000.0
        start = time.time()

        while True:
            resp = self._send("current-1000")
            try:
                raw = float(resp.split(";")[1])
                current = round(raw / 1000.0, 3)
            except (IndexError, ValueError):
                continue

            values.append(current)
            elapsed = time.time() - start

            if float(min_current) <= current <= float(max_current):
                in_range = True
                fail_count = 0
            else:
                in_range = False
                fail_count += 1

            if fail_count >= 5:
                return f"FAIL: dropped out of range — {values[-5:]}"

            if elapsed >= timeout_sec and in_range:
                return f"PASS: maintained {elapsed:.1f}s — {current}A"

            if elapsed >= timeout_sec and not in_range:
                return f"FAIL: not in range at timeout — {values[-5:]}"

    # ------------------------------------------------------------------
    # 버튼 / LED
    # ------------------------------------------------------------------

    def ButtonPress(self, button_num: int = 0, press_time: int = 1000) -> str:
        """버튼을 누릅니다.

        Args:
            button_num: 버튼 번호 (0 = ECALL 등)
            press_time: 누르는 시간 (ms)

        Returns:
            응답 메시지
        """
        cmd = f"button-{int(button_num)}-{int(press_time)}"
        return self._send(cmd)

    def LEDCheck(self, data: str = "") -> str:
        """LED 상태를 검증합니다.

        Args:
            data: LED 검증 데이터 (예: 'honda;4.0;GREEN;BLINK;RED;ON')

        Returns:
            "PASS: {details}" 또는 "FAIL: {details}"
        """
        if not data:
            return "ERROR: data 파라미터가 필요합니다"

        parts = data.split(";")
        oem = parts[0]
        meas_time = parts[1]

        cmd = f"led-{oem}-{meas_time}"
        resp = self._send(cmd)

        if resp.startswith("ERROR"):
            return resp

        # 간이 판정 — 상세 판정은 시나리오에서 직접 수행
        return f"PASS: {resp}" if "OK" in resp else f"FAIL: {resp}"

    # ------------------------------------------------------------------
    # 릴레이 범용
    # ------------------------------------------------------------------

    def Relay(self, relay_num: int, status: str = "on") -> str:
        """릴레이를 직접 제어합니다.

        Args:
            relay_num: 릴레이 번호
            status: 'on' 또는 'off'

        Returns:
            응답 메시지
        """
        cmd = f"relay-{int(relay_num)}-{status}"
        return self._send(cmd)

    def SendRaw(self, command: str) -> str:
        """원시 텍스트 명령을 직접 전송합니다.

        Args:
            command: 전송할 명령어 문자열

        Returns:
            응답 메시지
        """
        return self._send(command)

    # ------------------------------------------------------------------
    # 상태
    # ------------------------------------------------------------------

    def GetStatus(self) -> str:
        """현재 모듈 상태를 조회합니다."""
        return f"Host: {self._host}:{self._port} | Connected: {self.IsConnected()}"
