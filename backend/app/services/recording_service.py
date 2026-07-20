"""Recording service — 사용자 동작을 시나리오로 기록."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import shutil
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.scenario import GOTO_END, ROI, Scenario, Step, StepType
from .adb_service import ADBService
from .device_manager import DeviceManager

logger = logging.getLogger(__name__)

SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "scenarios"
SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "screenshots"

# 시나리오/그룹/폴더 이름에 쓸 수 없는 문자.
# - '/' '\\' 는 파일·디렉터리 경로와 URL 라우팅을 깨뜨림(예: 그룹명 "테마/화면구성")
# - ': * ? " < > |' 는 Windows 파일명 금지 문자
# - '#' 는 URL fragment 로 해석되어 경로 파라미터가 잘림(예: GET /scenario/a#b → /scenario/a)
INVALID_NAME_CHARS = set('/\\:*?"<>|#')
INVALID_NAME_CHARS_DISPLAY = '/ \\ : * ? " < > | #'


def validate_entity_name(name: str, kind: str = "이름") -> str:
    """시나리오/그룹/폴더 이름 검증. 문제가 되는 특수문자가 있으면 ValueError.

    반환값은 앞뒤 공백을 제거한 정규화된 이름.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValueError(f"{kind}을(를) 입력하세요")
    bad = sorted({c for c in cleaned if c in INVALID_NAME_CHARS or ord(c) < 32})
    if bad:
        raise ValueError(
            f"{kind}에 다음 문자는 사용할 수 없습니다: {INVALID_NAME_CHARS_DISPLAY}"
        )
    return cleaned


def _dedupe_step_uids(scenario: "Scenario") -> bool:
    """중복된 step.uid 를 재부여한다. 변경 시 True.

    스텝 복사/붙여넣기는 uid 까지 그대로 복제하므로, 그대로 두면 두 스텝이 같은
    기대이미지 파일을 가리켜 다시 교차오염이 발생한다. 뒤에 오는 쪽에 새 uid 를 준다.
    """
    from ..models.scenario import _new_step_uid
    seen: set[str] = set()
    changed = False
    for s in scenario.steps:
        if not s.uid or s.uid in seen:
            s.uid = _new_step_uid()
            changed = True
        seen.add(s.uid)
    return changed


def _migrate_step_identity(data: dict) -> bool:
    """스텝 식별자 마이그레이션 — pydantic 파싱 **이전에** raw dict 위에서 수행한다.

    파싱 전에 해야 하는 이유:
      · uid 는 Step 의 default_factory 가 채우지만, 저장하지 않으면 로드마다 새 값이
        부여되어 uid 기반 파일명이 매번 고아가 된다. 여기서 확정해 영속화한다.
      · on_pass_goto/on_fail_goto 는 이제 uid 문자열이라, 레거시 정수값을 그대로
        넘기면 pydantic 검증에서 실패한다. 정수 → uid 변환이 파싱보다 앞서야 한다.

    수행 순서가 중요하다: ① uid 부여/중복제거 → ② uid 표(id→uid) 작성 → ③ goto 변환.
    변경이 있었으면 True (호출자가 반드시 1회 저장해야 한다).
    """
    from ..models.scenario import _new_step_uid

    steps = [s for s in data.get("steps", []) if isinstance(s, dict)]
    if not steps:
        return False

    changed = False

    # ① uid 부여 + 중복 제거 (복사/붙여넣기로 uid 가 복제될 수 있음)
    seen: set[str] = set()
    for s in steps:
        uid = s.get("uid")
        if not uid or not isinstance(uid, str) or uid in seen:
            s["uid"] = _new_step_uid()
            changed = True
        seen.add(s["uid"])

    # ② 레거시 goto(정수 step.id) → uid 변환용 표.
    #    레거시 시나리오에서 step.id 는 항상 1-based 위치였다.
    uid_by_id: dict[int, str] = {}
    for s in steps:
        try:
            uid_by_id[int(s.get("id"))] = s["uid"]
        except (TypeError, ValueError):
            continue

    # ③ goto 변환. -1(END 센티널) → "END", 대상 없음 → None(자연 진행).
    for s in steps:
        for key in ("on_pass_goto", "on_fail_goto"):
            g = s.get(key)
            if g is None or isinstance(g, str):
                continue  # 이미 uid/"END" 이거나 미설정
            try:
                gi = int(g)
            except (TypeError, ValueError):
                s[key] = None
                changed = True
                continue
            s[key] = GOTO_END if gi == -1 else uid_by_id.get(gi)
            changed = True

    # ④ 레거시 loops(정수 start/end = step.id 위치) → 경계 uid 변환.
    #    경계 스텝을 못 찾으면 그 구간은 폐기한다(무엇을 반복할지 특정 불가).
    loops = data.get("loops")
    if isinstance(loops, list) and loops:
        new_loops = []
        for lp in loops:
            if not isinstance(lp, dict):
                continue
            if "start_uid" in lp and "end_uid" in lp:
                new_loops.append(lp)      # 이미 변환됨
                continue
            try:
                s_uid = uid_by_id.get(int(lp.get("start")))
                e_uid = uid_by_id.get(int(lp.get("end")))
            except (TypeError, ValueError):
                s_uid = e_uid = None
            changed = True
            if not s_uid or not e_uid:
                logger.warning(
                    "구간반복 폐기: 경계 스텝을 찾을 수 없습니다 "
                    f"(start={lp.get('start')}, end={lp.get('end')})"
                )
                continue
            new_loops.append({"start_uid": s_uid, "end_uid": e_uid,
                              "count": lp.get("count", 2)})
        data["loops"] = new_loops

    return changed


def _migrate_legacy_step_types(data: dict) -> bool:
    """레거시 cmd_send / cmd_check 스텝을 module_command (CMD 모듈)으로 변환.

    매핑:
      cmd_send (bg=False) → CMD.Run(command, timeout)
      cmd_send (bg=True)  → CMD.RunCapture(command)
      cmd_check (bg=False) → CMD.Check(command, expected, match_mode, timeout)
      cmd_check (bg=True)  → CMD.CheckCapture(command, expected, match_mode)

    device_id는 "Common"으로 변경 (기본 CMD 디바이스).
    1개라도 변환되면 True 반환.
    """
    steps = data.get("steps", [])
    changed = False
    for s in steps:
        st = s.get("type")
        if st not in ("cmd_send", "cmd_check"):
            continue
        params = s.get("params", {}) or {}
        cmd = params.get("command", "")
        background = bool(params.get("background", False))
        timeout = params.get("timeout")
        new_args: dict = {"command": cmd}
        if st == "cmd_send":
            func = "RunCapture" if background else "Run"
            if not background and timeout is not None:
                new_args["timeout"] = int(timeout)
        else:  # cmd_check
            new_args["expected"] = params.get("expected", "")
            new_args["match_mode"] = params.get("match_mode", "contains")
            func = "CheckCapture" if background else "Check"
            if not background and timeout is not None:
                new_args["timeout"] = int(timeout)
        s["type"] = "module_command"
        s["device_id"] = "Common"
        s["params"] = {"module": "CMD", "function": func, "args": new_args}
        # 설명에 마이그레이션 표시 (선택)
        if not s.get("description"):
            s["description"] = f"CMD::{func}()"
        changed = True
        logger.info("Migrated legacy step %s → module_command CMD.%s", st, func)

    # 레거시 모듈 이름 마이그레이션: CCIC_BENCH → WoohyunBench
    for s in steps:
        params = s.get("params") or {}
        if isinstance(params, dict) and params.get("module") == "CCIC_BENCH":
            params["module"] = "WoohyunBench"
            s["params"] = params
            changed = True

    # 레거시 win_repeat_tap (같은 위치 count회 클릭, 중간 버전에서만 생성) →
    # win_click_sequence (포커스 유지 다좌표 클릭). 같은 좌표를 count개 넣어 의미 보존.
    # 프로세스 식별 정보(process_name/exe_path/window_* 등)는 그대로 유지.
    for s in steps:
        if s.get("type") != "win_repeat_tap":
            continue
        params = s.get("params", {}) or {}
        x = int(params.get("x", 0) or 0)
        y = int(params.get("y", 0) or 0)
        count = max(1, int(params.get("count", 2) or 2))
        new_params = {k: v for k, v in params.items() if k not in ("x", "y", "count")}
        new_params["points"] = [{"x": x, "y": y} for _ in range(count)]
        new_params.setdefault("interval_ms", 100)
        new_params.setdefault("button", "left")
        s["type"] = "win_click_sequence"
        s["params"] = new_params
        # 자동 생성 설명만 갱신 — 사용자가 직접 쓴 설명은 보존.
        desc = s.get("description") or ""
        if not desc or desc.startswith("win_repeat_tap"):
            coords = " → ".join(f"({x},{y})" for _ in range(count))
            s["description"] = f"win_click_sequence {coords} @{new_params['interval_ms']}ms"
        changed = True
        logger.info("Migrated legacy step win_repeat_tap → win_click_sequence (%d,%d ×%d)", x, y, count)
    return changed


