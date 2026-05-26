"""OCR 서비스 — RapidOCR 기반 다국어 텍스트 검출 및 추출.

언어별 인식 모델은 backend/app/services/ocr_models/{lang}/{rec_infer.onnx, rec_keys.txt}
에 배치되어야 한다. 모델 설치는 `scripts/download_ocr_models.py`로 수행.

지원 언어 ID(폴더명): korean, english, japan, chinese, latin, cyrillic, arabic, devanagari.
설치되지 않은 언어를 요청하면 번들된 RapidOCR 기본(중국어 PP-OCRv4) 엔진으로 폴백한다.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "korean"
_MODELS_ROOT = Path(__file__).resolve().parent / "ocr_models"

# 언어별 엔진 캐시 — RapidOCR 인스턴스는 무겁고 thread-safe하므로 1회 생성 후 재사용.
# 키: language ID (예: "korean", "english"). 값: RapidOCR instance 또는 None(초기화 실패).
_engines: dict[str, object] = {}
# 언어별 초기화 실패 플래그 — 재시도 시 같은 import/init 비용 반복 방지.
_init_failed: set[str] = set()


def _language_paths(language: str) -> Tuple[Optional[Path], Optional[Path]]:
    """(rec_model_path, rec_keys_path) — 존재하지 않으면 (None, None)."""
    if not language:
        return None, None
    base = _MODELS_ROOT / language
    onnx = base / "rec_infer.onnx"
    keys = base / "rec_keys.txt"
    if onnx.exists() and keys.exists():
        return onnx, keys
    return None, None


def _get_engine(language: Optional[str] = None):
    """언어별 RapidOCR 엔진 반환. 모델 미설치 시 번들 기본 엔진으로 폴백."""
    lang = (language or DEFAULT_LANGUAGE).lower().strip() or DEFAULT_LANGUAGE

    if lang in _engines:
        return _engines[lang]
    if lang in _init_failed:
        # 폴백 엔진(""키)이 있으면 사용, 없으면 None
        return _engines.get("")

    import sys
    logger.info("OCR[%s]: Python 실행 경로 = %s", lang, sys.executable)
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError as e:
        _init_failed.add(lang)
        logger.error(
            "OCR: rapidocr_onnxruntime import 실패: %s\n"
            "  현재 Python: %s\n"
            "  → 백엔드를 venv Python으로 실행하세요: venv/bin/python -m uvicorn ...",
            e, sys.executable,
        )
        return None

    rec_model, rec_keys = _language_paths(lang)
    try:
        if rec_model is not None and rec_keys is not None:
            engine = RapidOCR(rec_model_path=str(rec_model), rec_keys_path=str(rec_keys))
            logger.info("OCR[%s]: 언어별 모델 로드 — %s", lang, rec_model.name)
        else:
            # 미설치 언어 → 번들 기본 엔진 사용 (한 번만 생성)
            if "" not in _engines:
                _engines[""] = RapidOCR()
                logger.info("OCR: 기본 번들 엔진(ch_PP-OCRv4) 초기화")
            _engines[lang] = _engines[""]  # 같은 객체 재사용
            logger.warning(
                "OCR[%s]: 모델 미설치 — 기본 번들 엔진으로 폴백. "
                "설치하려면 'python scripts/download_ocr_models.py %s' 실행.",
                lang, lang,
            )
            return _engines[lang]
        _engines[lang] = engine
        return engine
    except Exception as e:
        _init_failed.add(lang)
        logger.error("OCR[%s] 초기화 실패: %s", lang, e, exc_info=True)
        return None


def available_languages() -> list[str]:
    """`ocr_models/`에 설치된 언어 목록 (rec_infer.onnx + rec_keys.txt 쌍이 있는 것만)."""
    if not _MODELS_ROOT.exists():
        return []
    out = []
    for child in sorted(_MODELS_ROOT.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if (child / "rec_infer.onnx").exists() and (child / "rec_keys.txt").exists():
            out.append(child.name)
    return out


def _bytes_to_array(image_bytes: bytes):
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _fuzzy_score(candidate: str, target: str) -> float:
    """대소문자 무시 부분 일치 점수 (0.0~1.0)."""
    try:
        from rapidfuzz.fuzz import partial_ratio  # type: ignore
        return partial_ratio(target.lower(), candidate.lower()) / 100.0
    except ImportError:
        return 1.0 if target.lower() in candidate.lower() else 0.0


class OcrItem:
    def __init__(self, text: str, box: list, score: float):
        self.text = text
        self.box = box  # [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]
        self.score = score

    @property
    def center(self) -> Tuple[int, int]:
        xs = [p[0] for p in self.box]
        ys = [p[1] for p in self.box]
        return (int(sum(xs) / 4), int(sum(ys) / 4))


def run_ocr(image_bytes: bytes, language: Optional[str] = None) -> List[OcrItem]:
    """이미지에서 OCR 실행. language로 인식 모델 선택 (기본 korean)."""
    engine = _get_engine(language)
    if engine is None:
        logger.warning("OCR: engine 없음 (rapidocr_onnxruntime 미설치 또는 초기화 실패)")
        return []
    img = _bytes_to_array(image_bytes)
    if img is None:
        logger.warning("OCR: 이미지 디코딩 실패 (bytes len=%d)", len(image_bytes) if image_bytes else 0)
        return []
    logger.info("OCR[%s]: 이미지 크기 %dx%d, channels=%d",
                language or DEFAULT_LANGUAGE,
                img.shape[1], img.shape[0], img.shape[2] if img.ndim == 3 else 1)
    try:
        raw = engine(img)
    except Exception as e:
        logger.error("OCR 엔진 오류: %s", e)
        return []
    # RapidOCR 결과 형식이 버전마다 다름:
    #  - 구버전: (result, elapse_list) — result는 [[box, [text, score]], ...]
    #  - 신버전: TextRecResult 객체 또는 단일 result만 반환할 수도 있음
    if raw is None:
        logger.info("OCR: 결과 None (텍스트 미검출)")
        return []
    if isinstance(raw, tuple) and len(raw) == 2:
        result = raw[0]
    else:
        result = raw
    if result is None or not result:
        logger.info("OCR: 결과 empty")
        return []
    items = []
    for idx, item in enumerate(result):
        try:
            if hasattr(item, "txt") and hasattr(item, "box"):
                # 신버전 객체 형태
                items.append(OcrItem(text=item.txt, box=item.box, score=getattr(item, "score", 1.0)))
            elif isinstance(item, (list, tuple)):
                if len(item) >= 3 and not isinstance(item[1], (list, tuple)):
                    # [box, text, score] 형식
                    items.append(OcrItem(text=str(item[1]), box=item[0], score=float(item[2])))
                elif len(item) >= 2:
                    # [box, [text, score]] 형식
                    box = item[0]
                    ts = item[1]
                    if isinstance(ts, (tuple, list)) and len(ts) >= 2:
                        items.append(OcrItem(text=str(ts[0]), box=box, score=float(ts[1])))
                    else:
                        items.append(OcrItem(text=str(ts), box=box, score=1.0))
        except Exception as e:
            logger.warning("OCR 결과 파싱 실패 idx=%d item=%r err=%s", idx, item, e)
            continue
    logger.info("OCR: %d개 텍스트 검출 — 샘플: %s", len(items),
                [(it.text, round(it.score, 2)) for it in items[:5]])
    return items


def has_text(
    image_bytes: bytes, target: str, threshold: float = 0.8,
    language: Optional[str] = None,
) -> Tuple[bool, Optional[Tuple[int, int]]]:
    """이미지에서 target 텍스트 존재 여부 판단. (found, center_xy)"""
    items = run_ocr(image_bytes, language=language)
    best_score = 0.0
    best_center = None
    for item in items:
        score = _fuzzy_score(item.text, target)
        if score > best_score:
            best_score = score
            best_center = item.center
    if best_score >= threshold:
        return True, best_center
    return False, None


def find_text_center(
    image_bytes: bytes, target: str, threshold: float = 0.8,
    language: Optional[str] = None,
) -> Optional[Tuple[int, int]]:
    """텍스트 중심 좌표 반환. 없으면 None."""
    found, center = has_text(image_bytes, target, threshold, language=language)
    return center if found else None


def check_text_in_region(
    image_bytes: bytes, target: str, x: int, y: int, width: int, height: int,
    threshold: float = 0.8, language: Optional[str] = None,
) -> bool:
    """지정 영역을 크롭한 뒤 target 텍스트 포함 여부 검증."""
    region_text = extract_region_text(image_bytes, x, y, width, height, language=language)
    return _fuzzy_score(region_text, target) >= threshold


def find_text_center_in_region(
    image_bytes: bytes, target: str, x: int, y: int, width: int, height: int,
    threshold: float = 0.8, language: Optional[str] = None,
) -> Optional[Tuple[int, int]]:
    """지정 영역 내에서 target 텍스트 검색 후 원본 이미지 기준 중심 좌표 반환."""
    import cv2
    img = _bytes_to_array(image_bytes)
    if img is None:
        return None
    h, w = img.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(w, x + width), min(h, y + height)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = img[y1:y2, x1:x2]
    _, buf = cv2.imencode(".png", crop)
    if buf is None:
        return None
    items = run_ocr(buf.tobytes(), language=language)
    best_score = 0.0
    best_center: Optional[Tuple[int, int]] = None
    for item in items:
        score = _fuzzy_score(item.text, target)
        if score > best_score:
            best_score = score
            best_center = item.center
    if best_score < threshold or best_center is None:
        return None
    # 크롭 오프셋을 더해 원본 이미지 좌표로 변환
    cx, cy = best_center
    return (cx + x1, cy + y1)


def extract_region_text(
    image_bytes: bytes, x: int, y: int, width: int, height: int,
    language: Optional[str] = None,
) -> str:
    """지정 영역 크롭 후 OCR로 텍스트 추출."""
    import cv2
    img = _bytes_to_array(image_bytes)
    if img is None:
        return ""
    h, w = img.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(w, x + width), min(h, y + height)
    if x2 <= x1 or y2 <= y1:
        return ""
    crop = img[y1:y2, x1:x2]
    _, buf = cv2.imencode(".png", crop)
    if buf is None:
        return ""
    items = run_ocr(buf.tobytes(), language=language)
    return " ".join(item.text for item in items).strip()


def extract_region_items(
    image_bytes: bytes, x: int, y: int, width: int, height: int,
    language: Optional[str] = None,
) -> Tuple[List[OcrItem], int, int]:
    """지정 영역 크롭 후 OCR로 모든 텍스트 아이템 추출.

    Returns:
        (items, offset_x, offset_y) — items의 box/center 좌표는 크롭 로컬 좌표계.
        호출자가 offset_x/y를 더해 원본 이미지 좌표로 환산해야 한다.
        영역이 잘못되거나 디코딩 실패 시 ([], 0, 0).
    """
    import cv2
    img = _bytes_to_array(image_bytes)
    if img is None:
        return [], 0, 0
    h, w = img.shape[:2]
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(w, x + width), min(h, y + height)
    if x2 <= x1 or y2 <= y1:
        return [], 0, 0
    crop = img[y1:y2, x1:x2]
    ok, buf = cv2.imencode(".png", crop)
    if not ok or buf is None:
        return [], 0, 0
    items = run_ocr(buf.tobytes(), language=language)
    return items, x1, y1
