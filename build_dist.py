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


# ── PySide6 release dialog — cross-platform GUI (Win + Linux) ──
# 기존 tkinter _prompt_version_modal 대체. release.py 의 OS 선택 + 버전 + 옵션을
# 하나의 다이얼로그로 통합. Linux 에서도 동일 GUI 사용 (PySide6 LGPL).

# dist/ReplayKit (빌드 산출물) push 대상.
# 사내 LGE git 으로만 force push — 외부(GitHub) 배포 채널은 제거됨.
# 소스코드 sync (개인PC ↔ 회사PC) 는 PROJECT_ROOT/.git 의 origin (ReplayKit_Universal)
# 가 별도로 담당 — build_dist.py 와 무관.
DIST_PUSH_REMOTES = {
    "win": [
        ("lge",    "http://mod.lge.com/hub/dqa_replay_kit/replay_kit.git"),
    ],
    "linux": [
        ("lge",    "http://mod.lge.com/hub/dqa_replay_kit/replay_kit_linux.git"),
    ],
}
OS_LABELS = {"win": "Windows", "linux": "Linux"}


def _remote_url(target: str, name: str) -> str | None:
    """DIST_PUSH_REMOTES 에서 (target, remote_name) 의 URL 조회."""
    for n, u in DIST_PUSH_REMOTES.get(target, []):
        if n == name:
            return u
    return None


def _host_os() -> str:
    """현재 호스트의 OS 식별자 — 'win' / 'linux' / 그 외."""
    if sys.platform == "win32":
        return "win"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


# Modernized 다이얼로그 스타일시트 — 깔끔한 minimal 디자인.
# 컬러 톤: 라이트 회색 배경 + 차분한 파란 액센트 (#2563eb 류). border-radius 8px.
_DIALOG_QSS = """
QDialog {
    background: #f6f7f9;
}
QLabel#headerTitle {
    font-size: 16px;
    font-weight: 600;
    color: #111827;
}
QLabel#headerSub {
    font-size: 11px;
    color: #6b7280;
}
QLabel#sectionTitle {
    font-size: 11px;
    font-weight: 600;
    color: #6b7280;
    letter-spacing: 0.4px;
    text-transform: uppercase;
    margin-top: 4px;
}
QLabel#sectionHint {
    font-size: 10px;
    color: #9ca3af;
    margin-top: 4px;
}
QFrame#card {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
}
QFrame#osPill {
    background: #eff6ff;
    border: 1px solid #bfdbfe;
    border-radius: 14px;
    padding: 4px 12px;
}
QLabel#osPillText {
    color: #1d4ed8;
    font-size: 12px;
    font-weight: 600;
}
QLabel#currentVer {
    color: #374151;
    font-size: 13px;
    padding: 2px 0;
}
QLabel#currentVerVal {
    color: #111827;
    font-size: 14px;
    font-weight: 600;
}
QCheckBox {
    font-size: 13px;
    color: #1f2937;
    spacing: 8px;
    padding: 4px 0;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
}
QPushButton#bumpBtn {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 12px 14px;
    text-align: left;
    color: #111827;
    font-size: 13px;
}
QPushButton#bumpBtn:hover {
    background: #eff6ff;
    border: 1px solid #93c5fd;
}
QPushButton#bumpBtn:pressed {
    background: #dbeafe;
    border: 1px solid #60a5fa;
}
QPushButton#bumpBtnKeep {
    background: #fafafa;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 12px 14px;
    text-align: left;
    color: #6b7280;
    font-size: 13px;
}
QPushButton#bumpBtnKeep:hover {
    background: #f3f4f6;
    border: 1px solid #d1d5db;
}
QPushButton#ghost {
    background: transparent;
    border: 1px solid transparent;
    color: #6b7280;
    padding: 6px 14px;
    font-size: 12px;
}
QPushButton#ghost:hover {
    color: #111827;
    background: #f3f4f6;
}
QLabel#warning {
    color: #b91c1c;
    background: #fef2f2;
    border: 1px solid #fecaca;
    border-radius: 6px;
    padding: 8px 12px;
    font-size: 12px;
}
QLabel#hint {
    color: #6b7280;
    font-size: 11px;
}
"""


def _card(parent_layout, title: str):
    """카드 컨테이너 — section title + QFrame#card 안에 children layout."""
    from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
    t = QLabel(title)
    t.setObjectName("sectionTitle")
    parent_layout.addWidget(t)
    frame = QFrame()
    frame.setObjectName("card")
    inner = QVBoxLayout(frame)
    inner.setContentsMargins(14, 12, 14, 12)
    inner.setSpacing(10)
    parent_layout.addWidget(frame)
    return inner


