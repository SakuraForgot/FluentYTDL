# 构建、发布与安装维护

## 环境与入口

支持 Windows x64、Python 3.12.12；工具链版本见根目录 `build-environment.json`，Python 依赖由 `uv.lock` 锁定。

```powershell
uv sync --locked --extra dev --extra build
uv run --locked --no-sync python scripts/build.py --target all
```

GUI 使用 `scripts/build_gui.cmd`。`--version` 仅覆盖此次构建，不修改 VERSION、pyproject 或安装脚本。需要改变项目版本时运行 `version_manager.py set/bump`，该命令同步版本载体并刷新 uv.lock。

每次构建独立存放在 `build/runs/<id>/`。其中 `release/` 是本次产物，`build-result.json` 是产物清单及哈希。`build/latest-result.json` 指向最近成功构建；失败不覆盖它。历史目录不会被清空。

`--target spec` 编译主程序并执行冻结自检；`7z/full` 仅生成 Full；`app-core` 生成内部更新包与清单；`setup` 仅生成安装包；`all` 生成上述全部产物及校验和。自动更新不会分发安装器语言种子，但会更新卸载维护脚本。

## 所有附加组件每次取最新

每次非 spec 构建均从现有上游渠道解析最新 yt-dlp、FFmpeg/ffprobe、Deno、AtomicParsley、POT Provider，以及官方 7-Zip Extra。每组件只解析一次发布元数据，整个构建共用对应的字节。

`components/snapshot.json` 记录上游 release/tag、资产 ID、URL、版本、大小、SHA256、取得时间，以及落盘文件哈希。上游提供的资产摘要和校验文件必须验证；只有实测哈希时明确标记 observed-sha256。旧 TOOLS.lock.json 只是历史参考，不限制新版本或同版本上游资产刷新。无法取得最新或校验失败直接失败，不使用旧缓存顶替。

```powershell
# 单独取得一份最新快照，写入新的 build/components/<id>
uv run --locked --no-sync python scripts/fetch_tools.py

# 诊断重现：不会联网选择新版本，但必须验证快照文件哈希；禁止用于正式发布
uv run --locked --no-sync python scripts/build.py --target all --snapshot build/runs/<id>/components/snapshot.json
```

## CI 与发布

`checks.yml` 复用锁文件、版本、规则/翻译同步、Ruff 与 Windows Python 3.12 测试；Pyright 保持 advisory。CI 还验证真实冻结程序，最终提供稳定的 `Required checks` 状态。当前所有提交（包括文档）都执行检查，避免路径过滤产生永久等待。

tag 发布须与 VERSION 和当前提交一致，提交必须属于 main 历史。公开发布只能 target=all、源码干净、当次 latest 快照。stable 发布为 Latest；rc 为预发布；beta 仅存 Actions Artifacts。手动构建保留目标及版本覆盖，但不能跳过门禁公开发布。

### rc 预发布操作

使用 `version_manager.py set 3.7.2-rc.1` 同步版本及锁文件；准备分支可通过 `release.yml` 的手动入口选择 `targets=all`、`publish=false`，先验证完整构建及隔离 runner 安装生命周期。发布前必须将已验证改动合入 main。随后对应 tag 触发同一工作流，重新获取全部最新组件，通过检查后自动将已核验 Draft 公开为 Pre-release，显式设置 `prerelease=true`、`latest=false`。

无需增加单独的 pre.yml，也不接受 `-pre` 作为新版本后缀；版本号使用 `-rc.N`。rc 客户端目前保持手动下载后续版本，稳定版用户不会通过 Latest 收到此预发布。恢复已有 Draft 时也重新设置并验证预发布标记。

发布读取成功构建清单；上传 Draft 后下载核对，再公开同一批文件。公开后通过下载地址再次校验，stable 还检查 latest 清单入口。公开版本不可覆盖。失败不自动删除公开 Release；失败 Draft 只有资产完整且与当前产物逐字节一致才可恢复，否则需要维护者检查。

工作流合入且 main 上新的 `Required checks` 成功后，才能启用对应分支保护：

```powershell
uv run --locked --no-sync python scripts/configure_branch_protection.py
uv run --locked --no-sync python scripts/configure_branch_protection.py --apply
```

该命令在新检查未落地时拒绝修改远端，防止提前锁死 main；不要求维护者自我审批。

## 安装、升级和卸载

Inno 6.7.1，默认当前用户、可选所有用户。简中语言文件来自 jrsoftware/issrc 的 is-6_7_1 标签，保留文件中的翻译作者信息。系统 UI 语言决定默认值；安装时可切换；语言种子仅对没有配置的用户生效。

保留既有 AppId，覆盖安装沿用原范围。另一范围存在安装时阻止跨范围安装，并说明先卸载会清除数据。PATH 默认关闭，启用后添加真实工具子目录，仅记录和撤销本安装添加的项。

覆盖安装重写卸载日志，不追加旧版日志中的全局 taskkill 和递归删除用户目录指令。旧版本独有、已不由新安装器管理的文件可能留下；不得为清除这些遗留文件而扩大递归删除边界。

**卸载没有保留数据选项**：删除账号、Cookie、配置、数据库及历史、运行缓存、日志、WebView2 资料和认证临时文件。账号通常位于应用 `bin/dle_user`，安装版数据库通常位于 LocalAppData，两处均需处理；同时覆盖遗留数据目录和具有双向归属记录的自定义数据根。所有用户安装清理实际 Windows 用户的应用数据。

下载成品和未知文件保留，不能递归删除安装根或下载目录。目录联接、权限失败等清理障碍会使维护失败，而非报告全部清理成功。安装和卸载仅关闭路径属于本安装的进程及其子进程；安装器打开阶段不强杀应用。

**覆盖安装、应用更新和回滚必须保留数据**，不得调用卸载清理。

## 验证与运行边界

- 本地与 CI：冻结启动自检（Qt 窗口、资源、英文翻译、Python.NET/pywebview 导入）、Full/app-core 编码器限制及文件哈希、冻结 updater 在空 PATH 和中文路径下解压。
- 发布 runner：`scripts/test_installer.py --allow-isolated-runner` 对中英文、两个安装范围执行真实新装、覆盖安装、冻结启动和卸载，核对运行数据删除、下载成品保留。脚本只允许临时 GitHub-hosted runner，拒绝在开发者机器清理真实资料。
- 本地单测：最新下载解析/摘要失败、快照完整性/越界、版本只写暂存、资产被修改、回放快照禁发、双语消息、清理函数的媒体保留及目录联接拒绝。
- 需要单独记录的实机验收：100/150/200% DPI、非管理员账号跨账号提权、多个真实 Windows 使用者、WebView2 正常登录、受影响旧 updater 连续升级两次与真实回滚。未执行的项不能由单测或编译结果替代。

本次改造不配置签名证书，不自动升级版本、打 tag 或公开发布。本地编译与测试结果和无法执行的外部验收见同目录的实施记录。
