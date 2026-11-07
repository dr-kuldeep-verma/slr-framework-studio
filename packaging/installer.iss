; Inno Setup script for SLR Framework Studio.
;
; Build the PyInstaller folder first, then compile this with Inno Setup 6:
;     pyinstaller packaging/slr_studio.spec --noconfirm
;     iscc packaging\installer.iss
;
; Installs per-user by default so a researcher without administrator rights on
; a university machine can still install it.

#define AppName "SLR Framework Studio"
#define AppVersion "0.1.1"
#define AppPublisher "Kuldeep Verma"
#define AppExeName "SLRFrameworkStudio.exe"

[Setup]
AppId={{8F2C1E44-7B6A-4D15-9C3E-5A71D0E4B2C9}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=SLRFrameworkStudio-{#AppVersion}-setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
PrivilegesRequiredOverridesAllowed=dialog
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=app.ico
; The AGPL asks that a recipient be told the terms they receive the
; work under. An installer that shows them is the plainest way.
LicenseFile=..\LICENSE
AppCopyright=Copyright (C) 2026 Kuldeep Verma
AppPublisherURL=https://github.com/dr-kuldeep-verma/slr-framework-studio
AppSupportURL=https://github.com/dr-kuldeep-verma/slr-framework-studio/issues
UninstallDisplayIcon={app}\{#AppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; \
    GroupDescription: "Additional shortcuts:"

[Files]
Source: "..\dist\SLRFrameworkStudio\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; \
    Tasks: desktopicon

[Registry]
; Double-clicking a project.slr file opens it in the application.
Root: HKCU; Subkey: "Software\Classes\.slr"; ValueType: string; \
    ValueName: ""; ValueData: "SLRFrameworkStudio.Project"; Flags: uninsdeletevalue
Root: HKCU; Subkey: "Software\Classes\SLRFrameworkStudio.Project"; ValueType: string; \
    ValueName: ""; ValueData: "SLR review project"; Flags: uninsdeletekey
Root: HKCU; Subkey: "Software\Classes\SLRFrameworkStudio.Project\DefaultIcon"; \
    ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExeName},0"
Root: HKCU; Subkey: "Software\Classes\SLRFrameworkStudio.Project\shell\open\command"; \
    ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" ""%1"""

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Leaves every project folder alone -- those are the researcher's data.
Type: filesandordirs; Name: "{localappdata}\SLRFrameworkStudio\cache"
