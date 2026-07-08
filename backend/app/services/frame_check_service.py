"""Frame_Check 가상 모듈 — 녹화 영상 기반 동작 시간 측정.

시나리오에 Frame_Measure 스텝 하나로 측정 1건을 정의한다:
  - 시작점: mode='function'이면 스텝 실행 시각 자체, mode='image'면 스텝 실행 이후
    start_image 가 영상에 처음 나타난 프레임.
  - 타겟: 시작점 + wait_time(여유시간) 이후 target_image 가 처음 나타난 프레임.
    max_time 초를 넘기면 미발견 처리.

재생 중 스텝 실행은 마커(시각+파라미터)만 기록하고 즉시 반환한다. 실제 측정은
시나리오 종료 후 main.py 재생 잡이 웹캠 녹화 mp4를 프레임 단위로 스캔하여 수행하고,
결과는 ScenarioResult.frame_check_results 로 result.json/html 에 포함된다.
측정 구간 클립(시작 5초 전 ~ 종료 5초 후)도 pass/fail 무관하게 함께 제공한다.

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
DEFAULT_MAX_TIME_S = 60.0  # 타겟 탐색 최대 시간(초). 0 = 무제한
DEFAULT_WAIT_TIME_S = 0.0  # 시작점 이후 타겟 발생 전까지 여유시간(초) — 이 구간은 탐색 생략
CLIP_MARGIN_MS = 5000.0  # 결과 클립 여유 구간 — 시작점 5초 전 ~ 종료점 5초 후


@dataclass
class _Marker:
    """Frame_Measure 스텝 1회 실행 = 측정 1건."""
    ts_iso: str  # 스텝 실행 wall-clock (UTC ISO)
    iteration: int
    step_id: Optional[int]
    mode: str = "function"  # 시작점 기준: "function" | "image"
    start_image: str = ""  # SCREENSHOTS_DIR 기준 상대 경로 (mode='image'일 때 필수)
    start_threshold: float = DEFAULT_THRESHOLD
    wait_time_s: float = DEFAULT_WAIT_TIME_S  # 시작점 → 타겟 탐색 시작까지 여유시간(초)
    target_image: str = ""  # 필수
    target_threshold: float = DEFAULT_THRESHOLD
    max_time_s: float = DEFAULT_MAX_TIME_S  # 타겟 탐색 시작점부터 최대 확인 시간(초)


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
        if function_name != "Frame_Measure":
            return f"FAIL: unknown Frame_Check function '{function_name}'"

        mode = str(args.get("mode") or "function").strip().lower()
        if mode not in ("function", "image"):
            return f"FAIL: mode must be 'function' or 'image' (got '{mode}')"

        start_image = str(args.get("start_image") or "").strip().replace("\\", "/")
        if mode == "image":
            err = self._validate_image(start_image, "start_image")
            if err:
                return f"FAIL: {err}"

        target_image = str(args.get("target_image") or "").strip().replace("\\", "/")
        err = self._validate_image(target_image, "target_image")
        if err:
            return f"FAIL: {err}"

        step_id, iteration = self._current_iteration()
        marker = _Marker(
            ts_iso=datetime.now(timezone.utc).isoformat(),
            iteration=iteration,
            step_id=step_id,
            mode=mode,
            start_image=start_image,
            start_threshold=self._parse_threshold(args.get("start_threshold")),
            wait_time_s=self._parse_seconds(args.get("wait_time"), DEFAULT_WAIT_TIME_S),
            target_image=target_image,
            target_threshold=self._parse_threshold(args.get("target_threshold")),
            max_time_s=self._parse_seconds(args.get("max_time"), DEFAULT_MAX_TIME_S),
        )
        with self._lock:
            self._markers.append(marker)
        start_desc = f"start_image='{start_image}'" if mode == "image" else "스텝 실행 시점"
        limit = f"{marker.max_time_s:g}s" if marker.max_time_s > 0 else "무제한"
        return (f"측정 마커 기록 (mode={mode}, {start_desc}, wait={marker.wait_time_s:g}s, "
                f"target='{target_image}', max_time={limit}, cycle={iteration})")

    @staticmethod
    def _parse_seconds(raw: Any, default: float) -> float:
        """초 단위 인자 파싱. 빈 값/미지정/비숫자 = default, 음수는 0으로 클램프."""
        s = str(raw).strip().strip("'\"") if raw is not None else ""
        if not s:
            return default
        try:
            v = float(s)
        except ValueError:
            return default
        return max(0.0, v)

    @staticmethod
    def _parse_threshold(raw: Any) -> float:
        try:
            v = float(str(raw).strip().strip("'\""))
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD
        return min(1.0, max(0.0, v)) if v > 0 else DEFAULT_THRESHOLD

    @staticmethod
    def _validate_image(image: str, label: str) -> Optional[str]:
        if not image:
            return f"{label}가 설정되지 않았습니다 — 스텝 편집에서 웹캠 화면을 크롭해 지정하세요"
        p = SCREENSHOTS_DIR / image
        if not p.exists():
            return f"{label} 이미지 파일을 찾을 수 없습니다: {image}"
        return None

    # ---------- 영상 분석 (시나리오 종료 후) ----------

    def markers_for_iteration(self, iteration: int) -> list[_Marker]:
        with self._lock:
            return [m for m in self._markers if m.iteration == iteration]

    def iterations_with_markers(self) -> list[int]:
        with self._lock:
            return sorted({m.iteration for m in self._markers})

    def analyze_video(self, video_path: Path, started_at_iso: str,
                      iteration: int, assets_dir: Optional[Path] = None) -> list[dict]:
        """녹화 영상 1개(1 cycle)에서 마커별 측정 수행 (여러 측정 지원, 기록 순서대로).

        assets_dir 가 주어지면 pass 측정의 매치 프레임 ±5프레임 이미지를 그 폴더에
        저장하고 entry["frames"]/["match_image"] 에 파일명을 기록한다 (경로 rewrite 는
        호출자 몫 — 최종 위치는 run_dir/recordings/).
        """
        markers = self.markers_for_iteration(iteration)
        if not markers:
            return []

        results: list[dict] = []
        base_entry = {"iteration": iteration, "video": video_path.name}

        started_at = self._parse_iso(started_at_iso)
        if started_at is None:
            return [{**base_entry, **self._marker_info(m), "pair_index": i + 1,
                     "status": "no_recording_meta",
                     "message": "녹화 시작 시각(meta.json)을 알 수 없어 측정 불가"}
                    for i, m in enumerate(markers)]

        if not video_path.exists():
            return [{**base_entry, **self._marker_info(m), "pair_index": i + 1,
                     "status": "no_video", "message": "녹화 영상 파일 없음"}
                    for i, m in enumerate(markers)]

        from ..utils.cv2_loader import cv2  # 지연 임포트 (서버 기동 비용 회피)

        cap = cv2.VideoCapture(str(video_path))
        if not cap.isOpened():
            return [{**base_entry, **self._marker_info(m), "pair_index": i + 1,
                     "status": "video_open_failed", "message": "영상 파일을 열 수 없음"}
                    for i, m in enumerate(markers)]

        try:
            for idx, marker in enumerate(markers):
                entry: dict[str, Any] = {**base_entry, **self._marker_info(marker),
                                         "pair_index": idx + 1}
                asset_prefix = f"framecheck_r{iteration}_p{idx + 1}"
                try:
                    entry.update(self._measure(cv2, cap, marker, started_at,
                                               assets_dir=assets_dir,
                                               asset_prefix=asset_prefix))
                except Exception as e:
                    logger.exception("Frame_Check measurement %d analysis failed", idx + 1)
                    entry.update({"status": "error", "message": str(e)})
                results.append(entry)
        finally:
            cap.release()
        return results

    @staticmethod
    def _marker_info(m: _Marker) -> dict:
        return {
            "start_mode": m.mode,
            "start_image": m.start_image or None,
            "target_image": m.target_image,
            "step_id": m.step_id,
            "wait_time_s": m.wait_time_s,
            "max_time_s": m.max_time_s,
        }

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

    @staticmethod
    def _fmt_wall(started_at: datetime, offset_ms: float) -> str:
        """영상 내 오프셋(ms) → 로컬 wall-clock 문자열 (예: 2026-07-08 13:59:30.125)."""
        from datetime import timedelta
        dt = (started_at + timedelta(milliseconds=offset_ms)).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def _measure(self, cv2, cap, m: _Marker, started_at: datetime,
                 assets_dir: Optional[Path] = None, asset_prefix: str = "fc") -> dict:
        """마커 1건 측정.

        속도 최적화: 스텝 실행 시각 이전 구간과 시작점 이후 wait_time 구간은
        seek 로 점프한다. 타겟 탐색은 max_time 초를 넘기면 중단 — 영상이 길어도
        분석 시간이 유계.

        assets_dir 가 주어지면 pass 시 매치 프레임 앞뒤 5프레임 + 매치 프레임
        (빨간 박스·일치율 annotate)을 JPEG 로 저장한다.
        """
        marker_dt = self._parse_iso(m.ts_iso)
        if marker_dt is None:
            return {"status": "error", "message": "마커 시각 파싱 실패"}
        # 스텝 실행 시각 → 영상 내 오프셋(ms). 녹화 시작 전이면 0으로 클램프.
        exec_ms = max(0.0, (marker_dt - started_at).total_seconds() * 1000.0)

        tpl_target = self._load_template_gray(cv2, m.target_image)
        if tpl_target is None:
            return {"status": "error", "message": f"타겟 이미지 로드 실패: {m.target_image}"}

        tpl_start = None
        if m.mode == "image":
            tpl_start = self._load_template_gray(cv2, m.start_image)
            if tpl_start is None:
                return {"status": "error", "message": f"시작 이미지 로드 실패: {m.start_image}"}

        def _match(gray_frame, tpl) -> tuple[float, Optional[tuple[int, int]]]:
            th, tw = tpl.shape[:2]
            fh, fw = gray_frame.shape[:2]
            if th > fh or tw > fw:
                return -1.0, None
            res = cv2.matchTemplate(gray_frame, tpl, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            return float(max_val), (int(max_loc[0]), int(max_loc[1]))

        def _seek(ms: float) -> None:
            # FFMPEG seek 은 요청 시각 이전의 키프레임에 안착 → 이후 read 로 전진하며
            # pos < ms 프레임은 호출부 가드가 건너뜀 (프레임 누락 없음).
            if ms > 0:
                try:
                    cap.set(cv2.CAP_PROP_POS_MSEC, ms)
                except Exception:
                    pass

        # FFMPEG 백엔드는 read() **후**의 POS_MSEC가 방금 디코드한 프레임의 PTS다
        # (read 전에 읽으면 직전 프레임 값 — 1프레임 lag).
        def _scan(lower_ms: float, tpl, threshold: float, deadline_ms: float):
            """lower_ms 이후 프레임에서 tpl 최초 매치 탐색.

            반환 dict:
              pos/score/loc/frame — 매치 프레임 (미발견 시 None)
              last_pos — 마지막으로 본 프레임 PTS
              best_score — 탐색 구간에서 관측된 최고 유사도 (미발견 원인 진단용)
            """
            _seek(lower_ms)
            last_pos = lower_ms
            best_score = -1.0
            checked = 0  # 실제 매칭을 수행한 프레임 수 — 1이면 첫 프레임 즉시 매치
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                pos_ms = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                last_pos = pos_ms
                if pos_ms < lower_ms:
                    continue  # seek 가 키프레임(이전)에 안착한 구간 — 전진만
                if pos_ms > deadline_ms:
                    break
                checked += 1
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                score, loc = _match(gray, tpl)
                if score > best_score:
                    best_score = score
                if score >= threshold:
                    return {"pos": pos_ms, "score": round(score, 4), "loc": loc,
                            "frame": frame, "last_pos": last_pos,
                            "best_score": round(best_score, 4), "checked": checked}
            return {"pos": None, "score": None, "loc": None, "frame": None,
                    "last_pos": last_pos,
                    "best_score": round(best_score, 4) if best_score >= 0 else None,
                    "checked": checked}

        def _collect_pre_frames(match_pos_ms: float, count: int = 5) -> list:
            """매치 프레임 직전 count 프레임 수집 — 매치 지점 앞으로 re-seek 후 전진 디코드.

            탐색 중 롤링 버퍼 방식은 매치가 탐색 첫 프레임에서 나면(= wait_time 시점에
            타겟이 이미 떠 있으면) 직전 프레임이 하나도 없으므로, 항상 re-seek 로 확보한다.
            여유 구간(margin) 안에 프레임이 count 장 미만이면(초저 fps) 한 번 더 넓혀 재시도.
            """
            from collections import deque
            for margin_ms in (2000.0, 10000.0):
                if match_pos_ms <= 0:
                    return []
                buf: deque = deque(maxlen=count)
                _seek(max(0.0, match_pos_ms - margin_ms))
                while True:
                    ok, f = cap.read()
                    if not ok:
                        break
                    p = float(cap.get(cv2.CAP_PROP_POS_MSEC) or 0.0)
                    if p >= match_pos_ms - 0.001:  # 매치 프레임 자신은 제외
                        break
                    buf.append((p, f))
                if len(buf) >= count or match_pos_ms - margin_ms <= 0:
                    return list(buf)
            return list(buf)

        def _save_jpg(name: str, img) -> Optional[str]:
            if assets_dir is None:
                return None
            try:
                from ..utils.cv_io import safe_imwrite
                path = assets_dir / name
                if safe_imwrite(str(path), img):
                    return name
            except Exception as e:
                logger.warning("Frame_Check frame save failed (%s): %s", name, e)
            return None

        # 결과 클립 구간 — pass/fail 무관하게 스텝 실행 5초 전부터 제공.
        # 종료점은 결과에 따라 아래에서 확정.
        clip_from_ms = max(0.0, exec_ms - CLIP_MARGIN_MS)

        # 1) 시작점 — mode=function 이면 스텝 실행 시각 자체, mode=image 면
        #    실행 시각부터 시작 이미지 최초 등장 프레임 탐색 (이전 구간은 seek 점프).
        start_score: Optional[float] = None
        if m.mode == "function":
            start_video_ms: Optional[float] = exec_ms
        else:
            s = _scan(exec_ms, tpl_start, m.start_threshold, float("inf"))
            start_video_ms, start_score = s["pos"], s["score"]
            if start_video_ms is None:
                return {"status": "start_image_not_found",
                        "message": "시작 이미지가 영상에서 발견되지 않음",
                        "search_from_ms": round(exec_ms, 1),
                        # 미발견이어도 관측된 최고 유사도를 남겨 원인 진단에 사용
                        "start_score": s["best_score"],
                        "clip_from_ms": round(clip_from_ms, 1),
                        "clip_to_ms": round(s["last_pos"] + CLIP_MARGIN_MS, 1)}

        # 2) 타겟 — 시작점 + wait_time(여유시간) 이전은 점프하고, max_time 초까지만 탐색.
        target_from_ms = start_video_ms + m.wait_time_s * 1000.0
        deadline_ms = (
            target_from_ms + m.max_time_s * 1000.0
            if m.max_time_s > 0 else float("inf")
        )
        t = _scan(target_from_ms, tpl_target, m.target_threshold, deadline_ms)
        target_video_ms, target_score = t["pos"], t["score"]

        if target_video_ms is None:
            timed_out = deadline_ms != float("inf") and t["last_pos"] > deadline_ms
            msg = (
                f"타겟 이미지 미발견 — 최대 확인 시간 {m.max_time_s:g}초 초과"
                if timed_out else "타겟 이미지가 탐색 구간 영상에서 발견되지 않음"
            )
            return {"status": "target_not_found",
                    "message": msg,
                    "start_video_ms": round(start_video_ms, 1),
                    "start_time": self._fmt_wall(started_at, start_video_ms),
                    "start_score": start_score,
                    # 미발견이어도 탐색 구간의 최고 유사도를 남겨 원인 진단에 사용
                    "target_score": t["best_score"],
                    "search_from_ms": round(target_from_ms, 1),
                    "clip_from_ms": round(clip_from_ms, 1),
                    # fail 클립은 실제로 탐색한 구간 전체를 담는다 (마지막 확인 프레임 + 5초)
                    "clip_to_ms": round(t["last_pos"] + CLIP_MARGIN_MS, 1)}

        # pass — 매치 프레임 앞뒤 5프레임 + annotate 된 매치 프레임 저장
        frames_out: list[str] = []
        match_image: Optional[str] = None
        if assets_dir is not None:
            # 1) 매치 이후 5프레임 먼저 — cap 이 매치 프레임 직후에 위치해 있어 이어서 read
            #    (직전 프레임 수집이 re-seek 로 cap 위치를 바꾸므로 순서 중요)
            post_names: list[str] = []
            for i in range(1, 6):
                ok, f2 = cap.read()
                if not ok:
                    break
                name = _save_jpg(f"{asset_prefix}_post{i}.jpg", f2)
                if name:
                    post_names.append(name)
            # 2) 매치 직전 5프레임 — re-seek 로 확정 수집 (탐색 첫 프레임 매치여도 확보)
            prev = _collect_pre_frames(target_video_ms, 5)
            for i, (_p, f) in enumerate(prev):
                name = _save_jpg(f"{asset_prefix}_pre{len(prev) - i}.jpg", f)
                if name:
                    frames_out.append(name)
            # 3) 매치 프레임 annotate (빨간 박스 + 일치율)
            annotated = t["frame"].copy()
            if t["loc"] is not None:
                x, y = t["loc"]
                th_, tw_ = tpl_target.shape[:2]
                cv2.rectangle(annotated, (x, y), (x + tw_, y + th_), (0, 0, 255), 3)
                cv2.putText(annotated, f"{target_score * 100:.1f}%",
                            (x, max(y - 10, 25)), cv2.FONT_HERSHEY_SIMPLEX,
                            0.9, (0, 0, 255), 2, cv2.LINE_AA)
            match_image = _save_jpg(f"{asset_prefix}_match.jpg", annotated)
            if match_image:
                frames_out.append(match_image)
            frames_out.extend(post_names)

        result = {
            "status": "ok",
            "start_video_ms": round(start_video_ms, 1),
            "target_video_ms": round(target_video_ms, 1),
            "start_time": self._fmt_wall(started_at, start_video_ms),
            "target_time": self._fmt_wall(started_at, target_video_ms),
            "elapsed_ms": round(target_video_ms - start_video_ms, 1),
            "start_score": start_score,
            "target_score": target_score,
            "clip_from_ms": round(clip_from_ms, 1),
            "clip_to_ms": round(target_video_ms + CLIP_MARGIN_MS, 1),
        }
        # 탐색 첫 프레임에서 즉시 매치 = 타겟이 탐색 시작 시점에 이미 표시되어 있었다는 뜻.
        # 측정 시작점이 실제 등장보다 늦었을 가능성이 높으므로 경고를 남긴다
        # (Frame_Measure 스텝을 트리거 동작 뒤에 배치했거나 wait_time 이 과함).
        if t.get("checked") == 1:
            result["immediate_match"] = True
            result["message"] = (
                "주의: 탐색 시작 프레임에서 즉시 매치 — 타겟이 측정 시작 전에 이미 표시되었을 수 "
                "있습니다. Frame_Measure 스텝을 트리거 동작 이전에 배치하거나 wait_time 을 줄이세요."
            )
        if match_image:
            result["match_image"] = match_image
        if frames_out:
            result["frames"] = frames_out
        return result

    # ---------- 결과 클립 추출 ----------

    @staticmethod
    def extract_clip(src: Path, dst: Path, from_ms: float, to_ms: float) -> bool:
        """녹화 영상에서 측정 구간(±여유 포함) 클립을 추출한다.

        스트림 카피는 키프레임(-g 미지정 x264 = 최대 250프레임) 경계로만 잘려
        구간이 수 초씩 어긋나므로 재인코딩 사용 — 클립은 수십 초라 비용이 작다.
        """
        if to_ms <= from_ms or not src.exists():
            return False
        try:
            from .webcam_service import _find_ffmpeg
            ffmpeg = _find_ffmpeg()
        except Exception:
            ffmpeg = None
        if not ffmpeg:
            logger.warning("Frame_Check clip: ffmpeg not found — skip %s", dst.name)
            return False
        import subprocess
        import sys as _sys
        dur_s = (to_ms - from_ms) / 1000.0
        cmd = [
            ffmpeg, "-y",
            "-ss", f"{max(0.0, from_ms) / 1000.0:.3f}",
            "-i", str(src),
            "-t", f"{dur_s:.3f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            str(dst),
        ]
        try:
            flags = subprocess.CREATE_NO_WINDOW if _sys.platform == "win32" else 0
            r = subprocess.run(cmd, capture_output=True, timeout=300, creationflags=flags)
            if r.returncode != 0:
                logger.warning("Frame_Check clip ffmpeg rc=%d: %s", r.returncode,
                               r.stderr.decode(errors="replace")[-300:])
                return False
            return dst.exists() and dst.stat().st_size > 0
        except Exception as e:
            logger.warning("Frame_Check clip extraction failed (%s): %s", dst.name, e)
            return False


_service: Optional[FrameCheckService] = None
_service_lock = threading.Lock()


def get_frame_check_service() -> FrameCheckService:
    global _service
    with _service_lock:
        if _service is None:
            _service = FrameCheckService()
        return _service
