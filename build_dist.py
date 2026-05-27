"""배포용 빌드 스크립트.

backend Python 코드를 Cython으로 .pyd 바이너리로 컴파일하고,
frontend를 빌드하고, 배포 패키지를 생성합니다.

사전 요구사항:
  pip install cython
  Visual Studio Build Tools (Windows C 컴파일러)

사용법:
  python build_dist.py                    # 증분 빌드 + 패키징
  python build_dist.py --deploy           # 빌드 + 배포 repo에 commit & push
  python build_dist.py --deploy-only      # 빌드 없이 기존 dist를 push만
  python build_dist.py --full             # 캐시 무시 전체 재빌드
  python build_dist.py --backend          # 백엔드만 컴파일
  python build_dist.py --init-deploy      # 배포 repo 최초 설정
  python build_dist.py --clean            # 빌드 산출물 정리
  python build_dist.py --offline          # 완전 오프라인 배포본
                                          #   - .offline_mode 마커 파일 생성
                                          #   - git_remote.txt 제거 (자동 pull 금지)
                                          #   - OCR 모델 누락 시 빌드 중단
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent
DIST_DIR = PROJECT_ROOT / "dist" / "ReplayKit"
BUILD_DIR = PROJECT_ROOT / "build"
CACHE_DIR = BUILD_DIR / "cache"
HASH_FILE = BUILD_DIR / "build_hashes.json"
VERSION_FILE = PROJECT_ROOT / "version.txt"
HISTORY_FILE = PROJECT_ROOT / "build_history.txt"
INSTALLER_ISS = PROJECT_ROOT / "installer.iss"

NPM_CMD = "npm.cmd" if sys.platform == "win32" else "npm"

EMBED_PYTHON_VERSION = "3.10.11"
EMBED_PYTHON_URL = f"https://www.python.org/ftp/python/{EMBED_PYTHON_VERSION}/python-{EMBED_PYTHON_VERSION}-embed-amd64.zip"
GET_PIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# backend에서 컴파일 제외할 파일
SKIP_COMPILE = {"__init__.py", "dependencies.py",
                "device.py", "scenario.py", "results.py", "settings.py",
                "monitor_client.py"}

INCLUDE_ROOT_FILES = [
    "requirements.txt", "setup.bat", "ReplayKit.bat", "server.py", "replaykit.ico",
    "version.txt",
]

# 배포에서 보존할 항목 (삭제하지 않음)
_PRESERVE_NAMES = {".git", ".gitignore", ".gitattributes", "git_remote.txt", "scan_settings.json"}
_PRESERVE_EXTS = {".whl", ".msi", ".exe", ".zip"}


# ── 버전 관리 (Semantic Versioning) ──

def _read_version() -> str:
    """version.txt에서 현재 버전 읽기. 'v' 접두사 제거하여 'X.Y.Z'로 반환."""
    if VERSION_FILE.exists():
        raw = VERSION_FILE.read_text(encoding="utf-8").strip()
        return raw.lstrip("vV") or "0.0.0"
    return "0.0.0"


def _write_version(ver: str):
    """version.txt에 'vX.Y.Z' 형식으로 저장."""
    if not ver.startswith("v"):
        ver = "v" + ver
    VERSION_FILE.write_text(ver + "\n", encoding="utf-8")


def _update_installer_iss(ver: str) -> bool:
    """installer.iss의 '#define MyAppVersion "X.Y.Z"' 라인을 새 버전으로 갱신."""
    import re
    if not INSTALLER_ISS.exists():
        return False
    try:
        text = INSTALLER_ISS.read_text(encoding="utf-8")
        clean_ver = ver.lstrip("vV")
        new_text, n = re.subn(
            r'(#define\s+MyAppVersion\s+")[^"]*(")',
            rf'\g<1>{clean_ver}\g<2>',
            text,
            count=1,
        )
        if n == 0:
            print(f"  installer.iss: MyAppVersion 정의를 찾지 못함 — 스킵")
            return False
        if new_text != text:
            INSTALLER_ISS.write_text(new_text, encoding="utf-8")
            print(f"  installer.iss: MyAppVersion → {clean_ver}")
        return True
    except Exception as e:
        print(f"  installer.iss 갱신 실패: {e}")
        return False


def _bump(current: str, kind: str) -> str:
    """SemVer bump. kind ∈ {major, minor, patch, none}."""
    parts = (current.split(".") + ["0", "0", "0"])[:3]
    try:
        major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        major, minor, patch = 1, 0, 0
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    return f"{major}.{minor}.{patch}"


def _prompt_version_modal():
    """SemVer 선택 모달. 새 버전 문자열(X.Y.Z) 반환.
    창을 그냥 닫거나 취소하면 None 반환 → 호출 측에서 빌드 중단."""
    import tkinter as tk

    current = _read_version()
    result = {"version": None}

    BG = "#fafafa"
    ROW_BG = "#ffffff"
    ROW_HOVER = "#eef3fb"
    BORDER = "#d9d9d9"

    root = tk.Tk()
    root.title("ReplayKit — 빌드 버전 선택")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg=BG)

    frm = tk.Frame(root, padx=26, pady=22, bg=BG)
    frm.pack(fill="both", expand=True)

    tk.Label(frm, text=f"현재 버전:  v{current}",
             font=("Segoe UI", 12, "bold"), bg=BG, fg="#222").pack(pady=(0, 4))
    tk.Label(frm, text="시맨틱 버저닝 기반 — 빌드할 버전을 선택하세요",
             font=("Segoe UI", 9), bg=BG, fg="#888").pack(pady=(0, 16))

    def pick(kind: str):
        result["version"] = _bump(current, kind)
        root.destroy()

    options = [
        ("minor", _bump(current, "minor"), "하위 호환되는 기능 추가 (MINOR)"),
        ("patch", _bump(current, "patch"), "버그 수정 (PATCH)"),
        ("major", _bump(current, "major"), "하위 호환이 깨지는 변경 (MAJOR)"),
        ("none",  current,                  "버전 유지"),
    ]

    for kind, ver, desc in options:
        row = tk.Frame(frm, bg=ROW_BG, cursor="hand2",
                       highlightbackground=BORDER, highlightthickness=1)
        row.pack(fill="x", pady=4, ipady=8)

        # 버전 컬럼 (고정 너비, 가운데 정렬)
        ver_lbl = tk.Label(row, text=f"v{ver}", width=10, anchor="center",
                           bg=ROW_BG, fg="#1677ff",
                           font=("Segoe UI", 11, "bold"))
        ver_lbl.pack(side="left", padx=(12, 4))

        # 구분자
        sep_lbl = tk.Label(row, text="—", bg=ROW_BG, fg="#bbb",
                           font=("Segoe UI", 11))
        sep_lbl.pack(side="left", padx=8)

        # 설명 컬럼 (좌측 정렬)
        desc_lbl = tk.Label(row, text=desc, anchor="w",
                            bg=ROW_BG, fg="#333",
                            font=("Malgun Gothic", 10))
        desc_lbl.pack(side="left", fill="x", expand=True, padx=(0, 12))

        widgets = (row, ver_lbl, sep_lbl, desc_lbl)
        for w in widgets:
            w.bind("<Button-1>", lambda _e, k=kind: pick(k))
            w.bind("<Enter>", lambda _e, ws=widgets: [x.configure(bg=ROW_HOVER) for x in ws])
            w.bind("<Leave>", lambda _e, ws=widgets: [x.configure(bg=ROW_BG) for x in ws])

    root.update_idletasks()
    w = max(root.winfo_reqwidth(), 420)
    h = root.winfo_reqheight()
    sw, sh = root.winfo_screenwidth(), root.winfo_screenheight()
    root.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # 창을 그냥 닫으면 result["version"]는 None 그대로 → 빌드 취소
    root.protocol("WM_DELETE_WINDOW", root.destroy)
    root.mainloop()

    return result["version"]


def _record_build_history(version: str):
    """빌드 히스토리 기록: [날짜] vX.Y.Z — <short_hash> <subject>."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    commit_info = "(no commit)"
    try:
        r = subprocess.run(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=str(PROJECT_ROOT),
            capture_output=True, encoding="utf-8", errors="replace", timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            commit_info = r.stdout.strip()
    except Exception:
        pass

    entry = f"[{timestamp}] v{version} — {commit_info}\n"
    existing = HISTORY_FILE.read_text(encoding="utf-8") if HISTORY_FILE.exists() else ""
    HISTORY_FILE.write_text(entry + existing, encoding="utf-8")
    print(f"  build_history.txt 갱신: v{version}")


# ── 유틸리티 ──

def _run(cmd, cwd=None, check=True, timeout=300, live_output=False):
    print(f"  > {' '.join(str(c) for c in cmd)}")
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    if live_output:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd or PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
        )
        lines = []
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                print(f"    {line}")
                lines.append(line)
        proc.wait()
        result = subprocess.CompletedProcess(cmd, proc.returncode, "\n".join(lines), "")
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(result.returncode, cmd)
        return result
    return subprocess.run(
        cmd, cwd=str(cwd or PROJECT_ROOT), env=env,
        check=check, capture_output=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def _hash_file(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def _hash_dir(directory: Path, extensions: set[str] = None) -> str:
    """디렉토리 내 파일들의 결합 해시. extensions 지정 시 해당 확장자만."""
    h = hashlib.md5()
    for f in sorted(directory.rglob("*")):
        if not f.is_file():
            continue
        if f.name.startswith(".") or "__pycache__" in str(f):
            continue
        if extensions and f.suffix not in extensions:
            continue
        h.update(f.name.encode())
        h.update(str(f.stat().st_mtime_ns).encode())
    return h.hexdigest()


def _load_hashes() -> dict:
    if HASH_FILE.exists():
        return json.loads(HASH_FILE.read_text(encoding="utf-8"))
    return {}


def _save_hashes(hashes: dict):
    HASH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HASH_FILE.write_text(json.dumps(hashes, indent=2), encoding="utf-8")


# ── Step 1: Backend .pyd 컴파일 ──

def step_compile_backend(force=False) -> bool:
    print("\n=== [1/3] Backend .pyd 컴파일 ===")
    hashes = _load_hashes()
    backend_dir = PROJECT_ROOT / "backend" / "app"
    current_hash = _hash_dir(backend_dir, {".py"})
    server_hash = _hash_file(PROJECT_ROOT / "server.py")
    combined = current_hash + server_hash

    if not force and hashes.get("backend_src") == combined:
        print("  변경 없음 — 건너뜀")
        return True

    try:
        import Cython
        print(f"  Cython {Cython.__version__}")
    except ImportError:
        print("  ERROR: pip install cython 필요")
        return False

    py_files = []
    for root, dirs, files in os.walk(backend_dir):
        for f in files:
            if f.endswith(".py") and f not in SKIP_COMPILE:
                py_files.append(os.path.join(root, f))
    server_py = str(PROJECT_ROOT / "server.py")
    if os.path.exists(server_py):
        py_files.append(server_py)

    if not py_files:
        print("  컴파일할 파일 없음")
        return True

    print(f"  {len(py_files)}개 파일 컴파일 중...")
    setup_content = f"""
import os
from setuptools import setup, Extension
from Cython.Build import cythonize
if __name__ == '__main__':
    py_files = {py_files!r}
    extensions = [Extension(
        os.path.relpath(f, r"{PROJECT_ROOT}").replace(os.sep, ".").replace("/", ".")[:-3], [f]
    ) for f in py_files]
    setup(
        ext_modules=cythonize(extensions, compiler_directives={{'language_level': 3}}, nthreads=0),
        script_args=["build_ext", "--inplace"],
    )
"""
    setup_file = PROJECT_ROOT / "_cython_setup.py"
    setup_file.write_text(setup_content, encoding="utf-8")
    try:
        result = _run([sys.executable, str(setup_file)], check=False, live_output=True)
        if result.returncode != 0:
            print("  컴파일 실패")
            return False
        hashes["backend_src"] = combined
        _save_hashes(hashes)
        print("  컴파일 완료")
        return True
    finally:
        setup_file.unlink(missing_ok=True)


# ── Step 2: Frontend 빌드 ──

def step_build_frontend(force=False) -> bool:
    print("\n=== [2/3] Frontend 빌드 ===")
    fe_dir = PROJECT_ROOT / "frontend"
    hashes = _load_hashes()

    # npm install: package.json + lock 해시
    pkg_hash = _hash_file(fe_dir / "package.json") + _hash_file(fe_dir / "package-lock.json")
    if force or pkg_hash != hashes.get("fe_pkg") or not (fe_dir / "node_modules").exists():
        print("  npm install...")
        _run([NPM_CMD, "install"], cwd=fe_dir, check=False)
        hashes["fe_pkg"] = pkg_hash
    else:
        print("  npm install — skipped")

    # npm run build: src 해시
    src_hash = _hash_dir(fe_dir / "src", {".ts", ".tsx", ".css", ".html"})
    if not force and src_hash == hashes.get("fe_src") and (fe_dir / "dist" / "index.html").exists():
        print("  소스 변경 없음 — 빌드 건너뜀")
        return True

    result = _run([NPM_CMD, "run", "build"], cwd=fe_dir, check=False)
    if result.returncode != 0:
        print(f"  빌드 에러:\n{result.stderr[:500]}")
        return False

    hashes["fe_src"] = src_hash
    _save_hashes(hashes)
    print("  빌드 완료")
    return True


# ── 오프라인 빌드 사전 검증 ──

# 오프라인 배포본에 반드시 dist에 들어 있어야 하는 외부 리소스.
# 누락되면 설치 PC에서 인터넷이 없을 때 해당 기능이 동작 불가.
_OFFLINE_REQUIRED = [
    # (소스 경로 - PROJECT_ROOT 기준 상대 경로, 설명, 치명적 여부)
    ("backend/app/services/ocr_models/korean/rec_infer.onnx", "OCR 한국어 모델", True),
    ("backend/app/services/ocr_models/english/rec_infer.onnx", "OCR 영어 모델", True),
    ("backend/app/services/ocr_models/japan/rec_infer.onnx",   "OCR 일본어 모델", False),
    ("backend/app/services/ocr_models/chinese/rec_infer.onnx", "OCR 중국어 모델", False),
    ("tools/scrcpy-server.jar", "scrcpy 서버 (Android H.264 미러링)", True),
    ("tools/ffmpeg.exe", "ffmpeg (웹캠 녹화 처리)", False),
]


def _validate_offline_prereqs() -> bool:
    """오프라인 빌드 사전 검증. 치명적 자원 누락 시 False."""
    print("\n  [오프라인 검증] 필수 자원 확인...")
    missing_critical = []
    missing_optional = []
    for rel, desc, critical in _OFFLINE_REQUIRED:
        p = PROJECT_ROOT / rel
        if p.exists():
            print(f"    OK  {rel} ({desc})")
        else:
            print(f"    MISS {rel} ({desc})")
            (missing_critical if critical else missing_optional).append((rel, desc))
    # 루트 wheel/installer는 선택적 — 없어도 빌드는 통과
    optional_root_globs = [
        ("lge.auto-*.whl", "lge.auto Python wheel"),
        ("Git-*.exe", "Git for Windows 인스톨러"),
        ("vcredist_x64.exe", "VC++ Redistributable"),
        ("python-3.10.4-amd64.exe", "시스템 Python 3.10 인스톨러(폴백용)"),
        ("node-*-x64.msi", "Node.js MSI"),
        ("VimbaX_Setup*.exe", "Vimba X SDK(Vision Camera)"),
    ]
    for pattern, desc in optional_root_globs:
        found = list(PROJECT_ROOT.glob(pattern))
        if found:
            print(f"    OK  {found[0].name} ({desc})")
        else:
            print(f"    --  {pattern} ({desc}) — 없음(선택)")
    if missing_critical:
        print("\n  [오프라인 검증] 치명적 자원 누락:")
        for rel, desc in missing_critical:
            print(f"    * {rel} — {desc}")
        print("\n  해결 방법:")
        print("    - OCR 모델: 빌드 PC에서 한 번")
        print("        pip install paddle2onnx paddlepaddle")
        print("        python scripts/download_ocr_models.py")
        print("    - scrcpy-server.jar: tools/ 폴더에 미리 복사")
        return False
    if missing_optional:
        print("\n  [오프라인 검증] 선택 자원 누락(기능 일부만 비활성화):")
        for rel, desc in missing_optional:
            print(f"    - {rel} — {desc}")
    return True


# ── Step 3: 패키지 조립 ──

def step_package(force=False, offline=False) -> bool:
    print("\n=== [3/3] 배포 패키지 생성 ===")
    if offline:
        if not _validate_offline_prereqs():
            print("\n  오프라인 빌드 중단.")
            return False
    t0 = time.time()
    DIST_DIR.mkdir(parents=True, exist_ok=True)

    # 빌드가 관리하는 디렉토리만 삭제 (python은 캐시되므로 제외)
    for item in list(DIST_DIR.iterdir()):
        if item.name in _PRESERVE_NAMES or item.suffix in _PRESERVE_EXTS:
            continue
        if item.is_dir():
            if item.name in {"backend", "frontend", "docs", "scripts"}:
                shutil.rmtree(item)
        else:
            if item.suffix in {".py", ".bat", ".ico", ".txt"} and item.name != "git_remote.txt":
                item.unlink()

    # ── backend (.pyd + 설정파일) ──
    _copy_backend()

    # ── frontend/dist ──
    _copy_frontend()

    # ── 루트 파일 + server.py/.pyd ──
    _copy_root_files()

    # ── scripts/ (download_ocr_models.py 등 운영 보조 스크립트) ──
    _copy_scripts()

    # ── 외부 리소스 (보존 우선) ──
    _copy_external_resources()

    # ── Embedded Python (캐시 활용) ──
    _prepare_embedded_python(force)

    # ── 빈 디렉토리 ──
    for d in ["backend/scenarios", "backend/results", "backend/screenshots",
              "backend/app/plugins", "Results/Video", "logs"]:
        (DIST_DIR / d).mkdir(parents=True, exist_ok=True)

    # ── .gitignore ──
    _write_dist_gitignore()

    # ── 루트 인스톨러/wheel 자동 복사 (PROJECT_ROOT에 있는 것만) ──
    _copy_root_installers()

    # ── git_remote.txt / .offline_mode ──
    git_remote_file = DIST_DIR / "git_remote.txt"
    offline_marker = DIST_DIR / ".offline_mode"
    if offline:
        # 오프라인 모드: 자동 git pull 차단, 마커 파일 생성
        if git_remote_file.exists():
            git_remote_file.unlink()
            print("  오프라인 모드: git_remote.txt 제거")
        offline_marker.write_text(
            "# 이 파일이 있으면 ReplayKit.bat / setup.bat이 네트워크 액세스를\n"
            "# 시도하지 않습니다. 삭제하면 온라인 모드로 동작합니다.\n",
            encoding="utf-8",
        )
        print("  오프라인 모드: .offline_mode 마커 생성")
    else:
        # 온라인 모드: 마커 제거 + git_remote.txt 자동 채움
        if offline_marker.exists():
            offline_marker.unlink()
        remote_url = _get_deploy_remote()
        if remote_url:
            git_remote_file.write_text(remote_url, encoding="utf-8")
        elif git_remote_file.exists():
            git_remote_file.unlink()

    # 통계
    total = sum(1 for _ in DIST_DIR.rglob("*") if _.is_file())
    pyd_count = sum(1 for _ in DIST_DIR.rglob("*.pyd"))
    py_count = sum(1 for _ in DIST_DIR.rglob("*.py"))
    elapsed = time.time() - t0
    print(f"\n  패키지 완료: {DIST_DIR}" + (" [OFFLINE]" if offline else ""))
    print(f"  총 {total}개 파일 (.pyd: {pyd_count}, .py: {py_count})")
    print(f"  소요: {elapsed:.1f}s")
    return True


def _copy_root_installers():
    """PROJECT_ROOT에 있는 외부 인스톨러/wheel을 dist 루트에 복사.

    오프라인 배포본을 위해 빌드 PC에 미리 두어야 하는 파일들:
      - lge.auto-*.whl       (로컬 wheel, setup.bat에서 pip install)
      - Git-*.exe            (Git for Windows; installer.iss가 silent install)
      - vcredist_x64.exe     (VC++ Runtime; installer.iss가 silent install)
      - python-3.10.4-amd64.exe (시스템 Python 폴백; 임베디드 사용 시 불필요)
      - node-*-x64.msi       (개발자용 Node.js; production 모드에선 불필요)
      - VimbaX_Setup*.exe    (Vision Camera SDK; 컴포넌트 선택 시)

    빌드 시 PROJECT_ROOT에서 발견되면 자동 dist에 복사. 없으면 조용히 스킵.
    (step_package 첫머리의 보존 규칙(_PRESERVE_EXTS)이 .whl/.exe/.msi/.zip을 유지하므로
     이미 dist에 들어 있던 파일은 덮어쓰기만 됨.)
    """
    patterns = [
        "lge.auto-*.whl",
        "Git-*.exe",
        "vcredist_x64.exe",
        "python-3.10.4-amd64.exe",
        "node-*-x64.msi",
        "VimbaX_Setup*.exe",
    ]
    copied = []
    for pattern in patterns:
        for src in PROJECT_ROOT.glob(pattern):
            if src.is_file():
                shutil.copy2(str(src), str(DIST_DIR / src.name))
                copied.append(src.name)
    if copied:
        print(f"  루트 인스톨러 복사: {len(copied)}개")
        for name in copied:
            print(f"    + {name}")


def _copy_backend():
    print("  backend 복사 중...")
    src = PROJECT_ROOT / "backend"
    dst = DIST_DIR / "backend"
    skip_files = {"auxiliary_devices.json", "settings.json"}

    for root, dirs, files in os.walk(src):
        # 빌드 산출물에서 제외할 디렉토리:
        #  - __pycache__: 컴파일 캐시
        #  - scenarios/results/screenshots: 런타임 사용자 데이터
        #  - _tmp: download_ocr_models.py의 임시 다운로드/추출 디렉토리 (정상 종료 시 정리되지만 안전망)
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "scenarios", "results", "screenshots", "_tmp")]
        rel_root = Path(root).relative_to(src)
        dst_root = dst / rel_root
        dst_root.mkdir(parents=True, exist_ok=True)

        for f in files:
            if f in skip_files or f.endswith(".c"):
                continue
            src_file = Path(root) / f
            dst_file = dst_root / f

            if f.endswith(".py"):
                if f == "__init__.py":
                    dst_file.write_text("", encoding="utf-8")
                elif f in SKIP_COMPILE:
                    shutil.copy2(str(src_file), str(dst_file))
                    # 이전 빌드의 .pyd 제거 (Python이 .pyd 우선 로딩)
                    for old_pyd in dst_root.glob(f"{f[:-3]}.*.pyd"):
                        old_pyd.unlink()
            elif f.endswith(".pyd"):
                shutil.copy2(str(src_file), str(dst_file))
            else:
                shutil.copy2(str(src_file), str(dst_file))

    (dst / "__init__.py").touch()

    # plugins: .py 포함
    plugins_src = src / "app" / "plugins"
    plugins_dst = dst / "app" / "plugins"
    if plugins_src.is_dir():
        plugins_dst.mkdir(parents=True, exist_ok=True)
        for f in plugins_src.iterdir():
            if f.is_file():
                shutil.copy2(str(f), str(plugins_dst / f.name))


