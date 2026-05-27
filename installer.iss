; ReplayKit Inno Setup Script
; Compile: Open in Inno Setup Compiler, press Ctrl+F9

#define MyAppName "ReplayKit"
#define MyAppVersion "1.1.0"
#define MyAppPublisher "ReplayKit"

; dist/ReplayKit path (build_dist.py output)
#define DistDir "dist\ReplayKit"

[Setup]
AppId={{B8F2A3E1-4D5C-4F6A-9E7B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName=C:\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=ReplayKit_Setup_{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
SetupIconFile={#DistDir}\replaykit.ico
UninstallDisplayIcon={app}\replaykit.ico
ShowLanguageDialog=auto

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Types]
Name: "standard"; Description: "Standard (+ DLT Viewer)"
Name: "full"; Description: "Full (+ DLT Viewer + Vision Camera)"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "main"; Description: "ReplayKit Core"; Types: standard full custom; Flags: fixed
Name: "dltsdk"; Description: "DLT Viewer SDK (DLT log monitoring)"; Types: standard full
Name: "vimbax"; Description: "Vimba X SDK (Vision Camera support)"; Types: full

[Files]
; Main project files (인스톨러 전용 바이너리 제외)
Source: "{#DistDir}\*"; DestDir: "{app}"; Excludes: "*.msi,VimbaX_Setup*,python-3.10.4-amd64.exe,Git-*.exe,vcredist_x64.exe,DltViewerSDK_21.1.3_ver"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: main
; VC++ Runtime (설치 후 삭제)
Source: "{#DistDir}\vcredist_x64.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall; Check: not IsVCRedistInstalled
; Git installer (설치 후 삭제)
Source: "{#DistDir}\Git-*.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist
; DLT Viewer SDK (컴포넌트 선택 시)
Source: "{#DistDir}\DltViewerSDK_21.1.3_ver\*"; DestDir: "{app}\DltViewerSDK_21.1.3_ver"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: dltsdk
; Vimba X SDK installer (설치 후 삭제, 컴포넌트 선택 시)
Source: "{#DistDir}\VimbaX_Setup*.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall skipifsourcedoesntexist; Components: vimbax

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\ReplayKit.bat"; IconFilename: "{app}\replaykit.ico"
Name: "{commondesktop}\{#MyAppName}"; Filename: "{app}\ReplayKit.bat"; IconFilename: "{app}\replaykit.ico"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Additional tasks:"

[Code]
function IsVCRedistInstalled: Boolean;
begin
  Result := RegKeyExists(HKLM, 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64')
         or RegKeyExists(HKLM, 'SOFTWARE\WOW6432Node\Microsoft\VisualStudio\14.0\VC\Runtimes\x64');
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDir: String;
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec('cmd.exe', '/c taskkill /f /im adb.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);

  if CurUninstallStep = usPostUninstall then
  begin
    AppDir := ExpandConstant('{app}');
    if DirExists(AppDir) then
    begin
      if MsgBox('Remove all remaining files (venv, data, git)?' + #13#10 + AppDir,
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(AppDir, True, True, True);
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ResultCode: Integer;
  VCRedist: String;
  GitInstaller: String;
  VimbaInstaller: String;
begin
  if CurStep = ssPostInstall then
  begin
    // VC++ Runtime (silent)
    if not IsVCRedistInstalled then
    begin
      VCRedist := ExpandConstant('{tmp}\vcredist_x64.exe');
      if FileExists(VCRedist) then
        Exec(VCRedist, '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
    end;

    // Vimba X SDK (user-driven install, only if component selected)
    if IsComponentSelected('vimbax') then
    begin
      VimbaInstaller := '';
      if FileExists(ExpandConstant('{tmp}\VimbaX_Setup-2025-3-Win64.exe')) then
        VimbaInstaller := ExpandConstant('{tmp}\VimbaX_Setup-2025-3-Win64.exe');
      if (VimbaInstaller <> '') then
      begin
        Log('Launching Vimba X installer...');
        Exec(VimbaInstaller, '', '', SW_SHOW, ewWaitUntilTerminated, ResultCode);
      end;
    end;

    // Git (silent install if not present)
    if not FileExists(ExpandConstant('{sys}\git.exe')) and not RegKeyExists(HKLM, 'SOFTWARE\GitForWindows') then
    begin
      GitInstaller := '';
      if FileExists(ExpandConstant('{tmp}\Git-2.53.0.2-64-bit.exe')) then
        GitInstaller := ExpandConstant('{tmp}\Git-2.53.0.2-64-bit.exe');
      if (GitInstaller <> '') then
      begin
        Log('Installing Git...');
        Exec(GitInstaller, '/VERYSILENT /NORESTART /NOCANCEL /SP- /CLOSEAPPLICATIONS /RESTARTAPPLICATIONS /COMPONENTS="icons,ext\reg\shellhere,assoc,assoc_sh"', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
      end;
    end;

    // Run setup.bat (Python 패키지 설치, Git 설정 등)
    Exec('cmd.exe', '/c "' + ExpandConstant('{app}\setup.bat') + '"',
         ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end;
end;

[Run]
Filename: "{app}\ReplayKit.bat"; Description: "Run ReplayKit"; Flags: nowait postinstall skipifsilent shellexec
