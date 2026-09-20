# 仓库、入口、发布与 ControlCenter：首轮事实草案

调查日期：2026-09-19。范围是当前本地工作树的只读静态取证；本文件是完整架构方案的输入，**不是完整架构验收报告**。既有未提交修改属于调查基线，不宣称工作树干净。本轮未启动桌面应用、未运行下载、未构建发布物、未访问线上管理接口、未检查生产绑定。

证据标签：`[CONFIRMED]` 表示本轮直接从代码或配置确认；`[INFERRED]` 表示由代码关系推导、尚缺运行证据；`[UNKNOWN]` 表示尚未取证；`[RECOMMENDATION]` 表示后续方案建议。代码引用使用当前文件出发的相对路径，`#L` 后为当前工作树行号；工作树变化后须复核。每个小节中的“缘由”是对现有约束的分析，不冒充历史作者的完整决策记录。

## 1. 系统边界与调查顺序

| 结论 | 代码证据 | 为什么需要单独分析 |
|---|---|---|
| [CONFIRMED] 桌面客户端采用 Python 3.12 + PySide6 + QFluentWidgets；应用声明依赖中没有 yt-dlp Python 包 | [pyproject.toml：project/dependencies，L1–17](../../../../pyproject.toml#L1) | Python 环境依赖与下载用外部工具是两个生命周期；只画 Python 包依赖图会漏掉主要执行边界。 |
| [CONFIRMED] ControlCenter 是同级独立目录的 Vue 3 管理界面与 Cloudflare Worker，不能当作桌面包的一个 Python service | [package.json：scripts/dependencies，L1–27](../../../../../FluentYTDL-ControlCenter/package.json#L1)、[Worker Env，L1–12](../../../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L1) | 两套语言、构建、认证、存储与部署边界；接口变化需两端共同验收。 |
| [CONFIRMED] 客户端既存在 GitHub 更新路径，也存在检查会话的换源逻辑 | [update_transport.py：CheckSession._request，L252](../../../../src/fluentytdl/core/update_transport.py#L252)、[CheckSession.manifest，L325](../../../../src/fluentytdl/core/update_transport.py#L325) | Cloudflare 不等于所有网络流量入口；应区分元数据获取、文件下载和公告获取。 |
| [UNKNOWN] 当前生产 Worker 版本、线上 KV 新鲜度、D1 迁移状态、GitHub Release 与本地构建身份是否一致 | 静态入口是 [wrangler.toml：Worker/bindings，L1](../../../../../FluentYTDL-ControlCenter/wrangler.toml#L1)；它只能证明配置意图 | 完整方案要列生产读回证据，但本轮方案调查不应据配置宣称线上已按此运行。 |

[RECOMMENDATION] 先按“桌面启动与生命周期 → 解析/下载运行链 → 数据与文件事务 → 更新发布 → ControlCenter 公共协议”建立系统地图，再检查横切层。这个顺序能先确立控制权和真实执行位置，避免按目录名称推定分层已经满足。依据是根入口的依赖注入、下载子进程入口和更新检查入口：[main.py::launch_main_window，L423](../../../../main.py#L423)、[DownloadExecutor，L273](../../../../src/fluentytdl/download/executor.py#L273)、[CheckSession，L240](../../../../src/fluentytdl/core/update_transport.py#L240)。

## 2. 实际入口与启动契约

### 2.1 确认的启动链

1. [CONFIRMED] 源码实际 GUI 入口是仓库根 [main.py::main，L289](../../../../main.py#L289)，该函数把 `src` 加入 `sys.path`，再处理 `--restart-parent-pid` 和 `--update-worker`（L296–307）。文件尾通过 `multiprocessing.freeze_support()` 进入 `main()`（L541 起）。
2. [CONFIRMED] 创建 QApplication 后执行单实例检查，随后调用数据迁移；服务导入与主窗口创建发生在后面：[main.py::main，L331–361](../../../../main.py#L331)、[launch_main_window，L423–430](../../../../main.py#L423)。[INFERRED] 该顺序是在防止多个进程同时迁移，以及单例导入时过早固定数据路径；该解释与入口中的注释一致，仍需启动/双实例测试验证实际效果。
3. [CONFIRMED] 主窗口拿到的是 `app_controller` 实例，随后 `show()`；`QTimer.singleShot(0, finalize_startup)` 在事件循环调度后宣布更新启动就绪：[main.py::launch_main_window，L424–465](../../../../main.py#L424)。[INFERRED] 启动完成不是“Python 导入成功”，也不是“窗口对象构造成功”，而是与更新回滚握手相连的系统边界。
4. [CONFIRMED] Cookie 启动刷新在 daemon Python 线程执行，POT 预热用异步入口；应用事件循环结束后停止 POT，再尝试拉起更新器和待处理重启：[main.py::launch_main_window，L467–491](../../../../main.py#L467)、[main.py::main，L493–537](../../../../main.py#L493)。[RECOMMENDATION] 启动、退出、更新重启应共同画时序图，注明 Qt 主线程、Python 后台线程及外部进程，不能用一张“main → window”图代替。
5. [CONFIRMED] `--build-self-test` 在 GUI/service 导入之前走独立分支：[main.py：顶层自检分支，L7–11](../../../../main.py#L7)。[RECOMMENDATION] 架构验收分开记录“冻结自检”“隔离 UI 启动”“真实账户/网络工作流”，自检通过不能推导三者全通过。

### 2.2 已发现的入口声明漂移

[CONFIRMED] [pyproject.toml：project.scripts，L62–64](../../../../pyproject.toml#L62) 声明 `fluentytdl = "fluentytdl.main:main"`，而当前 `src/fluentytdl/main.py` 不存在（本轮 `Test-Path` 返回 False，文件清单也未出现该模块）。冻结入口明确是 [scripts/FluentYTDL.spec：Analysis，L78–79](../../../../scripts/FluentYTDL.spec#L78) 的 `../main.py`。

[INFERRED] 这是 console script 声明与源码布局不一致，不能在方案里把 `fluentytdl` 控制台命令列为已验证可用入口；也不能据此断言当前冻结 EXE 无法启动。两个入口的解析路径不同。

[RECOMMENDATION] 后续在隔离环境生成/检查 console launcher，并单独运行受控入口验证；先决定该入口是否仍属于支持契约，再选择修正声明或补齐模块。本次仅记录，不顺带改业务代码。

## 3. yt-dlp 边界：CLI 子进程与插件必须区分

### 维护程序与工具入口补充

| 入口 | 代码与调用契约（[CONFIRMED]） | 后续取证边界 |
| --- | --- | --- |
| 独立 updater.exe | [updater.spec 的 entry_script](../../../../scripts/updater.spec#L21) 指向 [updater.py::main](../../../../src/fluentytdl/core/updater.py#L1390)，文件尾调用 main | 与 GUI 的生命周期分离；权限、父进程退出、READY 与回滚要独立建模 |
| 组件更新子进程 | 根 [main.py 的 --update-worker 分支](../../../../main.py#L304) 调 [updater_worker.run_worker](../../../../src/fluentytdl/core/updater_worker.py#L169)，模块也有独立入口 | 不要混同应用更新器；逐调用追踪 stdin/输出消息与目标组件 |
| 安装/卸载维护 | [maintenance.ps1 参数](../../../../installer/maintenance.ps1#L1) 定义 Register/Stop/Clean/AddPath/RemovePath；[Inno 调用](../../../../installer/FluentYTDL.iss#L185) 提取和执行辅助脚本 | 同一个工具有不同动作，入口存在不证明全部清理范围安全；按 action/scope 调查 |
| 本地构建与构建 GUI | [build.py::main](../../../../scripts/build.py#L1232)、[build_gui.py::main](../../../../scripts/build_gui.py#L881) | 属维护者操作链；不可画入用户下载进程树 |

这些补充由 Lead 在整合复核中回查源码；仅确认静态入口，不表示已启动或执行维护动作。

| 事实 | 证据 | 对完整方案的要求 |
|---|---|---|
| [CONFIRMED] 元信息提取构建 `yt-dlp.exe ... -J ... URL`，以 `subprocess.run`/`Popen` 执行 | [yt_dlp_cli.py::run_dump_single_json，L1054–1084](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L1054)、[执行分支，L1111](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L1111) | 说明 argv、环境变量、stderr、退出码、取消和 JSON 输出的协议，不能写成应用内 `YoutubeDL.extract_info()`。 |
| [CONFIRMED] 下载执行器也解析工具路径后创建 `Popen` | [executor.py::DownloadExecutor，L373](../../../../src/fluentytdl/download/executor.py#L373)、[Popen，L469](../../../../src/fluentytdl/download/executor.py#L469) | 解析成功与下载启动/成功是不同事实；同一工具也有不同调用路径。 |
| [CONFIRMED] yt-dlp 运行时身份有专门模块与解析函数 | [ytdlp_runtime.py::RuntimeIdentity / resolve_runtime，L17–42](../../../../src/fluentytdl/utils/ytdlp_runtime.py#L17)、[yt_dlp_cli.py::resolve_yt_dlp_runtime，L153](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L153) | 将“当前使用的 executable”和“受管理安装目标”分别建模；版本 UI 与真实执行器要用同一身份验证。 |
| [CONFIRMED] 包内确实存在 `from yt_dlp...`，位置是分发给 yt-dlp 的 POT 插件源码 | [getpot_bgutil.py：插件导入，L8–16](../../../../src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/getpot_bgutil.py#L8)、[getpot_bgutil_http.py，L7](../../../../src/fluentytdl/yt_dlp_plugins_ext/yt_dlp_plugins/extractor/getpot_bgutil_http.py#L7) | 不应机械地把这类导入当作 GUI 违反 CLI 边界；应标注插件的宿主进程和加载位置。 |
| [CONFIRMED] CLI 模块有插件同步和环境准备 | [yt_dlp_cli.py::sync_pot_plugins_to_ytdlp，L273](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L273)、[prepare_yt_dlp_env，L325](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L325) | 解释为什么安装一个 Python 依赖并不足以保证冻结 yt-dlp 发现插件；需要单独检查插件复制、路径、覆盖及失败回退。 |
| [CONFIRMED] 解析会复制 Cookie 运行文件并把副本交给子进程 | [yt_dlp_cli.py::run_dump_single_json，L1067–1074](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L1067) | 不能把“唯一 Cookie 真相源”误写成子进程直接写回同一个文件；真相源、请求快照与可丢弃运行副本是不同所有权。 |

[UNKNOWN] 本轮没有启动 yt-dlp，未证明当前机器选中的 executable、插件发现、POT 服务状态、代理链或在线解析可用。完整方案中的运行验收须记录 executable 路径/版本、脱敏 argv、插件可发现证据、退出码及真实结果。

## 4. 模块地图的首轮锚点

以下是模块责任取证入口，**不是已证明全仓严格分层的声明**。

| 范围 | 首轮确认的入口/职责 | 下一步要回答的问题与缘由 |
|---|---|---|
| UI 与控制器 | [CONFIRMED] [AppController，L80](../../../../src/fluentytdl/core/controller.py#L80) 有添加、删除、暂停、恢复等命令方法；[main.py，L430](../../../../main.py#L430) 注入窗口 | [RECOMMENDATION] 区分构造时依赖注入、Qt signal 调度和直接方法调用；目录“core”并不代表纯基础设施。 |
| 解析服务 | [CONFIRMED] [YoutubeService.build_ydl_options，L302](../../../../src/fluentytdl/youtube/youtube_service.py#L302)、[extract_info_sync，L1407](../../../../src/fluentytdl/youtube/youtube_service.py#L1407)、[extract_playlist_flat，L2103](../../../../src/fluentytdl/youtube/youtube_service.py#L2103) 分别承担请求选项与不同提取入口 | [RECOMMENDATION] 六模式各自列参数快照、缓存与重试；不能只讲普通单视频。当前文件含既有修改，引用行号须在主稿定稿前复核。 |
| 下载调度 | [CONFIRMED] [DownloadManager.pump，L376](../../../../src/fluentytdl/download/download_manager.py#L376)、[create_worker，L403](../../../../src/fluentytdl/download/download_manager.py#L403)、[shutdown，L562](../../../../src/fluentytdl/download/download_manager.py#L562) 是队列与生命周期锚点 | [RECOMMENDATION] 任务状态、运行尝试、取消、进程退出与持久化完成应分别取证，避免把进度信号误认成最终状态。 |
| 并发解析 | [CONFIRMED] [MetadataFetchRunnable，L11](../../../../src/fluentytdl/download/extract_manager.py#L11)、[AsyncExtractManager，L65](../../../../src/fluentytdl/download/extract_manager.py#L65) 有入队/取消/容量方法 | [RECOMMENDATION] 图中要区分解析池与下载并发槽位，否则无法解释大列表懒加载与下载限额的关系。 |
| 任务存储 | [CONFIRMED] [TaskDB，L40](../../../../src/fluentytdl/storage/task_db.py#L40) 有建表、迁移、批处理、状态更新和查询入口 | [RECOMMENDATION] 区分 UI 内存模型、SQLite 持久记录、文件系统产物；三者不是同一数据库事务。 |
| 文件提交 | [CONFIRMED] [StagingArea.create，L752–759](../../../../src/fluentytdl/download/staging.py#L752) 使用全长 UUID 事务子目录，[commit，L1295–1316](../../../../src/fluentytdl/download/staging.py#L1295) 设置 committing/committed | [RECOMMENDATION] 单独记录取消门、预留目标、WAL、失败裁决及崩溃保证的上限；这是数据安全而非临时目录命名风格。 |
| 进程管理 | [CONFIRMED] [ProcessManager，L44](../../../../src/fluentytdl/core/process_manager.py#L44) 有 PID 注册、清理和子进程查询 | [RECOMMENDATION] 核对进程树的真实所有权、优雅退出与强制终止，不据方法存在就宣称不会误杀或残留。 |
| 公告客户端 | [CONFIRMED] [AnnouncementStore，L77](../../../../src/fluentytdl/notification/announcement_service.py#L77)、[AnnouncementFetch，L121](../../../../src/fluentytdl/notification/announcement_service.py#L121)、[AnnouncementService，L147](../../../../src/fluentytdl/notification/announcement_service.py#L147) 分离本地存储、请求线程和服务 | [RECOMMENDATION] 公告已读/确认按修订建模，不能与下载通知简单合并；需连接 Worker schema 与本地过滤/确认协议。 |

## 5. 构建、发布、安装与更新是独立验收链

[CONFIRMED] 构建环境配置固定 Python 3.12.12、uv 0.11.24、Inno 6.7.1、py7zr 1.1.3：[build-environment.json，L1–6](../../../../build-environment.json#L1)。构建预检明确限制 Windows x64 与精确 Python 版本，并核对锁定包：[build_environment.py：平台/版本检查，L40–63](../../../../scripts/build_environment.py#L40)。这比“跨平台 Python 项目”更接近实际发布契约。

[CONFIRMED] 产物集合只由 [scripts/build.py::TARGET_OUTPUTS，L77–94](../../../../scripts/build.py#L77) 声明：

| target | 产物 |
|---|---|
| `all` | full.7z、app-core.7z、setup.exe、update-manifest.json、SHA256SUMS.txt |
| `7z` / `full` | full.7z |
| `app-core` | app-core.7z 与 update-manifest.json |
| `setup` | setup.exe |
| `spec` | 不进入上述发布产物分发；是冻结/自检目标 |

[CONFIRMED] `pyproject.toml` 的 `[tool.fluentytdl.build]` 定义 app-core 的包含/排除和运行数据禁入项；包含 `updater.exe.new`，排除 `bin`、运行中的 `updater.exe` 和 `portable.txt`：[构建载荷契约，L133–141](../../../../pyproject.toml#L133)。`create_7z()` 为 full 包单独加便携标记，[L708–757](../../../../scripts/build.py#L708)；成功结果写入独立报告并更新 `build/latest-result.json`：[write_result，L925–951](../../../../scripts/build.py#L925)。

[INFERRED] full、app-core、setup 不是同一目录的任意压缩副本。它们分别服务便携分发、程序自更新、安装注册，便携标记与运行数据混入会改变数据根或泄漏会话。后续应解释每项包含/排除的具体缘由，而非只列文件名。

[CONFIRMED] [release.yml，L58–94](../../../../.github/workflows/release.yml#L58) 将构建、验证产物暂存、Actions artifact 上传和条件公开发布分成步骤。[release_pipeline.py::resolve，L32–62](../../../../scripts/release_pipeline.py#L32) 检查发布条件与 main 祖先关系；[publish，L175 起](../../../../scripts/release_pipeline.py#L175) 检查构建身份、已有 release，先 draft 后发布；[verify_downloads，L110](../../../../scripts/release_pipeline.py#L110) 做远端下载校验。

[RECOMMENDATION] 架构方案应把“源码检查通过”“冻结自检通过”“包结构/归档验证”“安装升级回滚”“公开下载字节一致”“ControlCenter 元数据可读”列为分开的证据等级。不能由 Actions 绿色推导用户已能公开下载，也不能用 `latest-result.json` 指针替代检查它引用的文件真实存在和哈希一致。

[UNKNOWN] 本轮没有执行这些构建或发布阶段；也没有据静态 release 脚本证明已有线上发布满足所有要求。

## 6. ControlCenter 的真实协议与存储边界

### 6.1 公共读取、后台写入、同步入口

| 路径与权限 | 真实执行位置 | 事实与缘由 |
|---|---|---|
| `GET /v1/health` | [index.ts::handle，L44](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L44) | [CONFIRMED] 返回服务标识；[RECOMMENDATION] 不能把此结果当作 KV 快照与 D1 全健康证明。 |
| `GET /v1/announcements` | [handle，L47](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L47) | [CONFIRMED] 读取 `announcements` KV 快照。 |
| `GET /v1/updates/app?channel=stable|pre` | [handle，L48–52](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L48) | [CONFIRMED] 两个 channel 对应独立 KV key；未知 channel 拒绝。 |
| `GET /v1/updates/components/{component}?channel=...` | [handle，L53–58](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L53) | [CONFIRMED] 仓库映射表限制组件/频道组合；不接受任意上游 URL。 |
| `POST /internal/sync` | [handle，L61–75](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L61) | [CONFIRMED] Bearer 同步凭据，可接受不持久化的 CI GitHub 临时 token；这是后台机器接口。 |
| `/admin` 与 `/admin/api/*` | [authenticate，L9–21](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L9)、[handle，L77–102](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L77) | [CONFIRMED] Cloudflare Access JWT 验 issuer/audience/email；非 GET 管理接口检查 Origin；本地旁路只允许 localhost/127.0.0.1。 |
| `GET /admin/api/state` | [handle，L81–87](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L81) | [CONFIRMED] 查 D1 公告、最近 100 条历史、服务状态；公共快照与管理事实来源不同。 |
| `POST /admin/api/sync|republish`、公告 `save|publish|unpublish` | [handle，L89–92](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L89) | [CONFIRMED] 分开同步上游、重新分发、修改草稿/发布状态；后续解释为何“保存”不等于“用户可见”。 |

[CONFIRMED] 公共 `cached()` 只调用 KV `get`，检查 `generated_at`，超过 86400 秒拒绝，计算 SHA-256 ETag，支持 304，响应缓存 60 秒：[index.ts::cached，L29–38](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L29)。[INFERRED] 这样把公共流量与 D1/GitHub 的限流、认证和写入故障隔离；代价是显式的新鲜度窗口与最终分发延迟，不能宣称强一致实时读取。

### 6.2 D1、KV 与发布一致性

[CONFIRMED] [migrations/0001.sql，L1–16](../../../../../FluentYTDL-ControlCenter/migrations/0001.sql#L1) 定义四张表：`announcements`（draft/published/revision/status）、`history`（修订动作与内容）、`service_state`、`operation_lock`。KV 是对外投影，D1 是后台内容/状态存储；没有在这些代码中发现公共读取时回源 D1 的路径。

[CONFIRMED] [editAnnouncement，L18–46](../../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L18) 的 save 只写 draft；publish 校验草稿、critical 强制确认，D1 batch 更新 published/revision 并追加 history；之后 `rebuild()` 从 published 数据重建 KV。[rebuild，L4–15](../../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L4) 把 KV put 和其后的 D1 publication 状态写放在同一个 try；失败时尝试记录 `snapshot_write_failed`。该错误也可能发生在 KV 已更新而状态写失败之后，且错误状态记录自身仍可能失败，不能由错误码唯一推断 KV 未更新。

[INFERRED] D1 提交与 KV 分发不能描述成一个原子事务。界面可能看到“内容已保存，分发失败”，并通过 republish 恢复。这种显式分层有利于重试，但验收应验证用户可见快照最终追上保存状态。

[CONFIRMED] [types.ts::locked，L22–27](../../../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22) 通过 D1 条件插入/更新取得 240000 毫秒租约，并按 token 释放。[UNKNOWN] 本轮未验证操作超过租约时是否可能重入，也未运行并发测试；后续应把“锁期限/最长上游耗时/定时任务重叠”作为小型故障模型，不直接断言线上已有竞争问题。

### 6.3 GitHub 同步与管理 UI

[CONFIRMED] [updates.ts::repositories，L4–14](../../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L4) 管理 app stable/pre，以及 yt-dlp stable/nightly/master、FFmpeg、Deno、POT Provider、AtomicParsley。`synchronize()` 取 GitHub release/manifest 后写 KV，再写状态；KV 写入前的上游获取/校验失败保留既有快照，catch 尝试记录失败状态，限流时记录冷却：[synchronize，L50–88](../../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L50)。KV put 已成功而后续 D1 状态写失败时，仍会走同步失败分支；失败状态的查询/写入本身也可能失败。因此不能仅据 sync error 推断客户端仍读旧快照。

[CONFIRMED] 返回的二进制下载 URL 由 [shared::releaseUrl，L42–45](../../../../../FluentYTDL-ControlCenter/packages/shared/src/index.ts#L42) 生成 GitHub release 地址。当前 Worker 路由没有二进制代理、R2 文件分发入口。[RECOMMENDATION] 图中把“缓存 release 元数据”和“GitHub 下载二进制”画成两条链；否则会错误解释 Cloudflare 可用但 GitHub 资产下载失败的情况。

[CONFIRMED] [wrangler.toml：triggers，L11–12](../../../../../FluentYTDL-ControlCenter/wrangler.toml#L11) 声明每 15 分钟触发；[Worker scheduled，L116–118](../../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L116) 分别触发同步与公告重建。前端 [main.ts，L1–4](../../../../../FluentYTDL-ControlCenter/apps/admin/src/main.ts#L1) mount Vue；[App.vue：api helper，L21](../../../../../FluentYTDL-ControlCenter/apps/admin/src/App.vue#L21) 通过相对 `/admin/api/` 调用管理接口。生产与本地是否已采用这一配置属于运行态待证项。

[CONFIRMED] `package.json::check` 串联 typecheck、Vitest、admin/worker build：[package.json，L6–14](../../../../../FluentYTDL-ControlCenter/package.json#L6)。[tests/worker.test.ts，L18–112](../../../../../FluentYTDL-ControlCenter/tests/worker.test.ts#L18) 已有草稿隔离、KV 失败重试、公共 KV-only/ETag、过期、认证、Origin、临时 token 与 channel 隔离用例。[UNKNOWN] 本轮只定位测试，未运行，不能写“测试通过”。

## 7. 既有架构文档的使用方式与已确认冲突

[CONFIRMED] 仓库已有 [ARCHITECTURE_CN.md](../../../ARCHITECTURE_CN.md#L1)、[ARCHITECTURE_EN.md](../../../ARCHITECTURE_EN.md#L1)，以及 archive 下的 [ARCHITECTURE.md](../../../archive/ARCHITECTURE.md#L1)、[PROJECT_ARCHITECTURE.md](../../../archive/PROJECT_ARCHITECTURE.md#L1)、[PLAYLIST_ARCHITECTURE.md](../../../archive/PLAYLIST_ARCHITECTURE.md#L1)。它们应保留为历史/导航参考，新目录承担“现状证据 + 适配执行方案”，不能通过覆盖旧文件消除差异。

[CONFIRMED] 已发现实质冲突：现有中文架构文档 [§4.3，L324](../../../ARCHITECTURE_CN.md#L324) 明确说“沙箱使用 DB 主键，非 UUID”，并描述 `task_{id}` 直接装所有中间文件；当前 [StagingArea.create，L755–759](../../../../src/fluentytdl/download/staging.py#L755) 在任务目录下新增完整 UUID 的 `txn_<sid>`。旧文档 [§4.4，L333–335](../../../ARCHITECTURE_CN.md#L333) 还有绕过沙箱的概述，需要逐路径与当前 worker 核对，不能继续整体引用为现状。

[RECOMMENDATION] 主方案设置差异表：旧主张 → 当前代码 → 风险 → 待验路径 → 后续文档修订。缘由是架构文档经常被当成行为保证；若继续依据旧版 task 目录清理设计，可能破坏当前事务的隔离和不可取消提交边界。当前约束从 [staging.py::commit，L1288–1316](../../../../src/fluentytdl/download/staging.py#L1288) 取证，不能由历史图推断。

## 8. 下一阶段验收建议

| 优先级 | 工作包 | 最小证据与完成条件 | 缘由 |
|---|---|---|---|
| P0 | 启动、退出、重启与更新握手 | 根入口、冻结 spec、console script 分开核对；隔离数据根验证单实例/ready token；产出实际调用时序 | [RECOMMENDATION] 入口漂移已确认，启动是所有功能成立的前提。依据见 §2。 |
| P0 | 六模式执行与文件事务 | 每条路径记录服务入口、子进程或直链边界、StagingArea、失败/取消裁决、最终文件；补齐 commit 前后取消故障注入证据 | [RECOMMENDATION] 旧沙箱文档已失配，而误清理影响真实用户文件。依据见 §3、§4、§7。 |
| P1 | 模块依赖与 Qt 并发 | 静态 import 图与关键 signal/connect 逐边核对；注明对象线程归属及后台 worker 终止方式 | [RECOMMENDATION] AGENTS 的目标分层不等于当前实现；直接方法调用、信号调用与外部进程要分别解释。依据见 §4。 |
| P1 | 本地数据与身份 | 列 config/task DB/announcement DB/cookie truth/runfile/transaction journal 的根路径、所有者、生命周期与迁移 | [RECOMMENDATION] 同样叫“用户数据”的对象有不同存储与清理承诺，不能用一个路径概括。依据见 §2、§3、§4。 |
| P1 | 发布与更新协议 | 按 TARGET_OUTPUTS 检查产物集合、真实字节哈希、冻结启动、安装/回滚及公开下载；不复用陈旧构建指针作为唯一证据 | [RECOMMENDATION] 构建成功与发布成功不同，更新器消费的是精确载荷。依据见 §5。 |
| P1 | ControlCenter 契约 | 本地 check；请求级证明 KV-only 读取、鉴权/Origin、D1 保存后 KV 失败重试；超租约并发/快照过期场景；生产另做只读版本和快照核对 | [RECOMMENDATION] 跨存储一致性与租约期限是可靠性边界；本轮静态代码不足以证明线上符合。依据见 §6。 |

本文件只完成首轮仓库与系统边界调查；源码中尚未逐条跟踪的模式、测试未运行项及线上状态继续标为 UNKNOWN，不以“计划已写”代替“架构已验证”。
