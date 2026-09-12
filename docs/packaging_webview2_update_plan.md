# Full 解压、应用更新与 WebView2 登录修复方案

日期：2026-09-11。状态：代码已实施，本地针对性验证通过；未发布，用户故障机及完整安装升级验收仍待复核。

## 1. 目标与证据

统一 Full/app-core 归档兼容性，让更新器不依赖用户安装解压工具，并使登录失败能够准确定位到 Python.NET 或 WebView2。

| 用户现象 | 已确认事实 | 尚不能确认的部分 |
| --- | --- | --- |
| Full 登录失败，Setup 可用 | 日志检测到 WebView2 152.0.4191.66；失败在 Python.NET 获取 Python.Runtime.Loader.Initialize；Full/Setup 当前共用 dist，安装脚本没有额外安装运行库 | 用户端 DLL 来源标记、文件完整性、实际依赖版本以及两包是否同版本；不能直接认定中文路径或 Runtime 缺失 |
| 更新提示 py7zr 不支持格式且找不到 7z | 构建优先使用系统 7z，未约束过滤器；回退错误地在 _update_tmp/bin 查找工具；错误文案混淆不同异常 | 对应 updater.log 与失败归档未取得，不能断定此次一定是 BCJ2 |
| Full 解压 win-arm64/native/WebView2Loader.dll 提示不支持压缩方法 | Full 同样使用无过滤器约束的 7z 高压缩参数；7-Zip 23.01 新增 ARM64 过滤器，旧引擎不支持 | 高度怀疑 ARM64 过滤器，最终需用失败归档的编码器列表确认 |

第三个现象与更新失败属于同类归档兼容性问题。它会导致解压结果不完整，但不能据此把第一项也定为同一根因：ARM64 WebView2Loader.dll 与 Python.Runtime.dll 是不同文件，且 win64 运行时通常使用 x64 Loader。

## 2. 统一 Full 与 app-core 的归档协议（优先实施）

- 在 scripts/build.py 集中定义兼容压缩配置，覆盖 Full 主归档、portable.txt 追加及 app-core，禁止每个调用点独立选择默认参数。
- CLI 固定使用 `-t7z -m0=LZMA2 -mf=off -md=32m -mx=7`；多线程可启用。禁用 BCJ、BCJ2、ARM64 等自动过滤器；32 MiB 字典限制解码内存，不随系统 7z 默认值变化。
- py7zr 回退显式指定单一 `FILTER_LZMA2`、`dict_size=32*1024*1024`、`preset=7`，不依赖库的默认过滤器链。两条路径保持解码协议相同，不要求字节级相同压缩结果。
- 压缩输出必须新建，不能更新旧发布归档后遗留旧编码块。Full 的 portable.txt 仍仅追加至归档，不写入共享 dist；app-core/Setup 继续排除该标记。
- 对完成追加后的最终归档检查编码器：只允许 Copy/LZMA2（含归档头编码），发现任何其它方法立即阻止发布。再完整解压并比较文件清单及 SHA256，不能只检查命令退出成功。
- 保持现有文件名、target 输出集合、manifest 结构和用户数据路径不变。重新压缩后重新生成 manifest 与校验和。
- 暂不裁剪 webview 包中的 ARM64/x86 资源。删除某个 DLL 不能修复其它文件的 BCJ2 问题；架构裁剪属于后续独立优化。

Full 首次解压由用户的解压软件完成，包里放 7za 无法帮助用户打开这个包。因此归档协议修复是必要条件，自带解压器不能替代它。

## 3. 更新器内嵌独立 7za

### 工具交付与运行

- 使用官方 7-Zip Extra 的 x64 独立 `7za.exe`，不采用需要外部 7z.dll 的普通 7z.exe。初次实施固定官方 26.03，下载地址、版本、SHA256 与许可证信息统一记录；实际 SHA256 在接入时从所取官方制品计算并固定，不填占位值或使用浮动 latest。
- 构建依赖准备阶段下载并校验；本地允许使用通过相同哈希校验的缓存。缺失、哈希不符或无法取得许可证均终止构建，运行时不联网下载工具。
- scripts/updater.spec 将工具和许可证嵌入 updater.exe 的 onefile 资源。运行时通过 `sys._MEIPASS` 下固定相对路径定位，随 updater.exe.new 一起交付，避免依赖 bin 组件更新。
- 解压顺序：内嵌 7za → py7zr。仅当内嵌工具不存在或无法启动时回退；工具已经执行但报告归档错误时保留原始错误并中止，不把错误转换为“系统未安装 7z”。取消对系统 PATH 工具的依赖。
- 内部接口显式区分归档路径、解压目标目录、工具资源目录；不得从解压目标推导应用安装目录。保留 `extract_archive(archive_path, dest_dir)` 的现有调用兼容性，工具定位由独立函数完成。
- 使用参数列表、`shell=False`、无窗口启动；输出目录作为单个 `-o...` 参数。传输、工具启动、解码失败分别记录，包含工具版本、退出码和经清理的 stderr/stdout；退出码非零均视为失败。
- 保留先解压到 _update_tmp、验证再替换及原有回滚流程。解压失败清理临时内容，现有程序和用户数据不变。