def _build_ctor_kwargs(dev) -> dict | None:
    """Build constructor kwargs from device info for module instantiation."""
    ct = dev.info.get("connect_type", "serial" if dev.type == "serial" else "none")
    if ct == "serial":
        return {"port": dev.address, "bps": dev.info.get("baudrate", 115200)}
    elif ct == "socket":
        kwargs = {"host": dev.address}
        for k, v in dev.info.items():
            if k not in ("module", "connect_type"):
                kwargs[k] = v
        return kwargs
    elif ct == "can":
        return {k: v for k, v in dev.info.items() if k not in ("module", "connect_type")}
    return None
GROUPS_FILE = SCENARIOS_DIR / "groups.json"
FOLDERS_FILE = SCENARIOS_DIR / "folders.json"
GROUP_FOLDERS_FILE = SCENARIOS_DIR / "group_folders.json"


class RecordingService:
    """Record user actions into a Scenario."""

    def __init__(self, adb: ADBService, device_manager: DeviceManager):
        self.adb = adb
        self.dm = device_manager
        self._recording = False
        self._current_scenario: Optional[Scenario] = None
        self._step_counter = 0
        self._last_action_time: Optional[float] = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    async def start_recording(self, scenario_name: str, description: str = "") -> Scenario:
        """Start a new recording session."""
        if self._recording:
            raise RuntimeError("Already recording")
        scenario_name = validate_entity_name(scenario_name, "시나리오 이름")

        self._current_scenario = Scenario(
            name=scenario_name,
            description=description,
            steps=[],
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        self._step_counter = 0
        self._last_action_time = time.time()
        self._recording = True
        logger.info("Recording started: %s", scenario_name)
        return self._current_scenario

    async def resume_recording(self, scenario_name: str) -> Scenario:
        """Resume recording on an existing saved scenario."""
        if self._recording:
            raise RuntimeError("Already recording")

        scenario = await self.load_scenario(scenario_name)
        self._current_scenario = scenario
        self._step_counter = max((s.id for s in scenario.steps), default=0)
        self._last_action_time = time.time()
        self._recording = True
        logger.info("Recording resumed: %s (from step %d)", scenario_name, self._step_counter)
        return self._current_scenario

    def _ensure_device_mapped(self, device_id: str) -> str:
        """device_id를 그대로 반환. (이전엔 device_map에 alias→address 매핑을 자동
        기록했으나, 시나리오를 다른 PC에서 사용할 때 사용자가 디바이스 페이지에서
        ID/순서를 직접 시나리오에 맞춰 조정하는 운영 방식이라 자동 매핑이 잉여 정보로
        누적되는 문제가 있었음 — 자동 채우기 비활성화. device_map 필드는 모델/재생
        경로에 그대로 남아 있어 export/import 호환성과 frontend override는 유지된다.)
        """
        return device_id or ""

    async def add_step(
        self,
        step_type: StepType,
        params: dict,
        device_id: str = "",
        description: str = "",
        delay_after_ms: int = 3000,
        roi: Optional[dict] = None,
        similarity_threshold: float = 0.95,
        skip_execute: bool = False,
    ) -> tuple[Step, str | None]:
        """Add a recorded step and optionally execute the action on the target device.

        Returns (step, response) where response is non-None for serial_command.
        """
        if not self._recording or self._current_scenario is None:
            raise RuntimeError("Not recording")

        self._step_counter += 1
        step_id = self._step_counter

        response = None
        if not skip_execute:
            response = await self._execute_step_action(step_type, params, device_id)

        # Ensure device_id is recorded in device_map (maps to real address)
        mapped_id = self._ensure_device_mapped(device_id) if device_id else None

        # params에 screen_type이 있으면 Step 최상위 필드에도 저장
        step_screen_type = params.get("screen_type") if params else None

        step = Step(
            id=step_id,
            type=step_type,
            device_id=mapped_id,
            screen_type=step_screen_type,
            params=params,
            delay_after_ms=delay_after_ms,
            expected_image=None,
            description=description,
            roi=ROI(**roi) if roi else None,
            similarity_threshold=similarity_threshold,
        )
        self._current_scenario.steps.append(step)
        self._last_action_time = time.time()
        logger.info("Step %d recorded: %s on device %s", step_id, step_type.value, device_id or "default")
        return step, response

    async def stop_recording(self) -> Scenario:
        """Stop recording and save the scenario."""
        if not self._recording or self._current_scenario is None:
            raise RuntimeError("Not recording")

        self._current_scenario.updated_at = datetime.now(timezone.utc).isoformat()
        self._recording = False

        # Save scenario to JSON
        await self.save_scenario(self._current_scenario)
        logger.info("Recording stopped: %s (%d steps)", self._current_scenario.name, len(self._current_scenario.steps))
        scenario = self._current_scenario
        self._current_scenario = None
        return scenario

    async def save_scenario(self, scenario: Scenario) -> str:
        """Save scenario to JSON file.

        저장 직전 device_map을 정리한다. 어떤 경로(녹화 add_step, PUT update_scenario,
        sync-steps, copy 등)로 들어와도 현재 steps에서 참조하지 않는 device_map 항목이
        잔존하지 않도록 일괄 보장. 또한 device_map이 비어있으면 JSON 출력에서 아예
        키를 생략한다 (가독성).
        """
        self._prune_unused_device_map(scenario)
        # 스텝 복사/붙여넣기로 uid 가 중복 유입될 수 있으므로 저장 직전 항상 유일성 보장.
        # (어떤 경로로 들어와도 uid 는 시나리오 내에서 유일하다는 불변식을 여기서 확정)
        _dedupe_step_uids(scenario)
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        filepath = SCENARIOS_DIR / f"{scenario.name}.json"

        # model_dump(대형 시나리오는 수천 스텝) + json.dumps + write_text 는 모두 동기
        # CPU/IO 작업이라 이벤트 루프를 점유한다. 워커 스레드로 오프로드해 /api/health 를
        # 굶기지 않는다(load_scenario 의 자동 저장 경로 등에서도 루프를 막지 않도록).
        def _write_sync() -> None:
            data = scenario.model_dump()
            # 빈 device_map은 직렬화 결과에서 제외 — 사용자가 매핑을 명시적으로 활용할 때만 노출
            if not data.get("device_map"):
                data.pop("device_map", None)
            filepath.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        await asyncio.to_thread(_write_sync)
        return str(filepath)

    @staticmethod
    def _prune_unused_device_map(scenario: Scenario) -> None:
        """현재 steps에서 참조되지 않는 device_map 항목을 in-place 제거."""
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

    async def load_scenario(self, name: str) -> Scenario:
        """Load scenario from JSON file.

        파일 읽기 + json 파싱 + pydantic 검증 + 이미지 참조 수리(_repair_image_refs 는
        스텝마다 glob 을 돌 수 있음)는 모두 동기 CPU/IO 작업이라, 대형 시나리오에서는
        이벤트 루프를 수초간 점유해 /api/health 를 굶긴다(재생 시작 직후 "서버 연결 중..."
        배너의 원인). 워커 스레드로 오프로드해 루프를 막지 않는다.
        """
        filepath = SCENARIOS_DIR / f"{name}.json"
        if not filepath.exists():
            raise FileNotFoundError(f"Scenario not found: {name}")

        def _load_sync() -> tuple[Scenario, bool]:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            # 레거시 cmd_send / cmd_check → module_command CMD.* 로 자동 마이그레이션
            migrated = _migrate_legacy_step_types(data)
            # 스텝 식별자(uid) 부여 + 레거시 goto(정수) → uid 변환.
            # 반드시 파싱 전에 raw dict 에서 수행한다 (_migrate_step_identity 주석 참조).
            identity_migrated = _migrate_step_identity(data)
            scenario = Scenario(**data)
            deduped = _dedupe_step_uids(scenario)
            # 이미지 참조 자동 수리: 파일이 없으면 폴더 내에서 같은 step ID 파일 탐색
            changed = self._repair_image_refs(name, scenario)
            return scenario, (changed or migrated or identity_migrated or deduped)

        scenario, needs_save = await asyncio.to_thread(_load_sync)
        if needs_save:
            await self.save_scenario(scenario)
        return scenario

    def _repair_image_refs(self, name: str, scenario: "Scenario") -> bool:
        """expected_image가 실제 파일과 불일치하면 자동 수리. 변경 시 True 반환."""
        ss_dir = SCREENSHOTS_DIR / name
        if not ss_dir.exists():
            return False
        changed = False
        for step in scenario.steps:
            if step.expected_image:
                if not (ss_dir / step.expected_image).exists():
                    # uid 로 매칭되는 파일 탐색.
                    # uid 는 불변이라 `*_{uid}_*` 는 반드시 이 스텝이 만든 파일만 잡는다.
                    # (옛 `*_step_NNN_*` 매칭은 step.id 재부여 탓에 남의 이미지를 물어
                    #  오결합을 일으켰다. 레거시 파일은 uid 가 없으므로 애초에 잡히지 않는다.)
                    # 그래도 안전하게 ① 후보 1개 ② 다른 스텝 미참조 조건을 유지하고,
                    # 애매하면 None 으로 비워 사용자가 재캡처하도록 유도한다.
                    pattern = f"*_{step.uid}*"
                    in_use = {s.expected_image for s in scenario.steps if s.expected_image}
                    in_use |= {ci.image for s in scenario.steps for ci in s.expected_images if ci.image}
                    candidates = [
                        f for f in ss_dir.glob(pattern)
                        if "crop" not in f.name and "annotated" not in f.name and "actual" not in f.stem
                        and f.name not in in_use
                    ]
                    if len(candidates) == 1:
                        step.expected_image = candidates[0].name
                    else:
                        if candidates:
                            logger.warning(
                                f"[{name}] step {step.id}: 기대이미지 후보가 {len(candidates)}개라 자동 수리를 "
                                f"보류합니다(오결합 방지). 재캡처가 필요합니다."
                            )
                        step.expected_image = None
                    changed = True
            for ci in step.expected_images:
                if ci.image and not (ss_dir / ci.image).exists():
                    ci.image = None
                    changed = True
            # None이 된 crop 항목 제거
            step.expected_images = [ci for ci in step.expected_images if ci.image]
        return changed

    async def list_scenarios(self) -> list[str]:
        """List all saved scenario names (이름 오름차순).

        glob() 순서는 파일시스템 의존 — Windows(NTFS)는 알파벳순이지만
        Linux(ext4)는 임의 순서라 명시적으로 정렬해야 UI 목록 순서가 보장된다.
        """
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        names = [p.stem for p in SCENARIOS_DIR.glob("*.json") if p.name not in ("groups.json", "folders.json", "group_folders.json")]
        return sorted(names, key=str.casefold)

    async def delete_scenario(self, name: str) -> bool:
        """Delete a scenario file + screenshots folder."""
        import shutil
        filepath = SCENARIOS_DIR / f"{name}.json"
        if filepath.exists():
            filepath.unlink()
            # 스크린샷 폴더 삭제 (기대 이미지 포함)
            ss_dir = SCREENSHOTS_DIR / name
            if ss_dir.is_dir():
                shutil.rmtree(str(ss_dir), ignore_errors=True)
            # Remove from any groups
            groups = self._load_groups()
            changed = False
            for gname in list(groups.keys()):
                before = len(groups[gname])
                groups[gname] = [m for m in groups[gname] if m["name"] != name]
                if len(groups[gname]) < before:
                    changed = True
            if changed:
                self._save_groups(groups)
            # Remove from any folders
            folders = self._load_folders()
            f_changed = False
            for fname in list(folders.keys()):
                before = len(folders[fname])
                folders[fname] = [n for n in folders[fname] if n != name]
                if len(folders[fname]) < before:
                    f_changed = True
            if f_changed:
                self._save_folders(folders)
            return True
        return False

    async def rename_scenario(self, old_name: str, new_name: str) -> bool:
        """Rename a scenario file and update group references."""
        new_name = validate_entity_name(new_name, "시나리오 이름")
        old_path = SCENARIOS_DIR / f"{old_name}.json"
        new_path = SCENARIOS_DIR / f"{new_name}.json"
        if not old_path.exists():
            return False
        if new_path.exists():
            raise ValueError(f"Scenario '{new_name}' already exists")
        # Load, update name, save to new path
        data = json.loads(old_path.read_text(encoding="utf-8"))
        data["name"] = new_name
        # ---- 스크린샷 폴더/파일 이름 변경 (원자적: 실패 시 롤백) ----
        # 파일명은 "시나리오 이름 프리픽스"만 교체하고 _step_NNN...timestamp 부분은
        # 원본 그대로 보존한다. 과거에는 현재 step id 로 파일명을 재구성했는데,
        # 스텝 재인덱싱으로 id 가 캡처 당시 번호와 어긋나면 파일명이 충돌하며
        # (Windows FileExistsError) rename 이 중단돼 폴더만 옮겨진 채 JSON 과
        # 불일치하는 손상 상태가 됐다.
        old_ss = SCREENSHOTS_DIR / old_name
        new_ss = SCREENSHOTS_DIR / new_name
        done: list[tuple[Path, Path]] = []  # 롤백용 (from, to)
        dir_moved = False
        json_written = False
        try:
            # 1) 폴더 안 파일들을 새 프리픽스로 rename (폴더는 아직 old 이름)
            if old_ss.exists():
                for step_data in data.get("steps", []):
                    ei = step_data.get("expected_image")
                    if ei:
                        step_data["expected_image"] = self._swap_image_prefix(
                            old_ss, ei, old_name, new_name, done)
                    for ci in step_data.get("expected_images", []):
                        ci_img = ci.get("image")
                        if ci_img:
                            ci["image"] = self._swap_image_prefix(
                                old_ss, ci_img, old_name, new_name, done)
                # 2) 폴더 이름 변경
                if not new_ss.exists():
                    old_ss.rename(new_ss)
                    dir_moved = True
            new_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            json_written = True
            old_path.unlink()
        except Exception:
            # 롤백: 새 JSON 제거 → 폴더 → 파일 순 원복 (경로가 old_ss 기준이라 폴더부터)
            if json_written and new_path.exists() and old_path.exists():
                new_path.unlink()
            if dir_moved and new_ss.exists() and not old_ss.exists():
                new_ss.rename(old_ss)
            for src, dst in reversed(done):
                if dst.exists() and not src.exists():
                    dst.rename(src)
            raise
        # Update group references
        groups = self._load_groups()
        changed = False
        for members in groups.values():
            for m in members:
                if m["name"] == old_name:
                    m["name"] = new_name
                    changed = True
        if changed:
            self._save_groups(groups)
        # Update folder references
        folders = self._load_folders()
        f_changed = False
        for fname in list(folders.keys()):
            items = folders[fname]
            for i, n in enumerate(items):
                if n == old_name:
                    items[i] = new_name
                    f_changed = True
        if f_changed:
            self._save_folders(folders)
        return True

    @staticmethod
    def _swap_image_prefix(ss_dir: Path, old_filename: str, old_name: str,
                           new_name: str, done: list) -> str:
        """이미지 파일명의 시나리오 이름 프리픽스만 교체하고 실제 파일도 rename.
        step 번호/timestamp/crop 인덱스는 원본 그대로 보존해 파일명 충돌을 막는다.
        실제 파일이 없으면 파일명(문자열)만 갱신한다. done 에 (src, dst) 를 기록해
        상위에서 롤백할 수 있게 한다."""
        prefix = f"{old_name}_"
        if not old_filename.startswith(prefix):
            return old_filename
        new_filename = f"{new_name}_" + old_filename[len(prefix):]
        if new_filename == old_filename:
            return old_filename
        src = ss_dir / old_filename
        dst = ss_dir / new_filename
        if src.exists():
            if dst.exists():
                # 이전 실패 시도의 잔여물 등 — 덮어쓰면 데이터 손실이라 명확히 실패시켜
                # 상위 롤백을 유도한다.
                raise FileExistsError(f"rename target already exists: {dst}")
            src.rename(dst)
            done.append((src, dst))
        return new_filename

    # ------------------------------------------------------------------
    # Folders (가상 폴더 — 시나리오 파일은 flat, 메타데이터로 관리)
    # ------------------------------------------------------------------

    def _load_folders(self) -> dict[str, list[str]]:
        """Load folder assignments. {folder_name: [scenario_name, ...]}"""
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        if FOLDERS_FILE.exists():
            return json.loads(FOLDERS_FILE.read_text(encoding="utf-8"))
        return {}

    def _save_folders(self, folders: dict[str, list[str]]) -> None:
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        FOLDERS_FILE.write_text(json.dumps(folders, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_folders(self) -> dict[str, list[str]]:
        folders = self._load_folders()
        # 유령 항목(존재하지 않는 시나리오) 자동 정리
        existing = {p.stem for p in SCENARIOS_DIR.glob("*.json") if p.name not in ("groups.json", "folders.json", "group_folders.json")}
        changed = False
        for fname in list(folders.keys()):
            before = len(folders[fname])
            folders[fname] = [n for n in folders[fname] if n in existing]
            if len(folders[fname]) < before:
                changed = True
        if changed:
            self._save_folders(folders)
        return folders

    def create_folder(self, name: str) -> dict[str, list[str]]:
        name = validate_entity_name(name, "폴더 이름")
        folders = self._load_folders()
        if name in folders:
            raise ValueError(f"폴더 '{name}'이(가) 이미 존재합니다")
        folders[name] = []
        self._save_folders(folders)
        return folders

    def rename_folder(self, old_name: str, new_name: str) -> dict[str, list[str]]:
        new_name = validate_entity_name(new_name, "폴더 이름")
        folders = self._load_folders()
        if new_name in folders:
            raise ValueError(f"폴더 '{new_name}'이(가) 이미 존재합니다")
        if old_name in folders:
            folders[new_name] = folders.pop(old_name)
        self._save_folders(folders)
        return folders

    def delete_folder(self, name: str) -> dict[str, list[str]]:
        folders = self._load_folders()
        folders.pop(name, None)
        self._save_folders(folders)
        return folders

    def move_to_folder(self, scenario_name: str, folder_name: str | None) -> dict[str, list[str]]:
        """시나리오를 폴더로 이동. folder_name=None이면 루트로."""
        folders = self._load_folders()
        # 기존 위치에서 제거
        for items in folders.values():
            if scenario_name in items:
                items.remove(scenario_name)
        # 새 위치에 추가
        if folder_name and folder_name in folders:
            if scenario_name not in folders[folder_name]:
                folders[folder_name].append(scenario_name)
        self._save_folders(folders)
        return folders

    # ------------------------------------------------------------------
    # Group Folders (시나리오 폴더와 동일 구조 — 그룹을 폴더로 묶기)
    # ------------------------------------------------------------------

    def _load_group_folders(self) -> dict[str, list[str]]:
        """Load group folder assignments. {folder_name: [group_name, ...]}"""
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        if GROUP_FOLDERS_FILE.exists():
            return json.loads(GROUP_FOLDERS_FILE.read_text(encoding="utf-8"))
        return {}

    def _save_group_folders(self, folders: dict[str, list[str]]) -> None:
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        GROUP_FOLDERS_FILE.write_text(json.dumps(folders, ensure_ascii=False, indent=2), encoding="utf-8")

    def get_group_folders(self) -> dict[str, list[str]]:
        """그룹 폴더 조회. 존재하지 않는 그룹은 자동 제거."""
        folders = self._load_group_folders()
        existing = set(self.get_groups().keys())
        changed = False
        for fname in list(folders.keys()):
            before = len(folders[fname])
            folders[fname] = [g for g in folders[fname] if g in existing]
            if len(folders[fname]) < before:
                changed = True
        if changed:
            self._save_group_folders(folders)
        return folders

    def create_group_folder(self, name: str) -> dict[str, list[str]]:
        name = validate_entity_name(name, "그룹 폴더 이름")
        folders = self._load_group_folders()
        if name in folders:
            raise ValueError(f"그룹 폴더 '{name}'이(가) 이미 존재합니다")
        folders[name] = []
        self._save_group_folders(folders)
        return folders

    def rename_group_folder(self, old_name: str, new_name: str) -> dict[str, list[str]]:
        new_name = validate_entity_name(new_name, "그룹 폴더 이름")
        folders = self._load_group_folders()
        if new_name in folders:
            raise ValueError(f"그룹 폴더 '{new_name}'이(가) 이미 존재합니다")
        if old_name in folders:
            folders[new_name] = folders.pop(old_name)
        self._save_group_folders(folders)
        return folders

    def delete_group_folder(self, name: str) -> dict[str, list[str]]:
        """폴더만 제거 — 그룹 자체는 삭제하지 않음 (루트로 이동)."""
        folders = self._load_group_folders()
        folders.pop(name, None)
        self._save_group_folders(folders)
        return folders

    def move_group_to_folder(self, group_name: str, folder_name: str | None) -> dict[str, list[str]]:
        """그룹을 폴더로 이동. folder_name=None이면 루트로."""
        folders = self._load_group_folders()
        for items in folders.values():
            if group_name in items:
                items.remove(group_name)
        if folder_name and folder_name in folders:
            if group_name not in folders[folder_name]:
                folders[folder_name].append(group_name)
        self._save_group_folders(folders)
        return folders

    # ------------------------------------------------------------------
    # Groups
    # ------------------------------------------------------------------

    def _load_groups_raw(self) -> dict:
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        if GROUPS_FILE.exists():
            return json.loads(GROUPS_FILE.read_text(encoding="utf-8"))
        return {}

    def _load_groups(self) -> dict[str, list[dict]]:
        """Load groups, auto-migrating old formats to new dict-based jump format.

        Current format: on_pass_goto / on_fail_goto = {scenario: int, step: int} | null,
                        play_count = int (>=1, default 1)
        Old format v1: list[str] (just scenario names)
        Old format v2: on_pass_goto / on_fail_goto = int | null (scenario index only)
        Old format v3: play_count 필드 부재
        """
        raw = self._load_groups_raw()
        migrated = False
        result: dict[str, list[dict]] = {}
        for gname, members in raw.items():
            if isinstance(members, list) and len(members) > 0 and isinstance(members[0], str):
                # Old format v1: list of scenario names
                result[gname] = [
                    {"name": m, "on_pass_goto": None, "on_fail_goto": None, "play_count": 1}
                    for m in members
                ]
                migrated = True
            else:
                entries = members if isinstance(members, list) else []
                for entry in entries:
                    # v2 → v3: 정수 jump를 dict로
                    for key in ("on_pass_goto", "on_fail_goto"):
                        val = entry.get(key)
                        if isinstance(val, int):
                            entry[key] = {"scenario": val, "step": 0}
                            migrated = True
                    # v3 → 현재: play_count 필드 보충
                    if "play_count" not in entry:
                        entry["play_count"] = 1
                        migrated = True
                result[gname] = entries
        if migrated:
            self._save_groups(result)
        return result

    def _save_groups(self, groups: dict[str, list[dict]]) -> None:
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        # 대용량 그룹(수천 멤버)에서 indent=2는 파일 크기·직렬화 비용을 크게 키운다.
        # groups.json은 기계 관리 파일이므로 compact(구분자 최소)로 저장한다.
        GROUPS_FILE.write_text(
            json.dumps(groups, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def get_groups(self) -> dict[str, list[dict]]:
        return self._load_groups()

    def create_group(self, group_name: str) -> dict[str, list[dict]]:
        group_name = validate_entity_name(group_name, "그룹 이름")
        groups = self._load_groups()
        if group_name in groups:
            raise ValueError(f"그룹 '{group_name}'이(가) 이미 존재합니다")
        groups[group_name] = []
        self._save_groups(groups)
        return groups

    def delete_group(self, group_name: str) -> dict[str, list[dict]]:
        groups = self._load_groups()
        groups.pop(group_name, None)
        self._save_groups(groups)
        return groups

    def rename_group(self, old_name: str, new_name: str) -> dict[str, list[dict]]:
        new_name = validate_entity_name(new_name, "그룹 이름")
        groups = self._load_groups()
        if new_name in groups:
            raise ValueError(f"그룹 '{new_name}'이(가) 이미 존재합니다")
        if old_name in groups:
            groups[new_name] = groups.pop(old_name)
            self._save_groups(groups)
            # 그룹 폴더 멤버 목록도 동기화 — 이름이 바뀌어도 같은 폴더에 남도록
            gfolders = self._load_group_folders()
            changed = False
            for items in gfolders.values():
                for i, n in enumerate(items):
                    if n == old_name:
                        items[i] = new_name
                        changed = True
            if changed:
                self._save_group_folders(gfolders)
        return groups

    def add_to_group(self, group_name: str, scenario_name: str) -> dict[str, list[dict]]:
        # 단건 추가는 배치(1건)로 위임 — 동작 동일.
        return self.add_batch_to_group(group_name, [scenario_name])

    def add_batch_to_group(self, group_name: str, scenario_names: list[str]) -> dict[str, list[dict]]:
        """여러 시나리오를 그룹에 한 번에 추가 — load 1회 + save 1회 (O(N)).

        기존 방식(시나리오당 add_to_group 호출)은 매 건마다 groups.json 전체를
        read+parse+write 해서 N건 추가 시 O(N²)가 되고, 동기 I/O가 이벤트 루프를
        오래 점유해 /api/health 를 굶겨 프론트가 "서버 연결 중"에 머무른다.
        배치로 묶어 파일 접근을 1회로 줄인다. 동일 시나리오 중복 추가 허용.
        """
        groups = self._load_groups()
        if group_name not in groups:
            groups[group_name] = []
        for scenario_name in scenario_names:
            groups[group_name].append({
                "name": scenario_name,
                "on_pass_goto": None,
                "on_fail_goto": None,
                "play_count": 1,
            })
        self._save_groups(groups)
        return groups

    def remove_from_group_by_index(self, group_name: str, index: int) -> dict[str, list[dict]]:
        groups = self._load_groups()
        if group_name in groups and 0 <= index < len(groups[group_name]):
            groups[group_name].pop(index)
            # 제거된 멤버 이후의 goto 참조 재매핑
            self._remap_group_jumps_after_remove(groups[group_name], index)
        self._save_groups(groups)
        return groups

    @staticmethod
    def _remap_jump_idx(jump, removed_idx: int):
        """제거된 인덱스 이후의 jump 참조를 -1 시프트. 제거 대상을 가리키면 None 반환."""
        if jump is None:
            return jump
        if isinstance(jump, dict):
            sc = jump.get("scenario", -1)
            if sc == -1:
                return jump
            if sc == removed_idx:
                return None
            if sc > removed_idx:
                return {**jump, "scenario": sc - 1}
            return jump
        return jump

    def _remap_group_jumps_after_remove(self, members: list[dict], removed_idx: int):
        """그룹 멤버 제거 후 모든 jump 참조를 재매핑."""
        for m in members:
            m["on_pass_goto"] = self._remap_jump_idx(m.get("on_pass_goto"), removed_idx)
            m["on_fail_goto"] = self._remap_jump_idx(m.get("on_fail_goto"), removed_idx)
            for sj in (m.get("step_jumps") or {}).values():
                sj["on_pass_goto"] = self._remap_jump_idx(sj.get("on_pass_goto"), removed_idx)
                sj["on_fail_goto"] = self._remap_jump_idx(sj.get("on_fail_goto"), removed_idx)

    def reorder_group(self, group_name: str, ordered_indices: list[int]) -> dict[str, list[dict]]:
        """기존 멤버 순서를 새 순서로 재배치. ordered_indices는 기존 인덱스의 순열."""
        groups = self._load_groups()
        if group_name in groups:
            old_members = groups[group_name]
            # 이전 인덱스 → 새 인덱스 매핑
            idx_remap = {old_i: new_i for new_i, old_i in enumerate(ordered_indices)}
            new_members = [old_members[i] for i in ordered_indices if 0 <= i < len(old_members)]
            # jump 참조의 scenario 인덱스를 새 인덱스로 재매핑
            for m in new_members:
                m["on_pass_goto"] = self._remap_jump_reorder(m.get("on_pass_goto"), idx_remap)
                m["on_fail_goto"] = self._remap_jump_reorder(m.get("on_fail_goto"), idx_remap)
                for sj in (m.get("step_jumps") or {}).values():
                    sj["on_pass_goto"] = self._remap_jump_reorder(sj.get("on_pass_goto"), idx_remap)
                    sj["on_fail_goto"] = self._remap_jump_reorder(sj.get("on_fail_goto"), idx_remap)
            groups[group_name] = new_members
        self._save_groups(groups)
        return groups

    @staticmethod
    def _remap_jump_reorder(jump, idx_remap: dict):
        """순서 변경 시 jump의 scenario 인덱스를 새 인덱스로 매핑."""
        if jump is None:
            return jump
        if isinstance(jump, dict):
            sc = jump.get("scenario", -1)
            if sc == -1:
                return jump
            new_sc = idx_remap.get(sc, sc)
            return {**jump, "scenario": new_sc}
        return jump

    def update_group_jumps(self, group_name: str, index: int, on_pass_goto, on_fail_goto) -> dict[str, list[dict]]:
        """Update conditional jump settings for a scenario in a group."""
        groups = self._load_groups()
        if group_name in groups and 0 <= index < len(groups[group_name]):
            groups[group_name][index]["on_pass_goto"] = on_pass_goto
            groups[group_name][index]["on_fail_goto"] = on_fail_goto
        self._save_groups(groups)
        return groups

    def update_group_play_count(self, group_name: str, index: int, play_count: int) -> dict[str, list[dict]]:
        """Update per-member play count for a scenario in a group."""
        try:
            pc = int(play_count)
        except (TypeError, ValueError):
            pc = 1
        if pc < 1:
            pc = 1
        groups = self._load_groups()
        if group_name in groups and 0 <= index < len(groups[group_name]):
            groups[group_name][index]["play_count"] = pc
        self._save_groups(groups)
        return groups

    def update_group_step_jumps(
        self,
        group_name: str,
        index: int,
        step_id: int,
        on_pass_goto,
        on_fail_goto,
        exclude_pass_from_result: bool = False,
        exclude_fail_from_result: bool = False,
    ) -> dict[str, list[dict]]:
        """Update conditional jump settings for a specific step within a scenario in a group.

        exclude_pass_from_result / exclude_fail_from_result: 체크 시 해당 방향(pass/fail)
        결과를 최종 집계·시나리오 판정에서 제외하고 Status를 '분기(branch)'로 중립 표시한다.
        (점프 분기 판단에는 실제 pass/fail을 그대로 사용 — 표시·집계에서만 제외)
        """
        groups = self._load_groups()
        if group_name in groups and 0 <= index < len(groups[group_name]):
            entry = groups[group_name][index]
            if "step_jumps" not in entry:
                entry["step_jumps"] = {}
            key = str(step_id)
            has_jump = on_pass_goto is not None or on_fail_goto is not None
            has_exclude = bool(exclude_pass_from_result) or bool(exclude_fail_from_result)
            if not has_jump and not has_exclude:
                entry["step_jumps"].pop(key, None)
            else:
                entry["step_jumps"][key] = {
                    "on_pass_goto": on_pass_goto,
                    "on_fail_goto": on_fail_goto,
                    "exclude_pass_from_result": bool(exclude_pass_from_result),
                    "exclude_fail_from_result": bool(exclude_fail_from_result),
                }
            # Clean up empty step_jumps
            if not entry["step_jumps"]:
                del entry["step_jumps"]
        self._save_groups(groups)
        return groups

    # ------------------------------------------------------------------
    # Copy & Merge
    # ------------------------------------------------------------------

    async def copy_scenario(self, source_name: str, target_name: str) -> Scenario:
        """Copy a scenario with a new name, including screenshots."""
        target_name = validate_entity_name(target_name, "시나리오 이름")
        source = await self.load_scenario(source_name)
        source.name = target_name
        source.created_at = datetime.now(timezone.utc).isoformat()
        source.updated_at = source.created_at

        # Remap expected_image filenames
        src_ss_dir = SCREENSHOTS_DIR / source_name
        tgt_ss_dir = SCREENSHOTS_DIR / target_name
        tgt_ss_dir.mkdir(parents=True, exist_ok=True)

        for step in source.steps:
            # 파일명은 step.uid 기준 — uid 는 불변이고 시나리오 내에서 유일하므로
            # 옛 `_step_NNN_` 방식처럼 번호가 밀려 충돌할 여지가 없다.
            # 대상 폴더가 새 폴더라 uid 만으로 유일성이 보장된다(접미사 파싱 불필요).
            if step.expected_image:
                old_file = src_ss_dir / step.expected_image
                ext = Path(step.expected_image).suffix or ".png"
                new_filename = f"{target_name}_{step.uid}{ext}"
                new_file = tgt_ss_dir / new_filename
                if old_file.exists():
                    shutil.copy2(str(old_file), str(new_file))
                step.expected_image = new_filename
            # multi_crop 이미지도 복사
            for ci_idx, ci in enumerate(step.expected_images):
                if ci.image:
                    old_ci = src_ss_dir / ci.image
                    new_ci_name = f"{target_name}_{step.uid}_crop_{ci_idx:02d}.png"
                    new_ci = tgt_ss_dir / new_ci_name
                    if old_ci.exists():
                        shutil.copy2(str(old_ci), str(new_ci))
                    ci.image = new_ci_name
            # IMAGE_TAP 템플릿 이미지도 복사 (params.template)
            if step.type == StepType.IMAGE_TAP and step.params:
                tpl = step.params.get("template")
                if tpl:
                    old_tpl = src_ss_dir / tpl
                    ext = Path(tpl).suffix or ".png"
                    new_tpl_name = f"{target_name}_{step.uid}_imgtap{ext}"
                    new_tpl = tgt_ss_dir / new_tpl_name
                    if old_tpl.exists():
                        shutil.copy2(str(old_tpl), str(new_tpl))
                    step.params["template"] = new_tpl_name

        await self.save_scenario(source)
        return source

    # ------------------------------------------------------------------
    # Export / Import
    # ------------------------------------------------------------------

    async def export_zip(self, scenario_names: list[str], group_names: list[str]) -> bytes:
        """Export selected scenarios and groups as a ZIP archive."""
        groups = self._load_groups()

        # Resolve: add scenarios referenced by selected groups
        all_scenario_names = set(scenario_names)
        selected_groups: dict[str, list[dict]] = {}
        for gn in group_names:
            if gn in groups:
                selected_groups[gn] = groups[gn]
                for m in groups[gn]:
                    all_scenario_names.add(m["name"])

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            manifest = {
                "version": 1,
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "scenarios": sorted(all_scenario_names),
                "groups": sorted(selected_groups.keys()),
            }
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

            # Scenario JSONs
            for name in sorted(all_scenario_names):
                spath = SCENARIOS_DIR / f"{name}.json"
                if spath.exists():
                    zf.write(spath, f"scenarios/{name}.json")

            # Screenshots
            for name in sorted(all_scenario_names):
                ss_dir = SCREENSHOTS_DIR / name
                if ss_dir.is_dir():
                    for fpath in ss_dir.rglob("*"):
                        if fpath.is_file() and "actual" not in fpath.parts:
                            arcname = f"screenshots/{name}/{fpath.relative_to(ss_dir).as_posix()}"
                            zf.write(fpath, arcname)

            # Groups
            if selected_groups:
                zf.writestr("groups.json", json.dumps(selected_groups, ensure_ascii=False, indent=2))

        return buf.getvalue()

    async def import_preview(self, zip_data: bytes) -> dict:
        """Analyze a ZIP for conflicts before importing."""
        existing_scenarios = set(await self.list_scenarios())
        existing_groups = set(self._load_groups().keys())

        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            manifest = {}
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json"))

            scenario_names = manifest.get("scenarios", [])
            group_names = manifest.get("groups", [])

            # Fallback: scan for scenario files if no manifest
            if not scenario_names:
                for n in zf.namelist():
                    if n.startswith("scenarios/") and n.endswith(".json"):
                        scenario_names.append(Path(n).stem)

            if not group_names and "groups.json" in zf.namelist():
                gdata = json.loads(zf.read("groups.json"))
                group_names = list(gdata.keys())

            scenarios_info = []
            # manifest 없는 레거시 ZIP은 zip 엔트리 순서 그대로라 이름순으로 정렬
            for sn in sorted(scenario_names, key=str.casefold):
                scenarios_info.append({"name": sn, "conflict": sn in existing_scenarios})

            groups_info = []
            for gn in group_names:
                groups_info.append({"name": gn, "conflict": gn in existing_groups})

        return {"scenarios": scenarios_info, "groups": groups_info}

    async def import_apply(self, zip_data: bytes, resolutions: dict) -> dict:
        """Apply import from ZIP with conflict resolutions.

        resolutions = {
            "scenarios": {"name": {"action": "overwrite|rename|skip", "new_name": "..."}},
            "groups": {"name": {"action": "overwrite|rename|skip|merge", "new_name": "..."}},
        }
        """
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        scenario_res = resolutions.get("scenarios", {})
        group_res = resolutions.get("groups", {})
        imported_scenarios: list[str] = []
        imported_groups: list[str] = []
        skipped: list[str] = []

        with zipfile.ZipFile(io.BytesIO(zip_data), "r") as zf:
            manifest = {}
            if "manifest.json" in zf.namelist():
                manifest = json.loads(zf.read("manifest.json"))

            # --- Import scenarios ---
            scenario_names = manifest.get("scenarios", [])
            if not scenario_names:
                for n in zf.namelist():
                    if n.startswith("scenarios/") and n.endswith(".json"):
                        scenario_names.append(Path(n).stem)

            name_map: dict[str, str] = {}  # original -> final name

            for orig_name in scenario_names:
                res = scenario_res.get(orig_name, {"action": "import"})
                action = res.get("action", "import")
                if action == "skip":
                    skipped.append(orig_name)
                    continue

                final_name = orig_name
                if action == "rename":
                    final_name = res.get("new_name", orig_name)

                # 경로/URL 을 깨뜨리는 문자가 든 이름은 가져오기 거부 — 사용자가
                # 충돌 해결(rename)로 정상 이름을 지정하도록 유도한다.
                final_name = validate_entity_name(final_name, "시나리오 이름")

                name_map[orig_name] = final_name

                # Read scenario JSON
                json_path = f"scenarios/{orig_name}.json"
                if json_path in zf.namelist():
                    sdata = json.loads(zf.read(json_path))
                    sdata["name"] = final_name
                    # Remap expected_image filenames if renamed
                    if final_name != orig_name:
                        for step in sdata.get("steps", []):
                            if step.get("expected_image"):
                                step["expected_image"] = step["expected_image"].replace(orig_name, final_name, 1)
                            new_imgs = []
                            for ci in step.get("expected_images", []):
                                if ci.get("image"):
                                    ci["image"] = ci["image"].replace(orig_name, final_name, 1)
                                new_imgs.append(ci)
                            step["expected_images"] = new_imgs
                            # IMAGE_TAP 템플릿 파일명도 함께 갱신
                            if step.get("type") == "image_tap":
                                params = step.get("params") or {}
                                tpl = params.get("template")
                                if tpl:
                                    params["template"] = tpl.replace(orig_name, final_name, 1)
                                    step["params"] = params

                    out_path = SCENARIOS_DIR / f"{final_name}.json"
                    out_path.write_text(json.dumps(sdata, ensure_ascii=False, indent=2), encoding="utf-8")

                # Extract screenshots
                ss_prefix = f"screenshots/{orig_name}/"
                tgt_dir = SCREENSHOTS_DIR / final_name
                tgt_dir.mkdir(parents=True, exist_ok=True)
                for entry in zf.namelist():
                    if entry.startswith(ss_prefix) and not entry.endswith("/"):
                        rel = entry[len(ss_prefix):]
                        if final_name != orig_name:
                            rel = rel.replace(orig_name, final_name, 1)
                        out_file = tgt_dir / rel
                        out_file.parent.mkdir(parents=True, exist_ok=True)
                        out_file.write_bytes(zf.read(entry))

                imported_scenarios.append(final_name)

            # --- Import groups ---
            if "groups.json" in zf.namelist():
                imported_groups_data = json.loads(zf.read("groups.json"))
                existing_groups = self._load_groups()

                for gname, members in imported_groups_data.items():
                    res = group_res.get(gname, {"action": "import"})
                    action = res.get("action", "import")
                    if action == "skip":
                        skipped.append(f"group:{gname}")
                        continue

                    final_gname = gname
                    if action == "rename":
                        final_gname = res.get("new_name", gname)

                    final_gname = validate_entity_name(final_gname, "그룹 이름")

                    # Remap member scenario names
                    remapped = []
                    for m in members:
                        orig_sname = m["name"]
                        mapped = name_map.get(orig_sname, orig_sname)
                        if mapped not in skipped:
                            m["name"] = mapped
                            remapped.append(m)

                    if action == "merge" and gname in existing_groups:
                        existing_names = {m["name"] for m in existing_groups[gname]}
                        for m in remapped:
                            if m["name"] not in existing_names:
                                existing_groups[gname].append(m)
                        existing_groups[final_gname] = existing_groups.pop(gname, existing_groups.get(final_gname, []))
                    else:
                        existing_groups[final_gname] = remapped

                    imported_groups.append(final_gname)

                self._save_groups(existing_groups)

        return {
            "imported_scenarios": imported_scenarios,
            "imported_groups": imported_groups,
            "skipped": skipped,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _execute_step_action(self, step_type: StepType, params: dict, device_id: str = "") -> str | None:
        """Execute an action on the target device. Returns response for serial_command."""
        if step_type == StepType.MODULE_COMMAND:
            from .module_service import execute_module_function
            module_name = params.get("module", "")
            func_name = params.get("function", "")
            func_args = params.get("args", {})
            # Pass device connection info + shared serial connection
            ctor_kwargs = None
            shared_conn = None
            if device_id:
                dev = self.dm.get_device(device_id)
                if dev:
                    ctor_kwargs = _build_ctor_kwargs(dev)
                    shared_conn = self.dm.get_serial_conn(device_id)
            await execute_module_function(module_name, func_name, func_args, ctor_kwargs, shared_conn)
            return None
        elif step_type == StepType.SERIAL_COMMAND:
            if not device_id:
                raise ValueError("serial_command requires a device_id")
            response = await self.dm.send_serial_command(
                device_id,
                params["data"],
                params.get("read_timeout", 1.0),
            )
            return response
        elif step_type in (StepType.HKMC_TOUCH, StepType.HKMC_SWIPE, StepType.HKMC_KEY, StepType.HKMC_LONG_PRESS, StepType.HKMC_MULTI_TOUCH):
            if not device_id:
                raise ValueError("HKMC/iSAP step requires a device_id")
            dev = self.dm.get_device(device_id)
            svc = None
            is_isap = dev and dev.type == "isap_agent"
            if is_isap:
                svc = self.dm.get_isap_service(device_id)
            else:
                svc = self.dm.get_hkmc_service(device_id)
            if not svc:
                raise ValueError(f"HKMC/iSAP device {device_id} not connected")
            screen_type = params.get("screen_type", "front_center")
            if step_type == StepType.HKMC_TOUCH:
                await svc.async_tap(params["x"], params["y"], screen_type)
            elif step_type == StepType.HKMC_LONG_PRESS:
                await svc.async_long_press(params["x"], params["y"],
                                           int(params.get("duration_ms", 3000)), screen_type)
            elif step_type == StepType.HKMC_SWIPE:
                await svc.async_swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                                      screen_type, int(params.get("duration_ms", 0)),
                                      hold_ms=int(params.get("hold_ms", 0) or 0))
            elif step_type == StepType.HKMC_MULTI_TOUCH:
                if not is_isap:
                    raise ValueError("HKMC_MULTI_TOUCH은 iSAP 디바이스 전용입니다")
                fingers = params.get("fingers", [])
                if not fingers:
                    raise ValueError("HKMC_MULTI_TOUCH requires fingers array")
                is_tap = all(f.get("x1") == f.get("x2") and f.get("y1") == f.get("y2")
                             for f in fingers)
                if is_tap:
                    pts = [{"x": f["x1"], "y": f["y1"]} for f in fingers]
                    await svc.async_multi_finger_tap(pts, screen_type)
                else:
                    await svc.async_multi_finger_swipe(
                        fingers, screen_type, int(params.get("duration_ms", 500)),
                        hold_ms=int(params.get("hold_ms", 0) or 0))
            elif step_type == StepType.HKMC_KEY:
                key_name = params.get("key_name")
                hold_ms = int(params.get("hold_ms", 0) or 0)
                if key_name:
                    sub_cmd = params.get("sub_cmd", 0x43)  # SHORT_KEY
                    direction = params.get("direction")
                    if is_isap:
                        await svc.async_send_key_by_name(key_name, sub_cmd, screen_type, direction,
                                                         hold_ms=hold_ms)
                    else:
                        # screen_type 반드시 전달 — 미전달 시 rear_left/rear_right 키의
                        # CCRC_MONITOR_LEFT/RIGHT 자동 라우팅이 동작하지 않아 리어 모니터로
                        # 이벤트가 전달되지 않음 (HKMC 스텝 테스트가 안 먹는 증상).
                        monitor = params.get("monitor", 0x00)
                        await svc.async_send_key_by_name(key_name, sub_cmd, monitor, direction, screen_type,
                                                         hold_ms=hold_ms)
                else:
                    if is_isap:
                        await svc.async_send_key(
                            params["cmd"], params["sub_cmd"], params["key_data"],
                            screen_type, params.get("direction"),
                        )
                    else:
                        await svc.async_send_key(
                            params["cmd"], params["sub_cmd"], params["key_data"],
                            params.get("monitor", 0x00), params.get("direction"),
                        )
        elif step_type in (StepType.ICAS_TOUCH, StepType.ICAS_SWIPE, StepType.ICAS_KEY, StepType.ICAS_LONG_PRESS):
            if not device_id:
                raise ValueError("ICAS step requires a device_id")
            # ICAS step types are shared with MIB (same ksend mechanism) — try both services
            dev = self.dm.get_device(device_id)
            svc = None
            if dev and dev.type == "mib_agent":
                svc = self.dm.get_mib_service(device_id)
            else:
                svc = self.dm.get_icas_service(device_id)
            if not svc:
                raise ValueError(f"ICAS/MIB device {device_id} not connected")
            screen_type = params.get("screen_type", "HU")
            if step_type == StepType.ICAS_TOUCH:
                await svc.async_tap(params["x"], params["y"], screen_type)
            elif step_type == StepType.ICAS_LONG_PRESS:
                await svc.async_long_press(params["x"], params["y"],
                                           int(params.get("duration_ms", 3000)), screen_type)
            elif step_type == StepType.ICAS_SWIPE:
                await svc.async_swipe(params["x1"], params["y1"], params["x2"], params["y2"],
                                      screen_type, int(params.get("duration_ms", 0)),
                                      hold_ms=int(params.get("hold_ms", 0) or 0))
            elif step_type == StepType.ICAS_KEY:
                key_name = params.get("key_name")
                if key_name:
                    sub_cmd = params.get("sub_cmd", 0x43)
                    direction = params.get("direction")
                    hold_ms = int(params.get("hold_ms", 0) or 0)
                    await svc.async_send_key_by_name(key_name, sub_cmd, screen_type, direction,
                                                     hold_ms=hold_ms if hold_ms > 0 else None)
                else:
                    await svc.async_send_key(
                        params["cmd"], params["sub_cmd"], params["key_data"],
                        screen_type, params.get("direction"),
                    )
        elif step_type in (StepType.WIN_TAP, StepType.WIN_DOUBLE_CLICK,
                           StepType.WIN_LONG_PRESS, StepType.WIN_SWIPE,
                           StepType.WIN_INPUT_TEXT, StepType.WIN_KEY,
                           StepType.WIN_KEY_COMBO):
            wc = self.dm.get_wincontrol_service()
            if not wc.is_available():
                raise ValueError(
                    f"WinControl unavailable: {wc.import_error() or 'pywin32 not installed'}"
                )
            import asyncio
            loop = asyncio.get_event_loop()
            # 자동 attach: params에 프로세스 정보가 포함된 경우 끊겼으면 재임베드/실행.
            proc_name = str(params.get("process_name", "") or "")
            exe_path = str(params.get("exe_path", "") or "")
            title_pattern = str(params.get("window_title", "") or "")
            class_name = str(params.get("window_class", "") or "")
            aumid = str(params.get("process_aumid", "") or "")
            if proc_name or exe_path or title_pattern or aumid:
                try:
                    import functools as _ft
                    await loop.run_in_executor(
                        None,
                        _ft.partial(
                            wc.ensure_attached,
                            process_name=proc_name, exe_path=exe_path,
                            title_pattern=title_pattern, class_name=class_name,
                            aumid=aumid,
                            launch_if_missing=True,
                            wait_seconds=float(params.get("launch_wait_seconds", 8.0) or 8.0),
                            target_width=int(params.get("window_width", 0) or 0),
                            target_height=int(params.get("window_height", 0) or 0),
                        ),
                    )
                except Exception as e:
                    raise ValueError(f"WinControl attach failed: {e}")
            elif not wc.is_attached():
                raise ValueError("WinControl: no window attached")
            import functools as _ft3
            if step_type == StepType.WIN_TAP:
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_tap, int(params["x"]), int(params["y"]),
                                 params.get("button", "left")))
            elif step_type == StepType.WIN_DOUBLE_CLICK:
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_double_click,
                                 int(params["x"]), int(params["y"])))
            elif step_type == StepType.WIN_LONG_PRESS:
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_long_press,
                                 int(params["x"]), int(params["y"]),
                                 int(params.get("duration_ms", 500)),
                                 params.get("button", "left")))
            elif step_type == StepType.WIN_SWIPE:
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_swipe,
                                 int(params["x1"]), int(params["y1"]),
                                 int(params["x2"]), int(params["y2"]),
                                 int(params.get("duration_ms", 300))))
            elif step_type == StepType.WIN_INPUT_TEXT:
                cfx = params.get("click_first_x")
                cfy = params.get("click_first_y")
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_text, str(params.get("text", "")),
                                 int(cfx) if cfx is not None else None,
                                 int(cfy) if cfy is not None else None))
            elif step_type == StepType.WIN_KEY:
                await loop.run_in_executor(None,
                    _ft3.partial(wc.send_key, str(params.get("key", ""))))
            elif step_type == StepType.WIN_KEY_COMBO:
                raw = params.get("keys") if "keys" in params else params.get("combo", "")
                if isinstance(raw, str):
                    import re as _re
                    keys_list = [s.strip() for s in _re.split(r"[+,]", raw) if s.strip()]
                else:
                    keys_list = [str(k).strip() for k in (raw or []) if str(k).strip()]
                if keys_list:
                    await loop.run_in_executor(None,
                        _ft3.partial(wc.send_key_combo, keys_list))
        elif step_type == StepType.WAIT:
            await _async_sleep(params.get("duration_ms", 1000) / 1000.0)
        else:
            # ADB actions — use device_id or fallback to active device
            serial = device_id or await self.adb.get_active_device()
            if not serial:
                raise ValueError("No ADB device specified")
            if step_type == StepType.TAP:
                await self.adb.tap(params["x"], params["y"], serial=serial)
            elif step_type == StepType.LONG_PRESS:
                await self.adb.long_press(params["x"], params["y"], params.get("duration_ms", 1000), serial=serial)
            elif step_type == StepType.SWIPE:
                await self.adb.swipe(
                    params["x1"], params["y1"],
                    params["x2"], params["y2"],
                    params.get("duration_ms", 300),
                    serial=serial,
                    hold_ms=int(params.get("hold_ms", 0) or 0),
                )
            elif step_type == StepType.INPUT_TEXT:
                await self.adb.input_text(params["text"], serial=serial)
            elif step_type == StepType.KEY_EVENT:
                await self.adb.key_event(params["keycode"], serial=serial)
            elif step_type == StepType.ADB_COMMAND:
                await self.adb.run_shell_command(params["command"], serial=serial)
            elif step_type == StepType.MULTI_TOUCH:
                fingers = params.get("fingers", [])
                is_tap = all(f.get("x1") == f.get("x2") and f.get("y1") == f.get("y2") for f in fingers)
                if is_tap:
                    points = [{"x": f["x1"], "y": f["y1"]} for f in fingers]
                    await self.adb.multi_finger_tap(points, serial=serial)
                else:
                    await self.adb.multi_finger_swipe(fingers, params.get("duration_ms", 500), serial=serial)

        return None


async def _async_sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
