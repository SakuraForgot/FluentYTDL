# 验收、故障注入与性能测量方案

全部为[RECOMMENDATION]，本轮只读现有测试并检查文档，**没有运行以下案例或测得任何提升**。已有测试的准确覆盖详见02–05，尤其mock进程、mock跨卷和直接调用helper不能代替产品全过程。

## 1. 证据层级

| 层级 | 用途 | 不能证明 |
| --- | --- | --- |
| L0 源码与纯函数 | compiler/决策矩阵/调用所有权；合成参数对照 | Qt投递、进程退出、磁盘持久性 |
| L1 临时真实文件/DB与受控barrier | 替换、冲突、SQL回滚、journal阶段、线程等待 | 真实子进程树、断电、跨卷硬件行为 |
| L2 Qt事件循环+假CLI协议进程 | stdout静默/大量输出、取消、队列/GUI桥、资源归属 | 真实yt-dlp/FFmpeg所有版本与媒体网站行为 |
| L3 Windows受控工具/真实不同卷 | 进程树、Job、FFmpeg取消、编码路径、同卷/跨卷/文件锁 | 外部网络与电源故障全过程 |
| L4 授权素材与受控环境 | 六模式、实际工具组合、有限网络场景与冻结包 | 任意网站、任意硬件或永远不失败 |

用barrier精确布置竞争窗口，超时仅作失败界限，不用sleep碰运气或terminate测试线程掩盖问题。产品测试采用现有conftest的临时数据根与offscreen；涉及进程/GUI线程必须单独观测，不把窗口消失当完成。

## 2. 正确性与故障矩阵

| ID / 提案 | 触发场景 | 最低断言与层级 |
| --- | --- | --- |
| T01 E01 | 标准/轻量/直链：0、非0、无产物、校验失败 | L1/L2：每run一个outcome，产物归属不串线，空产物不凭rc0成功 |
| T02 E01/E02 | cancel在EOF前后、execute返回后、终态锁定前后 | L2：按线性化顺序裁决，无unset常规路径，无取消后新attempt |
| T03 E01/A01 | commit后通知/cleanup错误；最后成员published检查点失败；最终committed记录失败 | L1：已确认整组交付保持成功，只signal；未知结果不伪造成功；finalizer最多一次 |
| T04 E02 | cancel早于等待发布、正进入wait、retry同时到达、旧generation动作 | L1/L2：等待有限结束，slot归还，取消永久谓词与commit gate同源 |
| T05 E05 | 密集/静默stdout、退避暂停、unsupported模式、commit中暂停 | L2/L3：请求与确认分开；不宣称实际网络已停；迟到暂停不覆盖终态 |
| T06 I02 | 旧请求晚于新请求、重复URL行、窗口销毁、取消静默 | L2：旧epoch写model=0；每实际请求settled一次；active最终0，仍可接纳后续任务 |
| T07 I03 | request A开始→手动clear→A返回；相同/不同context共享请求 | L0/L2：旧cache_epoch不回填；TTL0保持；取消单订阅不影响其它；强制刷新不复用旧请求 |
| T08 C01/I01 | 选中账户无材料/验证拒绝/meta保存失败；刷新与创建副本交错 | L1/L2：选择和生效分开；binding来源与字节同代，无证明则unknown；匿名无Cookie/POT策略不被自动改 |
| T09 C02/C04 | runfile获取后每个初始化点抛错；合成代理秘密与嵌套异常 | L1/L2：源字节不变、消费者退出后清副本、日志无合成秘密；非托管语义保持 |
| T10 E03 | 静默yt-dlp/VR FFmpeg、刚Popen未登记、登记时cancel、终止失败 | L2/L3：所有直接子进程纳管，等待退出证据，取消不被Feature catch降warning；不删活资源 |
| T11 C03 | 两任务同POT一取消、双实例、非本项目端口、Job失败 | L3：取消仅waiter，未知owner不杀；enabled未就绪仍前置失败；cancel不触发共享恢复 |
| T12 A04 | 批内SQL错、commit错、busy、磁盘满、旧run迟到 | L1/L2：ack不假成功；原子批策略明确，有界重试，旧run不覆盖新状态 |
| T13 A04 | worker结束但queued信号未消费、从列表移除仍活、封口与enqueue竞争 | L2：accepted消息均有归宿；关闭不阻塞GUI消费自身信号；超时列pending而非成功 |
| T14 A03 | 创建第一个占位后第二个异常；外部替换目标；同名并发 | L1/L3：仅删除可证自己占位，保留外来文件，整组命名一致；真实跨卷另验 |
| T15 A01 | 每次checkpoint前后、部分publish、全部publish、补偿回写失败 | L1/L3：按阶段停止/补偿/保护，无统一raise导致错误撤回；文件事实和journal质量分开 |
| T16 A02 | 坏/无/未知journal，PID复用/权限不足，新版→旧版GC→新版 | L1/L3：未知不默认删、新根不被旧GC访问、隔离不跨卷盲删；旧v1不凭补字段升级保证 |
| T17 A05/A06 | receipt前后、DB commit前后、ack丢失；删task/新run/文件变化；待核对时单项/批量/重启重试 | L1/L2：不重复发布、不复活已删任务、不旧run覆盖、无证明不猜同名文件归属；待核对门跨入口生效，后来确认不重发旧run outcome |
| T18 I01/E06 | 六模式True/False/缺省，旧/未知Plan，after_fix只改允许项 | L0/L2：输入不被compiler修改；语义变化有原因；task/run/attempt身份与显式意图保留 |
| T19 I05 | 排队换工具、custom/PATH、同identity多probe/不同identity慢probe | L0/L2/L3：同run实际身份一致或显式重绑；不覆写自管工具；probe不持全局长锁，不把timeout当版本 |
| T20 I04 | 0/1/50/51/500/501项、缺URL、重复子回调、准入中取消/DB失败 | L0/L2：批次部分接受准确、幂等、不默默截断；每目录批次初扫至多一次但最终防覆盖仍验证 |

