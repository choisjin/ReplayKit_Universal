"""pymobiledevice3 CLI wrapper. Python 3.10+."""
from __future__ import annotations
import os, re, time, tempfile, subprocess, threading, shutil, sys, json, asyncio, logging
from typing import Any, Optional, List
from contextlib import suppress

logger = logging.getLogger(__name__)

try:
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.remote.core_device.hid_service import (
        IndigoHIDService, UniversalHIDServiceService, touch_session,
        TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE,
        HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP, HID_BUTTON_STATE_CANCELED,
    )
    HAS_PMD_HID_API = True
except Exception as _hid_api_err:
    HAS_PMD_HID_API = False
    _hid_api_exc = _hid_api_err

# DIGITIZER_SURFACE_MAIN_TOUCHSCREEN 는 pymobiledevice3 버전에 따라
# 존재하지 않을 수 있다. 없으면 기본값 0 (메인 터치스크린 표면) 을 사용한다.
# HID API 전체가 로드되지 않더라도 아래 함수 정의(기본 인자)에서 참조되므로
# 별도 try/except 로 최소한 NameError 는 막는다.
try:
    from pymobiledevice3.remote.core_device.hid_service import DIGITIZER_SURFACE_MAIN_TOUCHSCREEN
except Exception:
    DIGITIZER_SURFACE_MAIN_TOUCHSCREEN = 0

try:
    # iOS 17+ RSD 터널에서는 com.apple.mobile.screenshotr(lockdown) 서비스가
    # 노출되지 않는 경우가 많다. 대신 DVT/Instruments screenshot 채널
    # (com.apple.instruments.server.services.screenshot)을 사용한다.
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.screenshot import Screenshot
    HAS_PMD_SCREEN_API = True
except Exception as _screen_api_err:
    HAS_PMD_SCREEN_API = False
    _screen_api_exc = _screen_api_err


# iPhone ProductType -> (screen_width, screen_height) in pixels.
# These are the native pixel resolutions for each model family.
PRODUCT_TYPE_TO_RESOLUTION = {
    # iPhone 12 / 12 Pro / 13 / 13 Pro / 14
    "iPhone13,1": (1170, 2532),  # iPhone 12 mini
    "iPhone13,2": (1170, 2532),  # iPhone 12
    "iPhone13,3": (1170, 2532),  # iPhone 12 Pro
    "iPhone13,4": (1284, 2778),  # iPhone 12 Pro Max
    "iPhone14,4": (1080, 2340),  # iPhone 13 mini
    "iPhone14,5": (1170, 2532),  # iPhone 13
    "iPhone14,2": (1170, 2532),  # iPhone 13 Pro
    "iPhone14,3": (1284, 2778),  # iPhone 13 Pro Max
    "iPhone14,7": (1170, 2532),  # iPhone 14
    "iPhone14,8": (1170, 2532),  # iPhone 14 Plus
    "iPhone15,2": (1179, 2556),  # iPhone 14 Pro
    "iPhone15,3": (1290, 2796),  # iPhone 14 Pro Max
    "iPhone15,4": (1179, 2556),  # iPhone 15
    "iPhone15,5": (1290, 2796),  # iPhone 15 Plus
    "iPhone16,1": (1179, 2556),  # iPhone 15 Pro
    "iPhone16,2": (1290, 2796),  # iPhone 15 Pro Max
    "iPhone17,1": (1206, 2622),  # iPhone 16 Pro
    "iPhone17,2": (1320, 2868),  # iPhone 16 Pro Max
    "iPhone17,3": (1179, 2556),  # iPhone 16
    "iPhone17,4": (1290, 2796),  # iPhone 16 Plus
    "iPhone17,5": (1206, 2622),  # iPhone 16e
    "iPhone18,1": (1206, 2622),  # iPhone 17 Pro
    "iPhone18,2": (1320, 2868),  # iPhone 17 Pro Max
    "iPhone18,3": (1179, 2556),  # iPhone 17
    "iPhone18,4": (1290, 2796),  # iPhone 17 Air
}


def _get_pymd_version() -> str:
    try:
        import importlib.metadata as m
        return m.version('pymobiledevice3')
    except Exception as e:
        try:
            import pymobiledevice3
            return getattr(pymobiledevice3, '__version__', f'not installed ({type(e).__name__}: {e})')
        except Exception as e2:
            return f'not installed ({type(e2).__name__}: {e2})'


def _is_admin() -> bool:
    """Windows 관리자 권한 여부를 확인한다."""
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# Linux 에는 CREATE_NO_WINDOW 가 없다 — 0 이면 플래그 없음과 동일.
_NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def _tunnel_log_path() -> str:
    """터널 디버그 로그 경로 — 배포본은 패키지 폴더가 읽기전용일 수 있어 logs/ 에 둔다."""
    root = os.environ.get('RECORDING_PROJECT_ROOT') or os.path.abspath(
        os.path.join(os.path.dirname(__file__), '..', '..', '..'))
    log_dir = os.path.join(root, 'logs')
    with suppress(Exception):
        os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, 'iphone_tunnel_debug.log')


def _log_tunnel_line(line: str) -> None:
    """터널 프로세스의 출력을 logs/iphone_tunnel_debug.log 에 누적한다."""
    try:
        log_path = _tunnel_log_path()
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        with open(log_path, 'a', encoding='utf-8', errors='replace') as f:
            f.write(f'{ts} {line}')
    except Exception:
        pass


PYMD_VERSION = _get_pymd_version()
# server.py를 실행한 인터프리터와 동일한 python을 써야 pymobiledevice3 버전이 맞춰진다.
# PATH의 'python'이 다른 버전을 가리킬 수 있으므로 sys.executable을 기본으로 사용하고,
# 필요하면 PYMD_PYTHON 환경변수로 강제할 수 있다.
PYTHON = os.environ.get('PYMD_PYTHON') or sys.executable

PYMD_TUNNEL_ENV = os.environ.get('PYMOBILEDEVICE3_TUNNEL')
if PYMD_TUNNEL_ENV is not None:
    PYMD_TUNNEL_ENV = PYMD_TUNNEL_ENV.strip()


