# FluentYTDL 系统架构与运行机制说明书：项目适配方案

> **执行更新（2026-09-19）：** 用户已要求开始新版逆向，并明确区分过时的旧书。正式成果改存 [docs/architecture-v2](../../architecture-v2/README.md)，总入口为 `NEW_ARCHITECTURE_REFERENCE.md`。本目录保留为方案与首轮证据，原调查中的行号对应当时快照。

> 2026-09-19 · 交付性质：执行方案与首轮静态调查。不是最终架构说明书，也不是产品重构方案。

## 从哪里开始

本方案把用户提供的《项目完整架构说明书 Agent 总提示词》落到 FluentYTDL 当前工作树：Python / Qt 桌面程序、外部下载与处理进程，以及同级 ControlCenter 更新和公告服务。每个调查项都要求说明**实际如何处理、为什么需要该处理、付出什么代价、失效时会怎样、凭什么确认**。

| 文档 | 解决的问题 |
| --- | --- |
| [01 执行方案](01-execution-plan.md) | 范围、架构层次、Phase 0–17 的输入输出与准出条件 |
| [02 Agent 工单](02-agent-work-orders.md) | 谁调查、谁复核、谁可写什么、如何交接与解决冲突 |
| [03 覆盖矩阵](03-coverage-matrix.md) | 主要模块、20 条纵向切片、状态/资源/控制回环如何覆盖 |
| [04 处理缘由](04-design-rationale.md) | FluentYTDL 必须重点解释的处理机制、约束、代价与待证问题 |
| [05 证据与模板](05-evidence-and-templates.md) | 证据分级、统一实体、状态/资源/失败记录、图和索引标准 |
| [06 验证与验收](06-verification-and-acceptance.md) | 反向验证、文档漂移、风险分类、后续运行验证和完成标准 |
| [仓库与部署调查](evidence/01-repository-survey.md) | 第一轮实际入口、模块、构建及 ControlCenter 边界 |
| [运行与状态调查](evidence/02-runtime-survey.md) | 第一轮调用链、任务、事务与并发 |
| [资源与安全调查](evidence/03-resource-survey.md) | 第一轮路径、数据库、认证、进程和清理 |
| [复核记录](evidence/04-verification.md) | 本次方案交付前发现的问题、修订与验证限制 |
| [机器清单](evidence/inventory.json) | 选定源码/构建配置的文件哈希、Python 符号与导入清单 |

## 基线与边界

- [CONFIRMED] 客户端根目录为 `D:/ALL Projects/YouTube/FluentYTDL`；调查开始时分支为 `release/3.7.3`，HEAD 为 `c441f2cde2e7973fe5902da50358f25c37c6525f`。结论对应**当时工作树**，不能只用该提交重现。
- [CONFIRMED] 调查前已有 locales 六个文件、`diagnostics/catalog.py`、`diagnostics/engine.py`、`youtube/youtube_service.py` 修改，以及未跟踪的 `specs/`、`tests/test_playlist_authcheck.py`。原始状态见 [baseline](evidence/baseline.json)。本次不回滚、不暂存、不提交这些改动。
- [CONFIRMED] 同级 `FluentYTDL-ControlCenter` 有客户端实际调用的服务源码，应作为关联子系统调查。是否线上部署了这些源码为 [UNKNOWN]；本次不访问线上、不读取凭据值。
- `REFERENCE-youtube-dl-gui`、两份 wiki、本地归档、用户日志与历史反馈不作为当前产品实现证据；有需要时另行按明确问题引用。第三方内嵌代码列入库存，但不当作自主模块设计。
- 本次只新增本专属目录内的方案、证据和只读分析工具；不修改业务逻辑、依赖、版本、翻译、构建及云端配置。

## 与现有架构文档的关系

保留 [中文架构](../../ARCHITECTURE_CN.md)、[英文架构](../../ARCHITECTURE_EN.md)、[构建发布说明](../../build_release.md) 和 [Cookie 模式说明](../../youtube_cookie_mode.md)。它们是调查线索，发生冲突以当前代码证据为准。

[CONFIRMED] 旧中文架构第 4 节仍描述 `task_{id}` 沙箱及以 DB 主键命名；当前 [StagingArea.create](../../../src/fluentytdl/download/staging.py) 使用事务标识。新说明书必须显式记录这个差异，不能复制旧图作为现状。精确行号见专项证据。

[RECOMMENDATION] 按用户后续命名要求，完成逆向和独立复核后，再生成 `docs/architecture-v2/NEW_ARCHITECTURE_REFERENCE.md`。旧中英文入口已增加历史参考提示，正文保留；不创建与旧书同名的最终总书。

## 阅读证据的方法

`[CONFIRMED]` 表示有直接源码或本次工具输出依据，**不代表实机运行通过**。`[INFERRED]` 表示推断；`[UNKNOWN]` 表示尚未确认；`[RECOMMENDATION]` 表示建议。所有缘由都区分“机制证据”“注释陈述的意图”和“分析者推断的动机”。

本次完成标准是：方案覆盖、职责落地、首轮调查、独立质疑与文档检查。完整架构逆向完成标准见 [06](06-verification-and-acceptance.md)，两者不能互相替代。
