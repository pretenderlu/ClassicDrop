; Build with: ISCC installer\ClassicDrop.iss   (after build.ps1 has produced dist\ClassicDrop)
#ifndef MyAppVersion
  #define VersionFile FileOpen(AddBackslash(SourcePath) + "..\version.py")
  #define VersionLine FileRead(VersionFile)
  #expr FileClose(VersionFile)
  #define MyAppVersion Copy(VersionLine, Pos('"', VersionLine) + 1, RPos('"', VersionLine) - Pos('"', VersionLine) - 1)
#endif
#ifndef SourceDir
  #define SourceDir "..\dist\ClassicDrop"
#endif
#define MyAppName "ClassicDrop"
#define MyAppPublisher "pretenderlu"
#define MyAppURL "https://github.com/pretenderlu/ClassicDrop"

[Setup]
AppId={{CA5BFAD3-452D-4355-A542-394534B8F732}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersion}.0
DefaultDirName={localappdata}\Programs\ClassicDrop
DefaultGroupName=ClassicDrop
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=ClassicDropSetup-v{#MyAppVersion}
SetupIconFile=..\ClassicDrop.ico
LicenseFile=..\LICENSE
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\ClassicDrop.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[InstallDelete]
; Files of an older version that the new one no longer ships.
Type: filesandordirs; Name: "{app}\_internal"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Icons]
Name: "{autoprograms}\ClassicDrop"; Filename: "{app}\ClassicDrop.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\ClassicDrop"; Filename: "{app}\ClassicDrop.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\ClassicDrop.exe"; Description: "启动 ClassicDrop"; Flags: postinstall nowait
