@echo off
chcp 65001 >nul
echo ============================================
echo   ReplayKit - Setup
echo ============================================
echo.

cd /d "%~dp0"

:: Detect production mode
set "PRODUCTION=0"
if exist "frontend\dist\index.html" (
    if not exist "frontend\package.json" set "PRODUCTION=1"
)

:: Offline mode detection - .offline_mode 파일이 있으면 PyPI / npm 등 외부 액세스 차단
set "OFFLINE_MODE=0"
if exist ".offline_mode" (
    set "OFFLINE_MODE=1"
    echo       [OFFLINE] .offline_mode detected - skipping network operations.
)

:: -------------------------------------------------------
:: [1/5] Python setup
:: -------------------------------------------------------
echo [1/5] Setting up Python...

:: --- Mode A: Embedded Python (zip in current dir) ---
if exist "python\python.exe" goto :python_ready

set "EMBED_ZIP="
for %%f in (python-*-embed-amd64.zip) do set "EMBED_ZIP=%%f"
if not defined EMBED_ZIP goto :try_system_python

echo       Extracting embedded Python: %EMBED_ZIP%
mkdir python 2>nul
tar -xf "%EMBED_ZIP%" -C python
:: Enable import site (required for pip)
for %%f in (python\python*._pth) do (
    findstr /v "^#import site" "%%f" > "%%f.tmp"
    echo import site>> "%%f.tmp"
    move /y "%%f.tmp" "%%f" >nul
)
echo       Embedded Python extracted
goto :python_ready

:: --- Mode B: System Python fallback (dev mode) ---
:try_system_python
:: Refresh PATH from registry
for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%b"
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USR_PATH=%%b"
if defined SYS_PATH set "PATH=%SYS_PATH%"
if defined USR_PATH set "PATH=%PATH%;%USR_PATH%"
if exist "C:\Python310" set "PATH=C:\Python310;C:\Python310\Scripts;%PATH%"

set "PYTHON="
py -3.10 --version >nul 2>&1
if %ERRORLEVEL% equ 0 set "PYTHON=py -3.10"
if defined PYTHON goto :system_python_ok

if exist "C:\Python310\python.exe" set "PYTHON=C:\Python310\python.exe"
if defined PYTHON goto :system_python_ok

python --version >nul 2>&1
if %ERRORLEVEL% equ 0 set "PYTHON=python"
if defined PYTHON goto :system_python_ok

:: No Python at all - try bundled installer
if not exist "python-3.10.4-amd64.exe" goto :python_error
echo.
echo       Python 3.10 is not installed.
echo.
echo       ================================================
echo       Python 3.10 installer will now open.
echo       [TIP] Check "Add Python to PATH" at the bottom!
echo       ================================================
echo.
start "" /wait "python-3.10.4-amd64.exe"
echo.
echo       ------------------------------------------
echo       Installation complete. Press any key to continue...
echo       ------------------------------------------
pause >nul

for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%b"
for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USR_PATH=%%b"
if defined SYS_PATH set "PATH=%SYS_PATH%"
if defined USR_PATH set "PATH=%PATH%;%USR_PATH%"
if exist "C:\Python310" set "PATH=C:\Python310;C:\Python310\Scripts;%PATH%"

set "PYTHON="
py -3.10 --version >nul 2>&1
if %ERRORLEVEL% equ 0 set "PYTHON=py -3.10"
if defined PYTHON goto :system_python_ok
if exist "C:\Python310\python.exe" set "PYTHON=C:\Python310\python.exe"
if defined PYTHON goto :system_python_ok
python --version >nul 2>&1
if %ERRORLEVEL% equ 0 set "PYTHON=python"
if defined PYTHON goto :system_python_ok

:python_error
echo       [ERROR] Python not found. Please install Python 3.10.
pause
exit /b 1

:system_python_ok
for /f "tokens=*" %%v in ('%PYTHON% --version 2^>nul') do echo       System %PYTHON%: %%v
echo [2/5] Creating venv...
if not exist "venv" (
    %PYTHON% -m venv venv
    if not exist "venv\Scripts\python.exe" (
        echo       [ERROR] venv creation failed
        pause
        exit /b 1
    )
    echo       venv created
) else (
    echo       venv already exists - skipped
)
set "PY=venv\Scripts\python.exe"
set "PIP=venv\Scripts\pip.exe"
goto :install_packages

:: --- Embedded Python ready ---
:python_ready
set "PY=python\python.exe"
echo       Embedded Python ready