def _copy_frontend():
    print("  frontend 복사 중...")
    src = PROJECT_ROOT / "frontend" / "dist"
    dst = DIST_DIR / "frontend" / "dist"
    if src.exists():
        shutil.copytree(str(src), str(dst))


def _copy_scripts():
    """운영 보조 스크립트 복사 (download_ocr_models.py 등).

    ReplayKit.bat의 자동 OCR 모델 설치(:update_ocr_models)가 폴백으로
    호출할 수 있어야 하므로 dist에 포함시킨다."""
    src = PROJECT_ROOT / "scripts"
    dst = DIST_DIR / "scripts"
    if not src.exists():
        return
    print("  scripts 복사 중...")
    dst.mkdir(parents=True, exist_ok=True)
    for f in src.iterdir():
        if f.is_file() and f.suffix in (".py", ".bat", ".ps1"):
            shutil.copy2(str(f), str(dst / f.name))


def _copy_root_files():
    print("  루트 파일 복사 중...")
    for f in INCLUDE_ROOT_FILES:
        if f == "server.py":
            continue
        src = PROJECT_ROOT / f
        if src.exists():
            shutil.copy2(str(src), str(DIST_DIR / f))

    # server.py → .pyd + 런처
    server_pyd = list(PROJECT_ROOT.glob("server.cp*.pyd"))
    if server_pyd:
        pyd_filename = server_pyd[0].name
        shutil.copy2(str(server_pyd[0]), str(DIST_DIR / pyd_filename))
        launcher_code = f"""import os, sys, importlib.util
_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _dir)
_spec = importlib.util.spec_from_file_location("server", os.path.join(_dir, "{pyd_filename}"))
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_mod.main()
"""
        (DIST_DIR / "server.py").write_text(launcher_code, encoding="utf-8")
        (DIST_DIR / "_launcher.py").write_text(launcher_code, encoding="utf-8")
        print(f"  {pyd_filename} + launcher")
    else:
        src = PROJECT_ROOT / "server.py"
        if src.exists():
            shutil.copy2(str(src), str(DIST_DIR / "server.py"))
            print("  server.py 원본 복사")