class PymobileDevice3Controller:
    def __init__(self, udid: str = "") -> None:
        # 대상 iPhone UDID — 여러 대 연결 시 usbmux 첫 기기가 아닌 이 기기를 제어한다.
        # 비어 있으면 usbmux 첫 기기(단일 연결 가정).
        self.udid = (udid or "").strip()
        self._tunnel_proc: Optional[subprocess.Popen] = None
        self._rsd_host: Optional[str] = None
        self._rsd_port: Optional[int] = None
        self._rsd_source: Optional[str] = None
        self._lock = threading.RLock()
        self._hid_lock = threading.Lock()
        # remote tunneld (장기 실행 터널 데몬) 관리
        self._tunneld_proc: Optional[subprocess.Popen] = None
        self._tunneld_url: Optional[str] = None
        self._tunneld_lock = threading.RLock()
        # iOS 27+: 하나의 RSD 터널 위에 HID touch/button 세션을 유지해
        # universal-hid-service CLI의 반복적인 DisplayService handshake를 피한다.
        self.hid_session = HidSessionManager(self) if HAS_PMD_HID_API else None
        # 라이브 프리뷰용: DVT/Instruments screenshot 채널로 주기적으로 최신 화면 캡처
        self.screen_stream = ScreenshotStreamManager(self)
        self._load_env_rsd()

    def _load_env_rsd(self) -> None:
        if PYMD_TUNNEL_ENV is not None:
            self._rsd_source = 'tunnel'
            return
        host = os.environ.get('PYMD_RSD_HOST')
        port = os.environ.get('PYMD_RSD_PORT')
        if host and port:
            try:
                host = host.strip().strip('"').strip("'")
                # 환경변수에 --rsd 가 붙어 들어온 경우를 정제
                if host.lower().startswith('--rsd'):
                    host = host[5:].strip()
                self._rsd_host = host
                self._rsd_port = int(port)
                self._rsd_source = 'env'
            except ValueError:
                pass

    @property
    def pymd_version(self) -> str:
        return PYMD_VERSION

    @property
    def tunnel_info(self) -> dict:
        with self._lock:
            tunneld_alive = bool(self._tunneld_proc and self._tunneld_proc.poll() is None)
            if self._rsd_source == 'tunnel':
                return {
                    'host': None,
                    'port': None,
                    'tunnel': PYMD_TUNNEL_ENV,
                    'source': 'tunnel',
                    'connected': True,
                    'pid': None,
                    'tunneld_url': self._tunneld_url if not PYMD_TUNNEL_ENV else None,
                    'tunneld_alive': tunneld_alive,
                }
            alive = False
            if self._rsd_source in ('env', 'manual'):
                alive = bool(self._rsd_host and self._rsd_port)
            elif self._tunnel_proc:
                alive = self._tunnel_proc.poll() is None
            return {
                'host': self._rsd_host,
                'port': self._rsd_port,
                'source': self._rsd_source,
                'connected': bool(self._rsd_host and self._rsd_port and alive),
                'pid': self._tunnel_proc.pid if self._tunnel_proc else None,
                'tunneld_url': self._tunneld_url,
                'tunneld_alive': tunneld_alive,
            }

    def set_rsd(self, host: str, port: int) -> dict:
        with self._lock:
            self.stop_tunnel()
            self._rsd_host = host.strip().strip('"').strip("'")
            self._rsd_port = int(port)
            self._rsd_source = 'manual'
        return {'success': True, 'rsd': f'{self._rsd_host}:{self._rsd_port}', 'source': 'manual'}

    def start_tunnel(self, timeout: int = 30) -> tuple[str, int]:
        with self._lock:
            if self._rsd_host and self._rsd_port:
                return self._rsd_host, self._rsd_port
            self.stop_tunnel()
            modes = [
                ('kernel', []),
                ('userspace', ['--userspace']),
            ]
            last_err = ''
            for mode, extra in modes:
                cmd = [PYTHON, '-m', 'pymobiledevice3', 'lockdown', 'start-tunnel', '--script-mode'] + extra
                if self.udid:
                    cmd += ['--udid', self.udid]
                self._tunnel_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                    creationflags=_NO_WINDOW)
                try:
                    host, port = self._parse_tunnel_output(timeout, mode=mode, cmd=cmd)
                    self._rsd_host, self._rsd_port = host, port
                    self._rsd_source = 'auto'
                    return host, port
                except RuntimeError as e:
                    last_err = str(e)
                    self.stop_tunnel()
            admin_status = 'admin' if _is_admin() else 'NOT_ADMIN'
            raise RuntimeError(
                f'RSD 터널 시작 실패 (kernel/userspace 모두 실패, user={admin_status}). '
                f'Windows에서는 kernel 모드 터널이 관리자 권한을 필요로 합니다. {last_err}'
            )

    def _parse_tunnel_output(self, timeout: int, mode: str = 'unknown', cmd: Optional[list[str]] = None) -> tuple[str, int]:
        if not self._tunnel_proc or not self._tunnel_proc.stdout:
            raise RuntimeError(f'[{mode}] 터널 프로세스 시작 실패')
        deadline = time.time() + timeout
        patterns = [
            re.compile(r"--rsd\s+(\S+)\s+(\d+)\b"),
            re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+(\d+)"),
            re.compile(r"([0-9a-fA-F:]+:[0-9a-fA-F]+)\s+(\d+)"),
            re.compile(r"(\d+\.\d+\.\d+\.\d+):(\d+)"),
        ]
        while time.time() < deadline:
            line = self._tunnel_proc.stdout.readline()
            if not line:
                if self._tunnel_proc.poll() is not None:
                    break
                continue
            _log_tunnel_line(line)
            for pat in patterns:
                m = pat.search(line)
                if m:
                    return m.group(1), int(m.group(2))
        rest = self._tunnel_proc.stdout.read() if self._tunnel_proc.stdout else ''
        exit_code = self._tunnel_proc.poll()
        cmd_str = ' '.join(cmd) if cmd else 'unknown'
        admin_status = 'admin' if _is_admin() else 'NOT_ADMIN'
        log_path = _tunnel_log_path()
        raise RuntimeError(
            f'[{mode}] RSD 주소 획득 실패 (exit={exit_code}, cmd={cmd_str}, user={admin_status}). '
            f'자세한 출력: {log_path}. rest: {rest[:1000]}'
        )

    def stop_tunnel(self) -> None:
        with self._lock:
            if self._tunnel_proc:
                with suppress(Exception):
                    self._tunnel_proc.terminate()
                    try:
                        self._tunnel_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._tunnel_proc.kill()
                self._tunnel_proc = None
            if self._rsd_source not in ('env', 'manual'):
                self._rsd_host = self._rsd_port = None
                self._rsd_source = None

    def stop_tunneld(self) -> None:
        with self._tunneld_lock:
            if self._tunneld_proc:
                with suppress(Exception):
                    self._tunneld_proc.terminate()
                    try:
                        self._tunneld_proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        self._tunneld_proc.kill()
                self._tunneld_proc = None
            self._tunneld_url = None

    def close(self) -> None:
        """세션 기반 HID manager와 화면 스트림을 먼저 종료한 뒤 터널을 닫는다."""
        try:
            if self.hid_session:
                self.hid_session.stop()
        except Exception:
            pass
        try:
            self.screen_stream.stop()
        except Exception:
            pass
        self.stop_tunnel()
        self.stop_tunneld()

    def start_tunneld(self, timeout: int = 30) -> str:
        """pymobiledevice3 remote tunneld를 시작하고 PYMOBILEDEVICE3_TUNNEL URL을 반환한다."""
        with self._tunneld_lock:
            if self._tunneld_proc and self._tunneld_proc.poll() is None and self._tunneld_url:
                return self._tunneld_url
            self.stop_tunneld()
            # --port 0을 주면 OS가 임시 포트를 할당하고, 실제 URL/포트는 출력에서 파싱한다.
            cmd = [PYTHON, '-m', 'pymobiledevice3', 'remote', 'tunneld', '--host', '127.0.0.1', '--port', '0']
            self._tunneld_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                creationflags=_NO_WINDOW)
            deadline = time.time() + timeout
            url_pattern = re.compile(r"(https?://\S+)" + r"|(\S+@\S+:\d+)")
            port_pattern = re.compile(r"[:/](\d{4,5})\b")
            host_port = None
            while time.time() < deadline:
                line = self._tunneld_proc.stdout.readline()
                if not line:
                    if self._tunneld_proc.poll() is not None:
                        break
                    continue
                # tunneld URL 직접 출력을 우선 사용 (예: http://127.0.0.1:12345)
                m = url_pattern.search(line)
                if m:
                    candidate = m.group(0)
                    if '@' in candidate:
                        host_port = candidate
                        break
                    pm = port_pattern.search(candidate)
                    if pm:
                        host_port = f"127.0.0.1:{pm.group(1)}"
                        break
                # URL이 없으면 줄에서 4~5자리 포트 번호를 찾는다
                pm = port_pattern.search(line)
                if pm:
                    host_port = f"127.0.0.1:{pm.group(1)}"
                    break
            if not host_port:
                rest = self._tunneld_proc.stdout.read() if self._tunneld_proc.stdout else ''
                self.stop_tunneld()
                raise RuntimeError(f'remote tunneld 시작 실패. 출력: {rest[:500]}')
            # UDID 획득
            udid = self._get_udid(timeout=10)
            if not udid:
                self.stop_tunneld()
                raise RuntimeError('연결된 iPhone의 UDID를 획득할 수 없습니다.')
            self._tunneld_url = f"{udid}@{host_port}"
            return self._tunneld_url

    def _get_usbmux_list(self, timeout: int = 10) -> list:
        """pymobiledevice3 usbmux list JSON을 반환한다."""
        try:
            r = subprocess.run([PYTHON, '-m', 'pymobiledevice3', 'usbmux', 'list'],
                               capture_output=True, text=True, timeout=timeout,
                               creationflags=_NO_WINDOW)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout.strip())
                if isinstance(data, list):
                    return data
        except Exception:
            pass
        return []

    @staticmethod
    def _row_udid(row: dict) -> str:
        return row.get('UniqueDeviceID') or row.get('Identifier') or ''

    def _find_usbmux_row(self, timeout: int = 10) -> Optional[dict]:
        """usbmux 목록에서 self.udid 기기 행을 찾는다 (udid 미지정이면 첫 기기)."""
        data = self._get_usbmux_list(timeout)
        if not data:
            return None
        if not self.udid:
            return data[0]
        want = self.udid.lower()
        for row in data:
            if isinstance(row, dict) and self._row_udid(row).lower() == want:
                return row
        return None

    def _get_udid(self, timeout: int = 10) -> Optional[str]:
        """pymobiledevice3 11.x 에서는 lockdown udid 명령이 없으므로 usbmux list JSON에서 추출한다.

        self.udid 가 지정돼 있으면 그 기기가 usbmux 에 보일 때만 반환한다.
        """
        row = self._find_usbmux_row(timeout)
        if row:
            return self._row_udid(row) or None
        return None

    def get_device_info(self, timeout: int = 10) -> dict:
        """iPhone 모델과 화면 해상도를 자동 감지한다.

        usbmux list에서 ProductType/DeviceName/UDID를 가져오고, ProductType을
        알려진 화면 해상도에 매핑한다. ProductType이 알려지지 않은 경우
        lockdown info 명령으로 화면 크기를 가져오려고 시도한다.

        Returns dict with keys: udid, product_type, device_name, screen_width, screen_height, source.
        """
        info = {
            "udid": None,
            "product_type": None,
            "device_name": None,
            "screen_width": None,
            "screen_height": None,
            "source": "unknown",
        }
        try:
            row = self._find_usbmux_row(timeout)
            if not row:
                return info
            info["udid"] = row.get("UniqueDeviceID") or row.get("Identifier") or None
            info["product_type"] = row.get("ProductType") or row.get("product_type") or None
            info["device_name"] = row.get("DeviceName") or row.get("device_name") or None
            # Map ProductType to screen resolution (width x height in pixels)
            resolution = PRODUCT_TYPE_TO_RESOLUTION.get(info["product_type"])
            if resolution:
                info["screen_width"] = resolution[0]
                info["screen_height"] = resolution[1]
                info["source"] = "product_type_map"
            else:
                # Try to fetch from device via lockdown info command (best effort)
                try:
                    info_cmd = [PYTHON, '-m', 'pymobiledevice3', 'lockdown', 'info', '--json']
                    if self.udid:
                        info_cmd += ['--udid', self.udid]
                    r = subprocess.run(info_cmd,
                                       capture_output=True, text=True, timeout=timeout,
                                       creationflags=_NO_WINDOW)
                    if r.returncode == 0 and r.stdout.strip():
                        info_data = json.loads(r.stdout.strip())
                        # Look for screen dimension keys in various formats
                        for key in ('ScreenWidth', 'screenWidth', 'width', 'DisplayWidth', 'displayWidth'):
                            if key in info_data:
                                info["screen_width"] = int(info_data[key])
                                break
                        for key in ('ScreenHeight', 'screenHeight', 'height', 'DisplayHeight', 'displayHeight'):
                            if key in info_data:
                                info["screen_height"] = int(info_data[key])
                                break
                        if info["screen_width"] and info["screen_height"]:
                            info["source"] = "lockdown_info"
                except Exception:
                    pass
        except Exception as e:
            logger.warning("get_device_info failed: %s", e)
        return info

    def detect_resolution(self, fallback_width: int = 1170, fallback_height: int = 2532) -> tuple[int, int]:
        """연결된 iPhone의 화면 해상도를 자동 감지한다.

        get_device_info를 사용해 ProductType을 찾아 알려진 해상도로 매핑한다.
        감지에 실패하면 제공된 기본값을 사용한다.
        """
        info = self.get_device_info()
        w = info.get("screen_width") or fallback_width
        h = info.get("screen_height") or fallback_height
        return int(w), int(h)

    def ensure_tunnel(self) -> tuple[str, int]:
        with self._lock:
            if self._rsd_source in ('env', 'manual') and self._rsd_host and self._rsd_port:
                return self._rsd_host, self._rsd_port
            if self._rsd_host and self._rsd_port and self._tunnel_proc and self._tunnel_proc.poll() is None:
                return self._rsd_host, self._rsd_port
        return self.start_tunnel()

    def run(self, args: List[str], timeout: int = 15, retries: int = 1,
            use_tunneld: bool = False, env: Optional[dict] = None) -> dict:
        if "not installed" in PYMD_VERSION:
            return {"success": False, "stdout": "", "stderr": f"pymobiledevice3 is not installed correctly: {PYMD_VERSION}", "rsd": None, "cmd": ""}

        # tunneld 우선순위: 1) controller가 직접 시작한 URL 2) PYMOBILEDEVICE3_TUNNEL 환경변수
        tunneld_url = self._tunneld_url or PYMD_TUNNEL_ENV
        if use_tunneld or tunneld_url:
            if not tunneld_url:
                try:
                    tunneld_url = self.start_tunneld()
                except Exception as e:
                    return {"success": False, "stdout": "", "stderr": f"tunneld 시작 실패: {type(e).__name__}: {e}", "rsd": None, "cmd": ""}
            cmd = [PYTHON, "-m", "pymobiledevice3"] + args
            rsd_display = f"tunnel:{tunneld_url}"
        else:
            host, port = self.ensure_tunnel()
            rsd_args = ["--rsd", host, str(port)]
            # universal-hid-service tap/drag/session 등 -- 구분자 뒤에는 positional args만 올 수 있으므로
            # --rsd는 -- 앞에 삽입해야 한다.
            if "--" in args:
                idx = args.index("--")
                cmd = [PYTHON, "-m", "pymobiledevice3"] + args[:idx] + rsd_args + args[idx:]
            else:
                cmd = [PYTHON, "-m", "pymobiledevice3"] + args + rsd_args
            rsd_display = f"{host}:{port}"

        run_env = os.environ.copy()
        if env:
            run_env.update(env)
        if tunneld_url:
            run_env['PYMOBILEDEVICE3_TUNNEL'] = tunneld_url

        last_err = ""
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                                   env=run_env, creationflags=_NO_WINDOW)
                if r.returncode == 0:
                    return {"success": True, "stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}

                # 수동/환경변수 RSD가 연결되지 않으면, 로컬에 아이폰이 연결되어 있다면 새 터널을 뚫어 재시도
                if (attempt == 0 and tunneld_url is None and self._rsd_source in ('manual', 'env') and
                        self._is_connection_error(r.stderr)):
                    try:
                        with self._lock:
                            self.stop_tunnel()
                            self._rsd_host = None
                            self._rsd_port = None
                            self._rsd_source = None
                        host, port = self.start_tunnel(timeout=timeout)
                        rsd_args = ["--rsd", host, str(port)]
                        if "--" in args:
                            idx = args.index("--")
                            cmd = [PYTHON, "-m", "pymobiledevice3"] + args[:idx] + rsd_args + args[idx:]
                        else:
                            cmd = [PYTHON, "-m", "pymobiledevice3"] + args + rsd_args
                        rsd_display = f"{host}:{port}"
                        continue
                    except Exception as e:
                        return {"success": False, "stdout": r.stdout.strip(), "stderr": f"{r.stderr.strip()}\nRSD 재연결(새 터널) 실패: {type(e).__name__}: {e}", "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}

                last_err = r.stderr.strip()
            except subprocess.TimeoutExpired:
                last_err = f"TimeoutError ({attempt + 1}/{retries})"
                # HID/XPC handshake가 막혔을 때는 공유 터널(tunneld)이나 RSD 터널을 새로 뚫어 재시도한다.
                if tunneld_url:
                    try:
                        self.stop_tunneld()
                        tunneld_url = self.start_tunneld(timeout=max(30, timeout))
                        run_env['PYMOBILEDEVICE3_TUNNEL'] = tunneld_url
                        rsd_display = f"tunnel:{tunneld_url}"
                    except Exception as e:
                        return {"success": False, "stdout": "", "stderr": f"{last_err}\ntunneld 재시작 실패: {type(e).__name__}: {e}", "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}
                else:
                    try:
                        self.stop_tunnel()
                        self._rsd_host = None
                        self._rsd_port = None
                        self._rsd_source = None
                        host, port = self.start_tunnel(timeout=max(30, timeout))
                        rsd_args = ["--rsd", host, str(port)]
                        if "--" in args:
                            idx = args.index("--")
                            cmd = [PYTHON, "-m", "pymobiledevice3"] + args[:idx] + rsd_args + args[idx:]
                        else:
                            cmd = [PYTHON, "-m", "pymobiledevice3"] + args + rsd_args
                        rsd_display = f"{host}:{port}"
                    except Exception as e:
                        return {"success": False, "stdout": "", "stderr": f"{last_err}\nRSD 재연결(새 터널) 실패: {type(e).__name__}: {e}", "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}
                time.sleep(0.5)
            except Exception as e:
                return {"success": False, "stdout": "", "stderr": f"{type(e).__name__}: {e}", "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}
        return {"success": False, "stdout": "", "stderr": last_err, "rsd": rsd_display, "cmd": " ".join(cmd), "pymd_version": PYMD_VERSION}

    @staticmethod
    def _is_connection_error(stderr: str) -> bool:
        markers = ('getaddrinfo', 'connection refused', 'no route', 'network is unreachable',
                   'timed out', 'timeouterror', 'incompletereaderror', 'cancellederror',
                   'connect call failed', 'target machine actively refused',
                   'remotexpc')
        return any(m in stderr.lower() for m in markers)

    def px_to_hid(self, px_x: int, px_y: int, w: int, h: int) -> tuple[int, int]:
        return (max(0, min(65535, int(px_x * 65535 / w))), max(0, min(65535, int(px_y * 65535 / h))))

    def _run_hid(self, args: List[str], timeout: int = 30, retries: int = 3,
                 extras: Optional[dict] = None, use_tunneld: bool = False) -> dict:
        # iOS 27 daemon cleanup 시간이 짧은 2초로는 부족할 수 있어 환경변수로 조정 가능.
        cooldown = float(os.environ.get('PYMD_HID_COOLDOWN', '2.0'))
        with self._hid_lock:
            try:
                # pymobiledevice3 11.9.2 + iOS 27 조합에서 tunneld는 media-stream remoteIP 문제로
                # HID에 쓸 수 없으므로, 매 HID 호출마다 새 RSD 터널을 뚫어 재사용으로 인한
                # handshake 멈춤/TimeoutError를 피한다.
                r = self.run(args, timeout=timeout, retries=retries, use_tunneld=use_tunneld)
                if extras:
                    r.update(extras)
                return r
            finally:
                # iOS 27에서 반복적인 RSD 터널 생성/파괴가 DisplayService handshake
                # TimeoutError를 유발하므로, 기본적으로 터널을 재사용한다.
                # session manager가 별도 터널을 유지 중일 때도 닫지 않는다.
                if os.environ.get('PYMD_CLOSE_TUNNEL_PER_HID'):
                    self.stop_tunnel()
                # HID/XPC 세션이 iOS 측에서 완전히 정리될 시간을 확보
                time.sleep(cooldown)

    def tap(self, x: int, y: int, w: int, h: int) -> dict:
        hx, hy = self.px_to_hid(x, y, w, h)
        if self.hid_session:
            r = self.hid_session.tap(x, y, w, h)
            if r.get("success"):
                return r
            # HID session manager 실패 시 에러 원인을 보존하면서 CLI fallback
            extras = {"pixel": [x, y], "hid": [hx, hy], "hid_session_error": r.get("stderr", "")}
        else:
            extras = {"pixel": [x, y], "hid": [hx, hy]}
        return self._run_hid(
            ["developer", "core-device", "universal-hid-service", "tap", "--", str(hx), str(hy)],
            extras=extras)

    def drag(self, x1: int, y1: int, x2: int, y2: int, w: int, h: int,
             duration: Optional[float] = None, hold: Optional[float] = None) -> dict:
        hx1, hy1 = self.px_to_hid(x1, y1, w, h)
        hx2, hy2 = self.px_to_hid(x2, y2, w, h)
        extras = {"pixel": {"from": [x1, y1], "to": [x2, y2]}, "hid": {"from": [hx1, hy1], "to": [hx2, hy2]}}
        if duration is not None:
            extras["duration"] = duration
        if hold is not None and hold > 0:
            extras["hold"] = hold
        if self.hid_session:
            r = self.hid_session.drag(x1, y1, x2, y2, w, h, duration=duration, hold=hold)
            if r.get("success"):
                return r
            extras["hid_session_error"] = r.get("stderr", "")
            # universal-hid-service CLI는 hold 옵션을 지원하지 않는다.
            # 앱 스위처 제스처처럼 hold가 필수인 경우, 세션 매니저 실패를 그대로 보고해야
            # hold 없이 동작하는 CLI fallback으로 잘못 넘어가는 일을 막을 수 있다.
            if hold is not None and hold > 0:
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": (
                        f"HID session drag failed and CLI fallback cannot honor hold={hold}s "
                        f"(universal-hid-service drag has no --hold option). "
                        f"Original error: {r.get('stderr', 'unknown')}"
                    ),
                    "rsd": r.get("rsd"),
                    "cmd": "",
                    "pymd_version": PYMD_VERSION,
                    **extras,
                }
        args = ["developer", "core-device", "universal-hid-service", "drag"]
        if duration is not None:
            args += ["--duration", str(duration)]
        args += [str(hx1), str(hy1), str(hx2), str(hy2)]
        return self._run_hid(args, extras=extras)

    def button(self, name: str, state: str = "press") -> dict:
        """iOS hardware button 이벤트 전송 (core-device hid indigo service)."""
        valid = {"home", "lock", "volume-up", "volume-down", "mute", "siri"}
        if name not in valid:
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Unsupported button '{name}'. Choose from: {', '.join(sorted(valid))}",
                "rsd": None,
                "cmd": "",
                "pymd_version": PYMD_VERSION,
            }
        if self.hid_session:
            r = self.hid_session.button(name, state)
            if r.get("success"):
                return r
        return self._run_hid(
            ["developer", "core-device", "hid", "button", name, state],
            extras={"button": name, "state": state})

    def button_sequence(self, sequence: List[tuple[str, str]], interval: float = 0.25) -> dict:
        """단일 RSD 터널 안에서 여러 hardware button 이벤트를 연속 전송."""
        valid = {"home", "lock", "volume-up", "volume-down", "mute", "siri"}
        # session manager가 있으면 재사용 (터널 재생성/파괴 최소화)
        if self.hid_session:
            results: List[dict] = []
            for i, (name, state) in enumerate(sequence):
                if name not in valid:
                    return {
                        "success": False,
                        "stdout": "",
                        "stderr": f"Unsupported button '{name}'. Choose from: {', '.join(sorted(valid))}",
                        "rsd": self.hid_session._rsd_addr(),
                        "cmd": "",
                        "pymd_version": PYMD_VERSION,
                    }
                if i > 0:
                    time.sleep(interval)
                r = self.hid_session.button(name, state)
                r["sequence_index"] = i
                results.append(r)
                if not r.get("success"):
                    return r
            return {
                "success": True,
                "results": results,
                "rsd": self.hid_session._rsd_addr(),
                "pymd_version": PYMD_VERSION,
                "sequence": sequence,
            }
        # fallback: legacy CLI
        cooldown = float(os.environ.get('PYMD_HID_COOLDOWN', '2.0'))
        results: List[dict] = []
        with self._hid_lock:
            try:
                host, port = self.ensure_tunnel()
                for i, (name, state) in enumerate(sequence):
                    if name not in valid:
                        return {
                            "success": False,
                            "stdout": "",
                            "stderr": f"Unsupported button '{name}'. Choose from: {', '.join(sorted(valid))}",
                            "rsd": f"{host}:{port}",
                            "cmd": "",
                            "pymd_version": PYMD_VERSION,
                        }
                    if i > 0:
                        time.sleep(interval)
                    r = self.run(
                        ["developer", "core-device", "hid", "button", name, state],
                        timeout=15, retries=1)
                    r["sequence_index"] = i
                    results.append(r)
                    if not r.get("success"):
                        return r
                return {
                    "success": True,
                    "results": results,
                    "rsd": f"{host}:{port}",
                    "pymd_version": PYMD_VERSION,
                    "sequence": sequence,
                }
            finally:
                if os.environ.get('PYMD_CLOSE_TUNNEL_PER_HID'):
                    self.stop_tunnel()
                time.sleep(cooldown)

    def session(self, commands: List[str], w: int, h: int) -> dict:
        hid: List[str] = []
        for line in commands:
            line = line.strip()
            if not line:
                continue
            p = line.split()
            if p[0] == "tap" and len(p) == 3:
                hx, hy = self.px_to_hid(int(p[1]), int(p[2]), w, h)
                hid.append(f"tap {hx} {hy}")
            elif p[0] == "drag" and len(p) == 5:
                x1, y1, x2, y2 = map(int, p[1:5])
                hx1, hy1 = self.px_to_hid(x1, y1, w, h)
                hx2, hy2 = self.px_to_hid(x2, y2, w, h)
                hid.append(f"drag {hx1} {hy1} {hx2} {hy2}")
            else:
                hid.append(line)
        script = "\n".join(hid) + "\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(script)
            path = f.name
        try:
            r = self._run_hid(
                ["developer", "core-device", "universal-hid-service", "session", "--script", path],
                timeout=60, retries=1)
            r.update({"commands_count": len(hid), "script_preview": "\n".join(hid[:10])})
            return r
        finally:
            with suppress(OSError):
                os.remove(path)


# iOS 27+ HID touch/button session manager.
# 기존 CLI 방식(universal-hid-service tap/drag)은 매 호출마다 DisplayService media stream
# handshake를 새로 해야 하므로 연속 사용 시 TimeoutError가 발생한다.
# 이 클래스는 하나의 RSD 터널 위에 touch_session을 유지하면서 tap/drag/button을 처리한다.
_HID_BUTTON_USAGE: dict[str, tuple[int, int, float]] = {
    'home': (12, 64, 0.05),
    'lock': (12, 48, 0.5),
    'volume-up': (12, 233, 0.05),
    'volume-down': (12, 234, 0.05),
    'mute': (12, 226, 0.05),
    'siri': (12, 207, 1.0),
}


async def _do_tap_session(svc: UniversalHIDServiceService, x: int, y: int,
                          tsid: int = DIGITIZER_SURFACE_MAIN_TOUCHSCREEN) -> None:
    await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y, service_id=tsid)
    await asyncio.sleep(0.05)
    await svc.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x, y, service_id=tsid)


