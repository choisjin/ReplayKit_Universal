# -*- coding: utf-8 -*-
"""로그인(사용자 식별) 서비스 — Jira 유저 검색 + 선택 사용자 영속화.

비밀번호 인증이 아니라 **누가 이 PC 를 쓰는지 식별**하는 용도다.
- 유저 검색은 **Manager(관제 서버)가 대행**한다. 이 PC 는 Jira 자격증명을 갖지 않는다.
  GET {monitor_server_url}/api/user-search?keyword= → {"users": [...]}
  (2026-07-28 보안 변경. 예전에는 Manager 가 Jira id/pw 를 평문으로 내려주고 각 PC 가
   Jira 를 직접 호출했다 — 인증 없는 엔드포인트라 사내망에서 계정이 통째로 유출됐다.
   자격증명이 여기까지 오지 않으므로 이 파일에는 Jira 접속 코드가 없다.)
- 검색 가능 여부(ready)와 검색 URL 은 GET {monitor_server_url}/api/login-config 로
  받아 TTL 캐시한다.
- 프로젝트/모델 목록은 Manager 가 아니라 로컬 디바이스 카탈로그가 원본이다.
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

# Manager 에서 받아온 로그인 구성 {"search_url": str, "ready": bool}
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


def _default_config(url: str = "") -> dict:
    """Manager 응답이 없을 때 쓰는 기본 구성.

    search_url 은 Manager URL 로부터 유추한다 — 구버전 Manager(응답에 search_url 이
    없는)라면 이 URL 이 404 를 내고, search_users 가 '매니저 업데이트 필요' 로 안내한다.
    """
    return {"search_url": f"{url}/api/user-search" if url else "", "ready": bool(url)}


def fetch_login_config(force: bool = False) -> dict:
    """Manager 의 /api/login-config 를 받아온다 (TTL 캐시, 실패 시 마지막 값 유지).

    반환: {"search_url": str, "ready": bool} — 항상 이 형태를 보장.
    **Jira 자격증명은 받지 않는다** — 검색은 Manager 가 대행한다(모듈 docstring 참고).
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
            search_url = str(data.get("search_url") or "").strip()
            _config_cache = {
                "search_url": search_url or f"{url}/api/user-search",
                # 구버전 Manager 는 ready 를 안 보낸다 — 그땐 일단 가능으로 두고
                # 실제 검색 시점의 404/오류로 안내한다(로그인 창을 미리 막지 않는다).
                "ready": bool(data.get("ready", True)),
            }
            _config_fetched_at = now
            return _config_cache
        except Exception as e:
            logger.warning("Manager 로그인 구성 조회 실패(%s): %s", url, e)

    # 실패 — 마지막으로 성공한 값이 있으면 그대로, 없으면 Manager URL 기반 기본값
    return _config_cache or _default_config(url)


def prefetch_login_config() -> None:
    """서버 시작 시 1회 미리 받아두기 (백그라운드 스레드에서 호출)."""
    cfg = fetch_login_config(force=True)
    logger.info("로그인 구성 수신: search_ready=%s (%s)",
                cfg["ready"], cfg["search_url"] or "-")


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


# ---------------------------------------------------------------- 유저 검색 (Manager 대행)

def search_users(keyword: str, max_results: int = 500) -> list[dict]:
    """키워드(이름/아이디/이메일/조직명)로 사용자 검색 — Manager 가 Jira 를 대신 조회한다.

    displayName 에 조직명이 포함되므로 팀명으로도 검색 가능(파싱은 Manager 담당).
    반환: {"name","title","team","display_name","user_id"} 리스트.

    실패는 RuntimeError 로 올린다 — 라우터가 그대로 사용자에게 보여준다.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return []

    cfg = fetch_login_config()
    search_url = cfg["search_url"]
    if not search_url:
        raise RuntimeError(
            "관제 서버(Manager) 주소가 설정되지 않았습니다 — #admin 에서 관제 서버 URL 을 "
            "등록하세요.")

    import requests
    try:
        resp = requests.get(
            search_url,
            params={"keyword": keyword, "max_results": max_results},
            timeout=30,   # Jira 왕복을 Manager 가 대신하므로 로컬 호출보다 넉넉히
        )
    except Exception as e:
        raise RuntimeError(f"관제 서버에 연결하지 못했습니다: {e}")

    # 구버전 Manager 판별 — /api/user-search 가 없는 서버는 404 를 주거나,
    # SPA 폴백이 index.html 을 200 으로 돌려준다(상태코드만으로는 구분되지 않는다).
    if resp.status_code == 404 or "json" not in (resp.headers.get("Content-Type") or "").lower():
        raise RuntimeError(
            "관제 서버(Manager)가 유저 검색을 지원하지 않습니다 — Manager 를 최신 버전으로 "
            "업데이트하세요.")
    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str((resp.json() or {}).get("detail") or "")
        except Exception:
            pass
        raise RuntimeError(detail or f"유저 검색 실패 (HTTP {resp.status_code})")

    try:
        users = (resp.json() or {}).get("users") or []
    except Exception as e:
        raise RuntimeError(f"유저 검색 응답을 해석하지 못했습니다: {e}")
    return users[:max_results]
