# 新版架构 V2：独立复核——系统、风险与发行边界

复核日期：2026-09-19。复核者为 Resource 调查代理；本报告只独立复核 Lead 的00/02/13/14与Repository的12/VS-16–20，**不把本人编写的04/07/11及VS-12–15列为独立通过**。所有结论为source/static，未启动产品、执行测试/构建/安装/卸载/网络请求，也未读取实际凭据或DB。

## 1. 判定规则与范围

`[CONFIRMED]`只用于本次回查可见代码条件；`[INFERRED]`用于风险后果；`[UNKNOWN]`用于生产发生与运行结果；`[RECOMMENDATION]`为建议。下面的“通过”只指指定静态主张，不是整章功能验收。

已全文阅读：[00](../00-system-overview.md)、[02](../02-module-map.md)、[13](../13-known-risks.md)、[14](../14-code-index.md)、[12](../12-deployment.md)、[VS-16](../vertical-slices/VS-16-app-update.md)、[VS-17](../vertical-slices/VS-17-component-update.md)、[VS-18](../vertical-slices/VS-18-announcements.md)、[VS-19](../vertical-slices/VS-19-install-uninstall.md)、[VS-20](../vertical-slices/VS-20-build-release.md)。14是概念入口索引，不按其声明误当动态函数调用图。

## 2. 修订要求

