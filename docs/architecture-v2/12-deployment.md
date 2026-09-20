# 新版架构 V2 · 构建、部署与维护模型

证据：2026-09-19 当前工作树 source/static；未联网、未构建、未安装、未卸载、未运行测试。本文描述实现支持的流程，`[UNKNOWN]` 的生产与实机结果不由源码推导。

## 1. 实际环境契约

| 对象 | [CONFIRMED] 源码契约 | 限制与缘由 |
|---|---|---|
| 开发 Python | [pyproject.toml，L5–16](../../pyproject.toml#L5) 为 >=3.12,<3.13；Qt6/QFluentWidgets/requests/rookiepy/pywebview 等包 | Python 项目不等于所有 OS 都有发行支持。 |
| 发行构建 | [build-environment.json](../../build-environment.json#L1) 固定 Python 3.12.12、uv、Inno、py7zr；[build_environment preflight，L40](../../scripts/build_environment.py#L40) Windows x64 检查 | 锁定构建降低漂移；运行系统最低版本、ARM 实机兼容未由本轮验证。 |
| 主程序冻结 | [FluentYTDL.spec::Analysis/EXE/COLLECT，L78](../../scripts/FluentYTDL.spec#L78) 以根 main.py 组装 | 不是仅发布一个源码脚本；外部组件和 _internal 共同构成运行。 |
| 独立 updater | [updater.spec::EXE，L117](../../scripts/updater.spec#L117) onefile、无 Qt、内嵌 7zip/py7zr 与本地化目录 | 即使主程序 _internal 正被替换，更新器也需独立存活。 |
| ControlCenter | [package.json scripts，L6–14](../../../FluentYTDL-ControlCenter/package.json#L6) Vite/Vue 管理端 + esbuild Worker；[wrangler，L1–12](../../../FluentYTDL-ControlCenter/wrangler.toml#L1) Worker 入口、15 分钟 cron | Node 用于构建；运行是 Cloudflare Worker，不是部署 Node 服务。 |

## 2. 发布目标与文件所有权

[CONFIRMED] 唯一目标集合来自 [TARGET_OUTPUTS，L77–94](../../scripts/build.py#L77)。`all` 包含 full/app-core/setup/manifest/checksums；`7z`（full 别名）只有 full；`app-core` 还产生 manifest；setup 只有安装器；spec 只冻结自检，提前返回：[run_all，L806–821](../../scripts/build.py#L806)。

| 产物 | 载荷/使用者 | 所有权理由 |
|---|---|---|
| Full.7z | [CONFIRMED] 应用、工具与 portable.txt；[create_7z，L708–757](../../scripts/build.py#L708) 仅在归档时注入标记 | 标记决定便携数据根，不应污染共享 dist/app-core/setup。 |
| app-core.7z | [CONFIRMED] whitelist 含 EXE/_internal/licenses/声明文件/BUILD_INFO/updater.exe.new，排除 bin/当前 updater/portable；[pyproject，L133–141](../../pyproject.toml#L133) | 保持独立组件、账号与运行更新器的所有权；新 updater 用 .new 后投递。 |
| setup.exe | [CONFIRMED] Inno 安装应用、快捷方式、维护 helper，可选择 PATH；[FluentYTDL.iss，L139](../../installer/FluentYTDL.iss#L139) | 安装注册与便携解压不同；升级安装 Stop 不调用 Clean。 |
| update-manifest.json | [CONFIRMED] 绑定 app-core 名称、大小与 SHA；[release_pipeline.validate_result，L81–93](../../scripts/release_pipeline.py#L81) 对照报告检查 | 元数据必须指向同批真实字节，空壳清单不是合法更新载荷。 |

[CONFIRMED] 构建先解析最新上游为单个本地 snapshot；复用只在同次 build 内，显式 replay 为诊断，公开发布拒绝 replay/dirty identity：[build.ensure_tools，L407](../../scripts/build.py#L407)、[component_snapshot.prepare，L68](../../scripts/component_snapshot.py#L68)、[release_pipeline.publish，L175](../../scripts/release_pipeline.py#L175)。build/runs 隔离，成功后 latest-result 指向报告；报告路径不是产物永久存在的证明。

## 3. 安装形态与数据根

[CONFIRMED] `user_data_dir()` 优先 override，其次 frozen portable 的 EXE 目录，其次 installed LocalAppData，开发为项目目录：[paths，L81](../../src/fluentytdl/utils/paths.py#L81)。认证材料还有 `bin/dle_user`，不能只备份/清理 LocalAppData。Inno 默认 current-user，允许 all-users，并保存既有作用域：[iss，L84–86](../../installer/FluentYTDL.iss#L84)。

[CONFIRMED] 维护 Stop 按完整 app executable 路径和进程后代集合清理，并在强停前核对 CreationDate；发现同目录 updater 活跃则拒绝安装/卸载：[maintenance::Stop，L35–63](../../installer/maintenance.ps1#L35)。[INFERRED] 路径和实例身份限制避免误停另一便携安装，但 CloseMainWindow 后 5 秒强停仍不证明文件事务安全完成。

[CONFIRMED] 卸载只通过 Inno usUninstall 调 Stop → Clean → RemovePath；安装后仅 Register/AddPath：[iss::CurStepChanged/CurUninstallStepChanged，L190](../../installer/FluentYTDL.iss#L190)。Clean 按记录找 override 与用户根、拒绝 reparse、保留选定媒体后缀/下载目标、按已知名称清理。详见 [VS-19](vertical-slices/VS-19-install-uninstall.md)。

[CONFIRMED] 已发现清单不匹配：公告数据库默认是数据根 `announcements.sqlite3`，而 Clean 已知条目未包含它：[AnnouncementService，L159](../../src/fluentytdl/notification/announcement_service.py#L159)、[maintenance，L205–218](../../installer/maintenance.ps1#L205)。[UNKNOWN] 实际卸载残留未运行验证，因此不能承诺当前实现已删除所有本地隐私状态。

## 4. 应用更新与组件更新的隔离

[CONFIRMED] 应用更新经过 pending generation、活跃任务确认、退出后拉 updater；版本 PE 能力决定是否传 data-dir/SID。updater 在支持 READY 的路径用 PID+nonce；旧模式用 15 秒 survival。**另有启动器完全无法拉起新版时仍 commit、提示手动启动的分支**：[updater，L1663–1677](../../src/fluentytdl/core/updater.py#L1663)。不能概括所有启动失败都回滚。

[CONFIRMED] 组件安装由 DependencyManager 的 QProcess 启动主程序 `--update-worker`，按 stdin JSON 下载安装到 managed 目录；runtime resolver 另选 custom/PATH，绝不以 resolved 路径作为安装目标：[dependency_manager，L197–209](../../src/fluentytdl/core/dependency_manager.py#L197)、[DownloaderWorker.start，L909](../../src/fluentytdl/core/dependency_manager.py#L909)。组件没有应用 READY 协议，不能复用应用更新成功定义。

[INFERRED] 这三种生命周期分开，是为了让更新保留用户状态、工具安装尊重用户自管文件、卸载清除应用拥有内容。但当前各自的取消/强停/补偿能力不同；应以路径代码为准，不能把最强保证推广到所有维护操作。

## 5. ControlCenter 部署及局部成功窗口

[CONFIRMED] Worker 绑定 D1 `DB`、KV `SNAPSHOTS`、静态 assets，public GET 不触碰 D1/GitHub；管理访问校验 Access JWT 与写 Origin：[index.handle，L41–102](../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L41)。目前 handle 管理静态页面从 generated assets 返回，因此 wrangler 的 ASSETS 绑定存在也不代表该请求必调用 ASSETS.fetch。

[CONFIRMED] 发布 D1 batch 后才 rebuild KV；KV 失败保留已保存发布内容并尝试记录失败状态，记录本身也可失败。rebuild 的 catch 同时覆盖 KV 成功后的 D1 状态失败，因此报错不保证旧快照仍在。更新同步也先写 KV 再记 D1 成功，后者失败时可出现“公共内容已更新、状态仍旧或显示失败”的窗口：[announcements，L4–15](../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L4)、[updates，L74–81](../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74)。

[CONFIRMED] D1 operation_lock 的 240 秒租约无续租，finally 删除使用 token 条件：[types.locked，L22–27](../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22)。[INFERRED] 超租约运行可被后续调用取得同名锁，旧调用不误删新 token，但两个 action 仍可能重叠；未运行该交错，不宣称线上已经发生。

## 6. 接受证据分级

[RECOMMENDATION] 分别记录：源码/锁定检查 → 冻结自检 → 归档/安装器校验 → 受控安装/升级/回滚/卸载 → draft 字节 → public 下载/latest → ControlCenter 快照 → 客户端真实获取。任何一层成功不能代替下一层。当前只完成 source/static 文档证据，其它均为 UNKNOWN。

相关纵向切片：[VS-16 应用更新](vertical-slices/VS-16-app-update.md)、[VS-17 组件](vertical-slices/VS-17-component-update.md)、[VS-18 公告](vertical-slices/VS-18-announcements.md)、[VS-19 安装卸载](vertical-slices/VS-19-install-uninstall.md)、[VS-20 构建发布](vertical-slices/VS-20-build-release.md)。