E04共享runner没有独立降低验收标准：接入一条策略即重跑对应T01/T02/T09/T10及该策略真实素材子集；涉及输出解读的改动追加全部模式回归。

## 3. 六模式与变体覆盖清单

| 模式 | 需要覆盖的关键组合 |
| --- | --- |
| 视频 | 默认/指定format，原音轨/语言回退，字幕/封面可选，匿名/认证，重试与后处理 |
| VR | 格式来源、转换成功/失败/取消、原媒体保留、生成产物晋升、元数据阶段 |
| 频道 | 三tab中单失败/空/不支持/取消、详情晚到、父批计划部分接纳 |
| 播放列表 | flat与详情、重复URL、滚动优先、QuickAdd阈值、跨块取消、恢复旧任务 |
| 字幕 | 多语言别名、明确无字幕、延后解析取消、轻量零产物、用户未请求媒体 |
| 封面 | 直链/轻量、过期URL重取匹配失败、极小合法图片、名称碰撞 |
| 音频/片段变体 | 目标容器、语言过滤、多音轨、precise互斥、部分进度线程、输出格式变换 |

不做不可控的全部笛卡尔积。每个模式至少一个正常样本、一个前置失败、一个执行中取消、一个交付后收尾异常；高风险变化再按T表有针对性组合。来源需可授权使用且有固定预期，不借网络波动替代可重复夹具。

## 4. 性能实验：先拆时段，再比较

建议按单调时钟记录：input→first_selectable、parse_queue_wait、POT_wait、runtime_probe、CLI_metadata、JSON/model_apply、admission、download_queue_wait、transfer、postprocess、verify/commit、DB_commit_ack、cancel_requested→settled、shutdown_requested→all_ack。

