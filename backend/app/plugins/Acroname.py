"""Acroname — Brainstem USB 허브/스위치 포트 제어 플러그인 (USB 로 연결되는 장비).

Reference/acroname/Acroname.py (Robot Framework `ACRONAME_Ctrl`) 를 ReplayKit 모듈 규약으로
이식했다. 원본의 `robot.api.deco.keyword` / `gevent` 의존은 제거했고(스텝은 모듈 스레드풀에서
실행되므로 `time.sleep` 으로 충분), 반환값을 ReplayKit 계약("ok: …" / "FAIL: …")으로 바꿨다.

지원 장비
--------
USBHub2x4(4포트) / USBHub3p(8포트) / USBHub3c(8포트) / USBCSwitch(4포트 + mux).
모델 상수는 설치된 brainstem 버전에 있는 것만 사용한다(없는 모델은 자동 제외).

설계
----
- **인스턴스 1개 = PC 에 물린 Acroname 장비 전부**. PCAN 이 CAN 채널을 스텝 인자로 고르듯,
  허브는 스텝의 `hub` 인자(시리얼 번호 또는 '#인덱스')로 고른다. 허브가 한 대면 비워두면 된다.
  connect_type="none" 모듈이라 디바이스는 모듈당 1개만 등록되므로(device_manager 의 dedup),
  여러 대 운용은 이 `hub` 인자가 담당한다.
- 등록 시 `serial_number` 또는 `index` 를 지정하면 그 장비 **하나만** 연다. 둘 다 비우면
  발견된 전 장비를 연다(다른 툴이 특정 허브를 점유 중이면 그 허브만 실패로 남고 나머지는 정상).
- **brainstem 미설치 / 장비 미탐지여도 hard-fail 금지**. import 는 지연 처리하고 Connect 는
  'ERROR' 를 반환해 미연결로 남는다 — 스텝 목록·UI 는 계속 동작한다.
- 포트 번호(`port`)와 mux 채널(`channel`)은 장비 문서 그대로 0-based.

결과 계약: 성공 "ok: …", 실패 "FAIL: …" (playback_service 가 "FAIL:" 접두를 스텝 실패로 처리).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

# brainstem 은 선택 의존성(pip install brainstem). 미설치여도 모듈 자체는 import 돼야
# 스텝 목록/가이드가 정상 동작한다 — 실제 사용 시점에만 실패시킨다.
try:  # pragma: no cover - 환경 의존
    import brainstem
    from brainstem.result import Result
    _IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # pragma: no cover - 환경 의존
    brainstem = None  # type: ignore[assignment]
    Result = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(_e)

_NO_BRAINSTEM = ("brainstem 라이브러리를 불러올 수 없습니다 (pip install brainstem)"
                 f"{f' — {_IMPORT_ERROR}' if _IMPORT_ERROR else ''}")

# (brainstem.defs 상수명, brainstem.stem 클래스명, 포트 수, mux 스위치 여부)
_MODEL_SPECS: tuple[tuple[str, str, int, bool], ...] = (
    ("MODEL_USBHUB_2X4", "USBHub2x4", 4, False),
    ("MODEL_USBHUB_3P", "USBHub3p", 8, False),
    ("MODEL_USBHUB_3C", "USBHub3c", 8, False),
    ("MODEL_USB_C_SWITCH", "USBCSwitch", 4, True),
)

# 전류/전압 폴링 간격 — 원본은 1초 고정이었으나 짧은 전환도 잡히도록 촘촘히 본다.
_POLL_INTERVAL_S = 0.2
# BUSY 재시도 (원본 Switch_Enable 의 5회 루프를 모든 포트 조작에 공통 적용)
_BUSY_RETRIES = 5
_BUSY_DELAY_S = 0.05


# ──────────────────────────────────────────────────────────────────────────
# 파싱 헬퍼 — 스텝 파라미터는 전부 문자열로 들어오므로 관대하게 캐스팅한다.
# ──────────────────────────────────────────────────────────────────────────
def _to_int(v: Any, default: int = 0) -> int:
    s = str(v).strip()
    if not s:
        return default
    try:
        return int(s, 0)  # '0x40' 같은 접두사도 허용
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return default


def _parse_range(text: Any) -> tuple[float, float]:
    """'4.5~5.5' → (4.5, 5.5). '~' 대신 '-'/','/공백도 허용."""
    s = str(text).strip()
    for sep in ("~", ",", ".."):
        if sep in s:
            lo, hi = s.split(sep, 1)
            break
    else:
        parts = s.split()
        if len(parts) == 2:
            lo, hi = parts
        else:
            raise ValueError(f"범위 형식이 잘못됨: '{text}' (예: '4.5~5.5')")
    lo_f, hi_f = float(lo.strip()), float(hi.strip())
    if lo_f > hi_f:
        lo_f, hi_f = hi_f, lo_f
    return lo_f, hi_f


def _fmt_serial(serial: Any) -> str:
    """brainstem 시리얼(int)을 장비 라벨과 같은 8자리 대문자 hex 로."""
    try:
        return f"0x{int(serial):08X}"
    except (TypeError, ValueError):
        return str(serial)


def _err_name(err: Any) -> str:
    """brainstem Result 에러 코드를 사람이 읽는 이름으로."""
    if Result is None or err is None:
        return str(err)
    for name in dir(Result):
        if name.isupper() and getattr(Result, name, None) == err:
            return f"{name}({err})"
    return str(err)


def _ok(err: Any) -> bool:
    return Result is not None and err == Result.NO_ERROR


class _Hub:
    """연결된 Acroname 장비 1대."""

    def __init__(self, stem: Any, serial: int, model_name: str,
                 n_ports: int, is_switch: bool) -> None:
        self.stem = stem
        self.serial = int(serial)
        self.model_name = model_name
        self.n_ports = int(n_ports)
        self.is_switch = bool(is_switch)

    @property
    def label(self) -> str:
        return f"{self.model_name} {_fmt_serial(self.serial)}"


class Acroname:
    """Acroname/Brainstem USB 허브·스위치 제어."""

    # 상수 (원본 ACRONAME_Ctrl 과 동일 의미)
    USBC_COMMON = 0
    ENABLE = 1
    DISABLE = 0

    def __init__(self, serial_number: str = "", index: str = "") -> None:
        # 둘 다 비면 발견된 전 장비를 연다. serial_number 가 index 보다 우선.
        self.serial_number = str(serial_number).strip()
        self.index = str(index).strip()
        self._hubs: list[_Hub] = []
        self._lock = threading.RLock()
        # 진단용 — 마지막 스캔에서 본 장비 라벨. None = 아직 스캔 안 함, [] = 스캔했으나 0대.
        self._last_discovery: Optional[list[str]] = None

    # ── 연결 수명주기 (스텝 UI 에서는 숨김) ─────────────────────────────
    def Connect(self):
        """USB 로 발견된 Acroname 장비에 연결한다. 최소 1대 성공 시 'OK'."""
        with self._lock:
            if self._hubs:
                return "OK"
            if brainstem is None:
                logger.error("Acroname Connect: %s", _NO_BRAINSTEM)
                return "ERROR"
            try:
                found = brainstem.discover.findAllModules(brainstem.link.Spec.USB)
            except Exception as e:
                logger.error("Acroname discover failed: %s", e)
                return "ERROR"

            self._last_discovery = [
                f"#{i} model={getattr(m, 'model', '?')} sn={_fmt_serial(getattr(m, 'serial_number', 0))}"
                for i, m in enumerate(found)
            ]
            if not found:
                logger.error("Acroname Connect: USB 로 발견된 장비가 없습니다")
                return "ERROR"

            targets = self._select_targets(found)
            if not targets:
                logger.error("Acroname Connect: 지정한 장비를 찾지 못함 "
                             "(serial_number=%r index=%r, 발견: %s)",
                             self.serial_number, self.index, self._last_discovery)
                return "ERROR"

            for spec in targets:
                hub = self._open(spec)
                if hub is not None:
                    self._hubs.append(hub)

            if not self._hubs:
                logger.error("Acroname Connect: 발견은 됐지만 연결에 실패했습니다 (%s)",
                             self._last_discovery)
                return "ERROR"
            logger.info("Acroname connected: %s", [h.label for h in self._hubs])
            return "OK"

    def _select_targets(self, found: list) -> list:
        """serial_number / index 설정에 따라 연결 대상 장비를 고른다."""
        if self.serial_number:
            want = _to_int(self.serial_number, -1)
            return [m for m in found if int(getattr(m, "serial_number", -2)) == want]
        if self.index:
            i = _to_int(self.index, -1)
            return [found[i]] if 0 <= i < len(found) else []
        return list(found)

    def _open(self, spec: Any) -> Optional[_Hub]:
        """발견된 장비 스펙 하나를 열어 _Hub 로 반환 (실패 시 None)."""
        model = getattr(spec, "model", None)
        serial = int(getattr(spec, "serial_number", 0))
        entry = None
        for defs_name, stem_name, n_ports, is_switch in _MODEL_SPECS:
            const = getattr(brainstem.defs, defs_name, None)
            cls = getattr(brainstem.stem, stem_name, None)
            if const is None or cls is None or model != const:
                continue
            entry = (cls, stem_name, n_ports, is_switch)
            break
        if entry is None:
            logger.warning("Acroname: 지원하지 않는 모델 %s (sn=%s) — 건너뜀",
                           model, _fmt_serial(serial))
            return None

        cls, stem_name, n_ports, is_switch = entry
        try:
            stem = cls()
            err = stem.discoverAndConnect(brainstem.link.Spec.USB, serial)
        except Exception as e:
            logger.error("Acroname %s (sn=%s) 연결 예외: %s", stem_name, _fmt_serial(serial), e)
            return None
        if not _ok(err):
            logger.error("Acroname %s (sn=%s) 연결 실패: %s",
                         stem_name, _fmt_serial(serial), _err_name(err))
            try:
                stem.disconnect()
            except Exception:
                pass
            return None
        return _Hub(stem, serial, stem_name, n_ports, is_switch)

    def IsConnected(self) -> bool:
        return bool(self._hubs)

    def Disconnect(self):
        """모든 장비 연결을 해제한다 (포트 상태는 건드리지 않음)."""
        with self._lock:
            for hub in self._hubs:
                try:
                    hub.stem.disconnect()
                except Exception:
                    pass
            self._hubs.clear()
            return "OK"

    # ── 내부 헬퍼 ───────────────────────────────────────────────────────
    def _get_hub(self, hub: str = "") -> _Hub:
        """`hub` 인자 → 대상 장비. 비면 (유일한/첫) 장비.

        형식: 시리얼 번호('0x40F5A1B2' 또는 10진수) 또는 '#0' 형태의 발견 인덱스.
        """
        if not self._hubs:
            raise RuntimeError(_NO_BRAINSTEM if brainstem is None
                               else "Acroname 장비가 연결되어 있지 않습니다")
        key = str(hub).strip()
        if not key:
            return self._hubs[0]
        if key.startswith("#"):
            i = _to_int(key[1:], -1)
            if 0 <= i < len(self._hubs):
                return self._hubs[i]
            raise ValueError(f"허브 인덱스 범위 초과: {key} (연결된 장비 {len(self._hubs)}대)")
        want = _to_int(key, -1)
        for h in self._hubs:
            if h.serial == want:
                return h
        known = ", ".join(f"#{i} {h.label}" for i, h in enumerate(self._hubs))
        raise ValueError(f"허브를 찾을 수 없음: '{hub}' (연결된 장비: {known})")

    def _check_port(self, hub: _Hub, port: Any) -> int:
        p = _to_int(port, -1)
        if not (0 <= p < hub.n_ports):
            raise ValueError(f"포트 번호 범위 초과: {port} "
                             f"({hub.label} 은 0~{hub.n_ports - 1})")
        return p

    @staticmethod
    def _retry_busy(fn, *args) -> Any:
        """BUSY 응답이면 잠깐 쉬었다 재시도 (원본 Switch_Enable 의 재시도 루프 일반화)."""
        err = None
        for _ in range(_BUSY_RETRIES):
            err = fn(*args)
            if Result is None or err != Result.BUSY:
                return err
            time.sleep(_BUSY_DELAY_S)
        return err

    def _read_micro(self, hub: _Hub, port: int, kind: str) -> float:
        """포트 전압/전류를 V/A 단위로 읽는다 (brainstem 은 uV/uA 반환)."""
        getter = hub.stem.usb.getPortVoltage if kind == "voltage" else hub.stem.usb.getPortCurrent
        res = getter(port)
        err = getattr(res, "error", None)
        if err is not None and not _ok(err):
            raise RuntimeError(f"{kind} 읽기 실패: {_err_name(err)}")
        raw = getattr(res, "value", None)
        if raw is None:
            raise RuntimeError(f"{kind} 값을 읽지 못했습니다")
        return float(raw) / 1_000_000.0

    def _monitor(self, kind: str, port: Any, value_range: Any,
                 timeout_ms: Any, hub: str, unit: str):
        """전압/전류가 범위에 들어올 때까지 폴링 (원본 Port_Voltage/Port_Current)."""
        with self._lock:
            try:
                h = self._get_hub(hub)
                p = self._check_port(h, port)
                lo, hi = _parse_range(value_range)
            except (RuntimeError, ValueError) as e:
                return f"FAIL: {e}"
            deadline = time.monotonic() + max(0, _to_int(timeout_ms, 5000)) / 1000.0
            samples: list[float] = []
            while True:
                try:
                    val = self._read_micro(h, p, kind)
                except Exception as e:
                    return f"FAIL: [{h.label}] port{p} {e}"
                samples.append(round(val, 4))
                if lo <= val <= hi:
                    return (f"ok: [{h.label}] port{p} {kind} {val:.3f}{unit} "
                            f"in {lo}~{hi} (samples={len(samples)})")
                if time.monotonic() >= deadline:
                    break
                time.sleep(_POLL_INTERVAL_S)
            tail = samples[-10:]
            return (f"FAIL: [{h.label}] port{p} {kind} {lo}~{hi}{unit} 범위에 들어오지 않음 "
                    f"(최근값 {tail})")

    # ── 포트 전원/데이터 제어 ────────────────────────────────────────────
    def port_enable(self, port, hub: str = ""):
        """지정 포트를 활성화합니다 (전원/데이터 ON)."""
        with self._lock:
            try:
                h = self._get_hub(hub)
                p = self._check_port(h, port)
            except (RuntimeError, ValueError) as e:
                return f"FAIL: {e}"
            err = self._retry_busy(h.stem.usb.setPortEnable, p)
            if not _ok(err):
                return f"FAIL: [{h.label}] port{p} enable 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] port{p} enabled"

    def port_disable(self, port, hub: str = ""):
        """지정 포트를 비활성화합니다 (전원/데이터 OFF)."""
        with self._lock:
            try:
                h = self._get_hub(hub)
                p = self._check_port(h, port)
            except (RuntimeError, ValueError) as e:
                return f"FAIL: {e}"
            err = self._retry_busy(h.stem.usb.setPortDisable, p)
            if not _ok(err):
                return f"FAIL: [{h.label}] port{p} disable 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] port{p} disabled"

    def port_reset(self, port, off_ms: str = "1000", hub: str = ""):
        """지정 포트를 껐다가(off_ms 대기) 다시 켭니다 — USB 재열거 테스트용."""
        with self._lock:
            try:
                h = self._get_hub(hub)
                p = self._check_port(h, port)
            except (RuntimeError, ValueError) as e:
                return f"FAIL: {e}"
            wait_ms = max(0, _to_int(off_ms, 1000))
            err = self._retry_busy(h.stem.usb.setPortDisable, p)
            if not _ok(err):
                return f"FAIL: [{h.label}] port{p} disable 실패 — {_err_name(err)}"
            time.sleep(wait_ms / 1000.0)
            err = self._retry_busy(h.stem.usb.setPortEnable, p)
            if not _ok(err):
                return f"FAIL: [{h.label}] port{p} 재활성화 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] port{p} reset (off {wait_ms}ms)"

    def all_port_enable(self, hub: str = ""):
        """장비의 모든 포트를 활성화합니다."""
        return self._all_ports(hub, enable=True)

    def all_port_disable(self, hub: str = ""):
        """장비의 모든 포트를 비활성화합니다."""
        return self._all_ports(hub, enable=False)

    def _all_ports(self, hub: str, enable: bool):
        with self._lock:
            try:
                h = self._get_hub(hub)
            except RuntimeError as e:
                return f"FAIL: {e}"
            fn = h.stem.usb.setPortEnable if enable else h.stem.usb.setPortDisable
            failed = []
            for p in range(h.n_ports):
                if not _ok(self._retry_busy(fn, p)):
                    failed.append(p)
            what = "enable" if enable else "disable"
            if failed:
                return f"FAIL: [{h.label}] all port {what} 중 실패 포트 {failed}"
            return f"ok: [{h.label}] all {h.n_ports} ports {what}d"

    def set_upstream_mode(self, mode, hub: str = ""):
        """업스트림 포트 선택 모드를 설정합니다 (0=Port0, 1=Port1, 2=Auto)."""
        with self._lock:
            try:
                h = self._get_hub(hub)
            except RuntimeError as e:
                return f"FAIL: {e}"
            m = _to_int(mode, -1)
            if m not in (0, 1, 2):
                return f"FAIL: upstream mode 는 0(Port0)/1(Port1)/2(Auto) 중 하나 — 받은 값 '{mode}'"
            err = self._retry_busy(h.stem.usb.setUpstreamMode, m)
            if not _ok(err):
                return f"FAIL: [{h.label}] upstream mode {m} 설정 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] upstream mode = {m}"

    # ── USB-C Switch (mux) 전용 ─────────────────────────────────────────
    def switch_enable(self, hub: str = ""):
        """USB-C Switch 전용: 공통 포트와 mux 를 활성화합니다."""
        with self._lock:
            try:
                h = self._get_hub(hub)
            except RuntimeError as e:
                return f"FAIL: {e}"
            if not h.is_switch:
                return f"FAIL: [{h.label}] 은 USB-C Switch 가 아닙니다"
            err = self._retry_busy(h.stem.usb.setPortEnable, self.USBC_COMMON)
            if not _ok(err):
                return f"FAIL: [{h.label}] 공통 포트 활성화 실패 — {_err_name(err)}"
            mux_err = h.stem.mux.setEnable(self.ENABLE)
            if not _ok(mux_err):
                return f"FAIL: [{h.label}] mux 활성화 실패 — {_err_name(mux_err)}"
            return f"ok: [{h.label}] switch enabled"

    def switch_disable(self, hub: str = ""):
        """USB-C Switch 전용: mux 와 공통 포트를 비활성화합니다."""
        with self._lock:
            try:
                h = self._get_hub(hub)
            except RuntimeError as e:
                return f"FAIL: {e}"
            if not h.is_switch:
                return f"FAIL: [{h.label}] 은 USB-C Switch 가 아닙니다"
            mux_err = h.stem.mux.setEnable(self.DISABLE)
            err = self._retry_busy(h.stem.usb.setPortDisable, self.USBC_COMMON)
            if not _ok(mux_err):
                return f"FAIL: [{h.label}] mux 비활성화 실패 — {_err_name(mux_err)}"
            if not _ok(err):
                return f"FAIL: [{h.label}] 공통 포트 비활성화 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] switch disabled"

    def select_channel(self, channel, hub: str = ""):
        """USB-C Switch 전용: mux 를 켜고 지정 채널로 전환합니다."""
        with self._lock:
            try:
                h = self._get_hub(hub)
            except RuntimeError as e:
                return f"FAIL: {e}"
            if not h.is_switch:
                return f"FAIL: [{h.label}] 은 USB-C Switch 가 아닙니다"
            ch = _to_int(channel, -1)
            if not (0 <= ch < h.n_ports):
                return f"FAIL: mux 채널 범위 초과: {channel} (0~{h.n_ports - 1})"
            h.stem.mux.setEnable(self.ENABLE)
            err = h.stem.mux.setChannel(ch)
            if not _ok(err):
                return f"FAIL: [{h.label}] 채널 {ch} 전환 실패 — {_err_name(err)}"
            return f"ok: [{h.label}] mux channel = {ch}"

    # ── 측정 / 검증 ─────────────────────────────────────────────────────
    def get_port_voltage(self, port, hub: str = ""):
        """지정 포트의 현재 전압(V)을 읽습니다."""
        return self._read_once("voltage", port, hub, "V")

    def get_port_current(self, port, hub: str = ""):
        """지정 포트의 현재 전류(A)를 읽습니다."""
        return self._read_once("current", port, hub, "A")

    def _read_once(self, kind: str, port: Any, hub: str, unit: str):
        with self._lock:
            try:
                h = self._get_hub(hub)
                p = self._check_port(h, port)
                val = self._read_micro(h, p, kind)
            except Exception as e:
                return f"FAIL: {e}"
            return f"ok: [{h.label}] port{p} {kind} {val:.3f}{unit}"

    def check_port_voltage(self, port, value_range, timeout_ms: str = "5000", hub: str = ""):
        """지정 포트 전압이 범위(예 '4.5~5.5' V)에 들어올 때까지 확인합니다."""
        return self._monitor("voltage", port, value_range, timeout_ms, hub, "V")

    def check_port_current(self, port, value_range, timeout_ms: str = "5000", hub: str = ""):
        """지정 포트 전류가 범위(예 '0.1~0.5' A)에 들어올 때까지 확인합니다."""
        return self._monitor("current", port, value_range, timeout_ms, hub, "A")

    # ── 진단 ────────────────────────────────────────────────────────────
    def check_status(self):
        """진단: 연결된 Acroname 장비 목록(모델/시리얼/포트 수)과 마지막 스캔 결과를 보고합니다."""
        with self._lock:
            if brainstem is None:
                return f"FAIL: {_NO_BRAINSTEM}"
            if not self._hubs:
                if self._last_discovery is None:
                    seen = "(아직 스캔하지 않음)"
                elif not self._last_discovery:
                    seen = "(USB 로 발견된 Acroname 장비 0대 — 케이블/전원 확인)"
                else:
                    seen = "; ".join(self._last_discovery)
                return f"FAIL: 연결된 장비 없음 — 마지막 스캔: {seen}"
            lines = [
                f"#{i} {h.label} ports=0~{h.n_ports - 1}"
                + (" (USB-C Switch, mux)" if h.is_switch else "")
                for i, h in enumerate(self._hubs)
            ]
            return "ok: " + " | ".join(lines)
