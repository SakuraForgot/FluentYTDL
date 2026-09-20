# 独立复核：运行、资源调查与方案 01–04

复核日期：2026-09-19。复核者为 Repository 调查代理，**未撰写本次受审的运行调查、资源调查与方案 01–04**。本轮没有把本人编写的仓库调查作为独立复核对象。只读回查源码，没有启动产品、跑测试、读取真实凭据或操作线上。

状态：以下记录的是审稿当时结论与修订要求；作者/Lead 修改后仍须读回确认。`通过` 仅表示指定静态主张与本轮回查代码一致，不是整个模块或运行场景验收通过。

## 必须修正与补充

| ID | 被审主张 | 源码证据 | 裁决 | 影响与修订要求 |
|---|---|---|---|---|
| V-01 | [资源调查 §2](03-resource-survey.md) 建议“只有证明吞吐瓶颈或所有权问题后再讨论独立 writer 队列”；§8 将 DB writer 显式 shutdown 列为 UNKNOWN | [db_writer.py::TaskDBWriter.__init__，L40–43](../../../../src/fluentytdl/storage/db_writer.py#L40) 创建 Queue、创建 daemon `Thread(target=self._run)` 并 start；[flush_and_stop，L74–77](../../../../src/fluentytdl/storage/db_writer.py#L74) 投毒丸并 join；[_run，L79–96](../../../../src/fluentytdl/storage/db_writer.py#L79) 消费、drain 后退出 | **必须修正。[CONFIRMED] 独立 writer 线程与队列已存在。** | TaskDB 共享连接/写锁与 TaskDBWriter 后台线程是两个同时成立的机制，不能二选一。资源表应新增 writer 所有权/启动/消费/关闭；主稿 [04 §12](../04-design-rationale.md) 应正面写明源码已证，删除仅因名字无法确认的悬置表述。TaskDB connection.close 未找到可以继续 UNKNOWN，但不能与 writer 停止协议混写。 |
| V-02 | [04 §11](../04-design-rationale.md) 写“POT 涉及 Node 服务” | [pot_manager.py::_find_pot_executable，L114–136](../../../../src/fluentytdl/youtube/pot_manager.py#L114) 寻找 provider EXE；[start_server，L202–212](../../../../src/fluentytdl/youtube/pot_manager.py#L202) argv 为 `exe server --host 127.0.0.1 --port ...`；[fetch_tools.py::fetch_pot_provider，L647–670](../../../../scripts/fetch_tools.py#L647) 获取 bgutil-ytdlp-pot-provider-rs 发布的 Windows EXE | **必须修正。[CONFIRMED] 本项目此处直接启动独立 provider EXE，未通过 Node 启动 POT。** | 改为“独立 POT Provider HTTP 服务”。Node 若在其它 JS runtime 候选中出现，应单列并注明调用点；不能沿用旧上游实现印象绘制当前进程图。 |
| V-03 | 资源调查 §3 用轻量提取的 runfile 包装支撑异常清理，尚未指出保护区起点 | [workers.py::_run_lightweight_extract，L2052–2057](../../../../src/fluentytdl/download/workers.py#L2052) 创建 ExitStack 并 enter runfile；[L2069](../../../../src/fluentytdl/download/workers.py#L2069) 开 staging；[L2137](../../../../src/fluentytdl/download/workers.py#L2137) 准备 env；主 `try` 到 [L2162](../../../../src/fluentytdl/download/workers.py#L2162) 才开始，`_cookie_stack.close()` 在 [L2325–2327](../../../../src/fluentytdl/download/workers.py#L2325) 的 finally | **必须补充边界。[CONFIRMED] runfile 进入早于显式 try/finally 覆盖区。** | 不能概括“轻量提取的所有初始化异常都有明确 finally 清理”。[INFERRED] staging/env 初始化异常会跳过该显式 close，临时文件能否由对象析构及时清理取决于引用/异常保留等，须用受控故障注入证明；不能直接宣称已发生永久凭据残留。后续统一资源表应记录“资源获取到进入清理保护区”的窗口。 |
| V-04 | 运行调查 §2 收尾表写“最后 flush DB writer”，且解释“更新文件替换排在下载和数据库收尾之后” | [download_manager.py::shutdown，L574–591](../../../../src/fluentytdl/download/download_manager.py#L574) worker 超时 terminate 后只 wait(500)，不检查该次等待结果；调用 `flush_and_stop(timeout=3.0)`；[db_writer.py::flush_and_stop，L74–77](../../../../src/fluentytdl/storage/db_writer.py#L74) join 超时后无存活检查、无成功返回 | **必须收窄保证。[CONFIRMED] 这是收尾尝试的调用顺序，不是确认所有 worker 与数据库写入已经完成。** | 收尾表补充“超时可能仍存活/未排空，当前函数不传播 writer drain 成功事实”。已有 RT-02 对 terminate 的风险分析正确，应与此处表格保持一致，避免读者只读启动/退出总览得到更强承诺。 |

## 指定关键主张的源码复核

| ID | Claim | 回查证据 | 裁决与边界 |
|---|---|---|---|
| V-05 | 六个产品入口共享配置窗口；Worker 分三路；三路交付接入 Staging | [MainWindow::_show_config_window，L680–708](../../../../src/fluentytdl/ui/reimagined_main_window.py#L680)、[入口族，L746–818](../../../../src/fluentytdl/ui/reimagined_main_window.py#L746)、[DownloadConfigWindow.start_extraction，L1376–1406](../../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1376)；[Worker.run，L1159–1172](../../../../src/fluentytdl/download/workers.py#L1159) 分流；标准 [L1243](../../../../src/fluentytdl/download/workers.py#L1243) create，轻量 [L2069](../../../../src/fluentytdl/download/workers.py#L2069) 与封面 [L2354](../../../../src/fluentytdl/download/workers.py#L2354) open，快速共同 [L1921–1923](../../../../src/fluentytdl/download/workers.py#L1921) verify/build/commit | **通过静态主张。** “三路都走 Staging 交付”解释为已进入实际产物执行的交付路径；缺 executable 等前置失败可能在 staging 建立前返回，不要扩大成每次调用必定创建事务。快速通道不走标准 Feature/Executor 的描述成立。 |
| V-06 | cancel 没有唤醒 suspend_event；after_fix 存在无超时 wait | [Worker.cancel，L791–815](../../../../src/fluentytdl/download/workers.py#L791) 只置 cancel/pause；[错误挂起，L1475–1485](../../../../src/fluentytdl/download/workers.py#L1475) 创建并 wait；[resume_suspension，L759–763](../../../../src/fluentytdl/download/workers.py#L759) 才 set suspend_event；[fix_registry.retry_now，L206–213](../../../../src/fluentytdl/ui/components/settings/fix_registry.py#L206) 调 retry | **通过静态事实及风险标签。** 当前只是识别“取消不能自然唤醒该等待点”的代码交错，不宣称线上任务已经卡死。测试应覆盖 cancel 在 Event 创建前/后，以及 retry 与 cancel 竞争。 |
| V-07 | shutdown terminate 与 Staging 不可取消提交区存在未协调边界 | [shutdown，L562–591](../../../../src/fluentytdl/download/download_manager.py#L562)、[StagingArea.commit，L1283–1316](../../../../src/fluentytdl/download/staging.py#L1283) | **通过。** 代码没有按 phase 决定强停；风险仍标 INFERRED。不得将正常 Python finally 的 outcome 保证扩大到线程/进程硬终止。进一步收窄收尾文字见 V-04。 |
| V-08 | POT 匿名 Job 不能证明启动端口清理具有实例所有权 | [匿名 Job，L70–98](../../../../src/fluentytdl/youtube/pot_manager.py#L70)、[_cleanup_orphan_servers，L151–161](../../../../src/fluentytdl/youtube/pot_manager.py#L151)、[start_server，L194–198](../../../../src/fluentytdl/youtube/pot_manager.py#L194) | **通过。** 清理向整个配置端口范围 POST shutdown，无 PID/nonce 验证；跨实例影响标 INFERRED 正确。主稿把“借用服务”列待调查对象可以保留，但不得写成当前已经确认支持复用外部服务。 |
| V-09 | runfile 仅隔离托管真相源，自管路径与 None 直通，GC 按前缀和年龄 | [cookie_runfile.py::_is_managed_truth_source，L37–54](../../../../src/fluentytdl/auth/cookie_runfile.py#L37)、[cookie_runfile，L58–88](../../../../src/fluentytdl/auth/cookie_runfile.py#L58)、[sweep，L91–113](../../../../src/fluentytdl/auth/cookie_runfile.py#L91) | **通过，但明确识别异常分支。** `_is_managed_truth_source()` 发生异常也返回 False，使路径直通；不能将“真相源绝不交给 CLI”写成无条件保证。现有资源调查采用条件说明基本准确；建议加此异常降级条件及 V-03。 |
| V-10 | TaskDB 单一连接、锁与 thread-local 事务深度 | [TaskDB._init_db，L67–88](../../../../src/fluentytdl/storage/task_db.py#L67)、[_writing/batch，L186](../../../../src/fluentytdl/storage/task_db.py#L186) | **通过。** 必须与 V-01 同时表达：共享连接并不否定已有后台 writer；主线程同步 insert/delete 与 writer 串行写共享该锁。 |
| V-11 | 主方案区分计划、首轮静态证据、运行验证与未来正式知识库 | [01 执行表和进度](../01-execution-plan.md)、[02 独立复核波次](../02-agent-work-orders.md)、[03 覆盖矩阵](../03-coverage-matrix.md)、[04 写作约定](../04-design-rationale.md) | **范围表述通过。** 明确没有把完整 Phase 0–17 或生产验证写成已完成；20 条纵向切片是计划而非测过的路径。V-01/V-02 修订前局部事实仍不宜交付为无争议现状。 |

## 链接检查与复核限制

[CONFIRMED] 本轮扫描当时所有已有 Markdown 的相对链接，源码目标与起始行号均可解析。README 中 05/06 正文、evidence/04-verification.md、inventory.json、baseline.json 尚未创建；Lead 正在生成，记录为**整合期未完成项**，不是永久坏链。交付前必须重新全量检查；本轮不等待这些文件，也不宣称其正文已审过。

[UNKNOWN] 未独立复核每一种语言/容器/字幕选择组合、全部信号线程亲和性、POT 真实多实例、强停期间文件系统结果、SQLite drain 的运行行为、全部工具更新哈希调用者、所有日志脱敏分支。未运行任何测试，故“通过”均限制于表中指定代码主张。

本轮必须由作者/Lead处理 V-01、V-02、V-03、V-04；V-09 建议追加判定异常直通的条件。修订无需改变业务代码。完成后按修改位置读回，再把修订结果写入总验证记录。

## 修订读回验收（2026-09-19，同轮追加）

| 条目 | 独立读回位置与结果 | 裁决 |
|---|---|---|
| V-01 | [资源调查 §1/§2/§8](03-resource-survey.md) 已增加 TaskDBWriter Queue/daemon Thread/消费/毒丸与 join，保留 TaskDB 连接 close 和 drain 结果未知；[主稿 04 §12](../04-design-rationale.md) 已明确连接模型与独立 writer 同时存在 | **修订验收通过。** 未再把已有 writer 当未来建议。 |
| V-02 | [主稿 04 §11](../04-design-rationale.md) 已改为 Provider EXE HTTP 服务，并分离 yt-dlp JS runtime；[覆盖矩阵进程行](../03-coverage-matrix.md) 已列 POT Provider EXE/JS runtime | **修订验收通过。** 未再把当前 POT 进程误写为 Node 服务。 |
| V-03/V-09 | [资源调查 §3](03-resource-survey.md) 已补托管路径识别异常直通、ExitStack 获取至 try 起点之间的显式保护窗口，保留对象回收/实际残留未经运行验证的限制；流转图也收窄为已进入保护区的 finally 路径 | **修订验收通过。** 没有将静态清理范围缺口夸大成已发生永久泄漏。 |
| V-04 | [运行调查 §2 收尾行](02-runtime-survey.md) 已改成收尾尝试的调用顺序，并列出 wait(500)/join(timeout) 未确认全部退出/排空；主稿 04 §12 同步注明限制 | **修订验收通过。** 不再用代码顺序替代完成确认。 |
| Task/Run 附加澄清 | [主稿 04 §5](../04-design-rationale.md) 与 [VS-09](../03-coverage-matrix.md) 已明确活 worker 暂停/恢复保留 run，结束后重建才新执行；与 [controller.handle_pause_resume_task，L477–524](../../../../src/fluentytdl/core/controller.py#L477)、[TaskTrace，L126–143](../../../../src/fluentytdl/observability/trace.py#L126) 一致 | **通过本次静态读回。** 不将 pause/resume 一律说成创建新 run。 |

### 新增文档工具的边界复核

[CONFIRMED] [tools/snapshot.py：根定位，L14–17](../tools/snapshot.py#L14) 中 `PLAN = __file__.parents[1]` 对应 reconstruction-plan，`ROOT = PLAN.parents[2]` 对应 FluentYTDL 根；本轮用文件路径父目录解析独立核对结果为 `D:\ALL Projects\YouTube\FluentYTDL`，且其 `src/fluentytdl` 存在。ControlCenter 定位为同级目录，未错误落到 YouTube 工作区根。

[CONFIRMED] [capture，L70–87](../tools/snapshot.py#L70) 使用显式目录/根文件列表与扩展名过滤，桌面范围为 src/scripts/tests/workflows/installer/packaging，ControlCenter 为 apps/packages/migrations/scripts/tests；没有纳入 bin、artifacts、运行账户和数据库目录。[describe，L41–67](../tools/snapshot.py#L41) 只读文件字节、计算哈希并 AST 解析 Python；没有 import 产品模块。仅 subprocess 调用为只读 git 身份查询。生成物写入方案 evidence 目录，因此“read-only”应理解为**源代码与产品状态只读**，不是工具零文件写入。

[CONFIRMED] [verify，L92–129](../tools/snapshot.py#L92) 检查 Markdown 链接目标、数字行号范围、代码围栏数量及 inventory 已记录文件的当前哈希，再写 documentation-check.json。它不运行应用、产品测试、构建、Mermaid 渲染或在线请求，属于文档取证工具。

[RECOMMENDATION] 对 verify 结果继续限定为“记录集合内文件的哈希一致”；当前不会重新枚举并检测 capture 之后新增且未收录的源文件，也不证明行号指向正确语义。工具自身已说明部分限制，本次未发现 ROOT/allowlist/verify 会触发产品运行或明显写入越界的问题。本轮仅静态审查该工具，未调用 capture/verify（它们会写入其它作者的 evidence 文件）。

本次修订验收关闭 V-01–V-04 的文档问题，V-09 条件亦已补齐；相关业务风险仍保持待验证状态。本文前面的审稿记录作为历史保留，不代表修订后仍然存在同一文档错误。