def _show_release_dialog():
    """PySide6 release 다이얼로그. (target_os, version, do_frontend, do_push) 또는 None.

    UI 흐름:
      1. target OS = host OS 자동 (선택 불필요, pill 라벨로 표시)
      2. OPTIONS 카드 — frontend 빌드 포함 / 배포 git push 체크
      3. VERSION 카드 — 현재 버전 라벨 + 4개 bump 행 (patch/minor/major/유지).
         각 행을 클릭하면 즉시 그 버전으로 dialog 종료 → 빌드 시작.
      None = 사용자 취소 (X 또는 '취소' 버튼).
    """
    try:
        from PySide6.QtWidgets import (
            QApplication, QDialog, QVBoxLayout, QHBoxLayout,
            QLabel, QPushButton, QCheckBox, QFrame,
        )
        from PySide6.QtCore import Qt
    except ImportError as e:
        msg = (
            "[ERROR] PySide6 미설치 — GUI 모드 사용 불가.\n"
            "  설치: pip install PySide6-Essentials\n"
            f"  원인: {e}\n"
            "  대안: CLI 모드 — python build_dist.py --version 1.2.0"
        )
        print(msg, file=sys.stderr)
        return None

    current = _read_version()
    host = _host_os()
    target = host  # 항상 host = target (현재 시스템에서만 빌드)
    if target not in OS_LABELS:
        print(f"[ERROR] 지원하지 않는 host OS: {host}", file=sys.stderr)
        return None

    app = QApplication.instance() or QApplication(sys.argv)
    dlg = QDialog()
    dlg.setWindowTitle("ReplayKit · Release")
    dlg.setMinimumWidth(520)
    dlg.setStyleSheet(_DIALOG_QSS)

    root = QVBoxLayout(dlg)
    root.setContentsMargins(22, 20, 22, 18)
    root.setSpacing(14)

    # ── 헤더 + OS pill (한 줄) ──
    head_row = QHBoxLayout()
    head_box = QVBoxLayout()
    head_box.setSpacing(2)
    title = QLabel("ReplayKit Release")
    title.setObjectName("headerTitle")
    sub = QLabel("OS 선택은 호스트 자동 · 버전 선택 시 즉시 빌드 시작")
    sub.setObjectName("headerSub")
    head_box.addWidget(title)
    head_box.addWidget(sub)
    head_row.addLayout(head_box)
    head_row.addStretch()

    # 호스트 OS pill
    pill = QFrame()
    pill.setObjectName("osPill")
    pill_lay = QHBoxLayout(pill)
    pill_lay.setContentsMargins(10, 4, 10, 4)
    pill_lay.setSpacing(6)
    icon = QLabel("●")
    icon.setStyleSheet("color:#2563eb; font-size:10px;")
    pill_text = QLabel(f"target  ·  {OS_LABELS[target]}")
    pill_text.setObjectName("osPillText")
    pill_lay.addWidget(icon)
    pill_lay.addWidget(pill_text)
    head_row.addWidget(pill, 0, Qt.AlignTop)
    root.addLayout(head_row)

    # ── 옵션 ──
    opt_layout = _card(root, "OPTIONS")
    cb_frontend = QCheckBox("frontend 빌드 포함  (npm run build · 약 30~90초)")
    cb_frontend.setChecked(True)
    cb_push = QCheckBox("배포 git push  (force, OS 별 LG remote)")
    cb_push.setChecked(True)
    opt_layout.addWidget(cb_frontend)
    opt_layout.addWidget(cb_push)
    # Linux 만 .deb 인스톨러 생성 옵션 노출 (Windows 는 installer.iss 가 별도 단계).
    cb_deb = QCheckBox(".deb 인스톨러 생성  (최초 설치/배포용, dist/replaykit_*.deb)")
    cb_deb.setChecked(True)
    if target == "linux":
        opt_layout.addWidget(cb_deb)
    hint = QLabel("• frontend 변경이 없으면 체크 해제로 빌드 시간 단축\n• git push 해제 시 빌드만 하고 배포 git 갱신은 건너뜀\n• .deb 해제 시 dist/ReplayKit/ 만 생성 (Mode A · git clone 사용자만 업데이트)")
    hint.setObjectName("hint")
    hint.setWordWrap(True)
    opt_layout.addWidget(hint)

    # ── 버전 (마지막) ──
    ver_layout = _card(root, "VERSION  ·  클릭 시 즉시 빌드 시작")

    # 현재 버전 라벨 (편집 불가)
    cur_row = QHBoxLayout()
    cur_lbl = QLabel("현재 버전")
    cur_lbl.setObjectName("currentVer")
    cur_val = QLabel(f"v{current.lstrip('vV')}")
    cur_val.setObjectName("currentVerVal")
    cur_row.addWidget(cur_lbl)
    cur_row.addSpacing(8)
    cur_row.addWidget(cur_val)
    cur_row.addStretch()
    ver_layout.addLayout(cur_row)

    # 구분선 한 줄 (subtle)
    sep = QFrame()
    sep.setFrameShape(QFrame.HLine)
    sep.setStyleSheet("color:#f3f4f6; background:#f3f4f6; max-height:1px;")
    ver_layout.addWidget(sep)

    # 4개 bump 옵션 행 — 각 행 클릭 시 즉시 dialog accept + 해당 버전으로
    result = {"version": None}

    def _pick(kind: str):
        if kind == "keep":
            result["version"] = current if current.startswith("v") else f"v{current}"
        else:
            result["version"] = _bump(current, kind)
        dlg.accept()

    bump_options = [
        ("patch", _bump(current, "patch"),
         "버그 수정 · 하위 호환 100%",
         "1.0.0 → 1.0.1"),
        ("minor", _bump(current, "minor"),
         "하위 호환되는 기능 추가 (신규 API/기능)",
         "1.0.0 → 1.1.0"),
        ("major", _bump(current, "major"),
         "하위 호환이 깨지는 변경 (API 제거/시그니처 변경)",
         "1.0.0 → 2.0.0"),
        ("keep",  current if current.startswith("v") else f"v{current}",
         "버전 유지 · 동일 버전 재빌드 (build_history 기록 안 함)",
         f"v{current.lstrip('vV')} 유지"),
    ]

    for kind, ver, desc, hint_text in bump_options:
        btn = QPushButton()
        btn.setObjectName("bumpBtnKeep" if kind == "keep" else "bumpBtn")
        # 다중 줄: kind / 큰 버전 / 설명
        btn.setText(f"{kind.upper():<6}   {ver}\n{desc}    ({hint_text})")
        btn.setMinimumHeight(58)
        btn.clicked.connect(lambda _checked=False, k=kind: _pick(k))
        ver_layout.addWidget(btn)

    # ── 취소 버튼 ──
    btn_row = QHBoxLayout()
    btn_row.setContentsMargins(0, 4, 0, 0)
    cancel_btn = QPushButton("취소")
    cancel_btn.setObjectName("ghost")
    btn_row.addStretch()
    btn_row.addWidget(cancel_btn)
    root.addLayout(btn_row)

    cancel_btn.clicked.connect(dlg.reject)

    if dlg.exec() != QDialog.Accepted:
        return None
    # Linux 만 .deb 옵션 의미 있음. Windows 는 항상 True 로 무시됨 (호출처에서 target 검사).
    return (target, result["version"], cb_frontend.isChecked(), cb_push.isChecked(), cb_deb.isChecked())


