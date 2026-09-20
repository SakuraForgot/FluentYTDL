# 新版架构 V2 · 运行、资源与状态独立反向验证

2026-09-19，Phase 16，source/static。复核者未撰写被审的 03/04/05/06/07/09/10/11 及 VS-01–15。先读取上述正文，再回到源码挑战事实范围；本报告不是产品验收。未 import 产品、运行测试/构建/安装、启动进程、访问网络或读取实际 Cookie/DB/日志。

裁决“通过”仅指所列 claim 在所列源码范围成立；“需修订”指正文需补重要边界；“未知”指无法用静态路径给出运行保证。没有对全部异常、全部文件或全部 Qt 线程时序证明正确。

## 1. 必要修订项

### RV-01 · GC 对未知状态和 journal 读取失败的默认动作

- **Challenged claim：** [10 错误恢复](../10-error-recovery.md) 与 [VS-11](../vertical-slices/VS-11-cancel-recovery.md) 列出 downloading/prepared/committed 回收、committing/rollback_failed 保留，容易被读成未知状态也保守保留。
- **源码：** [gc_orphans](../../../src/fluentytdl/download/staging.py#L1792) 先检查 txn 前缀、live ID 与年龄；[L1807](../../../src/fluentytdl/download/staging.py#L1807) 读 phase，只有 rollback_failed 和 committing 特判；[L1844](../../../src/fluentytdl/download/staging.py#L1844) 对其余值直接 rmtree。[ `_read_journal`](../../../src/fluentytdl/download/staging.py#L1864) 遇 OSError/ValueError 或非 dict 返回 None，未验证 journal version/schema。
- **裁决：需修订。[CONFIRMED/static]** 未知 phase、无法读取、非法 JSON、非对象 journal 均可能在年龄门后走默认删除；不是只有表内列出的三种合法 phase 才删除。
- **影响：[INFERRED]** 损坏/前后版本不兼容的 journal 不能自动获得“保留不确定现场”保证。此结论不等于本轮复现了误删或媒体丢失。
- **修订：** 已要求 Runtime 作者补入 10 与 VS-11，明确保守保留只覆盖两个被识别状态，并将未知 journal 纳入未来故障验证。

### RV-02 · write-ahead 顺序并不要求 journal 成功落盘

- **Challenged claim：** [05 控制流](../05-control-flow.md) 的 WAL 发布及 VS-11 的“journal write-ahead/tmp+replace 顺序，不等于 fsync 耐久”尚未覆盖实际写入失败时的分支。
- **源码：** [commit](../../../src/fluentytdl/download/staging.py#L1293) 先设 committing 并调用 `_write_journal`，随后 reserve/publish；[ `_write_journal`](../../../src/fluentytdl/download/staging.py#L1630) 对 open/json 写/replace 的 OSError 只发 journal_write_failed signal，不向调用者重抛。
- **裁决：需修订。[CONFIRMED/static]** 明确的 journal 写错误也不阻止用户目录副作用，不只是断电是否 fsync 的问题。
- **影响：[INFERRED]** 写失败后异常退出，磁盘可能缺 journal 或仍有旧 phase；RV-01 的 GC 会按读到的状态处理，而不是内存中已进入的 phase。未证明实际受影响字节或可复现概率。
- **修订：** 已要求 Runtime 作者明确“best-effort journal 顺序”，建议 Lead 风险表收录“journal 写失败 + 异常退出 + GC”组合；本轮不改业务策略。

### RV-03 · DBWriter 的 200 条上限只适用于正常收集

- **Challenged claim：** [03](../03-runtime-architecture.md) 实体表“Queue→至多 200 条批次”、[09](../09-concurrency-model.md) 上下文表同类表述范围过宽。
- **源码：** [ `_collect`](../../../src/fluentytdl/storage/db_writer.py#L98) 受 `_BATCH_MAX` 约束；[ `_drain_remaining`](../../../src/fluentytdl/storage/db_writer.py#L116) 收集当时队列剩余所有项，再一次 `_process_batch(rest)`，没有同样分块上限。
- **裁决：需修订。[CONFIRMED/static]** 正常批次最多 200，关闭 drain 不是。07 资源章已准确区分。
- **影响：[INFERRED]** 停止时事务占锁和内存量不能沿用正常批次预算；持续生产者也使 drain 与退出判断不同于封闭队列。
- **修订：** 已要求 03/09 与 07 一致。Queue 无容量、毒丸后不拒绝 enqueue、join 不检查存活的现有正文通过。

### RV-04 · 批回滚重试必须限定异常外逸

- **Challenged claim：** [10](../10-error-recovery.md) “batch 上层有回滚后逐条重试”若脱离 04 的说明阅读，仍可能误读为每项 SQL 失败都会得到重试。
- **源码：** [ `_process_batch`](../../../src/fluentytdl/storage/db_writer.py#L128) 捕获外逸异常后逐条调用 `_process`；[ `_process`](../../../src/fluentytdl/storage/db_writer.py#L166) 自己 catch Exception 并 warning。TaskDB [batch](../../../src/fluentytdl/storage/task_db.py#L200) 只对 yield 中外逸异常进入 rollback 分支。
- **裁决：需修订限定，不是实现与全文冲突。[CONFIRMED/static]** 04、07、13 已说明该边界；10 应同步写“仅外逸至 batch 的异常”。
- **影响：[INFERRED]** 单条失败可被吞掉，其余条目继续提交；不能推导整批必然回滚、失败条目已重放或 flush 成功。

## 2. 逐 claim 复核结果

| ID / 文档 claim | 回查源码、调用与边界 | 裁决 / 影响 |
| --- | --- | --- |
| RV-05 状态桥先接受 transition 再 enqueue，DB 不是事件逐条镜像 | [manager._emit_transition](../../../src/fluentytdl/download/download_manager.py#L33) 按 run 重置基线、同状态去重；completed/cancelled 拒绝迟到变更，但 error 可继续。[_on_unified_status](../../../src/fluentytdl/download/download_manager.py#L455) 成功后 enqueue；[coalesce](../../../src/fluentytdl/storage/db_writer.py#L148) 同(op,id)保留最后出现位置。 | **通过。[CONFIRMED/static]** 05/06 对 DB 终态分组与活 run 状态的区分正确。transition 表示已接受，不证明 SQL 已提交。 |
| RV-06 live 暂停/恢复保留同 run，停止后重建 worker | [pause/resume](../../../src/fluentytdl/download/workers.py#L765) 控制 pause Event；[controller](../../../src/fluentytdl/core/controller.py#L477) 活线程 resume，已结束则 create_worker(restore_db_id)。[batch_pause](../../../src/fluentytdl/core/controller.py#L537) queued 未运行者移除并 cancel。 | **通过。[CONFIRMED/static]** VS-09 没把按钮变色或停止 stdout 消费升级成 OS/网络瞬时暂停，也准确保留 queued“暂停”实际取消的差异。 |
| RV-07 after_fix cancel 不唤醒 suspend_event | [cancel](../../../src/fluentytdl/download/workers.py#L791) set cancel/pause 并终止已登记进程；[after_fix](../../../src/fluentytdl/download/workers.py#L1475) 创建独立 Event 后无超时 wait；[resume_suspension](../../../src/fluentytdl/download/workers.py#L759) 才 set 它。 | **通过。[CONFIRMED/static]** 05/09/10、VS-09–11 已准确标为终结风险。不能把“取消已请求”写成挂起线程必然退出。 |
| RV-08 shutdown 是有界收尾尝试 | [shutdown](../../../src/fluentytdl/download/download_manager.py#L562) grace wait 失败即 all_stopped=False、terminate、wait500不看bool；最终 writer join3秒。[remove_worker](../../../src/fluentytdl/download/download_manager.py#L599) 移除活 worker 不 wait。 | **通过。[CONFIRMED/static]** 03/07/09、VS-01/11 未承诺所有 finally/queued GUI slot/DB/子进程排空。返回 False 即使随后强停成功也不会改回 True。 |
| RV-09 三条执行路径最终交付接入 Staging | 标准 [verify/commit](../../../src/fluentytdl/download/workers.py#L1599)；快速共同 [land](../../../src/fluentytdl/download/workers.py#L1871)；轻量创建 [L2069](../../../src/fluentytdl/download/workers.py#L2069)、封面 [L2354](../../../src/fluentytdl/download/workers.py#L2354)。 | **通过但范围受限。[CONFIRMED/static]** 00 对“已进入产物执行”的限定正确；exe 缺失等前置失败可在事务前结束。快速 reconcile 普通异常被记录后仍继续 verify/plan，不能将每一步都画成失败即终止。 |
| RV-10 committed 不可反转与唯一 cancel gate | [commit](../../../src/fluentytdl/download/staging.py#L1283) gate 在 reserve 之前，临界区不再取消；成功记录 pending_cancel。 [finalize_failure/cancel](../../../src/fluentytdl/download/staging.py#L1517) 分 phase；标准最终路径 [L1615](../../../src/fluentytdl/download/workers.py#L1615) 在 commit 后发布。 | **通过。[CONFIRMED/static]** 06/VS-11 的正常 Python 裁决正确；不延伸到强停。快速 except 在 `_fastpath_fail` 前发 error/diagnosis 的边界，10 已主动保留。另须合并 RV-01/02。 |
| RV-11 verify 不是画质与所有附属要求的完整验收 | [verify](../../../src/fluentytdl/download/staging.py#L1172) re-stat kept；不要求 media 时直接返回，要求时校主媒体及最小尺寸；[build_plan](../../../src/fluentytdl/download/staging.py#L1227) 拒绝无stem、空成员、重复目标。 | **通过。[CONFIRMED/static]** VS-06/07 正确指出 rc0无文件仍可失败；03/10 没把 completed 等同完整解码/所有字幕/实际分辨率正确。 |
| RV-12 频道线程池取消及列表容量释放 | [Channel collector](../../../src/fluentytdl/download/workers.py#L360) with ThreadPoolExecutor、as_completed、取消 break；[EntryDetailWorker](../../../src/fluentytdl/download/workers.py#L579) 取消静默 return；[Runnable.run](../../../src/fluentytdl/download/extract_manager.py#L54) 直接调用 worker.run；active仅连完成/错误清理。 | **通过。[CONFIRMED/static]** VS-04/05 的非即时取消、静默取消后容量保留风险有源码支持；未实测 Qt信号线程投递及池析构耗时。 |
| RV-13 VR 转换与快速添加是额外取消边界 | [VR `_run_ffmpeg`](../../../src/fluentytdl/download/features.py#L624) 局部 p/readline，未登记 Worker executor/_proc_ref，无cancel/pause检查；[QuickAddWorker](../../../src/fluentytdl/core/quick_add_worker.py#L51) 独立解析编排。 | **通过所列静态范围。** VS-03/08 不把普通下载 cancel 推广到全部前后处理。VR元数据/转换成功率、QuickAdd真实列表输出仍 **[UNKNOWN]**。 |
| RV-14 请求模式不等于排队时的 Cookie 字节 | [cookie_runfile](../../../src/fluentytdl/auth/cookie_runfile.py#L58) 实际调用时 copyfile；[_is_managed_truth_source](../../../src/fluentytdl/auth/cookie_runfile.py#L37) 非托管/异常False直通；[sweep](../../../src/fluentytdl/auth/cookie_runfile.py#L91) 前缀年龄无owner。 | **通过。[CONFIRMED/static]** 04/07/11/VS-13 区分 mode、路径、当前字节。托管识别函数的注释“保证大小写”未被当作额外已验证保证。 |
| RV-15 轻量 runfile 显式保护窗口 | [workers L2052](../../../src/fluentytdl/download/workers.py#L2052) ExitStack enter；L2069 staging；try从 [L2162](../../../src/fluentytdl/download/workers.py#L2162) 开始；close在 [L2325](../../../src/fluentytdl/download/workers.py#L2325)。 | **通过。[CONFIRMED/static]** 初始化失败绕过该显式close；正文仅称窗口风险并保留对象回收未知，没有伪称每次永久泄漏。 |
| RV-16 选中账号不等于有效真相源已切换 | [set_current_webview2_account](../../../src/fluentytdl/auth/auth_service.py#L1307) 保存ID，忽略sync bool，返回True；[sync](../../../src/fluentytdl/auth/auth_service.py#L1319) 可因缺缓存返回False。 | **通过。[CONFIRMED/static]** VS-12/13 解释了保旧凭据收益及selected/effective差异；没有将本地AuthStatus当在线认证成功。 |
| RV-17 WebView2父子清理并非完整句柄证明 | [父 finally](../../../src/fluentytdl/auth/providers/webview2_provider.py#L627) 存活时 terminate/join5/kill，无kill后join及父Queue.close；[子 send](../../../src/fluentytdl/auth/providers/webview2_provider.py#L122) put/close/join_thread。 | **通过。[CONFIRMED/static]** 07/VS-12 区分父端、子端释放，实际句柄残留时间仍未知。 |
| RV-18 公共脱敏不能保护 WebView2 自写日志 | [provider._log](../../../src/fluentytdl/auth/providers/webview2_provider.py#L109) 直接append；[L155](../../../src/fluentytdl/auth/providers/webview2_provider.py#L155) 写proxy_full。[retention](../../../src/fluentytdl/utils/log_runtime.py#L184) 只收已知日志，排链接/bundles/active。 | **通过。[CONFIRMED/static]** 11/VS-15 仅对含凭据代理输入推导潜在泄漏，不声称读取了真实秘密。200MiB是可清理集合目标。 |
| RV-19 日志导出、异步写与停止边界 | [LogJobs](../../../src/fluentytdl/ui/components/common/log_jobs.py#L21) cancel只用于历史读取，export新non-daemon线程无共享取消检查；[sinks](../../../src/fluentytdl/observability/sinks.py#L72) LRU8/10MiB；[bundle](../../../src/fluentytdl/observability/bundle.py#L313) partial、replace、失败None与finally删tmp。 | **通过。[CONFIRMED/static]** VS-15 未把关闭窗口写成导出取消或 ZIP 存在写成证据完整。实际多窗口并发覆盖仍未知。 |
| RV-20 POT readiness、进程与 HTTP 所有权 | [端口health](../../../src/fluentytdl/youtube/pot_manager.py#L163) 仅connect；[cleanup](../../../src/fluentytdl/youtube/pot_manager.py#L151) 范围POST无owner；[wait](../../../src/fluentytdl/youtube/pot_manager.py#L807) join预热线程，无cancel参数；[ensure](../../../src/fluentytdl/youtube/pot_manager.py#L823) 触发式退避。 | **通过。[CONFIRMED/static]** VS-14/11 不混淆端口监听、生成token、CLI插件消费；也未用匿名Job证明HTTP关闭目标归属。 |

## 3. Lead 章节抽查与覆盖限制

[00 总览](../00-system-overview.md) 的“已进入产物执行的三路交付”“前置失败可能在创建事务前结束”、[02 模块图](../02-module-map.md) 的逻辑分类/非严格导入图、[13 风险](../13-known-risks.md) 对 SQL 单项吞错、after_fix、日志旁路等目前与本轮源码抽查吻合。未发现这些已读段落需要撤回主结论；建议 13 补 RV-01/02，防止正文中的文件安全边界没有进入风险索引。

本轮 VS-01–15 全文读过，源码抽查集中于状态桥、线程/进程终结、Staging、writer、Cookie/账号、日志/POT。未独立重做每个格式选择控件、每个字幕语言映射、每个网站后备HTTP、全部元数据字段与全部诊断规则。未执行任一动态测试，故“通过”不意味着每个切片已完成运行验收。

## 4. 本作者自有文件的外部复核修订记录

以下不是我对自有章节的独立验收，而是收到其他 Verification Agent 的发现后，回查源码并修正自己的稿件；关闭状态应由对应复核者读回确认。

- resource_survey 的 RB-01：VS-20 §4/§5 已改为 KV 写入前失败通常保旧，KV成功/D1失败不回滚，错误状态记录也可失败；依据 [updates L74–81](../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74)。同型边界同步修入 12 部署。
- runtime_survey：VS-17 已改为仅 PATH 分支检查 extra_exes，managed 主 EXE 存在即返回；依据 [resolve_exe L220–243](../../../src/fluentytdl/core/dependency_manager.py#L220)。
- runtime_survey：VS-18 rebuild catch 已涵盖 KV put 与 D1 state 两步骤，catch内state再失败也不保证预期503；依据 [announcements L4–15](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L4)。
- Lead：VS-19 的公告存储语义已统一为“已通知/已确认/快照”，避免与消息中心已读状态混淆。

## 5. 交付裁决

RV-01–04 是必要的事实范围修订，已通知对应作者；修订读回之前不宣称这些项目已关闭。其余所列关键 claim 在限定 source/static 范围通过；并发交错、崩溃耐久、真实账号/日志/网络行为仍为 UNKNOWN。建议先完成文档限定和风险索引，再在独立授权的隔离验证阶段取得运行证据。

## 6. 修订读回验收

2026-09-19 同轮读回：

- **RV-01 关闭（文档修订）**：10 错误恢复新增未知/损坏/不可读/非 dict journal 默认删除分支；VS-11 GC 表新增对应一行，并将“保守保留”限定到识别的两个状态。
- **RV-02 关闭（文档修订）**：10 与 VS-11 均明确 journal OSError 只记录后继续 reserve/publish，缺失/旧 phase 与后续 GC 的组合为条件性静态风险，没有冒充实际损失。
- **RV-03 关闭（文档修订）**：03 实体表、09 上下文与队列段落均将 200 限定于正常 `_collect`，说明关闭 drain 不受此限。
- **RV-04 关闭（文档修订）**：10 TaskDB 行已写“仅外逸到 batch 层的异常”触发回滚重试，和 04 保持一致。

本报告 68 个本地 Markdown 目标及显式行号范围静态检查通过。上述关闭只表示作者按实际源码补齐边界；业务缺口没有在本轮修复，动态 UNKNOWN 不因此关闭。Lead 风险总表 R25/R26 已读回，覆盖 journal 写失败后继续提交和 GC 未知/坏记录默认删除；本轮提出的必要文档修订均已闭合。
