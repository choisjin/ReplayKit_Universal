@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  MIB 0-version ksend 설치
rem   - ksend.dat 를 시료 /tmp/ksend 로 전송
rem   - /tmp 를 exec 로 remount + chmod +x
rem   - 실제 실행되는지 확인
rem  사용법: mib_ksend_install.bat [시료IP] [ksend 파일경로] [비밀번호]
rem  예)     mib_ksend_install.bat 192.168.1.4 C:\Users\user\Desktop\ksend.dat mypass
rem  ※ /tmp 는 시료 재부팅 시 비워지므로 재부팅할 때마다 다시 실행
rem ============================================================

rem ---- 비밀번호 자동입력: 아래 값을 채우면 묻지 않음 (비우면 직접 입력) ----
rem      예) set "DEFAULT_PASS=root"   ※ 느낌표는 사용 불가, 퍼센트 기호는 두 번 연속으로 적을 것
set "DEFAULT_PASS=k82dX^pNreND4R-w"

set "MIB_IP=%~1"
if "%MIB_IP%"=="" set "MIB_IP=192.168.1.4"
set "MIB_USER=root"
if "%MIB_PORT%"=="" set "MIB_PORT=22"

set "KSEND_FILE=%~2"
if "%KSEND_FILE%"=="" set "KSEND_FILE=%USERPROFILE%\Desktop\ksend.dat"

set "MIB_PASS=%~3"
if "%MIB_PASS%"=="" set "MIB_PASS=%DEFAULT_PASS%"

rem Windows 기본 OpenSSH 우선 (Git 의 ssh 는 askpass 로 .cmd 를 못 띄움)
set "SSH_EXE=%SystemRoot%\System32\OpenSSH\ssh.exe"
if not exist "%SSH_EXE%" set "SSH_EXE=ssh"
"%SSH_EXE%" -V >nul 2>nul
if errorlevel 1 (
    echo [오류] ssh 명령을 찾을 수 없습니다. Windows 설정 ^> 선택적 기능 ^> OpenSSH 클라이언트를 설치하세요.
    goto :fail
)

rem 비밀번호가 있으면 SSH_ASKPASS 로 자동 응답 (OpenSSH 8.4+ SSH_ASKPASS_REQUIRE=force).
rem 값은 환경변수로만 넘기고 파일에 쓰지 않는다 — askpass 는 지연확장으로 그대로 출력.
set "ASKPASS_CMD=%TEMP%\mib_askpass_%RANDOM%.cmd"
if not "%MIB_PASS%"=="" (
    > "%ASKPASS_CMD%" echo @setlocal enabledelayedexpansion
    >> "%ASKPASS_CMD%" echo @echo(^^!MIB_PASS^^!
    set "SSH_ASKPASS=%ASKPASS_CMD%"
    set "SSH_ASKPASS_REQUIRE=force"
    set "DISPLAY=dummy:0"
)

if not exist "%KSEND_FILE%" (
    echo [오류] ksend 파일이 없습니다: %KSEND_FILE%
    goto :fail
)

echo.
echo  시료   : %MIB_USER%@%MIB_IP%
echo  파일   : %KSEND_FILE%
echo  대상   : /tmp/ksend
echo.
if "%MIB_PASS%"=="" (
    echo  비밀번호를 물으면 입력하세요 ^(없으면 Enter^).
) else (
    echo  비밀번호: 자동 입력
)
echo.

rem 파일을 ssh 표준입력으로 흘려 cat 으로 저장 — 접속 1회, SFTP 불필요.
rem 호스트키 확인은 끔(시료 교체가 잦아 키 변경 경고로 막히는 것 방지).
"%SSH_EXE%" -o StrictHostKeyChecking=no -o UserKnownHostsFile=NUL -o ConnectTimeout=10 -p %MIB_PORT% %MIB_USER%@%MIB_IP% "mkdir -p /tmp && mount -o remount,exec /tmp ; if [ -d /tmp/ksend ] ; then rmdir /tmp/ksend 2>/dev/null || { echo KSEND_IS_DIR ; ls -la /tmp/ksend ; exit 4 ; } ; fi ; cat > /tmp/ksend && chmod +x /tmp/ksend && ls -la /tmp/ksend && if /tmp/ksend 2>&1 | grep -q denied ; then echo KSEND_DENIED ; mount | grep ' /tmp ' ; exit 3 ; else echo KSEND_RUN_OK ; fi" < "%KSEND_FILE%"
set "RC=%ERRORLEVEL%"
if exist "%ASKPASS_CMD%" del /q "%ASKPASS_CMD%" >nul 2>nul

echo.
if "%RC%"=="0" (
    echo [완료] /tmp/ksend 설치 + 실행 확인됨. ReplayKit 에서 바로 터치해 보세요.
    goto :end
)
if "%RC%"=="3" (
    echo [실패] 권한/리마운트 후에도 실행이 막혀 있습니다. 위 mount 결과를 공유해 주세요.
    goto :fail
)
if "%RC%"=="4" (
    echo [실패] 시료의 /tmp/ksend 가 파일이 아니라 폴더이고 안에 내용이 있어 덮어쓰지 않았습니다.
    echo        위 목록을 확인 후, 필요 없으면 시료에서 rm -rf /tmp/ksend 실행 뒤 다시 실행하세요.
    goto :fail
)
if "%RC%"=="255" (
    echo [실패] SSH 접속 실패 — IP/케이블/비밀번호를 확인하세요. ^(%MIB_IP%^)
    goto :fail
)
echo [실패] 원격 명령 오류 ^(exit=%RC%^) — 위 출력 내용을 확인하세요.

:fail
echo.
pause
exit /b 1

:end
echo.
pause
exit /b 0
