; ============================================================================
; FluentYTDL Inno Setup Script
; ============================================================================
; 
; 用于构建 Windows 安装程序
; 
; 构建命令:
;   ISCC.exe /DMyAppVersion=1.0.18 FluentYTDL.iss
;   ISCC.exe /DMyAppVersion=1.0.18 /DSourceDir=..\dist\FluentYTDL FluentYTDL.iss
;
; ============================================================================

; --- 版本定义 (可通过命令行覆盖) ---
#ifndef MyAppVersion
  #define MyAppVersion "3.7.2-rc.1"
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\FluentYTDL"
#endif

#ifndef OutputDir
  #define OutputDir "..\release"
#endif

#ifndef OutputBaseFilename
  #define OutputBaseFilename "FluentYTDL-setup"
#endif

; 提取纯数字版本 (去掉 "-pre" 等预发布后缀), 供 VersionInfoVersion 使用
#define MyAppVersionNumeric Copy(MyAppVersion, 1, Pos("-", MyAppVersion + "-") - 1)

; --- 应用程序信息 ---
#define MyAppName "FluentYTDL"
#define MyAppPublisher "FluentYTDL Team"
#define MyAppURL "https://github.com/SakuraForgot/FluentYTDL"
#define MyAppExeName "FluentYTDL.exe"
#define MyAppDescription "YouTube and X video downloader"

