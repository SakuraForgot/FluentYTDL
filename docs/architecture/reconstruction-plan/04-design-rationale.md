# 04 · 必须重点解释的处理缘由

## 1. 写作约定

用户要求重点说明“为什么这样处理”。因此每个机制采用五段式：**现状证据 → 要解决的问题 → 具体处理 → 代价/边界 → 如何验证**。

源码存在某机制，只能证明机制存在；“作者当时为什么选它”需要提交历史、设计记录或明确注释，不能擅自代言。以下“缘由”默认是 [INFERRED] 架构分析，机制确认范围见关联证据；改进内容明确标为 [RECOMMENDATION]。

## 2. 下载引擎与外部工具身份

[CONFIRMED] 解析和下载主路径经 CLI 子进程；工具选择有独立解析逻辑，构建还携带插件、JS 和处理工具。证据：[仓库调查](evidence/01-repository-survey.md)、[资源调查](evidence/03-resource-survey.md)。外置 yt-dlp 插件内部导入 yt_dlp，不等于桌面把 yt-dlp 当 Python API 调用。

[INFERRED] CLI 边界使引擎可以独立更新，也隔离其运行环境；代价是 Python 内部选项必须转换为 argv，结果经 stdout/stderr/退出码重建，错误上下文可能丢失。自定义、managed、PATH 三种来源使“设置页版本”与“真正执行版本”可能脱节。

[RECOMMENDATION] 文档同时记录选项生产者、CLI 转换、最终 exe、版本缓存与组件安装目标。验证从同一个 resolver 回查到 Popen；不能只看 `TOOLS.lock.json` 或 bundled 文件就断定运行版本。

## 3. 六种解析与三种下载通道分开

[CONFIRMED] 解析入口种类不等于下载执行通道数量；Worker 还区分标准下载、轻量提取与封面直链，当前快速通道同样接入 Staging。见 [运行调查](evidence/02-runtime-survey.md)。

[INFERRED] 解析阶段服务于用户选择，下载阶段服务于产物获取；共用终态与交付机制可避免字幕/封面被另一套成功和清理标准处理。代价是大量内部 options 标记承担分流契约，若只读 UI 页面容易漏掉后续合并/默认值覆盖。

[RECOMMENDATION] 逐模式记录 `__fluentytdl_` 键的产生、合并、持久化、消费和移除；绘制六入口到三执行分支的映射，不画六张复制的下载流水线。

## 4. 元数据懒加载与调度槽

[CONFIRMED] 频道/播放列表存在 flat 数据、详情 worker、提取管理和列表调度。见 [运行调查](evidence/02-runtime-survey.md)。

[INFERRED] 先给用户列表，再补当前需要的详情，可以缩短首屏等待并限制并行请求；代价是部分数据、过期回调、取消后的槽位释放都成为正确性问题。取消 UI 不一定等于 worker 已释放。

[RECOMMENDATION] 回环表必须收录 viewport 优先级、队列容量、任务完成/错误/取消三类出口和去重键。重点验证取消后复用提取管理器是否还能获得新槽位，不能只验成功加载。

## 5. Task、Run、Attempt 与 Transaction

[CONFIRMED] [TaskTrace](../../../src/fluentytdl/observability/trace.py) 的 `new_run()` 重置 run 级聚合，`next_attempt()` 只递增 attempt；Staging 使用独立事务标识。详见运行/资源证据。

[CONFIRMED] attempt 在自动重试和用户修复后的重试中递增；活 worker 的 pause/resume 延续同一 run，旧 worker 已结束后重新创建才是另一执行。见 [运行调查的身份与恢复](evidence/02-runtime-survey.md)。

[INFERRED] task 用于用户持久任务，run 用于一次执行结果，attempt 用于同 run 内的尝试，transaction 用于一组文件的交付归属。若合并这些身份，同一 task 的多次重新执行或跨会话恢复会混淆历史执行结果，重试也可能被算成多个独立任务。

[RECOMMENDATION] 不直接套用 Task→Run→Transaction 一对一假设；逐调用点确认创建关系，尤其恢复旧 journal 时。每张状态图只包含一种状态空间，再用对照表说明映射与不一致窗口。

## 6. Staging 的发现、验证与提交

[CONFIRMED] [StagingArea](../../../src/fluentytdl/download/staging.py) 分离 payload、parts、Manifest、verify、build_plan、commit；`committed` 是文件交付成功边界。旧文档的简单“扫描非 part 文件并搬走”不足以代表现状。