def _copy_external_resources():
    """외부 리소스: 소스에 있으면 복사/머지, 없으면 기존 유지."""
    # DLT Viewer SDK
    _sync_dir(PROJECT_ROOT / "DltViewerSDK_21.1.3_ver", DIST_DIR / "DltViewerSDK_21.1.3_ver", "DltViewerSDK")

    # tools (ffmpeg 등) — 머지 모드
    src_tools = PROJECT_ROOT / "tools"
    dst_tools = DIST_DIR / "tools"
    if src_tools.is_dir():
        dst_tools.mkdir(parents=True, exist_ok=True)
        for item in src_tools.rglob("*"):
            if item.is_file():
                target = dst_tools / item.relative_to(src_tools)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(item), str(target))
        print("  tools 머지 완료")
    elif dst_tools.is_dir():
        print("  tools 유지")

    # Git installer
    for gi in PROJECT_ROOT.glob("Git-*.exe"):
        shutil.copy2(str(gi), str(DIST_DIR / gi.name))
        print(f"  Git installer: {gi.name}")

    # docs
    src_docs = PROJECT_ROOT / "docs"
    dst_docs = DIST_DIR / "docs"
    if src_docs.is_dir():
        if dst_docs.exists():
            shutil.rmtree(str(dst_docs))
        shutil.copytree(str(src_docs), str(dst_docs))
        print("  docs 복사 완료")


