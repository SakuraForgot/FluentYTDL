# 新版架构 V2 · 仓库与执行入口地图

证据口径：2026-09-19 当前工作树的 source/static 阅读；客户端 HEAD 为 `c441f2cde2e7973fe5902da50358f25c37c6525f`、分支 `release/3.7.3`，存在既有修改，不能把本章等同于干净 commit 快照。未运行产品、安装、联网、构建或测试。`[CONFIRMED]` 是实现直接可见，`[INFERRED]` 是解释，`[UNKNOWN]` 是缺证，`[RECOMMENDATION]` 是后续建议。相对链接行号对应调查时工作树。

## 1. 仓库不是单一程序

| 边界 | 实际内容、入口与输出 | 责任与限制 |
|---|---|---|
| FluentYTDL 客户端 | [CONFIRMED] [根 main.py::main，L289](../../main.py#L289) → QApplication → app_controller → MainWindow | Windows 桌面 GUI、解析下载与本地状态；UI 进程包含 Qt 服务和线程，不是网页 frontend/backend。 |
| 应用更新器 | [CONFIRMED] [updater.py::main，L1390](../../src/fluentytdl/core/updater.py#L1390)，[updater.spec::entry_script，L21](../../scripts/updater.spec#L21) | 独立 updater.exe 替换应用载荷、监护新程序；不依赖 Qt，不能与组件下载 worker 混为一物。 |
| 组件安装 worker | [CONFIRMED] [main.py::main，L303](../../main.py#L303) 识别 `--update-worker` → [updater_worker.run_worker，L169](../../src/fluentytdl/core/updater_worker.py#L169) | 仍由当前 Python/FluentYTDL.exe 以特殊模式启动；stdin JSON 参数、stdout JSONL 消息，不开 QApplication。 |
| 安装维护 | [CONFIRMED] [FluentYTDL.iss::Maintain，L159](../../installer/FluentYTDL.iss#L159) → [maintenance.ps1 参数与动作，L1](../../installer/maintenance.ps1#L1) | Inno 执行安装/卸载；PowerShell Stop/Register/Clean/AddPath/RemovePath 负责特定资源。 |
| 构建发布工具 | [CONFIRMED] [build.py::run_all，L806](../../scripts/build.py#L806)、[release_pipeline.py::resolve，L32](../../scripts/release_pipeline.py#L32) | 锁定构建、组件快照、产物校验与公开发布；维护者路径，不参与普通下载执行。 |
| 同级 ControlCenter | [CONFIRMED] [Worker::handle，L41](../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L41)、[Vue createApp，L1](../../../FluentYTDL-ControlCenter/apps/admin/src/main.ts#L1) | 独立 TS/Vue 项目；更新元数据和公告的公共读取、后台管理、定时同步；不代理视频或 GitHub 二进制。 |

## 2. 桌面入口的先后约束

[CONFIRMED] 根入口先处理 `--build-self-test`、`--admin-mode`、`--data-dir`、`--update-ready-token`；`main()` 才补 `src` 路径，处理重启父 PID 和组件 worker 分支。普通路径在 QApplication 和单实例锁之后迁移数据，再加载服务：[main.py 顶层，L7–69](../../main.py#L7)、[main，L289–361](../../main.py#L289)。

[CONFIRMED] [launch_main_window，L423–491](../../main.py#L423) 注入 app_controller、显示窗口、下一 Qt 回合发 finalize_startup，并启动 Cookie daemon 线程；POT 在 [L493](../../main.py#L493) 异步预热。退出事件循环后依次尝试 stop POT、启动待处理 updater、启动待处理普通重启：[L509–538](../../main.py#L509)。

[INFERRED] 把路径环境设置放在服务导入前，是因为日志/配置/DB 单例会在导入或初始化时固定根路径；把 READY 放到事件循环回合，能区分“创建了窗口对象”和“已经进入 UI 事件循环”。代价是启动顺序本身成为跨模块契约。READY 不是下载验收，关闭函数被调用也不是所有线程已经停止，详见 [应用更新切片](vertical-slices/VS-16-app-update.md)。

[CONFIRMED] 入口声明存在漂移：[pyproject.toml::project.scripts，L62–64](../../pyproject.toml#L62) 指向 `fluentytdl.main:main`，但当前 `src/fluentytdl/main.py` 不存在；实际 [FluentYTDL.spec::Analysis，L78–79](../../scripts/FluentYTDL.spec#L78) 用根 main.py。[UNKNOWN] 未运行 console launcher，不将该声明列为可用入口，也不据它断言冻结 EXE 失效。

## 3. 源码区域到真实职责

| 区域 | 代码锚点与控制/数据责任 | 需要与它一起读的边界 |
|---|---|---|
| ui | [CONFIRMED] [MainWindow._show_config_window，L680](../../src/fluentytdl/ui/reimagined_main_window.py#L680) 建配置窗口、连接 downloadRequested；[add_tasks，L826](../../src/fluentytdl/ui/reimagined_main_window.py#L826) 直接调用 controller | UI 的同线程控制命令与后台结果 Signal 分开，不能把全部业务交互画成事件总线。 |
| core/controller | [CONFIRMED] [AppController，L80](../../src/fluentytdl/core/controller.py#L80) 编排添加、删除、暂停恢复与快加 | 目录名 core 不代表它没有业务依赖；函数级 lazy import 也是依赖。 |
| youtube | [CONFIRMED] [YoutubeService.build_ydl_options，L302](../../src/fluentytdl/youtube/youtube_service.py#L302) 组请求；[yt_dlp_cli.run_dump_single_json，L1054](../../src/fluentytdl/youtube/yt_dlp_cli.py#L1054) 执行 CLI | Python options、Cookie 模式、POT/JS、argv、stderr、JSON 是连续但不同的表示。 |
| download | [CONFIRMED] [DownloadManager.pump，L376](../../src/fluentytdl/download/download_manager.py#L376)、[Worker.run，L1143](../../src/fluentytdl/download/workers.py#L1143)、[Executor，L273](../../src/fluentytdl/download/executor.py#L273)、[StagingArea，L690](../../src/fluentytdl/download/staging.py#L690) | 调度、执行、文件事务分开；六入口映射到三个执行通道，两个快速通道同样有 Staging。 |
| processing | [CONFIRMED] [features.py](../../src/fluentytdl/download/features.py) 将字幕/封面/VR 等处理挂入标准下载 | 用户请求、处理实际产物、最终保留/交付之间由 Manifest 传递所有权，不能只按后缀猜。 |
| auth | [CONFIRMED] [CookieSentinel._commit_to_truth_source，L382](../../src/fluentytdl/auth/cookie_sentinel.py#L382)、[cookie_runfile，L58](../../src/fluentytdl/auth/cookie_runfile.py#L58) | 账户来源、平台真相源、请求身份快照、子进程可写副本不是同一文件。 |
| storage | [CONFIRMED] [TaskDB，L40](../../src/fluentytdl/storage/task_db.py#L40) 共用连接/锁；[TaskDBWriter，L40](../../src/fluentytdl/storage/db_writer.py#L40) 独立线程队列 | UI/worker 入队与 SQL 提交之间有时间窗口；文件 commit 另属 Staging。 |
| diagnostics/observability | [CONFIRMED] [diagnostics.engine](../../src/fluentytdl/diagnostics/engine.py)、[events.emit_event，L382](../../src/fluentytdl/observability/events.py#L382)、[TaskTrace，L126](../../src/fluentytdl/observability/trace.py#L126) | 根因裁决、成功路径信号、单 run 终态不是同一计数。 |
| 更新/公告 | [CONFIRMED] [CheckSession，L240](../../src/fluentytdl/core/update_transport.py#L240)、[ComponentUpdateManager，L191](../../src/fluentytdl/core/component_update_manager.py#L191)、[AnnouncementService，L147](../../src/fluentytdl/notification/announcement_service.py#L147) | 应用更新、组件安装、公告确认分别有状态与持久化。 |
| models/utils | [CONFIRMED] [models/video_task.py](../../src/fluentytdl/models/video_task.py)、[utils/paths.py](../../src/fluentytdl/utils/paths.py)、[utils/ytdlp_runtime.py](../../src/fluentytdl/utils/ytdlp_runtime.py) | DTO、路径、身份是跨模块契约；不能从“工具函数”名称推导无状态无副作用。 |

## 4. ControlCenter 内部地图

[CONFIRMED] `apps/admin` 管内容编辑；`apps/worker` 管路由/鉴权/同步/公告；`packages/shared` 提供 Zod schema、Markdown 子集、release URL 编码；`migrations` 定义 D1；`scripts/bundle-assets.mjs` 把后台静态内容纳入构建。证据：[package.json::scripts，L6](../../../FluentYTDL-ControlCenter/package.json#L6)、[shared，L6](../../../FluentYTDL-ControlCenter/packages/shared/src/index.ts#L6)、[0001.sql，L1](../../../FluentYTDL-ControlCenter/migrations/0001.sql#L1)。

[CONFIRMED] Worker public GET 使用 KV；admin GET 使用 D1；发布先 D1 再 KV；GitHub 同步逐 channel 更新 KV。`sync` 与 `publication` 为不同锁名，租约 240 秒无续租：[types.locked，L22](../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22)。这是本地客户端之外的独立服务，不是下载器本体的后台任务 DB。

## 5. 当前支持边界及后续检验

[CONFIRMED] 发行构建实际限制 Windows x64、Python 3.12.12：[build_environment.py，L40](../../scripts/build_environment.py#L40)。依赖声明 `[3.12,3.13)` 比构建精确版本宽；源码非 Windows 分支并不构成其它平台发行验收。桌面自带 Qt，WebView2 登录另需运行时探测，下载引擎需要外部 executable。

[UNKNOWN] 当前机器真实选中工具版本、最终安装包可运行性、最低 Windows 版本和 ARM 兼容实机结果、线上 Worker/绑定与源码一致性均未验证。[RECOMMENDATION] 后续以独立冻结启动、安装升级回滚、在线只读身份与受控下载验收补证，不能由文件地图替代运行接受标准。

[CONFIRMED] 旧架构的“db_id 非 UUID 沙箱”与当前 `StagingArea.create` 的 `task/txn_<完整 UUID>` 不同：[create，L755](../../src/fluentytdl/download/staging.py#L755)。旧文档仅作为漂移来源，本目录没有继承其行为保证。