[INFERRED] “磁盘存在”不能证明文件属于本次任务，也不能证明它满足用户要求。将报告输出、物理扫描、产物变更与最终交付计划分离，可以减少未知文件误交付/误删、命名冲突和部分提交难以追溯的问题。代价是更多状态、日志和恢复分支。

[RECOMMENDATION] 单独记录 `observed/present/kept/published` 的语义、何时封闭发现集合、Feature 如何变更产物、预留名如何释放、跨卷复制如何补偿。Manifest 是事务产物模型，不能与发布 manifest 混为一个实体。

## 7. 提交期间取消为何不能等同删除

[CONFIRMED] `commit()` 在进入提交前检查取消，进入后有 `pending_cancel` 与失败仲裁；`finalize_failure()` 按 phase 分流。见 [运行调查](evidence/02-runtime-survey.md)。

[INFERRED] 在文件陆续发布过程中外部直接删除 payload，会使交付组变成半完成；用户请求取消与事务可中止是两个不同事实。提交后日志/信号出错也不能抹掉已交付的文件成功事实。

边界：正常取消闸门并不自动证明强制终止线程或进程也安全。[UNKNOWN] 关闭超时、正在提交、文件锁和跨卷写入交错下的实际恢复行为需受控验证。[RECOMMENDATION] 把 GUI 删除、worker cancel、shutdown terminate 与 Staging finalizer 放在同一时序图比较。

## 8. 非零退出与用户实际交付

[CONFIRMED] 下载链有退出后产物检查、质量/大小判断及诊断分流，而不是仅按 exit code 记结果。见运行证据。

[INFERRED] 外部工具可能在媒体已经产生后因辅助清理失败返回非零；同时返回零也不能证明字幕、封面或正确质量已交付。因而“进程结束”“观察到文件”“用户要求满足”“文件已发布”需要分别确认。

[RECOMMENDATION] 明确每个容错分支的最低有效性条件和不可容错错误，记录 expected/actual 与 degraded 的计算范围。禁止把所有非零退出都当成功，也不把“未要求的功能缺失”计为降级。

## 9. Cookie 真相源、请求模式和调用副本

[CONFIRMED] 认证存在平台真相源写入闸门、请求 Cookie 模式快照及 `CookieRunfile` 机制；受管真相源可复制成临时运行文件，自管路径有不同分支。见 [资源调查](evidence/03-resource-survey.md)。因此规则里的“只有两个文件被读取”不能按物理 CLI 路径字面解释。

[INFERRED] 真相源负责可复用认证材料；请求快照固定本次操作身份；runfile 隔离并行 CLI 对材料的读写。验证失败保留旧材料，可以避免一次失败刷新破坏仍可使用的账号。代价是多个文件角色、来源和清理条件需要一致追踪。

[RECOMMENDATION] 平台、账号、cookie enabled、source path、runfile 分列；不记录真实 Cookie 内容。验证匿名路径是否在直接选项、Sentinel、merge、retry、lightweight 等所有入口都执行约束，不能只看到一个开关就宣称匿名隔离完成。

## 10. 缓存代际与设置快照

[CONFIRMED] 当前解析服务与任务选项包含请求上下文及缓存相关处理，详情见 Resource 报告；完整逐分支覆盖仍待展开。

[INFERRED] 若用户切换账号或匿名模式时，旧请求在稍后完成并写缓存，新页面可能收到旧身份结果。仅清空缓存无法阻止“清空后晚到的写入”，代际校验用来识别这种陈旧结果。排队任务保持快照则避免用户排队时的意图被后来设置悄悄改变。

[RECOMMENDATION] 画出请求开始、设置改变、旧请求返回三点交错，确认读键和写入准入都使用正确作用域；区分 SABR 的账号范围与匿名会话范围。

## 11. POT、插件目录和进程所有权

[CONFIRMED] POT 直接启动独立 Provider EXE 的 HTTP 服务，涉及端口探测、插件同步及 Windows Job；当前启动命令不经 Node。yt-dlp 的 JS runtime 是另一条依赖。见 [独立复核 V-02](evidence/05-independent-review.md) 与 Resource 报告。

[INFERRED] frozen 运行时不一定按开发期 PYTHONPATH 发现插件，故插件投放位置必须跟随实际 exe。Job 可帮助清理本实例的进程树，但固定端口并不表达服务所有权。