# ── release.py 에서 이식: build dispatch + deploy push ──
def _run_target_build(target: str, version: str, do_frontend: bool = True, make_deb: bool = True) -> int:
    """대상 OS 의 빌드 본체 호출. exit code 반환.

    do_frontend=False 면:
      - Windows: step_build_frontend 스킵, 기존 frontend/dist 그대로 패키징
      - Linux:   build_deb.sh 에 --no-frontend 인자 전달
    make_deb=False (Linux 만 의미):
      - build_deb.sh 에 --no-deb 전달 → dist/ReplayKit/ 만 스테이징, .deb 안 만듦.
    """
    host = _host_os()
    if target == "win":
        if host != "win":
            print(f"[ERROR] Windows 빌드는 Windows 호스트에서만 가능 (current: {host})", file=sys.stderr)
            return 2
        return _run_native_win_build(version, do_frontend=do_frontend)
    if target == "linux":
        if host != "linux":
            print(f"[ERROR] Linux 빌드는 Linux 호스트에서만 가능 (current: {host})", file=sys.stderr)
            return 2
        sh = PROJECT_ROOT / "scripts" / "build_deb.sh"
        if not sh.is_file():
            print(f"[ERROR] {sh} not found", file=sys.stderr)
            return 1
        cmd = ["bash", str(sh)]
        if not do_frontend:
            cmd.append("--no-frontend")
        if not make_deb:
            cmd.append("--no-deb")
        print(f"\n[BUILD] $ {' '.join(cmd)}\n")
        return subprocess.call(cmd, cwd=PROJECT_ROOT)
    print(f"[ERROR] unknown OS target: {target}", file=sys.stderr)
    return 1


