; DeskGuard installer – Inno Setup 6 script
; Produces dist\DeskGuard-Setup-<version>.exe from the PyInstaller output in dist\DeskGuard\.
; Built automatically by build.bat; to build by hand:
;   ISCC.exe /DAppVersion=1.1.0 installer\deskguard.iss

#define AppName "DeskGuard"
#ifndef AppVersion
  #define AppVersion "1.1.0"
#endif
#define AppExe "DeskGuard.exe"

[Setup]
AppId={{8F3C2A71-5B4E-4D7A-9C1E-2F6B8D0A4E13}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher=DeskGuard
VersionInfoVersion={#AppVersion}
VersionInfoDescription={#AppName} Setup
; Per-user install like most modern apps: no admin rights, no UAC prompt.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#AppName}
DisableDirPage=auto
DisableProgramGroupPage=yes
OutputDir=..\dist
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=..\resources\deskguard.ico
UninstallDisplayIcon={app}\{#AppExe}
UninstallDisplayName={#AppName}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
; Setup/uninstall asks to close DeskGuard if it is running (main.py holds this mutex).
AppMutex=DeskGuard.SingleInstance.Mutex
CloseApplications=yes

[Tasks]
Name: "autostart"; Description: "Start DeskGuard automatically when I sign in to Windows"; GroupDescription: "Startup:"
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "..\dist\DeskGuard\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
; Same value the app's own "Start with Windows" menu item reads and writes.
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "DeskGuard"; ValueData: """{app}\{#AppExe}"""; Flags: uninsdeletevalue; Tasks: autostart

[Run]
Filename: "{app}\{#AppExe}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}"

[Code]
// Also remove the startup entry if it was turned on later from the app's menu.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER, 'Software\Microsoft\Windows\CurrentVersion\Run', 'DeskGuard');
end;
