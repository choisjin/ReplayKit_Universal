"""Linux 그래픽 세션(Wayland/Xorg) 판별 + GDM Xorg 고정 설정.

Wayland 세션에서는 네이티브 Wayland 앱(터미널, Firefox 등)이 X11 로 열거/캡처/입력되지 않아
합성녹화 윈도우 소스·WinControl 이 동작하지 않는다 (XWayland 앱만 보임 — 예: Wine Notepad++).
→ GDM 의 WaylandEnable=false 로 Xorg 세션을 고정하고 재부팅하도록 유도한다.

설정 파일은 root 소유이므로 pkexec(polkit 그래픽 암호창)로 교체한다. 편집은 파이썬에서
하고, 권한 상승 구간은 백업 + install 한 줄로 최소화.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Ubuntu/Debian = gdm3, Fedora/RHEL = gdm
_GDM_CONF_CANDIDATES = (Path("/etc/gdm3/custom.conf"), Path("/etc/gdm/custom.conf"))

_WAYLAND_LINE_RE = re.compile(r"^\s*#?\s*WaylandEnable\s*=.*$")
_SECTION_RE = re.compile(r"^\s*\[([^\]]+)\]\s*$")


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _run(cmd: list[str], timeout: float = 5.0) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            return None
        return r.stdout
    except Exception:
        return None


def _loginctl_session_type() -> Optional[str]:
    """현재 사용자의 활성 그래픽 세션 Type (wayland/x11). 판별 불가면 None.

    백엔드가 systemd/ssh 등 세션 환경변수 없이 떠도 정확하도록 logind 에 직접 묻는다.
    """
    if not shutil.which("loginctl"):
        return None
    sid = os.environ.get("XDG_SESSION_ID", "")
    ids: list[str] = [sid] if sid else []
    out = _run(["loginctl", "list-sessions", "--no-legend"])
    if out:
        uid = str(os.getuid())
        for line in out.splitlines():
            parts = line.split()
            # 형식: SESSION UID USER [SEAT] [TTY] ...
            if len(parts) >= 2 and parts[1] == uid and parts[0] not in ids:
                ids.append(parts[0])
    for s in ids:
        info = _run(["loginctl", "show-session", s, "-p", "Type", "-p", "Active", "-p", "Class"])
        if not info:
            continue
        kv = dict(line.split("=", 1) for line in info.splitlines() if "=" in line)
        stype = (kv.get("Type") or "").lower()
        if kv.get("Class") == "user" and kv.get("Active") == "yes" and stype in ("wayland", "x11"):
            return stype
    return None


def current_session_type() -> str:
    """'wayland' | 'x11' | 'unknown'."""
    st = _loginctl_session_type()
    if st:
        return st
    env_type = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if env_type in ("wayland", "x11"):
        return env_type
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    if os.environ.get("DISPLAY"):
        return "x11"
    return "unknown"


def gdm_conf_path() -> Optional[Path]:
    for p in _GDM_CONF_CANDIDATES:
        if p.parent.is_dir():
            return p
    return None


def _xorg_forced(text: str) -> bool:
    """[daemon] 섹션에 주석 아닌 WaylandEnable=false 가 있는지."""
    section = ""
    for line in text.splitlines():
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().lower()
            continue
        s = line.strip()
        if section == "daemon" and not s.startswith("#") and "=" in s:
            k, v = (x.strip() for x in s.split("=", 1))
            if k == "WaylandEnable" and v.lower() in ("false", "0", "no"):
                return True
    return False


def _with_xorg_forced(text: str) -> str:
    """WaylandEnable=false 를 [daemon] 섹션에 반영한 새 내용."""
    lines = text.splitlines()
    out: list[str] = []
    section = ""
    done = False
    daemon_idx: Optional[int] = None
    for line in lines:
        m = _SECTION_RE.match(line)
        if m:
            section = m.group(1).strip().lower()
            out.append(line)
            if section == "daemon" and daemon_idx is None:
                daemon_idx = len(out)
            continue
        if section == "daemon" and _WAYLAND_LINE_RE.match(line):
            if not done:
                out.append("WaylandEnable=false")
                done = True
            continue  # 중복/주석 라인은 정리
        out.append(line)
    if not done:
        if daemon_idx is not None:
            out.insert(daemon_idx, "WaylandEnable=false")
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["[daemon]", "WaylandEnable=false"]
    return "\n".join(out) + "\n"


def status() -> dict:
    """프론트 표시용 상태.

    needs_action: Wayland 세션이라 조치 필요
    xorg_configured: 설정은 이미 반영됨 → 재부팅만 남음
    can_auto: GDM 설정 파일 위치 확인 + pkexec 존재 → 자동 설정 가능
    """
    if not _is_linux():
        return {"linux": False, "needs_action": False}
    session = current_session_type()
    conf = gdm_conf_path()
    configured = False
    if conf is not None and conf.exists():
        try:
            configured = _xorg_forced(conf.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:
            logger.debug("display_session: read %s failed: %s", conf, e)
    return {
        "linux": True,
        "session_type": session,
        "needs_action": session == "wayland",
        "display_manager": "gdm" if conf is not None else "",
        "conf_path": str(conf) if conf else "",
        "xorg_configured": configured,
        "can_auto": conf is not None and bool(shutil.which("pkexec")),
    }


def enable_xorg(timeout: float = 180.0) -> dict:
    """pkexec 로 GDM custom.conf 에 WaylandEnable=false 반영. 블로킹(암호 입력 대기) — 스레드에서 호출.

    반환: {"ok": bool, "cancelled": bool, "error": str}
    """
    if not _is_linux():
        return {"ok": False, "cancelled": False, "error": "Linux only"}
    conf = gdm_conf_path()
    if conf is None:
        return {"ok": False, "cancelled": False, "error": "GDM 설정 폴더(/etc/gdm3)를 찾을 수 없습니다"}
    pkexec = shutil.which("pkexec")
    if not pkexec:
        return {"ok": False, "cancelled": False, "error": "pkexec 가 없습니다"}
    try:
        old = conf.read_text(encoding="utf-8", errors="replace") if conf.exists() else ""
    except Exception as e:
        return {"ok": False, "cancelled": False, "error": f"설정 읽기 실패: {e}"}
    if _xorg_forced(old):
        return {"ok": True, "cancelled": False, "error": ""}
    new = _with_xorg_forced(old)
    fd, tmp = tempfile.mkstemp(prefix="replaykit_gdm_", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        # 최초 1회 원본 백업 후 교체 — 권한 상승 구간은 이 한 줄뿐
        script = 'set -e; [ -f "$1" ] && [ ! -f "$1.replaykit.bak" ] && cp -p "$1" "$1.replaykit.bak" || true; install -m 644 "$2" "$1"'
        r = subprocess.run(
            [pkexec, "/bin/sh", "-c", script, "sh", str(conf), tmp],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True, "error": "암호 입력 시간 초과"}
    except Exception as e:
        return {"ok": False, "cancelled": False, "error": str(e)}
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    # pkexec: 126 = 인증 취소/거부, 127 = 인증 에이전트 없음 등
    if r.returncode == 126:
        return {"ok": False, "cancelled": True, "error": "관리자 인증이 취소되었습니다"}
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[-300:]
        logger.warning("display_session: pkexec failed rc=%s: %s", r.returncode, err)
        return {"ok": False, "cancelled": False, "error": err or f"rc={r.returncode}"}
    logger.info("display_session: WaylandEnable=false applied to %s", conf)
    return {"ok": True, "cancelled": False, "error": ""}


def reboot() -> dict:
    """시스템 재부팅. 활성 로컬 세션 사용자는 logind polkit 기본 정책상 암호 없이 가능, 실패 시 pkexec."""
    if not _is_linux():
        return {"ok": False, "error": "Linux only"}
    try:
        r = subprocess.run(["systemctl", "reboot"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0:
            return {"ok": True, "error": ""}
        logger.warning("display_session: systemctl reboot rc=%s: %s", r.returncode, r.stderr.strip())
        pkexec = shutil.which("pkexec")
        if pkexec:
            r = subprocess.run([pkexec, "systemctl", "reboot"], capture_output=True, text=True, timeout=180)
            if r.returncode == 0:
                return {"ok": True, "error": ""}
        return {"ok": False, "error": (r.stderr or "").strip()[-300:] or f"rc={r.returncode}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