### 旧版本过渡

- 第一版修复必须仍发布 py7zr 可解的 app-core.7z，并包含 updater.exe.new。旧更新器先解开兼容包，新版主程序/helper 再完成更新器替换。
- 不允许依靠“新解压器在新归档中”解决第一次升级，这会形成先解包才能获得解包工具的循环。
- 对缺少完整 py7zr 的更旧更新器，提供 Setup 覆盖安装作为恢复入口，并明确沿用现有数据迁移规则；不要承诺这类版本能够自动自愈。
- 后续版本继续保持兼容压缩协议，不因已经内嵌 7za 而重新开启架构过滤器。

## 4. Python.NET / WebView2 分层诊断

### 错误与接口

- 在登录子进程中显式预检 Python.NET，再进入 pywebview 的窗口创建和启动，按失败阶段分类。
- 子进程失败消息保留 `error`，补充稳定的 `code`、`stage`；区别 DLL 缺失、Python.NET 加载失败、WebView2 Runtime 缺失、窗口初始化失败、超时、子进程退出和空 Cookie。
- Provider 保存本次错误并提供只读获取接口。auth_service 使用 Provider 的具体错误，不再在 cookies 为 None 时统一抛“超时或空数据”；每次调用重置错误，避免复用旧失败。
- 删除窗口初始化异常上无条件的 `webview2_unavailable` 归类和“通常缺少运行时”提示；只有有对应探测证据时才引导安装 WebView2。
- 不更改 Cookie 写入门禁、失败保留旧 Cookie 和双进程隔离模型。

### 诊断内容与根因验证

- 失败诊断记录进程架构、冻结状态、Python.NET/clr-loader/pywebview 版本，关键 DLL 的存在性、SHA256 和 Zone.Identifier 是否存在。路径及异常经过项目既有清理机制；不记录 Cookie 或代理凭证。
- 单独提供只读诊断脚本：在独立 Windows PowerShell/.NET Framework 进程中测试 Python.Runtime.dll 加载及目标类型/方法解析，输出异常类型、HRESULT、内部异常；不将诊断加载结果当成完整登录成功。
- clr_loader 上游会吞掉 .NET 异常并只返回空指针，因此单纯增加 Python traceback 无法补齐根因。诊断脚本和文件元数据必须一起提供。
- 建立同版本 Full/Setup 文件哈希对照；测试英文/中文/空格路径，以及带/不带互联网来源标记的副本。每次只改变一个变量。
- 若确认官方归档来源标记导致失败，提示用户对已核验来源的压缩包解除锁定后重新解压；不全盘递归解锁、不自动设置全局 loadFromRemoteSources。
- 若诊断证明文件缺失或不一致，提示重新完整解压/覆盖安装；若证明 .NET 依赖或加载器版本问题，再针对该证据修依赖。当前方案不凭推测更换 CLR 或给中文路径加绕过补丁。

## 5. 验证与验收

| 验证层 | 必须覆盖的场景 | 验收标准 |
| --- | --- | --- |
| 归档生成 | CLI 与 py7zr 两分支；Full 追加标记后、app-core 最终归档 | 编码器仅 Copy/LZMA2，完整解压后清单与哈希一致；归档包含真实 x64/ARM64 DLL 样本 |
| 外部解压 | 7-Zip 22.01 的旧引擎（仅隔离兼容性测试）、当前官方工具；用户反馈的软件取得后加入复现 | 包含 win-arm64 Loader 在内的全部文件无“不支持压缩方法”；不承诺所有不具备 LZMA2 支持的软件 |
| 冻结更新器 | 无系统 7z/PATH；英文、中文、空格、括号目录；缺失内嵌工具的回退 | 正常包可完整解压，工具定位与 _update_tmp 无关 |
| 失败保护 | 损坏/截断归档、工具无法启动、磁盘不足 | 显示准确错误、失败清理临时目录、原程序及用户数据保持不变 |
| 升级过渡 | 当前受影响旧 updater → 兼容包 → 新 updater；然后再更新一次 | 两次升级均成功，确认 updater.exe.new 已完成替换 |
| 登录 | Runtime 缺失、Python.NET DLL 缺失/被阻止、真实超时、空 Cookie、正常登录 | UI 与日志保留正确分类，DLL 错误不再显示成 Runtime 缺失或超时；失败不覆盖有效 Cookie |
| 打包一致性 | 同次构建 Full/Setup 的关键 DLL 与登录流程 | DLL 哈希相同，完整解压/安装后均可打开登录窗口 |

