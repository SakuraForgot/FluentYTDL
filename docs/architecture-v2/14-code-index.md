# 新版架构 V2 · 14 架构概念与代码双向索引

> source/static 索引。源码行号对应本轮工作树；代码变化后以符号和哈希重定位，不把行号当永久 API。

## 1. 从问题找到代码

| 概念/组件 | 源码入口与关键符号 | 调用关系 / 状态 / 资源 | 文档落点 |
| --- | --- | --- | --- |
| 应用启动 | [main](../../main.py#L289) | 参数→路径→QApplication→单实例→迁移→窗口；进程与数据根 | [VS-01](vertical-slices/VS-01-startup-shutdown.md) |
| 主窗口 | [MainWindow](../../src/fluentytdl/ui/reimagined_main_window.py#L161) | 配置窗/控制器/任务页；直接命令与信号混合 | [02 M01](02-module-map.md) |
| 解析配置 | [start_extraction](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1354) | 模式→提取worker；opts/flow/metadata | [VS-02](vertical-slices/VS-02-video.md)、[03](03-runtime-architecture.md) |
| 元数据服务 | [YoutubeService](../../src/fluentytdl/youtube/youtube_service.py#L177) | UI workers→service→CLI；Cookie快照/cache generation | [04](04-data-flow.md)、[VS-13](vertical-slices/VS-13-cookie-context.md) |
| 普通提取 | [InfoExtractWorker](../../src/fluentytdl/download/workers.py#L137) | 解析成功/错误信号；前置flow尚未有task | [VS-02](vertical-slices/VS-02-video.md)、[VS-05](vertical-slices/VS-05-playlist.md) |
| 详情提取 | [EntryDetailWorker](../../src/fluentytdl/download/workers.py#L518) | 列表详情；取消/完成/错误与槽回收 | [VS-04](vertical-slices/VS-04-channel.md)、[05](05-control-flow.md) |
| 列表调度 | [PlaylistScheduler](../../src/fluentytdl/ui/playlist_scheduler.py#L33) / [AsyncExtractManager](../../src/fluentytdl/download/extract_manager.py#L65) | URL队列/优先级→worker；不等于下载DB任务队列 | [VS-05](vertical-slices/VS-05-playlist.md)、[09](09-concurrency-model.md) |
| 行/选择模型 | [VideoTask](../../src/fluentytdl/models/video_task.py#L87) | metadata/thumbnail/detail/selection；UI状态与抓取结果分离 | [02 M03](02-module-map.md)、[04](04-data-flow.md) |
| 命令编排 | [AppController](../../src/fluentytdl/core/controller.py#L80) | UI→新增/删除/暂停/重建→manager；db_id/opts/文件删除请求 | [VS-08](vertical-slices/VS-08-quick-audio-section.md)、[VS-11](vertical-slices/VS-11-cancel-recovery.md) |
| 下载调度 | [DownloadManager](../../src/fluentytdl/download/download_manager.py#L153) | pump/create_worker/shutdown；pending/active；DBWriter queue | [03](03-runtime-architecture.md)、[06](06-state-machines.md) |
| 一轮执行 | [DownloadWorker](../../src/fluentytdl/download/workers.py#L597) | 标准/轻量/直链；pause/cancel/suspend；run/attempt/Staging | [VS-09](vertical-slices/VS-09-pause-resume.md)、[VS-10](vertical-slices/VS-10-retry.md) |
| 进程下载 | [DownloadExecutor](../../src/fluentytdl/download/executor.py#L273) | options→exe/env/argv→Popen→输出/异常；runfile/管道 | [10](10-error-recovery.md)、[07](07-resource-model.md) |
| CLI 协议 | [run_dump_single_json](../../src/fluentytdl/youtube/yt_dlp_cli.py#L1054) | -J/JSON/stderr/exit code；取消watcher与临时Cookie副本 | [08](08-dependency-model.md)、[VS-02](vertical-slices/VS-02-video.md) |
| 文件事务 | [StagingArea](../../src/fluentytdl/download/staging.py#L690) | create/verify/build_plan/commit/finalize/GC；phase/journal/payload | [06](06-state-machines.md)、[07](07-resource-model.md)、[VS-11](vertical-slices/VS-11-cancel-recovery.md) |
| 产物事实 | [Manifest](../../src/fluentytdl/download/staging.py#L322) / [CommitPlan](../../src/fluentytdl/download/staging.py#L295) | reported/reconcile/seal/变更/命名组；观察到≠已交付 | [04](04-data-flow.md)、[10](10-error-recovery.md) |
| 后处理 | [DownloadContext/Feature](../../src/fluentytdl/download/features.py#L23) | 标准worker固定顺序；加工文件并更新清单 | [VS-03](vertical-slices/VS-03-vr.md)、[VS-06](vertical-slices/VS-06-subtitle.md) |
| 字幕/封面快速通道 | [lightweight](../../src/fluentytdl/download/workers.py#L1988) / [cover direct](../../src/fluentytdl/download/workers.py#L2330) | 自建CLI→fastpath Staging；不同暂停/Feature行为 | [VS-06](vertical-slices/VS-06-subtitle.md)、[VS-07](vertical-slices/VS-07-cover.md) |
| 请求身份 | [youtube_request](../../src/fluentytdl/utils/youtube_request.py#L5) | enforce_cookie_mode；snapshot/merged opts/匿名参数 | [VS-13](vertical-slices/VS-13-cookie-context.md) |
| 认证与账号 | [AuthService](../../src/fluentytdl/auth/auth_service.py#L300) | 来源/账户选择→提取/同步；selected与已提交源可不同 | [VS-12](vertical-slices/VS-12-authentication.md)、[11](11-security-boundaries.md) |
| 凭据真相源 | [CookieSentinel](../../src/fluentytdl/auth/cookie_sentinel.py#L34) | 校验→replace→meta；平台refresh锁/保留旧材料 | [04](04-data-flow.md)、[VS-13](vertical-slices/VS-13-cookie-context.md) |
| 凭据运行副本 | [cookie_runfile](../../src/fluentytdl/auth/cookie_runfile.py#L58) | 托管匹配→临时复制→CLI→清理；自管/异常直通 | [07](07-resource-model.md)、[11](11-security-boundaries.md) |
| 登录子进程 | [WebView2CookieProvider](../../src/fluentytdl/auth/providers/webview2_provider.py#L495) | Process/Queue/profile；旁路日志/timeout/terminate | [VS-12](vertical-slices/VS-12-authentication.md)、[VS-15](vertical-slices/VS-15-diagnostics.md) |
| POT 服务 | [POTManager](../../src/fluentytdl/youtube/pot_manager.py#L31) | EXE/Job/端口/warm/recovery；端口操作owner需另查 | [VS-14](vertical-slices/VS-14-pot-runtime.md) |
| 真正的工具身份 | [resolve_runtime](../../src/fluentytdl/utils/ytdlp_runtime.py#L22) | custom/local/PATH/frozen；版本缓存与安装目标分开 | [08](08-dependency-model.md)、[VS-17](vertical-slices/VS-17-component-update.md) |
| 全局进程清理 | [ProcessManager](../../src/fluentytdl/core/process_manager.py#L44) | PID set/atexit/callback/ppid；不同于组件taskkill降级分支 | [09](09-concurrency-model.md)、[11](11-security-boundaries.md) |
| 配置 | [ConfigManager](../../src/fluentytdl/core/config_manager.py#L13) | load/migrate/set/save/configChanged；内存与磁盘窗口 | [04](04-data-flow.md)、[13 R15](13-known-risks.md) |
| 数据库 | [TaskDB](../../src/fluentytdl/storage/task_db.py#L40) | schema/共享连接/写锁/batch；task状态/opts/通知 | [04](04-data-flow.md)、[07](07-resource-model.md) |
| 后台写队列 | [TaskDBWriter](../../src/fluentytdl/storage/db_writer.py#L27) | enqueue→coalesce→batch→SQL；daemon/毒丸/join超时 | [09](09-concurrency-model.md)、[10](10-error-recovery.md) |
| 身份与结果 | [FlowTrace](../../src/fluentytdl/observability/trace.py#L42) / [TaskTrace](../../src/fluentytdl/observability/trace.py#L101) | flow→task/run/attempt；expected/actual/finish | [06](06-state-machines.md)、[VS-15](vertical-slices/VS-15-diagnostics.md) |
| 错误仲裁 | [diagnostics.engine](../../src/fluentytdl/diagnostics/engine.py) / [rules](../../src/fluentytdl/diagnostics/rules.py) | stderr/exit→主因+伴随→策略；UI消费 | [10](10-error-recovery.md)、[VS-10](vertical-slices/VS-10-retry.md) |
| 日志与导出 | [sinks](../../src/fluentytdl/observability/sinks.py) / [bundle](../../src/fluentytdl/observability/bundle.py#L87) | LRU handles/flush/cutoff/redact/ZIP/manifest | [VS-15](vertical-slices/VS-15-diagnostics.md)、[11](11-security-boundaries.md) |
| 更新检查会话 | [CheckSession](../../src/fluentytdl/core/update_transport.py#L240) | check_id/transport/换源方向和次数；元数据≠字节下载 | [VS-16](vertical-slices/VS-16-app-update.md)、[08](08-dependency-model.md) |
| 更新协调 | [ComponentUpdateManager](../../src/fluentytdl/core/component_update_manager.py#L191) | generation/待确认/下载/待拉起；app-core与工具不同生命周期 | [VS-16](vertical-slices/VS-16-app-update.md)、[VS-17](vertical-slices/VS-17-component-update.md) |
| 安装替换 | [updater.main](../../src/fluentytdl/core/updater.py#L1390) / [run_worker](../../src/fluentytdl/core/updater_worker.py#L169) | 备份/权限/READY/rollback；无PID特殊commit；组件safe_install | [12](12-deployment.md)、[VS-16](vertical-slices/VS-16-app-update.md) |
| 公告与通知 | [AnnouncementService](../../src/fluentytdl/notification/announcement_service.py#L147) / [NotificationCenter](../../src/fluentytdl/notification/notification_center.py#L15) | 独立公告DB与tasks通知表；revision确认/过滤/本地化 | [VS-18](vertical-slices/VS-18-announcements.md)、[04](04-data-flow.md) |
| 图片/翻译 | [ImageLoader](../../src/fluentytdl/utils/image_loader.py#L155) / [I18nManager](../../src/fluentytdl/core/i18n.py#L12) | Qt网络/磁盘缓存/全局signal；translator安装/替换 | [02支撑关系](02-module-map.md) |
| 云端管理与公开读取 | [Worker.handle](../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L41) | Access/Origin/internal token→D1；public→KV | [VS-18](vertical-slices/VS-18-announcements.md)、[12](12-deployment.md) |
| 安装卸载 | [maintenance](../../installer/maintenance.ps1#L1) / [Inno](../../installer/FluentYTDL.iss) | scope/action/已知根/清理名单；保留媒体与失败报告 | [VS-19](vertical-slices/VS-19-install-uninstall.md) |
| 发布 | [TARGET_OUTPUTS](../../scripts/build.py#L77) / [publish](../../scripts/release_pipeline.py#L175) | 锁环境/工具快照/包/草稿/精确字节/公开读回 | [VS-20](vertical-slices/VS-20-build-release.md) |

这些是重要概念入口，不声称每个符号均有独立 prose 段落。符号全量候选及源码清单见下。

## 2. 机器可用的反向索引

- [inventory.json](evidence/inventory.json)：源码路径/哈希/行数、Python 类与函数（含限定名称）、导入语句。以 `path + symbols[].name` 定位代码。
- [code-to-docs.json](evidence/code-to-docs.json)：源码文件→引用它的新版章节/切片→引用行号。由实际 Markdown 链接提取；这是文件级引用关系，不冒充函数调用图。
- [dependency-candidates.json](evidence/dependency-candidates.json)：src 内 import 边与 SCC 候选；条件/类型/局部导入也纳入，实际判定见 [08](08-dependency-model.md)。

正向查法：问题→本表→章节/切片→状态/资源实体→源码。反向查法：已有文件/符号→inventory 定位→code-to-docs 找说明→对应章节查读写/错误/退出。

## 3. 更新索引

在仓库根目录，使用已有 Python 执行：

```powershell
.venv/Scripts/python.exe docs/architecture-v2/tools/reference.py capture
.venv/Scripts/python.exe docs/architecture-v2/tools/reference.py index
.venv/Scripts/python.exe docs/architecture-v2/tools/reference.py verify
```

`capture` 建新源码快照，`index` 根据该快照和当前文档生成导入/反向引用候选，`verify` 验证链接与已记录集合哈希。不要为消除漂移警告盲目刷新 capture；先核对变化及受影响章节。

工具只解析源码，不 import 产品；输出仅写本目录 evidence。没有业务测试、构建、网络、安装/卸载副作用。新的未收录文件不会被“旧集合哈希一致”检查发现，新增模块需重做仓库普查并纳入 capture 范围。图渲染与源码链接语义仍需人工审阅。
