"""Windows process window control service.

PrintWindow + PostMessage 기반으로 다른 Windows 프로세스의 윈도우를 캡처/조작.
디바이스 타입 "wincontrol" 의 백엔드 구현.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import logging
import os
import sys
import threading
import time
from ctypes import Structure, Union, c_long, c_short
from ctypes.wintypes import DWORD, HANDLE, HWND, LONG, WORD
from typing import Callable, Optional, TypeVar

# windll 은 ctypes 의 Windows-only 어트리뷰트. Linux/macOS 에서는 존재하지
# 않아 module import 자체가 실패함. 모듈 로드는 항상 성공시키되, 실제 호출은
# 아래 _WIN32_AVAILABLE 가드로 차단된다.
if sys.platform == "win32":
    from ctypes import windll  # type: ignore[attr-defined]
else:
    windll = None  # type: ignore[assignment]


_T = TypeVar("_T")

# SendInput 용 구조체 (keybd_event 대체).
# keybd_event 는 KEYEVENTF_UNICODE 를 제대로 처리 못 해서 일부 앱(IME-aware,
# DirectInput 사용 등) 에 입력이 안 들어감. SendInput 이 표준.
ULONG_PTR = ctypes.c_size_t  # 32/64bit 자동


class _MOUSEINPUT(Structure):
    _fields_ = [("dx", LONG), ("dy", LONG), ("mouseData", DWORD),
                ("dwFlags", DWORD), ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class _KEYBDINPUT(Structure):
    _fields_ = [("wVk", WORD), ("wScan", WORD), ("dwFlags", DWORD),
                ("time", DWORD), ("dwExtraInfo", ULONG_PTR)]


class _HARDWAREINPUT(Structure):
    _fields_ = [("uMsg", DWORD), ("wParamL", WORD), ("wParamH", WORD)]


class _INPUT_UNION(Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", DWORD), ("u", _INPUT_UNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_EXTENDEDKEY = 0x0001

logger = logging.getLogger(__name__)

# Windows에서만 동작 — 모듈 import 자체는 항상 가능하도록 lazy import
_WIN32_AVAILABLE = False
_IMPORT_ERROR: Optional[str] = None
try:
    import win32gui  # type: ignore
    import win32con  # type: ignore
    import win32api  # type: ignore
    import win32process  # type: ignore
    import win32ui  # type: ignore
    import psutil
    from PIL import Image
    _WIN32_AVAILABLE = True
except Exception as _e:  # pragma: no cover — non-Windows
    _IMPORT_ERROR = str(_e)


# Per-Window DPI 컨텍스트 API (Win10 1607+) — DPI-unaware 타겟을 캡처할 때
# 스레드 awareness 를 타겟에 맞춰 GetClientRect/PrintWindow/ClientToScreen 를
# 일관된 좌표계로 동작시키기 위함. argtype/restype 미지정 시 64bit 핸들이 잘림.
if _WIN32_AVAILABLE:
    try:
        windll.user32.GetWindowDpiAwarenessContext.argtypes = [HWND]
        windll.user32.GetWindowDpiAwarenessContext.restype = HANDLE
        windll.user32.SetThreadDpiAwarenessContext.argtypes = [HANDLE]
        windll.user32.SetThreadDpiAwarenessContext.restype = HANDLE
    except (AttributeError, OSError):
        # Win10 1607 미만 — 타겟 매칭 불가, 프로세스 기본 awareness 그대로 사용.
        pass
    # DWM API — Win10/11 의 invisible drop shadow 제외한 visible bounds 조회용.
    try:
        from ctypes.wintypes import DWORD, LPVOID
        windll.dwmapi.DwmGetWindowAttribute.argtypes = [HWND, DWORD, LPVOID, DWORD]
        windll.dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long  # HRESULT
    except (AttributeError, OSError):
        pass


# 가상키 매핑 (대표 키만 노출 — 추후 필요시 확장)
VK_MAP: dict[str, int] = {}
if _WIN32_AVAILABLE:
    VK_MAP = {
        "ENTER": win32con.VK_RETURN,
        "RETURN": win32con.VK_RETURN,
        "TAB": win32con.VK_TAB,
        "ESC": win32con.VK_ESCAPE,
        "ESCAPE": win32con.VK_ESCAPE,
        "BACKSPACE": win32con.VK_BACK,
        "BACK": win32con.VK_BACK,
        "DELETE": win32con.VK_DELETE,
        "DEL": win32con.VK_DELETE,
        "SPACE": win32con.VK_SPACE,
        "UP": win32con.VK_UP,
        "DOWN": win32con.VK_DOWN,
        "LEFT": win32con.VK_LEFT,
        "RIGHT": win32con.VK_RIGHT,
        "HOME": win32con.VK_HOME,
        "END": win32con.VK_END,
        "PAGEUP": win32con.VK_PRIOR,
        "PAGEDOWN": win32con.VK_NEXT,
        "F1": win32con.VK_F1, "F2": win32con.VK_F2, "F3": win32con.VK_F3,
        "F4": win32con.VK_F4, "F5": win32con.VK_F5, "F6": win32con.VK_F6,
        "F7": win32con.VK_F7, "F8": win32con.VK_F8, "F9": win32con.VK_F9,
        "F10": win32con.VK_F10, "F11": win32con.VK_F11, "F12": win32con.VK_F12,
    }


def _resolve_vk(key: str) -> int:
    """문자열 키 → 가상키 코드. 'A'~'Z'/'0'~'9'는 ord, 그 외는 VK_MAP."""
    if not key:
        raise ValueError("empty key")
    upper = key.upper()
    if upper in VK_MAP:
        return VK_MAP[upper]
    if len(upper) == 1 and (upper.isalpha() or upper.isdigit()):
        return ord(upper)
    raise ValueError(f"Unknown key: {key}")


class WinControlService:
    """단일 Win32 윈도우를 임베드 대상으로 잡고 캡처/입력 처리."""

    def __init__(self) -> None:
        self._hwnd: Optional[int] = None
        self._pid: Optional[int] = None
        self._process_name: str = ""
        self._exe_path: str = ""
        self._window_title: str = ""
        self._window_class: str = ""
        # UWP/WinUI3 감지 — PrintWindow 가 PW_RENDERFULLCONTENT 없으면 검은 화면을 반환,
        # PostMessage 도 거의 안 먹는다. attach 시 1회 판정 후 캡처/사용자 알림에 활용.
        self._is_uwp: bool = False
        # UWP 의 진짜 콘텐츠 윈도우(Windows.UI.Core.CoreWindow) — 캡처 시 우선 사용.
        self._content_hwnd: Optional[int] = None
        # UWP AppUserModelID — 종료된 UWP 앱 재실행에 필요(.exe 직접 실행 불가).
        # 예: "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"
        self._aumid: str = ""
        # deferred_restore 상태 — 반드시 스레드 로컬이어야 함. 모든 액션은 watchdog
        # (run_action_with_timeout)의 별도 스레드에서 실행되는데, 타임아웃으로 leak 된
        # 스레드가 with 블록 안에 갇혀 있으면 인스턴스 공유 카운터로는 이후 모든 액션의
        # 복원(포커스+커서)이 스킵돼 마우스가 타겟에 남는다.
        self._defer_local = threading.local()

    @staticmethod
    def is_available() -> bool:
        return _WIN32_AVAILABLE

    @staticmethod
    def import_error() -> Optional[str]:
        return _IMPORT_ERROR

    # ── 프로세스/윈도우 검색 ──────────────────────────────────────────
    def _enum_windows(self) -> list[dict]:
        """모든 가시 최상위 윈도우 (PID당 여러 창 허용). 내부 검색용."""
        if not _WIN32_AVAILABLE:
            return []
        results: list[dict] = []

        def _cb(hwnd: int, _: object) -> bool:
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True
                rect = win32gui.GetWindowRect(hwnd)
                w = rect[2] - rect[0]
                h = rect[3] - rect[1]
                if w <= 0 or h <= 0:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                try:
                    proc = psutil.Process(pid)
                    name = proc.name()
                    try:
                        exe_path = proc.exe()
                    except (psutil.AccessDenied, FileNotFoundError):
                        exe_path = ""
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    return True
                try:
                    cls_name = win32gui.GetClassName(hwnd)
                except Exception:
                    cls_name = ""
                results.append({
                    "pid": int(pid),
                    "hwnd": int(hwnd),
                    "name": name,
                    "exe_path": exe_path,
                    "title": title,
                    "class_name": cls_name,
                    "width": w,
                    "height": h,
                })
            except Exception as e:
                logger.debug("enum_window callback error: %s", e)
            return True

        try:
            win32gui.EnumWindows(_cb, None)
        except Exception as e:
            logger.warning("EnumWindows failed: %s", e)
        return results

    def list_processes(self) -> list[dict]:
        """가시 최상위 윈도우 + PID/프로세스명 목록.

        같은 PID 라도 별개의 최상위 창(예: VS_BASE 메인 + CANDB TX CONTROL 자식 툴윈도우)이
        있으면 모두 노출 — 사용자가 임베드할 창을 직접 선택할 수 있게.
        프로세스명/타이틀 순으로 정렬.
        """
        if not _WIN32_AVAILABLE:
            return []
        return sorted(self._enum_windows(),
                      key=lambda d: (d["name"].lower(), d["title"].lower()))

    def find_window(
        self,
        process_name: str = "",
        exe_path: str = "",
        title_pattern: str = "",
        class_name: str = "",
    ) -> Optional[dict]:
        """주어진 조건과 일치하는 첫 번째 윈도우 정보 반환.

        매칭 규칙:
          - exe_path: 절대 경로 일치, 실패 시 파일명(basename) 일치로 폴백 (대소문자 무시)
          - process_name: 파일명 정확 일치 (대소문자 무시)
          - title_pattern: 부분 문자열 일치 (대소문자 무시)
          - class_name: 정확 일치
        지정된 필드만 사용 (빈 값은 무시). 모두 빈 값이면 None.

        exe_path 를 절대경로 완전일치로만 필터링하면, 시나리오를 내보내 다른 PC 에서
        재생할 때 녹화 PC 의 절대 경로(사용자 폴더/설치 위치 상이)와 어긋나 실행 중인
        창도 매칭에서 탈락한다. 파일명(basename)이 같으면 매칭을 허용해 이식성 확보.
        """
        if not _WIN32_AVAILABLE or not (process_name or exe_path or title_pattern or class_name):
            return None
        exe_path_norm = (exe_path or "").strip().lower()
        exe_base_norm = os.path.basename(exe_path_norm) if exe_path_norm else ""
        proc_name_norm = (process_name or "").strip().lower()
        title_norm = (title_pattern or "").strip().lower()
        cls_norm = (class_name or "").strip()
        for w in self._enum_windows():
            if exe_path_norm:
                w_exe = (w.get("exe_path") or "").lower()
                # 절대경로 완전일치 실패 시 basename 폴백 — 다른 PC 에서도 같은 exe 매칭.
                if w_exe != exe_path_norm and os.path.basename(w_exe) != exe_base_norm:
                    continue
            if proc_name_norm and (w.get("name") or "").lower() != proc_name_norm:
                continue
            if title_norm and title_norm not in (w.get("title") or "").lower():
                continue
            if cls_norm and (w.get("class_name") or "") != cls_norm:
                continue
            return w
        return None

    @staticmethod
    def _wait_for_input_idle(hwnd: int, timeout_ms: int = 3000) -> None:
        """대상 윈도우의 프로세스가 입력 받을 준비 될 때까지 대기.

        새로 spawn 한 프로세스(또는 UWP 활성화 직후)는 메시지 큐/페인팅이 안정화되기
        전에는 입력을 무시한다. user32!WaitForInputIdle 은 프로세스가 첫 GetMessage
        호출(=메인 루프 idle)에 도달할 때까지 블록 — Windows 표준 동기화 방법.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if not pid:
            return
        PROCESS_QUERY_INFORMATION = 0x0400
        SYNCHRONIZE = 0x00100000
        h = windll.kernel32.OpenProcess(
            PROCESS_QUERY_INFORMATION | SYNCHRONIZE, False, int(pid),
        )
        if not h:
            return
        try:
            # 0=성공, WAIT_TIMEOUT=258, WAIT_FAILED=0xFFFFFFFF.
            # 콘솔 앱은 WAIT_FAILED — 무시하고 진행해도 무해.
            windll.user32.WaitForInputIdle(h, int(timeout_ms))
        except Exception as e:
            logger.debug("WaitForInputIdle failed: %s", e)
        finally:
            try:
                windll.kernel32.CloseHandle(h)
            except Exception:
                pass

    @staticmethod
    def launch_process(exe_path: str, args: Optional[list[str]] = None) -> int:
        """일반 .exe 실행. 성공 시 PID 반환. exe_path 가 비어있으면 ValueError."""
        if not exe_path:
            raise ValueError("exe_path is empty")
        import subprocess
        cmd = [exe_path] + (args or [])
        # 새 콘솔 분리 + 입력 무시 — 백엔드 종료와 독립적으로 살아남기.
        creationflags = 0
        if _WIN32_AVAILABLE:
            try:
                creationflags = win32con.DETACHED_PROCESS | win32con.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
            except Exception:
                creationflags = 0x00000008  # DETACHED_PROCESS
        proc = subprocess.Popen(
            cmd, close_fds=True, creationflags=creationflags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("WinControl launched: %s pid=%d", exe_path, proc.pid)
        return proc.pid

    @staticmethod
    def launch_uwp(aumid: str) -> None:
        """UWP/Packaged 앱 활성화 — explorer.exe shell:AppsFolder\\<AUMID>.

        UWP 는 .exe 직접 실행 시 AppContainer 가 없어 빈 호스트만 뜨고 실제 앱은 안 뜸.
        explorer 가 AppX 매니페스트를 따라 정상 launch 한다.
        """
        if not aumid:
            raise ValueError("aumid is empty")
        import subprocess
        cmd = ["explorer.exe", f"shell:AppsFolder\\{aumid}"]
        creationflags = 0
        if _WIN32_AVAILABLE:
            try:
                creationflags = win32con.DETACHED_PROCESS  # type: ignore[attr-defined]
            except Exception:
                creationflags = 0x00000008
        subprocess.Popen(
            cmd, close_fds=True, creationflags=creationflags,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        logger.info("WinControl launched UWP: %s", aumid)

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
        """저장된 프로세스 정보를 사용해 임베드 상태를 보장.

        흐름:
          1) 이미 attached + 현재 프로세스명/타이틀이 일치하면 그대로 사용
          2) 일치 안 하거나 미임베드면 find_window로 매칭 윈도우 탐색 후 attach
          3) 못 찾고 launch_if_missing=True 이면 launch:
             - aumid 가 있으면 UWP 활성화 (explorer shell:AppsFolder\\AUMID) — UWP 우선
             - 아니면 exe_path 로 일반 .exe 실행
          4) target_width/height > 0 이면 attach 후 client area 를 해당 크기로 리사이즈
             — 좌표 기반 입력이 녹화 시점과 동일한 위치를 가리키도록 보장.
        성공 시 status() 반환, 실패 시 RuntimeError.
        """
        if not _WIN32_AVAILABLE:
            raise RuntimeError(f"WinControl unavailable: {_IMPORT_ERROR}")

        def _maybe_resize() -> None:
            if target_width > 0 and target_height > 0:
                cur_w, cur_h = self.get_window_size()
                if cur_w != int(target_width) or cur_h != int(target_height):
                    self.resize_client(int(target_width), int(target_height))

        # 1) 현재 attach 가 조건과 일치하는지
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
            # 조건 불일치 → 새 attach 시도 (기존 핸들 유지하지 않음)
            self.detach()

        # 2) 현재 시스템에서 매칭 윈도우 탐색
        match = self.find_window(process_name, exe_path, title_pattern, class_name)
        if match:
            self.attach(match["hwnd"])
            _maybe_resize()
            return self.status()

        # 3) 프로세스 실행 후 윈도우 등장 대기
        if not launch_if_missing or not (exe_path or aumid):
            raise RuntimeError(
                f"WinControl: matching window not found "
                f"(name={process_name!r}, exe={exe_path!r}, aumid={aumid!r}, title~={title_pattern!r})"
            )
        launched_what = ""
        try:
            if aumid:
                # UWP/Packaged 앱: explorer 로 활성화 — .exe 직접 실행 불가.
                self.launch_uwp(aumid)
                launched_what = f"AUMID={aumid}"
            elif exe_path:
                # 다른 PC 에서 재생 시 흔한 실패: 시나리오에 박힌 절대 경로가 이 PC 엔 없음
                # (사용자 폴더/설치 위치 상이). raw WinError 2 대신 실행 가능한 안내로 교체.
                if not os.path.exists(exe_path):
                    _base = os.path.basename(exe_path) or exe_path
                    raise RuntimeError(
                        f"대상 실행 파일을 이 PC 에서 찾을 수 없습니다 — '{exe_path}'. "
                        f"공유된 시나리오에 녹화 PC 의 절대 경로가 저장되어 있어 경로가 다릅니다. "
                        f"대상 앱('{_base}')을 이 PC 에서 직접 실행한 뒤 재생하거나, "
                        f"스텝 params 의 exe_path 를 이 PC 의 실제 경로로 수정하세요."
                    )
                self.launch_process(exe_path)
                launched_what = exe_path
        except RuntimeError:
            # 위에서 만든 안내 메시지는 그대로 전달 (이중 래핑 방지).
            raise
        except Exception as e:
            raise RuntimeError(f"WinControl: failed to launch ({launched_what or aumid or exe_path!r}): {e}")

        deadline = time.monotonic() + max(0.5, wait_seconds)
        while time.monotonic() < deadline:
            time.sleep(0.3)
            match = self.find_window(process_name, exe_path, title_pattern, class_name)
            if match:
                # 새로 launch 한 프로세스: 메시지 큐가 idle 상태에 도달할 때까지 대기.
                # 이게 없으면 첫 send_tap 이 paint/init 중인 윈도우에 흡수돼 무시된다.
                self._wait_for_input_idle(match["hwnd"], timeout_ms=3000)
                # 추가 안정화 — 페인팅/레이아웃 완료까지 약간 더 대기 (UWP 는 더 길게 필요).
                time.sleep(0.5)
                self.attach(match["hwnd"])
                _maybe_resize()
                return self.status()
        raise RuntimeError(
            f"WinControl: launched ({launched_what}) but window did not appear within {wait_seconds:.1f}s "
            f"(name={process_name!r}, title~={title_pattern!r})"
        )

    # ── UWP/WinUI3 감지 + AUMID 추출 ─────────────────────────────
    @staticmethod
    def _get_aumid_for_pid(pid: int) -> str:
        """프로세스의 AppUserModelID 반환. UWP/Packaged 앱이 아니면 빈 문자열.

        Win32 API: kernel32!GetApplicationUserModelId(hProcess, *pulLength, pBuf)
        반환 0=성공, 122(ERROR_INSUFFICIENT_BUFFER)=버퍼 부족, 그 외=비-패키지 앱.
        """
        if not _WIN32_AVAILABLE or not pid:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            size = ctypes.c_uint32(260)
            buf = ctypes.create_unicode_buffer(size.value)
            res = windll.kernel32.GetApplicationUserModelId(h, ctypes.byref(size), buf)
            if res == 122:  # ERROR_INSUFFICIENT_BUFFER → 더 큰 버퍼로 재시도
                buf = ctypes.create_unicode_buffer(size.value)
                res = windll.kernel32.GetApplicationUserModelId(h, ctypes.byref(size), buf)
            if res == 0:
                return buf.value or ""
            return ""
        except Exception as e:
            logger.debug("GetApplicationUserModelId failed for pid %d: %s", pid, e)
            return ""
        finally:
            try:
                windll.kernel32.CloseHandle(h)
            except Exception:
                pass

    @staticmethod
    def _detect_uwp(hwnd: int) -> tuple[bool, Optional[int]]:
        """UWP/WinUI3 여부 + 진짜 콘텐츠 윈도우(CoreWindow) hwnd 반환.

        UWP 앱은 ApplicationFrameWindow 가 호스트이고 콘텐츠는 자식 CoreWindow.
        WinUI3 (Win11 새 메모장 등) 도 비슷한 자식 윈도우 구조.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return (False, None)
        try:
            cls = win32gui.GetClassName(hwnd) or ""
        except Exception:
            cls = ""
        is_host = (cls == "ApplicationFrameWindow" or "Microsoft.UI" in cls)

        content_hwnd: Optional[int] = None
        # 자식 중 CoreWindow / Microsoft.UI.* 검색
        try:
            container = [None]  # type: list[Optional[int]]

            def _cb(child: int, _: object) -> bool:
                try:
                    ccls = win32gui.GetClassName(child) or ""
                except Exception:
                    return True
                if ccls == "Windows.UI.Core.CoreWindow" or "Microsoft.UI.Content" in ccls:
                    container[0] = child
                    return False
                return True
            win32gui.EnumChildWindows(hwnd, _cb, None)
            content_hwnd = container[0]
        except Exception:
            content_hwnd = None

        is_uwp = is_host or (content_hwnd is not None)
        return (is_uwp, content_hwnd)

    # ── 임베드(대상 윈도우) ──────────────────────────────────────────
    def attach(self, hwnd: int) -> dict:
        if not _WIN32_AVAILABLE:
            raise RuntimeError(f"pywin32 not available: {_IMPORT_ERROR}")
        if not win32gui.IsWindow(hwnd):
            raise ValueError(f"Invalid window handle: {hwnd}")
        self._hwnd = int(hwnd)
        try:
            _, self._pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            self._pid = None
        self._window_title = win32gui.GetWindowText(hwnd) or ""
        try:
            self._window_class = win32gui.GetClassName(hwnd) or ""
        except Exception:
            self._window_class = ""
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
        # UWP/WinUI3 판정 — 캡처 플래그 자동 결정 + 사용자에게 입력 모드 권장 정보 노출용
        self._is_uwp, self._content_hwnd = self._detect_uwp(hwnd)
        # UWP 면 AUMID 추출 — 콘텐츠 자식 PID 우선 (호스트 ApplicationFrameHost 는 비-패키지 프로세스).
        self._aumid = ""
        if self._is_uwp:
            target_pid: Optional[int] = None
            if self._content_hwnd:
                try:
                    _, target_pid = win32process.GetWindowThreadProcessId(self._content_hwnd)
                except Exception:
                    target_pid = None
            if not target_pid:
                target_pid = self._pid
            if target_pid:
                self._aumid = self._get_aumid_for_pid(target_pid)
        logger.info("WinControl attached: hwnd=%d pid=%s name=%s exe=%s title=%r class=%s uwp=%s aumid=%r content=%s",
                    hwnd, self._pid, self._process_name, self._exe_path,
                    self._window_title, self._window_class, self._is_uwp, self._aumid, self._content_hwnd)
        return self.status()

    def detach(self) -> None:
        logger.info("WinControl detached: hwnd=%s", self._hwnd)
        self._hwnd = None
        self._pid = None
        self._process_name = ""
        self._exe_path = ""
        self._window_title = ""
        self._window_class = ""
        self._is_uwp = False
        self._content_hwnd = None
        self._aumid = ""

    def is_attached(self) -> bool:
        if not _WIN32_AVAILABLE or self._hwnd is None:
            return False
        try:
            return bool(win32gui.IsWindow(self._hwnd))
        except Exception:
            return False

    def status(self) -> dict:
        if not self.is_attached():
            return {"attached": False, "available": _WIN32_AVAILABLE,
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
            # outer (타이틀바 포함) 크기 — 풀 윈도우 캡처 비트맵의 자연 크기.
            "outer_width": ow,
            "outer_height": oh,
            # client 영역의 outer 비트맵 내 좌상단 오프셋 — 프론트가 클릭 좌표를
            # client-space 로 변환할 때 빼는 값. (보더/타이틀바 두께)
            "client_offset_x": ox,
            "client_offset_y": oy,
            "is_uwp": self._is_uwp,
            "content_hwnd": self._content_hwnd,
            "aumid": self._aumid,
        }

    @contextlib.contextmanager
    def _target_dpi_ctx(self):
        """스레드 DPI 컨텍스트를 타겟 윈도우의 것과 일시적으로 일치시킴.

        백엔드 프로세스는 Per-Monitor V2(고DPI 타겟용) 인데 타겟 앱이 DPI-unaware
        면 GetClientRect 가 반환하는 크기와 PrintWindow 가 실제로 칠하는 영역이
        어긋나서 캡처 비트맵 우/하단이 잘린다. 스레드 awareness 를 타겟에 맞추면
        GetClientRect/PrintWindow/ClientToScreen/GetSystemMetrics 가 모두 같은
        단위(타겟 좌표계)로 동작 → 캡처가 잘리지 않고 입력 좌표도 정확.
        """
        if not _WIN32_AVAILABLE or self._hwnd is None:
            yield
            return
        user32 = windll.user32
        prev_ctx = None
        target_ctx = None
        try:
            target_ctx = user32.GetWindowDpiAwarenessContext(self._hwnd)
        except Exception:
            target_ctx = None
        if target_ctx:
            try:
                prev_ctx = user32.SetThreadDpiAwarenessContext(target_ctx)
            except Exception:
                prev_ctx = None
        try:
            yield
        finally:
            if prev_ctx:
                try:
                    user32.SetThreadDpiAwarenessContext(prev_ctx)
                except Exception:
                    pass

    @staticmethod
    def _get_visible_window_rect(hwnd: int) -> Optional[tuple[int, int, int, int]]:
        """Shadow 제외한 실제 visible 윈도우 rect (left, top, right, bottom).

        Win10/11 의 GetWindowRect 는 invisible drop shadow(각 모서리 ~7px)까지 포함하는
        extended frame bounds 를 반환 → BitBlt 시 가장자리에 shadow 너머의 화면이 섞임.
        DWM 의 DWMWA_EXTENDED_FRAME_BOUNDS(=9) 가 shadow 제외한 실제 visible bounds 반환.
        실패 시 None — 호출자가 GetWindowRect 폴백.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return None
        try:
            from ctypes.wintypes import RECT
            r = RECT()
            DWMWA_EXTENDED_FRAME_BOUNDS = 9
            hr = windll.dwmapi.DwmGetWindowAttribute(
                int(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(r), ctypes.sizeof(r),
            )
            if hr == 0:  # S_OK
                return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        except Exception:
            pass
        return None

    def get_window_size(self) -> tuple[int, int]:
        """Client area 크기 (물리 픽셀, Per-Monitor V2 기준)."""
        if not self.is_attached():
            return (0, 0)
        try:
            rect = win32gui.GetClientRect(self._hwnd)
            return (rect[2] - rect[0], rect[3] - rect[1])
        except Exception:
            return (0, 0)

    def get_outer_size(self) -> tuple[int, int]:
        """윈도우(타이틀바/보더 포함, shadow 제외) 크기 = BitBlt 비트맵 크기.
        DWM EXTENDED_FRAME_BOUNDS 기준 — _capture_via_screen 과 동일 좌표계.
        """
        if not self.is_attached():
            return (0, 0)
        try:
            vr = self._get_visible_window_rect(self._hwnd)
            if vr is None:
                vr = win32gui.GetWindowRect(self._hwnd)
            return (vr[2] - vr[0], vr[3] - vr[1])
        except Exception:
            return (0, 0)

    def get_client_offset(self) -> tuple[int, int]:
        """비트맵 (0,0) 에서 client (0,0) 까지의 오프셋 (물리 픽셀).

        - 비트맵 = DWM 물리 픽셀 rect 기준 (1193x855 같은 큰 값)
        - GetWindowRect / ClientToScreen 은 SYSTEM_AWARE 타겟에서 논리(시스템 DPI) 픽셀 반환
        → 좌표계 불일치 → 둘 다 직접 계산해서 물리로 환산:
          logical_client_offset = ClientToScreen(0,0) - GetWindowRect.topleft
          scale = DWM_size / GetWindowRect_size
          physical_client_offset = logical_client_offset * scale
        """
        if not self.is_attached():
            return (0, 0)
        try:
            hwnd = self._hwnd
            vr = self._get_visible_window_rect(hwnd)
            if vr is None:
                vr = win32gui.GetWindowRect(hwnd)
            wr = win32gui.GetWindowRect(hwnd)
            cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            log_off_x = cx - wr[0]
            log_off_y = cy - wr[1]
            log_w = max(1, wr[2] - wr[0])
            log_h = max(1, wr[3] - wr[1])
            phys_w = vr[2] - vr[0]
            phys_h = vr[3] - vr[1]
            scale_x = phys_w / log_w
            scale_y = phys_h / log_h
            return (int(round(log_off_x * scale_x)), int(round(log_off_y * scale_y)))
        except Exception:
            return (0, 0)

    @staticmethod
    def _resize_hwnd_client(hwnd: int, target_w: int, target_h: int) -> None:
        """임의 hwnd의 client area를 target 크기로 리사이즈 (self._hwnd와 무관).

        검증용 캡처 시 좌표/스케일 정합성을 위해 attached 상태 변경 없이 사용.
        """
        if not _WIN32_AVAILABLE or not hwnd or target_w <= 0 or target_h <= 0:
            return
        try:
            if win32gui.IsZoomed(hwnd) or win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.05)
            cur_window = win32gui.GetWindowRect(hwnd)
            cur_outer_w = cur_window[2] - cur_window[0]
            cur_outer_h = cur_window[3] - cur_window[1]
            cur_client = win32gui.GetClientRect(hwnd)
            cur_client_w = cur_client[2] - cur_client[0]
            cur_client_h = cur_client[3] - cur_client[1]
            if cur_client_w == target_w and cur_client_h == target_h:
                return
            dx = cur_outer_w - cur_client_w
            dy = cur_outer_h - cur_client_h
            new_outer_w = max(1, int(target_w) + dx)
            new_outer_h = max(1, int(target_h) + dy)
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, new_outer_w, new_outer_h,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            time.sleep(0.05)
        except Exception as e:
            logger.debug("WinControl _resize_hwnd_client failed: %s", e)

    def capture_window_by_match(
        self,
        process_name: str = "",
        exe_path: str = "",
        title_pattern: str = "",
        class_name: str = "",
        aumid: str = "",
        target_width: int = 0,
        target_height: int = 0,
        fmt: str = "png",
        launch_if_missing: bool = True,
        wait_seconds: float = 5.0,
    ) -> bytes:
        """대상 프로세스 윈도우를 임베드/포커스 상태 변경 없이 직접 캡처 (검증 전용).

        - find_window 로 매칭되는 윈도우 hwnd 검색
        - 못 찾고 launch_if_missing=True 면 exe_path / aumid 로 실행 후 등장 대기
        - target_width/height 가 주어지면 그 크기로 client area 리사이즈 (좌표 정합성)
        - capture_hwnd_bgr 로 직접 캡처 → PIL 인코딩

        self._hwnd(현재 임베드된 윈도우)는 건드리지 않음 — 사용자가 다른 윈도우를
        임베드/조작 중이어도 검증 캡처는 step 이 지정한 프로세스 윈도우에서 떠짐.
        """
        if not _WIN32_AVAILABLE:
            raise RuntimeError(f"WinControl unavailable: {_IMPORT_ERROR}")
        match = self.find_window(process_name, exe_path, title_pattern, class_name)
        if match is None and launch_if_missing and (exe_path or aumid):
            try:
                if aumid:
                    self.launch_uwp(aumid)
                else:
                    # 다른 PC 재생 시 절대 경로 부재 — raw WinError 2 대신 실행 가능한 안내.
                    if not os.path.exists(exe_path):
                        _base = os.path.basename(exe_path) or exe_path
                        raise RuntimeError(
                            f"대상 실행 파일을 이 PC 에서 찾을 수 없습니다 — '{exe_path}'. "
                            f"대상 앱('{_base}')을 이 PC 에서 직접 실행한 뒤 재생하거나, "
                            f"스텝 params 의 exe_path 를 이 PC 의 실제 경로로 수정하세요."
                        )
                    self.launch_process(exe_path)
            except RuntimeError:
                raise
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
                f"(name={process_name!r}, title~={title_pattern!r}, exe={exe_path!r}, aumid={aumid!r})"
            )
        hwnd = int(match["hwnd"])
        # 좌표 정합성을 위해 녹화 시점과 동일한 client 크기로 리사이즈.
        # 사용자 윈도우 크기가 변경됨 — 검증 중에만 발생하는 의도된 부작용.
        if target_width > 0 and target_height > 0:
            self._resize_hwnd_client(hwnd, int(target_width), int(target_height))
        # 모달 미리보기(capture_window) 와 동일한 dispatch 로 캡처 — 풀 윈도우(타이틀바 포함).
        # capture_hwnd_bgr 는 항상 PW_CLIENTONLY 라 일반 Win32 앱에서 타이틀바가 빠져 좌표가 어긋남.
        is_uwp, content_hwnd = self._detect_uwp(hwnd)
        img = self._capture_window_image_for(hwnd, is_uwp, content_hwnd)
        if img is None:
            raise RuntimeError("capture_window_by_match: capture failed (BitBlt + PrintWindow)")
        buf = io.BytesIO()
        if (fmt or "png").lower() == "jpeg":
            img.convert("RGB").save(buf, format="JPEG", quality=85)
        else:
            img.save(buf, format="PNG")
        return buf.getvalue()

    def resize_client(self, target_w: int, target_h: int) -> tuple[int, int]:
        """대상 윈도우의 client area 를 (target_w, target_h) 로 리사이즈.

        녹화 시점과 동일한 client 크기로 맞춰서 좌표 기반 입력이 항상 같은
        UI 요소를 가리키도록 보장. 외곽(타이틀바/보더) 크기를 빼고 더해
        실제 client 가 정확히 일치하도록 SetWindowPos 호출.

        반환: 리사이즈 후 실제 client (w, h). 윈도우가 최소/최대 크기 제약
        때문에 요청 크기보다 작거나 클 수 있으므로 호출자에게 실측값 전달.
        """
        if not self.is_attached() or target_w <= 0 or target_h <= 0:
            return self.get_window_size()
        hwnd = self._hwnd
        try:
            # 최대화/최소화 상태면 정상 크기로 복원 — 그래야 SetWindowPos 가 먹힘.
            try:
                if win32gui.IsZoomed(hwnd) or win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    time.sleep(0.05)
            except Exception:
                pass
            # 외곽-클라이언트 차이(보더/타이틀바)를 측정해서 outer 목표 크기 산출.
            cur_window = win32gui.GetWindowRect(hwnd)
            cur_outer_w = cur_window[2] - cur_window[0]
            cur_outer_h = cur_window[3] - cur_window[1]
            cur_client = win32gui.GetClientRect(hwnd)
            cur_client_w = cur_client[2] - cur_client[0]
            cur_client_h = cur_client[3] - cur_client[1]
            dx = cur_outer_w - cur_client_w
            dy = cur_outer_h - cur_client_h
            new_outer_w = max(1, int(target_w) + dx)
            new_outer_h = max(1, int(target_h) + dy)
            # SWP_NOMOVE: 위치 유지, SWP_NOZORDER: z-order 유지, SWP_NOACTIVATE: 포커스 안 뺏음.
            SWP_NOMOVE = 0x0002
            SWP_NOZORDER = 0x0004
            SWP_NOACTIVATE = 0x0010
            win32gui.SetWindowPos(
                hwnd, 0, 0, 0, new_outer_w, new_outer_h,
                SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE,
            )
            # 레이아웃 반영 시간.
            time.sleep(0.05)
        except Exception as e:
            logger.debug("WinControl resize_client failed: %s", e)
        return self.get_window_size()

    # ── 캡처 ─────────────────────────────────────────────────────────
    def _capture_via_screen(self, hwnd: int) -> Optional[Image.Image]:
        """화면(데스크탑 DC)에서 윈도우 영역만 BitBlt — DPI/DWM 합성 영향을 안 받는
        가장 안정적인 캡처 방법.

        BitBlt(GetWindowDC) 는 윈도우 자체 백버퍼에서 복사하므로 DPI virtualization
        으로 painted 영역 < frame 인 경우 garbage 가 섞임. 스크린 캡처는 DWM 이
        합성한 최종 픽셀을 가져오므로 DPI 호환성 설정과 무관하게 보이는 그대로 잡힘.

        제약:
          - 윈도우가 가려져 있으면(occluded) 위에 있는 창의 픽셀이 잡힘 → BitBlt/
            PrintWindow 가 실패한 마지막 폴백으로만 사용.
          - 위치는 visible_window_rect(shadow 제외) 기준 → 비트맵 = 풀 윈도우.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return None
        try:
            if not win32gui.IsWindow(hwnd) or win32gui.IsIconic(hwnd):
                return None
            # DWM EXTENDED_FRAME_BOUNDS 가 PMv2 스레드에서 일관되게 물리 픽셀 좌표를 반환 →
            # 실제 화면에 렌더링되는 영역과 정확히 일치. GetWindowRect 는 SYSTEM_AWARE 앱에서
            # 시스템 DPI 기준 논리 좌표를 반환할 수 있어 멀티 DPI 모니터 환경에서 어긋남.
            vr = self._get_visible_window_rect(hwnd)
            if vr is None:
                vr = win32gui.GetWindowRect(hwnd)
            x, y, x2, y2 = vr
            w, h = x2 - x, y2 - y
        except Exception:
            return None
        if w <= 0 or h <= 0:
            return None
        # Desktop DC (hwnd=0) 에서 윈도우 화면 좌표로 BitBlt.
        desktop_dc = win32gui.GetDC(0)
        if not desktop_dc:
            return None
        mfc_dc = None
        mem_dc = None
        bmp = None
        try:
            mfc_dc = win32ui.CreateDCFromHandle(desktop_dc)
            mem_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            mem_dc.SelectObject(bmp)
            mem_dc.BitBlt((0, 0), (w, h), mfc_dc, (x, y), win32con.SRCCOPY)
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (info["bmWidth"], info["bmHeight"]),
                bits, "raw", "BGRX", 0, 1,
            )
        except Exception:
            return None
        finally:
            if bmp is not None:
                try:
                    win32gui.DeleteObject(bmp.GetHandle())
                except Exception:
                    pass
            if mem_dc is not None:
                try:
                    mem_dc.DeleteDC()
                except Exception:
                    pass
            if mfc_dc is not None:
                try:
                    mfc_dc.DeleteDC()
                except Exception:
                    pass
            try:
                win32gui.ReleaseDC(0, desktop_dc)
            except Exception:
                pass

    def _capture_via_screen_activated(self, hwnd: int) -> Optional[Image.Image]:
        """대상 윈도우를 일시 활성화한 뒤 화면 영역에서 BitBlt 캡처 (Alt+PrintScreen 등가).

        DWM 합성된 최종 픽셀을 가져오므로 DPI 호환성/awareness 와 무관하게 항상 정확.
        BitBlt(window DC)나 PrintWindow 가 우/하단 garbage 로 실패하는 케이스의 결정적 해결책.

        제약: 대상 윈도우가 잠깐 포어그라운드로 와서 사용자 화면에 짧은 플리커 발생.
        AttachThreadInput 트릭 + 이전 포어그라운드 복원으로 영향 최소화.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return None
        try:
            if not win32gui.IsWindow(hwnd):
                return None
            if win32gui.IsIconic(hwnd):
                try:
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                except Exception:
                    pass
        except Exception:
            return None

        prev_fg = None
        try:
            prev_fg = windll.user32.GetForegroundWindow()
            if prev_fg and not win32gui.IsWindow(prev_fg):
                prev_fg = None
        except Exception:
            prev_fg = None

        # 최적화: 이미 타겟(또는 같은 프로세스 자식)이 포어그라운드면 활성화 스킵 →
        # 클릭 직후처럼 사용자가 임베드 안에서 작업 중일 땐 플리커 0.
        already_fg = False
        try:
            if prev_fg == hwnd:
                already_fg = True
            elif prev_fg:
                _, prev_pid = win32process.GetWindowThreadProcessId(prev_fg)
                if prev_pid and self._pid and int(prev_pid) == int(self._pid):
                    already_fg = True
        except Exception:
            pass

        if already_fg:
            # 활성화 없이 바로 스크린 캡처.
            # 주의: _hwnd_dpi_ctx 로 래핑하면 안 됨. 타겟이 DPI-unaware 일 때 스레드를
            # unaware 로 바꾸면 GetWindowRect/DWM/BitBlt 가 모두 논리 픽셀로 동작해서
            # 비트맵이 100% 크기로 잡힘. 반면 클릭 좌표 변환(ClientToScreen)은
            # 백엔드 기본 PMv2 라 물리 픽셀로 동작 → 단위 불일치로 클릭 어긋남.
            # 스크린 캡처는 백엔드 기본(PMv2) 컨텍스트에서 물리 픽셀로 통일.
            try:
                return self._capture_via_screen(hwnd)
            except Exception:
                return None

        # 1) 활성화 — Alt 키 self-inject + AttachThreadInput 트릭으로 foreground lock 우회.
        # Alt 탭은 최초 서버 시작 후 첫 호출에서 SetForegroundWindow 가 거부되는 문제 방지.
        self._unlock_foreground()
        try:
            cur_thread = win32api.GetCurrentThreadId()
            fg_thread = 0
            if prev_fg:
                try:
                    fg_thread, _ = win32process.GetWindowThreadProcessId(prev_fg)
                except Exception:
                    fg_thread = 0
            attached = False
            try:
                if fg_thread and fg_thread != cur_thread:
                    attached = bool(
                        windll.user32.AttachThreadInput(cur_thread, fg_thread, True)
                    )
                try:
                    win32gui.BringWindowToTop(hwnd)
                except Exception:
                    pass
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            finally:
                if attached:
                    try:
                        windll.user32.AttachThreadInput(cur_thread, fg_thread, False)
                    except Exception:
                        pass
        except Exception:
            pass

        # 2) DWM 컴포지션 + 렌더링 완료 대기 (너무 짧으면 이전 z-order 잔상 잡힘)
        time.sleep(0.08)

        # 3) 스크린 영역 BitBlt — DPI 컨텍스트 전환 없이 백엔드 기본(PMv2)에서.
        # _hwnd_dpi_ctx 래핑하면 타겟 DPI-unaware 시 비트맵이 논리 픽셀(100%) 로 잡혀
        # 클릭 좌표 변환과 단위가 어긋남.
        try:
            img = self._capture_via_screen(hwnd)
        except Exception:
            img = None

        # 4) 이전 포어그라운드 복원 — 사용자 앱(우리 frontend)에 포커스 돌려놓음.
        if prev_fg and prev_fg != hwnd:
            self._unlock_foreground()
            try:
                cur_thread = win32api.GetCurrentThreadId()
                target_thread, _ = win32process.GetWindowThreadProcessId(prev_fg)
                attached = False
                try:
                    if target_thread and target_thread != cur_thread:
                        attached = bool(
                            windll.user32.AttachThreadInput(cur_thread, target_thread, True)
                        )
                    win32gui.SetForegroundWindow(prev_fg)
                except Exception:
                    try:
                        windll.user32.SetForegroundWindow(prev_fg)
                    except Exception:
                        pass
                finally:
                    if attached:
                        try:
                            windll.user32.AttachThreadInput(cur_thread, target_thread, False)
                        except Exception:
                            pass
            except Exception:
                pass

        return img

    def _capture_with_flag(self, hwnd: int, flag: int) -> Optional[Image.Image]:
        """주어진 PrintWindow 플래그로 hwnd 를 캡처해 PIL Image 반환. 실패 시 None.

        flag 에 PW_CLIENTONLY(0x1) 가 빠져있으면 비트맵을 visible bounds(shadow 제외)
        크기로 잡아 타이틀바/보더 까지 포함한 풀 윈도우를 캡처한다 (그렇지 않으면
        GetClientRect). visible bounds 를 쓰는 이유: GetWindowRect 는 Win10/11 의
        invisible drop shadow 까지 포함 → PrintWindow 가 그리는 실제 영역(=shadow 제외)
        보다 비트맵이 커져서 우/하단에 빈 padding 이 생김.
        """
        is_client_only = bool(flag & 0x00000001)
        try:
            if is_client_only:
                rect = win32gui.GetClientRect(hwnd)
                w, h = rect[2] - rect[0], rect[3] - rect[1]
            else:
                vr = self._get_visible_window_rect(hwnd)
                if vr is None:
                    vr = win32gui.GetWindowRect(hwnd)
                w, h = vr[2] - vr[0], vr[3] - vr[1]
        except Exception:
            return None
        if w <= 0 or h <= 0:
            return None
        hwndDC = win32gui.GetWindowDC(hwnd)
        mfcDC = None
        saveDC = None
        saveBitMap = None
        try:
            mfcDC = win32ui.CreateDCFromHandle(hwndDC)
            saveDC = mfcDC.CreateCompatibleDC()
            saveBitMap = win32ui.CreateBitmap()
            saveBitMap.CreateCompatibleBitmap(mfcDC, w, h)
            saveDC.SelectObject(saveBitMap)
            ok = windll.user32.PrintWindow(hwnd, saveDC.GetSafeHdc(), flag)
            if not ok:
                return None
            bmpinfo = saveBitMap.GetInfo()
            bmpstr = saveBitMap.GetBitmapBits(True)
            return Image.frombuffer(
                "RGB",
                (bmpinfo["bmWidth"], bmpinfo["bmHeight"]),
                bmpstr, "raw", "BGRX", 0, 1,
            )
        except Exception:
            return None
        finally:
            if saveBitMap is not None:
                try:
                    win32gui.DeleteObject(saveBitMap.GetHandle())
                except Exception:
                    pass
            if saveDC is not None:
                try:
                    saveDC.DeleteDC()
                except Exception:
                    pass
            if mfcDC is not None:
                try:
                    mfcDC.DeleteDC()
                except Exception:
                    pass
            try:
                win32gui.ReleaseDC(hwnd, hwndDC)
            except Exception:
                pass

    @staticmethod
    def is_valid_window(hwnd: int) -> bool:
        """임의 hwnd 가 현재 유효한지 — compositor 의 hwnd 캐시 검증용.

        LinControlService.is_valid_window 와 인터페이스 동일 — cross-platform 호출처(compositor)에서
        OS 분기 없이 같은 메서드명 사용 가능.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return False
        try:
            return bool(win32gui.IsWindow(int(hwnd)))
        except Exception:
            return False

    @staticmethod
    def capture_hwnd_bgr(hwnd: int) -> "Optional['np.ndarray']":  # type: ignore[name-defined]
        """임의 hwnd를 BGR numpy 배열로 캡처 (UWP/WinUI3 자동 폴백 포함).

        CompositorService 등 attach 상태와 무관하게 여러 윈도우를 동시에 캡처해야 할 때 사용.
        실패 시 None 반환.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            return None
        try:
            import numpy as _np
        except Exception:
            return None
        try:
            if not win32gui.IsWindow(hwnd):
                return None
        except Exception:
            return None

        # WinControlService 인스턴스 메서드를 stateless 헬퍼로 재사용
        helper = WinControlService()
        # 자식 CoreWindow 탐지 (UWP)
        is_uwp, content_hwnd = helper._detect_uwp(hwnd)
        first_flag = 0x00000003 if is_uwp else 0x00000001
        img = helper._capture_with_flag(hwnd, first_flag)
        if helper._is_blank_image(img) and first_flag != 0x00000003:
            img = helper._capture_with_flag(hwnd, 0x00000003)
        if helper._is_blank_image(img) and content_hwnd:
            try:
                if win32gui.IsWindow(content_hwnd):
                    img = helper._capture_with_flag(content_hwnd, 0x00000003)
                    if helper._is_blank_image(img):
                        img = helper._capture_with_flag(content_hwnd, 0x00000001)
            except Exception:
                pass
        if img is None:
            return None
        # PIL RGB → BGR ndarray (cv2 호환)
        try:
            arr = _np.asarray(img)  # RGB
            if arr.ndim != 3 or arr.shape[2] < 3:
                return None
            # RGB → BGR (마지막 채널 순서만 뒤집음)
            return arr[:, :, ::-1].copy()
        except Exception:
            return None

    @staticmethod
    def _is_blank_image(img: Optional[Image.Image]) -> bool:
        """이미지가 사실상 단색(검정/흰색) 인지 — UWP 캡처 실패 감지."""
        if img is None:
            return True
        try:
            extrema = img.getextrema()
            # 각 채널의 (min,max) 가 거의 같으면 단색
            if not extrema:
                return True
            # RGB → tuple of 3 (min,max). RGBA 또는 단일채널 케이스도 안전 처리.
            if isinstance(extrema[0], tuple):
                for mn, mx in extrema:
                    if mx - mn > 8:  # 채널 변화 폭이 충분하면 정상
                        return False
                return True
            mn, mx = extrema
            return (mx - mn) <= 8
        except Exception:
            return False

    @contextlib.contextmanager
    def _hwnd_dpi_ctx(self, hwnd: int):
        """임의 hwnd 의 DPI awareness 로 스레드 컨텍스트를 일시 전환.

        _target_dpi_ctx 와 동일하지만 self._hwnd 대신 인자로 받은 hwnd 사용.
        capture_window_by_match 처럼 attached 와 무관하게 다른 hwnd 를 처리할 때 사용.
        """
        if not _WIN32_AVAILABLE or not hwnd:
            yield
            return
        user32 = windll.user32
        prev_ctx = None
        target_ctx = None
        try:
            target_ctx = user32.GetWindowDpiAwarenessContext(hwnd)
        except Exception:
            target_ctx = None
        if target_ctx:
            try:
                prev_ctx = user32.SetThreadDpiAwarenessContext(target_ctx)
            except Exception:
                prev_ctx = None
        try:
            yield
        finally:
            if prev_ctx:
                try:
                    user32.SetThreadDpiAwarenessContext(prev_ctx)
                except Exception:
                    pass

    def _capture_window_image_for(
        self, hwnd: int, is_uwp: bool, content_hwnd: Optional[int],
    ) -> Optional[Image.Image]:
        """capture_window 의 dispatch 로직을 임의 hwnd 에 적용.

        시도 순서:
          1) 일반 앱: 활성화 + 화면 영역 BitBlt (Alt+PrintScreen 등가) — DPI/배율 무관.
             이미 타겟이 포어그라운드면 활성화 스킵 (플리커 없음).
          2) 스크린 캡처 실패 시 PrintWindow PW_RENDERFULLCONTENT 폴백 — 가려진 윈도우 등.
          3) UWP 는 처음부터 PrintWindow 경로.
        반환된 이미지가 모달 미리보기(=capture_window) 와 동일 좌표/스케일을 가지도록 일관 유지.
        """
        img: Optional[Image.Image] = None
        used_path = None
        # 1순위: 활성화 + 스크린 캡처 (Alt+PrintScreen 등가).
        # DWM 합성된 최종 픽셀을 가져오므로 DPI/배율 무관하게 항상 정확.
        # 이미 타겟/같은 프로세스가 포어그라운드면 활성화 스킵 → 플리커 없음.
        if not is_uwp:
            img = self._capture_via_screen_activated(hwnd)
            if img is not None and not self._is_blank_image(img):
                used_path = "Screen+Activate"
        # 2순위: 스크린 캡처 실패 시 PrintWindow 폴백 — UWP 거나 윈도우가 완전 가려진 케이스.
        if img is None or self._is_blank_image(img):
            with self._hwnd_dpi_ctx(hwnd):
                img = self._capture_with_flag(hwnd, 0x00000002)
                if self._is_blank_image(img):
                    img = self._capture_with_flag(hwnd, 0x00000003)
                if self._is_blank_image(img) and content_hwnd:
                    try:
                        if win32gui.IsWindow(content_hwnd):
                            img = self._capture_with_flag(content_hwnd, 0x00000003)
                            if self._is_blank_image(img):
                                img = self._capture_with_flag(content_hwnd, 0x00000001)
                    except Exception:
                        pass
            used_path = used_path or "PrintWindow"

        return img

    def capture_window(self, fmt: str = "jpeg", render_full_content: bool = False) -> bytes:
        """대상 윈도우 캡처 (타이틀바 포함 풀 윈도우).

        시도 순서:
          1) 윈도우 자신의 DC 에서 BitBlt 직접 복사 (paint 메시지 무발신) — 깜박임/
             IME 포커스 풀림 없음. 일반 GDI 앱에서 안정적. occluded 여도 OK
             (윈도우 자신의 백버퍼에서 가져오므로 화면 위 다른 창 영향 없음).
          2) blank/단색이면 PrintWindow PW_RENDERFULLCONTENT|PW_CLIENTONLY(3) 폴백
             — DWM 컴포지션 앱(WPF 등) 또는 더블버퍼 안 쓰는 앱.
          3) UWP/WinUI3 는 처음부터 (3) + content_hwnd 폴백.
        """
        if not self.is_attached():
            raise RuntimeError("No window attached")
        img = self._capture_window_image_for(self._hwnd, self._is_uwp, self._content_hwnd)
        if img is None:
            raise RuntimeError("Capture failed (window-DC BitBlt + PrintWindow)")

        buf = io.BytesIO()
        if fmt.lower() == "png":
            img.save(buf, format="PNG")
        else:
            img.save(buf, format="JPEG", quality=70)
        return buf.getvalue()

    # ── 입력 ─────────────────────────────────────────────────────────
    # 입력 방식: SendInput 만 사용 — UWP/WinUI3/MMC/일반 Win32 앱 모두 호환.
    # 단점: 대상 윈도우에 포커스가 일시적으로 가져가짐 (SetForegroundWindow + 가상
    # 데스크탑 절대 좌표로 마우스 이동 후 클릭). 백그라운드 입력은 지원하지 않음.
    #
    # Watchdog: AttachThreadInput + SetForegroundWindow + OpenClipboard 등은 대상 앱의
    # 메시지 펌프가 막혀 있으면 영영 반환 안 함 (CAN/Serial 통신 중인 MFC 시뮬레이터 등).
    # native API 는 파이썬에서 cancel 불가 → 별도 데몬 스레드에 격리하고 호출자는
    # timeout 으로 빠져나와 워커 스레드를 풀에 돌려준다. hang 된 스레드는 leak 되지만
    # 백엔드 서비스는 살아남고 다음 요청을 받을 수 있다.
    def run_action_with_timeout(self, fn: "Callable[[], _T]", timeout_s: float = 20.0) -> "_T":
        """fn 을 데몬 스레드에서 실행, timeout 안에 끝나면 결과 반환.

        timeout 초과 시 TimeoutError raise. hang 된 스레드는 백그라운드에 남지만
        프로세스 관점에서는 leak 정도이고 새 요청 처리는 가능.

        fn 안에서 예외가 발생하면 그대로 재발생.
        """
        result: dict = {"value": None, "exc": None}
        done = threading.Event()

        def runner() -> None:
            try:
                result["value"] = fn()
            except BaseException as e:  # noqa: BLE001 — propagate everything
                result["exc"] = e
            finally:
                done.set()

        t = threading.Thread(target=runner, daemon=True, name="WinControlAction")
        t.start()
        if not done.wait(timeout_s):
            logger.error(
                "WinControl action timed out after %.1fs — target window message pump "
                "likely blocked. Leaking watchdog thread (hwnd=%s pid=%s name=%s).",
                timeout_s, self._hwnd, self._pid, self._process_name,
            )
            raise TimeoutError(
                f"WinControl action exceeded {timeout_s:.0f}s — "
                f"target window may be unresponsive"
            )
        if result["exc"] is not None:
            raise result["exc"]  # type: ignore[misc]
        return result["value"]  # type: ignore[return-value]

    def _check(self) -> int:
        if not self.is_attached():
            raise RuntimeError("No window attached")
        return self._hwnd  # type: ignore[return-value]

    @staticmethod
    def _unlock_foreground() -> None:
        """Windows SetForegroundWindow 락 우회 — 가상 Alt 키 down/up.

        백엔드 프로세스가 입력 이벤트 이력이 없으면 SetForegroundWindow 가 거부됨.
        ALTUP+ALTDOWN 을 self-inject 해서 OS 가 "최근 입력 이벤트 있음" 으로 판정하도록.
        부작용 최소화: down/up 매우 짧은 간격 + scan code 0 (메뉴 활성화 방지).
        최초 서버 시작 후 첫 임베드에서 포커스 전환이 실패하는 문제 해결용.
        """
        if not _WIN32_AVAILABLE:
            return
        try:
            # VK_MENU = 0x12, KEYEVENTF_KEYUP = 0x02
            windll.user32.keybd_event(0x12, 0, 0, 0)
            windll.user32.keybd_event(0x12, 0, 0x02, 0)
        except Exception:
            pass

    def _focus(self) -> None:
        """대상 윈도우를 전면으로 + 포커스. SendInput 모드 전제 조건.

        가드: 이미 타겟 프로세스(또는 그 자식 다이얼로그/팝업) 가 포어그라운드면
        SetForegroundWindow 를 다시 호출하지 않음. 이 호출이 자식 다이얼로그의 포커스
        상태(예: 사용자가 방금 클릭한 에디트박스) 를 리셋시켜 텍스트 입력이 빈 곳으로
        흘러가는 문제 회피. SendInput 은 어차피 포어그라운드의 포커스 컨트롤에 가니
        같은 프로세스가 이미 활성이면 그대로 둠.
        """
        hwnd = self._hwnd
        if not hwnd:
            return
        try:
            # 이미 같은 프로세스가 포어그라운드면 — 자식 다이얼로그에 사용자가 준 포커스
            # 를 보존해야 하므로 그대로 둔다. (단 최소화 상태면 복원은 필요)
            try:
                fg = windll.user32.GetForegroundWindow()
                if fg and not win32gui.IsIconic(hwnd):
                    _, fg_pid = win32process.GetWindowThreadProcessId(fg)
                    same_pids = {int(self._pid)} if self._pid else set()
                    # UWP: 호스트(ApplicationFrameHost)와 콘텐츠 앱 프로세스가 다름 —
                    # 콘텐츠 pid 도 "같은 앱" 으로 취급해야 플라이아웃/콤보 팝업이 열린
                    # 상태에서 불필요한 재활성화(=팝업 닫힘)를 피한다.
                    if self._content_hwnd:
                        try:
                            _, cpid = win32process.GetWindowThreadProcessId(self._content_hwnd)
                            if cpid:
                                same_pids.add(int(cpid))
                        except Exception:
                            pass
                    if fg_pid and int(fg_pid) in same_pids:
                        # 같은 프로세스가 이미 활성 — 추가 포커스 조작 없이 짧은 안정화
                        # 대기만 (입력 큐 처리 시간).
                        time.sleep(0.05)
                        return
            except Exception:
                pass
            # 최소화 상태면 복원 — 복원 직후엔 페인팅 시간이 필요하므로 약간 더 대기.
            was_iconic = False
            try:
                if win32gui.IsIconic(hwnd):
                    win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                    was_iconic = True
            except Exception:
                pass
            # 포어그라운드 락 우회 — 가상 Alt 키 탭 (최초 서버 시작 후 첫 호출 실패 방지)
            self._unlock_foreground()
            # 포어그라운드 락 회피 — AttachThreadInput 트릭
            try:
                fg = windll.user32.GetForegroundWindow()
                cur_thread = win32api.GetCurrentThreadId()
                fg_thread, _ = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
                if fg_thread and fg_thread != cur_thread:
                    windll.user32.AttachThreadInput(cur_thread, fg_thread, True)
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                        # BringWindowToTop / SetFocus 보강 — SetForegroundWindow 단독으로
                        # 거부되는 경우(다른 스레드가 포어그라운드 락 보유)에 대비.
                        try:
                            win32gui.BringWindowToTop(hwnd)
                            win32gui.SetFocus(hwnd)
                        except Exception:
                            pass
                    finally:
                        windll.user32.AttachThreadInput(cur_thread, fg_thread, False)
                else:
                    win32gui.SetForegroundWindow(hwnd)
                    try:
                        win32gui.SetFocus(hwnd)
                    except Exception:
                        pass
            except Exception:
                try:
                    win32gui.SetForegroundWindow(hwnd)
                except Exception:
                    pass
            # 안정화 대기 — 포커스 전환 + 메시지 큐 처리 + 타겟 앱이 입력 받을 준비까지.
            # 너무 짧으면 첫 클릭이 paint/init 중인 윈도우에 흡수돼 무시됨.
            # 최소화에서 복원했으면 페인팅까지 추가 대기.
            time.sleep(0.50 if was_iconic else 0.30)
        except Exception as e:
            logger.debug("WinControl focus failed: %s", e)

    # ── 액션 전후 컨텍스트(이전 활성 창 + 마우스 위치) 보존 ──────────
    def _save_context(self) -> dict:
        """현재 포어그라운드 hwnd + 마우스 커서 위치 캡처. 액션 후 복원에 사용."""
        ctx: dict = {"prev_fg": None, "cursor": None}
        try:
            fg = windll.user32.GetForegroundWindow()
            if fg and fg != self._hwnd and win32gui.IsWindow(fg):
                ctx["prev_fg"] = int(fg)
        except Exception:
            pass
        try:
            ctx["cursor"] = win32api.GetCursorPos()
        except Exception:
            pass
        return ctx

    def _restore_context(self, ctx: dict) -> None:
        """액션 후 이전 활성 창(z-order) + 마우스 커서 위치 복원.

        deferred_restore 컨텍스트 안이면 즉시 복원 안 하고 큐에 적재 — 컨텍스트 종료 시
        일괄 복원. 액션 후 캡처를 한 사이클 안에서 처리하기 위함 (이중 활성화 방지).
        """
        if not ctx:
            return
        # 현재 스레드가 deferred_restore 블록 안이면 큐에 적재 (스레드 로컬 —
        # 다른 요청/leaked 스레드의 defer 상태와 격리).
        loc = getattr(self, "_defer_local", None)
        if loc is not None and getattr(loc, "depth", 0) > 0:
            loc.ctxs.append(ctx)
            return
        self._do_restore_context(ctx)

    def _do_restore_context(self, ctx: dict) -> None:
        """실제 포어그라운드 + 커서 복원 동작."""
        if not ctx:
            return
        # 1) 이전 활성 창으로 포커스 복귀 (AttachThreadInput 트릭 + Alt unlock)
        prev_fg = ctx.get("prev_fg")
        if prev_fg:
            self._unlock_foreground()
            try:
                if win32gui.IsWindow(prev_fg):
                    cur_thread = win32api.GetCurrentThreadId()
                    target_thread, _ = win32process.GetWindowThreadProcessId(prev_fg)
                    attached = False
                    try:
                        if target_thread and target_thread != cur_thread:
                            attached = bool(
                                windll.user32.AttachThreadInput(cur_thread, target_thread, True)
                            )
                        win32gui.SetForegroundWindow(prev_fg)
                    except Exception:
                        try:
                            windll.user32.SetForegroundWindow(prev_fg)
                        except Exception:
                            pass
                    finally:
                        if attached:
                            try:
                                windll.user32.AttachThreadInput(cur_thread, target_thread, False)
                            except Exception:
                                pass
            except Exception as e:
                logger.debug("WinControl FG restore failed: %s", e)
        # 2) 마우스 커서 원위치 — SetCursorPos 직접 호출 (mouse_event 보다 깔끔)
        cursor = ctx.get("cursor")
        if cursor:
            try:
                win32api.SetCursorPos((int(cursor[0]), int(cursor[1])))
            except Exception as e:
                logger.debug("WinControl cursor restore failed: %s", e)

    @contextlib.contextmanager
    def deferred_restore(self):
        """블록 동안 _restore_context 호출을 모두 지연 → 블록 종료 시 일괄 복원.

        사용: 액션 후 캡처를 한 활성화 사이클 안에서 처리.
          with wc.deferred_restore():
              wc.send_tap(x, y)        # 액션이 _focus 로 타겟 활성화, restore 는 지연
              time.sleep(1)            # UI 반영 대기 (타겟 계속 FG)
              img = wc._capture_via_screen(wc._hwnd)  # 타겟 FG 상태 그대로 캡처
          # 블록 종료 — prev_fg(우리 앱) 로 복원 (1회만)
        """
        # 재진입 안전 — send_click_sequence(내부 with) 가 라우터의 캡처 사이클(외부 with)
        # 안에서 실행될 때, 내부 종료 시점에 복원해버리면 캡처 전에 포커스가 빠져
        # 가려진 화면이 잡힌다. depth 를 세서 최외곽 종료 시에만 1회 복원.
        #
        # 스레드 로컬 필수 — 액션마다 watchdog 데몬 스레드가 따로 돌므로, 타임아웃으로
        # leak 된 스레드(여전히 with 안)나 동시 요청이 인스턴스 공유 카운터를 오염시키면
        # 이후 모든 복원이 스킵돼 마우스/포커스가 영영 안 돌아온다.
        loc = self._defer_local
        loc.depth = getattr(loc, "depth", 0) + 1
        if loc.depth == 1:
            loc.ctxs = []
        try:
            yield
        finally:
            loc.depth -= 1
            if loc.depth == 0:
                # 모든 지연된 ctx 복원 — LIFO 가 아니라 첫 번째(=최외곽) ctx 만 의미가 있음.
                # 중첩된 액션이라면 결국 사용자 앱(맨 처음 fg)으로 돌아가야 하므로 첫 번째 사용.
                ctxs = loc.ctxs
                loc.ctxs = []
                if ctxs:
                    # 가장 처음 저장된 ctx 가 진짜 prev_fg(우리 frontend), 나머지는 타겟 자신.
                    self._do_restore_context(ctxs[0])

    def _client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """client 좌표 → screen 좌표.

        입력(프론트가 보낸 client 좌표)을 scale 로 나눠서 logical 좌표계로 변환 후
        GetWindowRect(논리) + ClientToScreen(논리) 기준으로 screen 좌표 계산.
        SYSTEM_AWARE 타겟이 자신의 논리 좌표계로 클릭을 처리하도록 맞춤.
        """
        hwnd = self._hwnd
        if not hwnd:
            return (int(x), int(y))
        try:
            vr = self._get_visible_window_rect(hwnd)
            if vr is None:
                vr = win32gui.GetWindowRect(hwnd)
            wr = win32gui.GetWindowRect(hwnd)
            cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
            log_off_x = cx - wr[0]
            log_off_y = cy - wr[1]
            log_w = max(1, wr[2] - wr[0])
            log_h = max(1, wr[3] - wr[1])
            phys_w = vr[2] - vr[0]
            phys_h = vr[3] - vr[1]
            scale_x = phys_w / log_w
            scale_y = phys_h / log_h
            # 입력을 scale 로 나눠서 logical 좌표로 변환
            x_log = int(x) / scale_x
            y_log = int(y) / scale_y
            # GetWindowRect(논리) + 논리 client offset + 변환된 좌표 = 논리 screen 좌표
            sx = wr[0] + log_off_x + x_log
            sy = wr[1] + log_off_y + y_log
            return (int(round(sx)), int(round(sy)))
        except Exception:
            try:
                return win32gui.ClientToScreen(hwnd, (int(x), int(y)))
            except Exception:
                return (int(x), int(y))

    def _send_input_mouse_move(self, screen_x: int, screen_y: int) -> None:
        """마우스 커서를 물리 픽셀 screen 좌표로 직접 이동.

        기존 SendInput MOUSEEVENTF_ABSOLUTE+VIRTUALDESK 방식은 GetSystemMetrics 가
        system-aware (system DPI 기준 논리값) 라 PMv2 스레드에서 물리 픽셀 좌표를
        넘겨도 정규화 비율이 어긋남 (예: system_dpi=96 인데 모니터 125% 면 SM_CXVIRTUALSCREEN
        이 논리값을 반환 → 비율 1/1.25 어긋남).
        SetCursorPos 는 PMv2 스레드에서 물리 픽셀 좌표를 직접 받음 → DPI 가상화 영향 없음.
        """
        try:
            windll.user32.SetCursorPos(int(screen_x), int(screen_y))
        except Exception as e:
            logger.debug("SetCursorPos failed (%s), fallback to mouse_event ABSOLUTE", e)
            # 폴백: 기존 VIRTUALDESK 방식 — 단일 모니터/system_dpi 일치 환경에선 정상 동작
            try:
                vx = windll.user32.GetSystemMetrics(76)
                vy = windll.user32.GetSystemMetrics(77)
                vw = windll.user32.GetSystemMetrics(78) or 1
                vh = windll.user32.GetSystemMetrics(79) or 1
                nx = int(((int(screen_x) - vx) * 65535) / vw)
                ny = int(((int(screen_y) - vy) * 65535) / vh)
                win32api.mouse_event(0x0001 | 0x8000 | 0x4000, nx, ny, 0, 0)
            except Exception:
                pass

    def _send_input_button(self, button: str, down: bool) -> None:
        # MOUSEEVENTF_LEFTDOWN=0x0002, LEFTUP=0x0004, RIGHTDOWN=0x0008, RIGHTUP=0x0010,
        # MIDDLEDOWN=0x0020, MIDDLEUP=0x0040
        if button == "right":
            flag = 0x0008 if down else 0x0010
        elif button == "middle":
            flag = 0x0020 if down else 0x0040
        else:
            flag = 0x0002 if down else 0x0004
        win32api.mouse_event(flag, 0, 0, 0, 0)

    # ── tap/click (FG = SendInput) ──────────────────────────────
    # 모든 send_* 는 액션 전 컨텍스트(이전 활성 창 + 마우스 위치)를 저장하고
    # finally 에서 복원 — 사용자가 작업 중이던 다른 창과 커서 위치를 방해하지 않는다.
    def send_tap(self, x: int, y: int, button: str = "left") -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._send_input_mouse_move(sx, sy)
            # 마우스 이동 후 hover 인식 시간 — UWP/WinUI 컨트롤은 mousemove 처리 후
            # 클릭을 받아야 정상 동작. 더 안정적인 인식을 위해 시간 확대.
            time.sleep(0.10)
            self._send_input_button(button, True)
            time.sleep(0.08)
            self._send_input_button(button, False)
            # OS 가 클릭을 처리할 시간 — 다음 액션(또는 컨텍스트 복원) 전 대기.
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    def send_double_click(self, x: int, y: int) -> None:
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._send_input_mouse_move(sx, sy)
            time.sleep(0.10)
            for _ in range(2):
                self._send_input_button("left", True)
                time.sleep(0.06)
                self._send_input_button("left", False)
                time.sleep(0.06)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    def send_click_sequence(self, points: list[dict], interval_ms: int = 150,
                            button: str = "left") -> None:
        """여러 위치를 포커스 유지한 채 순서대로 클릭 (원자 실행 — 재생/라이브 공용).

        points: [{"x": .., "y": ..}, ...]. 드롭다운 열기 → 항목 선택처럼 첫 클릭으로
        뜬 일시 팝업이 다음 클릭 전에 닫히면 안 되는 경우 사용. 한 호출(=한 요청) 안에서
        전체를 실행하므로 클릭 사이에 사용자 브라우저가 포커스를 가져갈 틈이 없다 —
        타겟 비활성화 시 팝업이 자동으로 닫히는 OS 동작을 회피하는 유일한 방법.
        deferred_restore 로 시퀀스 전체 동안 포어그라운드 복원을 미루고(중첩 안전),
        각 send_tap 의 _focus 가드(같은 프로세스가 이미 FG면 재포커스 안 함)가
        팝업의 포커스를 보존한다.
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
        """버튼을 누른 채로 duration_ms 만큼 유지 후 떼기.

        button: "left"(기본) / "right" / "middle".
        예) 우클릭 길게 = right 메뉴 트리거 (대부분의 앱은 mouse-up 시 컨텍스트 메뉴).
        """
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            sx, sy = self._client_to_screen(int(x), int(y))
            self._send_input_mouse_move(sx, sy)
            time.sleep(0.10)
            self._send_input_button(button, True)
            try:
                time.sleep(max(0.0, duration_ms / 1000.0))
            finally:
                self._send_input_button(button, False)
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
            self._send_input_mouse_move(sx1, sy1)
            time.sleep(0.10)
            self._send_input_button("left", True)
            for i in range(1, steps):
                t = i / steps
                x = int(x1 + (x2 - x1) * t)
                y = int(y1 + (y2 - y1) * t)
                sx, sy = self._client_to_screen(x, y)
                self._send_input_mouse_move(sx, sy)
                if delay > 0:
                    time.sleep(delay)
            sx2, sy2 = self._client_to_screen(int(x2), int(y2))
            self._send_input_mouse_move(sx2, sy2)
            time.sleep(0.08)
            self._send_input_button("left", False)
            time.sleep(0.12)
        finally:
            self._restore_context(ctx)

    @staticmethod
    def _send_input_keybd(wVk: int, wScan: int, flags: int) -> bool:
        """SendInput 으로 키보드 이벤트 1개 전송. True=성공.

        legacy keybd_event 는 KEYEVENTF_UNICODE 를 제대로 처리 못 함 → SendInput 필수.
        """
        inp = _INPUT()
        inp.type = INPUT_KEYBOARD
        inp.ki = _KEYBDINPUT(int(wVk), int(wScan), int(flags), 0, 0)
        n = windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))
        return n == 1

    @classmethod
    def _send_unicode_char(cls, ch: str) -> None:
        """Unicode 문자 1개 입력 (down + up). SendInput + KEYEVENTF_UNICODE.

        IME 가 없는 ASCII/숫자/기호도 안전하게 동작 — wScan 자리에 Unicode codepoint
        를 넣으면 OS 가 해당 문자를 직접 입력 큐에 넣음.
        """
        code = ord(ch)
        cls._send_input_keybd(0, code, KEYEVENTF_UNICODE)
        time.sleep(0.005)
        cls._send_input_keybd(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)

    @classmethod
    def _send_vk(cls, vk: int, hold_s: float = 0.0) -> None:
        """가상키 down/up. 일반 가상키(VK_*) — wVk 사용, KEYEVENTF_UNICODE 안 씀."""
        cls._send_input_keybd(vk, 0, 0)
        if hold_s > 0:
            time.sleep(hold_s)
        cls._send_input_keybd(vk, 0, KEYEVENTF_KEYUP)

    def send_text(
        self,
        text: str,
        click_first_x: Optional[int] = None,
        click_first_y: Optional[int] = None,
        press_enter: bool = False,
    ) -> None:
        """텍스트 입력 — KEYEVENTF_UNICODE 직접 키 인젝션.

        클립보드 + Ctrl+V 방식은 일부 레거시 Win32 앱(MFC 기반 CAN/Serial 시뮬레이터 등)
        에서 OpenClipboard/SetClipboardData 가 OS 락에 묶여 영영 안 끝나는 문제가 있어
        제거. SendInput + KEYEVENTF_UNICODE 는 키 이벤트를 비동기로 입력 큐에 넣고
        즉시 반환하므로 hang 위험이 없다. 한글/이모지도 wScan 자리에 codepoint 를
        그대로 넣으면 OS 가 처리.

        줄바꿈(\\r\\n, \\n, \\r) 은 Enter 키로 분리해서 처리 — single-line 에디트박스가
        줄바꿈을 잘라먹거나 form submit 으로 동작하는 케이스 방어.

        click_first_x/y 가 지정되면 텍스트 입력 전 그 client 좌표 클릭으로 입력 컨트롤
        포커스 부여. 분리된 win_tap → win_input_text 두 호출로 처리하면 사이의 fg 복원
        때문에 자식 다이얼로그의 포커스가 풀리는 문제가 있어 atomic 으로 합침.

        press_enter 가 True 면 텍스트 전송 후 Enter 키를 한 번 더 눌러줌 (검색창 제출 등).
        분리된 win_key 호출은 사이의 fg 복원으로 포커스가 풀리므로 같은 컨텍스트에 포함.
        """
        self._check()
        ctx = self._save_context()
        try:
            self._focus()
            # 1) 클릭으로 입력 컨트롤 포커스 — 같은 컨텍스트 안에서 해야 fg 안 풀림.
            if click_first_x is not None and click_first_y is not None:
                sx, sy = self._client_to_screen(int(click_first_x), int(click_first_y))
                self._send_input_mouse_move(sx, sy)
                time.sleep(0.10)
                self._send_input_button("left", True)
                time.sleep(0.08)
                self._send_input_button("left", False)
                # 클릭 → 캐럿 안착 + 입력 큐 처리 시간.
                time.sleep(0.20)
            # 2) 텍스트 전송 — 줄 단위로 분리 후 unicode 인젝션, 줄 사이는 Enter.
            if text:
                normalized = text.replace("\r\n", "\n").replace("\r", "\n")
                lines = normalized.split("\n")
                for i, line in enumerate(lines):
                    if i > 0:
                        self._send_vk(win32con.VK_RETURN)
                        time.sleep(0.05)
                    for ch in line:
                        self._send_unicode_char(ch)
                        time.sleep(0.010)
            # 3) press_enter — 텍스트 입력 후 Enter 제출 (같은 컨텍스트 안에서 포커스 유지).
            if press_enter:
                time.sleep(0.05)
                self._send_vk(win32con.VK_RETURN)
            time.sleep(0.10)
        finally:
            self._restore_context(ctx)

    def send_key(self, key: str) -> None:
        """가상키 한 번 누르고 떼기 (modifier 미지원 — 단일 키만). SendInput 사용."""
        self._check()
        vk = _resolve_vk(key)
        ctx = self._save_context()
        try:
            self._focus()
            self._send_vk(vk, hold_s=0.05)
            time.sleep(0.10)
        finally:
            self._restore_context(ctx)

    # Modifier 별칭 → 가상키 코드. 소문자 비교용.
    _MODIFIER_VK = {
        "ctrl": 0x11, "control": 0x11,         # VK_CONTROL
        "alt": 0x12, "menu": 0x12,             # VK_MENU
        "shift": 0x10,                          # VK_SHIFT
        "win": 0x5B, "lwin": 0x5B,             # VK_LWIN
        "super": 0x5B, "cmd": 0x5B, "meta": 0x5B,
        "rwin": 0x5C,                           # VK_RWIN
    }

    def send_key_combo(self, keys: list[str],
                       click_first_x: Optional[int] = None,
                       click_first_y: Optional[int] = None) -> None:
        """Modifier + key 조합 전송. 예: ['ctrl','a'], ['ctrl','shift','f5'], ['alt','f4'].

        시퀀스:
          1) 수정자 키 down (입력 순서대로)
          2) 비-수정자 키 각각 down → 짧은 hold → up (입력 순서대로)
          3) 수정자 키 up (역순 — Windows 표준)

        문자열로도 받을 수 있게 라우터에서 '+' 또는 ',' 분리 후 호출.
        파싱은 호출자 책임 (라우터 레이어에서). 빈 리스트는 noop.

        click_first_x/y 가 지정되면 단축키 전송 전 그 client 좌표를 먼저 클릭해
        대상 컨트롤에 포커스를 부여 (send_text 와 동일 패턴). 분리된 win_tap →
        win_key_combo 두 호출은 사이의 fg 복원 때문에 포커스가 풀리므로 atomic 으로 합침.
        """
        self.send_key_combos([keys] if keys else [], click_first_x, click_first_y)

    def send_key_combos(self, combos: list[list[str]],
                        click_first_x: Optional[int] = None,
                        click_first_y: Optional[int] = None) -> None:
        """여러 조합을 한 컨텍스트(포커스 유지) 안에서 순서대로 전송.

        'Ctrl+A → BackSpace' 처럼 연속 조합을 요청 2개로 분리하면 사이의 fg 복원 후
        재활성화 시 _focus 의 SetFocus(메인창)가 에디트 컨트롤 포커스를 빼앗아 두 번째
        조합이 허공에 떨어진다. 클릭(포커스 부여) + 조합 전체를 atomic 으로 묶는다.
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
                self._send_input_mouse_move(sx, sy)
                time.sleep(0.10)
                self._send_input_button("left", True)
                time.sleep(0.08)
                self._send_input_button("left", False)
                # 클릭 → 포커스 안착 + 입력 큐 처리 시간.
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

        시퀀스: 수정자 down (순서대로) → 일반 키 down/up → 수정자 up (역순).
        예외로 중간에 빠져나가도 finally 에서 수정자 강제 해제 (stuck key 방지).
        """
        modifiers: list[int] = []
        regulars: list[int] = []
        for k in keys or []:
            key = str(k).strip()
            if not key:
                continue
            mvk = self._MODIFIER_VK.get(key.lower())
            if mvk is not None:
                modifiers.append(mvk)
            else:
                regulars.append(_resolve_vk(key))
        if not regulars and not modifiers:
            return
        try:
            # 1) 모든 수정자 down
            for vk in modifiers:
                self._send_input_keybd(vk, 0, 0)
                time.sleep(0.02)
            # 2) 일반 키 down/up — modifier-only 조합도 허용 (regulars 비었으면 스킵).
            for vk in regulars:
                self._send_input_keybd(vk, 0, 0)
                time.sleep(0.03)
                self._send_input_keybd(vk, 0, KEYEVENTF_KEYUP)
                time.sleep(0.02)
        finally:
            # 3) 수정자 up (역순) — 정상/예외 경로 모두 해제.
            for vk in reversed(modifiers):
                try:
                    self._send_input_keybd(vk, 0, KEYEVENTF_KEYUP)
                except Exception:
                    pass
                time.sleep(0.02)