CI 为实际发布归档增加解压门禁，保留工具版本、归档方法、文件校验报告；不以 py7zr 自压自解文本文件替代真实产物测试。构建失败不得继续生成可发布状态。

实施顺序：统一归档协议及验证 → 内嵌 7za 与旧更新器升级验证 → 登录分层错误和诊断工具 → 用户故障环境复核。本文不包含版本升级、打 tag、发布 Release 或其它日志问题的修复。

## 6. 参考依据

- [7-Zip 官方 SDK 更新记录：23.01 新增 ARM64 过滤器](https://www.7-zip.org/sdk.html)
- [7-Zip 作者说明：旧版本不支持 ARM64 过滤器](https://sourceforge.net/p/sevenzip/discussion/45797/thread/3f550826d8/)
- [7-Zip 官方独立命令行工具下载](https://www.7-zip.org/download.html)
- [7-Zip 再分发说明](https://www.7-zip.org/faq.html)
- [py7zr 格式支持说明](https://py7zr.readthedocs.io/en/v0.18.7/archive_format.html)
- [clr-loader 异常处理源码](https://github.com/pythonnet/clr-loader/blob/main/netfx_loader/ClrLoader.cs)
- [clr-loader 程序集加载源码](https://github.com/pythonnet/clr-loader/blob/main/netfx_loader/DomainData.cs)
- [微软：带来源区域标记的程序集加载限制](https://learn.microsoft.com/en-us/dotnet/framework/configure-apps/file-schema/runtime/loadfromremotesources-element)

## 7. 实施记录与复核入口

- Full/app-core 的所有 CLI 压缩调用统一参数；Python 分支禁用过滤器和编码头，并逐个写入顶层项目，避免 `arcname="."` 产生被 py7zr 1.1.3 拒绝的根目录条目。
- 新增 `scripts/archive_compat.py`：发布前检查方法、用 py7zr 完整解压比对文件哈希，再用实际冻结更新器在空 PATH、中文空格括号目录下完整解压比对；冻结校验模式禁止回退，确保内嵌工具真实可用。CI 保存 JSON 报告。
- 新增 `scripts/prepare_7zip.py`：官方 26.03 Extra 和构建引导 7zr 均固定 SHA256；内嵌 x64 7za、版本元数据和官方许可证。7zr 只参与构建，不进入最终应用。
- Python.NET 预检失败时收集关键 DLL 存在性、哈希、下载标记与依赖版本；错误分类和原始消息通过 Provider 传给 AuthService。不会自动解除文件锁定或修改 .NET 全局配置。
- 139 项相关测试通过，Ruff 通过；真实 x64/ARM64 Loader、主程序 EXE 和冻结 updater 样本分别经过 CLI/Python 两种 Full/app-core 打包及解压验证。旧版 7-Zip 22.01 校验新 Full 样本成功；空 PATH 的冻结 updater 解开官方 BCJ2 包并核对输出哈希成功。
- 诊断脚本在本机未标记 DLL 上加载成功；对隔离副本添加 ZoneId=3 后捕获 .NET `0x80131515` 和内部异常。这个实验验证诊断能力，不证明用户故障机一定是同一原因。
- 未完成的外部验收：用户所用解压软件和实际失败归档复核、同版本 Full/Setup 完整登录对照、真实旧安装目录的两次 GUI 升级及回滚。此次未改版本号、打 tag 或发布制品。

仓库内只读诊断命令（使用 Windows PowerShell 5.1，不是 pwsh）：

```powershell
powershell.exe -NoProfile -File .\scripts\diagnose_webview2.ps1 -AppDirectory 'E:\工具\FluentYTDL'
```

新构建用户包中的脚本位于 `_internal\docs\diagnose_webview2.ps1`。同时收集登录账号 profile 目录下的 `webview_subprocess.log`；运行库证据不包含 Cookie 内容。
