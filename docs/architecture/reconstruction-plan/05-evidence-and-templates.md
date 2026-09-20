# 05 · 证据协议、实体模板与图规范

## 1. 证据标签与验证层次

| 标签 | 含义 | 允许的措辞 |
| --- | --- | --- |
| `[CONFIRMED]` | 本次工作树有直接源码/工具输出 | “该分支调用了…”，附路径、符号、行号和条件 |
| `[INFERRED]` | 多处证据支持但没有直接证明 | “由 A/B 推断可能…”，列出前提与反例 |
| `[UNKNOWN]` | 证据不足或冲突尚未消除 | 指出未查分支、所需验证、影响范围 |
| `[RECOMMENDATION]` | 工作建议/改进设想 | 与现状分开，不在当前状态图中画成已实现 |

结论标签之外另记验证层次：`source`、`static-tool`、`unit-test`、`isolated-runtime`、`packaged-runtime`、`online`。源码能证明调用存在，不能证明网络请求可达或二进制运行正常；测试文件存在不等于测试执行通过。

行号是阅读便利，文件哈希和符号是追溯基线。代码变化后重新定位行号，不机械沿用旧引用。一个 [CONFIRMED] 不能跨越没有证据的调用边界。

## 2. 基线与机器辅助

本目录的 [snapshot.py](tools/snapshot.py) 仅使用 Python 标准库读取指定源码/配置/测试区域；不导入项目、不访问网络、不读取用户运行数据。`capture` 输出文件清单、哈希、顶层与类内定义、import 候选；`verify` 检查文档本地链接/行号范围和基线漂移。

```powershell
# 在 FluentYTDL 根目录执行；只写本方案 evidence
.venv/Scripts/python.exe docs/architecture/reconstruction-plan/tools/snapshot.py capture
.venv/Scripts/python.exe docs/architecture/reconstruction-plan/tools/snapshot.py verify
```

若 `.venv` 不存在，使用可用 Python 3.12+ 的绝对路径。不要为了文档检查自动安装依赖。`capture` 会更新证据快照，因此正式复核之后不应为消除漂移警告随意覆盖；先说明变化，再取新基线。

清单是代码候选索引，**不是完整动态调用图**。它不解析 TS/Vue 类型，不模拟反射/Monkey Patch，不判断 Qt 线程归属，也不证明循环导入实际发生。嵌套函数/相对导入和插件执行上下文需人工核查。哈希校验只比较已收录集合，不发现后来新增且未收录的文件。没有扫描用户资料、构建缓存和二进制内容，应在报告中保留此范围说明。

## 3. 实体 ID 与术语

| 前缀 | 用途 | 示例 |
| --- | --- | --- |
| SYS / APP | 系统、应用/维护程序 | SYS-FYTDL、APP-DESKTOP、APP-UPDATER |
| MOD / CMP | 逻辑模块、组件 | MOD-AUTH、CMP-COOKIE-SENTINEL |
| FLOW / VS | 运行路径、用户纵切片 | FLOW-COMMIT、VS-11 |
| STATE / RES / LOOP | 状态空间、资源、控制回环 | STATE-STAGING、RES-COOKIE-RUNFILE |
| E / RISK / U | 证据、风险、未知项 | E-COOKIE-01、RISK-SHUTDOWN-01 |

保留代码真实名称和原始状态字符串；中文负责解释，不额外发明一套可互换的英文枚举。`TaskTrace` 的 task、Qt worker、SQLite task row、transaction 都应有独立实体。最终 ID 由 Lead 分配，图和索引使用同一份词表。

## 4. 架构实体模板

```text
ID / Name / Type / Parent:
Responsibility / Non-responsibility:
Files / Symbols / Source hash / Lines:
Entry / Public interface:
Called by / Calls / Connect-registration:
Reads / Writes / State owners:
Creates / Owns / Borrows / Releases:
Dependencies (build/runtime/control/data/resource/external/implicit):
Thread or process context / Runtime lifetime:
Success / Failure / Cancel / Restart recovery:
Evidence label / Verification level:
Related chapters / Diagrams / Vertical slices:
Mechanism / Rationale / Tradeoff / Unknown:
```