def _sync_dir(src: Path, dst: Path, label: str):
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(str(dst))
        shutil.copytree(str(src), str(dst))
        print(f"  {label} 복사")
    elif dst.is_dir():
        print(f"  {label} 유지")


# ── Embedded Python ──

def _prepare_embedded_python(force=False):
    print("  Embedded Python 준비 중...")
    import urllib.request

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached_zip = CACHE_DIR / f"python-{EMBED_PYTHON_VERSION}-embed-amd64.zip"
    cached_pip = CACHE_DIR / "get-pip.py"

    # 다운로드 (캐시)
    if not cached_zip.exists():
        print(f"  Downloading embedded Python...")
        urllib.request.urlretrieve(EMBED_PYTHON_URL, str(cached_zip))
    if not cached_pip.exists():
        print(f"  Downloading get-pip.py...")
        urllib.request.urlretrieve(GET_PIP_URL, str(cached_pip))

    # dist에 zip + get-pip.py 복사
    shutil.copy2(str(cached_zip), str(DIST_DIR / cached_zip.name))
    shutil.copy2(str(cached_pip), str(DIST_DIR / "get-pip.py"))

    # python/ 폴더: 해시로 변경 감지
    python_dir = DIST_DIR / "python"
    req_file = PROJECT_ROOT / "requirements.txt"
    req_hash = _hash_file(req_file)
    hash_file = python_dir / ".req_hash"
    old_hash = hash_file.read_text().strip() if hash_file.exists() else ""

    if not force and python_dir.exists() and (python_dir / "python.exe").exists() and req_hash == old_hash:
        print("  Embedded Python 변경 없음 — skipped")
        return

    # 재구성 필요
    if python_dir.exists():
        shutil.rmtree(str(python_dir))

    import zipfile
    print("  Extracting embedded Python...")
    with zipfile.ZipFile(str(cached_zip)) as zf:
        zf.extractall(str(python_dir))

    # ._pth 수정
    for pth in python_dir.glob("python*._pth"):
        lines = pth.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for line in lines:
            new_lines.append("import site" if line.strip() == "#import site" else line)
        if "Lib" not in "\n".join(new_lines):
            new_lines.insert(1, "Lib")
            new_lines.insert(2, "Lib\\site-packages")
        pth.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    # tkinter 복사
    _copy_tkinter(Path(sys.base_prefix), python_dir)

    # pip 설치
    print("  Installing pip...")
    _run([str(python_dir / "python.exe"), str(DIST_DIR / "get-pip.py"),
          "--no-warn-script-location", "-q"], check=False, live_output=False)

    # requirements.txt 패키지 설치
    if req_file.exists():
        print("  Installing packages from requirements.txt...")
        _run([str(python_dir / "python.exe"), "-m", "pip", "install",
              "-r", str(req_file), "-q", "--no-warn-script-location"],
             check=False, live_output=False)
        hash_file.write_text(req_hash)

    print("  Embedded Python ready")


