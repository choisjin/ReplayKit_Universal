# -*- coding: utf-8 -*-
"""Capture 스냅샷 저장 — 미러 화면 Capture 버튼 / `capture` 스텝 공용.

현재 프레임(PNG 바이트)을 Capture 폴더에 저장한다. 저장 규칙은 Webcam.Capture 영상과 같다:
  - 재생 중:            {run_dir}/Capture/c{사이클}/{prefix}{시각}_{full|roi}.png
  - 재생 아닐 때(스텝 테스트/직접 실행): results/Temp_logs/Capture/... (최근 _TEMP_KEEP 장만 유지)
결과 상세 'Capture 일괄보기'(/api/results/captures-for)가 이 폴더의 사진을 전부 모아 그리드로 보여준다.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 임시 폴더(Temp_logs/Capture) 보존 개수 — 초과분은 오래된 것부터 삭제. 런 폴더는 무제한.
_TEMP_KEEP = 100
IMAGE_EXTS = {".png", ".jpg", ".jpeg"}


class CaptureError(ValueError):
    """잘못된 입력(디코딩 실패, 빈 크롭 등) — 라우터가 400 으로 매핑."""


def _results_dir() -> Path:
    from .playback_service import RESULTS_DIR
    return Path(RESULTS_DIR)


def capture_output_dir() -> tuple[Path, int, bool]:
    """(저장 폴더, 사이클, 재생 중 여부)."""
    from .playback_service import get_current_step_context, get_run_output_dir
    run_dir = get_run_output_dir()
    if run_dir:
        _, cycle = get_current_step_context()
        cycle = int(cycle or 1)
        return Path(run_dir) / "Capture" / f"c{cycle}", cycle, True
    return _results_dir() / "Temp_logs" / "Capture", 0, False


def prune_capture_temp(out_dir: Path) -> None:
    try:
        files = sorted(
            (p for p in out_dir.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS),
            key=lambda p: p.stat().st_mtime,
        )
        for old in files[:max(0, len(files) - (_TEMP_KEEP - 1))]:
            old.unlink(missing_ok=True)
    except Exception as e:
        logger.debug("capture temp prune failed: %s", e)


def save_capture_snapshot(png_bytes: bytes, crop: Optional[dict] = None, name_prefix: str = "") -> dict:
    """PNG 바이트를 (선택적으로 crop 해서) Capture 폴더에 저장하고 저장 정보를 돌려준다.

    반환: {filename, rel_path(results 기준 posix), url(/results-files/...), cycle, in_run, width, height, roi}
    """
    from urllib.parse import quote

    import numpy as np

    from ..utils.cv2_loader import cv2
    from ..utils.cv_io import safe_imwrite

    img = cv2.imdecode(np.frombuffer(png_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise CaptureError("Cannot decode captured image")
    h, w = img.shape[:2]
    roi = None
    if crop:
        x = max(0, min(int(crop.get("x", 0)), w))
        y = max(0, min(int(crop.get("y", 0)), h))
        x2 = max(x, min(x + int(crop.get("width", 0)), w))
        y2 = max(y, min(y + int(crop.get("height", 0)), h))
        if x2 - x < 1 or y2 - y < 1:
            raise CaptureError("Crop region is empty")
        img = img[y:y2, x:x2]
        roi = {"x": x, "y": y, "width": x2 - x, "height": y2 - y}

    out_dir, cycle, in_run = capture_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    if not in_run:
        prune_capture_temp(out_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    filename = f"{name_prefix}{ts}_{'roi' if roi else 'full'}.png"
    target = out_dir / filename
    if not safe_imwrite(str(target), img):
        raise RuntimeError(f"Failed to write image: {target}")
    rel = target.resolve().relative_to(_results_dir().resolve()).as_posix()
    return {
        "filename": filename,
        "rel_path": rel,
        "url": "/results-files/" + "/".join(quote(seg) for seg in rel.split("/")),
        "cycle": cycle,
        "in_run": in_run,
        "width": int(img.shape[1]),
        "height": int(img.shape[0]),
        "roi": roi,
    }
