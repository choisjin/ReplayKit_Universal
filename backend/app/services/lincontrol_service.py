"""Linux (X11) 윈도우 컨트롤 서비스 — WinControlService 의 X11 등가물.

python-xlib + XTest 확장으로 다른 X11 클라이언트의 윈도우를 열거/캡처/조작.
디바이스 타입 "wincontrol" 의 Linux 백엔드 구현 — device_manager 가 sys.platform
에 따라 WinControlService / LinControlService 중 하나를 선택해 노출한다.

API surface 는 WinControlService 와 1:1 호환:
  - is_available / import_error
  - list_processes / find_window / launch_process / launch_uwp(=no-op) / ensure_attached
  - attach / detach / is_attached / status
  - get_window_size / get_outer_size / get_client_offset / resize_client
  - capture_window_by_match / capture_window / capture_hwnd_bgr
  - send_tap / send_double_click / send_long_press / send_swipe
  - send_text / send_key / send_key_combo
  - run_action_with_timeout / deferred_restore / _capture_via_screen

DPI:
  X11 은 Windows 의 Per-Monitor DPI virtualization 같은 게 없어서 좌표는 항상
  물리 픽셀. WinControlService 의 DPI 보정 로직은 모두 no-op 으로 단순화.

Wayland:
  Wayland 네이티브 세션은 보안상 임의 윈도우 캡처/입력을 차단함. XWayland 로
  뜬 X11 클라이언트는 부분 지원 — is_available() 은 XDG_SESSION_TYPE=wayland 인
  경우 경고만 남기고 True 반환 (가능한 만큼 동작).
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import sys
import threading
import time
import subprocess
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# ── X11 / mss / PIL lazy import ──────────────────────────────
_X11_AVAILABLE = False
_IMPORT_ERROR: Optional[str] = None

if sys.platform.startswith("linux"):
    try:
        # python-xlib
        from Xlib import X, display, Xutil, error as Xerror, XK  # type: ignore
        from Xlib.ext import xtest  # type: ignore
        from Xlib.protocol import event as Xevent  # type: ignore
        import psutil  # 이미 requirements 에 존재
        import mss  # 이미 requirements 에 존재
        from PIL import Image
        _X11_AVAILABLE = True
    except Exception as _e:  # pragma: no cover — 누락된 시스템에선 모듈 import 는 성공시키고 _IMPORT_ERROR 만 채움
        _IMPORT_ERROR = f"python-xlib/mss/PIL import failed: {_e}"
else:
    _IMPORT_ERROR = f"LinControl is Linux-only (current platform: {sys.platform})"


# 가상키 매핑 — WinControlService 와 동일한 키 이름을 X11 keysym 으로 매핑.
# XK.* 는 X11 keysymdef 의 표준 상수. XK.string_to_keysym() 도 사용 가능.
_KEY_MAP_X11: dict[str, int] = {}
if _X11_AVAILABLE:
    _KEY_MAP_X11 = {
        "ENTER": XK.XK_Return,
        "RETURN": XK.XK_Return,
        "TAB": XK.XK_Tab,
        "ESC": XK.XK_Escape,
        "ESCAPE": XK.XK_Escape,
        "BACKSPACE": XK.XK_BackSpace,
        "BACK": XK.XK_BackSpace,
        "DELETE": XK.XK_Delete,
        "DEL": XK.XK_Delete,
        "SPACE": XK.XK_space,
        "UP": XK.XK_Up,
        "DOWN": XK.XK_Down,
        "LEFT": XK.XK_Left,
        "RIGHT": XK.XK_Right,
        "HOME": XK.XK_Home,
        "END": XK.XK_End,
        "PAGEUP": XK.XK_Page_Up,
        "PAGEDOWN": XK.XK_Page_Down,
        "F1": XK.XK_F1, "F2": XK.XK_F2, "F3": XK.XK_F3,
        "F4": XK.XK_F4, "F5": XK.XK_F5, "F6": XK.XK_F6,
        "F7": XK.XK_F7, "F8": XK.XK_F8, "F9": XK.XK_F9,
        "F10": XK.XK_F10, "F11": XK.XK_F11, "F12": XK.XK_F12,
    }


# Modifier 이름 → X11 keysym
_MODIFIER_KEYSYM: dict[str, int] = {}
if _X11_AVAILABLE:
    _MODIFIER_KEYSYM = {
        "ctrl": XK.XK_Control_L, "control": XK.XK_Control_L,
        "alt": XK.XK_Alt_L, "menu": XK.XK_Alt_L,
        "shift": XK.XK_Shift_L,
        "win": XK.XK_Super_L, "lwin": XK.XK_Super_L,
        "super": XK.XK_Super_L, "cmd": XK.XK_Super_L, "meta": XK.XK_Super_L,
        "rwin": XK.XK_Super_R,
    }


def _wayland_session() -> bool:
    """Wayland 네이티브 세션 여부. XWayland 위에서 X11 클라이언트는 여전히 가능하지만
    네이티브 Wayland 윈도우는 enumerate/capture/input 불가."""
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or bool(os.environ.get("WAYLAND_DISPLAY"))
    )


class LinControlService:
    """단일 X11 윈도우를 임베드 대상으로 잡고 캡처/입력 처리.

    Display 객체는 thread-safe 하지 않으므로 모든 X 호출을 self._dpy_lock 으로 직렬화.
    """

    def __init__(self) -> None:
        self._hwnd: Optional[int] = None  # X11 window ID (XID)
        self._pid: Optional[int] = None
        self._process_name: str = ""
        self._exe_path: str = ""
        self._window_title: str = ""
        self._window_class: str = ""
        # UWP/CoreWindow 개념은 X11 에 없음 — API 호환을 위해 항상 False/None.
        self._is_uwp: bool = False
        self._content_hwnd: Optional[int] = None
        self._aumid: str = ""

        self._dpy = None  # Xlib display.Display
        self._root = None
        self._screen = None
        self._dpy_lock = threading.RLock()

        # deferred_restore 상태 — 스레드 로컬 필수. 액션마다 watchdog 데몬 스레드가
        # 따로 돌므로, 타임아웃으로 leak 된 스레드가 with 블록 안에 갇혀 있으면
        # 인스턴스 공유 카운터로는 이후 모든 복원(포커스+커서)이 스킵된다.
        self._defer_local = threading.local()

        # attach 시점에 1회 떠두는 초기 캡처 — 첫 capture_window 호출에 의해 1회 소비.
        # 임베드 직후 z-order 를 즉시 복구하지만 첫 프레임은 활성 상태에서 잡은 정상 비트맵.
        self._initial_capture: Optional["Image.Image"] = None

        # X11 이 사용 가능하면 즉시 연결 시도 — 실패 시 _IMPORT_ERROR 갱신.
        if _X11_AVAILABLE:
            self._try_open_display()

    # ── 가용성 ────────────────────────────────────────────────
    # 인스턴스 메서드로도 호출 가능하도록 staticmethod 가 아닌 일반 메서드 + 클래스 메서드 동시 노출.
    # WinControlService 는 staticmethod 라 wc.is_available() / WinControlService.is_available() 둘 다 됨 —
    # 여기서도 같은 동작. 단 인스턴스로 호출하면 display 연결 상태도 함께 검증.
    def is_available(self) -> bool:  # type: ignore[override]
        if not _X11_AVAILABLE:
            return False
        # 헤드리스 서버처럼 DISPLAY 가 없거나 X 서버에 닿지 못하면 False.
        if self._dpy is None:
            self._try_open_display()
        return self._dpy is not None

    def import_error(self) -> Optional[str]:  # type: ignore[override]
        return _IMPORT_ERROR

    def _try_open_display(self) -> bool:
        """Display 연결 시도. 성공 시 self._dpy / _root / _screen 설정.

        DISPLAY 환경변수가 없거나 X 서버에 닿을 수 없으면 실패 — 헤드리스/Wayland-only
        환경에서 호출되어도 안전하게 _IMPORT_ERROR 만 갱신하고 False 반환.
        """
        global _IMPORT_ERROR
        if not _X11_AVAILABLE:
            return False
        try:
            self._dpy = display.Display()
            self._screen = self._dpy.screen()
            self._root = self._screen.root
            return True
        except Exception as e:
            self._dpy = None
            self._root = None
            self._screen = None
            _IMPORT_ERROR = f"X11 display open failed: {e}"
            if _wayland_session():
                _IMPORT_ERROR += " (Wayland session detected — XWayland 필요)"
            return False

    def _ensure_display(self) -> bool:
        """Display 가 비어있으면 다시 시도. 호출자는 이후 self._dpy 사용 전 결과 확인."""
        if self._dpy is not None:
            return True
        return self._try_open_display()

    # ── 윈도우 열거 ──────────────────────────────────────────
    def _get_atom(self, name: str) -> int:
        return self._dpy.intern_atom(name)

    def _get_property(self, win, atom_name: str, type_atom: int = 0):
        """win.get_full_property 래퍼 — 예외 안전.

        type_atom 0 = AnyPropertyType.
        """
        try:
            prop = win.get_full_property(self._get_atom(atom_name), type_atom)
            return prop
        except (Xerror.BadWindow, Xerror.BadAtom, Exception):
            return None

    def _get_wm_pid(self, win) -> Optional[int]:
        prop = self._get_property(win, "_NET_WM_PID")
        if prop and prop.value:
            try:
                return int(prop.value[0])
            except (TypeError, ValueError):
                return None
        return None

    def _get_wm_name(self, win) -> str:
        # _NET_WM_NAME (UTF-8) 우선, 없으면 WM_NAME.
        prop = self._get_property(win, "_NET_WM_NAME")
        if prop and prop.value:
            try:
                val = prop.value
                if isinstance(val, bytes):
                    return val.decode("utf-8", errors="replace")
                return str(val)
            except Exception:
                pass
        try:
            wm_name = win.get_wm_name()
            if isinstance(wm_name, bytes):
                return wm_name.decode("utf-8", errors="replace")
            return str(wm_name or "")
        except Exception:
            return ""

    def _get_wm_class(self, win) -> str:
        # WM_CLASS = (instance, class) 튜플 반환. class 부분이 더 안정적인 식별자.
        try:
            wm_class = win.get_wm_class()
            if wm_class and len(wm_class) >= 2:
                return str(wm_class[1] or wm_class[0] or "")
            if wm_class:
                return str(wm_class[0] or "")
        except Exception:
            pass
        return ""

    def _get_wm_state_atoms(self, win) -> list[int]:
        prop = self._get_property(win, "_NET_WM_STATE")
        if prop and prop.value:
            try:
                return list(prop.value)
            except Exception:
                return []
        return []

    def _is_normal_window(self, win) -> bool:
        """desktop/dock/menu/toolbar/splash 등 시스템 윈도우 제외."""
        prop = self._get_property(win, "_NET_WM_WINDOW_TYPE")
        if not prop or not prop.value:
            return True  # 타입 미지정이면 일반 윈도우로 간주
        try:
            wtypes = list(prop.value)
        except Exception:
            return True
        excluded_names = {
            "_NET_WM_WINDOW_TYPE_DESKTOP",
            "_NET_WM_WINDOW_TYPE_DOCK",
            "_NET_WM_WINDOW_TYPE_TOOLBAR",
            "_NET_WM_WINDOW_TYPE_MENU",
            "_NET_WM_WINDOW_TYPE_SPLASH",
            "_NET_WM_WINDOW_TYPE_DROPDOWN_MENU",
            "_NET_WM_WINDOW_TYPE_POPUP_MENU",
            "_NET_WM_WINDOW_TYPE_TOOLTIP",
            "_NET_WM_WINDOW_TYPE_NOTIFICATION",
        }
        for at in wtypes:
            try:
                name = self._dpy.get_atom_name(at)
                if name in excluded_names:
                    return False
            except Exception:
                continue
        return True

    def _is_mapped(self, win) -> bool:
        try:
            attrs = win.get_attributes()
            return attrs.map_state == X.IsViewable
        except Exception:
            return False

    def _client_list(self) -> list:
        """_NET_CLIENT_LIST 로 EWMH 호환 WM 의 클라이언트 윈도우 ID 목록 조회.

        EWMH 미지원 WM 폴백은 root 의 모든 자식을 walk — 느리지만 호환성 ↑.
        """
        if not self._ensure_display():
            return []
        try:
            prop = self._get_property(self._root, "_NET_CLIENT_LIST")
            if prop and prop.value:
                wids = list(prop.value)
                wins = []
                for wid in wids:
                    try:
                        wins.append(self._dpy.create_resource_object("window", int(wid)))
                    except Exception:
                        continue
                return wins
        except Exception:
            pass
        # 폴백: root.query_tree
        try:
            tree = self._root.query_tree()
            return list(tree.children)
        except Exception:
            return []

    def _enum_windows(self) -> list[dict]:
        """가시 최상위 윈도우 정보 dict 목록. WinControlService 와 동일 스키마."""
        if not _X11_AVAILABLE or not self._ensure_display():
            return []
        results: list[dict] = []
        with self._dpy_lock:
            wins = self._client_list()
            for win in wins:
                try:
                    if not self._is_mapped(win):
                        continue
                    if not self._is_normal_window(win):
                        continue
                    title = self._get_wm_name(win)
                    if not title:
                        continue
                    geom = win.get_geometry()
                    w = int(geom.width)
                    h = int(geom.height)
                    if w <= 0 or h <= 0:
                        continue
                    pid = self._get_wm_pid(win) or 0
                    name = ""
                    exe_path = ""
                    if pid:
                        try:
                            proc = psutil.Process(pid)
                            name = proc.name()
                            try:
                                exe_path = proc.exe()
                            except (psutil.AccessDenied, FileNotFoundError):
                                exe_path = ""
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                    cls_name = self._get_wm_class(win)
                    results.append({
                        "pid": int(pid),
                        "hwnd": int(win.id),
                        "name": name,
                        "exe_path": exe_path,
                        "title": title,
                        "class_name": cls_name,
                        "width": w,
                        "height": h,
                    })
                except (Xerror.BadWindow, Xerror.BadDrawable, Exception) as e:
                    logger.debug("enum_window iter error: %s", e)
                    continue
        return results

    def list_processes(self) -> list[dict]:
        if not _X11_AVAILABLE:
            return []
        return sorted(self._enum_windows(),
                      key=lambda d: ((d["name"] or "").lower(), (d["title"] or "").lower()))

    def find_window(
        self,
        process_name: str = "",
        exe_path: str = "",
        title_pattern: str = "",
        class_name: str = "",
    ) -> Optional[dict]:
        if not _X11_AVAILABLE or not (process_name or exe_path or title_pattern or class_name):
            return None
        exe_path_norm = (exe_path or "").strip().lower()
        proc_name_norm = (process_name or "").strip().lower()
        title_norm = (title_pattern or "").strip().lower()
        cls_norm = (class_name or "").strip()
        for w in self._enum_windows():
            if exe_path_norm and (w.get("exe_path") or "").lower() != exe_path_norm:
                continue
            if proc_name_norm and (w.get("name") or "").lower() != proc_name_norm:
                continue
            if title_norm and title_norm not in (w.get("title") or "").lower():
                continue
            if cls_norm and (w.get("class_name") or "") != cls_norm:
                continue
            return w
        return None

    # ── 프로세스 launch ──────────────────────────────────────
    @staticmethod
    def launch_process(exe_path: str, args: Optional[list[str]] = None) -> int:
        """Linux 실행 파일 spawn. 백엔드 종료와 독립적으로 살아남도록 새 session 분리."""
        if not exe_path:
            raise ValueError("exe_path is empty")
        cmd = [exe_path] + (args or [])
        proc = subprocess.Popen(
            cmd, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,  # SIGINT/SIGHUP 영향 차단 — Windows DETACHED_PROCESS 등가
        )
        logger.info("LinControl launched: %s pid=%d", exe_path, proc.pid)
        return proc.pid

    @staticmethod
    def launch_uwp(aumid: str) -> None:
        """API 호환용 — Linux 에 UWP/AUMID 개념 없음. 빈 입력은 ValueError, 그 외 no-op + 경고."""
        if not aumid:
            raise ValueError("aumid is empty")
        logger.warning("LinControl.launch_uwp(%r): not applicable on Linux — ignored", aumid)

    @staticmethod
    def _wait_for_input_idle(hwnd: int, timeout_ms: int = 3000) -> None:
        """X11 에는 WaitForInputIdle 등가 API 없음 — 짧은 sleep 으로 대체."""
        # X 서버에는 프로세스가 메시지 큐 idle 인지 알려주는 표준 API 가 없음.
        # 새로 launch 한 프로세스가 첫 expose/configure 처리할 시간만 짧게 대기.
        time.sleep(min(0.3, max(0.0, timeout_ms / 1000.0)))

    def ensure_attached(
        self,
        process_name: str = "",
        exe_path: str = "",
        title_pattern: str = "",
        class_name: str = "",
        aumid: str = "",
        launch_if_missing: bool = True,
        wait_seconds: float = 8.0,
        target_width: int = 0,
        target_height: int = 0,
    ) -> dict:
        if not _X11_AVAILABLE:
            raise RuntimeError(f"LinControl unavailable: {_IMPORT_ERROR}")

        def _maybe_resize() -> None:
            if target_width > 0 and target_height > 0:
                cur_w, cur_h = self.get_window_size()
                if cur_w != int(target_width) or cur_h != int(target_height):
                    self.resize_client(int(target_width), int(target_height))

        # 1) 이미 attach 가 조건과 일치하는지
        if self.is_attached():
            cur_name_match = (not process_name) or (
                (self._process_name or "").lower() == (process_name or "").lower()
            )
            cur_title_match = (not title_pattern) or (
                (title_pattern or "").lower() in (self._window_title or "").lower()
            )
            if cur_name_match and cur_title_match:
                _maybe_resize()
                return self.status()
            self.detach()

        # 2) 매칭 윈도우 탐색
        match = self.find_window(process_name, exe_path, title_pattern, class_name)
        if match:
            self.attach(match["hwnd"])
            _maybe_resize()
            return self.status()

        # 3) launch — aumid 는 무시(Linux), exe_path 로 launch.
        if not launch_if_missing or not exe_path:
            raise RuntimeError(
                f"LinControl: matching window not found "
                f"(name={process_name!r}, exe={exe_path!r}, title~={title_pattern!r})"
            )
        try:
            self.launch_process(exe_path)
        except Exception as e:
            raise RuntimeError(f"LinControl: failed to launch ({exe_path!r}): {e}")

        deadline = time.monotonic() + max(0.5, wait_seconds)
        while time.monotonic() < deadline:
            time.sleep(0.3)
            match = self.find_window(process_name, exe_path, title_pattern, class_name)
            if match:
                self._wait_for_input_idle(match["hwnd"], timeout_ms=3000)
                time.sleep(0.3)
                self.attach(match["hwnd"])
                _maybe_resize()
                return self.status()
        raise RuntimeError(
            f"LinControl: launched ({exe_path}) but window did not appear within {wait_seconds:.1f}s"
        )

    # ── attach / detach ─────────────────────────────────────
    def attach(self, hwnd: int) -> dict:
        if not _X11_AVAILABLE or not self._ensure_display():
            raise RuntimeError(f"LinControl unavailable: {_IMPORT_ERROR}")
        try:
            win = self._dpy.create_resource_object("window", int(hwnd))
            # 유효성 확인 — get_attributes 호출이 BadWindow 면 무효.
            with self._dpy_lock:
                attrs = win.get_attributes()  # noqa: F841 — raises BadWindow if invalid
                self._hwnd = int(hwnd)
                self._pid = self._get_wm_pid(win)
                self._window_title = self._get_wm_name(win)
                self._window_class = self._get_wm_class(win)
        except Exception as e:
            raise ValueError(f"Invalid window handle: {hwnd} ({e})")

        try:
            if self._pid:
                proc = psutil.Process(self._pid)
                self._process_name = proc.name()
                try:
                    self._exe_path = proc.exe()
                except (psutil.AccessDenied, FileNotFoundError):
                    self._exe_path = ""
            else:
                self._process_name = ""
                self._exe_path = ""
        except Exception:
            self._process_name = ""
            self._exe_path = ""
        self._is_uwp = False
        self._content_hwnd = None
        self._aumid = ""
        logger.info("LinControl attached: xid=%d pid=%s name=%s exe=%s title=%r class=%s",
                    self._hwnd, self._pid, self._process_name, self._exe_path,
                    self._window_title, self._window_class)
        # 임베드 시점에 1회 activate → capture → restore z-order.
        # 사용자가 작업 중이던 윈도우 (prev_fg) 가 뒤로 가버리지 않도록 캡처 직후 즉시 복구.
        # 비활성 윈도우는 composited 환경에서 GPU backbuffer 가 stale 일 수 있어 첫 캡처가
        # 검은 화면일 수 있는데, 활성화하면 강제 re-paint → 정상 비트맵 확보 가능.
        # 결과는 self._initial_capture 에 캐시 — 첫 capture_window 호출이 1회 소비.
        self._initial_capture = None
        prev_fg: Optional[int] = None
        try:
            prev_fg = self._get_active_window()
            if prev_fg == int(hwnd):
                prev_fg = None  # 이미 같은 윈도우가 활성 — 복구 불필요
        except Exception:
            prev_fg = None
        try:
            self._activate(int(hwnd))
            # WM 가 _NET_ACTIVE_WINDOW 처리 + 앱이 expose 받아 paint 완료까지 대기.
            time.sleep(0.30)
            img = self._capture_via_screen(int(hwnd))
            if img is not None:
                self._initial_capture = img
                logger.info("LinControl initial capture cached: size=%s", img.size)
            else:
                logger.warning("LinControl initial capture returned None")
        except Exception as e:
            logger.debug("LinControl attach-time capture failed: %s", e)
        finally:
            # 항상 z-order 복구 — 캡처 성공/실패 무관, 사용자 워크플로우 보존이 우선.
            if prev_fg:
                try:
                    self._activate(int(prev_fg))
                except Exception as e:
                    logger.debug("LinControl attach-time restore failed: %s", e)
        return self.status()

    def detach(self) -> None:
        logger.info("LinControl detached: xid=%s", self._hwnd)
        self._hwnd = None
        self._pid = None
        self._process_name = ""
        self._exe_path = ""
        self._window_title = ""
        self._window_class = ""
        self._is_uwp = False
        self._content_hwnd = None
        self._aumid = ""
        # attach 시 캐시한 초기 캡처도 무효화 — 다음 attach 가 다시 채울 것.
        self._initial_capture = None

    def is_attached(self) -> bool:
        if not _X11_AVAILABLE or self._hwnd is None or self._dpy is None:
            return False
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", self._hwnd)
                win.get_attributes()  # raises BadWindow if invalid
                return True
        except Exception:
            return False

    def status(self) -> dict:
        if not self.is_attached():
            return {"attached": False, "available": _X11_AVAILABLE,
                    "import_error": _IMPORT_ERROR}
        w, h = self.get_window_size()
        ow, oh = self.get_outer_size()
        ox, oy = self.get_client_offset()
        return {
            "attached": True,
            "available": True,
            "hwnd": self._hwnd,
            "pid": self._pid,
            "name": self._process_name,
            "exe_path": self._exe_path,
            "class_name": self._window_class,
            "title": self._window_title,
            "width": w,
            "height": h,
            "outer_width": ow,
            "outer_height": oh,
            "client_offset_x": ox,
            "client_offset_y": oy,
            "is_uwp": False,
            "content_hwnd": None,
            "aumid": "",
        }

    # ── 좌표/지오메트리 ──────────────────────────────────────
    def _get_window_root_pos(self, hwnd: int) -> Optional[tuple[int, int]]:
        """윈도우의 (0,0) 이 root 좌표계에서 어디인지 — 즉 client area 의 screen 좌상단."""
        if not self._ensure_display():
            return None
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                # translate_coords: win 의 (0,0) 을 root 좌표로 변환.
                tc = win.translate_coords(self._root, 0, 0)
                # translate_coords 결과는 src 의 (x,y) 가 dst 좌표계에서 어디인지가 아니라,
                # dst 의 (0,0) 이 src 좌표계 어디인지. 따라서 부호 반전.
                return (-int(tc.x), -int(tc.y))
        except Exception:
            return None

    def _get_frame_extents(self, hwnd: int) -> tuple[int, int, int, int]:
        """_NET_FRAME_EXTENTS (left, right, top, bottom) — WM 가 그린 frame 두께.

        대부분의 modern WM (Mutter/KWin/Compiz/Openbox/etc.) 가 지원. 없으면 (0,0,0,0).
        """
        if not self._ensure_display():
            return (0, 0, 0, 0)
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                prop = self._get_property(win, "_NET_FRAME_EXTENTS")
                if prop and prop.value and len(prop.value) >= 4:
                    return (int(prop.value[0]), int(prop.value[1]),
                            int(prop.value[2]), int(prop.value[3]))
        except Exception:
            pass
        return (0, 0, 0, 0)

    def _get_gtk_frame_extents(self, hwnd: int) -> tuple[int, int, int, int]:
        """_GTK_FRAME_EXTENTS (left, right, top, bottom) — GTK 의 CSD 그림자 두께.

        GTK3/4 앱은 자체적으로 윈도우 주변에 ~32px 그림자를 그리며, X 의 윈도우 크기에
        이 그림자가 포함되어 있음. 사용자가 보는 'visible' 영역은 윈도우 크기에서 이
        그림자 두께를 뺀 만큼. WinControl 의 DWM EXTENDED_FRAME_BOUNDS 와 동일 컨셉.

        GTK 앱이 아니거나 그림자가 없으면 (0,0,0,0). _NET_FRAME_EXTENTS 와는 의미가 달라
        둘 다 따로 조회한다 (전자는 WM frame 두께 = X 가 추가, 후자는 GTK 그림자 = X 윈도우
        크기에 이미 포함되어 있어 차감 필요).
        """
        if not self._ensure_display():
            return (0, 0, 0, 0)
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                prop = self._get_property(win, "_GTK_FRAME_EXTENTS")
                if prop and prop.value and len(prop.value) >= 4:
                    return (int(prop.value[0]), int(prop.value[1]),
                            int(prop.value[2]), int(prop.value[3]))
        except Exception:
            pass
        return (0, 0, 0, 0)

    def _get_raw_window_size(self) -> tuple[int, int]:
        """get_geometry 가 반환하는 X 윈도우 크기 — GTK CSD 그림자 포함 raw 값."""
        if not self.is_attached():
            return (0, 0)
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", self._hwnd)
                geom = win.get_geometry()
                return (int(geom.width), int(geom.height))
        except Exception:
            return (0, 0)

    def get_window_size(self) -> tuple[int, int]:
        """사용자가 보는 visible client 크기 — GTK CSD 그림자 제거 후.

        WinControl 의 client area 와 의미 동일. GTK 앱이 아니면 raw 크기 그대로.
        """
        rw, rh = self._get_raw_window_size()
        if rw <= 0 or rh <= 0:
            return (0, 0)
        gl, gr, gt, gb = self._get_gtk_frame_extents(self._hwnd)
        return (max(1, rw - gl - gr), max(1, rh - gt - gb))

    def get_outer_size(self) -> tuple[int, int]:
        """타이틀바/보더 포함 outer 크기 — visible client + WM frame extents.

        GTK CSD 의 경우 타이틀바가 이미 visible client 안에 포함됨 (앱이 직접 그림) →
        outer == client. 비-CSD 앱은 WM 가 그린 frame 두께만큼 더 큼.
        """
        if not self.is_attached():
            return (0, 0)
        cw, ch = self.get_window_size()
        l, r, t, b = self._get_frame_extents(self._hwnd)
        return (cw + l + r, ch + t + b)

    def get_client_offset(self) -> tuple[int, int]:
        """outer 비트맵 (0,0) → client (0,0) 오프셋 = (left frame, top frame).

        GTK CSD 의 경우 타이틀바가 client 안에 포함되므로 추가 오프셋 없음.
        """
        if not self.is_attached():
            return (0, 0)
        l, _r, t, _b = self._get_frame_extents(self._hwnd)
        return (int(l), int(t))

    def resize_client(self, target_w: int, target_h: int) -> tuple[int, int]:
        """client area 를 (target_w, target_h) 로 리사이즈.

        X11 에서는 configure 가 client area 크기를 직접 받음 — frame extents 더할 필요 없음.
        WM 가 min/max size hints 를 강제하면 요청보다 작거나 큰 값이 적용될 수 있음.
        """
        if not self.is_attached() or target_w <= 0 or target_h <= 0:
            return self.get_window_size()
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", self._hwnd)
                # _NET_WM_STATE_MAXIMIZED 제거 — maximized 면 resize 가 무시됨.
                try:
                    self._set_maximized(win, False)
                except Exception:
                    pass
                win.configure(width=int(target_w), height=int(target_h))
                self._dpy.sync()
            time.sleep(0.05)
        except Exception as e:
            logger.debug("LinControl resize_client failed: %s", e)
        return self.get_window_size()

    def _set_maximized(self, win, on: bool) -> None:
        """_NET_WM_STATE_MAXIMIZED_{HORZ,VERT} 토글 — EWMH client message."""
        if not self._ensure_display():
            return
        try:
            a_state = self._get_atom("_NET_WM_STATE")
            a_horz = self._get_atom("_NET_WM_STATE_MAXIMIZED_HORZ")
            a_vert = self._get_atom("_NET_WM_STATE_MAXIMIZED_VERT")
            # action: 1=ADD, 0=REMOVE
            action = 1 if on else 0
            data = [action, a_horz, a_vert, 1, 0]  # source: 1=app
            ev = Xevent.ClientMessage(
                window=win, client_type=a_state,
                data=(32, data),
            )
            mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
            self._root.send_event(ev, event_mask=mask)
            self._dpy.flush()
        except Exception as e:
            logger.debug("_set_maximized failed: %s", e)

    # ── 캡처 ─────────────────────────────────────────────────
    def _get_screen_size(self) -> tuple[int, int]:
        """root window 의 (width, height) — mss 영역 clipping 용."""
        if not self._ensure_display():
            return (0, 0)
        try:
            geom = self._root.get_geometry()
            return (int(geom.width), int(geom.height))
        except Exception:
            return (0, 0)

    def _capture_via_xlib(self, hwnd: int, x: int, y: int, w: int, h: int):
        """Xlib XGetImage 로 직접 윈도우 캡처. mss 폴백.

        장점: same Display connection 사용 → composited 환경에서 mss 가 영역을 못 잡는
              케이스에서도 동작. 윈도우 자체의 backing pixmap 에서 가져오므로 화면 밖
              으로 벗어난 부분도 캡처 가능 (mss 는 화면 안쪽만).
        단점: GL/하드웨어 가속 콘텐츠는 검은 영역으로 나올 수 있음 (GPU backbuffer).
        """
        if not self._ensure_display():
            return None
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                # 0xFFFFFFFF = AllPlanes, X.ZPixmap = pixel-major (BGRA per pixel on most servers)
                ximg = win.get_image(0, 0, int(w), int(h), X.ZPixmap, 0xFFFFFFFF)
                # ximg.data 는 raw bytes — 보통 BGRA 또는 BGRX. PIL 의 "raw" decoder 에 "BGRX" 모드.
                # 검은 화면 방어: data 전체가 0 인지만 빠르게 확인 — 더 정밀한 blank 검출은 호출자 책임.
                return Image.frombytes("RGB", (int(w), int(h)), ximg.data, "raw", "BGRX")
        except Exception as e:
            logger.debug("LinControl Xlib get_image fallback failed: %s", e)
            return None

    def _capture_via_screen(self, hwnd: int):
        """윈도우 영역 캡처 → PIL.Image (RGB).

        시도 순서:
          1) mss 로 root region grab — composited 최종 픽셀, 정확. 좌표가 화면 밖이면
             clip 해서 시도. 모든 영역이 화면 밖이면 None.
          2) mss 실패 시 Xlib XGetImage 폴백 — 윈도우 자체에서 가져오므로 화면 밖이어도
             가능. 단 일부 GL 콘텐츠는 검은 화면.
        WinControlService._capture_via_screen 과 동일 의미.
        """
        if not _X11_AVAILABLE or not hwnd or not self._ensure_display():
            return None
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                if not self._is_mapped(win):
                    return None
                geom = win.get_geometry()
                tc = win.translate_coords(self._root, 0, 0)
                raw_x = -int(tc.x)
                raw_y = -int(tc.y)
                raw_w = int(geom.width)
                raw_h = int(geom.height)
                # GTK CSD 그림자 제거 — X 윈도우 크기에 그림자가 포함되어 있음.
                gl, gr, gt, gb = self._get_gtk_frame_extents(hwnd)
                # visible client (그림자 제외) 의 root 좌표/크기
                x = raw_x + gl
                y = raw_y + gt
                w = max(1, raw_w - gl - gr)
                h = max(1, raw_h - gt - gb)
                # WM frame extents 가 있으면 outer (타이틀바 포함) 영역으로 확장 — 비-CSD WM 용.
                # GTK 앱은 보통 frame_extents=(0,0,0,0) 이라 이 분기는 no-op.
                fl, fr, ft, fb = self._get_frame_extents(hwnd)
                x -= fl
                y -= ft
                w += fl + fr
                h += ft + fb
        except Exception as e:
            logger.debug("LinControl capture geometry failed: %s", e)
            return None
        if w <= 0 or h <= 0:
            return None

        # 1) mss 시도 — root region 캡처. 화면 밖 영역은 clip.
        # mss 는 mss 의 monitor dict 가 화면 경계를 살짝 벗어나면 grab 이 실패할 수 있어서
        # 명시적으로 화면 안쪽으로 잘라낸 다음 호출. clipped 영역이 0 이면 mss 스킵.
        screen_w, screen_h = self._get_screen_size()
        cx = max(0, x)
        cy = max(0, y)
        cw = w - (cx - x)
        ch = h - (cy - y)
        if screen_w > 0 and cx + cw > screen_w:
            cw = screen_w - cx
        if screen_h > 0 and cy + ch > screen_h:
            ch = screen_h - cy
        if cw > 0 and ch > 0:
            try:
                with mss.mss() as sct:
                    monitor = {"left": cx, "top": cy, "width": cw, "height": ch}
                    sct_img = sct.grab(monitor)
                    img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
                # 화면 밖으로 잘렸으면 원본 outer 크기 비트맵에 paste — 클릭 좌표계 일관성 보존.
                if cx == x and cy == y and cw == w and ch == h:
                    return img
                full = Image.new("RGB", (w, h), (0, 0, 0))
                full.paste(img, (cx - x, cy - y))
                return full
            except Exception as e:
                # mss 실패 — 진단/문제 추적 위해 warning 으로 한 단계 격상.
                logger.warning("LinControl mss grab failed (region=%dx%d+%d+%d, screen=%dx%d): %s — falling back to Xlib",
                               cw, ch, cx, cy, screen_w, screen_h, e)

        # 2) Xlib XGetImage 폴백 — same Display 사용. window-local 좌표.
        # GTK CSD 그림자는 윈도우 좌표계에서 (gl, gt) 위치부터 visible 영역. 그림자 영역을
        # 잘라내고 visible 만 잡으면 클릭 좌표계와 일관됨.
        # frame_extents 가 있는 경우 (대개 비-CSD WM) 윈도우의 (0,0) 은 이미 client 시작 —
        # outer 전체는 frame parent 에서 잡아야 하지만 비표준이므로 client(=visible) 만 잡고
        # outer 크기로 paste.
        vis_w = max(1, raw_w - gl - gr)
        vis_h = max(1, raw_h - gt - gb)
        img = self._capture_via_xlib(hwnd, gl, gt, vis_w, vis_h)
        if img is None:
            return None
        if (fl, fr, ft, fb) == (0, 0, 0, 0):
            return img
        outer = Image.new("RGB", (vis_w + fl + fr, vis_h + ft + fb), (0, 0, 0))
        outer.paste(img, (fl, ft))
        return outer

    def _capture_window_image_for(self, hwnd: int, *_args, **_kwargs):
        """WinControlService 와 동일한 dispatch — X11 에선 screen 캡처 하나로 충분."""
        return self._capture_via_screen(hwnd)

    def capture_window(self, fmt: str = "jpeg", render_full_content: bool = False) -> bytes:
        if not self.is_attached():
            raise RuntimeError("No window attached")
        # attach 가 캐시해둔 첫 비트맵이 있으면 1회 우선 사용 — 그 때만 윈도우가 활성 상태에서
        # 잡혔으니 정상 콘텐츠가 보장됨. 이후엔 비활성 상태의 라이브 캡처.
        img: Optional["Image.Image"] = None
        if self._initial_capture is not None:
            img = self._initial_capture
            self._initial_capture = None  # 1회만 소비
            logger.debug("LinControl capture_window: served initial cached image")
        else:
            img = self._capture_via_screen(self._hwnd)
        if img is None:
            raise RuntimeError("Capture failed (mss + Xlib fallback)")
        buf = io.BytesIO()
        if (fmt or "jpeg").lower() == "png":
            img.save(buf, format="PNG")
        else:
            img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()

    def capture_window_by_match(
        self,
        process_name: str = "",
        exe_path: str = "",
        title_pattern: str = "",
        class_name: str = "",
        aumid: str = "",  # noqa: ARG002 — API 호환, Linux 에선 미사용
        target_width: int = 0,
        target_height: int = 0,
        fmt: str = "png",
        launch_if_missing: bool = True,
        wait_seconds: float = 5.0,
    ) -> bytes:
        if not _X11_AVAILABLE:
            raise RuntimeError(f"LinControl unavailable: {_IMPORT_ERROR}")
        match = self.find_window(process_name, exe_path, title_pattern, class_name)
        if match is None and launch_if_missing and exe_path:
            try:
                self.launch_process(exe_path)
            except Exception as e:
                raise RuntimeError(f"capture_window_by_match: launch failed: {e}")
            deadline = time.monotonic() + max(0.5, wait_seconds)
            while time.monotonic() < deadline:
                time.sleep(0.3)
                match = self.find_window(process_name, exe_path, title_pattern, class_name)
                if match:
                    self._wait_for_input_idle(match["hwnd"], timeout_ms=3000)
                    time.sleep(0.3)
                    break
        if match is None:
            raise RuntimeError(
                f"capture_window_by_match: window not found "
                f"(name={process_name!r}, title~={title_pattern!r}, exe={exe_path!r})"
            )
        hwnd = int(match["hwnd"])
        if target_width > 0 and target_height > 0:
            try:
                with self._dpy_lock:
                    win = self._dpy.create_resource_object("window", hwnd)
                    win.configure(width=int(target_width), height=int(target_height))
                    self._dpy.sync()
                time.sleep(0.05)
            except Exception as e:
                logger.debug("capture_window_by_match resize failed: %s", e)
        img = self._capture_via_screen(hwnd)
        if img is None:
            raise RuntimeError("capture_window_by_match: capture failed (mss)")
        buf = io.BytesIO()
        if (fmt or "png").lower() == "jpeg":
            img.convert("RGB").save(buf, format="JPEG", quality=85)
        else:
            img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def is_valid_window(hwnd: int) -> bool:
        """임의 xid 가 현재 유효한 X 윈도우인지 — compositor 의 hwnd 캐시 검증용.

        WinControlService.is_valid_window 와 인터페이스 동일.
        """
        if not _X11_AVAILABLE or not hwnd:
            return False
        helper = LinControlService()
        if helper._dpy is None:
            return False
        try:
            with helper._dpy_lock:
                win = helper._dpy.create_resource_object("window", int(hwnd))
                win.get_attributes()
                return True
        except Exception:
            return False

    @staticmethod
    def capture_hwnd_bgr(hwnd: int):
        """임의 xid 의 BGR ndarray 캡처 — CompositorService 호환.

        WinControlService.capture_hwnd_bgr 와 인터페이스 동일. 실패 시 None.
        """
        if not _X11_AVAILABLE or not hwnd:
            return None
        try:
            import numpy as _np
        except Exception:
            return None
        helper = LinControlService()
        img = helper._capture_via_screen(int(hwnd))
        if img is None:
            return None
        try:
            arr = _np.asarray(img)
            if arr.ndim != 3 or arr.shape[2] < 3:
                return None
            return arr[:, :, ::-1].copy()  # RGB → BGR
        except Exception:
            return None

    # ── watchdog / focus / context ─────────────────────────
    def run_action_with_timeout(self, fn: "Callable[[], _T]", timeout_s: float = 20.0) -> "_T":
        """WinControlService 와 동일한 timeout-격리 실행. X 호출 hang 방어용."""
        result: dict = {"value": None, "exc": None}
        done = threading.Event()

        def runner() -> None:
            try:
                result["value"] = fn()
            except BaseException as e:  # noqa: BLE001
                result["exc"] = e
            finally:
                done.set()

        t = threading.Thread(target=runner, daemon=True, name="LinControlAction")
        t.start()
        if not done.wait(timeout_s):
            logger.error(
                "LinControl action timed out after %.1fs — leaking watchdog thread "
                "(xid=%s pid=%s name=%s).",
                timeout_s, self._hwnd, self._pid, self._process_name,
            )
            raise TimeoutError(
                f"LinControl action exceeded {timeout_s:.0f}s — target may be unresponsive"
            )
        if result["exc"] is not None:
            raise result["exc"]
        return result["value"]  # type: ignore[return-value]

    def _check(self) -> int:
        if not self.is_attached():
            raise RuntimeError("No window attached")
        return self._hwnd  # type: ignore[return-value]

    def _get_active_window(self) -> Optional[int]:
        """_NET_ACTIVE_WINDOW (현재 포커스 윈도우 XID)."""
        if not self._ensure_display():
            return None
        try:
            with self._dpy_lock:
                prop = self._get_property(self._root, "_NET_ACTIVE_WINDOW")
                if prop and prop.value:
                    val = int(prop.value[0])
                    return val if val else None
        except Exception:
            pass
        return None

    def _activate(self, hwnd: int) -> None:
        """타겟 윈도우를 활성화(포커스 + raise). EWMH ClientMessage."""
        if not self._ensure_display():
            return
        try:
            with self._dpy_lock:
                win = self._dpy.create_resource_object("window", int(hwnd))
                a_active = self._get_atom("_NET_ACTIVE_WINDOW")
                # source=2 (pager) — WM 가 더 잘 허용. timestamp=0, requestor=현재 active.
                data = [2, 0, 0, 0, 0]
                ev = Xevent.ClientMessage(
                    window=win, client_type=a_active, data=(32, data),
                )
                mask = X.SubstructureRedirectMask | X.SubstructureNotifyMask
                self._root.send_event(ev, event_mask=mask)
                win.configure(stack_mode=X.Above)  # raise
                self._dpy.flush()
        except Exception as e:
            logger.debug("LinControl _activate failed: %s", e)

    def _focus(self) -> None:
        """대상 윈도우 포커스. 이미 포커스면 skip — child dialog focus 보존."""
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            cur = self._get_active_window()
            if cur and cur == hwnd:
                time.sleep(0.05)
                return
            # 같은 프로세스의 다른 윈도우가 활성이면 그대로 두기.
            if cur and self._pid:
                try:
                    with self._dpy_lock:
                        cw = self._dpy.create_resource_object("window", cur)
                        cur_pid = self._get_wm_pid(cw)
                    if cur_pid and int(cur_pid) == int(self._pid):
                        time.sleep(0.05)
                        return
                except Exception:
                    pass
            self._activate(hwnd)
            time.sleep(0.30)
        except Exception as e:
            logger.debug("LinControl focus failed: %s", e)

    def _get_pointer_pos(self) -> Optional[tuple[int, int]]:
        if not self._ensure_display():
            return None
        try:
            with self._dpy_lock:
                qp = self._root.query_pointer()
                return (int(qp.root_x), int(qp.root_y))
        except Exception:
            return None

    def _save_context(self) -> dict:
        ctx: dict = {"prev_fg": None, "cursor": None}
        try:
            cur = self._get_active_window()
            if cur and cur != self._hwnd:
                ctx["prev_fg"] = int(cur)
        except Exception:
            pass
        try:
            ctx["cursor"] = self._get_pointer_pos()
        except Exception:
            pass
        return ctx

    def _restore_context(self, ctx: dict) -> None:
        if not ctx:
            return
        # 현재 스레드가 deferred_restore 블록 안이면 큐에 적재 (스레드 로컬).
        loc = getattr(self, "_defer_local", None)
        if loc is not None and getattr(loc, "depth", 0) > 0:
            loc.ctxs.append(ctx)
            return
        self._do_restore_context(ctx)

    def _do_restore_context(self, ctx: dict) -> None:
        if not ctx:
            return
        prev_fg = ctx.get("prev_fg")
        if prev_fg:
            try:
                self._activate(int(prev_fg))
            except Exception as e:
                logger.debug("LinControl FG restore failed: %s", e)
        cursor = ctx.get("cursor")
        if cursor and self._ensure_display():
            try:
                with self._dpy_lock:
                    self._root.warp_pointer(int(cursor[0]), int(cursor[1]))
                    self._dpy.flush()
            except Exception as e:
                logger.debug("LinControl cursor restore failed: %s", e)

    @contextlib.contextmanager
    def deferred_restore(self):
        # 재진입 안전 — send_click_sequence(내부 with) 가 라우터 캡처 사이클(외부 with)
        # 안에서 실행될 때 최외곽 종료 시에만 1회 복원 (WinControlService 와 동일).
        # 스레드 로컬 — leak 된 watchdog 스레드/동시 요청이 카운터를 오염시키는 것 방지.
        loc = self._defer_local
        loc.depth = getattr(loc, "depth", 0) + 1
        if loc.depth == 1:
            loc.ctxs = []
        try:
            yield
        finally:
            loc.depth -= 1
            if loc.depth == 0:
                ctxs = loc.ctxs
                loc.ctxs = []
                if ctxs:
                    self._do_restore_context(ctxs[0])

    # ── 좌표 변환 ──────────────────────────────────────────
    def _client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """visible client (0,0) 기준 좌표 → root(screen) 좌표.

        client 좌표계는 캡처 비트맵과 동일한 visible 영역 — GTK CSD 그림자는 제외된
        시점의 (0,0). 따라서 screen 좌표 = raw_root_pos + gtk_shadow_offset + (x, y).
        """
        hwnd = self._hwnd
        if not hwnd:
            return (int(x), int(y))
        root_xy = self._get_window_root_pos(hwnd)
        if root_xy is None:
            return (int(x), int(y))
        gl, _gr, gt, _gb = self._get_gtk_frame_extents(hwnd)
        sx = int(root_xy[0]) + int(gl) + int(x)
        sy = int(root_xy[1]) + int(gt) + int(y)
        return (sx, sy)

    # ── 입력 (XTest) ────────────────────────────────────────
    def _xtest_motion(self, sx: int, sy: int) -> None:
        if not self._ensure_display():
            return
        with self._dpy_lock:
            xtest.fake_input(self._dpy, X.MotionNotify, x=int(sx), y=int(sy))
            self._dpy.sync()

    def _xtest_button(self, button: str, press: bool) -> None:
        # X11 버튼 번호: 1=left, 2=middle, 3=right, 4=wheel-up, 5=wheel-down
        bnum = {"left": 1, "middle": 2, "right": 3}.get(button or "left", 1)
        evt = X.ButtonPress if press else X.ButtonRelease
        if not self._ensure_display():
            return
        with self._dpy_lock:
            xtest.fake_input(self._dpy, evt, bnum)
            self._dpy.sync()

    def _xtest_key(self, keycode: int, press: bool) -> None:
        evt = X.KeyPress if press else X.KeyRelease
        if not self._ensure_display():
            return
        with self._dpy_lock:
            xtest.fake_input(self._dpy, evt, int(keycode))
            self._dpy.sync()

    def _keysym_to_keycode(self, keysym: int) -> tuple[int, bool]:
        """keysym → (keycode, shift_needed). keycode=0 이면 매핑 없음.

        대문자/특수문자는 shift 가 필요하므로 그 정보도 함께 반환.
        """
        if not self._ensure_display():
            return (0, False)
        with self._dpy_lock:
            kc = self._dpy.keysym_to_keycode(int(keysym))
            if kc == 0:
                return (0, False)
            # keysym 이 shift 변종인지 확인 — 해당 keycode 의 (level=0) 기준 keysym 과 비교.
            try:
                base = self._dpy.keycode_to_keysym(kc, 0)
                if int(base) != int(keysym):
                    # 같은 keycode 의 level=1 (shift) keysym 과 일치하면 shift 필요.
                    shifted = self._dpy.keycode_to_keysym(kc, 1)
                    if int(shifted) == int(keysym):
                        return (int(kc), True)
            except Exception:
                pass
            return (int(kc), False)

    def _char_to_keysym(self, ch: str) -> int:
        """문자 1개 → X11 keysym.

        - Latin-1 (0x20-0xff): keysym == codepoint
        - 그 외 Unicode: keysym = 0x01000000 | codepoint (X11 Unicode keysym)
        """
        cp = ord(ch)
        if 0x20 <= cp <= 0xff:
            return cp
        return 0x01000000 | cp

    def _send_char_unicode(self, ch: str) -> None:
        """Unicode 문자 1개 입력. 매핑이 없는 keysym 은 임시 keycode remap 후 입력.

        엔터/탭 등 control char 는 호출자가 별도 처리.
        """
        keysym = self._char_to_keysym(ch)
        kc, need_shift = self._keysym_to_keycode(keysym)
        if kc == 0:
            # 매핑 없음 — 사용되지 않은 keycode 를 일시적으로 이 keysym 으로 매핑 후 사용.
            kc = self._remap_unused_keycode(keysym)
            if kc == 0:
                logger.debug("LinControl: no keycode for char %r (keysym=0x%x)", ch, keysym)
                return
            need_shift = False
        try:
            if need_shift:
                shift_kc = self._dpy.keysym_to_keycode(XK.XK_Shift_L)
                if shift_kc:
                    self._xtest_key(shift_kc, True)
            self._xtest_key(kc, True)
            time.sleep(0.005)
            self._xtest_key(kc, False)
        finally:
            if need_shift:
                shift_kc = self._dpy.keysym_to_keycode(XK.XK_Shift_L)
                if shift_kc:
                    self._xtest_key(shift_kc, False)

    def _remap_unused_keycode(self, keysym: int) -> int:
        """미사용 keycode 슬롯에 keysym 을 임시 매핑하고 keycode 반환.

        반환된 keycode 는 호출 후에도 그대로 남아 다음 호출에서 재사용됨 — 글로벌 keymap 을
        조금 더럽히지만, xdotool 도 같은 방식 사용. 깨끗하게 두려면 input 후 remap 원복
        필요하나, X 서버 재시작/유저 로그아웃 시 자동 초기화되므로 실용적으로 무해.
        """
        if not self._ensure_display():
            return 0
        try:
            with self._dpy_lock:
                min_kc, max_kc = self._dpy.display.info.min_keycode, self._dpy.display.info.max_keycode
                # min_keycode~max_keycode 범위에서 NoSymbol 만 매핑된 keycode 검색.
                for kc in range(max_kc, min_kc - 1, -1):
                    sym = self._dpy.keycode_to_keysym(kc, 0)
                    if int(sym) == X.NoSymbol or int(sym) == 0:
                        # 이 keycode 에 keysym 매핑.
                        self._dpy.change_keyboard_mapping(kc, [[int(keysym), int(keysym)]])
                        self._dpy.sync()
                        time.sleep(0.01)  # 매핑 반영 대기
                        return int(kc)
        except Exception as e:
            logger.debug("LinControl _remap_unused_keycode failed: %s", e)
        return 0

    # ── tap/click/swipe ─────────────────────────────────────
    def send_tap(self, x: int, y: int, button: str = "left") -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._xtest_motion(sx, sy)
            time.sleep(0.10)
            self._xtest_button(button, True)
            time.sleep(0.08)
            self._xtest_button(button, False)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    def send_double_click(self, x: int, y: int) -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._xtest_motion(sx, sy)
            time.sleep(0.10)
            for _ in range(2):
                self._xtest_button("left", True)
                time.sleep(0.06)
                self._xtest_button("left", False)
                time.sleep(0.06)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    def send_click_sequence(self, points: list[dict], interval_ms: int = 150,
                            button: str = "left") -> None:
        """여러 위치를 포커스 유지한 채 순서대로 클릭 (원자 실행 — 재생/라이브 공용).

        드롭다운 열기 → 항목 선택처럼 첫 클릭으로 뜬 일시 팝업이 다음 클릭 전에
        닫히면 안 되는 경우 사용. WinControlService.send_click_sequence 와 인터페이스
        동일 (OS 공용 디스패치).
        """
        self._check()
        pts = [p for p in (points or []) if p is not None]
        if not pts:
            return
        gap = max(0.0, int(interval_ms) / 1000.0)
        with self.deferred_restore():
            for i, pt in enumerate(pts):
                self.send_tap(int(pt["x"]), int(pt["y"]), button)
                if i < len(pts) - 1 and gap > 0:
                    time.sleep(gap)

    def send_long_press(self, x: int, y: int, duration_ms: int = 500, button: str = "left") -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._xtest_motion(sx, sy)
            time.sleep(0.10)
            self._xtest_button(button, True)
            try:
                time.sleep(max(0.0, duration_ms / 1000.0))
            finally:
                self._xtest_button(button, False)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    def send_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            steps = max(2, int(max(50, duration_ms) / 25))
            delay = max(0.0, duration_ms / 1000.0 / steps)
            sx1, sy1 = self._client_to_screen(int(x1), int(y1))
            self._xtest_motion(sx1, sy1)
            time.sleep(0.10)
            self._xtest_button("left", True)
            for i in range(1, steps):
                t = i / steps
                x = int(x1 + (x2 - x1) * t)
                y = int(y1 + (y2 - y1) * t)
                sx, sy = self._client_to_screen(x, y)
                self._xtest_motion(sx, sy)
                if delay > 0:
                    time.sleep(delay)
            sx2, sy2 = self._client_to_screen(int(x2), int(y2))
            self._xtest_motion(sx2, sy2)
            time.sleep(0.08)
            self._xtest_button("left", False)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    # ── 키 입력 ──────────────────────────────────────────────
    def _resolve_keysym(self, key: str) -> int:
        """문자열 키 → X11 keysym. 'A'~'Z'/'0'~'9' 는 ASCII, 그 외는 _KEY_MAP_X11.

        keysym 으로 lookup 안 되는 경우 XK.string_to_keysym 도 fallback 시도.
        """
        if not key:
            raise ValueError("empty key")
        upper = key.upper()
        if upper in _KEY_MAP_X11:
            return int(_KEY_MAP_X11[upper])
        if len(upper) == 1 and (upper.isalpha() or upper.isdigit()):
            # 영문/숫자 — keysym 은 소문자 기준. 대문자는 shift 변종.
            return ord(upper.lower())
        # XK.string_to_keysym 으로 last-resort lookup
        try:
            ks = XK.string_to_keysym(key)
            if ks:
                return int(ks)
        except Exception:
            pass
        raise ValueError(f"Unknown key: {key}")

    def send_text(
        self,
        text: str,
        click_first_x: Optional[int] = None,
        click_first_y: Optional[int] = None,
        press_enter: bool = False,
    ) -> None:
        # press_enter 가 True 면 텍스트 전송 후 Enter 키까지 눌러줌 (검색창 제출 등).
        # 분리된 send_key 호출은 사이의 fg 복원으로 포커스가 풀리므로 같은 컨텍스트에 포함.
        self._check()
        ctx = self._save_context()

        def _tap_return() -> None:
            enter_kc = self._dpy.keysym_to_keycode(XK.XK_Return)
            if enter_kc:
                self._xtest_key(enter_kc, True)
                time.sleep(0.01)
                self._xtest_key(enter_kc, False)

        try:
            self._focus()
            if click_first_x is not None and click_first_y is not None:
                sx, sy = self._client_to_screen(int(click_first_x), int(click_first_y))
                self._xtest_motion(sx, sy)
                time.sleep(0.10)
                self._xtest_button("left", True)
                time.sleep(0.08)
                self._xtest_button("left", False)
                time.sleep(0.20)
            if text:
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                lines = normalized.split("\n")
                for i, line in enumerate(lines):
                    if i > 0:
                        _tap_return()
                        time.sleep(0.05)
                    for ch in line:
                        self._send_char_unicode(ch)
                        time.sleep(0.010)
            if press_enter:
                time.sleep(0.05)
                _tap_return()
            time.sleep(0.10)
        finally:
            self._restore_context(ctx)

    def send_key(self, key: str) -> None:
        self._check()
        keysym = self._resolve_keysym(key)
        kc, need_shift = self._keysym_to_keycode(keysym)
        if kc == 0:
            raise ValueError(f"No keycode mapping for key: {key}")
        ctx = self._save_context()
        try:
            self._focus()
            shift_kc = self._dpy.keysym_to_keycode(XK.XK_Shift_L) if need_shift else 0
            try:
                if shift_kc:
                    self._xtest_key(shift_kc, True)
                self._xtest_key(kc, True)
                time.sleep(0.05)
                self._xtest_key(kc, False)
            finally:
                if shift_kc:
                    self._xtest_key(shift_kc, False)
            time.sleep(0.10)
        finally:
            self._restore_context(ctx)

    def send_key_combo(self, keys: list[str],
                       click_first_x: Optional[int] = None,
                       click_first_y: Optional[int] = None) -> None:
        # click_first_x/y: 단축키 전송 전 해당 client 좌표 클릭으로 대상 컨트롤 포커스
        # 부여 (atomic — WinControlService.send_key_combo 와 인터페이스 동일).
        self.send_key_combos([keys] if keys else [], click_first_x, click_first_y)

    def send_key_combos(self, combos: list[list[str]],
                        click_first_x: Optional[int] = None,
                        click_first_y: Optional[int] = None) -> None:
        """여러 조합을 한 컨텍스트(포커스 유지) 안에서 순서대로 전송.

        'Ctrl+A → BackSpace' 처럼 연속 조합을 요청 2개로 분리하면 사이의 fg 복원/재활성화
        로 대상 컨트롤 포커스가 풀려 두 번째 조합이 허공에 떨어진다.
        WinControlService.send_key_combos 와 인터페이스 동일 (OS 공용 디스패치).
        """
        seq = [c for c in (combos or []) if c]
        if not seq:
            return
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            # 0) 클릭으로 대상 컨트롤 포커스 — 같은 컨텍스트 안에서 해야 fg 안 풀림.
            if click_first_x is not None and click_first_y is not None:
                sx, sy = self._client_to_screen(int(click_first_x), int(click_first_y))
                self._xtest_motion(sx, sy)
                time.sleep(0.10)
                self._xtest_button("left", True)
                time.sleep(0.08)
                self._xtest_button("left", False)
                time.sleep(0.20)
            for i, keys in enumerate(seq):
                if i > 0:
                    # 조합 사이 간격 — 앞 조합(예: Ctrl+A 선택)이 처리될 시간.
                    time.sleep(0.15)
                self._press_combo(keys)
            time.sleep(0.10)
        finally:
            self._restore_context(ctx)

    def _press_combo(self, keys: list[str]) -> None:
        """단일 조합(수정자+키) press. 컨텍스트/포커스 관리는 호출자 책임.

        예외로 중간에 빠져나가도 finally 에서 modifier 강제 해제 (stuck key 방지).
        """
        modifiers: list[int] = []  # keycodes
        regulars: list[tuple[int, bool]] = []  # (keycode, need_shift)
        for k in keys or []:
            key = str(k).strip()
            if not key:
                continue
            mks = _MODIFIER_KEYSYM.get(key.lower())
            if mks is not None:
                kc = self._dpy.keysym_to_keycode(int(mks))
                if kc:
                    modifiers.append(int(kc))
            else:
                keysym = self._resolve_keysym(key)
                kc, need_shift = self._keysym_to_keycode(keysym)
                if kc:
                    regulars.append((int(kc), need_shift))
        if not regulars and not modifiers:
            return
        try:
            for kc in modifiers:
                self._xtest_key(kc, True)
                time.sleep(0.02)
            for kc, need_shift in regulars:
                shift_kc = self._dpy.keysym_to_keycode(XK.XK_Shift_L) if need_shift else 0
                try:
                    if shift_kc and shift_kc not in modifiers:
                        self._xtest_key(shift_kc, True)
                    self._xtest_key(kc, True)
                    time.sleep(0.03)
                    self._xtest_key(kc, False)
                    time.sleep(0.02)
                finally:
                    if shift_kc and shift_kc not in modifiers:
                        self._xtest_key(shift_kc, False)
        finally:
            # modifier up (역순) — 정상/예외 경로 모두 해제.
            for kc in reversed(modifiers):
                try:
                    self._xtest_key(kc, False)
                except Exception:
                    pass
                time.sleep(0.02)
