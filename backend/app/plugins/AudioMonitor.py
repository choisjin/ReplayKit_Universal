"""AudioMonitor — 마이크(오디오 입력) 녹음·무음(drop) 판정·기준음 비교 플러그인.

Reference/audio_record&compare/audio_monitor.py (standalone AUDIO_* 함수 모음) 를
ReplayKit 모듈 규약으로 이식했다. 판정 알고리즘(청크 평균 peak → drop/pass 카운트,
correlation + dB 차 비교)은 원본 그대로이고, 아래만 바꿨다.

  - print → logger, 반환값 → ReplayKit 계약("ok: …" / "FAIL: …").
    ("FAIL:" 접두사를 playback_service 가 스텝 실패로 처리한다)
  - 장치를 **디바이스 등록 시점에 고정**한다. 보조 디바이스 1개 = 마이크 1개이고,
    스텝에서는 session_name 만 지정하면 된다(다른 마이크를 쓰려면 device 인자로 override).
  - 저장 경로를 ReplayKit 규약으로: 재생 중이면 {run_dir}/logs/audio/, 스텝 테스트면
    results/Temp_logs/audio/. 기준음(reference)만은 런과 무관하게 재사용해야 하므로
    results/Audio_Reference/ 에 고정 저장한다.
  - 파형 이미지는 matplotlib 대신 numpy+cv2 로 그린다(이미 하드 의존이라 추가 설치 불필요).

설계
----
- **인스턴스 1개 = 마이크 1개**. module_service._instance_key 가 device_index 를 키에
  포함하므로 마이크를 여러 개 등록해도 서로 간섭하지 않는다.
- PyAudio 인스턴스는 프로세스 전역 1개를 공유(refcount). 마이크마다 PyAudio 를 만들면
  PortAudio 호스트 API 가 중복 초기화되어 장치 열거가 흔들린다.
- 녹음 중이 아닐 때는 스트림을 열어두지 않는다 — 다른 프로그램(통화/녹음 앱)이 같은
  마이크를 쓸 수 있어야 하기 때문. 연결 확인은 등록 시 짧은 probe open 으로 한 번만.

Windows 에서 장치 이름은 제어판 [소리]→[녹음]→속성 에서 바꿀 수 있다(IVI, Cluster 등).
Reference/audio_record&compare/ats_common_mic_name_change.md 참고.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# pyaudio 는 선택 의존성. 미설치여도 import 는 성공해야 스텝 목록/가이드가 동작한다.
try:  # pragma: no cover - 환경 의존
    import pyaudio
    _IMPORT_ERROR: Optional[str] = None
except Exception as _e:  # pragma: no cover - 환경 의존
    pyaudio = None  # type: ignore[assignment]
    _IMPORT_ERROR = str(_e)

try:  # numpy 는 requirements 에 있음 — 없으면 순수 파이썬 폴백
    import numpy as np
except Exception:  # pragma: no cover
    np = None  # type: ignore[assignment]

try:  # cv2 도 requirements(opencv-python-headless) — 없으면 이미지 생략
    import cv2
except Exception:  # pragma: no cover
    cv2 = None  # type: ignore[assignment]

_NO_PYAUDIO = ("PyAudio 를 불러올 수 없습니다 (pip install pyaudio)"
               f"{f' — {_IMPORT_ERROR}' if _IMPORT_ERROR else ''}")

# PortAudio 는 권한/점유 문제를 전부 -9999 'Unanticipated host error' 하나로 뭉뚱그린다.
# 장치 열거는 되는데 열기만 실패하면 십중팔구 Windows 마이크 권한이라 힌트를 붙인다.
_HOST_ERROR_HINT = (
    " — Windows [설정] → [개인 정보 및 보안] → [마이크] 에서 '마이크 액세스'와 "
    "'데스크톱 앱이 마이크에 액세스하도록 허용'을 켜세요. "
    "(다른 앱이 장치를 독점 중이거나 장치가 비활성 상태여도 같은 오류가 납니다)"
)


def _open_error(idx: int, name: str, exc: Exception) -> str:
    msg = str(exc)
    hint = _HOST_ERROR_HINT if ("-9999" in msg or "Unanticipated host error" in msg) else ""
    return f"장치 [{idx}] {name} 열기 실패 — {msg}{hint}"

# 녹음 포맷 — 원본과 동일 (44.1kHz / mono / 16-bit PCM)
DEFAULT_RATE = 44100
DEFAULT_CHUNK = 2 ** 11
CHANNELS = 1

# 비교 판정 임계 (원본 동일): 상관계수 > 0.5 그리고 dB 차 < 5.0 이면 PASS
COMPARE_MIN_CORRELATION = 0.5
COMPARE_MAX_DB_DIFF = 5.0


# ──────────────────────────────────────────────────────────────────────────
# PyAudio 공유 (프로세스 전역 1개)
# ──────────────────────────────────────────────────────────────────────────
_pa_lock = threading.Lock()
_pa_instance: Any = None
_pa_users: set[int] = set()


def _pa_get() -> Any:
    """공유 PyAudio 인스턴스 반환 (없으면 생성). pyaudio 미설치면 RuntimeError."""
    global _pa_instance
    if pyaudio is None:
        raise RuntimeError(_NO_PYAUDIO)
    with _pa_lock:
        if _pa_instance is None:
            _pa_instance = pyaudio.PyAudio()
        return _pa_instance


def _pa_release(user_id: int) -> None:
    """사용자(인스턴스) 등록 해제 후, 남은 사용자가 없으면 PortAudio 종료."""
    global _pa_instance
    with _pa_lock:
        _pa_users.discard(user_id)
        if _pa_users or _pa_instance is None:
            return
        try:
            _pa_instance.terminate()
        except Exception:
            pass
        _pa_instance = None


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def _decode_name(name: Any) -> str:
    """PyAudio 장치 이름을 한글 Windows 에서도 제대로 읽는다.

    PyAudio 0.2.14 는 대부분의 장치 이름을 이미 올바른 유니코드로 준다. 다만 두 가지
    깨짐이 남아 있어 순서대로 복구를 시도하고, **복구 결과에 한글이 생겼을 때만** 채택한다
    (멀쩡한 영문 이름을 건드려 망가뜨리지 않기 위함).
      1) latin-1 로 읽힌 cp949 바이트 (구버전 PyAudio)
      2) UTF-8 바이트를 cp949 로 읽은 문자열 (일부 블루투스 핸즈프리 장치명)
    """
    if isinstance(name, bytes):
        for enc in ("cp949", "euc-kr", "utf-8"):
            try:
                return name.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return name.decode("latin-1", "replace")
    if not isinstance(name, str):
        return str(name)
    if _has_hangul(name):
        return name
    for src, dst in (("latin-1", "cp949"), ("cp949", "utf-8")):
        try:
            fixed = name.encode(src).decode(dst)
        except (UnicodeDecodeError, UnicodeEncodeError, LookupError):
            continue
        if _has_hangul(fixed):
            return fixed
    return name


def list_input_devices() -> list[dict]:
    """PC 의 오디오 **입력** 장치 목록.

    디바이스 스캔(routers/device.py) 과 ListDevices() 스텝이 공유한다.
    반환: [{"index", "name", "channels", "rate"}, ...]
    """
    py = _pa_get()
    devices: list[dict] = []
    for i in range(py.get_device_count()):
        try:
            info = py.get_device_info_by_index(i)
        except Exception:
            continue
        if int(info.get("maxInputChannels", 0) or 0) < 1:
            continue
        devices.append({
            "index": i,
            # 일부 드라이버 이름에 개행이 섞여 있어 UI/파일명이 깨진다 — 한 줄로 정리.
            "name": " ".join(_decode_name(info.get("name", "")).split()),
            "channels": int(info.get("maxInputChannels", 0) or 0),
            "rate": int(float(info.get("defaultSampleRate", 0) or 0)),
        })
    return devices


# ──────────────────────────────────────────────────────────────────────────
# 저장 경로 (SerialLogging 과 동일 규약)
# ──────────────────────────────────────────────────────────────────────────
def _get_run_output_dir() -> Optional[Path]:
    try:
        from backend.app.services.playback_service import get_run_output_dir
        return get_run_output_dir()
    except Exception:
        return None


def _cycle_prefix() -> str:
    """재생 중이면 'c{N:03d}_' (N=현재 반복 사이클). 스텝 테스트면 빈 문자열."""
    try:
        from backend.app.services.playback_service import get_current_step_context
        return f"c{get_current_step_context()[1]:03d}_"
    except Exception:
        return ""


def _results_dir() -> Path:
    try:
        from backend.app.services.playback_service import RESULTS_DIR
        return Path(RESULTS_DIR)
    except Exception:
        return Path(__file__).resolve().parent.parent.parent / "results"


def _audio_base_dir() -> tuple[Path, str]:
    """(저장 기준 폴더, 사이클 접두사). 재생 중이면 런 폴더 안, 아니면 Temp_logs."""
    run_dir = _get_run_output_dir()
    if run_dir:
        return run_dir / "logs" / "audio", _cycle_prefix()
    return _results_dir() / "Temp_logs" / "audio", ""


def _reference_dir() -> Path:
    """기준음 저장 폴더 — 런과 무관하게 재사용되어야 하므로 고정 위치."""
    path = _results_dir() / "Audio_Reference"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe_name(text: Any) -> str:
    s = "".join(c if (c.isalnum() or c in "-._") else "_" for c in str(text)).strip("_")
    return s or "audio"


# ──────────────────────────────────────────────────────────────────────────
# 파형 이미지 (numpy + cv2)
# ──────────────────────────────────────────────────────────────────────────
_IMG_W, _IMG_H = 1400, 400
_BG = (255, 255, 255)
_WAVE = (180, 130, 70)      # BGR — steelblue
_THRESH = (0, 0, 255)
_GRID = (220, 220, 220)
_MID = (100, 100, 100)


def _draw_wave_panel(samples, rate: int, threshold: int, title: str,
                     width: int = _IMG_W, height: int = _IMG_H):
    """파형 1개 패널을 BGR ndarray 로 렌더. numpy/cv2 없으면 None."""
    if np is None or cv2 is None:
        return None
    img = np.full((height, width, 3), _BG, dtype=np.uint8)
    n = len(samples)
    if n == 0:
        return img

    for gy in range(1, 5):  # 가로 격자
        img[int(height * gy / 5), :] = _GRID
    duration = n / float(rate or DEFAULT_RATE)
    for sec in range(1, int(duration) + 1):  # 1초 간격 세로 격자
        x = int(sec / duration * width) if duration > 0 else 0
        if 0 < x < width:
            img[:, x] = _GRID

    max_val = max(int(abs(np.max(samples))), int(abs(np.min(samples))), 1)
    mid_y = height // 2
    margin = 20
    # 픽셀당 min/max 구간을 세로 선분으로 — 다운샘플해도 피크가 죽지 않는다.
    idx = np.linspace(0, n, num=width + 1).astype(np.int64)
    for px in range(width):
        s, e = idx[px], max(idx[px + 1], idx[px] + 1)
        if s >= n:
            break
        seg = samples[s:min(e, n)]
        if len(seg) == 0:
            continue
        y_top = mid_y - int(float(np.max(seg)) / max_val * (mid_y - margin))
        y_bot = mid_y - int(float(np.min(seg)) / max_val * (mid_y - margin))
        y_top = max(0, min(height - 1, y_top))
        y_bot = max(0, min(height - 1, y_bot))
        if y_top > y_bot:
            y_top, y_bot = y_bot, y_top
        img[y_top:y_bot + 1, px] = _WAVE

    if 0 < threshold < max_val:  # drop 임계선 (점선)
        ty_pos = max(0, min(height - 1, mid_y - int(threshold / max_val * (mid_y - margin))))
        ty_neg = max(0, min(height - 1, mid_y + int(threshold / max_val * (mid_y - margin))))
        for px in range(0, width, 8):
            img[ty_pos, px:px + 4] = _THRESH
            img[ty_neg, px:px + 4] = _THRESH

    img[mid_y, :] = _MID
    if title:
        cv2.putText(img, title, (10, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (40, 40, 40), 1,
                    cv2.LINE_AA)
    return img


def _save_waveform(samples, rate: int, threshold: int, title: str, path: Path) -> bool:
    panel = _draw_wave_panel(samples, rate, threshold, title)
    if panel is None:
        return False
    try:
        ok, buf = cv2.imencode(".png", panel)
        if not ok:
            return False
        path.write_bytes(buf.tobytes())
        return True
    except Exception as e:
        logger.warning("waveform save failed: %s", e)
        return False


def _save_compare_image(a, rate_a: int, title_a: str, b, rate_b: int, title_b: str,
                        header: str, path: Path) -> bool:
    """두 파형을 위아래로 붙여 비교 이미지 저장."""
    if np is None or cv2 is None:
        return False
    top = _draw_wave_panel(a, rate_a, 0, title_a, height=300)
    bot = _draw_wave_panel(b, rate_b, 0, title_b, height=300)
    if top is None or bot is None:
        return False
    try:
        band = np.full((40, _IMG_W, 3), _BG, dtype=np.uint8)
        cv2.putText(band, header, (10, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 2,
                    cv2.LINE_AA)
        merged = np.vstack([band, top, bot])
        ok, buf = cv2.imencode(".png", merged)
        if not ok:
            return False
        path.write_bytes(buf.tobytes())
        return True
    except Exception as e:
        logger.warning("compare image save failed: %s", e)
        return False


def _read_wav(path: Path):
    """WAV → (int16 샘플 배열/리스트, sample_rate). 실패 시 (None, None)."""
    try:
        wf = wave.open(str(path), "rb")
    except Exception as e:
        logger.warning("WAV open failed: %s (%s)", path, e)
        return None, None
    try:
        n_ch, sw, rate, nframes = (wf.getnchannels(), wf.getsampwidth(),
                                   wf.getframerate(), wf.getnframes())
        raw = wf.readframes(nframes)
    finally:
        wf.close()
    if sw != 2:
        return None, None
    if np is not None:
        samples = np.frombuffer(raw, dtype=np.int16)
        if n_ch == 2:
            samples = samples[0::2]
        return samples, rate
    count = len(raw) // 2
    samples = list(struct.unpack("<{}h".format(count), raw[:count * 2]))
    if n_ch == 2:
        samples = samples[0::2]
    return samples, rate


def _to_bool(v: Any, default: bool = False) -> bool:
    s = str(v).strip().lower()
    if not s:
        return default
    return s in ("1", "true", "yes", "y", "on")


def _to_int(v: Any, default: int) -> int:
    s = str(v).strip()
    if not s:
        return default
    try:
        return int(float(s))
    except ValueError:
        return default


def _to_float_or_none(v: Any) -> Optional[float]:
    s = str(v).strip().lower()
    if not s or s in ("none", "null", "0"):
        return None
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None


class AudioMonitor:
    """마이크 녹음 · 무음(drop) 판정 · 기준음 비교 모듈.

    생성자(디바이스 등록 시 입력):
        device_index: 오디오 입력 장치 번호 (스캔에서 자동으로 채워짐)
        device_name: 장치 이름 (번호가 바뀔 때의 폴백 — 이름으로 재탐색)
        drop_threshold: 스텝에서 생략 시 쓸 기본 무음 임계값
    """

    def __init__(self, device_index: str = "", device_name: str = "",
                 drop_threshold: int = 500, sample_rate: int = DEFAULT_RATE):
        self._device_index = str(device_index).strip()
        self._device_name = str(device_name).strip()
        self._default_threshold = _to_int(drop_threshold, 500)
        self._rate = _to_int(sample_rate, DEFAULT_RATE) or DEFAULT_RATE
        self._resolved_index: Optional[int] = None
        self._resolved_name = ""
        self._connected = False
        self._sessions: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 장치 해석
    # ------------------------------------------------------------------
    def _find_device(self, spec: str) -> tuple[Optional[int], str]:
        """번호 → 정확한 이름 → 부분 일치 순으로 입력 장치를 찾는다.

        (원본 _find_device 와 동일한 우선순위. 부분 일치가 여러 개면 첫 번째를 쓰고 경고.)
        """
        devices = list_input_devices()
        spec = str(spec or "").strip()
        if not spec:
            return None, ""

        if spec.isdigit():
            idx = int(spec)
            for d in devices:
                if d["index"] == idx:
                    return idx, d["name"]
            return None, ""

        low = spec.lower()
        for d in devices:  # 정확히 일치
            if d["name"].strip().lower() == low:
                return d["index"], d["name"]
        matches = [d for d in devices if low in d["name"].lower()]  # 부분 일치
        if len(matches) == 1:
            return matches[0]["index"], matches[0]["name"]
        if len(matches) > 1:
            logger.warning("Audio device '%s' matches %d devices — using [%d] %s",
                           spec, len(matches), matches[0]["index"], matches[0]["name"])
            return matches[0]["index"], matches[0]["name"]
        return None, ""

    def _resolve_target(self, device: str = "") -> tuple[Optional[int], str]:
        """스텝의 device 인자 > 등록된 번호 > 등록된 이름 순으로 대상 장치 결정.

        번호를 먼저 보되, 그 번호가 사라졌으면(USB 재삽입 등으로 인덱스 변동) 이름으로
        재탐색한다 — 등록 당시 인덱스에 못 박히지 않게 하기 위함.
        """
        if str(device).strip():
            return self._find_device(str(device).strip())
        if self._device_index:
            idx, name = self._find_device(self._device_index)
            if idx is not None:
                # 이름까지 등록돼 있으면 같은 장치인지 확인 — 다르면 이름 우선.
                if not self._device_name or self._device_name.lower() in name.lower():
                    return idx, name
        if self._device_name:
            return self._find_device(self._device_name)
        return None, ""

    # ------------------------------------------------------------------
    # 연결 lifecycle (스텝 목록에는 노출되지 않음)
    # ------------------------------------------------------------------
    def Connect(self) -> str:
        """마이크 존재/열림 여부를 확인한다 (짧게 열었다 닫는 probe)."""
        if pyaudio is None:
            self._connected = False
            return f"ERROR: {_NO_PYAUDIO}"
        try:
            idx, name = self._resolve_target()
        except Exception as e:
            self._connected = False
            return f"ERROR: 오디오 장치 열거 실패 — {e}"
        if idx is None:
            self._connected = False
            return (f"ERROR: 오디오 입력 장치를 찾을 수 없습니다 "
                    f"(index='{self._device_index}', name='{self._device_name}')")
        try:
            py = _pa_get()
            stream = py.open(format=pyaudio.paInt16, channels=CHANNELS, rate=self._rate,
                             input=True, input_device_index=idx,
                             frames_per_buffer=DEFAULT_CHUNK)
            stream.stop_stream()
            stream.close()
        except Exception as e:
            self._connected = False
            return f"ERROR: {_open_error(idx, name, e)}"
        with _pa_lock:
            _pa_users.add(id(self))
        self._resolved_index, self._resolved_name = idx, name
        self._connected = True
        logger.info("AudioMonitor connected: [%d] %s (%dHz)", idx, name, self._rate)
        return f"ok: audio input [{idx}] {name}"

    def IsConnected(self) -> bool:
        return bool(self._connected)

    def Disconnect(self) -> str:
        """진행 중인 모든 세션을 정리(저장)하고 장치를 놓는다."""
        stopped = []
        for name, session in list(self._sessions.items()):
            # 이미 StopMonitor 된 세션(stream 반납 완료)은 결과 보존용으로 남겨둔다 —
            # 여기서 다시 부르면 같은 판정이 중복 로깅될 뿐이다.
            if session.get("stream") is None:
                continue
            try:
                self.StopMonitor(name)
                stopped.append(name)
            except Exception as e:
                logger.warning("AudioMonitor stop '%s' on disconnect failed: %s", name, e)
        self._connected = False
        _pa_release(id(self))
        return f"ok: disconnected (stopped={stopped})" if stopped else "ok: disconnected"

    # ------------------------------------------------------------------
    # 스텝 함수
    # ------------------------------------------------------------------
    def ListDevices(self) -> str:
        """PC 에 연결된 오디오 입력 장치(마이크) 목록을 반환한다."""
        try:
            devices = list_input_devices()
        except Exception as e:
            return f"FAIL: 오디오 장치 열거 실패 — {e}"
        if not devices:
            return "FAIL: 오디오 입력 장치가 없습니다"
        lines = [f"[{d['index']}] {d['name']} ({d['channels']}ch, {d['rate']}Hz)"
                 for d in devices]
        return "ok: " + str(len(devices)) + " input device(s)\n" + "\n".join(lines)

    def StartMonitor(self, session_name: str, drop_threshold: int = 0,
                     judge_mode: str = "pass", judge_count: int = 50,
                     duration: float = 0, device: str = "") -> str:
        """오디오 무음(drop) 모니터링을 시작한다 (백그라운드 녹음).

        같은 session_name 이 이미 돌고 있으면 이전 세션을 정리하고 새로 시작한다.
        """
        session_name = str(session_name or "").strip()
        if not session_name:
            return "FAIL: session_name 이 비어 있습니다"
        if pyaudio is None:
            return f"FAIL: {_NO_PYAUDIO}"

        threshold = _to_int(drop_threshold, 0) or self._default_threshold
        mode = str(judge_mode or "pass").strip().lower()
        if mode not in ("pass", "fail"):
            logger.warning("Invalid judge_mode '%s' — using 'pass'", judge_mode)
            mode = "pass"
        count = _to_int(judge_count, 50)
        dur = _to_float_or_none(duration)

        try:
            idx, name = self._resolve_target(device)
        except Exception as e:
            return f"FAIL: 오디오 장치 열거 실패 — {e}"
        if idx is None:
            return (f"FAIL: 오디오 장치를 찾을 수 없습니다 "
                    f"(device='{device or self._device_index or self._device_name}')")

        with self._lock:
            if session_name in self._sessions and self._sessions[session_name].get("check"):
                logger.info("AudioMonitor '%s' already running — restarting", session_name)
                self._sessions[session_name]["check"] = False
                th = self._threads.get(session_name)
                if th:
                    th.join(timeout=5)

        # 스트림은 호출 스레드에서 연다 — 녹음 스레드에서 열면 PortAudio 가 불안정하다(원본 주석).
        try:
            py = _pa_get()
            stream = py.open(format=pyaudio.paInt16, channels=CHANNELS, rate=self._rate,
                             input=True, input_device_index=idx,
                             frames_per_buffer=DEFAULT_CHUNK)
        except Exception as e:
            return f"FAIL: {_open_error(idx, name, e)}"
        with _pa_lock:
            _pa_users.add(id(self))

        session = {
            "check": True,
            "result": "pass",
            "stream": stream,
            "threshold": threshold,
            "judge_mode": mode,
            "judge_count": count,
            "duration": dur,
            "device": f"[{idx}] {name}",
        }
        with self._lock:
            self._sessions[session_name] = session
        try:
            th = threading.Thread(target=self._record_loop, args=(session_name, session),
                                  name=f"audio-{session_name}", daemon=True)
            th.start()
            with self._lock:
                self._threads[session_name] = th
        except Exception as e:
            session["check"] = False
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            return f"FAIL: 녹음 스레드 시작 실패 — {e}"

        logger.info("AudioMonitor start: session=%s device=[%d] %s threshold=%d judge=%s:%d duration=%s",
                    session_name, idx, name, threshold, mode, count, dur)
        return (f"ok: '{session_name}' 모니터링 시작 (device=[{idx}] {name}, "
                f"threshold={threshold}, judge={mode}:{count}"
                + (f", duration={dur}s)" if dur else ")"))

    def StopMonitor(self, session_name: str = "") -> str:
        """모니터링을 종료하고 PASS/FAIL 판정 결과를 반환한다.

        duration 으로 이미 자동 종료된 세션도 결과를 그대로 돌려준다.
        session_name 을 비우면 진행 중인 세션이 하나일 때 그 세션을 종료한다.
        """
        session_name = str(session_name or "").strip()
        if not session_name:
            with self._lock:
                names = list(self._sessions.keys())
            if len(names) == 1:
                session_name = names[0]
            elif not names:
                return "FAIL: 진행 중인 오디오 세션이 없습니다"
            else:
                return f"FAIL: session_name 을 지정하세요 (진행 중: {', '.join(names)})"

        with self._lock:
            session = self._sessions.get(session_name)
            thread = self._threads.get(session_name)
        if session is None:
            return f"FAIL: '{session_name}' 세션이 없습니다 (StartMonitor 먼저 호출)"

        session["check"] = False
        if thread is not None:
            thread.join(timeout=30)
            if thread.is_alive():
                logger.warning("AudioMonitor '%s' record thread did not stop in 30s", session_name)

        stream = session.pop("stream", None)
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

        with self._lock:
            self._threads.pop(session_name, None)

        result = str(session.get("result", "error"))
        detail = (f"session={session_name} pass={session.get('pass_count', 0)} "
                  f"drop={session.get('drop_count', 0)} "
                  f"max_peak={int(session.get('maxpeak', 0) or 0)} "
                  f"judge={session.get('judge_mode')}:{session.get('judge_count')} "
                  f"wav={session.get('wav_path', '-')}")
        logger.info("AudioMonitor stop: %s → %s", detail, result.upper())

        if session.get("error"):
            return f"FAIL: {session['error']} ({detail})"
        if result == "pass":
            return f"ok: PASS ({detail})"
        return f"FAIL: 오디오 판정 실패 ({detail})"

    def SaveReference(self, reference_name: str, duration: float = 10,
                      device: str = "") -> str:
        """비교 기준(reference) 음원을 duration 초 동안 녹음해 저장한다.

        저장 위치는 런과 무관한 고정 폴더(results/Audio_Reference/<이름>.wav) — 이후 어떤
        재생에서도 CompareWithReference 로 참조할 수 있다.
        """
        reference_name = str(reference_name or "").strip()
        if not reference_name:
            return "FAIL: reference_name 이 비어 있습니다"
        if pyaudio is None:
            return f"FAIL: {_NO_PYAUDIO}"
        dur = _to_float_or_none(duration)
        if dur is None:
            return "FAIL: duration(초)을 0보다 큰 값으로 지정하세요"

        try:
            idx, name = self._resolve_target(device)
        except Exception as e:
            return f"FAIL: 오디오 장치 열거 실패 — {e}"
        if idx is None:
            return f"FAIL: 오디오 장치를 찾을 수 없습니다 (device='{device}')"

        try:
            py = _pa_get()
            stream = py.open(format=pyaudio.paInt16, channels=CHANNELS, rate=self._rate,
                             input=True, input_device_index=idx,
                             frames_per_buffer=DEFAULT_CHUNK)
        except Exception as e:
            return f"FAIL: {_open_error(idx, name, e)}"
        with _pa_lock:
            _pa_users.add(id(self))

        frames: list[bytes] = []
        started = time.monotonic()
        try:
            while time.monotonic() - started < dur:
                frames.append(stream.read(DEFAULT_CHUNK, exception_on_overflow=False))
        except Exception as e:
            return f"FAIL: 녹음 실패 — {e}"
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

        wav_path = _reference_dir() / f"{_safe_name(reference_name)}.wav"
        try:
            self._write_wav(wav_path, b"".join(frames))
        except Exception as e:
            return f"FAIL: WAV 저장 실패 — {e}"

        if np is not None:
            _save_waveform(np.frombuffer(b"".join(frames), dtype=np.int16), self._rate, 0,
                           f"Reference: {reference_name}",
                           wav_path.with_name(wav_path.stem + "_waveform.png"))
        logger.info("AudioMonitor reference saved: %s (%.1fs, device=[%d] %s)",
                    wav_path, dur, idx, name)
        return f"ok: 기준음 저장 ({reference_name}, {dur:g}s) → {wav_path}"

    def CompareWithReference(self, session_name: str, reference_name: str) -> str:
        """StopMonitor 로 저장된 녹음을 기준음과 비교해 PASS/FAIL 을 판정한다.

        판정: 상관계수 > 0.5 **그리고** RMS dB 차 < 5.0 이면 PASS.
        """
        session_name = str(session_name or "").strip()
        reference_name = str(reference_name or "").strip()
        if not session_name or not reference_name:
            return "FAIL: session_name / reference_name 을 모두 지정하세요"

        with self._lock:
            session = self._sessions.get(session_name)
        if not session or not session.get("wav_path"):
            return f"FAIL: '{session_name}' 의 녹음 결과가 없습니다 (StopMonitor 먼저 호출)"
        wav1 = Path(session["wav_path"])
        if not wav1.exists():
            return f"FAIL: 녹음 파일을 찾을 수 없습니다 — {wav1}"
        wav2 = _reference_dir() / f"{_safe_name(reference_name)}.wav"
        if not wav2.exists():
            return (f"FAIL: 기준음 '{reference_name}' 이 없습니다 "
                    f"(SaveReference 먼저 실행) — {wav2}")

        s1, rate1 = _read_wav(wav1)
        s2, rate2 = _read_wav(wav2)
        if s1 is None or s2 is None:
            return "FAIL: WAV 읽기 실패 (16-bit PCM 이어야 합니다)"

        if np is not None:
            a1 = np.asarray(s1, dtype=np.float64)
            a2 = np.asarray(s2, dtype=np.float64)
            peak1, peak2 = float(np.mean(np.abs(a1)) * 2), float(np.mean(np.abs(a2)) * 2)
            rms1 = float(np.sqrt(np.mean(a1 ** 2)))
            rms2 = float(np.sqrt(np.mean(a2 ** 2)))
            n = min(len(a1), len(a2))
            correlation = 0.0
            if n > 1:
                x, y = a1[:n] / 32768.0, a2[:n] / 32768.0
                if float(np.std(x)) > 0 and float(np.std(y)) > 0:
                    correlation = float(np.corrcoef(x, y)[0, 1])
        else:
            peak1 = sum(abs(s) for s in s1) / len(s1) * 2
            peak2 = sum(abs(s) for s in s2) / len(s2) * 2
            rms1 = math.sqrt(sum(s * s for s in s1) / len(s1))
            rms2 = math.sqrt(sum(s * s for s in s2) / len(s2))
            correlation = 0.0  # numpy 없이는 상관계수 계산을 생략 (판정은 FAIL 쪽으로 보수적)

        db1 = 20.0 * math.log10(rms1 / 32768.0) if rms1 > 0 else -120.0
        db2 = 20.0 * math.log10(rms2 / 32768.0) if rms2 > 0 else -120.0
        db_diff = abs(db1 - db2)
        passed = correlation > COMPARE_MIN_CORRELATION and db_diff < COMPARE_MAX_DB_DIFF

        out_dir = wav1.parent
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{ts}_compare_{_safe_name(session_name)}_{_safe_name(reference_name)}"
        # cv2.putText 는 ASCII 만 그린다 — 화살표/한글을 쓰면 '???' 로 찍힌다.
        header = (f"corr={correlation:.4f}  dB_diff={db_diff:.2f}  "
                  f"-> {'PASS' if passed else 'FAIL'}")
        img_path = out_dir / f"{stem}.png"
        if np is not None:
            _save_compare_image(
                np.asarray(s1, dtype=np.int16), rate1 or self._rate,
                f"{session_name}  peak={peak1:.0f}  {db1:.1f}dB",
                np.asarray(s2, dtype=np.int16), rate2 or self._rate,
                f"Reference({reference_name})  peak={peak2:.0f}  {db2:.1f}dB",
                header, img_path)
        try:
            (out_dir / f"{stem}.txt").write_text(
                "Audio Compare Report\n"
                "====================\n"
                f"{session_name} : peak={peak1:.0f} rms_dB={db1:.1f} wav={wav1}\n"
                f"Reference({reference_name}) : peak={peak2:.0f} rms_dB={db2:.1f} wav={wav2}\n"
                f"correlation = {correlation:.4f} (기준 > {COMPARE_MIN_CORRELATION})\n"
                f"dB diff = {db_diff:.2f} (기준 < {COMPARE_MAX_DB_DIFF})\n"
                f"result = {'PASS' if passed else 'FAIL'}\n",
                encoding="utf-8")
        except Exception as e:
            logger.warning("compare report save failed: %s", e)

        detail = (f"corr={correlation:.4f} dB_diff={db_diff:.2f} "
                  f"session={session_name} ref={reference_name} img={img_path}")
        logger.info("AudioMonitor compare: %s → %s", detail, "PASS" if passed else "FAIL")
        if passed:
            return f"ok: PASS ({detail})"
        reasons = []
        if correlation <= COMPARE_MIN_CORRELATION:
            reasons.append(f"상관계수 낮음({correlation:.4f})")
        if db_diff >= COMPARE_MAX_DB_DIFF:
            reasons.append(f"음량 차 큼({db_diff:.2f}dB)")
        return f"FAIL: 기준음과 불일치 — {', '.join(reasons)} ({detail})"

    # ------------------------------------------------------------------
    # 내부 — 녹음 루프
    # ------------------------------------------------------------------
    def _write_wav(self, path: Path, audio_bytes: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        wf = wave.open(str(path), "wb")
        try:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)  # paInt16
            wf.setframerate(self._rate)
            wf.writeframes(audio_bytes)
        finally:
            wf.close()

    def _record_loop(self, session_name: str, session: dict) -> None:
        """청크 단위로 읽어 peak 를 기록하고, 종료 시 wav/txt/png 를 저장한다.

        판정은 원본과 동일: judge_mode='pass' → pass_count >= judge_count 면 PASS,
        judge_mode='fail' → drop_count >= judge_count 면 FAIL.
        """
        stream = session.get("stream")
        threshold = int(session["threshold"])
        duration = session.get("duration")
        started = time.monotonic()

        base_dir, cyc = _audio_base_dir()
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        work_dir = base_dir / f"{cyc}{_safe_name(session_name)}_{ts}"
        try:
            work_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            session["error"] = f"결과 폴더 생성 실패: {e}"
            session["result"] = "error"
            session["check"] = False
            return

        txt_path = work_dir / f"{_safe_name(session_name)}.txt"
        drop_count = pass_count = 0
        minpeak, maxpeak = float("inf"), 0.0
        frames: list[bytes] = []

        try:
            with open(txt_path, "w", encoding="utf-8") as f:
                while session.get("check"):
                    if duration is not None and (time.monotonic() - started) >= duration:
                        session["check"] = False
                        break
                    data = stream.read(DEFAULT_CHUNK, exception_on_overflow=False)
                    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                    if np is not None:
                        peak = float(np.mean(np.abs(np.frombuffer(data, dtype=np.int16))) * 2)
                    else:
                        n = len(data) // 2
                        vals = struct.unpack("<{}h".format(n), data[:n * 2])
                        peak = float(sum(abs(v) for v in vals) / max(n, 1)) * 2
                    frames.append(data)
                    minpeak, maxpeak = min(minpeak, peak), max(maxpeak, peak)
                    if peak < threshold:
                        drop_count += 1
                        tag = "Audio Drops"
                    else:
                        pass_count += 1
                        tag = "Audio Pass "
                    f.write(f"{tag}: {stamp} {int(peak):05d} "
                            f"{'#' * int(50 * peak / 2 ** 16)}\n")
        except Exception as e:
            session["error"] = f"오디오 읽기 실패: {e}"
            session["result"] = "error"
            logger.warning("AudioMonitor '%s' read error: %s", session_name, e)

        audio_bytes = b"".join(frames)
        wav_path = work_dir / f"{_safe_name(session_name)}.wav"
        try:
            self._write_wav(wav_path, audio_bytes)
        except Exception as e:
            session["error"] = session.get("error") or f"WAV 저장 실패: {e}"
            logger.warning("AudioMonitor '%s' wav save failed: %s", session_name, e)

        img_path = work_dir / f"{_safe_name(session_name)}_waveform.png"
        if np is not None and audio_bytes:
            if not _save_waveform(np.frombuffer(audio_bytes, dtype=np.int16), self._rate,
                                  threshold, f"{session_name} (threshold={threshold})",
                                  img_path):
                img_path = Path("")
        else:
            img_path = Path("")

        if not session.get("error"):
            if session["judge_mode"] == "pass":
                session["result"] = "pass" if pass_count >= session["judge_count"] else "fail"
            else:
                session["result"] = "fail" if drop_count >= session["judge_count"] else "pass"

        try:
            with open(txt_path, "a", encoding="utf-8") as f:
                f.write("====================\n")
                f.write(f"Device       : {session.get('device', '-')}\n")
                f.write(f"Judgement    : {session['judge_mode']}:{session['judge_count']}\n")
                f.write(f"Pass count   : {pass_count}\n")
                f.write(f"Drop count   : {drop_count}\n")
                f.write(f"Max peak     : {int(maxpeak)}\n")
                f.write(f"Min peak     : {int(minpeak) if minpeak != float('inf') else 0}\n")
                f.write(f"Test result  : {str(session.get('result', '')).upper()}\n")
                f.write(f"WAV          : {wav_path}\n")
                if str(img_path):
                    f.write(f"Waveform     : {img_path}\n")
        except Exception as e:
            logger.warning("AudioMonitor '%s' summary write failed: %s", session_name, e)

        # 결과를 폴더명에 남긴다 (원본 동작 — _pass / _fail 접미사)
        final_dir = work_dir
        try:
            renamed = work_dir.with_name(work_dir.name + f"_{session.get('result', 'error')}")
            work_dir.rename(renamed)
            final_dir = renamed
            wav_path = renamed / wav_path.name
            txt_path = renamed / txt_path.name
            if str(img_path):
                img_path = renamed / img_path.name
        except Exception:
            pass

        session["wav_path"] = str(wav_path)
        session["txt_path"] = str(txt_path)
        session["img_path"] = str(img_path) if str(img_path) else ""
        session["dir"] = str(final_dir)
        session["drop_count"] = drop_count
        session["pass_count"] = pass_count
        session["maxpeak"] = maxpeak
        session["minpeak"] = 0 if minpeak == float("inf") else minpeak
        logger.info("AudioMonitor '%s' done: result=%s pass=%d drop=%d max=%d dir=%s",
                    session_name, str(session.get("result")).upper(), pass_count,
                    drop_count, int(maxpeak), final_dir)
