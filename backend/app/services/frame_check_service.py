"""Frame_Check 가상 모듈 — 녹화 영상 기반 동작 시간 측정.

시나리오에 두 종류의 마커 스텝을 추가한다:
  - MeasureStart (측정 시작점): mode='function'이면 스텝 실행 시각 자체가 시작점,
    mode='image'면 스텝 실행 이후 시작 이미지가 영상에 처음 나타난 프레임이 시작점.
  - MeasureEnd (타겟 이미지 시점): 시작점 이후 타겟 이미지가 영상에 처음 나타난 프레임.

재생 중 스텝 실행은 마커(시각+파라미터)만 기록하고 즉시 반환한다. 실제 측정은
시나리오 종료 후 main.py 재생 잡이 웹캠 녹화 mp4를 프레임 단위로 스캔하여 수행하고,
결과는 ScenarioResult.frame_check_results 로 result.json/html 에 포함된다.

프레임 시각 변환: 웹캠 녹화는 VFR(-use_wallclock_as_timestamps)이므로 mp4 PTS가
실제 도착 시각과 1:1 — 프레임 wall-clock = meta.json started_at + 프레임 PTS(ms).
마커의 wall-clock 시각을 같은 기준으로 영상 내 오프셋으로 변환해 탐색 하한으로 쓴다.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# backend/screenshots — 크롭 이미지는 "{scenario}/{filename}" 상대 경로로 저장/참조
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "screenshots"

DEFAULT_THRESHOLD = 0.8


@dataclass
class _Marker:
    kind: str  # "start" | "target"
    ts_iso: str  # 스텝 실행 wall-clock (UTC ISO)
    iteration: int
    step_id: Optional[int]
    mode: str = "function"  # start 전용: "function" | "image"
    image: str = ""  # SCREENSHOTS_DIR 기준 상대 경로 (예: "MyScenario/framecheck_123.png")
    threshold: float = DEFAULT_THRESHOLD


@dataclass
class FrameCheckService:
    """재생 1회(play job) 동안의 Frame_Check 마커 저장소 + 영상 분석기."""

    _markers: list[_Marker] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---------- 마커 기록 (스텝 실행 시) ----------

    def reset(self) -> None:
        with self._lock:
            self._markers.clear()

    def has_markers(self) -> bool:
        with self._lock:
            return bool(self._markers)

    def has_measurable_pair(self) -> bool:
        """start와 target 마커가 최소 1개씩 있어야 측정 가능."""
        with self._lock:
            kinds = {m.kind for m in self._markers}
        return "start" in kinds and "target" in kinds

    def _current_iteration(self) -> tuple[Optional[int], int]:
        try:
            from .playback_service import get_current_step_context
            return get_current_step_context()
        except Exception:
            return None, 1

    def execute_step(self, function_name: str, args: dict) -> str:
        """module_service._execute_sync 에서 호출되는 가상 함수 디스패치.

        반환 문자열이 "FAIL:"로 시작하면 스텝이 fail 처리된다 (playback 규약).
        """
        threshold = self._parse_threshold(args.get("threshold"))
        image = str(args.get("image") or "").strip().replace("\\", "/")
        step_id, iteration = self._current_iteration()
        now_iso = datetime.now(timezone.utc).isoformat()

        if function_name == "MeasureStart":
            mode = str(args.get("mode") or "function").strip().lower()
            if mode not in ("function", "image"):
                return f"FAIL: MeasureStart mode must be 'function' or 'image' (got '{mode}')"
            if mode == "image":
                err = self._validate_image(image)
                if err:
                    return f"FAIL: {err}"
            marker = _Marker(kind="start", ts_iso=now_iso, iteration=iteration,
                             step_id=step_id, mode=mode, image=image, threshold=threshold)
            with self._lock:
                self._markers.append(marker)
            detail = f"image='{image}'" if mode == "image" else "함수 실행 시점"
            return f"측정 시작점 기록 (mode={mode}, {detail}, cycle={iteration})"

        if function_name == "MeasureEnd":
            err = self._validate_image(image)
            if err:
                return f"FAIL: {err}"
            marker = _Marker(kind="target", ts_iso=now_iso, iteration=iteration,
                             step_id=step_id, mode="image", image=image, threshold=threshold)
            with self._lock:
                self._markers.append(marker)
            return f"타겟 이미지 시점 기록 (image='{image}', cycle={iteration})"

        return f"FAIL: unknown Frame_Check function '{function_name}'"

    @staticmethod
    def _parse_threshold(raw: Any) -> float:
        try:
            v = float(str(raw).strip().strip("'\""))
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD
        return min(1.0, max(0.0, v)) if v > 0 else DEFAULT_THRESHOLD

    @staticmethod
    def _validate_image(image: str) -> Optional[str]:
        if not image:
            return "이미지가 설정되지 않았습니다 — 스텝 편집에서 웹캠 화면을 크롭해 지정하세요"
        p = SCREENSHOTS_DIR / image
        if not p.exists():
            return f"이미지 파일을 찾을 수 없습니다: {image}"
        return None

    # ---------- 영상 분석 (시나리오 종료 후) ----------

    def markers_for_iteration(self, iteration: int) -> list[_Marker]:
        with self._lock:
            return [m for m in self._markers if m.iteration == iteration]

    def iterations_with_markers(self) -> list[int]:
        with self._lock:
            return sorted({m.iteration for m in self._markers})

    def analyze_video(self, video_path: Path, started_at_iso: str,
                      iteration: int) -> list[dict]:
        """녹화 영상 1개(1 cycle)를 프레임 단위로 스캔해 start→target 쌍별 측정 결과 반환.

        마커는 기록 순서대로 start→target 쌍을 이룬다 (여러 쌍 지원).
        탐색은 단일 순차 디코드 패스 — 각 쌍의 탐색 하한(ms) 이후 프레임에만
        matchTemplate 을 수행하고, 쌍이 완료되면 다음 쌍으로 넘어간다.
        """
        markers = self.markers_for_iteration(iteration)
        pairs = self._build_pairs(markers)
        if not pairs:
            return []

        results: list[dict] = []
        base_entry = {"iteration": iteration, "video": video_path.name}

        started_at = self._parse_iso(started_at_iso)
        if started_at is None:
            return [{**base_entry, **self._pair_info(s, t), "pair_index": i + 1,
                     "status": "no_recording_meta",
                     "message": "녹화 시작 시각(meta.json)을 알 수 없어 측정 불가"}
                    for i, (s, t) in enumerate(pairs)]

        if not video_path.exists():
            return [{**base_entry, **self._pair_info(s, t), "pair_index": i + 1,
                     "status": "no_video", "message": "녹화 영상 파일 없음"}
                    for i, (s, t) in enumerate(pairs)]

        from ..utils.cv2_loader import cv2  # 지연 임포트 (서버 기동 비용 회피)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return [{**base_entry, **self._pair_info(s, t), "pair_index": i + 1,
                     "status": "video_open_failed", "message": "영상 파일을 열 수 없음"}
                    for i, (s, t) in enumerate(pairs)]

        try:
            for idx, (start_m, target_m) in enumerate(pairs):
                entry: dict[str, Any] = {**base_entry, **self._pair_info(start_m, target_m),
                                         "pair_index": idx + 1}
                try:
                    entry.update(self._measure_pair(cv2, cap, start_m, target_m, started_at))
                except Exception as e:
                    logger.exception("Frame_Check pair %d analysis failed", idx + 1)
                    entry.update({"status": "error", "message": str(e)})
                results.append(entry)
        finally:
            cap.release()
        return results

    @staticmethod
    def _pair_info(start_m: _Marker, target_m: _Marker) -> dict:
        return {
            "start_mode": start_m.mode,
            "start_image": start_m.image or None,
            "target_image": target_m.image,
            "start_step_id": start_m.step_id,
            "target_step_id": target_m.step_id,
        }

    @staticmethod
    def _build_pairs(markers: list[_Marker]) -> list[tuple[_Marker, _Marker]]:
        """기록 순서대로 start → 다음 target 을 쌍으로 묶는다."""
        pairs: list[tuple[_Marker, _Marker]] = []
        pending_start: Optional[_Marker] = None
        for m in markers:
            if m.kind == "start":
                pending_start = m  # 연속 start 는 마지막 것을 사용
            elif m.kind == "target" and pending_start is not None:
                pairs.append((pending_start, m))
                pending_start = None
        return pairs

    @staticmethod
    def _parse_iso(raw: str) -> Optional[datetime]:
        if not raw:
            return None
        try:
            s = raw.strip()
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except Exception:
            return None

    def _load_template_gray(self, cv2, image_rel: str):
        from ..utils.cv_io import safe_imread
        img = safe_imread(str(SCREENSHOTS_DIR / image_rel))
        if img is None:
            return None
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    def _measure_pair(self, cv2, cap, start_m: _Marker, target_m: _Marker,
                      started_at: datetime) -> dict:
        """단일 start→target 쌍 측정. cap 은 현재 위치에서 이어서 순차 스캔."""
        start_marker_dt = self._parse_iso(start_m.ts_iso)
        if start_marker_dt is None:
            return {"status": "error", "message": "시작 마커 시각 파싱 실패"}
        # 스텝 실행 시각 → 영상 내 오프셋(ms). 녹화 시작 전이면 0으로 클램프.
        start_lower_ms = max(0.0, (start_marker_dt - started_at).total_seconds() * 1000.0)

        tpl_target = self._load_template_gray(cv2, target_m.image)
        if tpl_target is None:
            return {"status": "error", "message": f"타겟 이미지 로드 실패: {target_m.image}"}

        tpl_start = None
        if start_m.mode == "image":
            tpl_start = self._load_template_gray(cv2, start_m.image)
            if tpl_start is None:
                return {"status": "error", "message": f"시작 이미지 로드 실패: {start_m.image}"}

        start_video_ms: Optional[float] = None
        start_score: Optional[float] = None
        if start_m.mode == "function":
            # 함수 실행 시점 자체가 시작점 — 프레임 탐색 불필요
            start_video_ms = start_lower_ms

        target_video_ms: Optional[float] = None
        target_score: Optional[float] = None

        def _match(gray_frame, tpl) -> float:
            th, tw = tpl.shape[:2]
            fh, fw = gray_frame.shape[:2]
            if th > fh or tw > fw:
                return -1.0
            res = cv2.matchTemplate(gray_frame, tpl, cv2.TM_CCOEFF_NORMED)
            return float(cv2.minMaxLoc(res)[1])

        # 단일 순차 디코드 패스. FFMPEG 백엔드는 read() **후**의 POS_MSEC가
        # 방금 디코드한 프레임의 PTS다 (read 전에 읽으면 직전 프레임 값 — 1프레임 lag).
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pos_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
            gray = None
            # 1) 시작 이미지 탐색 (mode=image, 아직 미발견)
            if start_video_ms is None:
                if pos_ms < start_lower_ms:
                    continue
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                score = _match(gray, tpl_start)
                if score >= start_m.threshold:
                    start_video_ms = pos_ms
                    start_score = round(score, 4)
                else:
                    continue
            # 2) 타겟 이미지 탐색 — 시작점 이후 프레임부터
            if pos_ms < start_video_ms:
                continue
            if gray is None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            score = _match(gray, tpl_target)
            if score >= target_m.threshold:
                target_video_ms = pos_ms
                target_score = round(score, 4)
                break

        if start_video_ms is None:
            return {"status": "start_image_not_found",
                    "message": "시작 이미지가 영상에서 발견되지 않음",
                    "search_from_ms": round(start_lower_ms, 1)}
        if target_video_ms is None:
            return {"status": "target_not_found",
                    "message": "타겟 이미지가 시작점 이후 영상에서 발견되지 않음",
                    "start_video_ms": round(start_video_ms, 1),
                    "start_score": start_score}
        return {
            "status": "ok",
            "start_video_ms": round(start_video_ms, 1),
            "target_video_ms": round(target_video_ms, 1),
            "elapsed_ms": round(target_video_ms - start_video_ms, 1),
            "start_score": start_score,
            "target_score": target_score,
        }


_service: Optional[FrameCheckService] = None
_service_lock = threading.Lock()


def get_frame_check_service() -> FrameCheckService:
    global _service
    with _service_lock:
        if _service is None:
            _service = FrameCheckService()
        return _service