def _copy_tkinter(py_base: Path, embed_dir: Path):
    lib_dir = embed_dir / "Lib"
    lib_dir.mkdir(exist_ok=True)
    src_tkinter = py_base / "Lib" / "tkinter"
    if src_tkinter.is_dir():
        shutil.copytree(str(src_tkinter), str(lib_dir / "tkinter"))
    for name in ["_tkinter.pyd", "tcl86t.dll", "tk86t.dll"]:
        for parent in [py_base / "DLLs", py_base]:
            src = parent / name
            if src.exists():
                shutil.copy2(str(src), str(embed_dir / name))
                break
    src_tcl = py_base / "tcl"
    if src_tcl.is_dir():
        shutil.copytree(str(src_tcl), str(embed_dir / "tcl"))


def _write_dist_gitignore():
    (DIST_DIR / ".gitignore").write_text("""# 런타임
venv/
python/
__pycache__/
*.pyc
*.c
logs/

# 인스톨러
*.exe
*.msi
*.zip
*.whl
get-pip.py
DltViewerSDK_21.1.3_ver/
# tools/ 폴더의 binary들(ffmpeg.exe 100MB 등)은 deploy push 제외하되,
# scrcpy-server.jar는 60KB로 작고 H.264 미러링 백엔드의 필수 자원이므로
# negative pattern으로 추적 허용. "tools/"가 아닌 "tools/*"로 디렉토리
# 내용만 ignore해야 negative pattern이 적용된다는 git 동작에 주의.
tools/*
!tools/scrcpy-server.jar

# 사용자 데이터
backend/screenshots/
backend/results/
backend/scenarios/
backend/auxiliary_devices.json
backend/settings.json
Results/

# 기타
DLL_DEBUG/
.env
unins*
""", encoding="utf-8")