不要只写“负责下载管理”。必须说明谁能调用、影响哪份状态、由什么结束持有的资源；否则模块边界无法支持故障定位。

## 5. 调用链和数据模板

```text
Flow ID / User trigger / Input:
Entry → caller → callee → lowest relevant executor:
For every edge: direct call / Qt signal / queued callback / process pipe / HTTP
Registration site / Receiver lifetime / Thread affinity:
Option snapshot / Transformation / Validation:
State reads+writes / Created+released resources:
Success terminal / Error terminal / Cancellation terminal:
Retry owner / Exit condition / Restored state:
Evidence per edge / Unknown edges:
```

数据谱系至少包含 `字段/对象 → 生产者 → 校验 → 变换 → 消费者 → 持久化 → 销毁 → 脱敏边界`。对 `opts` 不只写 dict；单独列内部键、默认值、覆盖顺序、是否进入数据库以及恢复后谁重新解释。

## 6. 状态转换模板

| 状态空间 | 源状态 | 触发/前置条件 | 唯一或多个写者 | 目标状态 | 持久化时机 | 副作用/资源变化 | 非法/重复触发 | 证据 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 原始代码枚举/字段 | 原始值 | 用户/信号/错误/恢复 | 符号+线程 | 原始值 | 入队/flush/commit 要分开 | spawn/kill/publish | 拒绝/忽略/未约束 | 标签+源码 |

不存在集中转换校验表时，应写“分散写入的状态模型”，不宣称严格 FSM。以文件存在性推导的状态要另标 derived，跨启动保存的状态要标 persistence。

## 7. 资源与控制回环模板

资源字段：`名称/类型 → 创建者 → 所有者 → 借用者 → 读写者 → 寿命 → 重启后是否保留 → 正常释放 → 失败/取消释放 → 关闭超时 → 恢复者 → 关联状态 → 证据`。

回环字段：`入口 → 触发 → 循环体 → 重复条件 → 次数/退避/时间界限 → 共享状态 → 持锁范围 → 每轮创建的资源 → 错误策略 → 取消观察点 → 正常/异常退出 → 证据`。

资源树中的“拥有”表示有责任释放，不表示仅持有一个引用。复用外部 POT 服务和自己启动的进程必须分开；Windows Job 与按端口发请求也不能画成同一条所有权边。

## 8. 错误/缘由/风险模板

失败记录：`Source → 原始错误 → 转换 → 主因/伴随信号 → retry/fallback 决策 → 状态/资源影响 → cleanup/rollback → UI 结果 → 日志 → 最终终态`。

缘由记录：

1. 触发约束：来自 Windows 文件锁、CLI 协议、并发、用户语义还是构建限制。
2. 当前机制：代码实际执行什么，适用哪条分支。
3. 原因来源：实现注释/测试断言/历史记录，或明确标记的推断。
4. 取舍：新增状态、延迟、磁盘副本、维护成本、不能覆盖的故障。
5. 反事实：若删掉该处理，什么具体链条会失效；没有证据则作为待测假设。
6. 最小验证：哪一组隔离输入足以区分“机制有效”和“仅有代码”。

风险记录：`ID / 已证问题或潜在风险 / 触发条件 / 直接证据 / 可见后果 / 未确认部分 / 严重度依据 / 建议验证 / 改进建议 / 是否得到修复授权`。本次最后一项为未实施业务修复。

## 9. 图和双向索引

每张图注明 Purpose、Scope、Node semantics、Edge semantics、证据入口及遗漏范围。系统图只画子系统；运行图用 sequenceDiagram；状态图只画一个状态空间；资源树画 owner/borrow/release；SQLite/D1 的 schema 用 erDiagram。

统一箭头：`-->` 直接调用/控制、`-.->` 事件/消息、`==>` 关键运行路径；每条重要边加文字，避免依赖渲染器线型。图中未证节点明确标 UNKNOWN；不把建议画入当前架构。

正向索引：`概念 → 章节 → 实体 → 文件/符号 → 调用者/被调用者 → 状态/资源 → 切片`。

反向索引：`符号 → 所属实体 → 运行流 → 读写状态 → 持有资源 → 失败出口 → 文档/图`。

没有需要解释的关系时不画图；同一细节不复制到多章，使用稳定实体 ID 和链接关联，以降低漂移。
