; Inno Setup script — Stellar Smart Terminal (Windows installer)
;
; 把 PyInstaller 的 onedir 产物 (dist\StellarSmartTerminal\) 打成单文件安装包：
;   - 安装到用户目录（无需管理员 / 不弹 UAC）
;   - 开始菜单快捷方式
;   - 桌面快捷方式（安装向导里有勾选项，默认勾上）
;   - 自带卸载程序
;
; 版本号由 CI 通过 /DMyAppVersion=x.y.z 注入（见 .github/workflows/release.yml）；
; 本地手动编译可加该参数，缺省时退化为 0.0.0 仅供调试。
;
; 本地编译： "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DMyAppVersion=1.14.7 installer.iss
; 产物：     dist\Stellar-Smart-Terminal-v<版本>-windows-x64-setup.exe

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif

#define MyAppName "Stellar Smart Terminal"
#define MyAppExeName "StellarSmartTerminal.exe"
#define MyAppPublisher "huangqiliang"
#define MyAppURL "https://github.com/StellarStar255/stellar_smart_terminal"

[Setup]
; AppId 唯一标识此应用（升级/卸载靠它识别），固定不可变。
AppId={{8F3C2A14-7B6E-4D9A-9C21-5E0A7F1B2C34}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}/releases
DefaultDirName={autopf}\Stellar Smart Terminal
DefaultGroupName=Stellar Smart Terminal
DisableProgramGroupPage=yes
; 用户级安装：不要求管理员、不弹 UAC，桌面/开始菜单快捷方式照常可建。
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=dist
OutputBaseFilename=Stellar-Smart-Terminal-v{#MyAppVersion}-windows-x64-setup
SetupIconFile=assets\smart_termina128.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
; PyInstaller onedir 整目录（含 _internal\）原样拷入。
Source: "dist\StellarSmartTerminal\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent
