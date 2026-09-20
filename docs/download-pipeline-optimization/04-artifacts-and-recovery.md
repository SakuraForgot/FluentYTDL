# 下载流水线优化：04 产物交付、持久化与恢复

2026-09-19。基于 [架构 V2 状态机](../architecture-v2/06-state-machines.md)、[资源模型](../architecture-v2/07-resource-model.md)、[取消与恢复切片](../architecture-v2/vertical-slices/VS-11-cancel-recovery.md)，重新读取当前源码与相关测试。本文只提出设计，不修改业务，不运行测试、应用、故障注入或真实下载，不读取用户任务库、凭据及产物。开始前已读取 [AGENTS.md](../../AGENTS.md) 和工作区 dirty 清单；现有业务、测试、翻译、README 及架构文档改动均不归本次分析处置。

标记：`[CONFIRMED/static]` 是源码/测试文本事实，`[INFERRED]` 是影响推断，`[UNKNOWN]` 是尚未验证的运行性质，`[RECOMMENDATION]` 是提案。以下接口、返回类型、状态字段与 UI 文案均为草案，不能当作已实现能力。

需求映射：[G01–G09](00-goals-and-boundaries.md)中，A01/A02/A03覆盖 G03/G09，A04覆盖 G02/G04/G07，A05覆盖 G03/G04/G09，A06覆盖 G01/G08/G09。Windows/SQLite 基础机制参见[官方研究](evidence/research.md)，不把同步 API 或 SQLite WAL 等同于多文件事务。

## 1. 先保留已有价值，再闭合故障边界

