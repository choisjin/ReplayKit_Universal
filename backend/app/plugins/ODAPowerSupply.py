"""ODA Technology OPS-3010 DC power supply plugin.

Exposes serial SCPI control through the ReplayKit module/device system. The
class name matches the file name so ``module_service`` can load it as a local
plugin (``backend/app/plugins/ODAPowerSupply.py``).

Connection model (mirrors ``SerialPlugin``):
  * ``__init__`` stores ``port``/``bps`` but does **not** import ``pyserial``.
  * ``Connect()`` opens the port and returns the ``*IDN?`` response.
  * ``Disconnect()`` safely turns the output OFF and closes the port.
  * ``IsConnected()`` tells the device manager whether the port is open.

All SCPI traffic uses a LF terminator (``\\n``) and a short post-command settle
time, matching the verified behaviour of the OPS-3010 reference script.

The connection lifecycle methods are hidden from the module-step dropdown by
``module_service.per_module_excluded``; only the power-supply control methods
are exposed as scenario steps.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)


class ODAPowerSupply:
    """ODA OPS-3010 serial SCPI power-supply controller."""

    def __init__(
        self,
        port: str = "",
        bps: int = 9600,
        timeout: float = 1.0,
        settle_set: float = 0.2,
        settle_query: float = 0.1,
    ):
        self.port = port
        self.bps = bps
        self.timeout = timeout
        self.settle_set = settle_set
        self.settle_query = settle_query
        self._terminator = "\n"
        self._serial: Optional[object] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    def Connect(self) -> str:
        """Open the serial port and identify the instrument."""
        with self._lock:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                return f"Already connected to {self.port}"

            import serial

            self._serial = serial.Serial(self.port, self.bps, timeout=self.timeout)
            time.sleep(0.3)
            self._reset_input_buffer()

            try:
                idn = self._query("*IDN?")
            except Exception as exc:
                idn = f"(IDN failed: {exc})"
            return f"Connected to {self.port} @ {self.bps} — IDN: {idn}"

    def Disconnect(self) -> str:
        """Turn output OFF and close the serial port."""
        with self._lock:
            if self._serial is not None and getattr(self._serial, "is_open", False):
                try:
                    self._send("OUTP OFF", settle=0.3)
                except Exception:
                    pass
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
        return "Disconnected"

    def IsConnected(self) -> bool:
        """Return True when the serial port is open."""
        with self._lock:
            return self._serial is not None and getattr(self._serial, "is_open", False)

    # ------------------------------------------------------------------
    # Basic SCPI helpers
    # ------------------------------------------------------------------

    def SendCommand(self, command: str, is_query: bool = False) -> str:
        """Send an arbitrary SCPI command. Queries return the instrument reply."""
        self._ensure_connected()
        if is_query:
            return self._query(command)
        return self._send(command)

    def Query(self, command: str) -> str:
        """Send a SCPI query and return the reply."""
        return self.SendCommand(command, is_query=True)

    # ------------------------------------------------------------------
    # Power-supply control (exposed as ReplayKit module steps)
    # ------------------------------------------------------------------

    def SetVoltage(self, voltage: float) -> str:
        """Set the output voltage (V)."""
        return self._set_value("VOLT", voltage)

    def SetCurrent(self, current: float) -> str:
        """Set the output current limit (A)."""
        return self._set_value("CURR", current)

    def SetOutput(self, state: str) -> str:
        """Set the output state to 'ON' or 'OFF'."""
        state = str(state).strip().upper()
        if state not in ("ON", "OFF"):
            raise ValueError("state must be 'ON' or 'OFF'")
        return self._send(f"OUTP {state}")

    def GetVoltage(self) -> str:
        """Query the programmed voltage (V)."""
        return self._query("VOLT?")

    def GetCurrent(self) -> str:
        """Query the programmed current limit (A), not the measured current."""
        return self._query("CURR?")

    def GetOutputState(self) -> str:
        """Query whether the output is ON or OFF."""
        return self._query("OUTP?")

    def MeasureVoltage(self) -> str:
        """Measure the actual output voltage (V)."""
        return self._query("MEAS:VOLT?")

    def MeasureCurrent(self) -> str:
        """Measure the actual output current (A)."""
        return self._query("MEAS:CURR?")

    def CheckCurrent(self, min_current: float = 0.0, max_current: float = 5.0, check_delay: int = 10000) -> str:
        """Measure the actual output current until it stays within range for 3 consecutive readings.

        Continuously calls ``MeasureCurrent()`` and returns ``PASS`` as soon as 3 readings in a
        row fall inside ``[min_current, max_current]``. Returns ``FAIL`` if the timeout expires.

        Args:
            min_current: Lower current bound (A).
            max_current: Upper current bound (A).
            check_delay: Timeout in milliseconds. Minimum 3000 ms (forced upward if lower).

        Returns:
            "PASS: {current}A" or "FAIL: timeout ({timeout_sec}s) — last values: ...".
        """
        self._ensure_connected()
        check_delay_ms = int(check_delay)
        if check_delay_ms < 3000:
            logger.warning(
                "[ODAPowerSupply] CheckCurrent check_delay=%dms is below minimum 3000ms; forcing 3000ms",
                check_delay_ms,
            )
            check_delay_ms = 3000
        timeout_sec = check_delay_ms / 1000.0
        pass_count = 0
        values: list[str] = []
        start = time.time()

        while time.time() - start < timeout_sec:
            resp = self.MeasureCurrent()
            values.append(resp)
            try:
                current = float(resp)
            except (ValueError, TypeError):
                continue

            if float(min_current) <= current <= float(max_current):
                pass_count += 1
            else:
                pass_count = 0

            if pass_count >= 3:
                logger.info("[ODAPowerSupply] CheckCurrent PASS: %sA", current)
                return f"PASS: {current}A"

            # Brief throttle to avoid saturating the serial line.
            time.sleep(0.05)

        logger.info("[ODAPowerSupply] CheckCurrent FAIL: timeout")
        return f"FAIL: timeout ({timeout_sec}s) - last values: {values[-5:]}"

    def SetOVP(self, voltage: float) -> str:
        """Set the over-voltage protection level (V)."""
        return self._set_value("VOLT:PROT", voltage)

    def SetOCP(self, current: float) -> str:
        """Set the over-current protection level (A)."""
        return self._set_value("CURR:PROT", current)

    def GetOVP(self) -> str:
        """Query the over-voltage protection level (V)."""
        return self._query("VOLT:PROT?")

    def GetOCP(self) -> str:
        """Query the over-current protection level (A)."""
        return self._query("CURR:PROT?")

    def GetOVPState(self) -> str:
        """Query whether OVP is enabled."""
        return self._query("VOLT:PROT:STAT?")

    def GetOCPState(self) -> str:
        """Query whether OCP is enabled."""
        return self._query("CURR:PROT:STAT?")

    def SetVoltageStep(self, step: float) -> str:
        """Set the VOLT UP/DOWN step size (V)."""
        return self._set_value("VOLT:STEP", step)

    def SetCurrentStep(self, step: float) -> str:
        """Set the CURR UP/DOWN step size (A)."""
        return self._set_value("CURR:STEP", step)

    def VoltageUp(self) -> str:
        """Increase the programmed voltage by the current step."""
        return self._send("VOLT UP")

    def VoltageDown(self) -> str:
        """Decrease the programmed voltage by the current step."""
        return self._send("VOLT DOWN")

    def CurrentUp(self) -> str:
        """Increase the programmed current by the current step."""
        return self._send("CURR UP")

    def CurrentDown(self) -> str:
        """Decrease the programmed current by the current step."""
        return self._send("CURR DOWN")

    def SaveState(self, memory: int) -> str:
        """Save the current instrument state to memory (1..N)."""
        return self._send(f"*SAV {int(memory)}")

    def RecallState(self, memory: int) -> str:
        """Recall a previously saved instrument state."""
        return self._send(f"*RCL {int(memory)}")

    def Reset(self) -> str:
        """Reset the instrument to its default state (*RST)."""
        return self._send("*RST")

    def ClearStatus(self) -> str:
        """Clear the status data register (*CLS)."""
        return self._send("*CLS")

    def GetIdentity(self) -> str:
        """Query the instrument identification string."""
        return self._query("*IDN?")

    def GetSystemError(self) -> str:
        """Query the next error from the system error queue."""
        return self._query("SYST:ERR?")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_connected(self) -> None:
        if not self.IsConnected():
            raise RuntimeError("ODA Power Supply serial port not connected")

    def _reset_input_buffer(self) -> None:
        if self._serial is None:
            return
        reset = getattr(self._serial, "reset_input_buffer", None)
        if callable(reset):
            reset()

    def _send(self, command: str, settle: Optional[float] = None) -> str:
        """Send a non-query SCPI command and return 'OK' or any trailing reply."""
        if self._serial is None:
            raise RuntimeError("Serial port not connected")
        if settle is None:
            settle = self.settle_set

        data = (command + self._terminator).encode("ascii", errors="ignore")
        logger.debug("ODA Power Supply TX --> %s", command)
        self._serial.write(data)
        time.sleep(settle)

        leftover = getattr(self._serial, "in_waiting", 0) or 0
        if leftover:
            try:
                text = self._serial.read(leftover).decode("utf-8", errors="ignore").strip()
                if text:
                    return text
            except Exception:
                pass
        return "OK"

    def _query(self, command: str) -> str:
        """Send a SCPI query and return the response line."""
        if self._serial is None:
            raise RuntimeError("Serial port not connected")

        data = (command + self._terminator).encode("ascii", errors="ignore")
        logger.debug("ODA Power Supply TX --> %s", command)
        self._serial.write(data)
        time.sleep(self.settle_query)

        try:
            raw = self._serial.readline()
        except Exception as exc:
            raise RuntimeError(f"Failed to read response for '{command}': {exc}") from exc

        response = raw.decode("utf-8", errors="ignore").strip()
        logger.debug("ODA Power Supply RX <-- %s", response)
        return response

    @staticmethod
    def _fmt_number(value: float) -> str:
        """Format a numeric SCPI argument compactly (e.g. 12 -> '12', 12.5 -> '12.5')."""
        try:
            f = float(value)
            if f.is_integer():
                return str(int(f))
            return format(f, "g")
        except Exception:
            return str(value)

    def _set_value(self, scpi_prefix: str, value: float) -> str:
        return self._send(f"{scpi_prefix} {self._fmt_number(value)}")

