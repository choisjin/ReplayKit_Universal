@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Isolate embedded Python from system Python env vars (prevents cv2 DLL load conflicts)
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONSTARTUP="
set "PYTHONNOUSERSITE=1"

REM Ensure Git is on PATH
set "PATH=C:\Program Files\Git\cmd;C:\Program Files (x86)\Git\cmd;%PATH%"

REM Stop existing server BEFORE git pull / pip install - required to release .pyd locks
call :stop_existing_server

REM ============================================================
REM  Offline mode detection
REM  - .offline_mode 파일이 있으면 모든 네트워크 액세스 시도 스킵
REM    (git pull, pip install, paddlepaddle/OCR 모델 다운로드)
REM  - build_dist.py --offline 로 빌드한 배포본이 이 파일을 동봉함
REM ============================================================
set "OFFLINE_MODE=0"
if exist ".offline_mode" (
    set "OFFLINE_MODE=1"
    echo [OFFLINE] .offline_mode detected - network access disabled.
)

REM --home option: use git_remote_home.txt instead of git_remote.txt
set "GIT_REMOTE_FILE=git_remote.txt"
if "%~1"=="--home" (
    if exist "git_remote_home.txt" (
        set "GIT_REMOTE_FILE=git_remote_home.txt"
        echo [GIT] Using home remote: git_remote_home.txt
    ) else (
        echo [GIT] git_remote_home.txt not found - using default.
    )
)

REM Canonical remote URL - auto-correct if origin differs
set "CANONICAL_REMOTE=http://mod.lge.com/hub/dqa_replay_kit/replay_kit.git"

REM Git init or update (오프라인 모드에선 통째로 스킵)
if "%OFFLINE_MODE%"=="1" (
    echo [GIT] Skipped ^(offline mode^).
    goto :after_git
)
if not exist ".git" (
    if exist "%GIT_REMOTE_FILE%" (
        where git.exe >nul 2>nul
        if not errorlevel 1 (
            call :git_init
        ) else (
            echo [GIT] Git not found - skipping.
        )
    )
) else (
    where git.exe >nul 2>nul
    if not errorlevel 1 (
        if /i not "%~1"=="--home" call :fix_remote
        call :git_pull
    )
)
goto :after_git

:git_init
echo [GIT] Initializing repository...
set /p GIT_REMOTE=<%GIT_REMOTE_FILE%
set "SAFE_DIR=%CD:\=/%"
git init -b main
git config --global --add safe.directory "%SAFE_DIR%"
git remote add origin "%GIT_REMOTE%"
git fetch --depth 1 origin main
if errorlevel 1 (
    echo [GIT] Fetch failed - check network.
    goto :eof
)
git branch --set-upstream-to=origin/main main
git reset origin/main
git checkout origin/main -- .gitignore
echo [GIT] Initialized: %GIT_REMOTE%
goto :eof

:fix_remote
REM Correct origin URL if it does not match the canonical address
set "SAFE_DIR=%CD:\=/%"
for /f "delims=" %%u in ('git -c safe.directory="%SAFE_DIR%" remote get-url origin 2^>nul') do set "CUR_REMOTE=%%u"
if not "!CUR_REMOTE!"=="!CANONICAL_REMOTE!" (
    git -c safe.directory="%SAFE_DIR%" remote set-url origin "!CANONICAL_REMOTE!"
    echo [GIT] Remote corrected: !CUR_REMOTE! -^> !CANONICAL_REMOTE!
)
goto :eof

:git_pull
REM In --home mode, do not touch origin URL; just fetch+reset against current setting
set "SAFE_DIR=%CD:\=/%"
git -c safe.directory="%SAFE_DIR%" fetch origin main
git -c safe.directory="%SAFE_DIR%" reset --hard origin/main
echo [GIT] Updated.
goto :eof

:after_git

REM Auto dependency update - pip install only when requirements.txt changed
REM Uses .req_hash pattern same as build_dist.py (python\.req_hash)
REM 오프라인 모드: dist에 포함된 site-packages를 신뢰하고 pip install 시도 안 함
if "%OFFLINE_MODE%"=="1" (
    echo [DEPS] Skipped ^(offline mode^).
) else (
    if exist "python\python.exe" if exist "requirements.txt" call :update_deps
)

REM Auto OCR multilingual model install (first boot only)
REM 오프라인 모드: dist에 OCR 모델이 동봉되어 있어야 함 (build_dist.py --offline 검증)
if "%OFFLINE_MODE%"=="1" (
    echo [OCR] Skipped ^(offline mode - models must be bundled in dist^).
) else (
    if exist "python\python.exe" if exist "scripts\download_ocr_models.py" call :update_ocr_models
)

goto :start_server