[UNKNOWN] 多实例、复用服务和启动清理是否会互相干扰，不能从“使用匿名 Job”推出完全隔离。[RECOMMENDATION] 区分“本实例 spawn”“借用健康服务”“按端口探测”“发送 shutdown”四个动作的授权对象，优先补此处并发反例。

## 12. SQLite 批量写与状态权威

[CONFIRMED] TaskDB 使用共享连接、写锁及线程局部批量事务；TaskDBWriter 另有独立 daemon 后台线程，通过 Queue 接收写入请求，批量调用 TaskDB，停止时投递毒丸并 join。两者同时存在，不能把连接模型与线程模型二选一。见 [独立复核 V-01](evidence/05-independent-review.md) 和修订后的 Resource 报告。

[INFERRED] 状态高频刷新直接逐次落库会增加开销；合并写降低频率，但产生 UI 状态与数据库状态暂时不同的窗口。批量提交也让 flush、终态、关闭清空队列的先后关系更重要。

[RECOMMENDATION] 从 `unified_status` 到 DBWriter 到 SQL 的实际执行上下文逐步追踪，核对终态是否绕过节流、异常如何保留或丢弃待写项、关闭前是否 drain。`join(timeout)` 无成功确认不能证明已排空；“有锁”也不是无竞态证明。

## 13. 诊断、信号和最终结果为何分开

[CONFIRMED] observability 提供 flow/task 身份、事件集合、run 级结果与 best-effort 发射；diagnostics 提供主因仲裁。见 Runtime、Resource 报告。

[INFERRED] 同一成功下载可能有格式回退警告；同一失败也可能有多个伴随异常。把每条警告都写 diagnosis 会夸大失败数，把子系统恢复写最终 outcome 又会提前结束执行。日志层自身抛错还可能掩盖原始错误。

[RECOMMENDATION] 对 transition、diagnosis、outcome 逐个搜索生产者；用代码调用边界证明唯一性，不只引用规则。结构化字段使用稳定 code，展示层本地化；原始 stderr/argv、导出包及 UI 日志都需独立查脱敏覆盖。

## 14. 更新服务与二进制来源分离

[CONFIRMED] ControlCenter 对公开客户端提供 KV 快照，管理侧维护 D1/发布；二进制 URL 仍指向 GitHub Release。见 Repository 报告。

[INFERRED] 元数据缓存减少每台客户端直接查询上游 API 的需求，公开读取与管理写入分离减少公开路径权限和数据库负载。代价是 D1、KV、客户端缓存间可能短暂不一致，ETag/过期/发布失败需要明确语义。

[RECOMMENDATION] 分别画控制元数据与文件字节的流向；验证发布失败是否可重试、公开读取是否会意外回源、客户端 fallback 到底改变元数据来源还是下载来源。源码中出现域名不能证明线上部署状态。

## 15. 应用更新、组件更新、安装与卸载

[CONFIRMED] 应用与组件有不同更新入口，构建存在 Full/app-core/setup 等目标；数据根依部署模式而变化，认证目录有自己的路径契约。见 Repository、Resource 报告。

[INFERRED] 应用更新要替换程序但保留用户状态；组件更新应保持用户自定义工具的所有权；卸载处理隐私数据，同时不能误删用户媒体或未知文件。这些需求使三类流程无法共享一个“递归清空再覆盖”的动作。

[RECOMMENDATION] 做 installed/portable/override × 标准用户/提升权限 × 升级/回滚/卸载矩阵。记录 updater.exe.new、备份、ready token、降权启动、copy-only migration 的时序；分别说明何时可删除旧内容，避免用一个“更新成功”掩盖启动尚未确认。

## 16. 为什么保留旧文档而不直接重写

[CONFIRMED] 当前仓库已有内容丰富的中英文架构和规则，但发现沙箱模型、Cookie 路径与入口声明等需重新核对。见三份调查。

[INFERRED] 它们包含历史知识，直接覆盖会丢失有用的缘由；继续把冲突段落当现状又会误导后续开发。将冲突逐条登记，并在正式逆向验收后维护入口，可以同时保留来源和明确当前事实。

[RECOMMENDATION] 维护 `旧段落 → 当前证据 → 差异 → 是否需要改文档/规则 → 后续负责人` 表。当前任务不运行规则/翻译同步器，因为这会修改方案以外的文件且不能替代事实复核。
