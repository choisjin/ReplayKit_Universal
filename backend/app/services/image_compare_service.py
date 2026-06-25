"""Image comparison service — SSIM, ROI, template matching, exclusion, multi-crop."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional


def _env_blur_params() -> tuple[int, float]:
    """IMG_COMPARE_BLUR_SIGMA 환경변수로 노이즈 흡수 강도 조정.

    기본값 σ=0.5 (약한 노이즈 흡수). 같은 정지 화면을 두 번 캡처해도 컴포지터 sub-pixel
    재렌더링/안티알리어싱 jitter로 SSIM이 0.92 정도 나오는 환경(MIB 등)에서 이 값을 1.0~1.5로
    올리면 흡수율이 높아져 점수가 올라감 (실제 변화는 여전히 잡힘). 너무 크면 작은 UI 변화도
    묻히므로 1.5 이상은 비추천. 커널 크기는 σ에 비례해 자동 조정.
    """
    try:
        sigma = float(os.environ.get("IMG_COMPARE_BLUR_SIGMA", "") or 0.5)
    except Exception:
        sigma = 0.5
    sigma = max(0.0, min(3.0, sigma))
    if sigma <= 0.6:
        ksize = 3
    elif sigma <= 1.2:
        ksize = 5
    else:
        ksize = 7
    return ksize, sigma


def _env_debug() -> bool:
    return os.environ.get("IMG_COMPARE_DEBUG", "").strip() in ("1", "true", "yes")

from ..utils.cv2_loader import cv2

try:
    import numpy as np
    from skimage.metrics import structural_similarity as ssim
    _CV2_AVAILABLE = cv2 is not None
except (ImportError, OSError):
    _CV2_AVAILABLE = False
    np = None  # type: ignore
    ssim = None  # type: ignore

from ..utils.cv_io import safe_imread, safe_imwrite

logger = logging.getLogger(__name__)


class ImageCompareService:
    """Compare expected vs actual screenshots."""

    @staticmethod
    def _require_cv2():
        if not _CV2_AVAILABLE:
            raise RuntimeError(
                "opencv-python is not installed or failed to load. "
                "Install it with: pip install opencv-python-headless"
            )

    @staticmethod
    def _resolve_img(img, path):
        """ndarray가 주어지면 그대로, 아니면 path에서 읽는다. 둘 다 없으면 None."""
        if img is not None:
            return img
        if path:
            return safe_imread(path)
        return None

    @staticmethod
    def _normalize_for_ssim(gray):
        """SSIM 비교 전 sub-pixel 노이즈 정규화.

        같은 윈도우/화면이라도 캡처 시점·GPU 컴포지터·폰트 안티알리어싱·마우스 호버 등의
        영향으로 픽셀값이 ±1~3 정도 흔들리는 경우가 흔하다. 가벼운 가우시안 블러로 sub-pixel
        노이즈를 흡수하면 SSIM 점수가 시각적 유사도와 더 잘 일치한다 (의미 있는 변화는 그대로 잡힘).

        강도는 IMG_COMPARE_BLUR_SIGMA 환경변수로 조정 가능 (기본 0.5).
        """
        if cv2 is None or gray is None:
            return gray
        try:
            ksize, sigma = _env_blur_params()
            if sigma <= 0:
                return gray
            return cv2.GaussianBlur(gray, (ksize, ksize), sigma)
        except Exception:
            return gray

    # ------------------------------------------------------------------
    # Level 1 — Full-image SSIM
    # ------------------------------------------------------------------

    def compare_ssim(
        self,
        expected_path: str,
        actual_path: str,
        roi: Optional[dict] = None,
        img_exp=None,
        img_act=None,
    ) -> dict:
        """Return similarity score and diff image path.

        Args:
            expected_path: path to expected screenshot PNG
            actual_path: path to actual screenshot PNG
            roi: optional dict with x, y, width, height to crop before comparing
            img_exp/img_act: pre-loaded ndarrays (재사용 최적화용, 주어지면 path는 무시)
        """
        self._require_cv2()
        img_exp = self._resolve_img(img_exp, expected_path)
        img_act = self._resolve_img(img_act, actual_path)
        if img_exp is None or img_act is None:
            return {"score": 0.0, "error": "Could not read one or both images"}

        # Apply ROI crop if specified
        if roi:
            x, y, w, h = roi["x"], roi["y"], roi["width"], roi["height"]
            ah, aw = img_act.shape[:2]
            eh, ew = img_exp.shape[:2]
            # actual은 ROI로 자르고, expected는 이미 크롭된 상태일 수 있으므로 크기 비교 후 결정
            img_act = img_act[y : y + h, x : x + w]
            if (eh, ew) != (h, w):
                # expected가 풀 스크린샷이면 동일 ROI로 자른다
                if eh >= y + h and ew >= x + w:
                    img_exp = img_exp[y : y + h, x : x + w]

        # 크기가 다르면 actual을 expected에 맞춰 resize (template matching 사용 안 함)
        if img_exp.shape != img_act.shape:
            img_act = cv2.resize(img_act, (img_exp.shape[1], img_exp.shape[0]))

        # Convert to grayscale for SSIM
        gray_exp = cv2.cvtColor(img_exp, cv2.COLOR_BGR2GRAY)
        gray_act = cv2.cvtColor(img_act, cv2.COLOR_BGR2GRAY)
        # Sub-pixel 노이즈 정규화 — 같은 화면 캡처가 100% 가까이 나오도록.
        gray_exp_n = self._normalize_for_ssim(gray_exp)
        gray_act_n = self._normalize_for_ssim(gray_act)

        score, diff = ssim(gray_exp_n, gray_act_n, full=True)
        diff_uint8 = (diff * 255).astype("uint8")

        # 진단: blur 적용 전후 SSIM, BGR 픽셀 동일성, 픽셀 차이 분포까지 한 번에 측정.
        # IMG_COMPARE_DEBUG=1 또는 score<0.99 이면 자동 출력. single_crop은 playback_service가
        # 미리 ROI를 잘라 이 함수에 전달하므로 여기서도 진단해야 함.
        if _env_debug() or float(score) < 0.99:
            try:
                bgr_diff = cv2.absdiff(img_exp, img_act)
                identical = bool((bgr_diff == 0).all())
                bgr_max = int(bgr_diff.max())
                bgr_mean = float(bgr_diff.mean())
                bgr_gt5 = 100.0 * float((bgr_diff > 5).sum()) / max(1, bgr_diff.size)
                raw_score, _ = ssim(gray_exp, gray_act, full=True)
                gd = cv2.absdiff(gray_exp, gray_act)
                logger.info(
                    "compare_ssim shape=%s "
                    "ssim_blurred=%.4f ssim_raw=%.4f identical=%s "
                    "bgr_diff: max=%d mean=%.2f >5=%.1f%% "
                    "gray_diff: max=%d mean=%.2f >5=%.1f%%",
                    tuple(img_exp.shape),
                    float(score), float(raw_score), identical,
                    bgr_max, bgr_mean, bgr_gt5,
                    int(gd.max()), float(gd.mean()),
                    100.0 * float((gd > 5).sum()) / max(1, gd.size),
                )
            except Exception as e:
                logger.debug("compare_ssim debug log failed: %r", e)

        return {
            "score": round(float(score), 4),
            "diff_array": diff_uint8,
        }

    def _compare_at_roi(
        self,
        img_exp: np.ndarray,
        img_act: np.ndarray,
        roi: dict,
    ) -> dict:
        """ROI 영역을 actual에서 잘라 expected 크롭 이미지와 직접 SSIM 비교.

        Template matching을 사용하지 않고, 녹화 시 저장된 ROI 좌표를 그대로 사용한다.
        """
        ah, aw = img_act.shape[:2]
        rx = max(0, int(roi["x"]))
        ry = max(0, int(roi["y"]))
        rw = int(roi["width"])
        rh = int(roi["height"])
        rx2 = min(aw, rx + rw)
        ry2 = min(ah, ry + rh)

        # 진단: actual 풀 사이즈 + 요청 roi + clamp 후 실제 잘린 영역 + expected shape.
        # 좌표 misalignment 즉시 감지.
        logger.info(
            "_compare_at_roi align: actual_full=%dx%d roi_req=(x=%d y=%d w=%d h=%d) "
            "clamped=(x=%d y=%d w=%d h=%d) expected_shape=%s",
            aw, ah, int(roi.get("x", 0)), int(roi.get("y", 0)), rw, rh,
            rx, ry, rx2 - rx, ry2 - ry, tuple(img_exp.shape),
        )

        actual_crop = img_act[ry:ry2, rx:rx2]
        if actual_crop.size == 0:
            return {
                "score": 0.0,
                "error": "ROI is outside actual image bounds",
                "match_location": {"x": rx, "y": ry, "width": rw, "height": rh},
            }
        if actual_crop.shape[:2] != img_exp.shape[:2]:
            logger.warning(
                "_compare_at_roi SHAPE MISMATCH: actual_crop=%s expected=%s → resize will distort",
                actual_crop.shape, img_exp.shape,
            )

        # expected 크롭 이미지와 크기가 다르면 actual_crop을 expected에 맞춤
        if img_exp.shape[:2] != actual_crop.shape[:2]:
            actual_crop = cv2.resize(actual_crop, (img_exp.shape[1], img_exp.shape[0]))

        gray_exp = cv2.cvtColor(img_exp, cv2.COLOR_BGR2GRAY)
        gray_act = cv2.cvtColor(actual_crop, cv2.COLOR_BGR2GRAY)
        # Sub-pixel 노이즈 정규화 — 작은 크롭은 안티알리어싱 차이에 특히 민감.
        gray_exp_n = self._normalize_for_ssim(gray_exp)
        gray_act_n = self._normalize_for_ssim(gray_act)
        score, diff = ssim(gray_exp_n, gray_act_n, full=True)
        diff_uint8 = (diff * 255).astype("uint8")

        # 진단: blur 적용 전후 SSIM, BGR 픽셀 동일성, 픽셀 차이 분포까지 한 번에 측정.
        # 환경변수 IMG_COMPARE_DEBUG=1 로 항상 켜거나, score<0.99 일 때 자동으로 한 번 로깅
        # (FAIL 원인을 환경변수 설정 없이도 즉시 확인할 수 있도록).
        if _env_debug() or float(score) < 0.99:
            try:
                # 1) BGR 원본 차이 — 캡처 자체의 픽셀 차이 (블러 영향 0)
                bgr_diff = cv2.absdiff(img_exp, actual_crop)
                identical = bool((bgr_diff == 0).all())
                bgr_max = int(bgr_diff.max())
                bgr_mean = float(bgr_diff.mean())
                bgr_gt5 = 100.0 * float((bgr_diff > 5).sum()) / max(1, bgr_diff.size)
                # 2) 그레이스케일 raw SSIM (블러 OFF)
                raw_score, _ = ssim(gray_exp, gray_act, full=True)
                # 3) 그레이스케일 raw 픽셀 차이
                gd = cv2.absdiff(gray_exp, gray_act)
                logger.info(
                    "image_compare ROI(%dx%d) "
                    "ssim_blurred=%.4f ssim_raw=%.4f identical=%s "
                    "bgr_diff: max=%d mean=%.2f >5=%.1f%% "
                    "gray_diff: max=%d mean=%.2f >5=%.1f%%",
                    actual_crop.shape[1], actual_crop.shape[0],
                    float(score), float(raw_score), identical,
                    bgr_max, bgr_mean, bgr_gt5,
                    int(gd.max()), float(gd.mean()),
                    100.0 * float((gd > 5).sum()) / max(1, gd.size),
                )
            except Exception as e:
                logger.debug("image_compare debug log failed: %r", e)

        return {
            "score": round(float(score), 4),
            "diff_array": diff_uint8,
            "match_location": {"x": rx, "y": ry, "width": rx2 - rx, "height": ry2 - ry},
        }

    # ------------------------------------------------------------------
    # Full-image SSIM with exclusion regions
    # ------------------------------------------------------------------

    def compare_ssim_with_exclusions(
        self,
        expected_path: str,
        actual_path: str,
        exclude_rois: list[dict],
        img_exp=None,
        img_act=None,
    ) -> dict:
        """SSIM comparison with specified regions excluded.

        Computes per-pixel SSIM, masks out excluded regions, and averages
        only the unmasked pixels for the final score.
        """
        self._require_cv2()
        img_exp = self._resolve_img(img_exp, expected_path)
        img_act = self._resolve_img(img_act, actual_path)
        if img_exp is None or img_act is None:
            return {"score": 0.0, "error": "Could not read one or both images"}

        if img_exp.shape != img_act.shape:
            img_act = cv2.resize(img_act, (img_exp.shape[1], img_exp.shape[0]))

        gray_exp = cv2.cvtColor(img_exp, cv2.COLOR_BGR2GRAY)
        gray_act = cv2.cvtColor(img_act, cv2.COLOR_BGR2GRAY)
        # Sub-pixel 노이즈 정규화 (compare_ssim 와 동일).
        gray_exp = self._normalize_for_ssim(gray_exp)
        gray_act = self._normalize_for_ssim(gray_act)

        # Compute full SSIM map
        _, diff = ssim(gray_exp, gray_act, full=True)

        # Build inclusion mask (True = include, False = exclude)
        h, w = diff.shape
        mask = np.ones((h, w), dtype=bool)
        for roi in exclude_rois:
            rx, ry = roi["x"], roi["y"]
            rw, rh = roi["width"], roi["height"]
            mask[ry:ry + rh, rx:rx + rw] = False

        # Average SSIM only over included pixels
        if mask.sum() == 0:
            score = 1.0  # nothing to compare
        else:
            score = float(diff[mask].mean())

        # Build diff array with excluded regions zeroed out (shown as identical)
        diff_uint8 = (diff * 255).astype("uint8")
        diff_uint8[~mask] = 255  # mark excluded as "identical" in diff

        logger.info(
            "Exclusion comparison: %d regions excluded, score=%.4f",
            len(exclude_rois), score,
        )

        return {
            "score": round(score, 4),
            "diff_array": diff_uint8,
            "exclude_rois": exclude_rois,
        }

    # ------------------------------------------------------------------
    # Multi-crop comparison
    # ------------------------------------------------------------------

    def compare_multi_crop(
        self,
        actual_path: str,
        crop_items: list[dict],
        threshold_pass: float = 0.95,
        img_act=None,
    ) -> dict:
        """Compare multiple cropped expected images against a single actual screenshot.

        Returns per-crop sub-results. Overall status is fail if any crop fails.
        """
        self._require_cv2()
        img_act = self._resolve_img(img_act, actual_path)
        if img_act is None:
            return {"error": "Could not read actual image", "sub_results": []}

        sub_results = []

        for item in crop_items:
            img_exp = safe_imread(item["image"])
            if img_exp is None:
                # 비교 불가도 error 가 아니라 fail 로 분류하고 원인을 남긴다.
                sub_results.append({
                    "label": item.get("label", ""),
                    "expected_image": item.get("rel_path", ""),
                    "score": 0.0,
                    "status": "fail",
                    "reason": "기대 이미지 로드 실패",
                    "match_location": None,
                })
                continue

            roi = item.get("roi")
            if not roi:
                # ROI 정보가 없는 구버전 데이터는 비교 불가 (template matching 폐기)
                sub_results.append({
                    "label": item.get("label", ""),
                    "expected_image": item.get("rel_path", ""),
                    "score": 0.0,
                    "status": "fail",
                    "reason": "ROI 정보 없음(구버전 데이터)",
                    "match_location": None,
                })
                continue

            result = self._compare_at_roi(img_exp, img_act, roi)
            score = result["score"]
            if "error" in result:
                status = "fail"
                reason = result["error"]
            else:
                status = "pass" if score >= threshold_pass else "fail"
                reason = "" if status == "pass" else f"유사도 {score:.4f} < {threshold_pass:.4f}"

            sub_results.append({
                "label": item.get("label", ""),
                "expected_image": item.get("rel_path", ""),
                "score": score,
                "status": status,
                "reason": reason,
                "match_location": result.get("match_location"),
            })

        logger.info(
            "Multi-crop comparison: %d crops, results=%s",
            len(crop_items),
            [(sr["label"] or f"#{i}", sr["status"], sr["score"]) for i, sr in enumerate(sub_results)],
        )

        return {"sub_results": sub_results}

    # ------------------------------------------------------------------
    # Match-crop comparison (위치 무관 단일 템플릿 매칭)
    # ------------------------------------------------------------------

    def compare_match_crop(
        self,
        expected_path: str,
        actual_path: str,
        img_exp=None,
        img_act=None,
    ) -> dict:
        """저장된 크롭(expected)을 actual 전체 화면에서 template matching 으로 탐색.

        score 는 cv2.TM_CCOEFF_NORMED 의 최대 응답값(0.0~1.0)을 그대로 사용한다.
        같은 위치에서의 SSIM 도 함께 계산해 진단·diff 생성에 활용할 수 있도록 반환.
        match_location 은 매치된 영역의 (x, y, width, height) — 단일크롭과 동일 포맷.
        """
        self._require_cv2()
        img_exp = self._resolve_img(img_exp, expected_path)
        img_act = self._resolve_img(img_act, actual_path)
        if img_exp is None or img_act is None:
            return {"score": 0.0, "error": "Could not read one or both images"}

        # 1) template matching 으로 최고 매치 좌표/신뢰도 획득
        gray_exp = cv2.cvtColor(img_exp, cv2.COLOR_BGR2GRAY)
        gray_act = cv2.cvtColor(img_act, cv2.COLOR_BGR2GRAY)
        if gray_exp.shape[0] > gray_act.shape[0] or gray_exp.shape[1] > gray_act.shape[1]:
            return {
                "score": 0.0,
                "error": "Expected crop is larger than actual screenshot",
            }
        res = cv2.matchTemplate(gray_act, gray_exp, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(res)
        confidence = float(max_val)
        mx, my = int(max_loc[0]), int(max_loc[1])
        h, w = gray_exp.shape[:2]

        # 2) 매치된 영역의 SSIM (diff heatmap 용 — 보조 정보)
        cropped_act = img_act[my:my + h, mx:mx + w]
        if cropped_act.shape[:2] != img_exp.shape[:2]:
            # 경계 케이스 — 안전 차원에서 resize
            cropped_act = cv2.resize(cropped_act, (img_exp.shape[1], img_exp.shape[0]))
        gray_c_act = cv2.cvtColor(cropped_act, cv2.COLOR_BGR2GRAY)
        gray_exp_n = self._normalize_for_ssim(gray_exp)
        gray_act_n = self._normalize_for_ssim(gray_c_act)
        try:
            ssim_score, diff = ssim(gray_exp_n, gray_act_n, full=True)
            diff_uint8 = (diff * 255).astype("uint8")
        except Exception:
            ssim_score = 0.0
            diff_uint8 = None

        logger.info(
            "match_crop: confidence=%.4f ssim=%.4f match=(%d,%d,%dx%d) actual=%dx%d expected=%dx%d",
            confidence, float(ssim_score), mx, my, w, h,
            img_act.shape[1], img_act.shape[0], w, h,
        )

        return {
            "score": round(confidence, 4),
            "ssim_score": round(float(ssim_score), 4),
            "diff_array": diff_uint8,
            "match_location": {"x": mx, "y": my, "width": w, "height": h},
        }

    # ------------------------------------------------------------------
    # Level 2 — SSIM with status-bar masking
    # ------------------------------------------------------------------

    def compare_ssim_masked(
        self,
        expected_path: str,
        actual_path: str,
        mask_top_px: int = 80,
    ) -> dict:
        """SSIM comparison with top status bar masked out."""
        return self.compare_ssim(
            expected_path,
            actual_path,
            roi=None,  # masking is applied below
        )

    # ------------------------------------------------------------------
    # Level 3 — Template matching
    # ------------------------------------------------------------------

    def template_match(
        self,
        screenshot_path: str,
        template_path: str,
        threshold: float = 0.8,
    ) -> dict:
        """Check if a template image exists within a screenshot.

        Returns location and confidence score.
        """
        self._require_cv2()
        img = safe_imread(screenshot_path)
        tmpl = safe_imread(template_path)
        if img is None or tmpl is None:
            return {"found": False, "error": "Could not read one or both images"}

        img_gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        tmpl_gray = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)

        result = cv2.matchTemplate(img_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)

        found = float(max_val) >= threshold
        return {
            "found": found,
            "confidence": round(float(max_val), 4),
            "location": {"x": int(max_loc[0]), "y": int(max_loc[1])} if found else None,
        }

    # ------------------------------------------------------------------
    # Diff heatmap generation
    # ------------------------------------------------------------------

    def generate_diff_heatmap(
        self,
        expected_path: str,
        actual_path: str,
        output_path: str,
        roi: Optional[dict] = None,
        exclude_rois: Optional[list[dict]] = None,
        img_exp=None,
        img_act=None,
        diff_array=None,
    ) -> str:
        """Generate a heatmap PNG highlighting differences.

        diff_array가 주어지면 SSIM을 다시 계산하지 않고 재사용한다 (주로 judge() 이후
        FAIL 케이스에서 호출될 때 호출자가 이미 계산한 diff를 넘겨 중복 계산 회피).
        img_exp/img_act도 마찬가지로 재사용 가능.
        """
        if diff_array is not None:
            diff = diff_array
        else:
            if exclude_rois:
                result = self.compare_ssim_with_exclusions(
                    expected_path, actual_path, exclude_rois,
                    img_exp=img_exp, img_act=img_act,
                )
            else:
                result = self.compare_ssim(
                    expected_path, actual_path, roi=roi,
                    img_exp=img_exp, img_act=img_act,
                )
            if "error" in result:
                raise RuntimeError(result["error"])
            diff = result["diff_array"]
        # Invert so differences are bright
        diff_inv = 255 - diff
        heatmap = cv2.applyColorMap(diff_inv, cv2.COLORMAP_JET)

        # Gray out excluded regions in heatmap
        if exclude_rois:
            for roi_r in exclude_rois:
                rx, ry = roi_r["x"], roi_r["y"]
                rw, rh = roi_r["width"], roi_r["height"]
                heatmap[ry:ry + rh, rx:rx + rw] = (128, 128, 128)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        safe_imwrite(output_path, heatmap)
        return output_path

    def generate_multi_crop_annotated(
        self,
        actual_path: str,
        sub_results: list[dict],
        output_path: str,
        img_act=None,
    ) -> str:
        """Draw match boxes for each crop on the actual screenshot."""
        src = img_act if img_act is not None else safe_imread(actual_path)
        if src is None:
            raise RuntimeError("Could not read actual image")
        # 원본 훼손 방지용 복사 (호출자가 같은 ndarray를 다른 용도로 재사용할 수 있음)
        img = src.copy()

        for i, sr in enumerate(sub_results):
            loc = sr.get("match_location")
            if not loc:
                continue
            x, y = loc["x"], loc["y"]
            w, h = loc["width"], loc["height"]
            color = (0, 255, 0) if sr["status"] == "pass" else (0, 0, 255)
            cv2.rectangle(img, (x, y), (x + w, y + h), color, 3)
            label = sr.get("label") or f"#{i + 1}"
            cv2.putText(img, f"{label} {sr['score']:.2f}", (x, y - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        safe_imwrite(output_path, img)
        return output_path

    # ------------------------------------------------------------------
    # High-level judge
    # ------------------------------------------------------------------

    def judge(
        self,
        expected_path: str,
        actual_path: str,
        threshold_pass: float = 0.95,
        roi: Optional[dict] = None,
        compare_mode: str = "full",
        exclude_rois: Optional[list[dict]] = None,
        crop_items: Optional[list[dict]] = None,
        img_exp=None,
        img_act=None,
    ) -> dict:
        """Return pass/fail judgement with mode-aware dispatch.

        img_exp/img_act가 주어지면 내부 path 기반 imread를 생략한다.
        FAIL 케이스에서 호출자가 diff 재사용을 원하면 반환 dict의 diff_array를 활용.
        """

        # --- Multi-crop mode ---
        if compare_mode == "multi_crop" and crop_items:
            mc_result = self.compare_multi_crop(
                actual_path, crop_items,
                threshold_pass=threshold_pass,
                img_act=img_act,
            )
            if "error" in mc_result:
                return {"status": "error", "score": 0.0, "message": mc_result["error"], "sub_results": []}

            sub_results = mc_result["sub_results"]
            statuses = [sr["status"] for sr in sub_results]
            status = "fail" if ("fail" in statuses or "error" in statuses) else "pass"
            return {
                "status": status,
                "sub_results": sub_results,
            }

        # --- Match-crop mode (위치 무관 템플릿 매칭) ---
        if compare_mode == "match_crop":
            result = self.compare_match_crop(
                expected_path, actual_path,
                img_exp=img_exp, img_act=img_act,
            )
            if "error" in result:
                return {"status": "error", "score": 0.0, "message": result["error"]}
            score = result["score"]
            status = "pass" if score >= threshold_pass else "fail"
            out: dict = {"status": status, "score": score}
            if "match_location" in result:
                out["match_location"] = result["match_location"]
            if "diff_array" in result:
                out["diff_array"] = result["diff_array"]
            if "ssim_score" in result:
                out["ssim_score"] = result["ssim_score"]
            return out

        # --- Full-exclude mode ---
        if compare_mode == "full_exclude" and exclude_rois:
            result = self.compare_ssim_with_exclusions(
                expected_path, actual_path, exclude_rois,
                img_exp=img_exp, img_act=img_act,
            )
            if "error" in result:
                return {"status": "error", "score": 0.0, "message": result["error"]}
            score = result["score"]
            status = "pass" if score >= threshold_pass else "fail"
            out: dict = {"status": status, "score": score}
            if "diff_array" in result:
                out["diff_array"] = result["diff_array"]
            return out

        # --- Full / Single-crop mode (existing behavior) ---
        result = self.compare_ssim(
            expected_path, actual_path, roi=roi,
            img_exp=img_exp, img_act=img_act,
        )
        if "error" in result:
            return {"status": "error", "score": 0.0, "message": result["error"]}

        score = result["score"]
        status = "pass" if score >= threshold_pass else "fail"

        out: dict = {"status": status, "score": score}
        if "match_location" in result:
            out["match_location"] = result["match_location"]
        if "diff_array" in result:
            out["diff_array"] = result["diff_array"]
        return out
