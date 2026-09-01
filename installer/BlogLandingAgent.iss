; ══════════════════════════════════════════════════════════════════
;  블로그 랜딩 Agent — Windows 설치 프로그램
;
;  · 관리자 권한 없이 설치된다(사용자 폴더).
;  · 설치가 끝나면 바로 실행되고, 다음부터는 윈도우 시작 시 자동 실행된다.
;  · 파이썬·Playwright 를 따로 깔 필요가 없다(안에 들어 있다).
;    크롬(Chromium)만 첫 실행 때 자동으로 내려받는다.
;
;  ★비밀값은 들어가지 않는다.
;    들어가는 접속 정보는 공개해도 되는 publishable 키뿐이고,
;    구글 시트 인증 파일은 사용자가 트레이 메뉴에서 직접 지정한다.
; ══════════════════════════════════════════════════════════════════
#define AppName "블로그 랜딩 Agent"
#define AppVer "1.0.0"
#define AppExe "BlogLandingAgent.exe"

[Setup]
AppId={{8E2C1B84-3F5A-4C1D-9E77-BLOGLANDING01}
AppName={#AppName}
AppVersion={#AppVer}
AppPublisher=894PLUS
DefaultDirName={localappdata}\Programs\BlogLandingAgent
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=yes
PrivilegesRequired=lowest
OutputDir=..\installer\out
OutputBaseFilename=BlogLandingAgentSetup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExe}
SetupLogging=yes

[Languages]
Name: "korean"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\installer\dist\BlogLandingAgent\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; 접속 정보(비밀 아님) — 이미 있으면 덮어쓰지 않는다(사용자 설정 보존)
Source: "..\installer\payload\agent_config.json"; DestDir: "{userappdata}\BlogLandingAgent"; \
    Flags: onlyifdoesntexist

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExe}"

[Registry]
; 윈도우 시작 시 자동 실행(사용자 계정 범위)
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "BlogLandingAgent"; \
    ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#AppExe}"; Description: "지금 {#AppName} 실행"; \
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; 제거 전에 돌고 있는 Agent 를 끈다
Filename: "{cmd}"; Parameters: "/C taskkill /F /IM {#AppExe}"; Flags: runhidden

[Messages]
korean.WelcomeLabel2=이 프로그램은 [name] 을(를) 이 컴퓨터에 설치합니다.%n%n설치 후 자동으로 실행되며, 다음부터는 컴퓨터를 켤 때 자동으로 시작됩니다.%n파이썬이나 Playwright 를 따로 설치할 필요는 없습니다.%n%n설치가 끝나면 웹 화면에서 [연결 코드 받기] 를 눌러 나온 6자리 숫자를,%n작업표시줄 오른쪽 아이콘 → '이 PC 연결' 에 입력해 주세요.

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{userappdata}\BlogLandingAgent');
    if DirExists(DataDir) then
    begin
      if MsgBox('로그인 세션·설정도 함께 지울까요?' + #13#10 + #13#10 +
                DataDir + #13#10 + #13#10 +
                '[아니오] 를 누르면 남겨 두고, 다시 설치할 때 그대로 씁니다.',
                mbConfirmation, MB_YESNO) = IDYES then
        DelTree(DataDir, True, True, True);
    end;
  end;
end;