# ── 배포 repo ──

def _get_deploy_remote() -> str:
    try:
        r = _run(["git", "remote", "get-url", "origin"], cwd=DIST_DIR, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def init_deploy():
    print("\n=== 배포 repo 초기화 ===")
    DIST_DIR.mkdir(parents=True, exist_ok=True)
    if (DIST_DIR / ".git").exists():
        print(f"  이미 git repo 존재: {DIST_DIR}")
        return
    url = input("  배포 repo URL: ").strip()
    if not url:
        print("  취소됨")
        return
    _run(["git", "init"], cwd=DIST_DIR)
    _run(["git", "remote", "add", "origin", url], cwd=DIST_DIR)
    print(f"  완료: {url}")


def deploy(commit_msg=None):
    print("\n=== 배포 push ===")
    if not (DIST_DIR / ".git").exists():
        print("  ERROR: --init-deploy 먼저 실행")
        return False

    if not commit_msg:
        try:
            r = _run(["git", "log", "-1", "--format=%s"], check=False)
            commit_msg = r.stdout.strip() or "Update build"
        except Exception:
            commit_msg = "Update build"

    _run(["git", "add", "-A"], cwd=DIST_DIR)
    r = _run(["git", "status", "--porcelain"], cwd=DIST_DIR, check=False)
    if not r.stdout.strip():
        print("  변경 없음 — skip")
        return True

    _run(["git", "commit", "-m", commit_msg], cwd=DIST_DIR, check=False)

    r_remotes = _run(["git", "remote"], cwd=DIST_DIR, check=False)
    remotes = [n.strip() for n in r_remotes.stdout.strip().splitlines() if n.strip()] or ["origin"]

    ok = True
    for remote in remotes:
        print(f"  push → {remote}...", end=" ")
        result = _run(["git", "push", "-u", remote, "main"], cwd=DIST_DIR, check=False)
        if result.returncode != 0:
            _run(["git", "branch", "-M", "main"], cwd=DIST_DIR, check=False)
            result = _run(["git", "push", "-u", remote, "main"], cwd=DIST_DIR, check=False)
        if result.returncode == 0:
            print("OK")
        else:
            print(f"FAIL: {result.stderr[:200]}")
            ok = False
    return ok


# ── 정리 ──

def clean():
    print("\n=== 정리 ===")
    count = 0
    for pattern in ("*.c", "*.pyd"):
        for f in (PROJECT_ROOT / "backend").rglob(pattern):
            f.unlink()
            count += 1
    for pattern in ("server.*.pyd", "server.c"):
        for f in PROJECT_ROOT.glob(pattern):
            f.unlink()
            count += 1
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    for f in [PROJECT_ROOT / "_cython_setup.py"]:
        if f.exists():
            f.unlink()
    print(f"  {count}개 파일 삭제")


# ── 메인 ──

def main():
    # OS 가드 — Windows 전용 (Cython + MSVC + Inno Setup 필요).
    # Linux 에선 scripts/build_deb.sh 또는 scripts/release.py 사용.
    if sys.platform != "win32":
        print(f"[ERROR] build_dist.py 는 Windows 전용입니다 (current: {sys.platform})", file=sys.stderr)
        print("        Linux 빌드: ./scripts/build_deb.sh", file=sys.stderr)
        print("        통합 TUI:   ./scripts/release.py", file=sys.stderr)
        sys.exit(2)

    args = set(sys.argv[1:])
    force = "--full" in args
    offline = "--offline" in args

    if "--clean" in args:
        clean()
        return

    if "--init-deploy" in args:
        init_deploy()
        return

    if "--deploy-only" in args:
        deploy()
        return

    # ── 빌드 시작: SemVer 모달 ──
    print("\n버전 선택 모달을 표시합니다...")
    new_version = _prompt_version_modal()
    if new_version is None:
        print("\n빌드 취소: 버전이 선택되지 않았습니다.")
        return

    current_version = _read_version()
    version_changed = new_version != current_version
    if version_changed:
        _write_version(new_version)
        print(f"  version.txt: v{current_version} → v{new_version}")
    else:
        print(f"  version.txt: v{current_version} (유지) — build_history.txt 기록 생략")
    _update_installer_iss(new_version)

    if "--backend" in args:
        ok = step_compile_backend(force)
        clean()
        if ok and version_changed:
            _record_build_history(new_version)
        return

    t_start = time.time()
    print("=" * 50)
    mode_tag = []
    if force:
        mode_tag.append("FULL")
    if offline:
        mode_tag.append("OFFLINE")
    tag = f" ({', '.join(mode_tag)})" if mode_tag else " (증분)"
    print(f"  ReplayKit — 배포 빌드 v{new_version}{tag}")
    print("=" * 50)

    if not step_compile_backend(force):
        print("\n빌드 중단: backend 컴파일 실패")
        return

    if not step_build_frontend(force):
        print("\n빌드 중단: frontend 빌드 실패")
        return

    if not step_package(force, offline=offline):
        print("\n빌드 중단: 패키지 조립 실패")
        return
    clean()

    if version_changed:
        _record_build_history(new_version)

    elapsed = time.time() - t_start
    print(f"\n{'=' * 50}")
    print(f"  빌드 완료! v{new_version} ({elapsed:.1f}s)")
    print(f"  배포 폴더: {DIST_DIR}")
    print(f"{'=' * 50}")

    if "--deploy" in args:
        deploy()


if __name__ == "__main__":
    main()