async def _do_drag_session(svc: UniversalHIDServiceService, x1: int, y1: int, x2: int, y2: int,
                           steps: int, duration: float, hold: float = 0.0,
                           tsid: int = DIGITIZER_SURFACE_MAIN_TOUCHSCREEN) -> None:
    frame_interval = duration / max(steps, 1)
    for i in range(steps):
        t = i / steps
        x = round(x1 + (x2 - x1) * t)
        y = round(y1 + (y2 - y1) * t)
        await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x, y, service_id=tsid)
        await asyncio.sleep(frame_interval)
    await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x2, y2, service_id=tsid)
    if hold > 0:
        # 손가락을 멈춘 상태에서도 주기적으로 CONTACT 이벤트를 전송해 iOS가 hold로 인식하도록 함.
        # 20 Hz보다 60 Hz에 가깝게 보내는 편이 iOS 터치스크린 스케줄러가 멈춤(hold)으로 인식하기에 더 안정적이다.
        hold_interval = 1.0 / 60.0
        deadline = asyncio.get_running_loop().time() + hold
        while asyncio.get_running_loop().time() < deadline:
            await svc.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, x2, y2, service_id=tsid)
            await asyncio.sleep(hold_interval)
    await svc.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, x2, y2, service_id=tsid)


class HidSessionManager:
    """pymobiledevice3 HID touch/button API를 단일 RSD 터널 위에서 유지."""

    def __init__(self, controller: PymobileDevice3Controller) -> None:
        self.controller = controller
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._touch_ctx: Any = None
        self._touch_svc: Optional[UniversalHIDServiceService] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return

        def _run_loop() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run_loop, daemon=True, name='HidSessionLoop')
        self._thread.start()
        deadline = time.time() + 5
        while self._loop is None and time.time() < deadline:
            time.sleep(0.01)
        if self._loop is None:
            raise RuntimeError('HID session event loop thread did not start')

    def stop(self) -> None:
        with self._lock:
            self._reset_touch_locked()
            self._close_rsd_sync()
        if self._loop is not None and self._thread is not None and self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass
            self._thread.join(timeout=3)
        self._loop = None
        self._thread = None

    def _close_rsd_sync(self) -> None:
        """event loop thread 안에서 RSD를 닫는다."""
        if self._rsd is None:
            return
        try:
            if self._loop is not None and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(self._rsd.close(), self._loop).result(timeout=5)
        except Exception:
            pass
        self._rsd = None

    def _reset_touch_locked(self) -> None:
        if self._touch_ctx is not None:
            try:
                if self._loop is not None and not self._loop.is_closed():
                    fut = asyncio.run_coroutine_threadsafe(
                        self._touch_ctx.__aexit__(None, None, None), self._loop)
                    fut.result(timeout=5)
            except Exception:
                pass
            finally:
                self._touch_ctx = None
                self._touch_svc = None

    def _ensure_rsd(self) -> None:
        healthy = False
        if self._rsd is not None:
            try:
                conn = getattr(self._rsd, 'service', None)
                xpc = getattr(conn, 'connection', None) if conn else None
                healthy = bool(xpc and not getattr(xpc, 'closed', True))
            except Exception:
                pass
        if not healthy:
            self._close_rsd_sync()
            host, port = self.controller.ensure_tunnel()
            self._rsd = RemoteServiceDiscoveryService((host, port))
            if self._loop is None or self._loop.is_closed():
                raise RuntimeError('HID session event loop is not running')
            try:
                asyncio.run_coroutine_threadsafe(
                    self._rsd.connect(), self._loop).result(timeout=15)
            except TimeoutError as e:
                raise RuntimeError(
                    f'RSD connect timed out ({host}:{port}); '
                    f'ensure device is unlocked and screen is on'
                ) from e

    def _ensure_touch(self) -> None:
        self._ensure_rsd()
        if self._touch_svc is None:
            self._touch_ctx = touch_session(self._rsd)
            if self._loop is None or self._loop.is_closed():
                raise RuntimeError('HID session event loop is not running')
            try:
                self._touch_svc = asyncio.run_coroutine_threadsafe(
                    self._touch_ctx.__aenter__(), self._loop).result(timeout=15)
            except TimeoutError as e:
                raise RuntimeError(
                    'Touch session setup timed out (media stream / DisplayService). '
                    'The device dtuhidd/backboardd may be wedged; reboot the device and retry.'
                ) from e

    def _call(self, coro_factory, ensure=None, timeout: float = 15.0, retries: int = 1):
        """coroutine factory를 실행하고, 실패 시 touch 세션을 재생성해 재시도한다."""
        last_err: Optional[Exception] = None
        for attempt in range(retries + 1):
            try:
                if self._loop is None or self._loop.is_closed():
                    raise RuntimeError('HID session event loop is not running')
                if ensure is not None:
                    ensure()
                coro = coro_factory()
                return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)
            except Exception as e:
                last_err = e
                if attempt < retries:
                    self._reset_touch_locked()
                    time.sleep(0.5)
        if last_err is None:
            last_err = RuntimeError('HID session call failed')
        raise last_err

    def _rsd_addr(self) -> Optional[str]:
        if self._rsd is not None:
            try:
                addr = self._rsd.service.address
                return f"{addr[0]}:{addr[1]}"
            except Exception:
                pass
        return None

    def tap(self, x: int, y: int, w: int, h: int) -> dict:
        self.start()
        hx, hy = self.controller.px_to_hid(x, y, w, h)
        with self._lock:
            try:
                self._call(
                    lambda: _do_tap_session(self._touch_svc, hx, hy),
                    ensure=self._ensure_touch,
                    timeout=10, retries=1)
                return {
                    'success': True,
                    'pixel': [x, y],
                    'hid': [hx, hy],
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                }
            except Exception as e:
                return {
                    'success': False,
                    'stdout': '',
                    'stderr': f'{type(e).__name__}: {e}',
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                    'pixel': [x, y],
                    'hid': [hx, hy],
                }

    def drag(self, x1: int, y1: int, x2: int, y2: int, w: int, h: int,
             duration: Optional[float] = None, hold: Optional[float] = None) -> dict:
        self.start()
        hx1, hy1 = self.controller.px_to_hid(x1, y1, w, h)
        hx2, hy2 = self.controller.px_to_hid(x2, y2, w, h)
        duration = duration if duration is not None else 0.6
        hold = hold if hold is not None else 0.0
        with self._lock:
            try:
                steps = max(10, int(duration * 60))
                self._call(
                    lambda: _do_drag_session(self._touch_svc, hx1, hy1, hx2, hy2, steps, duration, hold=hold),
                    ensure=self._ensure_touch,
                    timeout=20, retries=1)
                return {
                    'success': True,
                    'pixel': {'from': [x1, y1], 'to': [x2, y2]},
                    'hid': {'from': [hx1, hy1], 'to': [hx2, hy2]},
                    'duration': duration,
                    'hold': hold,
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                }
            except Exception as e:
                return {
                    'success': False,
                    'stdout': '',
                    'stderr': f'{type(e).__name__}: {e}',
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                    'pixel': {'from': [x1, y1], 'to': [x2, y2]},
                    'hid': {'from': [hx1, hy1], 'to': [hx2, hy2]},
                    'duration': duration,
                    'hold': hold,
                }

    def button(self, name: str, state: str = 'press') -> dict:
        self.start()
        if name not in _HID_BUTTON_USAGE:
            return {
                'success': False,
                'stdout': '',
                'stderr': f"Unsupported button '{name}'",
                'rsd': self._rsd_addr(),
                'source': 'hid_session',
            }
        usage_page, usage_code, hold = _HID_BUTTON_USAGE[name]
        state_map = {
            'press': None,
            'down': HID_BUTTON_STATE_DOWN,
            'up': HID_BUTTON_STATE_UP,
            'canceled': HID_BUTTON_STATE_CANCELED,
        }
        hid_state = state_map.get(state)
        if hid_state is None and state != 'press':
            return {
                'success': False,
                'stdout': '',
                'stderr': f"Unsupported button state '{state}'",
                'rsd': self._rsd_addr(),
                'source': 'hid_session',
            }

        def _btn_factory():
            async def _btn() -> None:
                async with IndigoHIDService(self._rsd) as svc:
                    if state == 'press':
                        await svc.send_button(usage_page, usage_code, HID_BUTTON_STATE_DOWN)
                        await asyncio.sleep(hold)
                        await svc.send_button(usage_page, usage_code, HID_BUTTON_STATE_UP)
                    else:
                        await svc.send_button(usage_page, usage_code, hid_state)
                await asyncio.sleep(0.1)
            return _btn()

        with self._lock:
            try:
                self._call(
                    _btn_factory,
                    ensure=self._ensure_rsd,
                    timeout=10, retries=1)
                return {
                    'success': True,
                    'button': name,
                    'state': state,
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                }
            except Exception as e:
                return {
                    'success': False,
                    'stdout': '',
                    'stderr': f'{type(e).__name__}: {e}',
                    'rsd': self._rsd_addr(),
                    'source': 'hid_session',
                    'button': name,
                    'state': state,
                }


