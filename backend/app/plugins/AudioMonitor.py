"""AudioMonitor — 마이크(오디오 입력) 녹음·무음(drop) 판정·기준음 비교 플러그인.

Reference/audio_record&compare/audio_monitor.py (standalone AUDIO_* 함수 모음) 를
ReplayKit 모듈 규약으로 이식했다. 판정 알고리즘(청크 평균 peak → drop/pass 카운트,
correlation + dB 차 비교)은 원본 그대로이고, 아래만 바꿨다.

  - print → logger, 반환값 → ReplayKit 계약("ok: …" / "FAIL: …").
    ("FAIL:" 접두사를 playback_service 가 스텝 실패로 처리한다)
  - **session_name** 으로 측정 상황을 구분한다 (예: "usb", "bt"). session_name 은
    폴더명 및 세션 식별 키가 된다. StartMonitor(session_name="usb") → StopMonitor(session_name="usb").
    장치는 __init__ 의 device_index/device_name 또는 첫 번째 입력 장치를 자동 사용한다.
  - 저장 경로를 ReplayKit 규약으로: 재생 중이면 {run_dir}/logs/audio/, 스텝 테스트면
    results/Temp_logs/audio/. 기준음(reference)만은 런과 무관하게 재사용해야 하므로
    results/Audio_Reference/ 에 고정 저장한다.
  - 파형 이미지는 matplotlib 대신 numpy+cv2 로 그린다(이미 하드 의존이라 추가 설치 불필요).

설계
----
- ReplayKit 은 호출마다 새 인스턴스를 생성하므로, 세션 상태는 클래스 레벨(class-level)의
  `_sessions`/`_threads` 에 공유한다. 세션 식별은 `session_name` 파라미터로 한다.
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

# 비교 판정 임계 (audiocomparetest 참고): NCC 유사도 >= threshold 이면 PASS
# 기본 threshold 0.85 (같은 장비·환경에서 녹음 권장값)
COMPARE_DEFAULT_THRESHOLD = 0.85
COMPARE_MIN_OVERLAP_RATIO = 0.5


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
    """사용자(인스턴스) 등록 해제 후, 남은 사용자가 없으면 PortAudio 종료.

    terminate() 는 내부적으로 pending 작업이 끝날 때까지 수 초간 블로킹할 수 있으므로
    백그라운드 스레드에서 실행한다 (이벤트 루프를 막지 않기 위해).
    """
    global _pa_instance
    with _pa_lock:
        _pa_users.discard(user_id)
        if _pa_users or _pa_instance is None:
            return
        inst = _pa_instance
        _pa_instance = None
    if inst is not None:
        threading.Thread(target=_pa_terminate_async, args=(inst,), daemon=True).start()


def _pa_terminate_async(inst) -> None:
    """PortAudio terminate 를 백그라운드에서 실행한다."""
    try:
        inst.terminate()
    except Exception:
        pass


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


# ──────────────────────────────────────────────────────────────────────────
# NCC 기반 비교 헬퍼 (audiocomparetest/AudioCompareTest.py 참고, scipy 없이 numpy 만 사용)
# ──────────────────────────────────────────────────────────────────────────
def _resample_poly(x: np.ndarray, up: int, down: int) -> np.ndarray:
    """다항식 리샘플링 (scipy.signal.resample_poly 의 간단한 대체).

    up/down 비율로 선형 보간 기반 리샘플링을 수행한다. 정확도는 scipy 보다
    낮지만 샘플레이트 차이 보정에는 충분하다.
    """
    if np is None:
        return x
    if up == down:
        return x
    n = len(x)
    if n < 2:
        return x
    # 원본 인덱스 (0 ~ n-1) 를 새 길이에 매핑
    new_len = int(round(n * up / down))
    if new_len < 1:
        return x[:1]
    old_idx = np.linspace(0, n - 1, num=new_len)
    i0 = np.floor(old_idx).astype(np.int64)
    i1 = np.minimum(i0 + 1, n - 1)
    frac = (old_idx - i0).astype(np.float64)
    return x[i0] * (1.0 - frac) + x[i1] * frac


def _trim_silence_ncc(signal: np.ndarray, sr: int,
                      threshold_db: float = -40.0, frame_ms: int = 10) -> np.ndarray:
    """앞뒤 무음 구간을 제거 (AudioCompareTest._trim_silence 와 동일)."""
    if np is None:
        return signal
    frame_len = int(sr * frame_ms / 1000)
    if frame_len < 1 or len(signal) < frame_len:
        return signal
    threshold_amp = 10 ** (threshold_db / 20)
    n_frames = len(signal) // frame_len
    frames = signal[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    voiced = np.where(rms > threshold_amp)[0]
    if len(voiced) == 0:
        return signal
    start = voiced[0] * frame_len
    end = (voiced[-1] + 1) * frame_len
    return signal[start:end]


def _overlap_indices_ncc(len_ref: int, len_rec: int, lag: int) -> tuple[int, int, int, int]:
    """lag 에 따른 겹치는 구간의 (ref_start, ref_end, rec_start, rec_end)."""
    if lag >= 0:
        s_ref = lag
        s_rec = 0
    else:
        s_ref = 0
        s_rec = -lag
    overlap = min(len_ref - s_ref, len_rec - s_rec)
    return s_ref, s_ref + overlap, s_rec, s_rec + overlap


def _find_best_offset_ncc(ref: np.ndarray, rec: np.ndarray) -> int:
    """ref 기준으로 rec 가 몇 샘플 밀렸는지 반환 (cross-correlation).

    큰 파일 성능을 위해 다운샘플 후 coarse 탐색 → 원본에서 fine 탐색.
    """
    if np is None:
        return 0
    if len(ref) < 2 or len(rec) < 2:
        return 0
    # ── coarse pass (다운샘플) ──
    COARSE_SR = 8000
    factor = max(1, min(len(ref), len(rec)) // (COARSE_SR * 120))
    ref_ds = ref[::factor]
    rec_ds = rec[::factor]
    if len(ref_ds) < 2 or len(rec_ds) < 2:
        return 0
    # numpy 기반 cross-correlation (full mode)
    corr = np.correlate(ref_ds, rec_ds, mode="full")
    coarse_lag = int(np.argmax(corr)) - (len(rec_ds) - 1)
    coarse_lag *= factor

    # ── fine pass (coarse 주변 ±factor 범위) ──
    search = factor * 2
    best_lag = coarse_lag
    best_val = -np.inf
    for delta in range(-search, search + 1):
        lag = coarse_lag + delta
        s_ref, e_ref, s_rec, e_rec = _overlap_indices_ncc(len(ref), len(rec), lag)
        if e_ref - s_ref < 1:
            continue
        a = ref[s_ref:e_ref]
        b = rec[s_rec:e_rec]
        val = float(np.dot(a, b))
        if val > best_val:
            best_val = val
            best_lag = lag
    return best_lag


def _ncc(a: np.ndarray, b: np.ndarray) -> float:
    """Normalized Cross-Correlation: -1 ~ 1 (DC 제거 + 크기 정규화)."""
    if np is None:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom < 1e-12:
        return 0.0
    return float(np.dot(a, b) / denom)


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

    
    # ReplayKit 호출 시 매 호출마다 새 인스턴스가 생성되므로,
    # 세션 맵/스레드 맵을 **클래스 속성(class-level)**으로 공유한다.
    _sessions: dict[str, dict] = {}
    _threads: dict[str, threading.Thread] = {}
    _lock = threading.Lock()
    # 이번 실행(run)에서 이미 사용된 장치 인덱스 집합.
    # 여러 AudioMonitor 인스턴스가 device/session_name 을 지정하지 않아도
    # 서로 다른 마이크를 자동으로 배정받도록 추적한다.
    _used_devices: set[int] = set()

    def __init__(self, device_index: str = "", device_name: str = "",
                 drop_threshold: int = 500, sample_rate: int = DEFAULT_RATE):
        self._device_index = str(device_index).strip()
        self._device_name = str(device_name).strip()
        self._default_threshold = _to_int(drop_threshold, 500)
        self._rate = _to_int(sample_rate, DEFAULT_RATE) or DEFAULT_RATE
        self._resolved_index: Optional[int] = None
        self._resolved_name = ""
        self._connected = False
        # 클래스 레벨 장치 레지스트리: Connect()에서 저장한 장치 정보를 새 인스턴스가 공유.
        # 키는 id(self)가 아니라 **장치 이름**을 사용한다. ReplayKit은 호출마다 새 인스턴스를
        # 만들므로 id(self)는 매번 달라지지만, 장치 이름은 안정적이어서 새 인스턴스가
        # 이 레지스트리에서 자신의 장치를 찾을 수 있다.
        if not hasattr(type(self), "_device_registry"):
            type(self)._device_registry: dict[str, tuple[int, str]] = {}
        logger.info("AudioMonitor __init__: id=%s device_name='%s' device_index='%s' "
                    "sessions=%s threads=%s registry=%s",
                    id(self), self._device_name, self._device_index,
                    list(type(self)._sessions.keys()), list(type(self)._threads.keys()),
                    list(type(self)._device_registry.keys()))

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

    def _resolve_target(self, hint: str = "") -> tuple[Optional[int], str]:
        """대상 오디오 입력 장치를 결정한다.

        우선순위: 등록된 device_index > 등록된 device_name > hint(세션명 등) > 클래스 레벨 장치 레지스트리 > 자동 할당 > 첫 번째 입력 장치.
        Connect()에서 이미 스캔된 device_index/device_name이 __init__에 전달되므로, 여기서 다시 지정할 필요가 없다.
        ReplayKit은 호출마다 새 인스턴스를 만들지만, 클래스 레벨 device registry는 장치 이름을 키로 하여 공유되므로 새 인스턴스도 자신의 장치를 찾을 수 있다.

        hint 가 비어 있으면 **이미 활성 세션에서 사용 중이 아닌 장치**를 자동으로 선택한다.
        이렇게 하면 여러 AudioMonitor 인스턴스가 device/session_name 을 지정하지 않아도
        서로 다른 마이크를 자동으로 할당받는다 (예: AudioMonitor_1 → Cluster, AudioMonitor_2 → IVI).
        """
        # 1) 등록된 device_index (__init__에서 Connect() 스캔으로 채워짐) - 최우선
        if self._device_index:
            idx, name = self._find_device(self._device_index)
            if idx is not None:
                if not self._device_name or self._device_name.lower() in name.lower():
                    return idx, name
        # 2) 등록된 device_name (__init__에서 Connect() 스캔으로 채워짐)
        if self._device_name:
            return self._find_device(self._device_name)
        # 3) hint (예: session_name="Cluster")를 장치명으로 시도 - 세션명이 장치명과
        # 일치하면 해당 장치를 우선 사용한다. (여러 장치가 있을 때 세션마다 다른
        # 장치를 녹음해야 하므로 레지스트리보다 우선한다)
        if hint:
            idx, name = self._find_device(hint)
            if idx is not None:
                return idx, name
        # 4) 클래스 레벨 device registry에서 장치 이름으로 조회 (새 인스턴스가 공유)
        registry = getattr(type(self), "_device_registry", {})
        if registry:
            # hint가 장치 이름과 일치하면 그 장치를 사용 (여러 장치가 있을 때 정확한 선택)
            if hint:
                for reg_name, (reg_idx, reg_full) in registry.items():
                    if hint.lower() in reg_name.lower() or hint.lower() in reg_full.lower():
                        found = self._find_device(reg_name)
                        if found[0] is not None:
                            return found[0], found[1]
        # 5) hint 가 비어 있으면 **이번 실행에서 아직 사용하지 않은 장치를 자동 할당**한다.
        #    여러 AudioMonitor 인스턴스가 device/session_name 을 지정하지 않아도
        #    서로 다른 마이크를 자동으로 배정받도록 한다.
        #    _used_devices 는 클래스 레벨 집합으로, 세션이 종료된 후에도 유지되므로
        #    AudioMonitor_1 → Cluster, AudioMonitor_2 → IVI 처럼 서로 다른 장치를 보장한다.
        #    **중요**: Microsoft Sound Mapper - Input(인덱스 0) 같은 기본 장치를
        #    선택하지 않도록, **Connect() 로 등록된 장치(_device_registry)를 우선** 사용한다.
        #    _device_registry 에 등록된 장치가 없으면 전체 입력 장치 목록으로 폴백한다.
        devices = list_input_devices()
        if not devices:
            return None, ""
        # 이미 활성 세션에서 사용 중인 장치 인덱스 집합
        with self._lock:
            used_indices = {
                s.get("device_index")
                for s in self._sessions.values()
                if s.get("device_index") is not None
            }
        # 이번 실행에서 이미 사용된 장치 인덱스 (클래스 레벨, 세션 종료 후에도 유지)
        used_indices.update(getattr(type(self), "_used_devices", set()))

        # 5a) Connect() 로 등록된 장치(_device_registry)를 우선 사용한다.
        #     Microsoft Sound Mapper - Input(인덱스 0) 같은 기본 장치를 건너뛰고
        #     실제 연결된 USB 마이크(Cluster, IVI 등)만 선택한다.
        registry = getattr(type(self), "_device_registry", {})
        if registry:
            # 레지스트리의 장치 이름을 실제 장치 목록에서 찾아 사용하지 않은 것 중 첫 번째 선택
            for reg_name, (reg_idx, reg_full) in registry.items():
                if reg_idx in used_indices:
                    continue
                found = self._find_device(reg_name)
                if found[0] is not None and found[0] not in used_indices:
                    return found[0], found[1]
            # 모든 등록 장치가 사용되었으면 첫 번째 등록 장치로 폴백
            first_reg = next(iter(registry.items()))
            found = self._find_device(first_reg[0])
            if found[0] is not None:
                return found[0], found[1]

        # 5b) 레지스트리가 비어 있으면 전체 입력 장치 목록에서 사용하지 않은 첫 번째 장치 선택
        for d in devices:
            if d["index"] not in used_indices:
                return d["index"], d["name"]
        # 모든 장치가 사용되었으면 첫 번째 장치로 폴백 (하위 호환)
        return devices[0]["index"], devices[0]["name"]

    def _session_key(self, device_name: str = "") -> str:
        """이 인스턴스의 세션 키를 결정한다.

        등록된 device_name > device_index > 클래스 레벨 장치 레지스트리 > 기본값 "audio".
        ReplayKit은 호출마다 새 인스턴스를 만들지만, 클래스 레벨 device registry는 장치 이름을
        키로 하여 공유되므로 새 인스턴스도 자신의 장치를 찾을 수 있다.

        device_name 이 주어지면 (StartMonitor 에서 _resolve_target 으로 선택된 장치) 그 장치
        이름을 세션 키로 사용한다. 이렇게 하면 여러 인스턴스가 device/session_name 을
        지정하지 않아도 각자 다른 장치를 자동 할당받아 서로 다른 세션 키를 갖게 된다.
        """
        if device_name:
            return _safe_name(device_name)
        if self._device_name:
            return self._device_name
        if self._device_index:
            return self._device_index
        # 클래스 레벨 장치 레지스트리에서 첫 번째 장치 이름을 세션 키로 사용 (새 인스턴스가 공유)
        registry = getattr(type(self), "_device_registry", {})
        if registry:
            return list(registry.keys())[0]  # 장치 이름이 키이므로 그대로 사용
        return "audio"

    def _find_session_name(self) -> Optional[str]:
        """이 인스턴스의 장치에 해당하는 세션 이름을 찾는다.

        ReplayKit 은 호출마다 새 인스턴스를 만들므로 user_id(id(self)) 는 매번 달라진다.
        따라서 장치 이름/인덱스로 세션을 매칭한다. __init__ 에 device 정보가 없으면
        (ctor_kwargs=None 인 경우) 세션 키 자체가 장치 이름과 일치하는지도 확인한다.
        Connect()에서 이미 스캔된 device_index/device_name이 __init__에 전달되므로, 그 정보로 세션을 찾는다.
        또한 클래스 레벨 device registry에서 device 정보를 추가로 수집한다 (새 인스턴스가 registry를 공유).

        여러 세션이 존재하고 인스턴스에 device 정보가 없으면 **가장 최근에 시작된 세션**을
        반환한다. 이는 사용자가 device/session_name 을 지정하지 않고
        StartMonitor → StopMonitor 를 순차적으로 호출하는 시나리오를 지원한다.
        """
        targets = [self._device_name, self._device_index]
        targets = [t for t in targets if t]
        # 클래스 레벨 device registry에서 device 정보를 추가로 수집 (새 인스턴스가 공유)
        registry = getattr(type(self), "_device_registry", {})
        for reg_name, (reg_idx, reg_full) in registry.items():
            if reg_name not in targets:
                targets.append(reg_name)
            if reg_full not in targets:
                targets.append(reg_full)
            if str(reg_idx) not in targets:
                targets.append(str(reg_idx))
        candidates: list[tuple[str, float]] = []  # (session_name, created_at)
        with self._lock:
            for name, s in self._sessions.items():
                dev = str(s.get("device", ""))
                sidx = s.get("device_index")
                created = float(s.get("created_at", 0) or 0)
                matched = False
                # 1) 세션에 저장된 device_index와 인스턴스의 device_index가 일치하면 매칭 (가장 정확)
                if self._device_index and sidx is not None and str(sidx) == str(self._device_index).strip():
                    matched = True
                # 2) 등록된 device_name/device_index 가 세션의 device 문자열에 포함되면 매칭
                if not matched:
                    for t in targets:
                        if t and t.lower() in dev.lower():
                            matched = True
                            break
                # 3) 세션 키 자체가 device 문자열에 포함되면 매칭 (예: 'Cluster' in '[1] Cluster(7- USB Audio Device)')
                if not matched and name and name.lower() in dev.lower():
                    matched = True
                # 4) 세션 키가 장치 이름과 부분 일치하면 매칭 (예: 'Cluster' vs 'Cluster(7- USB Audio Device)')
                if not matched and name and any(name.lower() in d.lower() for d in targets):
                    matched = True
                # 5) 세션 키가 장치 이름과 정확히 일치하면 매칭 (예: session_name="Cluster" vs device_name="Cluster")
                if not matched and name and any(name.lower() == d.lower() for d in targets):
                    matched = True
                # 6) 세션 키가 sanitized 장치 이름과 일치하면 매칭
                #    (예: session_name="Cluster_7-_USB_Audio_Device" vs device="[1] Cluster(7- USB Audio Device)")
                if not matched and name:
                    # sanitized 세션 키에서 원래 장치 이름의 일부를 추출해 비교
                    # 예: "Cluster_7-_USB_Audio_Device" → "Cluster" 부분이 device 문자열에 있는지
                    for part in name.split("_"):
                        if len(part) >= 3 and part.lower() in dev.lower():
                            matched = True
                            break
                if matched:
                    candidates.append((name, created))
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0][0]
        # 여러 후보가 있으면 가장 최근에 시작된 세션을 반환
        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    # ------------------------------------------------------------------
    # 연결 lifecycle (스텝 목록에는 노출되지 않음)
    # ------------------------------------------------------------------
    def Connect(self) -> str:
        """마이크 존재/열림 여부를 확인한다 (짧게 열었다 닫는 probe)."""
        # 새 실행(run)이 시작되면 _used_devices 를 초기화한다.
        # ReplayKit 은 각 디바이스 등록 시 Connect() 를 호출하므로,
        # 이 시점에 이전 실행에서 사용된 장치 추적을 리셋한다.
        getattr(type(self), "_used_devices", set()).clear()
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
        # 클래스 레벨 장치 레지스트리에 저장 (새 인스턴스가 공유).
        # 키는 id(self)가 아니라 **장치 이름**을 사용한다. ReplayKit은 호출마다 새 인스턴스를
        # 만들므로 id(self)는 매번 달라지지만, 장치 이름은 안정적이어서 새 인스턴스가
        # 이 레지스트리에서 자신의 장치를 찾을 수 있다.
        type(self)._device_registry[name] = (idx, name)
        logger.info("AudioMonitor connected: [%d] %s (%dHz) registry=%s",
                    idx, name, self._rate, list(type(self)._device_registry.keys()))
        return f"ok: audio input [{idx}] {name}"

    def IsConnected(self) -> bool:
        return bool(self._connected)

    def Disconnect(self) -> str:
        """진행 중인 모든 세션을 정리(저장)하고 장치를 놓는다."""
        stopped = []
        session_name = self._find_session_name()
        if session_name is not None:
            try:
                self.StopMonitor()
                stopped.append(session_name)
            except Exception as e:
                logger.warning("AudioMonitor stop '%s' on disconnect failed: %s", session_name, e)
        self._connected = False
        _pa_release(id(self))
        # 모든 세션이 종료되었으므로 _used_devices 를 초기화한다.
        with self._lock:
            if not self._sessions:
                getattr(type(self), "_used_devices", set()).clear()
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

    def StartMonitor(self, drop_threshold: int = 0,
                     judge_mode: str = "pass", judge_count: int = 50,
                     duration: float = 0, device: str = "", session_name: str = "") -> str:
        """오디오 무음(drop) 모니터링을 시작한다 (백그라운드 녹음).

        device: 선택 장치명/번호 (예: "Cluster", "IVI", "1"). 비어 있으면 자동 선택.
        session_name: 세션 식별 키. 비어 있으면 선택된 장치 이름으로 자동 결정.
        Connect()에서 이미 스캔된 device_index/device_name이 __init__에 전달되므로, 여기서 지정할 필요가 없다.
        **사용 중이 아닌 장치를 자동으로 선택**한다.
        이렇게 하면 여러 AudioMonitor 인스턴스가 각자 다른 마이크를 자동으로 배정받는다.
        세션 이름은 선택된 장치 이름으로 자동 결정한다 (예: "Cluster" → session_name="Cluster").
        같은 세션이 이미 돌고 있으면 이전 세션을 정리하고 새로 시작한다.
        """
        if pyaudio is None:
            return f"FAIL: {_NO_PYAUDIO}"

        threshold = _to_int(drop_threshold, 0) or self._default_threshold
        mode = str(judge_mode or "pass").strip().lower()
        if mode not in ("pass", "fail"):
            logger.warning("Invalid judge_mode '%s' — using 'pass'", judge_mode)
            mode = "pass"
        count = _to_int(judge_count, 50)
        dur = _to_float_or_none(duration)

        # device/session_name 이 주어지면 그 이름으로 장치를 지정, 없으면 자동 선택.
        hint = str(device or session_name or "").strip() if (device or session_name) else ""
        try:
            idx, name = self._resolve_target(hint=hint)
        except Exception as e:
            return f"FAIL: 오디오 장치 열거 실패 — {e}"
        if idx is None:
            return (f"FAIL: 오디오 입력 장치를 찾을 수 없습니다 "
                    f"(index='{self._device_index}', name='{self._device_name}')")
        # session_name 이 주어지면 그대로, 없으면 선택된 장치 이름으로 자동 생성 (예: "Cluster" → "Cluster")
        if session_name and str(session_name).strip():
            session_name = str(session_name).strip()
        else:
            session_name = self._session_key(device_name=name) or "audio"
        # 이번 실행에서 사용된 장치로 기록 (다음 인스턴스가 다른 장치를 선택하도록)
        with self._lock:
            getattr(type(self), "_used_devices", set()).add(idx)

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
            "device_index": idx,
            "user_id": id(self),
            "created_at": time.time(),
        }
        with self._lock:
            self._sessions[session_name] = session
            logger.info("AudioMonitor StartMonitor: id=%s session='%s' stored, total_sessions=%s",
                        id(self), session_name, list(self._sessions.keys()))
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

        session_name: 종료할 세션 이름 (예: "Cluster"). 비어 있으면 자동 탐색.
        _find_session_name() 으로 이 인스턴스의 세션을 자동 탐색한다.
        Connect()에서 이미 스캔된 device_index/device_name이 __init__에 전달되므로, 그 정보로 세션을 찾는다.
        duration 으로 이미 자동 종료된 세션도 결과를 그대로 돌려준다.
        """
        # 1) 명시적 session_name 이 주어지면 그 키로 직접 조회
        if session_name and str(session_name).strip():
            session_name = str(session_name).strip()
            with self._lock:
                session = self._sessions.get(session_name)
                thread = self._threads.get(session_name)
            if session is None:
                # 명시적 키로 못 찾으면 자동 탐색으로 폴백
                alt = self._find_session_name()
                if alt is not None:
                    logger.info("AudioMonitor StopMonitor: session='%s' not found, falling back to '%s'",
                                session_name, alt)
                    session_name = alt
                    with self._lock:
                        session = self._sessions.get(session_name)
                        thread = self._threads.get(session_name)
            if session is None:
                return f"FAIL: '{session_name}' 세션이 없습니다 (StartMonitor 먼저 호출)"
        else:
            # 2) session_name 이 없으면 자동 탐색
            session_name = self._find_session_name()
            if session_name is None:
                return "FAIL: 이 인스턴스의 오디오 세션이 없습니다 (StartMonitor 먼저 호출)"
            with self._lock:
                session = self._sessions.get(session_name)
                thread = self._threads.get(session_name)
                logger.info("AudioMonitor StopMonitor: id=%s session='%s' found=%s total_sessions=%s",
                            id(self), session_name, session is not None, list(self._sessions.keys()))
            if session is None:
                return f"FAIL: '{session_name}' 세션이 없습니다 (StartMonitor 먼저 호출)"

        session["check"] = False

        # 스트림을 여기서 닫지 않는다. record thread 가 read() 에서 블로킹 중일 때
        # main thread 에서 stream.close() 를 호출하면 PortAudio 가 내부적으로
        # 스트림이 멈출 때까지 대기하므로 수 초간 블로킹될 수 있다.
        # 대신 check=False 를 설정하고 record thread 가 read() 를 마치고
        # finally 블록에서 스트림을 닫도록 한다.
        if thread is not None:
            thread.join(timeout=1)
            if thread.is_alive():
                logger.warning("AudioMonitor '%s' record thread did not stop in 1s", session_name)
                # 스레드가 아직 살아있으면 잠시 더 기다린다 (read() 가 최대 ~50ms 블로킹)
                thread.join(timeout=2)
                if thread.is_alive():
                    logger.warning("AudioMonitor '%s' record thread still alive after 3s", session_name)

        # 세션을 시작한 인스턴스의 user_id로 PyAudio 방출
        # (스레드가 아직 살아있으면 PyAudio 를 종료하지 않는다 - 스트림 사용 중일 수 있음)
        user_id = session.pop("user_id", None)
        if user_id is not None and not (thread is not None and thread.is_alive()):
            _pa_release(user_id)

        with self._lock:
            self._threads.pop(session_name, None)
            self._sessions.pop(session_name, None)
            logger.info("AudioMonitor StopMonitor: id=%s session='%s' stopped, remaining_sessions=%s",
                        id(self), session_name, list(self._sessions.keys()))

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

    def SaveReference(self, reference_name: str, duration: float = 10) -> str:
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
            idx, name = self._resolve_target()
        except Exception as e:
            return f"FAIL: 오디오 장치 열거 실패 — {e}"
        if idx is None:
            return f"FAIL: 오디오 입력 장치를 찾을 수 없습니다"

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

    def _find_latest_recording(self) -> Optional[Path]:
        """오디오 결과 폴더에서 가장 최근에 저장된 .wav 파일을 찾는다.

        StopMonitor 가 _record_loop 를 통해 저장한 녹음 파일을 세션 상태 없이
        직접 찾기 위한 헬퍼. 재생 중이면 {run_dir}/logs/audio/, 아니면
        results/Temp_logs/audio/ 아래의 세션 폴더에서 가장 최근 .wav 를 반환한다.
        """
        base_dir, _ = _audio_base_dir()
        if not base_dir.exists():
            return None
        candidates: list[Path] = []
        for p in base_dir.rglob("*.wav"):
            # 기준음(reference) 폴더는 별도 위치이므로 여기엔 없지만, 혹시 모르니 제외
            if "Audio_Reference" in str(p):
                continue
            candidates.append(p)
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    def CompareWithReference(self, reference_name: str, threshold: float = 0.85) -> str:
        """가장 최근에 저장된 녹음을 기준음과 비교해 PASS/FAIL 을 판정한다.

        세션 상태에 의존하지 않고, StopMonitor 가 저장한 가장 최근 .wav 파일을
        직접 찾아 비교한다. 판정 방식은 audiocomparetest/AudioCompareTest.py 를
        따른다 (NCC 기반):

          1. WAV 로드 → 모노 변환 → float 정규화
          2. 샘플레이트가 다르면 높은 쪽에 맞춰 리샘플링
          3. 앞뒤 무음 구간 트리밍
          4. Cross-correlation 으로 최적 정렬 오프셋 탐색
          5. 정렬된 겹치는 구간에 대해 NCC(Normalized Cross-Correlation) 계산
          6. NCC >= threshold → PASS, NCC < threshold → FAIL
             (겹치는 구간이 ref 길이의 50% 미만이면 비교 불가로 FAIL)

        threshold: NCC 유사도 기준값 (0~1). 기본 0.85 (같은 장비·환경 권장).
        """
        reference_name = str(reference_name or "").strip()
        if not reference_name:
            return "FAIL: reference_name 을 지정하세요"

        # threshold 파라미터 파싱 (0~1 범위)
        try:
            thr = float(threshold)
        except (TypeError, ValueError):
            thr = COMPARE_DEFAULT_THRESHOLD
        if not (0.0 < thr <= 1.0):
            logger.warning("AudioMonitor compare: invalid threshold=%r — using default %.2f",
                           threshold, COMPARE_DEFAULT_THRESHOLD)
            thr = COMPARE_DEFAULT_THRESHOLD

        wav1 = self._find_latest_recording()
        if wav1 is None:
            return "FAIL: 녹음 결과가 없습니다 (StopMonitor 먼저 호출)"
        session_name = wav1.parent.name
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
            # ── 1) float 정규화 (int16 → float64, -1~1) ──
            a1 = np.asarray(s1, dtype=np.float64) / 32768.0
            a2 = np.asarray(s2, dtype=np.float64) / 32768.0
            peak1 = float(np.mean(np.abs(a1)) * 2)
            peak2 = float(np.mean(np.abs(a2)) * 2)
            rms1 = float(np.sqrt(np.mean(a1 ** 2)))
            rms2 = float(np.sqrt(np.mean(a2 ** 2)))

            # ── 2) 샘플레이트 통일 (높은 쪽에 맞춤) ──
            sr_ref, sr_rec = rate1 or self._rate, rate2 or self._rate
            if sr_ref != sr_rec:
                target_sr = max(sr_ref, sr_rec)
                if sr_ref < target_sr:
                    g = math.gcd(target_sr, sr_ref)
                    a1 = _resample_poly(a1, target_sr // g, sr_ref // g)
                else:
                    g = math.gcd(target_sr, sr_rec)
                    a2 = _resample_poly(a2, target_sr // g, sr_rec // g)
                sr = target_sr
            else:
                sr = sr_ref

            # ── 3) 무음 구간 트리밍 (앞뒤 무음 제거로 정렬 정확도 향상) ──
            t1 = _trim_silence_ncc(a1, sr)
            t2 = _trim_silence_ncc(a2, sr)

            # ── 4) 최적 오프셋 탐색 (cross-correlation) ──
            lag = _find_best_offset_ncc(t1, t2)

            # ── 5) 겹치는 구간 추출 ──
            s_ref, e_ref, s_rec, e_rec = _overlap_indices_ncc(len(t1), len(t2), lag)
            overlap_len = e_ref - s_ref
            overlap_ratio = overlap_len / len(t1) if len(t1) > 0 else 0.0
            overlap_sec = overlap_len / sr

            # ── 6) 겹침이 너무 짧으면 불일치 ──
            if overlap_ratio < COMPARE_MIN_OVERLAP_RATIO:
                similarity = 0.0
                passed = False
                logger.info("AudioMonitor compare: overlap too short "
                            "(overlap=%.1f%%, 기준=%.1f%%)", overlap_ratio * 100,
                            COMPARE_MIN_OVERLAP_RATIO * 100)
            else:
                # ── 7) NCC 계산 ──
                seg_ref = t1[s_ref:e_ref]
                seg_rec = t2[s_rec:e_rec]
                similarity = _ncc(seg_ref, seg_rec)
                passed = similarity >= thr
                logger.info("AudioMonitor compare: NCC=%.4f (기준 >= %.2f) lag=%d samples "
                            "overlap=%.2fs (%.1f%%)",
                            similarity, thr, lag, overlap_sec, overlap_ratio * 100)
        else:
            # numpy 없이는 NCC 계산 불가 — 보수적으로 FAIL 처리
            peak1 = sum(abs(s) for s in s1) / len(s1) * 2
            peak2 = sum(abs(s) for s in s2) / len(s2) * 2
            rms1 = math.sqrt(sum(s * s for s in s1) / len(s1))
            rms2 = math.sqrt(sum(s * s for s in s2) / len(s2))
            similarity = 0.0
            lag = 0
            sr = rate1 or self._rate
            overlap_sec = 0.0
            overlap_ratio = 0.0
            passed = False

        db1 = 20.0 * math.log10(rms1) if rms1 > 0 else -120.0
        db2 = 20.0 * math.log10(rms2) if rms2 > 0 else -120.0

        out_dir = wav1.parent
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        stem = f"{ts}_compare_{_safe_name(session_name)}_{_safe_name(reference_name)}"
        # cv2.putText 는 ASCII 만 그린다 — 화살표/한글을 쓰면 '???' 로 찍힌다.
        header = (f"NCC={similarity:.4f}  threshold={thr:.2f}  "
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
                f"NCC similarity = {similarity:.4f} (기준 >= {thr:.2f})\n"
                f"offset = {lag:+d} samples ({lag / sr:+.4f}s)\n"
                f"overlap = {overlap_sec:.2f}s (ref 대비 {overlap_ratio:.1%})\n"
                f"result = {'PASS' if passed else 'FAIL'}\n",
                encoding="utf-8")
        except Exception as e:
            logger.warning("compare report save failed: %s", e)

        detail = (f"NCC={similarity:.4f} threshold={thr:.2f} "
                  f"session={session_name} ref={reference_name} img={img_path}")
        logger.info("AudioMonitor compare: %s → %s", detail, "PASS" if passed else "FAIL")
        if passed:
            return f"ok: PASS ({detail})"
        reasons = []
        if overlap_ratio < COMPARE_MIN_OVERLAP_RATIO:
            reasons.append(f"겹치는 구간 부족({overlap_ratio:.1%})")
        else:
            reasons.append(f"유사도 낮음({similarity:.4f} < {thr:.2f})")
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
                    # 스트림이 닫혔는지 먼저 확인 (StopMonitor 가 닫으면 즉시 종료)
                    if not session.get("check"):
                        break
                    try:
                        data = stream.read(DEFAULT_CHUNK, exception_on_overflow=False)
                    except Exception:
                        # StopMonitor 가 스트림을 닫았으면 정상 종료
                        if not session.get("check"):
                            break
                        raise
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
            # StopMonitor 에서 스트림을 닫았으면 read() 에러는 예상된 동작이다.
            # 이때는 error로 처리하지 않고 정상 종료로 간주한다.
            if session.get("check"):
                session["error"] = f"오디오 읽기 실패: {e}"
                session["result"] = "error"
                logger.warning("AudioMonitor '%s' read error: %s", session_name, e)
            else:
                logger.info("AudioMonitor '%s' stream closed by StopMonitor (expected)", session_name)

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

        # 스트림을 여기서 닫는다 (StopMonitor 가 닫지 않으므로 record thread 가 닫는다)
        stream = session.get("stream")
        if stream is not None:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            session["stream"] = None
