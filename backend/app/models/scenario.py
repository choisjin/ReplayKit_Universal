from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class StepType(str, Enum):
    TAP = "tap"
    LONG_PRESS = "long_press"
    SWIPE = "swipe"
    INPUT_TEXT = "input_text"
    KEY_EVENT = "key_event"
    WAIT = "wait"
    ADB_COMMAND = "adb_command"
    SERIAL_COMMAND = "serial_command"
    MODULE_COMMAND = "module_command"
    HKMC_TOUCH = "hkmc_touch"
    HKMC_SWIPE = "hkmc_swipe"
    HKMC_KEY = "hkmc_key"
    HKMC_LONG_PRESS = "hkmc_long_press"
    ICAS_TOUCH = "icas_touch"
    ICAS_SWIPE = "icas_swipe"
    ICAS_KEY = "icas_key"
    ICAS_LONG_PRESS = "icas_long_press"
    MULTI_TOUCH = "multi_touch"  # 멀티핑거 제스처 (핀치, 멀티스와이프)
    REPEAT_TAP = "repeat_tap"    # 같은 위치 연속 터치
    ALL_RANDOM = "all_random"    # 랜덤 스트레스 (HK/SK/DRAG 가중 선택)
    IMAGE_TAP = "image_tap"      # 템플릿 매칭으로 위치를 찾아 중심 클릭 (좌표 비-종속)
    # WinControl 스텝 (Windows 임베드 프로세스 조작)
    WIN_TAP = "win_tap"
    WIN_DOUBLE_CLICK = "win_double_click"
    # 포커스를 유지한 채 여러 위치를 연속 클릭 (드롭다운 열기→항목 선택 등).
    # 일반 win_tap 은 클릭마다 포어그라운드를 원래 창으로 복원해서 드롭다운 같은
    # 일시 팝업이 닫히지만, 이 스텝은 시퀀스 전체가 끝날 때 1회만 복원한다.
    WIN_CLICK_SEQUENCE = "win_click_sequence"
    WIN_LONG_PRESS = "win_long_press"
    WIN_SWIPE = "win_swipe"
    WIN_INPUT_TEXT = "win_input_text"
    WIN_KEY = "win_key"
    WIN_KEY_COMBO = "win_key_combo"  # Ctrl/Alt/Shift 등 수정자 + 키 조합 (예: Ctrl+A, Ctrl+Shift+F)


class TapParams(BaseModel):
    x: int
    y: int


class SwipeParams(BaseModel):
    x1: int
    y1: int
    x2: int
    y2: int
    duration_ms: int = 300
    # 패턴(연속 스와이프) — 2개 이상의 waypoint가 주어지면 L자 등 다구간 연속 터치.
    # 비어 있거나 길이 < 2면 (x1,y1)→(x2,y2) 일반 스와이프로 동작.
    points: list[dict[str, int]] = Field(default_factory=list)


class InputTextParams(BaseModel):
    text: str


class KeyEventParams(BaseModel):
    keycode: str  # e.g. "KEYCODE_HOME", "KEYCODE_BACK"


class WaitParams(BaseModel):
    duration_ms: int = 1000


class AdbCommandParams(BaseModel):
    command: str


class SerialCommandParams(BaseModel):
    data: str
    read_timeout: float = 1.0


class ROI(BaseModel):
    """Region of Interest for image comparison."""
    x: int
    y: int
    width: int
    height: int


class CompareMode(str, Enum):
    FULL = "full"                    # 전체화면 SSIM
    SINGLE_CROP = "single_crop"      # 단일 크롭 — 녹화 시 ROI 좌표 그대로 SSIM
    FULL_EXCLUDE = "full_exclude"    # 전체화면에서 영역 제외 SSIM
    MULTI_CROP = "multi_crop"        # 여러 크롭 각각 비교
    MATCH_CROP = "match_crop"        # 매칭크롭 — 위치 무관하게 template matching 으로 탐색