class ScreenshotStreamManager:
    """RSD 터널 위에서 DVT/Instruments screenshot 채널을 이용해 주기적으로 화면을 캡처하고 캐시한다."""

    def __init__(self, controller: PymobileDevice3Controller) -> None:
        self.controller = controller
        self._lock = threading.RLock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._rsd: Optional[RemoteServiceDiscoveryService] = None
        self._svc: Optional[Any] = None
        self._running = False
        self._fps = 5.0
        self._latest_frame: Optional[bytes] = None
        self._latest_meta: dict = {}
        self._task: Optional[asyncio.Task] = None
        self._error_count = 0
        self._last_error: Optional[str] = None

    def start(self, fps: float = 5.0) -> dict:
        with self._lock:
            if self._running:
                self._fps = max(0.5, min(30.0, float(fps)))
                return {
                    'success': True,
                    'running': True,
                    'fps': self._fps,
                    'message': '이미 실행 중, FPS만 갱신',
                }
            self._fps = max(0.5, min(30.0, float(fps)))
            self._running = True
            self._error_count = 0
            self._last_error = None
            self._latest_frame = None
            self._latest_meta = {}
            self._start_loop()
            if self._loop is None or self._loop.is_closed():
                self._running = False
                return {
                    'success': False,
                    'running': False,
                    'stderr': 'stream event loop failed to start',
                }
            self._task = asyncio.run_coroutine_threadsafe(
                self._capture_loop(), self._loop)
            return {
                'success': True,
                'running': True,
                'fps': self._fps,
                'message': '라이브 스트림 시작',
            }

    def stop(self) -> dict:
        with self._lock:
            self._running = False
            if self._task is not None:
                try:
                    self._task.cancel()
                except Exception:
                    pass
                self._task = None
            self._close_rsd_sync()
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                try:
                    self._loop.call_soon_threadsafe(self._loop.stop)
                except Exception:
                    pass
                self._thread.join(timeout=3)
            self._loop = None
            self._thread = None
            return {'success': True, 'running': False, 'message': '라이브 스트림 중지'}

    def is_running(self) -> bool:
        return self._running

    def get_latest_frame(self) -> tuple[Optional[bytes], dict]:
        with self._lock:
            return self._latest_frame, dict(self._latest_meta)

    def get_status(self) -> dict:
        with self._lock:
            return {
                'running': self._running,
                'fps': self._fps,
                'error_count': self._error_count,
                'last_error': self._last_error,
                'latest': dict(self._latest_meta),
                'has_api': HAS_PMD_SCREEN_API,
            }

    def _start_loop(self) -> None:
        if (self._thread is not None and self._thread.is_alive() and
                self._loop is not None and not self._loop.is_closed()):
            return

        def _run_loop() -> None:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._thread = threading.Thread(target=_run_loop, daemon=True, name='ScreenStreamLoop')
        self._thread.start()
        deadline = time.time() + 5
        while self._loop is None and time.time() < deadline:
            time.sleep(0.01)

    async def _ensure_rsd(self) -> None:
        healthy = False
        if self._rsd is not None:
            try:
                conn = getattr(self._rsd, 'service', None)
                xpc = getattr(conn, 'connection', None) if conn else None
                healthy = bool(xpc and not getattr(xpc, 'closed', True))
            except Exception:
                pass
        if not healthy:
            await self._close_rsd_async()
            host, port = self.controller.ensure_tunnel()
            self._rsd = RemoteServiceDiscoveryService((host, port))
            try:
                await asyncio.wait_for(self._rsd.connect(), timeout=15)
            except asyncio.TimeoutError as e:
                raise RuntimeError(
                    f'RSD connect timed out ({host}:{port}); '
                    f'ensure device is unlocked and screen is on'
                ) from e

    async def _close_rsd_async(self) -> None:
        if self._svc is not None:
            try:
                provider = getattr(self._svc, 'provider', None)
                if provider is not None:
                    await provider.close()
            except Exception:
                pass
            self._svc = None
        if self._rsd is not None:
            try:
                await self._rsd.close()
            except Exception:
                pass
            self._rsd = None

    def _close_rsd_sync(self) -> None:
        if self._rsd is None:
            return
        try:
            if self._loop is not None and not self._loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self._close_rsd_async(), self._loop).result(timeout=5)
        except Exception:
            pass
        finally:
            self._rsd = None
            self._svc = None

    async def _connect_service(self) -> None:
        await self._ensure_rsd()
        if self._svc is None:
            if not HAS_PMD_SCREEN_API:
                raise RuntimeError('DVT screenshot API is not available')
            dvt = DvtProvider(self._rsd)
            screenshot = Screenshot(dvt)
            await screenshot.connect()
            self._svc = screenshot

    async def _capture_loop(self) -> None:
        consecutive_errors = 0
        while self._running:
            t0 = time.time()
            try:
                if self._svc is None:
                    await self._connect_service()
                data = await self._svc.get_screenshot()
                fmt = self._detect_format(data)
                with self._lock:
                    self._latest_frame = data
                    self._latest_meta = {
                        'timestamp': time.time(),
                        'size': len(data),
                        'format': fmt,
                    }
                    self._error_count = 0
                    self._last_error = None
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                err = f'{type(e).__name__}: {e}'
                with self._lock:
                    self._error_count = consecutive_errors
                    self._last_error = err
                try:
                    await self._close_rsd_async()
                except Exception:
                    pass
                await asyncio.sleep(min(2.0, 0.2 * consecutive_errors))
            interval = max(0.033, 1.0 / self._fps)
            elapsed = time.time() - t0
            sleep_time = max(0.0, interval - elapsed)
            await asyncio.sleep(sleep_time)

    @staticmethod
    def _detect_format(data: bytes) -> str:
        if data.startswith(b'\x89PNG'):
            return 'png'
        if data.startswith(b'II*\x00') or data.startswith(b'MM\x00*'):
            return 'tiff'
        return 'unknown'