def _run_native_win_build(version: str, do_frontend: bool = True) -> int:
    """기존 build_dist.py 의 Windows 빌드 단계 (Cython → [frontend] → 패키지 → installer).

    do_frontend=False 면 npm run build 스킵 — frontend/dist 이미 빌드돼 있을 때 시간 단축.
    """
    if sys.platform != "win32":
        print("[ERROR] _run_native_win_build 는 Windows 에서만 실행 가능", file=sys.stderr)
        return 2
    new_version = version.lstrip("vV")
    current_version = _read_version().lstrip("vV")
    version_changed = new_version != current_version
    if version_changed:
        _write_version(f"v{new_version}")
        print(f"  version.txt: v{current_version} → v{new_version}")
    _update_installer_iss(new_version)

    t_start = time.time()
    print("=" * 50)
    fe_tag = "with frontend" if do_frontend else "no frontend"
    print(f"  ReplayKit — Windows 배포 빌드 v{new_version}  ({fe_tag})")
    print("=" * 50)
    if not step_compile_backend(False):
        print("\n빌드 중단: backend 컴파일 실패")
        return 1
    if do_frontend:
        if not step_build_frontend(False):
            print("\n빌드 중단: frontend 빌드 실패")
            return 1
    else:
        # frontend/dist 가 없으면 패키징이 깨짐 — 사전 검증.
        fe_dist = PROJECT_ROOT / "frontend" / "dist" / "index.html"
        if not fe_dist.is_file():
            print(f"\n[ERROR] frontend 빌드를 스킵했지만 {fe_dist} 가 없음 — 먼저 빌드 필요", file=sys.stderr)
            print("        해결: GUI 에서 'frontend 빌드 포함' 체크 또는 cd frontend && npm run build", file=sys.stderr)
            return 1
        print("[FRONTEND] 스킵 (--skip-frontend / GUI 체크 해제) — 기존 frontend/dist 사용")
    if not step_package(False, offline=False):
        print("\n빌드 중단: 패키지 조립 실패")
        return 1
    clean()
    if version_changed:
        _record_build_history(new_version)
    elapsed = time.time() - t_start
    print(f"\n{'=' * 50}")
    print(f"  빌드 완료! v{new_version} ({elapsed:.1f}s)")
    print(f"  배포 폴더: {DIST_DIR}")
    print(f"{'=' * 50}")
    return 0


