; The Windows installer (LightShowStudio-Windows.exe), around PyInstaller's dist\LightShowStudio folder.
; Built by .github/workflows/release.yml: iscc /DVersion=1.2.3 packaging\installer.iss
; Installs for the current user only (no administrator prompt). The data folder,
; %LOCALAPPDATA%\LightShowStudio, is left alone on uninstall: it holds your shows and settings.

#ifndef Version
  #define Version "0.0.0"
#endif

[Setup]
AppId={{6C2F4E8A-1B7D-4C55-9E2A-6A3F0D9B7C41}
AppName=Light Show Studio
AppVersion={#Version}
AppPublisher=Light Show Studio
DefaultDirName={localappdata}\Programs\Light Show Studio
DefaultGroupName=Light Show Studio
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=LightShowStudio-Windows
SetupIconFile=..\Light Show Studio.ico
UninstallDisplayIcon={app}\LightShowStudio.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\dist\LightShowStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Light Show Studio"; Filename: "{app}\LightShowStudio.exe"; AppUserModelID: "LightShowStudio.Studio"
Name: "{autodesktop}\Light Show Studio"; Filename: "{app}\LightShowStudio.exe"; Tasks: desktopicon; AppUserModelID: "LightShowStudio.Studio"

[Run]
Filename: "{app}\LightShowStudio.exe"; Description: "{cm:LaunchProgram,Light Show Studio}"; Flags: nowait postinstall skipifsilent
