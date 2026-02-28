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
  #define MyAppVersion "2.0.5"
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
#define MyAppURL "https://github.com/FluentYTDL/FluentYTDL"
#define MyAppExeName "FluentYTDL.exe"
#define MyAppDescription "专业 YouTube 下载器"

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
SetupIconFile=..\assets\logo.ico

; 压缩配置 (LZMA2 最高压缩)
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
LZMADictionarySize=65536
LZMANumFastBytes=273

; 界面配置
WizardStyle=modern
WizardSizePercent=110,100

; 权限配置
; 使用 admin 是因为程序安装到 Program Files，需要管理员权限
; UsedUserAreasWarning=no 抑制关于用户区域的警告，因为我们有意在卸载时清理用户数据
PrivilegesRequired=admin
PrivilegesRequiredOverridesAllowed=dialog commandline
UsedUserAreasWarning=no

; 卸载配置
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Uninstallable=yes
CreateUninstallRegKey=yes

; 兼容性
MinVersion=10.0
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

; 日志
SetupLogging=yes

; ============================================================================
; [Languages] 多语言支持
; ============================================================================
[Languages]
; 注意: 中文语言包需要单独下载安装
; 下载地址: https://github.com/jrsoftware/issrc/tree/main/Files/Languages/Unofficial
Name: "english"; MessagesFile: "compiler:Default.isl"

; ============================================================================
; [CustomMessages] 自定义消息
; ============================================================================
[CustomMessages]
english.AddToPath=Add tools directory to system PATH (enables command-line usage of yt-dlp, etc.)
english.SystemIntegration=System Integration:

; ============================================================================
; [Tasks] 安装任务选项
; ============================================================================
[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
Name: "quicklaunchicon"; Description: "{cm:CreateQuickLaunchIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "addtopath"; Description: "{cm:AddToPath}"; GroupDescription: "{cm:SystemIntegration}"; Flags: unchecked

; ============================================================================
; [Dirs] 目录创建
; ============================================================================
[Dirs]
Name: "{app}\logs"; Permissions: users-modify
Name: "{app}\bin"; Permissions: users-modify

; ============================================================================
; [Files] 文件部署
; ============================================================================
[Files]
; 主程序和运行时
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

; ============================================================================
; [Icons] 快捷方式
; ============================================================================
[Icons]
; 开始菜单
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Comment: "{#MyAppDescription}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"

; 桌面图标
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon; Comment: "{#MyAppDescription}"

; 快速启动栏
Name: "{userappdata}\Microsoft\Internet Explorer\Quick Launch\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: quicklaunchicon

; ============================================================================
; [Registry] 注册表项
; ============================================================================
[Registry]
; 添加 PATH 环境变量 (仅当用户选择时)
; 注意: 使用 HKCU 是因为 PATH 修改应针对当前用户
; 如果以管理员身份安装但想修改当前用户的 PATH，这是正确的做法
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\bin"; Tasks: addtopath; Check: NeedsAddPath(ExpandConstant('{app}\bin')); Flags: uninsdeletekeyifempty

; ============================================================================
; [Run] 安装后运行
; ============================================================================
[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

; ============================================================================
; [UninstallDelete] 卸载时删除
; ============================================================================
[UninstallDelete]
; 清理安装目录中的用户数据和日志
Type: filesandordirs; Name: "{app}\logs"
Type: filesandordirs; Name: "{app}\config.json"
Type: filesandordirs; Name: "{app}\*.log"
Type: filesandordirs; Name: "{app}\cache"
Type: filesandordirs; Name: "{app}\data"

; 清理用户目录中的应用数据 (AppData)
; 注意: 这些是用户数据目录，卸载时清理是预期行为
; 如果以管理员身份卸载，将清理运行卸载程序的用户的数据
Type: filesandordirs; Name: "{userappdata}\FluentYTDL"
Type: filesandordirs; Name: "{localappdata}\FluentYTDL"

; 清理应用创建的注册表 (通过 [UninstallRun] 或 [Code] 实现)

[UninstallRun]
; 确保关闭正在运行的程序
; RunOnceId 确保此条目在卸载时只执行一次
Filename: "taskkill"; Parameters: "/F /IM FluentYTDL.exe"; Flags: runhidden nowait; RunOnceId: "KillFluentYTDL"

[Registry]
; 卸载时删除应用程序可能创建的注册表项 (Flags: uninsdeletekey)
Root: HKCU; Subkey: "Software\FluentYTDL"; Flags: uninsdeletekey dontcreatekey

; ============================================================================
; [Code] Pascal 脚本
; ============================================================================
[Code]
const
  WM_SETTINGCHANGE = $001A;
  SMTO_ABORTIFHUNG = $0002;

// ========== 工具函数 ==========

// 检查是否需要添加 PATH
function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
  SearchPath: string;
begin
  Result := True;
  
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
    Exit;
  
  // 规范化路径以进行比较
  SearchPath := ';' + UpperCase(Param) + ';';
  OrigPath := ';' + UpperCase(OrigPath) + ';';
  
  // 检查路径是否已存在
  Result := Pos(SearchPath, OrigPath) = 0;
end;

// 从 PATH 中移除指定路径
procedure RemoveFromPath(PathToRemove: string);
var
  OrigPath: string;
  NewPath: string;
  PathUpper: string;
  OrigUpper: string;
  StartPos: Integer;
begin
  if not RegQueryStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', OrigPath) then
    Exit;
  
  PathUpper := UpperCase(PathToRemove);
  OrigUpper := UpperCase(OrigPath);
  
  // 查找并移除路径 (处理各种边界情况)
  NewPath := OrigPath;
  
  // 情况 1: ;path;
  StartPos := Pos(';' + PathUpper + ';', ';' + OrigUpper + ';');
  if StartPos > 0 then
  begin
    if StartPos = 1 then
      // 在开头: path;...
      Delete(NewPath, 1, Length(PathToRemove) + 1)
    else
      // 在中间或结尾: ...;path;... 或 ...;path
      Delete(NewPath, StartPos, Length(PathToRemove) + 1);
  end;
  
  // 清理可能的双分号
  while Pos(';;', NewPath) > 0 do
    StringChangeEx(NewPath, ';;', ';', True);
  
  // 清理首尾分号
  if (Length(NewPath) > 0) and (NewPath[1] = ';') then
    Delete(NewPath, 1, 1);
  if (Length(NewPath) > 0) and (NewPath[Length(NewPath)] = ';') then
    Delete(NewPath, Length(NewPath), 1);
  
  // 写回注册表
  if NewPath <> OrigPath then
    RegWriteStringValue(HKEY_CURRENT_USER, 'Environment', 'Path', NewPath);
end;

// ========== 安装过程钩子 ==========

// 安装前初始化
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  
  // 尝试关闭正在运行的程序实例
  Exec('taskkill', '/F /IM FluentYTDL.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  
  // 等待进程完全退出
  Sleep(500);
end;

// 安装完成后
procedure CurStepChanged(CurStep: TSetupStep);
begin
  // PATH 更改将在下次登录或重启资源管理器后生效
end;

// ========== 卸载过程钩子 ==========

// 卸载前初始化
function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  
  // 关闭正在运行的程序
  Exec('taskkill', '/F /IM FluentYTDL.exe', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Sleep(500);
end;

// 卸载过程钩子
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  BinPath: string;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // 从 PATH 中移除 bin 目录
    BinPath := ExpandConstant('{app}\bin');
    RemoveFromPath(BinPath);
  end;
end;