[CONFIRMED/static] 目前不是直接把下载文件散落在最终目录：每个 run 使用独立 UUID 沙盒，Manifest 记录产物事实/意图，Feature 经 Staging 原子操作修改文件，verify 后生成 CommitPlan，commit 负责整组预留、发布与补偿。唯一取消门在预留之前；已 committed 后清理/观测失败不能推翻成功。这个约束同时适用于标准媒体和快速字幕/封面路径。[StagingArea.create](../../src/fluentytdl/download/staging.py#L739)、[Manifest](../../src/fluentytdl/download/staging.py#L322)、[commit](../../src/fluentytdl/download/staging.py#L1283)、[标准路径](../../src/fluentytdl/download/workers.py#L1593)、[快速路径](../../src/fluentytdl/download/workers.py#L1918)

[INFERRED] 优化重点应是“能否证明完成、证明没有越权删除、证明退出前持久化到哪里”，而非用一个更大的状态 enum 替换现有模块。文件系统、SQLite、Qt 队列、JSONL 之间没有共同事务；将它们描述成一次原子提交，会掩盖故障窗口。

| 提案 | 推荐取舍 | 依赖及边界 |
|---|---|---|
| A01 分阶段 journal 确认 | 发布前必要记录失败则停止进入下一危险步骤；发布后按交付事实裁决 | 先于更激进自动恢复；不把所有 journal 异常交给同一 rollback |
| A02 未知现场分类和隔离 | 未知/坏 journal 默认保护，提供可解释处置；新格式避开旧 GC 扫描域 | 与 A01 同批设计迁移，不能仅递增 version |
| A03 冲突、占位和补偿所有权 | 保留整组 O_EXCL，补逐项归属登记与失败分类 | 不引入覆盖已有文件策略；跨卷校验成本单独度量 |
| A04 可确认的 DBWriter 与退出 | 高频进度仍合并；终态有结果回执，关闭有生产者屏障和显式结果 | 不在 Qt 主线程同步等 SQLite；不承诺超时等于成功 |
| A05 交付凭据及终态对账 | 文件交付成功与记录同步状态分开；延迟删除必要恢复证据 | 依赖 A01/A02/A04，不能让数据库失败撤回成品 |
| A06 明示恢复层级 | 先完善暂停恢复/重新下载与交付对账，延后跨启动字节复用 | 复用旧 .part 必须另有版本化候选契约和引擎验证 |

表内为 `[RECOMMENDATION]`。实施顺序宜先补故障观测与分类契约，再升级写入/退出，再接终态对账；跨启动字节续传最后单独立项。

## 2. A01：把 journal 从统一 best-effort 改为分阶段确认

### 源码事实与现有缘由

[CONFIRMED/static] journal v1 包含 task_key、staging_id、run_id、attempt、phase、suffix、pending_cancel、placeholders 和 items；写入是 JSON 临时文件关闭后 os.replace，没有显式 fsync。`_write_journal` 捕获 OSError，只发 journal_write_failed；其注释明确希望不要因记录失败改变事务走向。[version](../../src/fluentytdl/download/staging.py#L156)、[snapshot](../../src/fluentytdl/download/staging.py#L1613)、[writer](../../src/fluentytdl/download/staging.py#L1630)

[CONFIRMED/static] 初始 committing 写入位于 commit 的补偿 try 之前；reserve/publish 在 try 中；全部发布后的 committed 写入在该 try 之后。成员先记 publishing，再 os.replace/copy，随后记 published 和目标指纹。当前写前记录是调用顺序，并不是成功落盘的准入条件。[commit](../../src/fluentytdl/download/staging.py#L1293)、[_publish_group](../../src/fluentytdl/download/staging.py#L1379)

[INFERRED] 现策略使下载不因诊断记录不可写而立即中断，但风险是最终文件已经变化、磁盘 journal 仍是 prepared/旧 items；重启读旧状态可能错误清理沙盒。反过来，简单删除 catch 改成统一抛错，也可能让“物理发布已完成，回执尚未写好”误入撤回或失败分类。必须把产物事务记录与始终 best-effort 的 observability 区分，后者仍不得抛入业务。

### 可选方案与推荐

| 方案 | 收益 | 代价/不选择原因 |
|---|---|---|
| 保持现状，仅增加告警可见性 | 改动少，不增加磁盘同步时延 | 无法证明进入危险阶段前 journal 可读；只能继续承认崩溃现场可能缺失 |
| 所有 journal 失败立即抛错并统一补偿 | 代码表面简单 | 拒绝：改变 try 边界语义，发布后未必可安全回滚，committed 后不得撤回成品 |
| **分阶段确认和明确不确定结果** | 能在无外部副作用时拒绝继续；在已有交付时保留真实事实 | 推荐；需增加结果类型、phase 安全约束及存储能力验证，不能一处 catch 改完 |

### 接口契约草案与阶段安全表

[RECOMMENDATION] `write_checkpoint(snapshot, requirement) -> JournalWriteResult(ok, sequence, durability, error_code)`；durability 至少区分 `replaced` 与 `sync_confirmed`，不得用“写入函数返回”声称设备断电耐久。仅在选定的关键提交检查点研究 flush/fsync，按[官方机制研究](evidence/research.md)区分 SQLite WAL 和媒体文件协议，不对每条进度或每次非关键快照强制同步，并验证 Windows/目标文件系统支持与时延；目录项及硬件缓存边界未验证前不宣称断电零损失。

| 失败位置 | 推荐允许的动作 | 明确禁止 |
|---|---|---|
| create/prepared，尚未向用户目录写任何文件 | 停在可重试的记录不可写状态；保留已下载字节或在确认无外部副作用后按用户动作丢弃 | 仍带故障进入 reserve；日志失效时无提示地继续 |
| 写 committing 准入记录失败，reserve 尚未开始 | 不 reserve；保留此前安全状态及原始异常；记录不可用不得假称已进入可恢复事务 | 因内存已改 committing 就直接复用普通取消分支 |
| 部分占位已创建，下一检查点失败 | 停止发布；仅清可证明归属的空占位，保留 payload；清理不完整转人工待处理 | 未记录 owner 的文件按名称删除；把“attempt失败”当作用户目录已干净 |
| 已发布部分成员且尚有成员未交付，写 published/下一 publishing 失败 | 停止继续扩大影响；用内存成功记录与指纹决定可验证补偿；归属/实际执行结果不确定则保留现场并报告不确定 | 重放 os.replace 猜测成功；依据过时磁盘 journal 删除目标；自动宣布整组回滚成功 |
| 最后成员已物理发布且本轮内存确认其成功指纹，但该成员 published 检查点失败 | 按整组已交付收口，不再走部分发布补偿；保留证据并标记记录待修复。若最后成员执行/归属尚不确定，则进入 needs_reconciliation，不能伪造成功 | 因错误发生在 `_publish_group` 内，就机械进入当前外层 `_compensate` 撤回整组 |
| 所有成员已确认发布，最终 committed journal 失败 | 将本轮内存交付事实封为成功，保留 journal/receipt 待修复，记录同步告警；释放路径预留必须有 finally | 因记录失败撤回已交付整组；将用户成功下载显示成下载失败 |
| 补偿完成后写 prepared 失败 | 磁盘继续保守视为 committing/待处理，重启重新核验；允许多留现场 | 提前发布安全状态，或假设 journal 已反映补偿完成 |

[RECOMMENDATION] `commit(plan) -> CommitResult(delivery, checkpoint, receipt)`，delivery 为内部结果 `not_published / rolled_back / committed / needs_reconciliation`；它不是新增日志 outcome 枚举。若结果为 needs_reconciliation，不允许由普通重试自动重复交付。已经 committed 的成功边界继续服从 AGENTS；“全部已物理发布但最终记录失败”的新分支需要在交付事实封存后进入独立收尾，不能把全局 raise 塞回 `_publish_group`。

### 迁移、旧 journal 与回滚

[RECOMMENDATION] v1 仅作读取和保守核验；缺少确认级别不能默认升级为强保证。A01 新格式与 A02 隔离/扫描策略一同上线。回滚应用时不删除新现场，不把不确定状态降成旧 prepared；若旧版本无法理解新协议，应只允许新任务使用旧流程，已有新现场由升级后的恢复工具/版本处置。临时回退策略必须在开始新事务前选定，不能在一次部分发布中切回旧语义。

### 验收故障矩阵

| 故障注入点 | 必须断言 | 现有测试证据/缺口 |
|---|---|---|
| tmp open/json write/replace 各失败；首次准入前 | 用户目录无新文件，不启动 publish，原始原因可定位 | 本轮搜索未定位 journal OSError 分阶段注入测试 |
| publishing 已记录、replace 成功、published 记录失败 | 不盲目二次发布；归属不明保留，不能自动判安全回滚 | 现有 [test_11_journal_is_write_ahead](../../tests/test_download_staging.py#L856) 检查顺序，未覆盖记录本身失败 |
| 最后成员 published 检查点失败；随后最终 committed 检查点失败，分别注入 | 两处均在已确认全组物理交付后保留文件、成功不变；checkpoint 告警及可重启对账证据可见，必须单独覆盖 `_publish_group` 内的前一处 | [test_25](../../tests/test_download_staging.py#L1532) 覆盖 committed 后异常，不等于这两个新窗口 |
| 补偿过程和安全状态回写分别失败 | 不把未知现场记为 prepared；可重复读取但不重复破坏 | [test_26b](../../tests/test_download_staging.py#L1604) 覆盖回退顺序；需补写失败 |
| 子进程硬退出/受控机器掉电场景 | 分别声明进程崩溃与设备掉电结果；核验 journal/实际文件/DB | 本轮均未执行，单元 mock 不替代耐久验收 |

## 3. A02：未知 GC 现场默认保护，并设计旧版本安全回退

### 源码事实与现有缘由

[CONFIRMED/static] GC 按默认六小时与本进程 live staging_id 过滤；识别 committing 则只清指纹匹配且零字节占位，保留沙盒；rollback_failed 保留；其它 phase 进入 rmtree。坏 JSON/读取失败/非 dict 返回 None，未知 phase 同样落入删除分支；没有 version/schema 的准入判断。[gc_orphans](../../src/fluentytdl/download/staging.py#L1744)、[分支](../../src/fluentytdl/download/staging.py#L1807)、[_read_journal](../../src/fluentytdl/download/staging.py#L1864)

[CONFIRMED/static] Manager 提供的是当前 active/pending worker 的 live 集合，扫描配置目录及恢复 worker 下载目录。[启动调用](../../src/fluentytdl/download/download_manager.py#L291)。[INFERRED] 年龄是保守延迟，不能独立证明所有进程都不再使用该目录；如果有另一实例指向同目录，其活动事务未必在本进程集合内。[UNKNOWN] 本轮没有启动多实例验证这条风险。

[INFERRED] 现策略便于回收早期崩溃与无用暂存，避免磁盘无限增长；与 A01 结合时却可能把“无法证明安全”错误等同“没有外部副作用”。因此不能只追加一个 unknown 状态名：旧 GC 对未知 phase 本来就会删除。

### 方案、契约与取舍

| 方案 | 取舍 |
|---|---|
| 保持现状，延长 TTL | 最便宜；仅延迟风险，也延迟正常回收，不解决损坏/版本不明 |
| 所有残留永不删除 | 最大限度保留证据；磁盘耗尽会反过来增加写入失败，没有处置出口 |
| **先分类、再确认所有权和动作；未知保护** | 推荐。可证安全/已完成按规则收，未知或版本不支持只列为待处理；提供用户可理解的保留/隔离/删除入口 |

[RECOMMENDATION] `inspect_orphan(path) -> OrphanAssessment(schema, phase, owner_status, side_effect_evidence, action, reason)` 为只读；`apply_assessment(assessment, expected_identity)` 在执行前复验 canonical 路径、目录身份、无活 owner、非 reparse point 等条件。owner_status 至少区分 `live / proven_dead / unknown`，PID 单独不作证明，需启动身份/进程创建时间或 OS 锁；证明失败默认只报告。扫描预算限制 I/O，但不以预算为理由删除未完成分类的现场。

[RECOMMENDATION] 未知现场优先原地保护；只有已确认无活动 owner 且能够保持恢复引用完整时，才允许移入同卷受控隔离区。隔离是可选移动，不是自动恢复；记录原路径/身份/原因，迁移失败保留原位，不跨卷复制后盲删源，不自动触碰沙盒外目标。用户删除未知现场需显示可能丢弃的暂存字节与外部文件未被处置的事实；不以“清缓存”文案隐藏含义。恢复/GC 读取 journal 的外部路径一律视为待校验输入。

### 迁移与旧 journal 兼容回滚

[RECOMMENDATION] 读取 v1 时做 schema 最小校验，无法确认无外部副作用的 prepared/无记录现场也归待核验；不要为方便回收把缺字段补成“安全”。旧版本忽略 version，因此**仅 version=2 或新增 phase 不能保障回滚**。新的 v2 活事务/隔离现场应放在旧 `gc_orphans` 未扫描的新私有根，路径规则同步到所有 Staging/GC/安装清理入口；旧 `.fluent_temp` 只读识别后进行可审计迁移，原始 journal 先保留。若无法可靠迁移或确认无 owner，则阻止自动降级清理，保留现场并说明旧版无法接管；不得声称单纯将 phase 写成 committing 就获得安全兼容。

[RECOMMENDATION] 新根优先仍位于各任务 `download_dir` 下，采用不同的受控根名避开旧枚举，不默认移至系统盘统一缓存。创建/迁移时验证实际卷身份与无 reparse 跳转；不能只因路径前缀相同就认定同卷。若最终目标按用户设置位于另一卷，显式走 A03 跨卷交付并验额外空间/复制故障；未知现场的隔离不得自动跨卷复制后删源。现有根定位见 [create](../../src/fluentytdl/download/staging.py#L757) 与 [GC root](../../src/fluentytdl/download/staging.py#L1770)。

### 验收故障矩阵

| 场景 | 必须断言 |
|---|---|
| 无 journal、截断、非 dict、未知 phase/version、错误字段类型 | 不默认 rmtree，不因单一坏现场中断整次扫描；可读原因 |
| 同机另一进程持有锁；owner 查询失败；PID 已重用 | 活跃/不确定现场均不迁移不删；当前进程 live 集合不是唯一依据 |
| 同卷隔离移动前后强退、移动失败、目标已存在 | 原/新位置至少一份可定位完整证据；不得覆盖隔离目录 |
| 新版写新格式→回滚旧应用→再升级 | 旧 GC 不触达新根；旧任务仍可新建；新现场可再读，不能装作旧版能续做 |
| 用户选择删除，目录在确认后被替换为链接或外部身份 | 执行前复验拒绝；用户授权的对象必须与实际对象一致 |

[CONFIRMED/static] 已有 [test_11b](../../tests/test_download_staging.py#L881) 反而明确断言无 journal 删除；[test_11c](../../tests/test_download_staging.py#L903) 校验零字节指纹；[test_11d](../../tests/test_download_staging.py#L922) 覆盖 live/新目录过滤。变更测试应体现策略有意变化，不能把旧断言简单删除。上述多实例、未知 schema、隔离及降级矩阵均需补充；未运行现有测试。

## 4. A03：保留整组冲突策略，补齐占位和补偿的所有权窗口

### 源码事实与缘由

[CONFIRMED/static] CommitPlan 同组后缀，拒绝重复目标；reserve 在全局锁下用 O_EXCL，EEXIST/EACCES 都被视为换后缀冲突，最多 0..999。每轮占位先暂存局部 `created`，整组成功后才写入 area `_placeholders`/`_items_state`。非这两种 errno 立即抛出。[build_plan](../../src/fluentytdl/download/staging.py#L1227)、[_reserve_group](../../src/fluentytdl/download/staging.py#L1327)

[INFERRED] 若先创建了一部分占位，再因磁盘满等其它错误退出，该局部 created 尚未归入 area，外层补偿未必拥有这些记录；这不是已复现残留。将权限拒绝等同重名还可能遍历许多后缀后才报错，掩盖真正不可写阶段。保留 O_EXCL 和整组后缀是必要的，否则两个任务可能覆盖目标或把字幕分到别组。

[CONFIRMED/static] 同卷发布使用 os.replace；跨卷是 copy2→长度/主媒体最小尺寸校验→同目标卷临时文件 replace，源仍留 payload。临时名含 staging_id 前八位和成员 index；不是完整 UUID。补偿只处理 state=published 且目标指纹匹配的项，按反序搬回/删副本；失败后 rollback_failed。[publish](../../src/fluentytdl/download/staging.py#L1379)、[cross-volume](../../src/fluentytdl/download/staging.py#L1403)、[compensate](../../src/fluentytdl/download/staging.py#L1427)

### 方案与推荐

| 方案 | 取舍 |
|---|---|
| 保持现状，仅错误文案细化 | 不改变交付动作；局部归属登记窗口和临时路径冲突仍在 |
| 每次发布前 exists 检查、冲突直接覆盖 | 拒绝：检查与使用竞态，覆盖用户文件；破坏已有约束 |
| **保留整组 O_EXCL，逐项登记和分层失败** | 推荐。每项创建即纳入可补偿记录，预留失败的 finally 处理本轮 created，错误区分重名/权限/空间；保留可解释副作用 |

[RECOMMENDATION] `reserve_group(plan) -> ReservationSet(owner_id, members)` 内部每项返回 `ReservedPath(path, handle_identity, fingerprint, status)`，不得只靠字符串。对确实存在目标的 EACCES 可视为冲突，对目录不可写等证据明确的权限错误直接返回阶段错误；不确定时不按重名无限重试。发布前复验占位身份能缩小窗口，却不是绝对防并发篡改保证；若目标系统缺少所需原子 no-clobber/句柄能力，应保留威胁边界，不能宣称“复验+replace”消除了所有竞态。

[RECOMMENDATION] 跨卷临时文件用完整 owner 身份加排他创建，复制/校验/rename 都在所有权对象内；大小校验保留为默认成本，是否增加内容哈希由真实文件大小与存储故障验收决定。不要为了“强校验”无条件重新读取每个大媒体两遍。`CompensationResult(restored, retained, uncertain)` 不能只返回 bool；目标指纹不符、已被用户改动或复制结果不明确时保留并提示人工核对。

### 迁移、回滚与验收

[RECOMMENDATION] 新对象不要求改 Manifest 意图语义；旧 v1 没有每项句柄/临时 owner 信息时只做现有可证明的核验，不补造所有权。新字段仅由新协议事务写；A02 的根隔离继续适用。回滚前必须停止创建新事务并完成/隔离现有事务，不能在部分占位已产生时切算法。

| 故障 | 断言/覆盖 |
|---|---|
| 两线程及两独立进程同名媒体+字幕+封面 | 整组后缀一致、用户旧文件不变；现有 [test_08](../../tests/test_download_staging.py#L694) 是两线程，不是多进程证明 |
| 第二个占位创建报 ENOSPC；首个占位删除报权限错误 | 每个已建占位有 owner 记录；删除失败明确 retained，不伪称目录干净 |
| 目标目录不可写、现有目录占同名、后缀耗尽 | 分类和重试次数正确；不把权限问题显示成网络重试 |
| 跨卷 copy 中断、大小不符、校验后目标被替换 | 保留源，不删外部替代文件，临时文件只清自己的；[test_09/09b](../../tests/test_download_staging.py#L746) 通过 monkeypatch `_same_volume=False`，仍需真实双卷验收 |
| 第 N 成员发布失败、补偿失败、指纹被改 | 无盲删、rollback_failed 可定位；已有 [test_10/10b](../../tests/test_download_staging.py#L786) 可作语义基线 |

## 5. A04：DBWriter 的提交回执与退出屏障

### 源码事实与现有缘由

[CONFIRMED/static] TaskDB 是共享连接加写锁，SQLite WAL、synchronous=NORMAL；这套数据库 WAL 与 Staging JSON journal 是不同对象。TaskDBWriter 有真实 daemon 后台线程、无界 Queue、普通批次最多 200，同 `(op, db_id)` 保留最后出现位置，以保住 metadata/result 对 output_path 的先后顺序。[TaskDB](../../src/fluentytdl/storage/task_db.py#L67)、[writer](../../src/fluentytdl/storage/db_writer.py#L40)、[coalesce](../../src/fluentytdl/storage/db_writer.py#L147)

[CONFIRMED/static] `_process` 自己捕获异常，因此不少单条写失败不会传播到外层 batch rollback；flush_and_stop 只投毒丸并 join(timeout)，不返回是否成功；没有停止状态禁止后续 enqueue，drain 把余项一次交给 process_batch，未沿用 200 上限。Manager 先 stop_all、QThread wait/terminate/wait，再 writer join 3 秒，未把数据库确认纳入返回 bool。[process](../../src/fluentytdl/storage/db_writer.py#L167)、[flush](../../src/fluentytdl/storage/db_writer.py#L74)、[drain](../../src/fluentytdl/storage/db_writer.py#L116)、[shutdown](../../src/fluentytdl/download/download_manager.py#L562)

[INFERRED] 异步合并能避免高频进度写阻塞 UI，这是应保留的优化；缺少确认则让“已入队”“批次结束”“DB 提交成功”容易混为一谈。仅把 daemon 改 non-daemon 或延长 join 都没有解决在途 Qt 信号和迟到生产者问题，反而可能挂住退出。

### 方案与接口草案

| 方案 | 取舍 |
|---|---|
| 保持现状，加长超时 | 无契约迁移；慢盘仍可能超时、生产者仍能迟到，不推荐作为根治 |
| 所有更新同步写 SQLite | 简单获得调用回执；高频进度阻塞 UI/下载线程，违背原设计目的 |
| **进度可合并，关键命令有回执，关闭显式收口** | 推荐。保留吞吐优点，失败可回传，结束时能回答到底还有什么未完成 |

[RECOMMENDATION] `enqueue(command) -> Admission(accepted, sequence, reason)`；关键命令提供异步 `WriteReceipt(sequence, committed|failed|superseded, error)`，仅在所属事务 commit 成功后发 committed。`flush_through(sequence, deadline) -> FlushResult` 区分入队已消费、提交已成功、失败项与未完成项。内部 `_execute` 必须把实际 SQL 异常交给事务 owner；重试只对幂等覆盖命令有界进行，不能吞异常后仍发成功回执。未知 op 要显式 rejected，不沿用静默忽略作为确认协议。

[RECOMMENDATION] writer 生命周期为内部 `OPEN→DRAINING→CLOSED`，接受/封口由同一锁保护；DRAINING 后拒绝新写需有明确接收者，不静默丢弃。关闭先阻止新 worker/任务，协作结束已登记生产者（包括从可见列表移除但未退出的 worker），让 Qt 终态桥接实际交付，再按已确认 sequence 封口，分批排空，最后确认回执。不可在 Qt 主线程 blocking wait 的同时期待该线程消费排队信号；宜用异步关闭状态机。超时返回“有未完成交付/写入”，交由退出流程明示；强退仍可能丢未持久消息，不称优雅退出成功。

### 迁移、兼容与回滚

[RECOMMENDATION] 先增加可选回执并保留旧 enqueue 方法适配层，再接关键终态，最后启用封口；高频状态最后值合并不可跨 run/终态屏障。A04 本身可不改数据库 schema/旧 journal，旧应用可以继续读取原任务表；若 A05 增列/附表，则用加法迁移，不删旧字段。回滚必须确认新 writer 已关闭且未完成 receipts 已记录，不能让新旧两个 writer 并行写同一连接/协议。

### 验收故障矩阵

| 场景 | 必须断言 |
|---|---|
| 200+ 项、同 task 多 op、多 run 遗留状态 | 普通/退出批次均有上限；最后位置顺序保留，旧 run 不覆盖新 run 终态 |
| 批内一 SQL 失败、commit 失败、busy/磁盘满 | 回执不假成功；明确整批回滚/按项失败策略；有界重试后保留失败原因 |
| 封口与 enqueue 同时发生；毒丸后生产者发消息 | 每条 admission 要么被接受并纳入 flush，要么明确 rejected，没有无主消息 |
| worker 已结束但 Qt 终态信号未处理；移除列表的 worker 尚活 | 关闭不能提前封口；生产者表不只依赖可见 active 列表 |
| flush 超时、再调用 shutdown、强退再启动 | 可重复查询 pending；不报已排空；重启审计按实际持久值执行 |

[CONFIRMED/static] [test_db_writer_batch](../../tests/test_db_writer_batch.py#L32) 覆盖合并、最后顺序、临时 DB 应用结果和 collect 遇毒丸/上限；fixture 用 `__new__` 避开 writer 初始化线程。[fixture](../../tests/test_db_writer_batch.py#L71)、[collect tests](../../tests/test_db_writer_batch.py#L143)。这些用例不是实际后台线程退出、Qt 在途消息、SQL 部分失败或 drain 时限证明；本轮不执行。

## 6. A05：交付成功与历史记录同步分开，对账后再清必要证据

### 事实、缘由与风险窗口

[CONFIRMED/static] 标准 worker commit 后设置 output_path 并发信号，随后 emit_actual、cleanup，再 force_update completed 和 completed 信号。Manager 通过 Qt QueuedConnection 把状态与路径分别 enqueue；最终 SQL 亦分别更新。快速路径也在成功收口前 cleanup。[worker](../../src/fluentytdl/download/workers.py#L1615)、[cleanup→completed](../../src/fluentytdl/download/workers.py#L1675)、[Qt bridge](../../src/fluentytdl/download/download_manager.py#L455)、[TaskDB result](../../src/fluentytdl/storage/task_db.py#L279)、[fastpath cleanup](../../src/fluentytdl/download/workers.py#L1953)

[INFERRED] 文件已交付、journal 已清、Qt/DB 消息还没消费时退出，会留下成品与旧数据库状态；重启可能给出暂停恢复壳，用户再试会生成另一组文件。相反，数据库已有 completed 也不证明之后用户没移动/删除文件。现系统优先保持文件成功事实并尽快清暂存，这一方向正确；应补持久收口证据，而非在 DB 失败时删除文件“恢复一致”。

### 方案与推荐契约

| 方案 | 取舍 |
|---|---|
| 保持现状，重启全部当未完成重新下载 | 简单；可能重复下载/重复成品，记录与文件无法自动解释 |
| DB completed 写成功后才发布文件 | 拒绝：倒置事实，发布失败会留下虚假完成；仍非跨系统事务 |
| **交付 receipt + 原子任务终态更新 + 幂等对账** | 推荐。文件交付结果先确立，记录失败不推翻成功；保留足够证据修复历史 |

[RECOMMENDATION] `DeliveryReceipt(schema, receipt_id, task_id, run_id, staging_id, members, committed_at, checkpoint_quality)` 记录全部交付成员及实际目标指纹；不包含 Cookie、带签名下载 URL 等不必要上下文。主路径和 fastpath 统一交 `finalize_delivery(receipt)`；TaskDB 在单事务更新 state/progress/main output/size/run identity，并登记唯一 receipt_id，返回 A04 WriteReceipt。任务的 UI/DB 状态不新增日志 outcome：UI 可显示“文件已保存，任务记录待同步”；日志 outcome 仍是 success，持久化问题用 signal 及已有错误分类表达。

[RECOMMENDATION] `finalize_delivery` 仅负责 receipt 持久化/移交与 DB ack，不成为第二个终态裁决者或事件生产者。交付结果由 [E01 终态裁决](03-execution-and-control.md)读取，仍由 worker finally 发 run outcome、manager 发 transition；迟到 ack 只改变记录同步展示，不重新宣布一次下载成功。

[RECOMMENDATION] 与总裁决保持一致：needs_reconciliation必须有持久阻止重复交付的标记，所有重试与启动恢复入口读取；如果关联事务尚未核对，不按普通error自动重试。标记/证据写入失败时不得宣称跨启动防重已经保证。旧run终结后才确认交付，修复的是历史与交付事实，使用signal记录后续发现，不覆盖或重发旧outcome；具体入口与验收见[06](06-decisions-and-target-design.md)、[T17](08-validation-and-benchmarks.md)。

[RECOMMENDATION] 仅 DB commit 回执后才清除最后一份未消费交付证据；为免长期保留整个大沙盒，可把小 receipt 先移交专用可靠收件区，确认移交后清大文件暂存。若 receipt 写入/移交也失败，保留原现场并提示，不把“已经尝试写两处”称冗余耐久保证。重启先核验 receipt schema/任务身份/路径归属/指纹，再幂等应用；任务已删除、run 已替换、目标被改动时不得悄悄复活任务或重写现用户状态，应进入待核对列表。没有可靠 receipt 的旧现场不猜测同名文件就是本任务成品。

### 迁移、回滚与验收

[RECOMMENDATION] tasks 保留旧 completed/error 等值；可新增 delivery_receipts 表和版本化 receipt 文件，不替换旧字段。旧数据没有 receipt，继续标注为旧恢复能力；不扫描整个下载根按名称补“成功”。receipt 收件区位于 A02 新协议保护域，回滚旧应用保留它并停用自动对账；再升级后可恢复。不能跨版本同时由两个实例重复认领 receipt，需独占认领/幂等键及期望 task/run 校验。

| 中断窗口 | 必须断言 |
|---|---|
| 文件全部发布→receipt 落盘前 | 成品不删除；在场内存仍成功；下次不能无证据宣称可自动修复 |
| receipt 已落盘→Qt bridge→DB 队列→事务 commit，逐处强退 | 重启最多应用一次同 receipt；state/path/size 同事务；未完成记录不导致无提示重下载 |
| DB commit 成功→回执丢失→receipt 清理失败 | 幂等重读不重复创建任务/统计，不重复发布文件 |
| 窗口关闭、记录被用户删除/同 task 新 run 已开始 | 不复活删除任务，不以旧 receipt 覆盖新 run；明确冲突待处理 |
| 目标已移动/替换，或部分附件缺失 | 不盲改 completed，不覆盖目标；展示实际证据与未知范围 |

[CONFIRMED/static] [test_25/25b](../../tests/test_download_staging.py#L1532) 验证 Staging 已提交异常及 cleanup 失败后 GC；[观测 outcome 测试](../../tests/test_observability_contract.py#L671) 验证 TaskTrace 单 run 收口。它们不覆盖 Qt→DB→文件对账或 receipt 协议；这些验收均是待实现后的新增项。

## 7. A06：把“恢复任务”“暂停继续”和“断点续传”分开承诺

### 已有真实能力

| 场景 | `[CONFIRMED/static]` 当前行为 | `[UNKNOWN]` 不能直接承诺 |
|---|---|---|
| 活 worker 暂停/继续 | 控制同一个 pause Event，不重建 Staging。[pause/resume](../../src/fluentytdl/download/workers.py#L765) | 所有外部后处理都即时暂停 |
| 同 run 自动/after_fix 重试 | 新 attempt，沿用本轮 Staging，prepare_attempt 写各自路径报告。[prepare](../../src/fluentytdl/download/workers.py#L1341)、[retry](../../src/fluentytdl/download/workers.py#L1421) | 每种格式/片段引擎必定复用已下载字节 |
| 重启恢复 | DB active/queued 转 paused，重建壳不自动下载；skip_download 任务标 error 不恢复壳。[load_unfinished_tasks](../../src/fluentytdl/download/download_manager.py#L234) | 重启从上次百分比对应字节继续 |
| 已结束 worker 用户继续/重试 | 复用 db_id、创建新 worker；run 开始新 UUID 事务目录。[controller](../../src/fluentytdl/core/controller.py#L477)、[Staging create](../../src/fluentytdl/download/workers.py#L1229) | 新 txn 自动继承旧 .part/manifest |
| 旧 committing/rollback_failed | 保留供对账，不自动重放 CommitPlan。[GC](../../src/fluentytdl/download/staging.py#L1822) | 自动续提交/自动回滚成功 |

[CONFIRMED/static] 标准 worker 在 enable_resume 配置为真时设置 continuedl=True，但这发生在新建 Staging 之后。[opts](../../src/fluentytdl/download/workers.py#L1270)。[INFERRED] 一个选项不能跨过“旧文件在别的 UUID 目录、没有重建 Manifest/引擎匹配契约”的边界。因此恢复壳上的旧百分比是历史展示，不是已确认可复用字节。

### 方案与建议

| 方案 | 取舍 |
|---|---|
| 保持现有恢复动作，继续统称“继续下载” | 最少修改；用户易把任务恢复理解为精确断点续传 |
| 自动把旧目录绑定到新 worker | 拒绝：可能混入旧格式/字幕/Feature 结果，复用不兼容临时文件，破坏每 run 所有权隔离 |
| **先明确恢复级别，后评估受控字节复用** | 推荐近期做前半：A05 排除已交付未记账，余者明示“重新开始”；真正跨启动字节复用单列后续实验 |

[RECOMMENDATION] `assess_resume(task_snapshot, orphan_assessment, runtime_identity) -> ResumeCapability(mode, reasons, reusable_bytes_estimate)`，mode 内部为 `live_resume / new_download / delivery_reconcile / manual_review`；估计字节不是进度承诺。UI 动作和提示由此结果派生，不从“旧 state=paused”直接推断能力。

[RECOMMENDATION] 若后续增字节复用，先只支持可验证的下载阶段，排除 committing/rollback_failed、直播、未知 schema、需丢失弹窗上下文的轻量任务和不匹配引擎。候选契约至少要媒体身份、选中格式/字幕/区段和影响输出的 opts 指纹、运行时版本、旧 sandbox owner、临时片段布局/校验、原始字节与已后处理字节区分；不得保存可过期签名 URL 或凭据以图省事。新 run 重新解析 URL，在取得旧现场独占权并验证后把明确可复用候选导入新事务，保留来源记录；不能把整个旧 payload 直接 reconcile 成自己的产物。

### 迁移、兼容回滚与故障验收

[RECOMMENDATION] 初期只改能力表达与对账，不改变 v1 旧目录所有权，回滚不影响旧文件。后续候选 schema 由新根保存，旧 v1 不自动转换为可续传；回滚时停止认领，保留候选供新版处理。任何候选不匹配都回到用户可理解的新下载/人工处理，不通过“尽力试一下”静默拼接。

| 场景 | 必须断言 |
|---|---|
| 活暂停→继续、同 run retry、重启再继续 | run/attempt/staging_id 是否变化与 UI 文案一致，不伪造复用字节 |
| 改画质、区段、语言、容器、Feature 配置或引擎版本 | 不兼容候选拒绝；原现场保留；不把旧副产物交付 |
| 链接过期、原视频变化、直播、轻量上下文缺失 | 重新解析/新下载或明确不支持，不无限重试旧签名地址 |
| 候选导入中退出、另一实例同时认领、旧路径变链接 | 不损毁原候选，不跨 owner 复用，不自动扫入未知文件 |
| A05 已交付 receipt 待入库且用户按“继续” | 先对账/提示，不能默认重新下载产生重复成品 |

[CONFIRMED/static] [test_20/20b/20c](../../tests/test_download_staging.py#L1225) 钉住独立事务目录和不复用目录；[trace retry/new_run](../../tests/test_observability_contract.py#L207) 验证身份规则。这些不能作为跨进程/跨启动实际续传通过证据；未看到对应真实引擎字节复用验收，本轮亦未执行。

## 8. 文档评审与后续实施门槛

[RECOMMENDATION] 六项共享的验收底线：用户已有文件不变；已交付成功不因持久化/清理问题反转；未知归属不自动删除；普通取消不跨过提交临界区；任何回执明确区分接收/应用/提交；降级旧版不破坏新现场。验收同时记录文件树/指纹、journal/receipt、DB 行、UI 状态和 trace，但日志缺失本身不应阻断文件安全裁决。

[RECOMMENDATION] 后续实施需先冻结接口与故障分类，再用隔离临时库、合成媒体、可控子进程和真实 Windows 双卷补矩阵。现有 AST 规则检查集中目录扫描与破坏性文件操作，应随新增协议更新明确边界，而不是扩大通配豁免。[架构扫描约束](../../tests/test_download_artifact_architecture.py#L339)

[UNKNOWN] 本文没有性能数据、磁盘耐久实测、多实例复现、端到端 UI 一致性测试或任何下载成功证据。所有推荐取舍是待交叉复核的设计结论；尤其 A01/A02/A05 必须作为一个兼容性整体评审，不宜拆成“journal 改抛异常”“unknown 改保留”“completed 多写一次库”三个零散修补。
