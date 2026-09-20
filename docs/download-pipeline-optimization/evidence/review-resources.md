# 下载流水线优化 · 资源与提交边界独立复核

2026-09-19。复核者：Resource 子代理。审阅对象为其他作者的 [03 执行与控制](../03-execution-and-control.md) 和 [05 上下文与运行时](../05-context-and-runtime.md)，回查相关源码；不把本人编写的 04 算作独立通过。仅文档审查，无业务改动、测试执行、真实进程/下载/凭据访问。

## 复核结论与范围

[CONFIRMED/static] 03 保留唯一 commit 取消门、committed 成功优先、静默 stdout 独立停止、取消异常穿透 VR 和 Feature catch、run/attempt 资源 scope 及单一事件出口；05 保留弱回退，区分选择与实际材料，POT 任务等待取消不停止共享 Provider，不依赖未知 port=0/nonce 上游能力。未看到把原始日志/指纹/单一 PID 当作充分所有权证明的推荐。

[INFERRED] 两章的主要方向可以减少“已取消但仍等待”“已交付却报失败”和“取消一项停掉共享资源”这类交错风险。下列两处契约需明确连接，避免后续实施时各模块局部正确而整体出现时间窗口。它们是设计文档问题，不是本轮运行复现。

## RR-01：RunControl 取消接受必须同步到 Staging 所读谓词

状态：**已关闭（文档修订已独立读回）**。03 E02 已补同步更新永久取消谓词、Staging/登记/等待同源以及 UI 信号只通知，不承担控制状态复制。[修订段](../03-execution-and-control.md#L76)

[CONFIRMED/static] 03 E01 承诺“先接受取消且未越 commit 门则取消”；E02 提议新 cancel_requested，E03 登记进程还会读取取消状态。当前实际 Staging 由 worker 传 `_cancel_event.is_set`，commit 第一行调用该检查。[Worker 绑定](../../../src/fluentytdl/download/workers.py#L1243)、[commit gate](../../../src/fluentytdl/download/staging.py#L1293)

[INFERRED] 如果新 RunControl 接受请求后仅通过 Qt 排队信号再更新旧 Event，就可能在两个表示之间越过 commit 门；用户请求的线性化承诺失效。控制锁不得跨文件提交这一原则仍应保留，不需要用大锁解决。

[RECOMMENDATION] 接受取消的同一同步动作更新/暴露供所有等待点、Popen 登记和 Staging gate 使用的永久谓词；适配期可以同步设置旧 Event，但不能延迟转发。只有阶段展示/确认通知走 Qt 队列。验收加“新控制层已接受、旧适配层尚未消费 UI 消息时到达 gate”交错，确认既不丢取消也不在提交之后撤回。

## RR-02：ExecutionBinding 元数据与运行副本必须来自同一材料代次

状态：**已关闭（文档修订已独立读回）**。05 C01 增加受管发布、receipt 与副本共享每平台同步边界，跨进程使用相同 OS 级互斥协议，且明确前后 revision 相等不足以证明一致；bytes_committed 与 metadata_confirmed 分开，旧材料缺证明仍 unknown。[修订段](../05-context-and-runtime.md#L27)

[CONFIRMED/static] 05 C01 已写明 txt/meta 不一致为 unknown，但 resolve_binding 含 effective_source/material_revision/owned_runfile，需进一步规定这三者如何绑定。现 Sentinel txt replace 后独立写 meta，runfile 从源路径 copyfile。[写入顺序](../../../src/fluentytdl/auth/cookie_sentinel.py#L382)、[运行副本](../../../src/fluentytdl/auth/cookie_runfile.py#L58)

[INFERRED] 若先读旧有效来源/版本，再遇刷新替换 txt，最后复制新的字节，可能得到“标记旧身份、运行新材料”的绑定；反向刷新交错同样不能靠当初读取到有效 meta 保证。对显式账户策略尤其不能默默通过。

[RECOMMENDATION] 同一同步边界读取材料/来源代次并建立不可变副本，或使用有完整发布保证的等价版本协议；仅复制前后版本相等不够，读侧单独加锁但写侧不参与也不能成立。若当前存储无法证明一致则 unknown，显式身份任务停在前置错误，不给新字节补造旧来源。匿名继续拒绝材料；版本和身份标识不公开秘密哈希。

## 已接受的关键设计边界

| 检查项 | 复核结果 |
|---|---|
| 取消、退出与文件提交 | 03 明确 committing 不由 ProcessScope 中断，不直接 rmtree live txn；退出 DB 屏障由 04 A04 负责，不能用 QThread.terminate 使验收“通过”。 |
| runfile 的生命周期 | 05 C02 先前移保护区，再共享 E03 的资源 scope；子进程未确认退出时保留并报告，不假称已释放；自管来源语义不被统一复制改写。 |
| POT 多实例与共享资源 | 05 C03 移除无归属端口广播，未知端口避让，有界启动重试；单任务 cancel 只影响 waiter。Job 关联失败需验，不能以按名 kill 兜底。 |
| 日志与观察 | 05 C04 在旁路 writer 处脱敏，不指望公共 logger 覆盖直接 open/write；测量 best-effort，不新增 transition/outcome producer。 |
| 兼容与回滚 | 03 控制对象不跨重启序列化；05 新 runfile 避开旧 GC 模式，并明确旧版本不会负责新目录清理。危险端口广播/秘密原文日志不作为回退选项。 |
| 测试证据强度 | 两章将源码/mock/Qt/真实 Windows 验收分开，未声称现有测试已跑或实际资源已回收。 |

表内 `[CONFIRMED/static]` 仅表示已在提案文档中确认相应约束；并非产品已经实现。RR-01/RR-02 闭合后，仍须依据各章故障矩阵进入未来实施验收。

## 最终读回

[CONFIRMED/static] RR-01、RR-02 均已由各作者修订并独立读回，没有遗留本文提出的文档阻断项。03 E01 同时补接 A01 的 CommitResult：最后成员/最终 committed 检查点失败但已确认整组交付时保持成功；不由 E01 数文件猜成功，needs_reconciliation 仍保留不确定；A05 不成为第二 outcome 生产者。[跨章修订](../03-execution-and-control.md#L52)

[UNKNOWN] 独立评审通过只说明设计契约在本轮文字层面闭合，不是取消时序、材料一致性、存储耐久或进程资源回收已经运行验证。本人 04 的独立审查由另一代理记录在 [review-commit-protocol](review-commit-protocol.md)，不以自查替代。
