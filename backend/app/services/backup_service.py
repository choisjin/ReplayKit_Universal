"""자동/수동 백업 및 복원 서비스.

재생에 필요한 전체 자산(모든 시나리오 JSON + 스크린샷 + 그룹/폴더 메타)을
타임스탬프 ZIP 스냅샷으로 저장하고, 전체/개별 시나리오 단위로 복원한다.

저장 위치:
  - 내부: backend/backups/  (기본, .gitignore — git-pull/업데이트에도 보존)
  - 외부: settings.backup_dir 이 지정되면 그 폴더에도 동일 스냅샷을 복사
    (디스크/PC 통째 고장까지 대비하려면 외부 드라이브 지정 권장)

ZIP 구조(recording_service.import_apply 와 호환 — 복원 시 재사용):
    manifest.json                    스냅샷 메타(생성시각/사유/개수/무결성 서명)
    scenarios/<name>.json            전체 시나리오
    screenshots/<name>/...           전체 스크린샷(실행 결과 actual_* 제외)
    groups.json                      전체 그룹
    folders.json / group_folders.json 폴더 메타
    settings.json                    참고용 스냅샷(복원 시 자동 적용하지 않음)

스케줄러: settings.backup_interval_minutes 주기로 create_backup("auto").
마지막 스냅샷과 내용 서명(content_sig)이 같으면 건너뛴다(중복 방지).
복원(전체/개별) 직전에는 항상 안전 백업("pre-restore")을 먼저 만들어 되돌릴 수 있게 한다.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from .recording_service import (
    SCENARIOS_DIR,
    SCREENSHOTS_DIR,
    GROUPS_FILE,
    FOLDERS_FILE,
    GROUP_FOLDERS_FILE,
)

logger = logging.getLogger(__name__)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent  # .../backend
INTERNAL_BACKUPS_DIR = _BACKEND_ROOT / "backups"
_SETTINGS_FILE = _BACKEND_ROOT / "settings.json"

_MANIFEST_VERSION = 2
_BACKUP_PREFIX = "backup_"
_TS_FMT = "%Y%m%d_%H%M%S"

# scenarios/ 안의 메타 파일들은 시나리오 JSON 이 아니므로 개별 시나리오 취급에서 제외
_META_JSON_NAMES = {"groups.json", "folders.json", "group_folders.json"}


# ----------------------------------------------------------------------
# 설정 로드 (settings 라우터에 의존하지 않고 파일 직접 읽음 — 순환 import 회피)
# ----------------------------------------------------------------------

def _load_settings() -> dict:
    try:
        if _SETTINGS_FILE.exists():
            return json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug("[backup] settings 로드 실패: %s", e)
    return {}


def _backup_dirs(settings: Optional[dict] = None) -> list[Path]:
    """스냅샷을 쓸 디렉토리 목록. 항상 내부 + (지정 시)외부."""
    settings = settings if settings is not None else _load_settings()
    dirs = [INTERNAL_BACKUPS_DIR]
    ext = (settings.get("backup_dir") or "").strip()
    if ext:
        p = Path(ext)
        if p != INTERNAL_BACKUPS_DIR:
            dirs.append(p)
    return dirs


# ----------------------------------------------------------------------
# 스냅샷 생성
# ----------------------------------------------------------------------

def _iter_source_files() -> list[tuple[Path, str]]:
    """백업에 담을 (실제경로, ZIP내경로) 목록을 수집한다."""
    entries: list[tuple[Path, str]] = []

    # 시나리오 JSON (메타 파일 포함 — groups/folders/group_folders 는 별도 arcname 로도 넣음)
    if SCENARIOS_DIR.is_dir():
        for jp in sorted(SCENARIOS_DIR.glob("*.json")):
            if jp.name in _META_JSON_NAMES:
                continue
            entries.append((jp, f"scenarios/{jp.name}"))

    # 스크린샷 (실행 결과 actual_* 는 재생에 불필요 → 제외해 용량 절약)
    if SCREENSHOTS_DIR.is_dir():
        for fp in sorted(SCREENSHOTS_DIR.rglob("*")):
            if not fp.is_file():
                continue
            if "actual" in fp.name:
                continue
            rel = fp.relative_to(SCREENSHOTS_DIR).as_posix()
            # 기대이미지 교체 시 남긴 직전 세대 백업(_prev/) 은 재생에 불필요 → 제외
            if "/_prev/" in rel:
                continue
            entries.append((fp, f"screenshots/{rel}"))

    return entries


def _content_sig(entries: list[tuple[Path, str]]) -> str:
    """포함 파일들의 (경로,크기,수정시각) 으로 내용 서명 계산 — 중복 스냅샷 방지용."""
    h = hashlib.sha256()
    for path, arc in entries:
        try:
            st = path.stat()
            h.update(arc.encode("utf-8"))
            h.update(str(st.st_size).encode())
            h.update(str(int(st.st_mtime)).encode())
        except OSError:
            continue
    return h.hexdigest()


def _latest_backup(dir_: Path) -> Optional[Path]:
    if not dir_.is_dir():
        return None
    files = sorted(dir_.glob(f"{_BACKUP_PREFIX}*.zip"))
    return files[-1] if files else None


def _read_manifest(zip_path: Path) -> dict:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            if "manifest.json" in zf.namelist():
                return json.loads(zf.read("manifest.json"))
    except Exception as e:
        logger.debug("[backup] manifest 읽기 실패 %s: %s", zip_path, e)
    return {}


def create_backup(reason: str = "manual", force: bool = False) -> dict:
    """전체 스냅샷 ZIP 을 만들어 내부(+외부) 백업 디렉토리에 저장한다.

    reason: "manual" | "auto" | "pre-restore" 등 — manifest 와 목록에 표시.
    force:  True 면 content_sig 가 같아도 건너뛰지 않고 무조건 생성.

    Returns: {status: "created"|"skipped", id, ...meta}
    """
    settings = _load_settings()
    entries = _iter_source_files()
    sig = _content_sig(entries)

    # 중복 방지: 최신 스냅샷과 내용 서명이 같으면 auto 는 건너뜀
    if not force:
        latest = _latest_backup(INTERNAL_BACKUPS_DIR)
        if latest is not None:
            prev = _read_manifest(latest)
            if prev.get("content_sig") == sig:
                return {"status": "skipped", "reason": "no-change", "id": latest.stem}

    now = datetime.now()
    # 같은 초에 여러 번(예: pre-restore 직후) 생성돼도 id 가 겹쳐 덮어쓰지 않도록 유일화
    base_id = f"{_BACKUP_PREFIX}{now.strftime(_TS_FMT)}"
    backup_id = base_id
    _n = 2
    _dirs = _backup_dirs(settings)
    while any((d / f"{backup_id}.zip").exists() for d in _dirs):
        backup_id = f"{base_id}_{_n}"
        _n += 1

    scenario_names = [
        Path(arc).stem for _, arc in entries
        if arc.startswith("scenarios/") and arc.endswith(".json")
    ]
    ss_count = sum(1 for _, arc in entries if arc.startswith("screenshots/"))

    groups = _read_json(GROUPS_FILE, default={})
    folders = _read_json(FOLDERS_FILE, default={})
    group_folders = _read_json(GROUP_FOLDERS_FILE, default={})

    manifest = {
        "version": _MANIFEST_VERSION,
        "id": backup_id,
        "created_at": now.astimezone().isoformat(),
        "reason": reason,
        "content_sig": sig,
        "scenarios": sorted(scenario_names, key=str.casefold),
        "groups": sorted(groups.keys(), key=str.casefold),
        "counts": {
            "scenarios": len(scenario_names),
            "screenshots": ss_count,
            "groups": len(groups),
        },
    }

    # ZIP 을 메모리에 만든 뒤 각 대상 디렉토리에 기록 (동일 바이트 복사)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        for path, arc in entries:
            try:
                zf.write(path, arc)
            except OSError as e:
                logger.warning("[backup] 파일 담기 실패 %s: %s", path, e)
        if groups:
            zf.writestr("groups.json", json.dumps(groups, ensure_ascii=False, indent=2))
        if folders:
            zf.writestr("folders.json", json.dumps(folders, ensure_ascii=False, indent=2))
        if group_folders:
            zf.writestr("group_folders.json", json.dumps(group_folders, ensure_ascii=False, indent=2))
        if _SETTINGS_FILE.exists():
            try:
                zf.write(_SETTINGS_FILE, "settings.json")
            except OSError:
                pass
    data = buf.getvalue()

    written: list[str] = []
    for d in _backup_dirs(settings):
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / f"{backup_id}.zip").write_bytes(data)
            written.append(str(d))
        except Exception as e:
            logger.warning("[backup] %s 에 저장 실패: %s", d, e)

    if not written:
        raise RuntimeError("백업을 저장할 수 있는 위치가 없습니다(내부/외부 모두 실패).")

    # 보존 정책 적용
    keep = int(settings.get("backup_keep", 10) or 10)
    for d in _backup_dirs(settings):
        _apply_retention(d, keep)

    logger.info("[backup] 생성 %s reason=%s size=%dKB → %s",
                backup_id, reason, len(data) // 1024, written)
    return {
        "status": "created",
        "id": backup_id,
        "size": len(data),
        "written_to": written,
        **manifest,
    }


def _apply_retention(dir_: Path, keep: int) -> None:
    """디렉토리에서 오래된 스냅샷을 keep 개만 남기고 삭제한다."""
    if keep <= 0 or not dir_.is_dir():
        return
    files = sorted(dir_.glob(f"{_BACKUP_PREFIX}*.zip"))
    excess = len(files) - keep
    for f in files[:max(0, excess)]:
        try:
            f.unlink()
            logger.info("[backup] 보존정책으로 삭제: %s", f.name)
        except OSError as e:
            logger.warning("[backup] 삭제 실패 %s: %s", f, e)


# ----------------------------------------------------------------------
# 목록 / 조회
# ----------------------------------------------------------------------

def _find_backup(backup_id: str) -> Optional[Path]:
    """id 로 실제 zip 경로를 찾는다(내부 우선, 없으면 외부)."""
    if not backup_id or "/" in backup_id or "\\" in backup_id or ".." in backup_id:
        return None
    for d in _backup_dirs():
        p = d / f"{backup_id}.zip"
        if p.exists():
            return p
    return None


def list_backups() -> list[dict]:
    """모든 백업 스냅샷을 최신순으로 반환(내부+외부, id 중복 제거)."""
    seen: dict[str, dict] = {}
    for d in _backup_dirs():
        if not d.is_dir():
            continue
        for zp in d.glob(f"{_BACKUP_PREFIX}*.zip"):
            bid = zp.stem
            if bid in seen:
                seen[bid]["locations"].append(str(d))
                continue
            man = _read_manifest(zp)
            try:
                size = zp.stat().st_size
            except OSError:
                size = 0
            seen[bid] = {
                "id": bid,
                "created_at": man.get("created_at", ""),
                "reason": man.get("reason", "unknown"),
                "counts": man.get("counts", {}),
                "scenarios": man.get("scenarios", []),
                "groups": man.get("groups", []),
                "size": size,
                "locations": [str(d)],
            }
    return sorted(seen.values(), key=lambda b: b["id"], reverse=True)


def get_backup_detail(backup_id: str) -> Optional[dict]:
    zp = _find_backup(backup_id)
    if zp is None:
        return None
    man = _read_manifest(zp)
    try:
        size = zp.stat().st_size
    except OSError:
        size = 0
    return {**man, "id": backup_id, "size": size, "path": str(zp)}


def delete_backup(backup_id: str) -> bool:
    """모든 위치(내부+외부)에서 해당 스냅샷 삭제."""
    removed = False
    for d in _backup_dirs():
        p = d / f"{backup_id}.zip"
        if p.exists():
            try:
                p.unlink()
                removed = True
            except OSError as e:
                logger.warning("[backup] 삭제 실패 %s: %s", p, e)
    return removed


def read_backup_bytes(backup_id: str) -> Optional[bytes]:
    zp = _find_backup(backup_id)
    return zp.read_bytes() if zp is not None else None


# ----------------------------------------------------------------------
# 복원
# ----------------------------------------------------------------------

def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _restore_meta_json(zf: zipfile.ZipFile, entry: str, dest: Path) -> None:
    if entry in zf.namelist():
        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zf.read(entry))


def preview_restore(backup_id: str) -> Optional[dict]:
    """복원 전 미리보기 — 백업에 담긴 시나리오와 현재 충돌 여부."""
    zp = _find_backup(backup_id)
    if zp is None:
        return None
    man = _read_manifest(zp)
    existing = {p.stem for p in SCENARIOS_DIR.glob("*.json") if p.name not in _META_JSON_NAMES} \
        if SCENARIOS_DIR.is_dir() else set()
    scenarios = [
        {"name": n, "conflict": n in existing}
        for n in man.get("scenarios", [])
    ]
    return {
        "id": backup_id,
        "created_at": man.get("created_at", ""),
        "reason": man.get("reason", ""),
        "counts": man.get("counts", {}),
        "scenarios": scenarios,
        "groups": man.get("groups", []),
    }


def restore_full(backup_id: str, mode: str = "merge") -> dict:
    """스냅샷 전체를 복원한다.

    mode:
      - "merge"   : 백업의 시나리오/그룹/폴더를 덮어쓰되, 백업에 없는 현재 항목은 유지.
      - "replace" : 현재 시나리오/스크린샷/그룹/폴더를 모두 지우고 백업 상태로 교체.

    복원 직전 현재 상태를 "pre-restore" 백업으로 남겨 되돌릴 수 있게 한다.
    """
    zp = _find_backup(backup_id)
    if zp is None:
        raise FileNotFoundError(f"백업을 찾을 수 없습니다: {backup_id}")

    # 복원 소스를 먼저 메모리로 읽어둔다 — 이후 pre-restore 백업/보존정책이 이 파일을
    # 건드려도(같은 초 id 충돌 등) 안전하도록 디스크 파일과 분리.
    zip_bytes = zp.read_bytes()

    # 안전망: 복원 전에 현재 상태 백업(강제 — 서명 같아도 남김)
    try:
        create_backup(reason="pre-restore", force=True)
    except Exception as e:
        logger.warning("[backup] pre-restore 백업 실패(복원은 계속): %s", e)

    if mode == "replace":
        _wipe_current_data()

    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()

        # 1) 시나리오 JSON + 스크린샷
        restored_scenarios: list[str] = []
        for entry in names:
            if entry.startswith("scenarios/") and entry.endswith(".json"):
                sname = Path(entry).name
                if sname in _META_JSON_NAMES:
                    continue
                SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
                (SCENARIOS_DIR / sname).write_bytes(zf.read(entry))
                restored_scenarios.append(Path(sname).stem)

        for entry in names:
            if entry.startswith("screenshots/") and not entry.endswith("/"):
                rel = entry[len("screenshots/"):]
                out = SCREENSHOTS_DIR / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(zf.read(entry))

        # 2) 그룹/폴더 메타
        if mode == "replace":
            _restore_meta_json(zf, "groups.json", GROUPS_FILE)
            _restore_meta_json(zf, "folders.json", FOLDERS_FILE)
            _restore_meta_json(zf, "group_folders.json", GROUP_FOLDERS_FILE)
        else:
            _merge_meta_json(zf, "groups.json", GROUPS_FILE)
            _merge_meta_json(zf, "folders.json", FOLDERS_FILE)
            _merge_meta_json(zf, "group_folders.json", GROUP_FOLDERS_FILE)

    logger.info("[backup] 복원(%s) 완료: %d개 시나리오 from %s",
                mode, len(restored_scenarios), backup_id)
    return {"status": "restored", "mode": mode,
            "restored_scenarios": restored_scenarios}


def restore_scenario(backup_id: str, scenario_name: str) -> dict:
    """백업에서 특정 시나리오 하나만 복원(현재 항목 덮어씀).

    복원 직전 현재 상태를 pre-restore 백업으로 남긴다.
    """
    zp = _find_backup(backup_id)
    if zp is None:
        raise FileNotFoundError(f"백업을 찾을 수 없습니다: {backup_id}")

    # 소스를 먼저 메모리로 읽어 디스크 파일과 분리(같은 초 id 충돌/보존정책 대비)
    zip_bytes = zp.read_bytes()
    json_entry = f"scenarios/{scenario_name}.json"
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        names = zf.namelist()
        if json_entry not in names:
            raise FileNotFoundError(f"백업에 '{scenario_name}' 시나리오가 없습니다.")

        try:
            create_backup(reason="pre-restore", force=True)
        except Exception as e:
            logger.warning("[backup] pre-restore 백업 실패(복원은 계속): %s", e)

        SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
        (SCENARIOS_DIR / f"{scenario_name}.json").write_bytes(zf.read(json_entry))

        # 해당 시나리오의 스크린샷 폴더: 현재 것을 지우고 백업본으로 교체
        tgt_ss = SCREENSHOTS_DIR / scenario_name
        if tgt_ss.exists():
            shutil.rmtree(tgt_ss, ignore_errors=True)
        ss_prefix = f"screenshots/{scenario_name}/"
        for entry in names:
            if entry.startswith(ss_prefix) and not entry.endswith("/"):
                rel = entry[len(ss_prefix):]
                out = tgt_ss / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(zf.read(entry))

    logger.info("[backup] 개별 복원 완료: %s from %s", scenario_name, backup_id)
    return {"status": "restored", "scenario": scenario_name}


def _wipe_current_data() -> None:
    """replace 복원 전 현재 시나리오/스크린샷/메타를 모두 제거."""
    if SCENARIOS_DIR.is_dir():
        for jp in SCENARIOS_DIR.glob("*.json"):
            try:
                jp.unlink()
            except OSError:
                pass
    if SCREENSHOTS_DIR.is_dir():
        for child in SCREENSHOTS_DIR.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink()
            except OSError:
                pass


def _merge_meta_json(zf: zipfile.ZipFile, entry: str, dest: Path) -> None:
    """dict 형태 메타(groups/folders)를 병합 — 백업 키가 현재를 덮어씀."""
    if entry not in zf.namelist():
        return
    try:
        incoming = json.loads(zf.read(entry))
    except Exception:
        return
    if not isinstance(incoming, dict):
        return
    current = _read_json(dest, default={})
    if not isinstance(current, dict):
        current = {}
    current.update(incoming)
    SCENARIOS_DIR.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------
# 스케줄러
# ----------------------------------------------------------------------

def _due_for_auto_backup(interval_minutes: int) -> bool:
    """마지막 자동/수동 스냅샷 이후 interval 이상 지났으면 True.

    파일 타임스탬프 기반이라 서버 재시작에도 주기가 유지되고,
    설정에서 주기를 바꾸면 즉시 반영된다.
    """
    latest = _latest_backup(INTERNAL_BACKUPS_DIR)
    if latest is None:
        return True
    try:
        age_min = (datetime.now().timestamp() - latest.stat().st_mtime) / 60.0
    except OSError:
        return True
    return age_min >= interval_minutes


async def scheduler_loop() -> None:
    """settings.backup_interval_minutes 주기로 자동 백업. lifespan 에서 태스크로 실행."""
    await asyncio.sleep(60)  # 부팅 직후 잠시 대기(다른 startup 작업과 경합 회피)
    while True:
        try:
            cfg = _load_settings()
            if cfg.get("backup_enabled", True):
                interval = max(5, int(cfg.get("backup_interval_minutes", 1440) or 1440))
                if _due_for_auto_backup(interval):
                    result = await asyncio.to_thread(create_backup, "auto")
                    if result.get("status") == "created":
                        logger.info("[backup] 자동 백업 생성: %s", result.get("id"))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[backup] 스케줄러 오류(무시하고 계속): %s", e)
        # 1분마다 조건 확인 — 실제 생성은 주기 도래 시에만
        await asyncio.sleep(60)