class CropItem(BaseModel):
    """Multi-crop expected image entry."""
    image: str          # filename of the cropped expected image
    label: str = ""     # optional user label
    roi: Optional[ROI] = None  # crop region on the source screenshot


class Step(BaseModel):
    id: int
    type: StepType
    device_id: Optional[str] = None  # target device for this step
    screen_type: Optional[str] = None  # front_center|rear_left|rear_right|cluster (HKMC only)
    params: dict[str, Any]
    delay_after_ms: int = 3000
    expected_image: Optional[str] = None
    description: str = ""
    roi: Optional[ROI] = None  # optional region for verification
    similarity_threshold: float = 0.95
    on_pass_goto: Optional[int] = None  # step ID to jump to on pass (None = next)
    on_fail_goto: Optional[int] = None  # step ID to jump to on fail (None = next)
    compare_mode: CompareMode = CompareMode.FULL
    exclude_rois: list[ROI] = Field(default_factory=list)  # regions to exclude (full_exclude mode)
    expected_images: list[CropItem] = Field(default_factory=list)  # multi_crop mode
    screenshot_device_id: Optional[str] = None  # 이미지 비교용 디바이스 (wait 등 디바이스 비종속 스텝에서 사용)


class Scenario(BaseModel):
    name: str
    description: str = ""
    device_serial: Optional[str] = None
    resolution: Optional[dict[str, int]] = None  # {"width": 1080, "height": 1920}
    steps: list[Step] = Field(default_factory=list)
    device_map: dict[str, str] = Field(default_factory=dict)  # alias -> real device id (e.g. "Android_1" -> "RXCT30...")
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class SubResult(BaseModel):
    """Per-crop comparison result for multi_crop mode."""
    label: str = ""
    expected_image: str = ""
    score: float = 0.0
    status: str = "pass"  # pass/warning/fail
    match_location: Optional[dict] = None


class StepResult(BaseModel):
    step_id: int
    repeat_index: int = 1  # which cycle (1-based)
    timestamp: Optional[str] = None  # ISO timestamp when step started
    device_id: str = ""  # which device executed this step
    command: str = ""  # human-readable action description
    description: str = ""  # user remark for the step
    status: str  # "pass", "fail", "error"
    # 부모 스텝 id — fail_on_keyword(time>0)이 모니터링 중 검출한 fail row가 부모 스텝 직하에 인라인 표시될 때 사용.
    # None이면 일반 스텝, 값이 있으면 해당 스텝이 trigger한 runtime fail.
    parent_step_id: Optional[int] = None
    # 부모 내 순번 (1-based). 같은 parent_step_id 그룹 내에서 Fail_Count_N 표기용.
    fail_index: Optional[int] = None
    similarity_score: Optional[float] = None
    expected_image: Optional[str] = None
    expected_annotated_image: Optional[str] = None  # expected with regions drawn
    actual_image: Optional[str] = None
    actual_annotated_image: Optional[str] = None  # actual with match box drawn
    diff_image: Optional[str] = None
    roi: Optional[ROI] = None  # ROI used for comparison (for frontend display)
    match_location: Optional[dict] = None  # {x, y, width, height} of matched region
    message: str = ""
    delay_ms: int = 0  # configured delay_after_ms
    execution_time_ms: int = 0  # actual duration
    compare_mode: Optional[str] = None
    sub_results: list[SubResult] = Field(default_factory=list)  # per-crop details for multi_crop


class ScenarioResult(BaseModel):
    scenario_name: str
    device_serial: str
    status: str  # "pass", "fail", "error", "stopped"
    total_steps: int  # steps per cycle
    total_repeat: int = 1
    passed_steps: int = 0
    failed_steps: int = 0
    warning_steps: int = 0
    error_steps: int = 0
    step_results: list[StepResult] = Field(default_factory=list)  # ALL cycles combined
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    # 중단 시 진행 중이던 회차/스텝 추적 (status="stopped"일 때 의미 있음).
    stopped_at_iteration: Optional[int] = None  # 1-based; None이면 일반 종료
    stopped_at_step: Optional[int] = None       # 1-based; iteration 안에서의 step 인덱스
