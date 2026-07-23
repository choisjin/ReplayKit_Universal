# -*- coding: utf-8 -*-
"""로그인(사용자 식별) 서비스 — Jira 유저 검색 + 선택 사용자 영속화.

비밀번호 인증이 아니라 **누가 이 PC 를 쓰는지 식별**하는 용도다.
- Jira 계정(검색용)과 프로젝트 목록은 Manager(관제 서버)가 관리하며,
  서버 시작 시 GET {monitor_server_url}/api/login-config 로 받아온다.
  Jira 계정은 이 백엔드에만 머물고 브라우저(프론트)에는 절대 내려주지 않는다.
- 유저 검색은 Jira Server REST(user/search)를 requests 로 직접 호출한다
  (jira 패키지 의존 없음 — person_org_search.py 의 파싱/페이지네이션 이식).
- 선택된 사용자는 backend/login_user.json 에 저장되어 재시작 후에도 유지되고,
  관제 status_update payload 의 "user" 로 실려 관제/통계/버그리포트에 표기된다.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/login_user.json — settings.json 과 같은 위치
_USER_FILE = Path(__file__).resolve().parent.parent.parent / "login_user.json"

# project/model 은 주 디바이스 카탈로그(device_catalog)의 프로젝트/모델에서 고른다.
_USER_FIELDS = ("user_id", "name", "title", "team", "project", "model")

_lock = threading.Lock()
_user_cache: dict | None = None
_user_loaded = False
# 임시 로그인 여부 — True 면 파일에 저장하지 않아 다음 실행 시 로그인 창이 다시 뜬다.
_user_temporary = False

# Manager 에서 받아온 로그인 구성 {jira: {server,id,pw}, projects: [...]}
_config_cache: dict | None = None
_config_fetched_at = 0.0
_CONFIG_TTL_SEC = 300.0


# ---------------------------------------------------------------- 선택 사용자

def _sanitize_user(user: dict) -> dict:
    return {k: str(user.get(k) or "").strip() for k in _USER_FIELDS}


def get_login_user() -> dict | None:
    """현재 선택된 사용자 (없으면 None). 파일은 최초 1회만 읽고 메모리 캐시."""
    global _user_cache, _user_loaded
    with _lock:
        if not _user_loaded:
            _user_loaded = True
            try:
                if _USER_FILE.exists():
                    data = json.loads(_USER_FILE.read_text(encoding="utf-8"))
                    if isinstance(data, dict) and data.get("name"):
                        _user_cache = _sanitize_user(data)
            except Exception as e:
                logger.warning("login_user.json 로드 실패: %s", e)
        return dict(_user_cache) if _user_cache else None


def is_temporary_login() -> bool:
    """현재 로그인이 임시인지 (사이드바 '임시' 표기용)."""
    with _lock:
        return _user_temporary and _user_cache is not None


def set_login_user(user: dict | None, *, persist: bool = True) -> dict | None:
    """사용자 선택/해제. 다음 관제 status 전송(2초)부터 반영된다.

    persist=False = **임시 로그인** — 메모리에만 두고 파일은 지운다.
    서버를 다시 실행하면 저장된 사용자가 없어 로그인 창이 다시 뜬다.
    (기존에 유지 로그인된 다른 사용자가 있었어도 임시 사용자가 빌려 쓰는
    상황이므로 그 파일을 남겨 두지 않는다 — 다음 실행 시 재선택이 맞다)
    """
    global _user_cache, _user_loaded, _user_temporary
    with _lock:
        _user_loaded = True
        if user and user.get("name"):
            _user_cache = _sanitize_user(user)
            _user_temporary = not persist
            if persist:
                try:
                    _USER_FILE.write_text(
                        json.dumps(_user_cache, ensure_ascii=False, indent=2), encoding="utf-8")
                except Exception as e:
                    logger.warning("login_user.json 저장 실패: %s", e)
            else:
                try:
                    _USER_FILE.unlink(missing_ok=True)
                except Exception as e:
                    logger.warning("login_user.json 삭제 실패: %s", e)
        else:
            _user_cache = None
            _user_temporary = False
            try:
                _USER_FILE.unlink(missing_ok=True)
            except Exception as e:
                logger.warning("login_user.json 삭제 실패: %s", e)
        return dict(_user_cache) if _user_cache else None


# ---------------------------------------------------------------- Manager 구성

def _manager_url() -> str:
    # 라우터 → 서비스 의존 방향을 어기지 않도록 지연 import (settings 라우터가 파일 소유)
    from ..routers.settings import _load as _load_settings
    return (_load_settings().get("monitor_server_url") or "").rstrip("/")


def fetch_login_config(force: bool = False) -> dict:
    """Manager 의 /api/login-config(Jira 계정)를 받아온다 (TTL 캐시, 실패 시 마지막 값 유지).

    반환: {"jira": {"server","id","pw"}} — 항상 이 형태를 보장.
    프로젝트/모델 목록은 Manager 가 아니라 로컬 디바이스 카탈로그에서 온다
    (login_projects 참고).
    """
    global _config_cache, _config_fetched_at
    now = time.monotonic()
    if not force and _config_cache is not None and now - _config_fetched_at < _CONFIG_TTL_SEC:
        return _config_cache

    url = _manager_url()
    if url:
        try:
            import requests
            resp = requests.get(f"{url}/api/login-config", timeout=10)
            resp.raise_for_status()
            data = resp.json()
            jira = data.get("jira") or {}
            _config_cache = {
                "jira": {
                    "server": str(jira.get("server") or ""),
                    "id": str(jira.get("id") or ""),
                    "pw": str(jira.get("pw") or ""),
                },
            }
            _config_fetched_at = now
            return _config_cache
        except Exception as e:
            logger.warning("Manager 로그인 구성 조회 실패(%s): %s", url, e)

    # 실패 — 마지막으로 성공한 값이 있으면 그대로, 없으면 빈 구성
    return _config_cache or {"jira": {"server": "", "id": "", "pw": ""}}


def prefetch_login_config() -> None:
    """서버 시작 시 1회 미리 받아두기 (백그라운드 스레드에서 호출)."""
    cfg = fetch_login_config(force=True)
    ready = bool(cfg["jira"]["id"] and cfg["jira"]["pw"])
    logger.info("로그인 구성 수신: jira_ready=%s", ready)


def login_projects() -> list[dict]:
    """로그인 모달의 프로젝트/모델 선택지 — 주 디바이스 카탈로그가 원본.

    반환: [{"name": "HKMC", "models": ["ccRC", ...]}] (enabled 항목만).
    """
    # 카탈로그 로더는 device 라우터가 소유 — 순환 import 방지를 위해 지연 import
    from ..routers.device import _load_device_catalog
    out: list[dict] = []
    try:
        cat = _load_device_catalog()
        for p in cat.get("projects", []) or []:
            if not p.get("enabled", True) or not p.get("name"):
                continue
            models = [
                str(m.get("value"))
                for m in (p.get("models", []) or [])
                if m.get("enabled", True) and m.get("value")
            ]
            out.append({"name": str(p["name"]), "models": models})
    except Exception as e:
        logger.warning("디바이스 카탈로그 로드 실패 — 프로젝트 목록 비어 있음: %s", e)
    return out


# ---------------------------------------------------------------- Jira 유저 검색
# person_org_search.py 이식 — displayName 형식 가정:
#   "최세진/(협력사) 선임연구원/VS TC설계/검증자동화팀(sejin3569.choi)"
#   → 이름 / 직급 / 조직... 순. 세 번째 이후를 합쳐 팀으로 취급하고
#     '(' 앞까지만 사용 (뒤의 계정 ID 표기는 제거).

def parse_display_name(display_name: str) -> dict:
    result = {"name": "", "title": "", "team": ""}
    if not display_name:
        return result
    parts = [p.strip() for p in display_name.split("/")]
    result["name"] = parts[0]
    if len(parts) >= 2:
        result["title"] = parts[1]
    if len(parts) >= 3:
        team = "/".join(parts[2:])
        result["team"] = team.split("(")[0].strip()
    return result


def search_users(keyword: str, max_results: int = 500, batch: int = 1000) -> list[dict]:
    """키워드(이름/아이디/이메일/조직명)로 Jira 사용자 검색.

    displayName 에 조직명이 포함되므로 팀명으로도 검색 가능.
    이름 없는 계정은 제외. 반환: {"name","title","team","display_name","user_id"} 리스트.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    cfg = fetch_login_config()
    jira = cfg["jira"]
    if not (jira["server"] and jira["id"] and jira["pw"]):
        raise RuntimeError(
            "Jira 계정이 설정되지 않았습니다 — 관제 서버(Manager) 설정 페이지에서 "
            "Jira ID/비밀번호를 등록하세요.")

    import requests
    session = requests.Session()
    session.auth = (jira["id"], jira["pw"])
    base = jira["server"].rstrip("/")

    results: list[dict] = []
    seen: set[str] = set()
    start = 0
    while len(results) < max_results:
        resp = session.get(
            f"{base}/rest/api/2/user/search",
            params={"username": keyword, "startAt": start, "maxResults": batch},
            timeout=15,
        )
        if resp.status_code in (401, 403):
            raise RuntimeError("Jira 인증 실패 — Manager 에 저장된 Jira 계정을 확인하세요.")
        resp.raise_for_status()
        page = resp.json()
        if not page:
            break
        for user in page:
            display_name = user.get("displayName") or ""
            info = parse_display_name(display_name)
            if not info["name"]:
                continue
            user_id = user.get("name") or user.get("key") or ""
            dedup_key = user_id or display_name
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            info["display_name"] = display_name
            info["user_id"] = user_id
            results.append(info)
        if len(page) < batch:
            break
        start += len(page)

    return results[:max_results]