def _ensure_dist_git(target: str) -> bool:
    """dist/ReplayKit 의 git repo 를 보장.

    - DIST_DIR/.git 없으면 init (main branch).
    - DIST_PUSH_REMOTES[target] 의 모든 (name, url) 을 등록/sync.
    - PROJECT_ROOT/.git (소스 sync 용 ReplayKit_Universal) 과는 완전히 분리됨.
    """
    remotes = DIST_PUSH_REMOTES.get(target, [])
    if not remotes:
        print(f"[ERROR] no dist push remotes for OS={target}", file=sys.stderr)
        return False
    if not DIST_DIR.exists():
        print(f"[ERROR] dist 폴더 없음 — 빌드 먼저 필요: {DIST_DIR}", file=sys.stderr)
        return False

    if not (DIST_DIR / ".git").exists():
        r = subprocess.run(["git", "init", "-b", "main"], cwd=DIST_DIR)
        if r.returncode != 0:
            # 구버전 git 호환 — -b 플래그 미지원 시 init 후 branch -M main
            subprocess.run(["git", "init"], cwd=DIST_DIR, check=True)
            subprocess.run(["git", "checkout", "-b", "main"], cwd=DIST_DIR, check=False)
        print(f"  dist/.git 초기화")
        # safe.directory 등록 (Windows 권한 이슈 회피 — ReplayKit.bat 와 동일 패턴)
        safe_dir = str(DIST_DIR).replace("\\", "/")
        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", safe_dir],
            check=False,
        )

    for name, url in remotes:
        r = subprocess.run(
            ["git", "remote", "get-url", name],
            cwd=DIST_DIR, capture_output=True, encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            subprocess.run(["git", "remote", "add", name, url], cwd=DIST_DIR, check=True)
            print(f"  remote '{name}' 추가: {url}")
        elif r.stdout.strip() != url:
            subprocess.run(["git", "remote", "set-url", name, url], cwd=DIST_DIR, check=True)
            print(f"  remote '{name}' URL 갱신: {url}")

    # DIST_PUSH_REMOTES 에 없는 stale remote (예: 과거 'github') 정리.
    # 이름만 매칭 — 의도하지 않은 remote 가 남아 다음 사용자가 수동 push 했을 때
    # 잘못된 위치로 가는 사고 방지.
    wanted = {name for name, _ in remotes}
    r = subprocess.run(["git", "remote"], cwd=DIST_DIR, capture_output=True, encoding="utf-8", errors="replace")
    if r.returncode == 0:
        for existing in r.stdout.split():
            if existing and existing not in wanted:
                subprocess.run(["git", "remote", "remove", existing], cwd=DIST_DIR, check=False)
                print(f"  stale remote '{existing}' 제거")
    return True


def _deploy_force_push(target: str, version: str | None = None) -> int:
    """dist/ReplayKit 의 빌드 산출물을 OS 별 모든 배포 remote 로 force push.

    동작:
      1. dist/.git 보장 (_ensure_dist_git) — 첫 빌드 시 자동 init + remote 등록.
      2. add -A + commit (변경 있을 때만, 메시지에 version 포함).
      3. branch -M main 으로 브랜치 고정.
      4. DIST_PUSH_REMOTES[target] 의 모든 remote 로 차례로 force push.
         일부 실패해도 나머지 remote 는 시도 (LG/GitHub 한쪽만 네트워크 문제일 때 부분 성공).
    반환: 모든 remote 성공 = 0, 일부라도 실패 = 1.
    """
    if not _ensure_dist_git(target):
        return 1
    remotes = DIST_PUSH_REMOTES[target]

    # stage
    subprocess.run(["git", "add", "-A"], cwd=DIST_DIR, check=False)

    # 변경 있을 때만 commit (없으면 기존 HEAD 그대로 push)
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=DIST_DIR, capture_output=True, encoding="utf-8", errors="replace",
    )
    if r.stdout.strip():
        # 개발 git (PROJECT_ROOT/.git) 의 HEAD commit subject 를 가져와서 dist commit 메시지에
        # 그대로 사용. 사용자가 dist push 로그를 봤을 때 "어떤 변경이 배포된 건지" 알 수 있게.
        # 버전만 표기하면 동일 버전 내 여러 빌드가 모두 "Release v1.1.0" 으로만 보여서 구분 불가.
        dev_subject = ""
        try:
            log_r = subprocess.run(
                ["git", "log", "-1", "--pretty=format:%s"],
                cwd=PROJECT_ROOT, capture_output=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            if log_r.returncode == 0:
                dev_subject = (log_r.stdout or "").strip()
        except Exception:
            pass

        if dev_subject and version:
            msg = f"Release {version}: {dev_subject}"
        elif dev_subject:
            msg = dev_subject
        elif version:
            msg = f"Release {version}"
        else:
            msg = "Update build"

        # 첫 commit 시 user.name/email 누락으로 실패할 수 있어 fallback config 적용.
        commit_r = subprocess.run(
            ["git", "commit", "-m", msg],
            cwd=DIST_DIR, capture_output=True, encoding="utf-8", errors="replace",
        )
        if commit_r.returncode != 0 and "Please tell me who you are" in (commit_r.stderr or ""):
            print("  dist git user 미설정 — 로컬 fallback (ReplayKit Build / build@local) 적용")
            subprocess.run(["git", "config", "user.name", "ReplayKit Build"], cwd=DIST_DIR, check=False)
            subprocess.run(["git", "config", "user.email", "build@local"], cwd=DIST_DIR, check=False)
            commit_r = subprocess.run(
                ["git", "commit", "-m", msg],
                cwd=DIST_DIR, capture_output=True, encoding="utf-8", errors="replace",
            )
        if commit_r.returncode == 0:
            print(f"  dist commit: {msg}")
        else:
            print(f"  dist commit 실패: {(commit_r.stderr or commit_r.stdout or '').strip()[:200]}")
    else:
        print("  dist 변경 없음 — 기존 HEAD push")

    # 브랜치 main 고정 (init 직후 HEAD 가 분리되어 있거나 master 인 경우 보정)
    subprocess.run(["git", "branch", "-M", "main"], cwd=DIST_DIR, check=False)

    # Push subprocess 환경 정리 — VS Code / GUI askpass helper 가 prompt 를 띄우려다
    # 터미널에 보이지 않아 영구 hang 되는 사고 방지. credential.helper (store/cache) 가
    # 캐시한 자격증명을 사용하거나, 터미널에서 직접 prompt 받게 함.
    push_env = os.environ.copy()
    push_env.pop("GIT_ASKPASS", None)
    push_env.pop("SSH_ASKPASS", None)
    push_env["GIT_TERMINAL_PROMPT"] = "1"

    failed: list[str] = []
    for name, url in remotes:
        print(f"\n[PUSH] {OS_LABELS[target]} dist → {name} ({url})")
        rc = subprocess.run(
            ["git", "-c", "core.askPass=", "push", "--force", "--progress", name, "main"],
            cwd=DIST_DIR, env=push_env,
        ).returncode
        if rc == 0:
            print(f"[PUSH] OK — {name}")
        else:
            print(f"[PUSH] FAIL — {name}  (수동: cd {DIST_DIR} && git push --force {name} main)", file=sys.stderr)
            print(f"        인증 실패 시: git config --global credential.helper store  실행 후 한 번 수동 push 로 자격증명 캐시", file=sys.stderr)
            failed.append(name)

    if failed:
        print(f"\n[PUSH] {len(failed)}/{len(remotes)} remote 실패: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"\n[PUSH] OK — {len(remotes)}개 remote 모두 갱신.")
    return 0


# 기존 _prompt_version_modal 호환 stub — _bump 등 외부 참조 보존용 (deprecated).
def _prompt_version_modal():
    """Deprecated: PySide6 _show_release_dialog 로 통합됨. 직접 호출 금지."""
    out = _show_release_dialog()
    if out is None:
        return None
    target, version, do_build, do_push = out
    return version.lstrip("vV")


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
    """Frontend 무조건 빌드 (npm install + npm run build).

    옛 dist 가 push 되어 사용자가 옛 UI 받는 사고를 막기 위해 hash-based skip 제거.
    npm install 은 package.json/lock 해시로 스킵 가능 (의미 있는 시간 절약).
    """
    print("\n=== [2/3] Frontend 빌드 ===")
    fe_dir = PROJECT_ROOT / "frontend"
    hashes = _load_hashes()

    # npm install: package.json + lock 해시 — 의존성 변경 없으면 스킵해도 안전.
    pkg_hash = _hash_file(fe_dir / "package.json") + _hash_file(fe_dir / "package-lock.json")
    if force or pkg_hash != hashes.get("fe_pkg") or not (fe_dir / "node_modules").exists():
        print("  npm install...")
        _run([NPM_CMD, "install"], cwd=fe_dir, check=False)
        hashes["fe_pkg"] = pkg_hash
    else:
        print("  npm install — skipped (package.json 변경 없음)")

    # npm run build — 무조건 실행. src 해시 검사로 스킵하던 로직 제거.
    print("  npm run build...")
    result = _run([NPM_CMD, "run", "build"], cwd=fe_dir, check=False)
    if result.returncode != 0:
        print(f"  빌드 에러:\n{result.stderr[:500]}")
        return False

    # src 해시는 더 이상 skip 결정에 사용 안 하지만, 로그/디버깅용으로 계속 기록.
    hashes["fe_src"] = _hash_dir(fe_dir / "src", {".ts", ".tsx", ".css", ".html"})
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
    ("tools/scrcpy-server.jar", "scrcpy 서버 v1.25 (자동차 IVI/구 Android H.264 미러링)", True),
    ("tools/scrcpy-server-v3.3.4.jar", "scrcpy 서버 v3.3.4 (Android 14+ 일반 폰 H.264 미러링)", True),
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
    # 루트 wheel/installer는 선택적 — 없어도 빌드는 통과.
    # lge.auto wheel 은 host OS 별 휠만 검사 (양쪽 휠이 함께 있어도 다른 OS 휠은 무시).
    host = _host_os()
    lge_whl_pattern = "lge.auto-*-win_amd64.whl" if host == "win" else "lge.auto-*-linux_*.whl"
    optional_root_globs = [
        (lge_whl_pattern, f"lge.auto Python wheel ({host})"),
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
        print("    - scrcpy-server*.jar: tools/ 폴더에 미리 복사 (v1.25 + v3.3.4 둘 다)")
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
    # 단, host OS 와 맞지 않는 lge.auto wheel 은 push 잔재 방지를 위해 제거.
    host = _host_os()
    for item in list(DIST_DIR.iterdir()):
        # OS-cross lge.auto wheel 정리 — Windows 빌드 중 Linux wheel 잔재 (반대도 동일) 제거.
        if item.is_file() and item.name.startswith("lge.auto-") and item.suffix == ".whl":
            if host == "win" and "linux_" in item.name:
                item.unlink()
                print(f"  cross-OS wheel 제거: {item.name}")
                continue
            if host == "linux" and "win_" in item.name:
                item.unlink()
                print(f"  cross-OS wheel 제거: {item.name}")
                continue
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

    # ── git_remote.txt / git_remote_home.txt / .offline_mode ──
    # git_remote.txt: 사내 사용자 자동 pull URL (LG GitLab).
    # git_remote_home.txt: 과거 외부(GitHub) 배포용. GitHub 푸시 제거 이후로는 항상 정리.
    #   ReplayKit.bat --home 모드는 파일 부재 시 기본(git_remote.txt) 로 폴백.
    git_remote_file = DIST_DIR / "git_remote.txt"
    git_remote_home_file = DIST_DIR / "git_remote_home.txt"
    offline_marker = DIST_DIR / ".offline_mode"
    host = _host_os()
    # GitHub 배포 채널 제거됨 — home remote 파일은 어떤 모드에서든 항상 정리.
    if git_remote_home_file.exists():
        git_remote_home_file.unlink()
        print(f"  git_remote_home.txt 제거 (GitHub 배포 없음)")
    if offline:
        # 오프라인 모드: 자동 git pull 차단, 마커 파일 생성
        if git_remote_file.exists():
            git_remote_file.unlink()
            print(f"  오프라인 모드: git_remote.txt 제거")
        offline_marker.write_text(
            "# 이 파일이 있으면 ReplayKit.bat / setup.bat이 네트워크 액세스를\n"
            "# 시도하지 않습니다. 삭제하면 온라인 모드로 동작합니다.\n",
            encoding="utf-8",
        )
        print("  오프라인 모드: .offline_mode 마커 생성")
    else:
        # 온라인 모드: 마커 제거 + DIST_PUSH_REMOTES 기반 자동 채움 (LGE 만)
        if offline_marker.exists():
            offline_marker.unlink()
        lge_url = _remote_url(host, "lge")
        if lge_url:
            git_remote_file.write_text(lge_url, encoding="utf-8")
            print(f"  git_remote.txt → {lge_url}")
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
    # Windows 빌드 — Windows 호환 wheel 만 (Linux 휠이 함께 있어도 거름).
    # lge.auto-*-win_amd64.whl 매칭. Linux 빌드 (build_deb.sh) 는 자체 staging 으로 처리.
    patterns = [
        "lge.auto-*-win_amd64.whl",
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
# lge.auto wheel 만 추적 허용 — setup.bat/setup.sh 가 embedded Python 에 설치.
# 양 OS wheel (lge.auto-*-win_amd64.whl, lge.auto-*-linux_*.whl) 모두 포함.
!lge.auto-*.whl
get-pip.py
DltViewerSDK_21.1.3_ver/
# tools/ 폴더 — 기본 ignore. 단, webcam 녹화/미러링에 필수인 binary 는 추적 허용.
# 이전엔 ffmpeg.exe(97MB)를 제외했으나 사용자가 git pull 로 dist 받는 경우 ffmpeg.exe 가
# 없어 webcam 녹화가 cv2.VideoWriter(mp4v) fallback → 브라우저(SRC_NOT_SUPPORTED) 디코드
# 실패가 발생. negative pattern 으로 git 에 포함시킴.
# "tools/" 가 아닌 "tools/*" 로 디렉토리 내용만 ignore 해야 negative 패턴이 적용된다는 git 동작에 주의.
tools/*
!tools/scrcpy-server.jar
!tools/scrcpy-server-v3.3.4.jar
!tools/ffmpeg.exe
!tools/ffmpeg
# 동봉 adb (platform-tools) — 전 PC 동일 버전 보장. 미포함 시 배포 PC가 PATH adb 로
# 폴백해 PC별 캡처 깨짐("Cannot decode screenshot")이 재발. Windows는 DLL 2개 필수.
!tools/platform-tools
!tools/platform-tools/adb
!tools/platform-tools/adb.exe
!tools/platform-tools/AdbWinApi.dll
!tools/platform-tools/AdbWinUsbApi.dll

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


# ── 배포 repo (legacy --init-deploy / --deploy / --deploy-only 경로) ──
# 신규 통합 release 흐름(_deploy_force_push)이 DIST_PUSH_REMOTES 기반으로
# dist/.git 을 자동 init + multi-remote force push 하므로 init_deploy/deploy 는
# 사실상 backward-compat 용. legacy CLI 플래그를 쓰는 외부 스크립트만 의존.

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
    """Cross-platform entry point.

    동작 모드:
      a) GUI (default, 인자 없음): PySide6 _show_release_dialog 띄움 → OS/버전/옵션 선택
         → 해당 OS 빌드 + 배포 git force push.
      b) CLI 자동 (--os / --version 지정): 인터랙티브 없이 즉시 빌드/push.
      c) 레거시 (--clean / --init-deploy / --deploy / --backend / --full / --offline):
         기존 build_dist.py 동작 보존 (Windows 호스트 전용).
    Linux 에서도 GUI/CLI release 모드 사용 가능 — Linux 선택 시 scripts/build_deb.sh 호출.
    """
    args_list = list(sys.argv[1:])

    # --os / --version 분리 추출 (값을 가진 인자)
    cli_os: str | None = None
    cli_version: str | None = None
    i = 0
    while i < len(args_list):
        a = args_list[i]
        if a == "--os" and i + 1 < len(args_list):
            cli_os = args_list[i + 1].lower()
            del args_list[i:i + 2]
            continue
        if a == "--version" and i + 1 < len(args_list):
            cli_version = args_list[i + 1].lstrip("vV")
            del args_list[i:i + 2]
            continue
        i += 1
    args = set(args_list)
    force = "--full" in args
    offline = "--offline" in args
    do_skip_build = "--skip-build" in args
    do_skip_push = "--skip-push" in args
    do_skip_frontend = "--skip-frontend" in args
    do_skip_deb = "--no-deb" in args  # Linux 만 의미 — dist/ReplayKit/ 만 만들고 .deb 생략
    legacy_deploy = "--deploy" in args

    # ── 레거시 명령 (Windows 호스트 전용 — 기존 build_dist.py 호환) ──
    if "--clean" in args:
        clean()
        return
    if "--init-deploy" in args:
        if sys.platform != "win32":
            print("[ERROR] --init-deploy 는 Windows 빌드 전용. Linux 는 build_deb.sh 자동 push.", file=sys.stderr)
            sys.exit(2)
        init_deploy()
        return
    if "--deploy-only" in args:
        if sys.platform != "win32":
            print("[ERROR] --deploy-only 는 Windows 빌드 전용.", file=sys.stderr)
            sys.exit(2)
        deploy()
        return
    if "--backend" in args:
        if sys.platform != "win32":
            print("[ERROR] --backend 는 Windows Cython 컴파일 전용.", file=sys.stderr)
            sys.exit(2)
        new_v = cli_version or _read_version().lstrip("vV")
        ok = step_compile_backend(force)
        clean()
        if ok and new_v != _read_version().lstrip("vV"):
            _record_build_history(new_v)
        return

    # ── 통합 release 모드 ──
    # GUI: 인자 없거나 --os 만 지정 (버전 prompt 필요)
    # CLI: --os AND --version 둘 다 지정
    if cli_os and cli_version:
        # 완전 자동 모드 — CLI 가 --os 지정해도 host 와 다르면 거부 (GUI 와 동일 정책: target=host 고정).
        requested = "linux" if cli_os in ("linux", "lin", "l", "2") else "win"
        host = _host_os()
        if requested != host:
            print(f"[ERROR] target {requested!r} 가 host {host!r} 와 다릅니다. host OS 에서만 빌드 가능.", file=sys.stderr)
            sys.exit(2)
        target = requested
        version = f"v{cli_version}"
        do_build = not do_skip_build
        do_frontend = not do_skip_frontend
        do_push = not do_skip_push
        make_deb = not do_skip_deb
        print(f"[CLI] OS={OS_LABELS[target]}, version={version}, build={do_build}, frontend={do_frontend}, push={do_push}, make_deb={make_deb}")
    else:
        # GUI dialog — 버전 클릭 = 즉시 빌드 시작. 빌드는 항상 실행 (불필요한 토글 제거).
        out = _show_release_dialog()
        if out is None:
            print("\n[ABORT] 사용자 취소")
            return
        target, version, do_frontend, do_push, make_deb = out
        do_build = True

    # version.txt 갱신
    current = _read_version()
    if version != current:
        _write_version(version)
        print(f"  version.txt: {current} → {version}")
    else:
        print(f"  version.txt: {version} (유지)")

    # 빌드
    if do_build:
        rc = _run_target_build(target, version, do_frontend=do_frontend, make_deb=make_deb)
        if rc != 0:
            print(f"\n[ABORT] 빌드 실패 (exit {rc}) — push 건너뜀.", file=sys.stderr)
            sys.exit(rc)
        # dist/ReplayKit/ 가 채워진 후 .gitignore 작성. Windows native build 는 step_package_dist 가
        # 이미 호출하지만 Linux build_deb.sh 는 안 하므로 여기서 일괄 보장.
        if target == "linux":
            _write_dist_gitignore()
    else:
        print("[BUILD] skipped")

    # 배포 push — DIST_PUSH_REMOTES 의 remote (LGE) 로 dist 산출물 force push.
    # legacy --deploy 도 신규 흐름으로 흡수 (별도 deploy() 호출 불필요 — dist/.git 자동 init 처리됨).
    if do_push or legacy_deploy:
        rc = _deploy_force_push(target, version)
        if rc != 0:
            sys.exit(rc)
    else:
        print("[PUSH] skipped")

    print(f"\n=== Release 완료: {OS_LABELS[target]} {version} ===")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n[ABORT] 사용자 취소 (Ctrl+C)", file=sys.stderr)
        sys.exit(130)