; ============================================================================
; [Setup] 安装程序配置
; ============================================================================
[Setup]
; 应用程序唯一标识符 (GUID) - 首次生成后请勿更改!
; 使用 https://www.guidgenerator.com/ 生成新 GUID
AppId={{E8F3A9D2-4B7C-4E1F-9A3D-2C8B6F4E7A1D}

; 基本信息
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
AppUpdatesURL={#MyAppURL}/releases
VersionInfoVersion={#MyAppVersionNumeric}
VersionInfoCompany={#MyAppPublisher}
VersionInfoDescription={#MyAppDescription}
VersionInfoProductName={#MyAppName}
VersionInfoProductVersion={#MyAppVersionNumeric}

; 安装目录
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
AllowNoIcons=yes

; 输出配置
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
SetupIconFile=..\assets\FluentYTDL_v2.ico

; 压缩配置 (LZMA2 最高压缩)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMADictionarySize=65536
LZMANumFastBytes=273

; 界面配置
WizardStyle=modern
WizardSizePercent=110,100

PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog commandline
UsePreviousPrivileges=yes
ChangesEnvironment=yes
CloseApplications=yes
RestartApplications=no
LanguageDetectionMethod=uilanguage
ShowLanguageDialog=yes

; 卸载配置
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Uninstallable=yes
; Old logs contain global process termination and recursive data deletion instructions.
; Never inherit these commands when upgrading from the previous installer.
UninstallLogMode=overwrite
CreateUninstallRegKey=yes

; 兼容性
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 日志
SetupLogging=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"
Name: "chinesesimp"; MessagesFile: "languages\ChineseSimplified.isl"

[CustomMessages]
english.AppDescription=YouTube and X video downloader
chinesesimp.AppDescription=YouTube 和 X 视频下载器
english.AddToPath=Add command-line tools to PATH for the selected installation scope
chinesesimp.AddToPath=将命令行工具加入所选安装范围的 PATH
english.SystemIntegration=Command-line tools:
chinesesimp.SystemIntegration=命令行工具：
english.CleanupWarning=Uninstall removes accounts, cookies, settings and download history for this installation. Downloaded media files are kept. For an all-users installation, application data for all users will be cleared.
chinesesimp.CleanupWarning=卸载将清除本安装的账号、Cookie、配置和下载历史，保留已下载的媒体文件。所有用户安装将清理各用户的应用数据。
english.MaintenanceFailed=Could not finish application maintenance. See the uninstall/setup log; remaining application data has not been reported as cleared.
chinesesimp.MaintenanceFailed=应用维护未完成，请查看安装或卸载日志。残留数据尚未清理完成。
english.ScopeConflict=An installation in the other scope already exists. Use its existing scope to upgrade. Uninstalling it first will clear its accounts, settings and history.
chinesesimp.ScopeConflict=检测到另一安装范围的旧版本，请沿用旧范围升级。若先卸载旧版本，其账号、配置和历史记录将被清空。

[Messages]
english.ConfirmUninstall=Are you sure you want to uninstall %1? Accounts, cookies, settings and download history will be deleted. Downloaded media files are kept.
chinesesimp.ConfirmUninstall=确定卸载 %1 吗？账号、Cookie、配置和下载历史将全部删除，已下载媒体文件保留。

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "addtopath"; Description: "{cm:AddToPath}"; GroupDescription: "{cm:SystemIntegration}"; Flags: unchecked

[Dirs]
; Authentication and component updates still write below bin; data relocation is out of scope.
Name: "{app}\bin"; Permissions: users-modify; Check: IsAdminInstallMode

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "portable.txt"
Source: "maintenance.ps1"; DestDir: "{app}\_internal\installer"; Flags: ignoreversion
Source: "maintenance.ps1"; Flags: dontcopy

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{cm:AppDescription}"; AppUserModelID: "FluentYTDL"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "{cm:AppDescription}"; AppUserModelID: "FluentYTDL"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#MyAppName}}"; Flags: nowait postinstall skipifsilent runasoriginaluser

[Code]
function InstallScope: String;
begin
  if IsAdminInstallMode then Result := 'machine' else Result := 'user';
end;

function Maintain(Action, Script: String): Boolean;
var
  Code: Integer;
  Args: String;
begin
  Args := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "' + Script +
    '" -Action ' + Action + ' -AppDirectory "' + ExpandConstant('{app}') +
    '" -Scope ' + InstallScope;
  Result := ExecAndLogOutput(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    Args, '', SW_SHOWNORMAL, ewWaitUntilTerminated, Code, nil);
  Result := Result and (Code = 0);
  Log('Maintenance ' + Action + ': exit=' + IntToStr(Code));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  OtherRoot: Integer;
  Key: String;
begin
  Result := '';
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{E8F3A9D2-4B7C-4E1F-9A3D-2C8B6F4E7A1D}_is1';
  if IsAdminInstallMode then OtherRoot := HKCU else OtherRoot := HKLM64;
  if RegKeyExists(OtherRoot, Key) then begin
    Result := CustomMessage('ScopeConflict');
    Exit;
  end;
  ExtractTemporaryFile('maintenance.ps1');
  if not Maintain('Stop', ExpandConstant('{tmp}\maintenance.ps1')) then
    Result := CustomMessage('MaintenanceFailed');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Language: String;
begin
  if CurStep = ssPostInstall then begin
    if not Maintain('Register', ExpandConstant('{tmp}\maintenance.ps1')) then
      RaiseException(CustomMessage('MaintenanceFailed'));
    if ActiveLanguage = 'chinesesimp' then Language := 'zh_CN' else Language := 'en_US';
    if not FileExists(ExpandConstant('{app}\install-language.txt')) then
      if not SaveStringToFile(ExpandConstant('{app}\install-language.txt'), Language, False) then
        RaiseException(CustomMessage('MaintenanceFailed'));
    if WizardIsTaskSelected('addtopath') then
      if not Maintain('AddPath', ExpandConstant('{tmp}\maintenance.ps1')) then
        RaiseException(CustomMessage('MaintenanceFailed'));
  end;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Script: String;
begin
  if CurUninstallStep = usUninstall then begin
    Script := ExpandConstant('{app}\_internal\installer\maintenance.ps1');
    if not Maintain('Stop', Script) then RaiseException(CustomMessage('MaintenanceFailed'));
    if not Maintain('Clean', Script) then RaiseException(CustomMessage('MaintenanceFailed'));
    if not Maintain('RemovePath', Script) then RaiseException(CustomMessage('MaintenanceFailed'));
    DeleteFile(ExpandConstant('{app}\install-language.txt'));
    DeleteFile(ExpandConstant('{app}\.install-owner.json'));
  end;
end;
