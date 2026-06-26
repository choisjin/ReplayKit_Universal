"""Scenario management API routes."""

import base64
import json
import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

from ..dependencies import adb_service as adb_svc
from ..dependencies import device_manager as dm
from ..dependencies import playback_service as playback_svc
from ..dependencies import recording_service as recording_svc
from ..models.scenario import ROI, CompareMode, CropItem, Scenario, StepType
from ..services.image_compare_service import ImageCompareService
from ..services.recording_service import SCREENSHOTS_DIR

router = APIRouter(prefix="/api/scenario", tags=["scenario"])


# ------------------------------------------------------------------
# Recording
# ------------------------------------------------------------------

class StartRecordingRequest(BaseModel):
    name: str
    description: str = ""


class AddStepRequest(BaseModel):
    type: StepType
    device_id: str = ""
    params: dict
    description: str = ""
    delay_after_ms: int = 3000
    roi: Optional[dict] = None
    similarity_threshold: float = 0.95
    skip_execute: bool = False


@router.post("/record/start")
async def start_recording(req: StartRecordingRequest):
    """Start a new recording session."""
    try:
        scenario = await recording_svc.start_recording(req.name, req.description)
        return {"status": "recording", "scenario": scenario.model_dump()}
    except (RuntimeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/record/step")
async def add_step(req: AddStepRequest):
    """Add a step to the current recording."""
    try:
        step, response = await recording_svc.add_step(
            step_type=req.type,
            params=req.params,
            device_id=req.device_id,
            description=req.description,
            delay_after_ms=req.delay_after_ms,
            roi=req.roi,
            similarity_threshold=req.similarity_threshold,
            skip_execute=req.skip_execute,
        )
        result = {"status": "ok", "step": step.model_dump()}
        if response is not None:
            result["response"] = response
        return result
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


class ResumeRecordingRequest(BaseModel):
    name: str


@router.post("/record/resume")
async def resume_recording(req: ResumeRecordingRequest):
    """Resume recording on an existing scenario."""
    try:
        scenario = await recording_svc.resume_recording(req.name)
        return {"status": "recording", "scenario": scenario.model_dump()}
    except (RuntimeError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/record/stop")
async def stop_recording():
    """Stop recording and save the scenario."""
    try:
        scenario = await recording_svc.stop_recording()
        return {"status": "saved", "scenario": scenario.model_dump()}
    except RuntimeError as e:
        raise HTTPException(status_code=400, detail=str(e))


class DeleteStepRequest(BaseModel):
    step_index: int  # 0-based


def _prune_device_map(scenario) -> None:
    """현재 steps에서 더 이상 참조되지 않는 device_id를 device_map에서 제거.

    스텝의 device_id와 screenshot_device_id를 모두 used set으로 모은 뒤
    device_map의 키 중 used에 없는 것들을 제거한다. 호출 시 in-place 수정.
    """
    if not getattr(scenario, "device_map", None):
        return
    used: set[str] = set()
    for s in scenario.steps:
        if getattr(s, "device_id", None):
            used.add(s.device_id)
        sd = getattr(s, "screenshot_device_id", None)
        if sd:
            used.add(sd)
    scenario.device_map = {k: v for k, v in scenario.device_map.items() if k in used}


@router.post("/record/delete-step")
async def delete_step(req: DeleteStepRequest):
    """Delete a step from the current recording session."""
    if not recording_svc.is_recording or not recording_svc._current_scenario:
        raise HTTPException(status_code=400, detail="Not recording")
    scenario = recording_svc._current_scenario
    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")
    removed = scenario.steps.pop(req.step_index)
    # Re-number step IDs sequentially
    for i, step in enumerate(scenario.steps):
        step.id = i + 1
    # device_map에서 사라진 디바이스 정리
    _prune_device_map(scenario)
    await recording_svc.save_scenario(scenario)
    return {"status": "ok", "removed_step_id": removed.id, "remaining": len(scenario.steps)}


class UpdateStepRequest(BaseModel):
    scenario_name: str
    step_index: int
    updates: dict  # e.g. {"delay_after_ms": 5000}


@router.post("/record/update-step")
async def update_step(req: UpdateStepRequest):
    """시나리오 스텝의 속성을 업데이트 (딜레이 등)."""
    scenario = await _resolve_scenario(req.scenario_name)
    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")
    step = scenario.steps[req.step_index]
    for k, v in req.updates.items():
        if hasattr(step, k):
            setattr(step, k, v)
    # device_id/screenshot_device_id 변경 시 잔존 매핑 정리
    _prune_device_map(scenario)
    await recording_svc.save_scenario(scenario)
    return {"status": "ok"}


@router.get("/record/status")
async def recording_status():
    """Check if recording is in progress."""
    return {"recording": recording_svc.is_recording}


class SyncStepsRequest(BaseModel):
    scenario_name: str
    steps: list[dict]  # 프론트엔드의 현재 steps 배열 (재정렬·복사·이동 결과)


@router.post("/record/sync-steps")
async def sync_steps(req: SyncStepsRequest):
    """프론트엔드의 현재 steps 상태를 in-memory 시나리오로 즉시 반영.

    녹화 중 사용자가 프론트에서 이동/복사/재정렬한 결과가 백엔드 _current_scenario.steps
    와 어긋나 step_index 기반 API(capture-expected-image 등)가 오류를 내는 문제를
    해결하기 위한 동기화 경로.
    id는 프론트엔드가 1-based 로 재할당해 보낸 값을 그대로 사용한다.
    """
    if not recording_svc.is_recording or not recording_svc._current_scenario:
        raise HTTPException(status_code=400, detail="Not recording")
    cur = recording_svc._current_scenario
    if cur.name != req.scenario_name:
        raise HTTPException(status_code=400, detail=f"Scenario mismatch: recording='{cur.name}', requested='{req.scenario_name}'")

    from ..models.scenario import Step
    new_steps: list[Step] = []
    for i, raw in enumerate(req.steps):
        try:
            # 프론트 임의 필드(_imageVer 등) 무시하도록 pydantic이 기본 처리
            s = Step(**{k: v for k, v in raw.items() if not str(k).startswith("_")})
            s.id = i + 1  # 안전 차원에서 backend도 1-based 재할당
            new_steps.append(s)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Step {i} invalid: {e}")

    cur.steps = new_steps
    # step_counter도 맞춰 다음 addStep이 올바른 id를 가지도록 보정
    recording_svc._step_counter = len(new_steps)
    # 사용되지 않는 device_map 항목 정리 (스텝 이동·삭제 후 잔존 매핑 제거)
    _prune_device_map(cur)
    return {"status": "ok", "count": len(new_steps)}


class SaveExpectedImageRequest(BaseModel):
    scenario_name: str
    step_index: int  # 0-based
    image_base64: str  # PNG base64 data (without data:image/png;base64, prefix)
    crop: Optional[dict] = None  # {x, y, width, height} in image pixels
    compare_mode: Optional[str] = None  # 분기/저장 모드 — "multi_crop"이면 추가, "match_crop"이면 step.compare_mode 보존
    crop_label: str = ""  # label for multi_crop item
    preserve_crops: bool = False  # True: multi_crop items 유지 (multi_crop base 이미지 갱신 시 사용)
    screen_type: Optional[str] = None  # 캡처 시점의 화면 선택값 (rear_left 등) — 스텝에 저장


async def _resolve_scenario(scenario_name: str):
    """Get scenario from in-memory recording or disk.

    녹화 중이고 이름이 일치하면 메모리 버전을 반환한다 — 미저장 스텝 포함.
    이름이 다르면 (다른 시나리오 참조) 디스크에서 로드.
    """
    cur = recording_svc._current_scenario
    if cur and cur.name == scenario_name:
        return cur
    try:
        return await recording_svc.load_scenario(scenario_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Scenario '{scenario_name}' not found")


@router.post("/record/save-expected-image")
async def save_expected_image(req: SaveExpectedImageRequest):
    """Manually save an expected image for a step."""
    scenario = await _resolve_scenario(req.scenario_name)

    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

    step = scenario.steps[req.step_index]

    # Decode base64 PNG
    try:
        raw = req.image_base64
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        png_bytes = base64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    # Optionally crop
    if req.crop:
        import cv2
        import numpy as np
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="Cannot decode image")
        x, y, w, h = req.crop["x"], req.crop["y"], req.crop["width"], req.crop["height"]
        cropped = img[y:y + h, x:x + w]
        _, png_bytes = cv2.imencode(".png", cropped)
        png_bytes = png_bytes.tobytes()

    save_dir = SCREENSHOTS_DIR / req.scenario_name
    save_dir.mkdir(parents=True, exist_ok=True)

    if req.compare_mode == "multi_crop":
        # Multi-crop: append to expected_images list
        crop_idx = len(step.expected_images)
        filename = f"{req.scenario_name}_step_{step.id:03d}_crop_{crop_idx:02d}.png"
        (save_dir / filename).write_bytes(png_bytes)
        crop_roi = ROI(x=int(req.crop["x"]), y=int(req.crop["y"]),
                       width=int(req.crop["width"]), height=int(req.crop["height"])) if req.crop else None
        step.expected_images.append(CropItem(image=filename, label=req.crop_label, roi=crop_roi))
    else:
        # Single image (full / single_crop / full_exclude / multi_crop base)
        # 타임스탬프로 캐시 충돌 방지 (capture-expected-image와 동일 패턴)
        import time as _time
        ts = int(_time.time() * 1000) % 1000000
        filename = f"{req.scenario_name}_step_{step.id:03d}_{ts}.png"
        # 이전 기대이미지 파일 삭제
        if step.expected_image and step.expected_image != filename:
            old_file = save_dir / step.expected_image
            if old_file.exists():
                old_file.unlink(missing_ok=True)
        # 이전 multi_crop 이미지 파일 삭제 + 관련 필드 초기화
        # (이전 모드가 multi_crop / full_exclude였다면 stale ROI가 렌더링에 끼어들어
        # 다른 스텝 ROI처럼 보이는 버그 방지). preserve_crops=True면 유지 (multi_crop base 갱신용)
        if not req.preserve_crops:
            for ci in step.expected_images:
                if ci.image:
                    old_crop = save_dir / ci.image
                    if old_crop.exists():
                        old_crop.unlink(missing_ok=True)
            step.expected_images.clear()
        # single_crop (crop 있음) 저장 시에만 exclude_rois 초기화 — 이전 full_exclude 잔재 제거
        if req.crop:
            step.exclude_rois.clear()
        (save_dir / filename).write_bytes(png_bytes)
        step.expected_image = filename
        if req.crop:
            step.roi = ROI(x=int(req.crop["x"]), y=int(req.crop["y"]),
                           width=int(req.crop["width"]), height=int(req.crop["height"]))
        else:
            step.roi = None

    # 캡처 시점의 화면 값 저장 — rear_left/rear_right에서 저장한 기대이미지가
    # 재생 시 front_center 로 잘못 비교되는 문제 방지.
    if req.screen_type:
        step.screen_type = req.screen_type

    # 비교 모드 명시 저장 — 프론트가 selectCompareMode 로 미리 sync 했더라도
    # 클라이언트가 명시적으로 compare_mode 를 전달하면 동기화 누락 방지 차원에서
    # 다시 설정한다 (특히 match_crop / single_crop 등 crop 기반 모드).
    if req.compare_mode and req.compare_mode in {"single_crop", "match_crop", "full_exclude", "multi_crop", "full"}:
        try:
            step.compare_mode = CompareMode(req.compare_mode)
        except ValueError:
            pass

    await recording_svc.save_scenario(scenario)
    return {"status": "ok", "filename": filename, "step_id": step.id}


class CaptureExpectedImageRequest(BaseModel):
    scenario_name: str
    step_index: int  # 0-based
    device_id: str  # ADB serial or HKMC device ID to take screenshot from
    screen_type: str = "front_center"  # HKMC screen type
    crop: Optional[dict] = None  # {x, y, width, height} in device pixels
    compare_mode: Optional[str] = None  # "multi_crop" to append
    crop_label: str = ""
    preserve_crops: bool = False  # True이면 기존 multi_crop 이미지 보존


@router.post("/record/capture-expected-image")
async def capture_expected_image(req: CaptureExpectedImageRequest):
    """Capture a screenshot from the device and save as expected image."""
    scenario = await _resolve_scenario(req.scenario_name)

    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

    step = scenario.steps[req.step_index]

    # Resolve device and take screenshot
    dev = dm.get_device(req.device_id)
    try:
        if dev and dev.type == "hkmc_agent":
            hkmc = dm.get_hkmc_service(req.device_id)
            if not hkmc:
                raise HTTPException(status_code=400, detail=f"HKMC device {req.device_id} not connected")
            png_bytes = await hkmc.async_screencap_bytes(screen_type=req.screen_type, fmt="png")
        elif dev and dev.type == "isap_agent":
            isap = dm.get_isap_service(req.device_id)
            if not isap:
                raise HTTPException(status_code=400, detail=f"iSAP device {req.device_id} not connected")
            png_bytes = await isap.async_screencap_bytes(screen_type=req.screen_type, fmt="png")
        elif dev and dev.type == "icas_agent":
            icas = dm.get_icas_service(req.device_id)
            if not icas:
                raise HTTPException(status_code=400, detail=f"ICAS device {req.device_id} not connected")
            # ICAS 기본 화면은 HU. HKMC 호환으로 기본이 front_center로 들어올 수 있어 변환.
            st = req.screen_type if req.screen_type in ("HU", "IID", "HUD") else "HU"
            png_bytes = await icas.async_screencap_bytes(screen_type=st, fmt="png")
        elif dev and dev.type == "mib_agent":
            mib = dm.get_mib_service(req.device_id)
            if not mib:
                raise HTTPException(status_code=400, detail=f"MIB device {req.device_id} not connected")
            st = req.screen_type if req.screen_type in ("HU", "IID", "HUD") else "HU"
            png_bytes = await mib.async_screencap_bytes(screen_type=st, fmt="png")
        elif dev and dev.type == "bmw_agent":
            bmw = dm.get_bmw_service(req.device_id)
            if not bmw:
                raise HTTPException(status_code=400, detail=f"BMW device {req.device_id} not connected")
            png_bytes = await bmw.async_screencap_bytes(screen_type=req.screen_type, fmt="png")
        elif dev and dev.type == "vision_camera":
            cam = dm.get_vision_camera(req.device_id)
            if not cam or not cam.IsConnected():
                raise HTTPException(status_code=400, detail=f"VisionCamera {req.device_id} not connected")
            import asyncio
            loop = asyncio.get_event_loop()
            png_bytes = await loop.run_in_executor(None, cam.CaptureBytes, "png")
        elif dev and dev.type == "webcam":
            cam = dm.get_webcam_device(req.device_id)
            if not cam or not cam.IsConnected():
                raise HTTPException(status_code=400, detail=f"Webcam {req.device_id} not connected")
            import asyncio
            loop = asyncio.get_event_loop()
            png_bytes = await loop.run_in_executor(None, cam.CaptureBytes, "png")
        elif dev and dev.type == "wincontrol":
            wc = dm.get_wincontrol_service()
            if not wc.is_attached():
                # 저장된 프로세스 정보로 자동 attach 시도 (step.params 또는 그대로 실패)
                step_params = step.params or {}
                if step_params.get("process_name") or step_params.get("exe_path") or step_params.get("process_aumid"):
                    import asyncio, functools
                    loop = asyncio.get_event_loop()
                    try:
                        await loop.run_in_executor(
                            None,
                            functools.partial(
                                wc.ensure_attached,
                                process_name=str(step_params.get("process_name", "") or ""),
                                exe_path=str(step_params.get("exe_path", "") or ""),
                                title_pattern=str(step_params.get("window_title", "") or ""),
                                class_name=str(step_params.get("window_class", "") or ""),
                                aumid=str(step_params.get("process_aumid", "") or ""),
                                launch_if_missing=True,
                                target_width=int(step_params.get("window_width", 0) or 0),
                                target_height=int(step_params.get("window_height", 0) or 0),
                            ),
                        )
                    except Exception as e:
                        raise HTTPException(status_code=400, detail=f"WinControl attach failed: {e}")
                else:
                    raise HTTPException(status_code=400, detail="WinControl: no window attached")
            import asyncio
            loop = asyncio.get_event_loop()
            png_bytes = await loop.run_in_executor(None, wc.capture_window, "png")
        else:
            adb_serial = dev.address if dev else req.device_id
            # screen_type → SF display ID 변환
            from ..services.adb_service import resolve_sf_display_id
            adb_did = None
            try:
                adb_did = int(req.screen_type)
            except (ValueError, TypeError):
                pass
            sf_did = resolve_sf_display_id(dev.info if dev else None, adb_did)
            # 미러링과 동일한 base64 스트리머 경로 — exec-out raw 바이너리 손상 방지
            # (특정 PC/adb 버전에서 "Cannot decode screenshot" 회귀 차단). 실패 시 내부 폴백.
            png_bytes = await adb_svc.streaming_screencap_bytes(serial=adb_serial, fmt="png", sf_display_id=sf_did)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Screenshot failed: {e}")

    # Optionally crop
    if req.crop:
        import cv2
        import numpy as np
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise HTTPException(status_code=400, detail="Cannot decode screenshot")
        x, y, w, h = int(req.crop["x"]), int(req.crop["y"]), int(req.crop["width"]), int(req.crop["height"])
        # 크롭 좌표 진단 — 캡처 해상도와 요청 좌표 일치 여부 검증
        ih, iw = img.shape[:2]
        if x < 0 or y < 0 or x + w > iw or y + h > ih:
            logger.warning(
                "expected_image crop OUT OF BOUNDS: img=%dx%d crop=(%d,%d,%dx%d) → clamped",
                iw, ih, x, y, w, h,
            )
        logger.info(
            "expected_image save: full_capture=%dx%d crop_req=(%d,%d,%dx%d) result_shape=%s",
            iw, ih, x, y, w, h, None,
        )
        cropped = img[y:y + h, x:x + w]
        logger.info(
            "expected_image saved: cropped_shape=%s (expected %dx%d)",
            cropped.shape, w, h,
        )
        _, buf = cv2.imencode(".png", cropped)
        png_bytes = buf.tobytes()

    scenario_name = scenario.name
    save_dir = SCREENSHOTS_DIR / scenario_name
    save_dir.mkdir(parents=True, exist_ok=True)

    if req.compare_mode == "multi_crop":
        # Multi-crop: append to expected_images list
        crop_idx = len(step.expected_images)
        filename = f"{scenario_name}_step_{step.id:03d}_crop_{crop_idx:02d}.png"
        (save_dir / filename).write_bytes(png_bytes)
        crop_roi = ROI(x=int(req.crop["x"]), y=int(req.crop["y"]),
                       width=int(req.crop["width"]), height=int(req.crop["height"])) if req.crop else None
        step.expected_images.append(CropItem(image=filename, label=req.crop_label, roi=crop_roi))
    else:
        # Single image (full or single_crop) — 타임스탬프 포함으로 캐시 충돌 방지
        import time as _time
        ts = int(_time.time() * 1000) % 1000000
        filename = f"{scenario_name}_step_{step.id:03d}_{ts}.png"
        # 이전 기대이미지 파일 삭제
        if step.expected_image and step.expected_image != filename:
            old_file = save_dir / step.expected_image
            if old_file.exists():
                old_file.unlink(missing_ok=True)
        if not req.preserve_crops:
            # 이전 multi_crop 이미지 파일 삭제
            for ci in step.expected_images:
                if ci.image:
                    old_crop = save_dir / ci.image
                    if old_crop.exists():
                        old_crop.unlink(missing_ok=True)
            step.expected_images.clear()
            step.exclude_rois.clear()
        (save_dir / filename).write_bytes(png_bytes)
        step.expected_image = filename
        if req.crop:
            step.roi = ROI(x=int(req.crop["x"]), y=int(req.crop["y"]),
                           width=int(req.crop["width"]), height=int(req.crop["height"]))
        else:
            step.roi = None

    # 스크린샷 디바이스/화면 기록 (재생/테스트 시 동일 디바이스·동일 화면으로 캡처).
    # screen_type을 저장하지 않으면 rear_left/rear_right에서 캡처한 기대이미지와
    # 재생 시 front_center로 캡처한 actual이 비교되어 항상 FAIL이 된다.
    step.screenshot_device_id = req.device_id
    if req.screen_type:
        step.screen_type = req.screen_type

    await recording_svc.save_scenario(scenario)
    return {"status": "ok", "filename": filename, "step_id": step.id}


class RemoveExpectedImageRequest(BaseModel):
    scenario_name: str
    step_index: int


@router.post("/record/remove-expected-image")
async def remove_expected_image(req: RemoveExpectedImageRequest):
    """Remove expected image and crop files from a step."""
    scenario = await _resolve_scenario(req.scenario_name)
    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

    step = scenario.steps[req.step_index]
    save_dir = SCREENSHOTS_DIR / scenario.name

    # 기대이미지 파일 삭제
    if step.expected_image:
        f = save_dir / step.expected_image
        if f.exists():
            f.unlink(missing_ok=True)
        step.expected_image = None

    # multi_crop 이미지 파일 삭제
    for ci in step.expected_images:
        if ci.image:
            f = save_dir / ci.image
            if f.exists():
                f.unlink(missing_ok=True)
    step.expected_images.clear()
    step.exclude_rois.clear()
    step.roi = None

    await recording_svc.save_scenario(scenario)
    return {"status": "ok"}


class ImageTapRequest(BaseModel):
    """녹화 중 '이미지 터치' 1회 실행 요청.

    프론트에서 보고 있던 현재 화면 이미지(image_base64)와 크롭 영역(crop), 유사도 임계값을
    함께 보내면 백엔드가:
      1) crop 영역을 template png 로 저장 (screenshots/{scenario}/),
      2) 같은 image_base64 에서 template_match 를 돌려 최고 매치 위치를 찾고,
      3) 매치되면 device 에 중심 좌표로 tap 을 즉시 실행,
      4) IMAGE_TAP 스텝을 시나리오에 기록한다 (params 에 template 파일명/유사도/마지막 매치 좌표).

    재생 시에는 playback_service 가 동일하게 actual 캡처 → template_match → 중심 tap 을 수행.
    """
    scenario_name: str
    device_id: str  # 터치를 실행할 디바이스 (ADB/HKMC/iSAP/ICAS/MIB/WinControl)
    image_base64: str  # 모달에 표시되던 현재 화면 PNG (data: prefix 허용)
    crop: dict  # {x, y, width, height} — 사용자가 드래그한 크롭 영역 (image_base64 픽셀 좌표)
    similarity: float = 0.85  # 0.0~1.0
    screen_type: Optional[str] = None  # HKMC/ICAS rear_left/HU 등 — 캡처 화면과 일치해야 함
    # HKMC 일체형 표시 보정: AVN(front_center) 캡처는 로컬 좌표인데 실제 터치 좌표계는
    # x로 x_offset(일체형=1920)만큼 밀려 있다. 프론트가 일체형이면 1920, 기본형이면 0 전송.
    # 레코딩 즉시 탭과 저장 step params(재생 시 _run_image_tap이 읽음)에 모두 반영.
    x_offset: int = 0
    delay_after_ms: int = 3000
    description: str = ""
    # 이미지 롱터치: True면 매치 중심에 tap 대신 long press 실행/기록.
    # duration_ms는 누르고 있는 시간 (재생 시 _run_image_tap도 동일 적용).
    long_press: bool = False
    duration_ms: int = 3000


@router.post("/record/image-tap")
async def record_image_tap(req: ImageTapRequest):
    """녹화 중 이미지 터치 한 번 실행 + IMAGE_TAP 스텝 기록.

    `current scenario` 와 이름이 일치하지 않으면 400. 반드시 녹화 중이어야 함.
    """
    import cv2
    import numpy as np
    import time as _time

    if not recording_svc.is_recording or recording_svc._current_scenario is None:
        raise HTTPException(status_code=400, detail="Not recording")
    if recording_svc._current_scenario.name != req.scenario_name:
        raise HTTPException(status_code=400, detail="Scenario name mismatch with current recording")

    scenario = recording_svc._current_scenario

    # 1) base64 디코딩 + crop
    try:
        raw = req.image_base64
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        png_bytes = base64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    src_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if src_img is None:
        raise HTTPException(status_code=400, detail="Cannot decode screenshot")

    cx = int(req.crop.get("x", 0))
    cy = int(req.crop.get("y", 0))
    cw = int(req.crop.get("width", 0))
    ch = int(req.crop.get("height", 0))
    if cw < 5 or ch < 5:
        raise HTTPException(status_code=400, detail="Crop region too small (need >=5×5)")
    ih, iw = src_img.shape[:2]
    cx = max(0, min(cx, iw - 1))
    cy = max(0, min(cy, ih - 1))
    cw = max(1, min(cw, iw - cx))
    ch = max(1, min(ch, ih - cy))
    cropped = src_img[cy:cy + ch, cx:cx + cw]

    # 2) template png 저장
    save_dir = SCREENSHOTS_DIR / scenario.name
    save_dir.mkdir(parents=True, exist_ok=True)
    next_step_id = recording_svc._step_counter + 1
    ts = int(_time.time() * 1000) % 1000000
    tpl_filename = f"{scenario.name}_step_{next_step_id:03d}_imgtap_{ts}.png"
    ok, buf = cv2.imencode(".png", cropped)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encode template image")
    (save_dir / tpl_filename).write_bytes(buf.tobytes())

    # 3) 보낸 이미지에서 template_match (저장된 동일 이미지 기준으로 매칭 — 무조건 발견)
    #    cv2.matchTemplate 직접 사용해 절대 좌표 + 신뢰도를 얻는다.
    src_gray = cv2.cvtColor(src_img, cv2.COLOR_BGR2GRAY)
    tpl_gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    if tpl_gray.shape[0] > src_gray.shape[0] or tpl_gray.shape[1] > src_gray.shape[1]:
        raise HTTPException(status_code=400, detail="Crop is larger than the screenshot")
    res = cv2.matchTemplate(src_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    confidence = round(float(max_val), 4)
    threshold = max(0.0, min(1.0, float(req.similarity)))
    found = confidence >= threshold

    if not found:
        # 템플릿이 원본에서 나온 것이므로 보통 1.0 이지만, threshold 가 너무 높을 수 있음.
        # 실패 시 저장한 템플릿 파일은 남겨두지 않는다.
        try:
            (save_dir / tpl_filename).unlink(missing_ok=True)
        except Exception:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"Template not found (confidence={confidence:.3f} < threshold={threshold:.3f})",
        )

    match_x = int(max_loc[0])
    match_y = int(max_loc[1])
    center_x = match_x + cw // 2
    center_y = match_y + ch // 2

    # 4) 디바이스에 tap 실행 — 디바이스 종류에 따라 적절한 step_type 으로 직접 실행.
    #    추후 IMAGE_TAP 재생 시에도 동일한 디스패치 로직을 사용한다.
    dev = dm.get_device(req.device_id)
    dev_type = dev.type if dev else "adb"

    # 일체형 오프셋은 AVN(front_center) 좌표계를 쓰는 agent 터치에만 적용.
    # win/adb는 x_offset이 의미 없으므로 0으로 둔다.
    x_offset = int(req.x_offset or 0)
    tap_x = center_x + x_offset

    long_press = bool(req.long_press)
    duration_ms = max(1, int(req.duration_ms or 3000))
    try:
        if dev_type in ("hkmc_agent", "isap_agent"):
            await recording_svc._execute_step_action(
                StepType.HKMC_LONG_PRESS if long_press else StepType.HKMC_TOUCH,
                {"x": tap_x, "y": center_y, "duration_ms": duration_ms,
                 "screen_type": req.screen_type or "front_center"},
                req.device_id,
            )
        elif dev_type in ("icas_agent", "mib_agent"):
            await recording_svc._execute_step_action(
                StepType.ICAS_LONG_PRESS if long_press else StepType.ICAS_TOUCH,
                {"x": tap_x, "y": center_y, "duration_ms": duration_ms,
                 "screen_type": req.screen_type or "HU"},
                req.device_id,
            )
        elif dev_type == "bmw_agent":
            # BMW는 generic TAP/LONG_PRESS를 쓰되 선택된 디스플레이(screen_type)를 전달.
            await recording_svc._execute_step_action(
                StepType.LONG_PRESS if long_press else StepType.TAP,
                {"x": center_x, "y": center_y, "duration_ms": duration_ms,
                 "screen_type": req.screen_type or "0"},
                req.device_id,
            )
        elif dev_type == "wincontrol":
            await recording_svc._execute_step_action(
                StepType.WIN_LONG_PRESS if long_press else StepType.WIN_TAP,
                {"x": center_x, "y": center_y, "duration_ms": duration_ms},
                req.device_id,
            )
        else:
            # ADB / 그 외 → 일반 TAP / LONG_PRESS
            await recording_svc._execute_step_action(
                StepType.LONG_PRESS if long_press else StepType.TAP,
                {"x": center_x, "y": center_y, "duration_ms": duration_ms},
                req.device_id,
            )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Tap execution failed: {e}")

    # 5) IMAGE_TAP 스텝 기록 (skip_execute=True 로 이미 실행한 액션 중복 방지)
    step_params = {
        "template": tpl_filename,
        "similarity": threshold,
        "screen_type": req.screen_type,
        "x_offset": x_offset,  # 재생 시 _run_image_tap이 매칭 좌표에 더해 터치 좌표계로 환산
        "matched_x": center_x,
        "matched_y": center_y,
        "template_width": cw,
        "template_height": ch,
    }
    if long_press:
        # 이미지 롱터치 — 재생 시 _run_image_tap이 tap 대신 long press 로 분기
        step_params["long_press"] = True
        step_params["duration_ms"] = duration_ms
    step, _resp = await recording_svc.add_step(
        step_type=StepType.IMAGE_TAP,
        params=step_params,
        device_id=req.device_id,
        description=req.description,
        delay_after_ms=req.delay_after_ms,
        skip_execute=True,
    )

    return {
        "status": "ok",
        "step": step.model_dump(),
        "match": {
            "found": True,
            "confidence": confidence,
            "center_x": center_x,
            "center_y": center_y,
            "match_x": match_x,
            "match_y": match_y,
            "template_width": cw,
            "template_height": ch,
        },
        "template_filename": tpl_filename,
    }


class UpdateImageTapRequest(BaseModel):
    """IMAGE_TAP 스텝의 템플릿 이미지를 새 크롭으로 교체.

    프론트엔드 모달에 표시되던 현재 화면(image_base64)에서 사용자가 새 크롭 영역을
    드래그하면 백엔드가:
      1) 기존 템플릿 파일 삭제,
      2) 새 크롭 영역을 PNG로 저장,
      3) 새 템플릿 기준으로 매칭 위치/신뢰도 계산,
      4) step.params(template, similarity, matched_x/y, template_width/height) 갱신,
      5) device_id가 주어지면 step.device_id / screenshot_device_id 도 덮어쓰기.
    실제 디바이스에 tap을 실행하지는 않는다 (편집 중 의도치 않은 입력 방지).
    """
    scenario_name: str
    step_index: int
    image_base64: str
    crop: dict
    similarity: float = 0.85
    screen_type: Optional[str] = None
    device_id: Optional[str] = None  # 현재 화면에서 선택된 디바이스 — 스텝의 device_id를 덮어씀
    x_offset: Optional[int] = None  # 일체형 표시 보정값(일체형=1920, 기본형=0). None이면 기존 값 유지


@router.post("/record/update-image-tap")
async def update_image_tap(req: UpdateImageTapRequest):
    """기존 IMAGE_TAP 스텝의 템플릿 이미지/파라미터를 교체."""
    import cv2
    import numpy as np
    import time as _time

    scenario = await _resolve_scenario(req.scenario_name)
    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")
    step = scenario.steps[req.step_index]
    if step.type != StepType.IMAGE_TAP:
        raise HTTPException(status_code=400, detail="Step is not IMAGE_TAP")

    # 1) base64 디코딩
    try:
        raw = req.image_base64
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        png_bytes = base64.b64decode(raw)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid base64 image data")

    arr = np.frombuffer(png_bytes, dtype=np.uint8)
    src_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if src_img is None:
        raise HTTPException(status_code=400, detail="Cannot decode screenshot")

    cx = int(req.crop.get("x", 0))
    cy = int(req.crop.get("y", 0))
    cw = int(req.crop.get("width", 0))
    ch = int(req.crop.get("height", 0))
    if cw < 5 or ch < 5:
        raise HTTPException(status_code=400, detail="Crop region too small (need >=5×5)")
    ih, iw = src_img.shape[:2]
    cx = max(0, min(cx, iw - 1))
    cy = max(0, min(cy, ih - 1))
    cw = max(1, min(cw, iw - cx))
    ch = max(1, min(ch, ih - cy))
    cropped = src_img[cy:cy + ch, cx:cx + cw]

    # 2) template_match — 새 크롭 기준 매칭 위치/신뢰도 (같은 이미지에서 잘랐으므로 보통 1.0)
    src_gray = cv2.cvtColor(src_img, cv2.COLOR_BGR2GRAY)
    tpl_gray = cv2.cvtColor(cropped, cv2.COLOR_BGR2GRAY)
    if tpl_gray.shape[0] > src_gray.shape[0] or tpl_gray.shape[1] > src_gray.shape[1]:
        raise HTTPException(status_code=400, detail="Crop is larger than the screenshot")
    res = cv2.matchTemplate(src_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
    _, max_val, _, max_loc = cv2.minMaxLoc(res)
    confidence = round(float(max_val), 4)
    threshold = max(0.0, min(1.0, float(req.similarity)))
    match_x = int(max_loc[0])
    match_y = int(max_loc[1])
    center_x = match_x + cw // 2
    center_y = match_y + ch // 2

    # 3) 새 템플릿 파일 저장 — 타임스탬프로 캐시 충돌 방지
    save_dir = SCREENSHOTS_DIR / scenario.name
    save_dir.mkdir(parents=True, exist_ok=True)
    ts = int(_time.time() * 1000) % 1000000
    new_tpl = f"{scenario.name}_step_{step.id:03d}_imgtap_{ts}.png"
    ok, buf = cv2.imencode(".png", cropped)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to encode template image")
    (save_dir / new_tpl).write_bytes(buf.tobytes())

    # 4) 이전 템플릿 파일 삭제 (이름이 같으면 건너뜀 — 타임스탬프로 충돌 거의 없음)
    old_tpl = (step.params or {}).get("template")
    if old_tpl and old_tpl != new_tpl:
        old_path = save_dir / old_tpl
        if old_path.exists():
            old_path.unlink(missing_ok=True)

    # 5) step.params 갱신
    new_params = dict(step.params or {})
    new_params.update({
        "template": new_tpl,
        "similarity": threshold,
        "matched_x": center_x,
        "matched_y": center_y,
        "template_width": cw,
        "template_height": ch,
    })
    if req.screen_type is not None:
        new_params["screen_type"] = req.screen_type
        step.screen_type = req.screen_type
    if req.x_offset is not None:
        new_params["x_offset"] = int(req.x_offset or 0)
    step.params = new_params

    # device_id 덮어쓰기 — 편집 시 현재 화면에 선택된 디바이스로 갱신.
    # screenshot_device_id 도 같은 디바이스로 맞춰 device_map 일관성 유지.
    if req.device_id:
        step.device_id = req.device_id
        step.screenshot_device_id = req.device_id
        # device_map에 별칭이 없다면 그대로 두고, 있다면 그대로 유지 (사용자가 명시적으로 매핑한 경우 보존).

    await recording_svc.save_scenario(scenario)
    return {
        "status": "ok",
        "step": step.model_dump(),
        "match": {
            "confidence": confidence,
            "center_x": center_x,
            "center_y": center_y,
            "template_width": cw,
            "template_height": ch,
        },
        "template_filename": new_tpl,
    }


class ImportStepsRequest(BaseModel):
    target_name: str
    source_name: str
    step_indices: list[int]  # 0-based indices
    move: bool = False  # True면 복사 후 소스에서 제거 (move 동작)


@router.post("/record/import-steps")
async def import_steps(req: ImportStepsRequest):
    """소스 시나리오에서 선택된 스텝들을 복사해온다 (기대이미지 포함).

    move=True이면 복사 후 소스 시나리오에서 해당 스텝들을 제거하고 소스도 저장.
    동일 시나리오(source == target)에서 move는 허용되지 않음 (드래그앤드롭 사용).
    """
    import shutil, time as _time
    # 녹화 중 미저장 상태에서도 메모리 스텝을 참조하도록 _resolve_scenario 사용
    source = await _resolve_scenario(req.source_name)
    tgt_ss_dir = SCREENSHOTS_DIR / req.target_name
    tgt_ss_dir.mkdir(parents=True, exist_ok=True)
    src_ss_dir = SCREENSHOTS_DIR / req.source_name

    is_move = req.move and req.source_name != req.target_name

    imported = []
    src_images_to_delete: list[Path] = []
    for idx in req.step_indices:
        if idx < 0 or idx >= len(source.steps):
            continue
        orig = source.steps[idx]
        step_data = orig.model_dump()
        # 새 타임스탬프 기반 ID (충돌 방지)
        ts = int(_time.time() * 1000) % 1000000
        new_id = 900 + len(imported)  # 프론트에서 재인덱싱하므로 임시값

        # 기대이미지 복사
        if step_data.get("expected_image"):
            old_file = src_ss_dir / step_data["expected_image"]
            new_filename = f"{req.target_name}_step_{new_id:03d}_{ts}.png"
            new_file = tgt_ss_dir / new_filename
            if old_file.exists():
                shutil.copy2(str(old_file), str(new_file))
                if is_move:
                    src_images_to_delete.append(old_file)
            step_data["expected_image"] = new_filename
            ts += 1

        # multi_crop 이미지 복사
        new_crops = []
        for ci_idx, ci in enumerate(step_data.get("expected_images", [])):
            if ci.get("image"):
                old_ci = src_ss_dir / ci["image"]
                new_ci_name = f"{req.target_name}_step_{new_id:03d}_crop_{ci_idx:02d}.png"
                new_ci = tgt_ss_dir / new_ci_name
                if old_ci.exists():
                    shutil.copy2(str(old_ci), str(new_ci))
                    if is_move:
                        src_images_to_delete.append(old_ci)
                ci["image"] = new_ci_name
            new_crops.append(ci)
        step_data["expected_images"] = new_crops
        # IMAGE_TAP 템플릿 이미지 복사 (params.template)
        if step_data.get("type") == "image_tap":
            params = step_data.get("params") or {}
            tpl = params.get("template")
            if tpl:
                old_tpl = src_ss_dir / tpl
                new_tpl_name = f"{req.target_name}_step_{new_id:03d}_imgtap_{ts}.png"
                new_tpl = tgt_ss_dir / new_tpl_name
                if old_tpl.exists():
                    shutil.copy2(str(old_tpl), str(new_tpl))
                    if is_move:
                        src_images_to_delete.append(old_tpl)
                params["template"] = new_tpl_name
                step_data["params"] = params
                ts += 1
        step_data["id"] = new_id
        # goto는 초기화 (다른 시나리오에서 온 경우 의미 없음)
        step_data["on_pass_goto"] = None
        step_data["on_fail_goto"] = None
        imported.append(step_data)

    # Move: 소스에서 선택된 스텝 제거 + 소스 저장 + 이미지 파일 정리
    if is_move and req.step_indices:
        remove_set = {i for i in req.step_indices if 0 <= i < len(source.steps)}
        # 제거 후 id 재번호 + goto 참조 재매핑
        remaining_pairs = [(i, s) for i, s in enumerate(source.steps) if i not in remove_set]
        # old 1-based position → new 1-based position (제거된 것은 None)
        pos_map: dict[int, Optional[int]] = {}
        for new_idx, (old_idx, _s) in enumerate(remaining_pairs):
            pos_map[old_idx + 1] = new_idx + 1
        for old_idx in remove_set:
            pos_map[old_idx + 1] = None

        def _remap_goto(g):
            if g is None or g == -1:
                return g
            return pos_map.get(g, None)

        new_steps = []
        for new_idx, (_old_idx, s) in enumerate(remaining_pairs):
            s_copy = s.model_copy(update={
                "id": new_idx + 1,
                "on_pass_goto": _remap_goto(s.on_pass_goto),
                "on_fail_goto": _remap_goto(s.on_fail_goto),
            })
            new_steps.append(s_copy)
        source.steps = new_steps
        # 이동(move)으로 소스에서 스텝이 빠지면 device_map도 정리
        _prune_device_map(source)
        await recording_svc.save_scenario(source)

        # 원본 이미지 파일 제거
        for f in src_images_to_delete:
            try:
                if f.exists():
                    f.unlink()
            except Exception as e:
                logger.warning("Failed to delete source image %s: %s", f, e)

    return {"steps": imported, "moved": is_move}


class RemoveCropRequest(BaseModel):
    scenario_name: str
    step_index: int
    crop_index: int


@router.post("/record/remove-crop")
async def remove_crop(req: RemoveCropRequest):
    """Remove a crop item from a multi-crop step."""
    scenario = await _resolve_scenario(req.scenario_name)

    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

    step = scenario.steps[req.step_index]
    if req.crop_index < 0 or req.crop_index >= len(step.expected_images):
        raise HTTPException(status_code=400, detail=f"Invalid crop index: {req.crop_index}")

    removed = step.expected_images.pop(req.crop_index)
    # Delete the image file
    img_path = SCREENSHOTS_DIR / req.scenario_name / removed.image
    if img_path.exists():
        img_path.unlink()

    await recording_svc.save_scenario(scenario)
    return {"status": "ok", "removed": removed.image}


class CropFromExpectedRequest(BaseModel):
    scenario_name: str
    step_index: int
    crop: dict  # {x, y, width, height}
    crop_label: str = ""
    replace_index: Optional[int] = None  # if set, replace existing crop at this index


@router.post("/record/crop-from-expected")
async def crop_from_expected(req: CropFromExpectedRequest):
    """Crop a region from the step's expected_image and save as a multi-crop item."""
    import cv2
    import numpy as np

    scenario = await _resolve_scenario(req.scenario_name)

    if req.step_index < 0 or req.step_index >= len(scenario.steps):
        raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

    step = scenario.steps[req.step_index]
    if not step.expected_image:
        raise HTTPException(status_code=400, detail="Step has no expected image to crop from")

    # Read the expected image (한글 경로 대응)
    from ..utils.cv_io import safe_imread
    img_path = SCREENSHOTS_DIR / req.scenario_name / step.expected_image
    img = safe_imread(img_path)
    if img is None:
        raise HTTPException(status_code=400, detail=f"기대이미지를 읽을 수 없음: {step.expected_image} (exists={img_path.exists()})")

    img_h, img_w = img.shape[:2]
    x, y = int(req.crop["x"]), int(req.crop["y"])
    w, h = int(req.crop["width"]), int(req.crop["height"])
    # 범위 클램핑
    x = max(0, min(x, img_w - 1))
    y = max(0, min(y, img_h - 1))
    w = min(w, img_w - x)
    h = min(h, img_h - y)
    if w <= 0 or h <= 0:
        raise HTTPException(status_code=400, detail=f"Crop region out of bounds (image: {img_w}x{img_h}, crop: x={x} y={y} w={w} h={h})")
    cropped = img[y:y + h, x:x + w]

    save_dir = SCREENSHOTS_DIR / req.scenario_name
    save_dir.mkdir(parents=True, exist_ok=True)

    roi = ROI(x=x, y=y, width=w, height=h)

    if req.replace_index is not None:
        # Replace existing crop
        if req.replace_index < 0 or req.replace_index >= len(step.expected_images):
            raise HTTPException(status_code=400, detail=f"Invalid replace index: {req.replace_index}")
        old = step.expected_images[req.replace_index]
        from ..utils.cv_io import safe_imwrite
        filename = old.image  # reuse same filename
        safe_imwrite(save_dir / filename, cropped)
        step.expected_images[req.replace_index] = CropItem(
            image=filename, label=req.crop_label or old.label, roi=roi,
        )
    else:
        # Append new crop
        from ..utils.cv_io import safe_imwrite
        crop_idx = len(step.expected_images)
        filename = f"{req.scenario_name}_step_{step.id:03d}_crop_{crop_idx:02d}.png"
        safe_imwrite(save_dir / filename, cropped)
        step.expected_images.append(CropItem(image=filename, label=req.crop_label, roi=roi))

    await recording_svc.save_scenario(scenario)
    return {
        "status": "ok",
        "filename": filename,
        "roi": roi.model_dump(),
        "index": req.replace_index if req.replace_index is not None else len(step.expected_images) - 1,
    }


# ------------------------------------------------------------------
# Groups
# ------------------------------------------------------------------
# Folders
# ------------------------------------------------------------------

@router.get("/folders")
async def get_folders():
    return {"folders": recording_svc.get_folders()}


class FolderRequest(BaseModel):
    name: str


class FolderRenameRequest(BaseModel):
    old_name: str
    new_name: str


class FolderMoveRequest(BaseModel):
    scenario_name: str
    folder_name: Optional[str] = None  # None = 루트


@router.post("/folders/create")
async def create_folder(req: FolderRequest):
    try:
        folders = recording_svc.create_folder(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"folders": folders}


@router.post("/folders/rename")
async def rename_folder(req: FolderRenameRequest):
    try:
        folders = recording_svc.rename_folder(req.old_name, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"folders": folders}


@router.post("/folders/delete")
async def delete_folder(req: FolderRequest):
    folders = recording_svc.delete_folder(req.name)
    return {"folders": folders}


@router.post("/folders/move")
async def move_to_folder(req: FolderMoveRequest):
    folders = recording_svc.move_to_folder(req.scenario_name, req.folder_name)
    return {"folders": folders}


# ------------------------------------------------------------------
# Group Folders (그룹을 폴더로 묶기)
# ------------------------------------------------------------------

class GroupFolderMoveRequest(BaseModel):
    group_name: str
    folder_name: Optional[str] = None  # None = 루트


@router.get("/group-folders")
async def get_group_folders():
    return {"folders": recording_svc.get_group_folders()}


@router.post("/group-folders/create")
async def create_group_folder(req: FolderRequest):
    try:
        folders = recording_svc.create_group_folder(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"folders": folders}


@router.post("/group-folders/rename")
async def rename_group_folder(req: FolderRenameRequest):
    try:
        folders = recording_svc.rename_group_folder(req.old_name, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"folders": folders}


@router.post("/group-folders/delete")
async def delete_group_folder(req: FolderRequest):
    folders = recording_svc.delete_group_folder(req.name)
    return {"folders": folders}


@router.post("/group-folders/move")
async def move_group_to_folder(req: GroupFolderMoveRequest):
    folders = recording_svc.move_group_to_folder(req.group_name, req.folder_name)
    return {"folders": folders}


# ------------------------------------------------------------------
# Groups
# ------------------------------------------------------------------

@router.get("/groups")
async def get_groups():
    """Get all scenario groups."""
    return {"groups": recording_svc.get_groups()}


class CreateGroupRequest(BaseModel):
    name: str


@router.post("/groups")
async def create_group(req: CreateGroupRequest):
    try:
        groups = recording_svc.create_group(req.name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"groups": groups}


class RenameGroupRequest(BaseModel):
    old_name: str
    new_name: str


@router.put("/groups")
async def rename_group(req: RenameGroupRequest):
    try:
        groups = recording_svc.rename_group(req.old_name, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"groups": groups}


# 주의: 그룹 이름에는 '/'(예: "테마/화면구성")가 포함될 수 있어 URL 경로 파라미터로
# 쓰면 라우팅이 깨진다(%2F 인코딩도 ASGI 디코드로 무력화됨). 따라서 그룹을 변경하는
# 모든 엔드포인트는 group_name을 요청 본문(body)으로 받는다.

class DeleteGroupRequest(BaseModel):
    group_name: str


@router.post("/groups/delete")
async def delete_group(req: DeleteGroupRequest):
    groups = recording_svc.delete_group(req.group_name)
    return {"groups": groups}


class GroupScenarioRequest(BaseModel):
    group_name: str
    scenario_name: str


class GroupIndexRequest(BaseModel):
    group_name: str
    index: int


@router.post("/groups/add")
async def add_to_group(req: GroupScenarioRequest):
    groups = recording_svc.add_to_group(req.group_name, req.scenario_name)
    return {"groups": groups}


@router.post("/groups/remove")
async def remove_from_group(req: GroupIndexRequest):
    groups = recording_svc.remove_from_group_by_index(req.group_name, req.index)
    return {"groups": groups}


class ReorderGroupRequest(BaseModel):
    group_name: str
    ordered_indices: list[int]


@router.post("/groups/reorder")
async def reorder_group(req: ReorderGroupRequest):
    groups = recording_svc.reorder_group(req.group_name, req.ordered_indices)
    return {"groups": groups}


class JumpTarget(BaseModel):
    scenario: int  # group index (0-based), -1 = END
    step: int = 0  # step index within the scenario (0-based)


class UpdateGroupJumpsRequest(BaseModel):
    group_name: str
    index: int
    on_pass_goto: Optional[JumpTarget] = None
    on_fail_goto: Optional[JumpTarget] = None


@router.post("/groups/jumps")
async def update_group_jumps(req: UpdateGroupJumpsRequest):
    pass_goto = req.on_pass_goto.model_dump() if req.on_pass_goto else None
    fail_goto = req.on_fail_goto.model_dump() if req.on_fail_goto else None
    groups = recording_svc.update_group_jumps(req.group_name, req.index, pass_goto, fail_goto)
    return {"groups": groups}


class UpdateGroupStepJumpsRequest(BaseModel):
    group_name: str
    index: int        # scenario index in group
    step_id: int      # step id within scenario
    on_pass_goto: Optional[JumpTarget] = None
    on_fail_goto: Optional[JumpTarget] = None
    # 체크 시 해당 방향 결과를 최종 집계에서 제외('분기'로 중립 표시)
    exclude_pass_from_result: bool = False
    exclude_fail_from_result: bool = False


@router.post("/groups/step-jumps")
async def update_group_step_jumps(req: UpdateGroupStepJumpsRequest):
    pass_goto = req.on_pass_goto.model_dump() if req.on_pass_goto else None
    fail_goto = req.on_fail_goto.model_dump() if req.on_fail_goto else None
    groups = recording_svc.update_group_step_jumps(
        req.group_name, req.index, req.step_id, pass_goto, fail_goto,
        req.exclude_pass_from_result, req.exclude_fail_from_result,
    )
    return {"groups": groups}


class UpdateGroupPlayCountRequest(BaseModel):
    group_name: str
    index: int
    play_count: int = Field(ge=1, le=999)


@router.post("/groups/play-count")
async def update_group_play_count(req: UpdateGroupPlayCountRequest):
    """Update per-member play count for a scenario in a group."""
    groups = recording_svc.update_group_play_count(req.group_name, req.index, req.play_count)
    return {"groups": groups}


# ------------------------------------------------------------------
# Copy & Merge
# ------------------------------------------------------------------

class CopyScenarioRequest(BaseModel):
    target_name: str


@router.post("/copy/{name}")
async def copy_scenario(name: str, req: CopyScenarioRequest):
    """Copy a scenario with a new name."""
    try:
        scenario = await recording_svc.copy_scenario(name, req.target_name)
        return {"status": "ok", "scenario": scenario.model_dump()}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found")


# ------------------------------------------------------------------
# Playback & Verification (before /{name} to avoid conflicts)
# ------------------------------------------------------------------

class TestStepRequest(BaseModel):
    scenario_name: str
    step_index: int  # 0-based
    step_data: Optional[dict] = None  # current (unsaved) step data from frontend
    # 프론트엔드 라이브 뷰가 현재 보고 있는 화면과 동일한 장면을 캡처하도록 강제하는 override.
    # 스텝에 저장된 screenshot_device_id/screen_type이 사용자가 보고 있는 화면과 다를 때
    # 발생하는 "stale image" 이슈를 차단한다.
    screenshot_device_id_override: Optional[str] = None
    screen_type_override: Optional[str] = None


@router.post("/clean-test-screenshots")
async def clean_test_screenshots(scenario_name: str = ""):
    """단일 스텝 테스트 임시 스크린샷(actual*/) 삭제.

    execute_single_step이 매 호출마다 ``actual_<ms_timestamp>/`` 서브디렉토리에
    캡처를 저장하므로, 패턴으로 일괄 삭제한다. 레거시 ``actual/`` 디렉토리도 함께.
    """
    import shutil
    cleaned = 0

    def _wipe(scenario_dir: Path) -> int:
        n = 0
        if not scenario_dir.is_dir():
            return 0
        for entry in scenario_dir.iterdir():
            if entry.is_dir() and (entry.name == "actual" or entry.name.startswith("actual_")):
                shutil.rmtree(str(entry), ignore_errors=True)
                n += 1
        return n

    if scenario_name:
        cleaned += _wipe(SCREENSHOTS_DIR / scenario_name)
    else:
        if SCREENSHOTS_DIR.is_dir():
            for d in SCREENSHOTS_DIR.iterdir():
                cleaned += _wipe(d)
    return {"cleaned": cleaned}


@router.post("/test-step")
async def test_step(req: TestStepRequest):
    """Execute a single step on the device and verify against expected image."""
    device_map: dict = {}

    if req.step_data:
        # Use the step data sent from frontend (may differ from saved file)
        from ..models.scenario import Step
        step = Step(**req.step_data)
        scenario_name = req.scenario_name
        # Load device_map from in-memory scenario or saved file
        cur = recording_svc._current_scenario
        if cur and cur.name == req.scenario_name and cur.device_map:
            device_map = dict(cur.device_map)
        else:
            try:
                scenario = await recording_svc.load_scenario(req.scenario_name)
                device_map = dict(scenario.device_map) if scenario.device_map else {}
            except FileNotFoundError:
                pass
    else:
        # 녹화 중 메모리 시나리오 또는 저장된 파일에서 로드
        cur = recording_svc._current_scenario
        if cur and cur.name == req.scenario_name:
            scenario = cur
        else:
            try:
                scenario = await recording_svc.load_scenario(req.scenario_name)
            except FileNotFoundError:
                raise HTTPException(status_code=404, detail=f"Scenario '{req.scenario_name}' not found")

        if req.step_index < 0 or req.step_index >= len(scenario.steps):
            raise HTTPException(status_code=400, detail=f"Invalid step index: {req.step_index}")

        step = scenario.steps[req.step_index]
        scenario_name = scenario.name
        device_map = dict(scenario.device_map) if scenario.device_map else {}

    # 프론트엔드가 라이브 뷰 기준으로 보낸 override를 스텝 복사본에 적용.
    # 이후 execute_single_step은 이 override된 값으로 스크린샷 디바이스를 해결한다.
    if req.screenshot_device_id_override:
        step = step.model_copy(update={
            "screenshot_device_id": req.screenshot_device_id_override,
            "screen_type": req.screen_type_override or step.screen_type,
        })
    elif req.screen_type_override:
        step = step.model_copy(update={"screen_type": req.screen_type_override})

    logger.info(
        "test-step: scenario=%s step_id=%s type=%s screenshot_dev=%s screen_type=%s",
        scenario_name, step.id, step.type,
        step.screenshot_device_id, step.screen_type,
    )

    result = await playback_svc.execute_single_step(step, scenario_name, device_map=device_map)
    return result.model_dump()


@router.delete("/cmd-result/{task_id}")
async def cancel_cmd_task(task_id: str):
    """백그라운드 태스크 취소 요청. SSH 스트리밍 reader가 다음 tick에 채널을 닫고 종료한다."""
    from ..services import bg_task_store
    ok = bg_task_store.request_cancel(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"cancelled": True, "task_id": task_id}


@router.get("/cmd-result/{task_id}")
async def get_cmd_result(task_id: str):
    """백그라운드 CMD 결과 폴링.

    완료 시 expected/match_mode가 저장되어 있으면 서버에서 비교까지 수행하여
    final_message와 final_status를 반환한다. 프론트엔드는 이 값을 step result에
    그대로 반영하기만 하면 된다.
    """
    from ..services import bg_task_store
    result = bg_task_store.get_task(task_id)
    if result is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if result["status"] != "running":
        # 완료된 태스크: 최종 메시지/판정 계산
        stdout = result.get("stdout", "") or ""
        stderr = result.get("stderr", "") or ""
        rc = result.get("rc")
        expected = result.get("expected")
        match_mode = result.get("match_mode", "contains")

        combined = stdout
        if stderr:
            combined = (combined + "\n" + stderr).strip() if combined else stderr

        if expected is not None:
            # CheckCapture 결과: 비교 수행
            actual = combined.strip()
            exp = (expected or "").strip()
            if not exp:
                # expected가 비어있으면 "출력 없음"일 때만 pass
                passed = actual == ""
                if passed:
                    result["final_message"] = "(no output)"
                    result["final_status"] = "pass"
                else:
                    result["final_message"] = f"FAIL: expected({match_mode}): (no output)\n---\n{combined}"
                    result["final_status"] = "fail"
            else:
                if match_mode == "exact":
                    passed = actual == exp
                else:
                    passed = exp in actual
                if passed:
                    result["final_message"] = combined if combined else f"(exit code: {rc})"
                    result["final_status"] = "pass"
                else:
                    result["final_message"] = f"FAIL: expected({match_mode}): {expected}\n---\n{combined}"
                    result["final_status"] = "fail"
        else:
            # RunCapture 결과: 메시지만 제공, 상태는 변경하지 않음
            result["final_message"] = combined if combined else f"(exit code: {rc})"
            result["final_status"] = None

        # 반환 후 정리
        bg_task_store.cleanup_task(task_id)
    return result


class PlaybackRequest(BaseModel):
    verify: bool = True


@router.post("/playback/stop")
async def stop_playback():
    """Stop the currently running playback.

    WebSocket과 무관하게 REST로도 호출 가능 — 프론트엔드가 죽거나
    연결이 끊어진 상태에서 백그라운드 재생을 강제 중단할 때 사용.
    """
    from ..services.playback_service import (
        publish_event, mark_playback_active,
    )
    was_running = playback_svc.is_running
    # race 방지: stop이 bg task 종료를 기다리는 동안 새 WS가 연결되더라도
    # 이전 run의 버퍼가 replay되지 않도록 먼저 inactive로 표시.
    mark_playback_active(False)
    # stop()은 내부적으로 bg 재생 태스크 종료까지 대기 (최대 15초).
    await playback_svc.stop()
    publish_event({"type": "playback_stopped", "result_filename": "", "source": "rest"})
    return {"status": "stopped", "was_running": was_running}


@router.get("/playback/status")
async def playback_status():
    """Check if playback is running + current monitor state (scenario name, progress)."""
    return {
        "running": playback_svc.is_running,
        "monitor": getattr(playback_svc, "_monitor_state", {}) or {},
    }


# ------------------------------------------------------------------
# Scenario CRUD (/{name} wildcard routes MUST be last)
# ------------------------------------------------------------------

@router.get("/list")
async def list_scenarios():
    """List all saved scenarios."""
    names = await recording_svc.list_scenarios()
    return {"scenarios": names}


# ------------------------------------------------------------------
# Migration (LGSI 전용 임시) — WoohyunBench SendAvnCan → SendCan
# ------------------------------------------------------------------

@router.post("/migrate/woohyun-can")
async def migrate_woohyun_can():
    """기존 시나리오의 WoohyunBench `SendAvnCan` 스텝을 `SendCan`으로 일괄 변환.

    - function: SendAvnCan → SendCan
    - args: msg_id / type / payload_hex 는 기존 값 그대로 유지
    - args: mcu=mcu1, channel=B, repeat=0, cycle_ms=200 로 일괄 설정

    디스크의 모든 시나리오 JSON을 순회하여 매칭 스텝을 변환·저장한다.
    raw JSON으로 직접 처리하여 레거시 데이터에서도 pydantic 검증 오류 없이 동작.
    """
    from ..services.recording_service import SCENARIOS_DIR

    reserved = {"groups.json", "folders.json", "group_folders.json"}
    changed_scenarios: list[dict] = []
    total_steps = 0

    if not SCENARIOS_DIR.is_dir():
        return {"status": "ok", "changed_scenarios": 0, "changed_steps": 0, "details": []}

    for spath in SCENARIOS_DIR.glob("*.json"):
        if spath.name in reserved:
            continue
        try:
            data = json.loads(spath.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("migrate-woohyun-can: skip unreadable %s: %s", spath.name, e)
            continue

        file_changed = 0
        for step in data.get("steps") or []:
            if step.get("type") != "module_command":
                continue
            params = step.get("params") or {}
            if params.get("module") != "WoohyunBench" or params.get("function") != "SendAvnCan":
                continue
            old_args = params.get("args") or {}
            params["function"] = "SendCan"
            params["args"] = {
                "mcu": "mcu1",
                "msg_id": old_args.get("msg_id", ""),
                "type": old_args.get("type", "STA"),
                "payload_hex": old_args.get("payload_hex", ""),
                "channel": "B",
                "repeat": 0,
                "cycle_ms": 200,
            }
            step["params"] = params
            file_changed += 1

        if file_changed:
            spath.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            changed_scenarios.append({"name": data.get("name", spath.stem), "steps": file_changed})
            total_steps += file_changed

    logger.info(
        "migrate-woohyun-can: %d scenarios, %d steps converted",
        len(changed_scenarios), total_steps,
    )
    return {
        "status": "ok",
        "changed_scenarios": len(changed_scenarios),
        "changed_steps": total_steps,
        "details": changed_scenarios,
    }


# ------------------------------------------------------------------
# Export / Import
# ------------------------------------------------------------------

class ExportRequest(BaseModel):
    scenarios: list[str] = []
    groups: list[str] = []
    include_all: bool = False


@router.post("/export")
async def export_scenarios(req: ExportRequest):
    """Export selected scenarios and groups as a ZIP file."""
    scenario_names = req.scenarios
    group_names = req.groups

    if req.include_all:
        scenario_names = await recording_svc.list_scenarios()
        group_names = list(recording_svc.get_groups().keys())

    if not scenario_names and not group_names:
        raise HTTPException(status_code=400, detail="Nothing to export")

    zip_bytes = await recording_svc.export_zip(scenario_names, group_names)
    from datetime import datetime, timezone
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="replaykit_export_{ts}.zip"'},
    )


@router.post("/import/preview")
async def import_preview(file: UploadFile = File(...)):
    """Preview a ZIP import and check for conflicts."""
    zip_data = await file.read()
    try:
        result = await recording_svc.import_preview(zip_data)
        return result
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/import/apply")
async def import_apply(file: UploadFile = File(...), resolutions: str = Form("{}")):
    """Apply a ZIP import with conflict resolutions."""
    zip_data = await file.read()
    try:
        res_dict = json.loads(resolutions)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid resolutions JSON")

    try:
        result = await recording_svc.import_apply(zip_data, res_dict)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/{name}")
async def get_scenario(name: str):
    """Load a scenario by name."""
    try:
        scenario = await recording_svc.load_scenario(name)
        return scenario.model_dump()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found")


@router.delete("/{name}")
async def delete_scenario(name: str):
    """Delete a scenario."""
    deleted = await recording_svc.delete_scenario(name)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found")
    return {"status": "deleted"}


class RenameScenarioRequest(BaseModel):
    new_name: str


@router.post("/{name}/rename")
async def rename_scenario(name: str, req: RenameScenarioRequest):
    """Rename a scenario."""
    try:
        ok = await recording_svc.rename_scenario(name, req.new_name)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found")
    # 새 이름으로 저장된 시나리오를 응답에 포함 — 프론트엔드가 갱신된
    # expected_image / expected_images 파일명을 로컬 state에 동기화할 수 있도록.
    # (이전: 프론트가 stale 파일명으로 update 호출해 JSON이 존재하지 않는
    #  파일을 가리키게 되어 기대이미지가 초기화되는 버그)
    try:
        scenario = await recording_svc.load_scenario(req.new_name)
        return {
            "status": "renamed",
            "old_name": name,
            "new_name": req.new_name,
            "scenario": scenario.model_dump(),
        }
    except FileNotFoundError:
        return {"status": "renamed", "old_name": name, "new_name": req.new_name}


@router.put("/{name}")
async def update_scenario(name: str, scenario: Scenario):
    """Update a scenario."""
    await recording_svc.save_scenario(scenario)
    return {"status": "updated"}


@router.post("/{name}/play")
async def play_scenario(name: str, req: PlaybackRequest):
    """Execute a saved scenario."""
    try:
        scenario = await recording_svc.load_scenario(name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Scenario '{name}' not found")

    # Preflight device check
    errors = await playback_svc.preflight_check(scenario)
    if errors:
        raise HTTPException(status_code=400, detail="디바이스 연결 확인 실패:\n" + "\n".join(errors))

    try:
        result = await playback_svc.execute_scenario(scenario, verify=req.verify)
        return result.model_dump()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