字段只包含内部任务标识、模式、阶段、策略版本与安全资源量。不收Cookie、token、账号原文、完整签名URL和用户目录。高频数据留有限内存聚合，阶段末一次输出；collector异常必须best-effort。重复run或失败重试分开统计，不能把失败样本丢弃后报告“更快”。

| 实验 | 控制变量 / 对照 | 主要指标 | 采纳判断 |
| --- | --- | --- | --- |
| B01 批量准入 | 相同合成目录、相同任务序列，0/1/50/500项；旧循环vs分块索引 | listdir次数、DB插入次数、UI heartbeat延迟、峰值内存、取消延迟 | 次数目标满足且无重复/丢任务；耗时按实测，不凭复杂度声称秒数 |
| B02 在途合并 | TTL0、同有效上下文N个订阅，对照N个独立调用；不同身份作负例 | 真实CLI启动次数、首屏/尾延迟、取消传播、内存 | 等价请求降为一份、独立取消正确；实际重复请求不足则不启用 |
| B03 runner提取 | 同一假CLI字节流/退出/静默行为，逐策略前后对照 | 输出消费、峰值缓冲、进程清理、CPU/延迟、维护路径数量 | 首先语义一致；维护简化不能牺牲快速路径轻量性 |
| B04 writer回执 | 同样进度/终态比例、慢SQL及持续生产者；不同批次上限 | queue高水位、coalesce率、ack延迟、GUI延迟、退出完成率 | 不丢关键命令，不无界积压；吞吐提升不是唯一目标 |
| B05 文件协议 | 相同大小/成员数、同卷/真实跨卷、不同锁/磁盘条件 | 关键checkpoint耗时、copy字节、commit尾延迟、故障后可恢复性 | 先通过文件安全门，再比较同步成本；不能为了速度取消关键安全记录 |
| B06 POT/预检 | 冷/热预热、成功/失败/取消、同/不同runtime身份 | 首次就绪、等待方取消、probe合并率、启动次数 | 不杀共享服务、不静默降级；增加探针本身的成本列入 |

所有表中尚无实测数值。建议先热身，再对每个确定性配置做多轮交错A/B；例如每组30次作初始样本，不把它自动当充分统计功效。报告p50/p95/max、失败/取消数量、环境/工具版本、样本量与分布；网络实验波动大时不强行归因。冷缓存和热缓存分组，吞吐和响应分别记录。

第一轮只设正确性和操作次数硬门；耗时/内存阈值在取得可信基线后确定，不能在结果出来后挑有利指标改门槛。02提出的每块20项或8ms是待校准起点，单次IO可超时，不能宣称硬实时保证。

## 5. 回归资产与新增测试原则

保留当前[test_download_staging](../../tests/test_download_staging.py)、[test_db_writer_batch](../../tests/test_db_writer_batch.py)、[test_observability_contract](../../tests/test_observability_contract.py)、[test_parse_cache](../../tests/test_parse_cache.py)、[test_youtube_cookie_mode](../../tests/test_youtube_cookie_mode.py)等资产。新增测试应断言用户可观察结果、资源归属和危险交错，避免只断言新方法被调用或重写一份实现逻辑。

已有AST唯一producer/gate检查可以作为辅助，但重构后行为验证更重要。变化会有意改变旧GC无journal删除等策略，需保留旧行为说明及新版迁移断言；不能删掉冲突测试后声称全通过。

## 6. 实施验收记录模板

```text
工作包 / 提案 / G要求 / T案例 / B实验
源码版本与既有未提交修改范围
环境 / 工具身份 / 数据根 / 素材及授权范围
执行层级 L0–L4 / 命令 / 通过失败跳过计数
任务状态 / 文件与journal / 子进程 / UI与DB / outcome
旧新版对照数据及所有失败样本
剩余缺证 / 回滚演练 / 是否可以扩大接入
```

当前表格均为后续验收设计。本轮实际完成项仅见[evidence/verification.md](evidence/verification.md)和[文档机械检查](evidence/checks.json)。