REM ------------------------------------------------------------
REM  stop_existing_server: kill running backend / frontend / python
REM ------------------------------------------------------------
:stop_existing_server
echo [STOP] Checking for running server...
REM 1) Kill backend (port 8000)
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr /r /c:":8000 .*LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
    if not errorlevel 1 echo [STOP] Killed backend PID %%p ^(port 8000^)
)
REM 2) Kill frontend Vite dev server (port 5173)
for /f "tokens=5" %%p in ('netstat -ano 2^>nul ^| findstr /r /c:":5173 .*LISTENING"') do (
    taskkill /F /PID %%p >nul 2>&1
    if not errorlevel 1 echo [STOP] Killed frontend PID %%p ^(port 5173^)
)
REM 3) Kill python(w).exe running server.py / _launcher.py (covers tray/GUI mode)
where powershell.exe >nul 2>&1
if not errorlevel 1 (
    powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" | Where-Object { $_.CommandLine -match 'server\.py|_launcher\.py' } | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; Write-Host ('[STOP] Killed Python PID ' + $_.ProcessId) } catch {} }" 2>nul
)
REM 4) Wait for port release (TIME_WAIT)
timeout /t 1 /nobreak >nul 2>&1
goto :eof

:update_deps
set "REQ_HASH_FILE=python\.req_hash"
set "OLD_HASH="
if exist "%REQ_HASH_FILE%" set /p OLD_HASH=<"%REQ_HASH_FILE%"
set "NEW_HASH="
for /f "skip=1 tokens=1" %%h in ('certutil -hashfile "requirements.txt" SHA256 2^>nul') do (
    if not defined NEW_HASH set "NEW_HASH=%%h"
)
if not defined NEW_HASH goto :eof

REM Verify critical modules can be imported - safety net for missing entries in requirements.txt.
REM If any are missing, force reinstall even when the hash matches.
set "NEED_INSTALL="
if /i not "!NEW_HASH!"=="!OLD_HASH!" set "NEED_INSTALL=1"
python\python.exe -c "import rapidocr_onnxruntime, rapidfuzz" >nul 2>nul
if errorlevel 1 (
    set "NEED_INSTALL=1"
    set "CRITICAL_MISSING=1"
)
if not defined NEED_INSTALL goto :eof

echo [DEPS] Installing/updating packages...
python\python.exe -E -s -m pip install -r requirements.txt --no-warn-script-location -q
if errorlevel 1 (
    echo [DEPS] Install failed - continuing with existing packages.
    goto :eof
)

REM If critical modules are still missing (not listed in requirements.txt), install them directly
if defined CRITICAL_MISSING (
    python\python.exe -c "import rapidocr_onnxruntime, rapidfuzz" >nul 2>nul
    if !errorlevel! NEQ 0 (
        echo [DEPS] Critical modules still missing - installing directly...
        python\python.exe -E -s -m pip install rapidocr-onnxruntime rapidfuzz --no-warn-script-location -q
    )
)

>"%REQ_HASH_FILE%" echo !NEW_HASH!
echo [DEPS] Dependencies updated.
goto :eof

REM ------------------------------------------------------------
REM  update_ocr_models: download multilingual OCR rec models on first boot
REM  - Sentinel file: backend\app\services\ocr_models\korean\rec_infer.onnx
REM  - If present  -> skip (already installed)
REM  - If missing  -> install paddle2onnx (one-time) + run download script
REM  - Failures are non-fatal; server starts anyway
REM ------------------------------------------------------------
:update_ocr_models
set "OCR_SENTINEL=backend\app\services\ocr_models\korean\rec_infer.onnx"
if exist "%OCR_SENTINEL%" goto :eof
echo [OCR] Multilingual models not found - first-time setup...
echo [OCR]   ^(downloads ~40MB models + ~150MB paddlepaddle one-time^)
REM paddle2onnx + paddlepaddle 둘 다 필요 (paddle2onnx __init__이 paddle import)
python\python.exe -m paddle2onnx.command --version >nul 2>nul
if errorlevel 1 (
    echo [OCR] Installing paddle2onnx + paddlepaddle ^(one-time, ~160MB^)...
    python\python.exe -E -s -m pip install paddle2onnx paddlepaddle --no-warn-script-location -q
    if errorlevel 1 (
        echo [OCR] paddle2onnx/paddlepaddle install failed - skipping OCR model setup.
        echo [OCR] Korean OCR will fall back to bundled Chinese model.
        echo [OCR] To install manually later:
        echo [OCR]   python\python.exe -m pip install paddle2onnx paddlepaddle
        echo [OCR]   python\python.exe scripts\download_ocr_models.py
        goto :eof
    )
)
REM 기본 4종(korean/english/japan/chinese) 다운로드 + ONNX 변환
python\python.exe scripts\download_ocr_models.py
if errorlevel 1 (
    echo [OCR] Model download partially failed - check logs above.
    echo [OCR] Missing languages fall back to bundled Chinese model.
)
goto :eof

:start_server
set "ENTRY=server.py"
if exist "_launcher.py" set "ENTRY=_launcher.py"

set "PY="
set "PYW="
if exist "python\pythonw.exe" set "PYW=python\pythonw.exe"
if exist "python\python.exe" set "PY=python\python.exe"
if not defined PYW if exist "venv\Scripts\pythonw.exe" set "PYW=venv\Scripts\pythonw.exe"
if not defined PY if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"

if not defined PYW if not defined PY (
    echo [ERROR] Python not found. Run setup.bat first.
    pause
    exit /b 1
)

if defined PYW (
    echo [START] %PYW% %ENTRY%
    start "" "%PYW%" %ENTRY%
) else (
    echo [START] %PY% %ENTRY%
    start "" cmd /c ""%PY%" %ENTRY% || (echo. & echo [ERROR] Server crashed. Press any key to close. & pause >nul)"
)
