# 运行时、任务状态与产物事务调查

调查日期：2026-09-19。范围为当前工作树源码，已完整阅读根目录 `AGENTS.md`。本调查未启动应用、未执行下载、未运行测试；`[CONFIRMED]` 仅表示源码能直接证明，不代表真实 Windows 运行已验收。规则文件是约束背景，以下结论以实现为依据。

## 1. 架构结论与阅读方法

[CONFIRMED] 六种产品入口不是六条独立下载引擎。视频、VR、频道、播放列表、字幕、封面共享配置窗口和任务调度；执行阶段在 `DownloadWorker.run()` 分成标准媒体、轻量字幕/封面提取、封面 URL 直下三路，三路都走 `StagingArea` 交付。证据：[入口分派 `start_extraction`，1354 行](../../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1354)、[执行分派 `run`，1143 行](../../../../src/fluentytdl/download/workers.py#L1143)、[快速通道 `_fastpath_land`，1871 行](../../../../src/fluentytdl/download/workers.py#L1871)。

[RECOMMENDATION] 总体方案应分别画“用户入口”“元数据解析”“任务执行”“事务交付”四层图。若直接画六条端到端流水线，会掩盖共享队列、相同取消边界和相同文件冲突策略，也容易把只修标准视频路径误当成覆盖全部模式。

## 2. 启动与退出调用链

| 环节 | 已确认实现 | 处理缘由与边界 |
|---|---|---|
| 参数/特殊进程 | [main.py 顶层，7、63 行](../../../../main.py#L7) 在常规服务加载前处理构建自检、数据目录与更新握手参数；[`main`，289 行](../../../../main.py#L289) 处理重启父进程、更新 worker | 数据目录必须在配置、日志和数据库导入前确定。自检不是完整业务启动。 |
| 单实例与迁移 | [`main`，331 行](../../../../main.py#L331) 创建 QApplication，获取单实例锁后调用 `migrate_user_data()`，再初始化语言、配置和主题 | 顺序防止两个进程同时迁移同一数据库，也防止导入期已固定日志/配置路径后再迁移。 |
| 服务与恢复 | [`launch_main_window`，423 行](../../../../main.py#L423) 导入 controller 并注入 MainWindow；[`DownloadManager.__init__`，168 行](../../../../src/fluentytdl/download/download_manager.py#L168) 立即调用 `load_unfinished_tasks()` | 服务单例导入有实际恢复副作用，不能把 import 当成纯定义，也不宜通过导入真实应用模块做文档扫描。 |
| UI 就绪与后台 | [main.py，465 行](../../../../main.py#L465) 用 `QTimer.singleShot(0, finalize_startup)`；[488 行](../../../../main.py#L488) 启动 daemon Cookie 线程；[494 行](../../../../main.py#L494) 根据设置异步预热 POT | 更新就绪信号在事件循环回合发出；Cookie 刷新和 POT 预热不应阻塞窗口显示。此握手不证明实际下载成功。 |
| 关闭窗口 | [`MainWindow.closeEvent`，596 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L596) 有可见托盘时隐藏并忽略关闭；否则 shutdown；`quit_app` 明确 shutdown 后 QApplication.quit | 关闭窗口、退出进程、取消任务是三个不同用户语义。 |
| 收尾 | [`DownloadManager.shutdown`，562 行](../../../../src/fluentytdl/download/download_manager.py#L562) 取消活 worker，逐个等待、必要时 terminate，最后调用 DB writer 停止/flush；[main.py，509 行](../../../../main.py#L509) 事件循环退出后停止 POT，再启动待执行 updater/restart | 这是收尾尝试的调用顺序；terminate 后 wait(500) 与 writer join(timeout) 未进一步确认全部退出/排空，不能据此保证更新替换前资源已完全释放。详见第 8 节及[独立复核 V-04](05-independent-review.md)。 |

表中实现均为 [CONFIRMED]；缘由取自明确代码约束与调用顺序，并不宣称异常路径已运行验证。

## 3. 六入口的真实路径

[CONFIRMED] 统一窗口通过 `downloadRequested` 信号进入主窗口 `add_tasks`，主窗口直接调用 controller，controller 创建 DB 行/worker 并交给 manager 调度。证据：[`_show_config_window`，680、708 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L680)、[`add_tasks` 内调用，835 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L835)、[`AppController.handle_add_tasks`，95、171 行](../../../../src/fluentytdl/core/controller.py#L95)、[`DownloadManager.create_worker`，403 行](../../../../src/fluentytdl/download/download_manager.py#L403)。

| 产品入口 | 解析与选择链（[CONFIRMED]） | 执行链与为何分流 |
|---|---|---|
| 普通视频 | [`show_selection_dialog`，746 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L746) → 配置窗口 → [`InfoExtractWorker.run`，168 行](../../../../src/fluentytdl/download/workers.py#L168) → `YoutubeService.extract_info_for_dialog_sync` | 选择格式后标准 DownloadWorker → DownloadExecutor → yt-dlp 子进程 → Feature → Staging。元数据解析与真正下载隔离，使用户选择发生在任务落库之前。 |
| VR | [`show_vr_selection_dialog`，766 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L766) → [`VRInfoExtractWorker.run`，453 行](../../../../src/fluentytdl/download/workers.py#L453) → [`extract_vr_info_sync`，1963 行](../../../../src/fluentytdl/youtube/youtube_service.py#L1963) | 使用 android_vr 专用解析；后续仍走标准下载，Feature 列表末尾有 VRFeature。普通解析的预览不能替代 VR 格式重新解析。 |
| 频道 | [`_show_channel_dialog`，783 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L783) 规范化 base URL → [`ChannelExtractWorker.run`，309 行](../../../../src/fluentytdl/download/workers.py#L309) → 按 tabs 并发调用 `extract_channel_flat` → 汇总为列表 | tabs 是不同枚举任务；每个选中条目创建独立下载任务，防止一个单视频任务再次展开整个频道。 |
| 播放列表 | `InfoExtractWorker.run` 根据 playlist_flat 调 `extract_playlist_flat`；[`setup_playlist_ui` 中 2562 行](../../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L2562) 建 AsyncExtractManager；[`PlaylistScheduler._pump`，297 行](../../../../src/fluentytdl/ui/playlist_scheduler.py#L297) → EntryDetailWorker | flat 首屏、逐项深解析与批量下载分开，避免为完整格式信息一次启动大量进程。调度器 task_id 使用 URL，不是数据库任务 ID。 |
| 字幕 | [`show_subtitle_selection_dialog`，812 行](../../../../src/fluentytdl/ui/reimagined_main_window.py#L812) → 普通/列表解析 → [`get_selected_tasks`，4023 行](../../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4023) 设置 skip_download 并禁用嵌入、封面等 | [`_run_lightweight_extract`，1988 行](../../../../src/fluentytdl/download/workers.py#L1988) 自行拼装 yt-dlp 子进程参数，不走 Executor/Feature；仍需事务交付，避免纯字幕写出半成品。 |
| 封面 | 配置窗口封面模式不读旧解析缓存；[`get_selected_tasks`，4045 行](../../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4045) 有选中图片 URL 时设 `__fluentytdl_is_cover_direct`，否则 skip_download + writethumbnail | [`_run_cover_direct_download`，2330 行](../../../../src/fluentytdl/download/workers.py#L2330) 或轻量提取。即使“直下”也是 yt-dlp 子进程；不应写成 Python HTTP 下载。绕过缓存的原因是旧 thumbnail URL 可能失效。 |

[CONFIRMED] `DownloadExecutor.execute()` 当前总是调用 `_execute_native()`；因此旧注释里的“Strategy 管线”不能作为现行策略对象调度的证据。[executor.py，299—352 行](../../../../src/fluentytdl/download/executor.py#L299)。标准任务合并选项后明确设置 `noplaylist=True`，防止单条任务二次变成列表下载。[workers.py，1174—1186 行](../../../../src/fluentytdl/download/workers.py#L1174)。

[CONFIRMED] 快速添加是额外编排入口：controller 创建一次 flow，QuickAddWorker 解析完后仍回到 `handle_add_tasks()`；不是第七条下载引擎。[controller.py，184 行](../../../../src/fluentytdl/core/controller.py#L184)。

## 4. 调度、线程、控制循环

| 所有者 | 控制范围（[CONFIRMED]） | 不能混同的语义 |
|---|---|---|
| Qt 主线程/窗口 | Signal/Slot 消费结果；列表分块 UI 更新；直接调用 controller 编排任务 | UI 是控制面，不等于所有业务调用都已经通过 signal。 |
| DownloadManager | [`pump`，376 行](../../../../src/fluentytdl/download/download_manager.py#L376) 消费 pending deque；`start_worker` 控并发；精确片段任务额外互斥；finished 后释放引用并继续 pump | active_workers 包含可展示的恢复壳，不等于所有 worker 都正在运行。并发数以 `isRunning()` 统计。 |
| DownloadWorker(QThread) | 一个应用约定的执行 run；包含自动重试循环、暂停 Event、取消 Event、错误修复 suspension Event | “暂停下载”“错误挂起等待修复”“质量守卫挂起队列”使用不同机制，不能共用一个布尔 paused 描述。 |
| Info/VR/Channel extract worker | 独立 QThread；频道内部还有 ThreadPoolExecutor 按 tab 枚举 | 一次解析 flow 可在多个线程产生事件，线程 ID 不可代替 flow/task 身份。 |
| AsyncExtractManager | [`MetadataFetchRunnable.run`，54 行](../../../../src/fluentytdl/download/extract_manager.py#L54) 在 QThreadPool 内直接调用 EntryDetailWorker.run；[`enqueue`，100 行](../../../../src/fluentytdl/download/extract_manager.py#L100) 用 mutex 管活动表 | EntryDetailWorker 在此不是通过自身 `start()` 启动；Qt QThread 对象的生命周期和执行代码所在的池线程需分别描述。 |
| PlaylistScheduler | [`set_viewport`，157 行](../../../../src/fluentytdl/ui/playlist_scheduler.py#L157)、`_pump`、`_crawl_tick` 管前台/后台队列、视口与抓取计时器 | 解析并发预算与下载并发预算独立；降低一个预算不会自动限制另一个。 |
| yt-dlp 子进程 | [`DownloadExecutor._execute_native`，469 行](../../../../src/fluentytdl/download/executor.py#L469) 启动并读输出；解析 helper [`extract_info` 附近，1124 行](../../../../src/fluentytdl/youtube/yt_dlp_cli.py#L1124) 有 cancel watcher，单次 communicate | 暂停 Python 输出消费不是 OS 级挂起子进程，不应承诺点击暂停后即时零网络传输。 |

[CONFIRMED] 标准下载暂停由 `_pause_event.clear()` 加 UI 状态完成；实际等待点在 `on_progress()` → `_wait_if_paused()`，每 0.5 秒检查取消。`resume()` set Event；`cancel()` set cancel Event、唤醒 pause Event，并终止 executor/快速通道子进程。证据：[`pause/resume/cancel`，765 行](../../../../src/fluentytdl/download/workers.py#L765)、[`_wait_if_paused`，891 行](../../../../src/fluentytdl/download/workers.py#L891)、[on_progress，1276 行](../../../../src/fluentytdl/download/workers.py#L1276)。

[INFERRED] 因暂停等待发生在进度回调，元数据解析、长时间无输出的子进程、部分后处理并不自动拥有同等暂停响应。快速通道输出循环只检查 is_cancelled，没有调用 `_wait_if_paused()`；见 [2163 行](../../../../src/fluentytdl/download/workers.py#L2163)、[2413 行](../../../../src/fluentytdl/download/workers.py#L2413)。应通过六模式验收矩阵界定“暂停”的实际能力，不能由按钮存在推断功能一致。

## 5. 身份、状态、结果的分层所有权

| 概念 | 来源与权威（[CONFIRMED]） | 为什么需要 |
|---|---|---|
| session / flow | `FlowTrace`；配置窗口/快速添加创建 flow，条目和新任务可共享 | 解析发生在 db_id 产生之前，需要先关联用户操作链。 |
| task | [`create_worker`，426 行](../../../../src/fluentytdl/download/download_manager.py#L426) insert_task 或复用 restore_db_id；`bind_task_id` 发 identity | 同一数据库任务可重试/重下多次，不能拿 worker 对象地址做稳定任务身份。 |
| run | [`TaskTrace.__post_init__`，119 行](../../../../src/fluentytdl/observability/trace.py#L119) 铸 run；worker.started 的 queued 桥写 last_run_id | 只为实际开始的 run 落身份；恢复壳不应覆盖上次中断 run。 |
| attempt | [`next_attempt`，143 行](../../../../src/fluentytdl/observability/trace.py#L143) 在同 run 内递增；自动/用户修复重试均使用 | 预算 `_auto_retries` 可重置，尝试序号不能倒退；否则证据混叠。 |
| staging_id | [`StagingArea.create`，739 行](../../../../src/fluentytdl/download/staging.py#L739) 全长 uuid 与独立 txn 目录 | 日志短 run_id 只适合展示；文件所有权必须防碰撞，重复下载同 task 也不可复用残骸。 |
| 展示状态 | [`_on_clean_update/effective_state`，717 行](../../../../src/fluentytdl/download/workers.py#L717) 合成 _final_state 与 QThread 状态 | progress 文本、有效 UI 状态、持久状态不能默认同时改变。 |
| 持久状态/transition | [`_on_unified_status`，455 行](../../../../src/fluentytdl/download/download_manager.py#L455) → `_emit_transition` → db_writer.enqueue_status | 这里接受状态并排队落库；发出 transition 并不等于 SQLite 已完成写入。 |
| run outcome | [`_finish_run`，1108 行](../../../../src/fluentytdl/download/workers.py#L1108) → [`TaskTrace.finish`，195 行](../../../../src/fluentytdl/observability/trace.py#L195)；独立启动恢复审计补旧 run | diagnosis 可按 attempt 多次出现，outcome 才是一轮执行的终局；degraded 从 expected−actual 派生。 |
| 文件事务 phase | StagingArea: downloading → prepared → committing → committed；失败可能 rollback_failed | 文件完成事实必须独立于 UI state；committed 后清理失败不能把已经交付的文件翻成下载失败。 |

[CONFIRMED] `_emit_transition` 去重同状态 tick，并拒绝 completed/cancelled 后的迟到状态；它不是穷尽合法状态边的严格状态机，error → parsing 明确允许自动恢复。[download_manager.py，22—76 行](../../../../src/fluentytdl/download/download_manager.py#L22)。

[CONFIRMED] `handle_pause_resume_task` 在旧 worker 已结束时创建新 worker、复用 db_id；活 worker 则 resume 同一线程。项目是主动规定“一 worker 一 run”，不能把源码注释里的“QThread 不能重用”写成 Qt 底层必然限制。[controller.py，477 行](../../../../src/fluentytdl/core/controller.py#L477)。

## 6. 失败、重试与启动恢复

[CONFIRMED] 标准下载在每次 `YtDlpExecutionError` 处 diagnose 并发 diagnosis：自动策略在预算内递增 attempt、cancel Event 可打断退避；never 策略直接终止；after_fix 或自动预算耗尽进入 suspension，等待用户修复后重试。`_auto_retries` 在用户修复后归零，但 attempt 继续递增。证据：[workers.py，1369—1509 行](../../../../src/fluentytdl/download/workers.py#L1369)。这一区分保护批量任务不被永久不可用的视频长期阻塞，但 after_fix 仍会占用当前运行名额。

[CONFIRMED] Executor 对 `rc != 0` 有实际产物检查：至少达到媒体字节阈值，并在有可比的预期体积时拒绝小于 50% 的残留；提音频、删除赞助段、片段下载不使用同一体积比判定。证据：[executor.py，684—747 行](../../../../src/fluentytdl/download/executor.py#L684)。[RECOMMENDATION] 方案要解释该路径是容忍 Windows 清理错误的恢复启发式，不能称为完整媒体解码校验或证明播放完整。

[CONFIRMED] 启动恢复仅查 unfinished + error，按 500 条分页；旧 active 状态改 paused 且不自动下载，error 受保留期限制但不会自动重试；skip_download 的提取任务改 error 后跳过壳恢复。旧 run 根据原始状态补 interrupted/restored_pending 事件。证据：[load_unfinished_tasks，174—290 行](../../../../src/fluentytdl/download/download_manager.py#L174)、[_emit_recovery_audit，96 行](../../../../src/fluentytdl/download/download_manager.py#L96)。

[CONFIRMED] 恢复最后按目录调用孤儿沙盒 GC 和 Cookie 运行副本清理；GC 默认仅处理达到 6 小时年龄阈值的目录，committing 仅清可证明属于本事务的占位符并保留沙盒，rollback_failed 保留。证据：[manager，291 行](../../../../src/fluentytdl/download/download_manager.py#L291)、[`gc_orphans`，1744 行](../../../../src/fluentytdl/download/staging.py#L1744)。

[INFERRED] `continuedl=True` 不等于重启后复用旧事务 `.part`：当前恢复创建新 worker，新 run 创建新的 UUID txn，GC 把旧目录视为独立残骸。应把“同 run 内继续/重试”“退出后重新执行同 task”分开描述，重新执行是否能恢复已有下载字节需专项确认。

## 7. 标准与快速通道共享的提交契约

[CONFIRMED] 标准路径为 `create → apply_to_opts → 字幕迟解析 → Feature.configure/on_download_start → emit_expect → prepare_attempt → Executor → register/reconcile/seal → Feature.on_post_process → verify → build_plan → commit → output_path_ready → emit_actual → cleanup → success`。具体证据：[workers.py，1229 行](../../../../src/fluentytdl/download/workers.py#L1229)、[1330 行](../../../../src/fluentytdl/download/workers.py#L1330)、[1526 行](../../../../src/fluentytdl/download/workers.py#L1526)、[1593 行](../../../../src/fluentytdl/download/workers.py#L1593)、[1648 行](../../../../src/fluentytdl/download/workers.py#L1648)。

| 边界 | 实现与处理缘由（[CONFIRMED]） |
|---|---|
| 沙盒内路径 | [`apply_to_opts`，839 行](../../../../src/fluentytdl/download/staging.py#L839) 区分 payload、parts、用户目标意图；[`assert_inside`，906 行](../../../../src/fluentytdl/download/staging.py#L906) 防 `..` 等逃逸。相对 outtmpl 本身不足以证明安全。 |
| attempt 产物发现 | [`prepare_attempt`，789 行](../../../../src/fluentytdl/download/staging.py#L789) 为每次 attempt 分 final 路径报告文件；[worker `_register_primary_media`，865 行](../../../../src/fluentytdl/download/workers.py#L865) 只读当前报告。原因是 yt-dlp print-to-file 为 append 语义，不能混用失败 attempt 的旧主媒体路径。 |
| reconcile 与 seal | [`reconcile`，982 行](../../../../src/fluentytdl/download/staging.py#L982) 扫 payload 补发现；`seal_discovery` 冻结发现；Feature 后续通过明确原子 API 修改清单 | 目录扫描、日志猜后缀、后处理自建文件不能各自成为独立产物真相。 |
| verify | [`verify`，1172 行](../../../../src/fluentytdl/download/staging.py#L1172) 重查保留文件存在性，若请求媒体则检查主媒体和最小字节数；没有要求所有附属期望先满足 | 字幕缺失可降级交付视频；提交前不得要求提交后才存在的 delivered token。 |
| 命名计划 | [`build_plan`，1227 行](../../../../src/fluentytdl/download/staging.py#L1227) 固定整组 stem、拒绝重复目标、拒绝空计划 | 主媒体和同组字幕/封面一起避让名称，不能单个文件各改各的名字。 |
| 不可取消临界区 | [`commit`，1283 行](../../../../src/fluentytdl/download/staging.py#L1283) 首先检查取消，再设 committing、写 journal，随后 reserve/publish | O_EXCL 预留已经产生用户目录副作用，所以不能把它放在取消门之前。 |
| 文件发布 | [`_reserve_group`，1327 行](../../../../src/fluentytdl/download/staging.py#L1327) O_EXCL；[`_publish_group`，1379 行](../../../../src/fluentytdl/download/staging.py#L1379) 先写记录再发布；跨卷 copy→验证→replace | 不能用 exists→move 的竞态逻辑；跨卷不能假设 rename 原子适用。整组事务不代表所有文件在一个 OS 操作中同时可见。 |
| 成功不可逆 | [`finalize_failure`，1517 行](../../../../src/fluentytdl/download/staging.py#L1517) 已 committed 返回 already_succeeded；[`finalize_cancel`，1574 行](../../../../src/fluentytdl/download/staging.py#L1574) committing/rollback_failed 仅 pending | 文件交付成功后，日志或 cleanup 异常只报告信号，不能告诉用户“下载失败”诱导重下。 |

[CONFIRMED] 快速通道通过 `_fastpath_open_staging` / `_fastpath_land` 复用上述交付契约，但没有 Executor 的 attempt 路径报告、Feature 后处理和 embed 证据。不能以“字幕/封面很小”省略同名、取消、半成品保护。[workers.py，1818 行](../../../../src/fluentytdl/download/workers.py#L1818)、[1871 行](../../../../src/fluentytdl/download/workers.py#L1871)。

[UNKNOWN] journal 的 write-ahead 顺序可由代码确认，但 `_write_journal()` 的 tmp+replace 本身不是断电耐久性证明；此调查未验证文件系统缓存刷新、断电恢复或跨卷故障时的实际行为。[staging.py，1630 行](../../../../src/fluentytdl/download/staging.py#L1630)。

## 8. 需要评审的规则与实现差异 / 风险假设

以下均未通过产品运行或测试确认，不直接宣称已发生用户故障。

| ID | 静态事实 | 推断与方案中的处理 |
|---|---|---|
| RT-01 | [CONFIRMED] `cancel()` 仅唤醒 pause Event；错误挂起使用无超时 `suspend_event.wait()`；UI retry 才调用 resume_suspension。证据：[workers 791](../../../../src/fluentytdl/download/workers.py#L791)、[1485](../../../../src/fluentytdl/download/workers.py#L1485)、[主窗口 1936](../../../../src/fluentytdl/ui/reimagined_main_window.py#L1936) | [INFERRED] 挂起中取消/删除/退出可能无法自然终止并释放下载名额。[RECOMMENDATION] 设计取消唤醒所有等待点的统一协议，验证 race：取消发生在 suspension 创建前/后。 |
| RT-02 | [CONFIRMED] shutdown 超过 grace 后直接 `worker.terminate()`，未按 staging.phase 协调；commit 内部已建立不可取消区。证据：[manager 562](../../../../src/fluentytdl/download/download_manager.py#L562)、[staging 1283](../../../../src/fluentytdl/download/staging.py#L1283) | [INFERRED] 强停可绕过 finally/提交补偿和 outcome；当前“每 run 一 outcome”只能描述正常 Python 收尾保证。[RECOMMENDATION] 用关闭协调器区分下载取消和提交排空；以真实停机故障注入确认。 |
| RT-03 | [CONFIRMED] Manager 只在 task_finished/task_error 清 active 表，取消本身保留记录；EntryDetailWorker 取消直接 return，不发上述信号。证据：[extract_manager 139](../../../../src/fluentytdl/download/extract_manager.py#L139)、[workers 579](../../../../src/fluentytdl/download/workers.py#L579) | [INFERRED] manager 被复用时可能容量残留。[RECOMMENDATION] 区分“无 UI 成功/失败提示的取消”和“必须执行的内部终结通知”；验证 cancel→新列表/同 URL 重试。 |
| RT-04 | [CONFIRMED] restore_state 写 `_last_transition_state`，但 `_emit_transition` 首次遇到未记录的 run 又清该字段。证据：[workers 742](../../../../src/fluentytdl/download/workers.py#L742)、[manager 48](../../../../src/fluentytdl/download/download_manager.py#L48) | [INFERRED] 恢复任务首条 transition 的 from 可能显示 `-` 而非恢复状态。[RECOMMENDATION] 明确定义“run 初态”与“task 旧态”字段，避免注释承诺与事件事实冲突。 |
| RT-05 | [CONFIRMED] UI 主窗口直接调用 controller.handle_add_tasks / pause_resume / remove，而规则文字要求 UI 事件处理仅发信号。证据：[主窗口 835](../../../../src/fluentytdl/ui/reimagined_main_window.py#L835)、[1147](../../../../src/fluentytdl/ui/reimagined_main_window.py#L1147) | [RECOMMENDATION] 先在架构决策中说明允许的同线程控制命令与跨线程结果信号边界；不得把实际图画成纯事件总线。修正规范或实现需要单独决策。 |
| RT-06 | [CONFIRMED] pause 只挡标准下载进度回调；快速通道未调用 pause 检查点。证据见第 4 节 | [RECOMMENDATION] 为不同阶段明确可暂停/仅可取消/不可取消能力，UI 文案跟随真实能力；以无输出、FFmpeg、快速通道场景验收。 |

## 9. 必须贯穿方案的纵向场景与验收理由

以下全部是 [RECOMMENDATION]，不是本次已通过的测试结果。

| 场景 | 必须观察的跨层事实 | 为什么不能只验一个函数 |
|---|---|---|
| 单视频成功 | 解析 flow→db task→run→attempt→staging→最终路径→DB 展示→outcome | 最常见 happy path 仍可能在输出路径、UI 信号与异步 DB 写之间脱节。 |
| VR 解析/转换 | android_vr 格式身份、Cookie 策略、VRFeature 输入/新文件登记、最终容器 | 初始解析成功不能代表 VR 专用格式或转换产物正确。 |
| 频道/列表部分失败 | tab 枚举结果、flat 首屏、逐项并发、取消容量释放、各条独立任务 | 列表有数据不代表某个 tab/条目的失败没被吞掉；不能让一个失败堵住全部任务。 |
| 字幕/封面两路 | 不下载主媒体、快通道参数、无媒体 verify、重名成组提交、空产物失败 | 标准视频测试不会经过快速通道；小文件同样可能覆盖或留下半成品。 |
| 自动重试→成功 | run 不变、attempt 单调、诊断按尝试、raw 输出累计、唯一 outcome | 只看最终 completed 会丢掉故障恢复成本和真实原因。 |
| after_fix→取消/退出 | suspension 创建时序、取消唤醒、名额释放、DB/outcome/staging | 同时跨 GUI 事件、线程 Event、队列和事务，是最容易缺失终结路径的组合。 |
| 下载中暂停→恢复 | 子进程实际活动、进度冻结、Event 状态、恢复同 run | UI 显示 paused 不足以证明传输已经停止。 |
| committing 时取消/关闭 | cancel gate 前后、占位符与 WAL、完整交付/补偿、pending_cancel、终态 | 防止“UI 已取消而文件部分提交”，也防止强停破坏不可逆成功边界。 |
| 崩溃→重启 | 原 DB state/last_run、恢复审计、暂停壳、错误保留、旧 txn GC | 启动可展示任务与字节级断点续传是不同能力；旧 sandbox 不可误当新结果。 |
| 提交后日志/清理失败 | 文件仍完整、outcome success、signal 记录、GC 延迟清理 | 可观测性必须帮助解释问题，不能成为下载失败来源。 |

[CONFIRMED] 仓库已存在与这些合同相关的测试文件：[`test_download_staging.py`](../../../../tests/test_download_staging.py)、[`test_download_artifact_architecture.py`](../../../../tests/test_download_artifact_architecture.py)、[`test_observability_contract.py`](../../../../tests/test_observability_contract.py)、[`test_observability_threads.py`](../../../../tests/test_observability_threads.py)、[`test_channel_tab_status.py`](../../../../tests/test_channel_tab_status.py)、[`test_playlist_authcheck.py`](../../../../tests/test_playlist_authcheck.py)。本次仅检视文件/部分用例声明，未声称这些测试覆盖了全部风险，亦未声称测试通过。

[RECOMMENDATION] 后续先复用现有合同测试并补“取消唤醒、关机提交、解析取消释放、恢复首状态”四条缺口，再进行隔离数据目录的 Windows 端到端验收。顺序的缘由是先证实终结与文件安全边界，再扩大网络和 UI 场景；不要在架构方案阶段顺手修改业务实现。
