# 新版架构 V2 · 13 已确认差异、潜在风险与未知项

> 本章记录当前源码边界，不是业务修复记录。没有在本轮启动产品、执行破坏性故障注入或访问真实凭据。

## 1. 如何理解分类

**已确认差异/机制缺口**只描述可直接看到的代码或清单不一致；**潜在风险**描述在指定条件下可能出现的后果，未必已在真实用户环境发生；**建议**是后续行动，不属于当前架构。

优先级按“可能触及用户文件/凭据/更新恢复”以及“是否影响任务终结”排序，不是未经复现的漏洞评分。每项关联原因、状态/资源影响、最小补证方法与当前处理边界。

## 2. 文件、凭据与程序维护边界

| ID / 优先级 | 已确认代码事实 | 潜在后果与未确认部分 | 后续验证 / 处理缘由 |
| --- | --- | --- | --- |
| R01 / 高 | [shutdown](../../src/fluentytdl/download/download_manager.py#L562) 超时后 terminate worker；[commit](../../src/fluentytdl/download/staging.py#L1283) 有不可取消区，但 shutdown 未按 phase 分流 | [INFERRED] 强停可能绕过正常 finalizer/补偿和 outcome；实际文件组及锁是否残留未测 | 用临时目录在 reserve/publish/committed 三点注入关闭；之所以不能简单“关线程”是文件提交已经有用户目录副作用 |
| R02 / 高 | [WebView2 `_log`](../../src/fluentytdl/auth/providers/webview2_provider.py#L109) 直接追加 profile 日志；[代理注入](../../src/fluentytdl/auth/providers/webview2_provider.py#L155) 写 proxy_full | [INFERRED] 若输入含 userinfo，可能落入此旁路文件，且不在公共 JSONL 脱敏/保留链；未读真实日志证明泄露 | 仅用合成代理凭据验证；应逐日志写者建隐私边界，公共 patch 不能保护自定义 open/write |
| R03 / 高 | [POT cleanup](../../src/fluentytdl/youtube/pot_manager.py#L151) 向固定端口范围 POST shutdown，无 owner/nonce 校验；随后再选端口 | [INFERRED] 另一活实例服务可能受影响，探测与 bind 存在时间窗口；多实例未实测 | 受控双实例/假本地服务验证目标所有权；匿名 Job 只约束进程树，不约束 HTTP 命令目标 |
| R04 / 高 | [updater 无启动 PID 分支](../../src/fluentytdl/core/updater.py#L1663) 仍 `_commit_update` 清备份、提示手动启动、返回 1 | [CONFIRMED] “全部启动失败均回滚”“只有 READY 才提交”均不是现状；[UNKNOWN] 此分支的用户恢复可用性 | 分开 no-PID、ready、survival、watchdog-failure 四种场景；不要用单一 success/fail 字眼替代磁盘、启动和回滚材料状态 |
| R05 / 高 | [updater 等旧进程](../../src/fluentytdl/core/updater.py#L1488) 超时警告后继续替换 | [INFERRED] 可能遇到仍持有的文件锁或活进程竞争；不等于已确认每次都会覆盖失败 | 隔离受控持锁进程；记录权限、旧进程与替换结果，不能只验下载哈希 |
| R06 / 高 | [组件 worker 缺 psutil 分支](../../src/fluentytdl/core/updater_worker.py#L52) 按 executable name 调 taskkill /IM | [INFERRED] 降级分支可能停止其他同名进程；与 ProcessManager 自身 ppid 过滤不同 | 核对冻结包 psutil 可用性及降级分支的作用范围；不能概括所有工具停止都按 owner |
| R07 / 中 | [轻量提取](../../src/fluentytdl/download/workers.py#L2052) 先 enter runfile，显式 try 起于 [2162](../../src/fluentytdl/download/workers.py#L2162) | [INFERRED] staging/env 初始化异常会绕过该显式 close；对象回收和残留时长未测 | 合成 Cookie+临时根故障注入，区别显式释放、GC/析构和下次启动年龄清理 |
| R08 / 中 | [runfile sweep](../../src/fluentytdl/auth/cookie_runfile.py#L91) 按前缀与年龄；[识别受管源](../../src/fluentytdl/auth/cookie_runfile.py#L37) 异常返回 False | [INFERRED] 较老活跃副本可能被清理；识别失败走自管直通，隔离不是无条件保证 | 长任务与重叠实例测试；不将“足够旧”当作已证明无 owner |
| R09 / 中 | [公告默认路径](../../src/fluentytdl/notification/announcement_service.py#L155) 为 data root/announcements.sqlite3；[卸载名单](../../installer/maintenance.ps1#L205) 未列它 | [CONFIRMED] 默认文件位置与所列清理项不匹配；[UNKNOWN] 完整安装卸载后的实际残留 | 受控安装验收确认公告 DB 及 WAL/SHM；只声明清理已列路径，不能保证全部用户数据已清 |
| R10 / 中 | [账户切换](../../src/fluentytdl/auth/auth_service.py#L1307) 保存选择后忽略同步 bool 并返回 True；[同步](../../src/fluentytdl/auth/auth_service.py#L1324) 可因材料缺失/校验失败保留旧源 | [INFERRED] UI 当前账户与实际使用的真相源身份可能不同；未运行切换失败场景 | 分开 selected-account 与 committed-truth-source；失败保留旧材料有恢复价值，但结果应能解释身份差异 |

## 3. 终结、持久化与未接通能力

| ID / 优先级 | 代码事实与范围 | 后果 / 缺证 | 最小验证 |
| --- | --- | --- | --- |
| R11 / 高 | [cancel](../../src/fluentytdl/download/workers.py#L791) 唤醒 pause Event，不唤醒 [suspend_event.wait](../../src/fluentytdl/download/workers.py#L1485) | [INFERRED] after_fix 挂起时取消可能无法自然退出或释放槽；关闭随后可能强停 | 取消在 Event 创建前/后、修复与取消竞争、删除/退出三入口 |
| R12 / 中 | [AsyncExtractManager.cancel](../../src/fluentytdl/download/extract_manager.py#L139) 等待结果回调清理；[EntryDetailWorker](../../src/fluentytdl/download/workers.py#L579) 取消直接 return | [INFERRED] 复用 manager 时 _active_tasks 容量可能不释放；实际列表交互未复现 | 取消后继续添加足够条目，观察 active/pending/final signals |
| R13 / 中 | [DBWriter.flush_and_stop](../../src/fluentytdl/storage/db_writer.py#L74) join(timeout) 后无存活确认 | [UNKNOWN] 超时后到底已提交多少，主界面退出不证明 drain 完成 | 制造慢提交验证队列、线程、DB 的独立状态 |
| R14 / 中 | [DBWriter._process](../../src/fluentytdl/storage/db_writer.py#L166) 吞单项异常；[_process_batch](../../src/fluentytdl/storage/db_writer.py#L125) 的回滚降级依赖异常到外层 | [INFERRED] 单项异常未必触发注释描述的整批 rollback/retry；可能部分项丢失而其余提交 | 在批中间 SQL 注入受控失败，核对实际 commit/rollback 与队列消费，不仅看 warning |
| R15 / 中 | [ConfigManager.save](../../src/fluentytdl/core/config_manager.py#L461) 直接写且捕获异常；set 随后通知 | [INFERRED] 内存/UI 已改变而磁盘未保存；没有此函数层的临时文件原子替换 | 不可写临时目录/截断故障，比较配置内存、文件和用户反馈 |
| R16 / 中 | [transition 首次 run](../../src/fluentytdl/download/download_manager.py#L48) 清基线；[restore_state](../../src/fluentytdl/download/workers.py#L754) 写恢复基线 | [INFERRED] 首次状态事件可能重新解释历史状态；不是已证明任务状态本身损坏 | 恢复 queued/error/paused 后首事件，检查 transition 统计与数据库实际状态 |
| R17 / 中 | [QuickAddWorker](../../src/fluentytdl/core/quick_add_worker.py#L129) single_worker 路径 noplaylist=False；[DownloadWorker](../../src/fluentytdl/download/workers.py#L1186) 统一覆盖 True | [CONFIRMED] 同选项存在相反赋值；[UNKNOWN] 真实 playlist URL 在该分支的最终行为 | 记录合并后实际 argv，并使用受控列表输入验证；避免根据上游选项宣称一路保持 |
| R18 / 中 | [VRFeature._run_ffmpeg](../../src/fluentytdl/download/features.py#L624) 局部 Popen，无同函数 cancel/pause 检查 | [UNKNOWN] 标准 cancel 是否经其他路径完整覆盖该 FFmpeg；不能宣称所有后处理可立即取消 | 受控长转码中取消，观察进程退出与 Staging 所有权 |
| R19 / 中 | 在 main/src 搜索未找到 post_verify/on_quality_warning/on_quality_ok/enqueue_quality 的生产调用；有预检调用 | [CONFIRMED] 在受查范围没有已连接的完整后验质量链；动态外部调用未知 | 文档先区分意图与实际接线；后续以实际业务调用与断言证明而非只测孤立函数 |
| R20 / 低 | [console entry](../../pyproject.toml#L64) 指向缺失的 fluentytdl.main，frozen spec 使用根 main | [CONFIRMED] 声明和布局不一致；不能推导冻结 EXE 同样不可启动 | 先确定 console launcher 是否支持契约，再在隔离安装中验证 |

## 4. 云端与架构耦合

| ID | 事实 | 风险与处理边界 |
| --- | --- | --- |
| R21 | [updates](../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74) 与 [announcements](../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L8) 先写 KV 后写状态；两步可部分成功 | [INFERRED] 报错时公开快照可能已经变新；重试/用户解释必须区分内容保存、公开分发、状态记录，不假设跨存储原子性 |
| R22 | [operation_lock](../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22) 240 秒租约、按 token 释放，未见续租/写入 fencing | [INFERRED] 若 action 超期且另一请求到达，可能重叠写；[UNKNOWN] 线上是否出现，不能由时间常量推断必然超时 |
| R23 | [导入候选图](evidence/dependency-candidates.json) 有条件/局部/TYPE_CHECKING 在内的 SCC | [CONFIRMED] 静态双向关系；[UNKNOWN] 不据此认定启动循环导入异常。见08逐候选人工判断，延迟 import 往往是当前处理方式，代价是隐藏初始化约束 |
| R24 | 主窗口直接调用 controller，utils 部分模块依赖 config/observability，与理想规则文字不完全一致 | [CONFIRMED] 规则不能当现状图。文档需区分命令调用、事件结果和启动期局部导入；是否修正规则或实现是后续设计决定 |

## 5. 交叉复核补充：恢复材料和终结竞争

| ID / 优先级 | 已确认代码事实 | 条件性后果与最小验证 |
| --- | --- | --- |
| R25 / 高 | [journal 写入](../../src/fluentytdl/download/staging.py#L1630) 捕获 OSError 并 signal；[commit](../../src/fluentytdl/download/staging.py#L1293) 调用后继续 reserve/publish | [INFERRED] 日志未成功保存时仍可能改变用户目录，崩溃恢复材料可能落后实际文件；源码的 write-ahead 顺序不保证 durable WAL。用临时根注入 journal 写错，核对占位符、成品与重启恢复，不能只测试正常提交 |
| R26 / 高 | [GC 分派](../../src/fluentytdl/download/staging.py#L1807) 对可识别 committing/rollback_failed 保留；坏/不可读 journal 和未知 phase 满足年龄/live 门后走[删除](../../src/fluentytdl/download/staging.py#L1845) | [INFERRED] 未知现场未获得同样的保守保护；与 R25 组合时可能失去恢复材料。未复现数据损失；用损坏、旧版、未知 phase 合成 journal 验证，不宣称所有异常事务都会保留 |
| R27 / 中 | 快速通道异常先发 error/diagnosis，再由 [_fastpath_fail](../../src/fluentytdl/download/workers.py#L1965) 仲裁 committed；调用见[轻量](../../src/fluentytdl/download/workers.py#L2310)、[封面](../../src/fluentytdl/download/workers.py#L2503) | [INFERRED] 已交付文件和最终 success 可能伴随失败诊断或短暂 error 展示；用 postcommit 合成异常同时核对日志与 UI，不能只看最终 outcome |
| R28 / 中 | 快速通道取消检查在逐行循环，EOF 后按 rc 分流；[轻量](../../src/fluentytdl/download/workers.py#L2251)、[封面](../../src/fluentytdl/download/workers.py#L2464)没有统一的取消优先仲裁 | [INFERRED] 静默子进程被取消后可能分类为 failed；用无后续 stdout 的受控子进程验证，不据结构宣称所有取消都分类错误 |
| R29 / 中 | 标准 Executor 返回后，[is_cancelled 门](../../src/fluentytdl/download/workers.py#L1528)可跳过后处理；没有 outcome 时 [finalizer](../../src/fluentytdl/download/workers.py#L1122)记 run_outcome_unset 并归 failed | [INFERRED] 取消在该窗口到达可能产生未设终态的失败记录；实际时序未测。验证取消与 attempt 返回交错，同时检查沙盒、UI、DB 与 trace |

这些条目来自独立反向复核，详见 [运行与资源复核](evidence/review-runtime-resources.md)、[部署与模块复核](evidence/review-deployment-modules.md)。它们补充源码参考的保证上限，不代表本轮修复或运行验收。

## 6. 剩余未知与修复边界

全部日志自由文本脱敏覆盖、真实 Windows 强停时文件一致性、文件系统断电耐久性、Qt 的所有动态连接线程归属、全语言/容器组合、真实网站/POT 可用性、线上部署与源代码一致性仍未实测。

[RECOMMENDATION] 后续修复按最小有证据场景排序，先写“触发→预期→资源/状态→故障验证”，再改业务。不要因为本章指出问题就擅自删除候选代码、重构线程、改认证策略或部署服务。本轮到记录与可追溯为止。