:: Install pip if not present
echo [2/5] Checking pip...
%PY% -m pip --version >nul 2>&1
if %ERRORLEVEL% equ 0 goto :pip_ok
if not exist "get-pip.py" goto :pip_ok
echo       Installing pip...
%PY% get-pip.py --no-warn-script-location -q
:pip_ok
set "PIP=%PY% -m pip"
echo       pip ready

:: -------------------------------------------------------
:: [3/5] Install packages
:: -------------------------------------------------------
:install_packages
echo [3/5] Installing Python packages...
:: Embedded Python(python/ 폴더)은 패키지가 이미 포함 — pip install 건너뜀
if exist "python\python.exe" (
    echo       Embedded Python — pip install 건너뜀 (패키지 내장)
) else (
    if "%OFFLINE_MODE%"=="1" (
        echo       [OFFLINE] PyPI install skipped - system Python must have packages pre-installed.
    ) else (
        %PY% -m pip install --upgrade pip -q --no-warn-script-location 2>nul
        %PIP% install -r requirements.txt -q --no-warn-script-location
    )
)
:: lge.auto는 로컬 .whl — 오프라인 설치 가능 (PyPI 접근 불필요)
if exist "lge.auto-*.whl" (
    for %%f in (lge.auto-*.whl) do %PIP% install "%%f"
    echo       lge.auto installed
) else (
    echo       [Note] lge.auto .whl not found
)
:: vmbpy (Vimba X Python API) - install from SDK if available (로컬 .whl이라 오프라인 OK)
set "VMBPY_WHL="
for %%f in ("C:\Program Files\Allied Vision\Vimba X\api\python\vmbpy-*.whl") do set "VMBPY_WHL=%%f"
if defined VMBPY_WHL (
    %PIP% install "%VMBPY_WHL%" -q 2>nul
    echo       vmbpy installed
) else (
    echo       [Note] Vimba X SDK not found - VisionCamera IP features unavailable
)
:: DLT Viewer SDK
if exist "DltViewerSDK_21.1.3_ver\dlt-viewer.exe" (
    echo       DLT Viewer SDK found
) else (
    echo       [Note] DLT Viewer SDK not found - DLT Viewer GUI unavailable
)
:: ffmpeg
if exist "tools\ffmpeg.exe" (
    echo       ffmpeg found
) else (
    where ffmpeg.exe >nul 2>&1
    if %ERRORLEVEL% equ 0 (
        echo       ffmpeg found ^(system^)
    ) else (
        echo       [Note] ffmpeg not found - webcam trim unavailable
    )
)

:: -------------------------------------------------------
:: [4/4] Node.js (dev mode only)
:: -------------------------------------------------------
if "%PRODUCTION%"=="1" (
    echo [4/4] Production mode - skipping Node.js
    goto :skip_npm
)
if "%OFFLINE_MODE%"=="1" (
    echo [4/4] Offline mode - skipping npm install
    goto :skip_npm
)

echo [4/4] Checking Node.js...
where npm.cmd >nul 2>&1
if %ERRORLEVEL% neq 0 (
    echo.
    echo       Node.js is not installed.
    if exist "node-v24.14.0-x64.msi" (
        echo.
        echo       ================================================
        echo       Node.js installer will now open.
        echo       ================================================
        echo.
        start "" /wait msiexec /i "node-v24.14.0-x64.msi"
        echo.
        echo       ------------------------------------------
        echo       Installation complete. Press any key to continue...
        echo       ------------------------------------------
        pause >nul
        for /f "tokens=2*" %%a in ('reg query "HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment" /v Path 2^>nul') do set "SYS_PATH=%%b"
        for /f "tokens=2*" %%a in ('reg query "HKCU\Environment" /v Path 2^>nul') do set "USR_PATH=%%b"
        if defined SYS_PATH set "PATH=%SYS_PATH%"
        if defined USR_PATH set "PATH=%PATH%;%USR_PATH%"
        where npm.cmd >nul 2>&1
        if %ERRORLEVEL% neq 0 (
            echo       [Warning] Node.js not detected - frontend install skipped.
            goto :skip_npm
        )
    ) else (
        echo       Please install Node.js LTS from https://nodejs.org
        goto :skip_npm
    )
)

for /f "tokens=*" %%v in ('node --version 2^>nul') do echo       Node.js %%v detected
if exist "frontend\package.json" (
    echo       Installing frontend packages...
    cd frontend
    call npm install
    cd ..
    echo       npm install done
) else (
    echo       [Warning] frontend/package.json not found - skipped
)

:skip_npm

echo.
echo ============================================
echo   Setup complete!
if "%PRODUCTION%"=="1" (
    echo   Run ReplayKit.bat to start.
) else (
    echo   Run: python server.py
)
echo ============================================
pause