| ID | 被审主张 | 独立回查依据 | 裁决与要求 |
|---|---|---|---|
| RB-01 | VS-20 §4“失败保留旧snapshot并记失败”、§5“每key旧快照/错误状态” | [updates.synchronize](../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74)：KV put先于D1 state；catch内 [L80–81](../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L80)的读取/写状态也可失败 | **需要收窄。** `[CONFIRMED]` KV成功后D1失败没有回滚KV；不能保证返回错误时旧快照仍在，亦不能保证错误状态已成功记下。改为区分KV前失败、KV成功后state失败、catch记录再失败。已发作者，等待读回。 |
| RB-02 | 02 M13称“公告修订已读/确认” | [AnnouncementStore DDL/state](../../../src/fluentytdl/notification/announcement_service.py#L83)是notified/acknowledged；receive在通知push后mark notified | **术语修订建议。** `[CONFIRMED]` 应称“已通知/已确认”，与TaskDB通知的is_read分开，避免误导读状态同步。已发Lead，等待读回。 |

## 3. 关键风险条件复核

| 主张/范围 | 源码回查与结论 | 边界 |
|---|---|---|
| 00/02：独立POT EXE与TaskDBWriter线程 | `[CONFIRMED]` [POT argv](../../../src/fluentytdl/youtube/pot_manager.py#L202)使用provider EXE；[db_writer初始化](../../../src/fluentytdl/storage/db_writer.py#L40)建Queue/daemon Thread，同时[TaskDB](../../../src/fluentytdl/storage/task_db.py#L67)共享连接与写锁。**通过。** | 未把provider写成Node；未把已有writer误当未来建议。 |
| 13 R01/R13：退出仅收尾尝试 | `[CONFIRMED]` [shutdown](../../../src/fluentytdl/download/download_manager.py#L562)强停后wait(500)不查结果；writer [join(timeout)](../../../src/fluentytdl/storage/db_writer.py#L74)无is_alive/成功返回。**通过。** | `[INFERRED]` 文件和队列后果未复现；没有把调用顺序写成排空保证。 |
| 13 R02：WebView2日志旁路 | `[CONFIRMED]` [_log](../../../src/fluentytdl/auth/providers/webview2_provider.py#L109)直接open/append；[proxy_full](../../../src/fluentytdl/auth/providers/webview2_provider.py#L155)写入。**通过。** | 风险条件限输入含userinfo；没有声称查看到真实泄漏。 |
| 13 R03：POT所有权 | `[CONFIRMED]` [cleanup](../../../src/fluentytdl/youtube/pot_manager.py#L151)向默认端口范围POST shutdown，无owner检查；[Job](../../../src/fluentytdl/youtube/pot_manager.py#L70)匿名。**通过。** | 匿名Job仅约束子进程，不约束HTTP请求目标；多实例实际影响仍未知。 |
| 13 R04/R05、12/VS-16：应用更新例外 | `[CONFIRMED]` [无PID分支](../../../src/fluentytdl/core/updater.py#L1663)仍commit、提示手动、return1；[旧进程超时](../../../src/fluentytdl/core/updater.py#L1488)继续。**通过。** | 没有“非零就回滚”或“仅READY才清备份”的过强总结。 |
| 13 R06、VS-17：组件强停 | `[CONFIRMED]` [kill_locking_processes](../../../src/fluentytdl/core/updater_worker.py#L52)缺psutil分支taskkill /IM；有psutil按路径而非父PID。**通过。** | 文档正确不借ProcessManager的ppid保证代替该分支。 |
| 13 R07/R08：runfile保护区与GC | `[CONFIRMED]` [workers获取](../../../src/fluentytdl/download/workers.py#L2052)在[try](../../../src/fluentytdl/download/workers.py#L2162)之前，close在2325；[识别](../../../src/fluentytdl/auth/cookie_runfile.py#L37)异常False；[GC](../../../src/fluentytdl/auth/cookie_runfile.py#L91)按年龄。**通过。** | 没有将显式close缺口扩大成确定永久泄漏；年龄不当owner证明。 |
| 13 R09、VS-18/19：公告DB清理差异 | `[CONFIRMED]` [创建端](../../../src/fluentytdl/notification/announcement_service.py#L159)生成根announcements.sqlite3；[维护清单](../../../installer/maintenance.ps1#L205)缺此文件。**通过。** | 只认静态差异，实际安装卸载后残留未验证。 |
| 13 R10：选中账户与真相源 | `[CONFIRMED]` [set_current](../../../src/fluentytdl/auth/auth_service.py#L1307)忽略同步bool返回True；[同步](../../../src/fluentytdl/auth/auth_service.py#L1324)可保留旧源。**通过。** | 账户选择是事实，实际使用身份差异为条件性推断。 |
| 13 R11：after_fix取消等待 | `[CONFIRMED]` [cancel](../../../src/fluentytdl/download/workers.py#L791)唤醒pause不唤醒suspend；[wait](../../../src/fluentytdl/download/workers.py#L1475)无超时。**通过。** | 仅指出不自然唤醒该等待点，未宣称所有取消都卡死。 |
| 13 R12：详情容量残留 | `[CONFIRMED]` [MetadataFetchRunnable](../../../src/fluentytdl/download/extract_manager.py#L11)直接调用EntryDetailWorker.run，只接finished/error；[cancel](../../../src/fluentytdl/download/extract_manager.py#L139)不删active；[worker取消](../../../src/fluentytdl/download/workers.py#L579)直接return。**通过。** | 证明这两组件确实相接，而非仅同名取消API；实际UI容量故障未测。 |
| 13 R14/R15：写错边界 | `[CONFIRMED]` [_process](../../../src/fluentytdl/storage/db_writer.py#L166)吞单项异常；[batch fallback](../../../src/fluentytdl/storage/db_writer.py#L128)需异常外传；[config save](../../../src/fluentytdl/core/config_manager.py#L461)直接写吞错。**通过。** | 不能承诺整批失败都会回滚重试成功或UI设置一定落盘。 |
| 13 R16/R17：跨层相反状态/选项 | `[CONFIRMED]` [restore_state](../../../src/fluentytdl/download/workers.py#L742)写基线，而[首run](../../../src/fluentytdl/download/download_manager.py#L48)清基线；[quick single_worker](../../../src/fluentytdl/core/quick_add_worker.py#L129)设noplaylist=False，而[标准worker](../../../src/fluentytdl/download/workers.py#L1186)设True。**通过。** | 只认覆盖路径和静态冲突，最终媒体网站行为尚未知。 |
| 13 R18/R19与02候选能力 | `[CONFIRMED]` [VR FFmpeg](../../../src/fluentytdl/download/features.py#L624)局部Popen循环无同函数cancel；重新rg main/src中的post_verify/on_quality_warning/on_quality_ok/enqueue_quality只得定义/注释。**通过指定范围。** | 没有把完整后验质量闭环当运行事实；未排除反射/外部调用。 |
| 12/VS-18/20与13 R21/R22：云端事务/租约 | `[CONFIRMED]` [announcements rebuild](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L4)与[updates](../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74)均KV→状态；[锁](../../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22)240秒token租约无续租。**通过机制，RB-01需收窄局部摘要。** | 不据租约常量断言线上必超时；不假设D1/KV共同事务。 |

## 4. 发行与公开接口抽查

[CONFIRMED] 另行搜索`main.py`与`src`的NetworkWatchdog/network_watchdog/DiskMonitor/disk_monitor，仅返回network_watchdog类及模块单例、disk_monitor类，未见业务import/start；02保持候选代码而非已运行能力的表述准确。`pyproject.toml` console entry指`fluentytdl.main:main`，路径`src/fluentytdl/main.py`本轮Test-Path为False，13 R20没有据此误判冻结根main不可运行。

[CONFIRMED] VS-16对CheckSession至多一次换源且取消不换源的描述吻合 [_request](../../../src/fluentytdl/core/update_transport.py#L252)，锁为RLock。VS-17对QProcess stdin JSON、done不立即发成功、exit0且无error才发成功、只有progress重置stall的描述吻合 [DownloaderWorker](../../../src/fluentytdl/core/dependency_manager.py#L909)。它没有把QObject/QProcess误写QThread。

[CONFIRMED] 12/VS-18公读只KV、管理JWT issuer/audience/email、写Origin、内部Bearer以及generated静态asset分支吻合 [Worker.index](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L9)。本轮未访问部署，不验证实际环境变量、线上路由和资产新鲜度。

[CONFIRMED] VS-20对resolve版本/tag/HEAD/main祖先、beta不发布的描述吻合 [release_pipeline.resolve](../../../scripts/release_pipeline.py#L32)；validate_result重查目标文件集合、size/hash、manifest绑定吻合 [validate_result](../../../scripts/release_pipeline.py#L66)。其文档明确“测试存在不是已通过”“Actions artifact不是public release”，没有把本轮文档工作写成真实构建/发布。

## 5. 复核限制

本轮没有重新逐行审完全部打包代码、安装器、第三方依赖、所有Qt动态连接、全部符号索引；没有进行独立图渲染。14里inventory/index机器结果应由Lead最终生成并验证，不能从链接存在推出内容完整。RB-01、RB-02待作者修订后读回；其它“通过”是表中指定静态主张的有限结论。

## 6. 修订读回与关闭

2026-09-19同轮追加，保留上面审稿历史。

| 条目 | 独立读回 | 状态 |
|---|---|---|
| RB-01 | [VS-20 §4/§5](../vertical-slices/VS-20-build-release.md#4-controlcenter-同步是另一完成条件)已区分KV前失败、KV后D1失败及失败状态写入再失败，与updates.ts L74–81吻合 | **关闭，修订静态验收通过。** |
| RB-02 | [02模块表M13](../02-module-map.md)实际正文已改“公告修订已通知/已确认”，不再冒充is_read | **关闭，术语修订通过。** |

本报告指定复核项无未关闭文档修订要求。业务风险、运行缺证、未覆盖代码保持原分类；关闭文档问题不意味着修好了程序或已通过动态测试。
