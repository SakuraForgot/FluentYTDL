# 入口视角的独立方案复核

2026-09-19，仅文档/source-static。复核范围：未由本作者撰写的 [00目标](../00-goals-and-boundaries.md)、[01链路](../01-download-chain.md)、[03执行控制](../03-execution-and-control.md)、[05身份与资源](../05-context-and-runtime.md)。聚焦匿名模式、有效凭据、POT共享生命周期、Plan/RunSpec/资源scope的所有权；没有扩大为全系统复审，没有修改作者文件、运行产品或测试、联网或读取真实数据。

“通过”表示在所列范围内建议与已知源码约束相容，**不表示提案已经实现或验证性能**。新接口与修订仍为 RECOMMENDATION。

## IR-01 · 有效材料版本必须与消费者字节对应

**挑战：** C01建议“meta缺失或txt/meta不一致时标unknown”，但只列结果字段，没有说明如何发现“语法正常的旧meta + 新txt”。该判据是ExecutionBinding可解释身份的核心，不能依靠字段命名解决。

**源码反证 [CONFIRMED/static]：** [commit](../../../src/fluentytdl/auth/cookie_sentinel.py#L424)先replace txt，再[_save_meta](../../../src/fluentytdl/auth/cookie_sentinel.py#L180)；后者捕获所有Exception只warning，调用端仍清warning、返回True。现有runfile [copyfile](../../../src/fluentytdl/auth/cookie_runfile.py#L79)又是独立读取动作。账户切换调用[同步](../../../src/fluentytdl/auth/auth_service.py#L1307)也不能从selected ID推导材料来源。

**裁决：需补充设计。** [RECOMMENDATION] 明确发布者与读取绑定共享一致性协议：可选择所有受管写入口和复制快照共用每平台短锁/提交receipt，或不可变material版本加原子指针。新绑定消费本次字节快照对应的非秘密版本及来源；没有证据时unknown。不得只靠meta存在/可解析便绑定来源；跨进程和崩溃后不能证明的旧材料保留unknown，不凭空补身份。

**验收补充：** 发布A→txt换为B→meta写失败→resolve/copy；并发切换与copy；重启读取旧meta。断言最终binding的revision/source与实际runfile一致，或明确unknown；用户指定身份时unknown不能静默按该账号成功执行。只用合成材料。

已通知Lead；初次记录待修订读回。

## IR-02 · Binding是资料，scope才是资源释放者

**挑战：** C01接口字段 `owned_runfile` 容易让ExecutionBinding也成为close owner，而C02和E03又让ResourceScope负责文件/消费者清理。

**裁决：需限定接口，而不是要求增加抽象。** [RECOMMENDATION] 将其命名为借用handle/resource_id或注明仅引用；唯一close owner是E03统一run/attempt ResourceScope，ExecutionBinding只描述mode/platform/effective source/material revision。RunSpec引用这些资料，不自行删除runfile，也不拥有POT或Staging提交权。原实现的[runfile上下文](../../../src/fluentytdl/auth/cookie_runfile.py#L58)与[轻量close](../../../src/fluentytdl/download/workers.py#L2325)说明重复清理来源确实需要收口。

**验收补充：** 初始化失败、最后消费者结束、重复finalize三场景中每份副本仅一个清理登记，任何consumer仍活着时不删除；失败时保留资源的原因可观测。

已通知Lead；初次记录待修订读回。

## IR-03 · POT的not-ready与cancelled必须保留不同控制结果

**挑战：** C03设计了ReadyResult分层和任务独立取消，但未显式写出“POT开启而未就绪”是否允许无POT继续，也没有明确cancelled是否被上层普通失败重试吞掉。

**源码 [CONFIRMED/static]：** [build_ydl_options](../../../src/fluentytdl/youtube/youtube_service.py#L560)当前在POT开启时wait_until_ready False就抛前置失败；[wait_until_ready](../../../src/fluentytdl/youtube/pot_manager.py#L807)无任务cancel参数；[端口清理](../../../src/fluentytdl/youtube/pot_manager.py#L151)与共享服务生命周期是不同问题。

**裁决：需补充兼容合同。** [RECOMMENDATION] enabled且not_ready/timeout继续停在preflight，除非用户另行明确关闭该策略，不能新接口默认静默无POT解析。cancelled独立返回并转换为控制取消，不当作普通“POT坏了”触发共享recover/restart；取消一个waiter不改变全局warm状态和别的waiter结果。保持源码现状的依赖政策，改变的是可取消等待与目标所有权。

**验收补充：** enabled+not_ready不启动媒体CLI；A waiter取消、B waiter成功；A取消不能令SharedPOT关闭或重启；等待前/后取消不产生误导网络diagnosis。

已通知Lead；初次记录待修订读回。

## 已挑战且未发现必要修订的部分

| 项目 | 挑战与对照证据 | 裁决与限制 |
| --- | --- | --- |
| 00/01目标是否冒充当前事实 | G01–G09显式RECOMMENDATION；01概念阶段说明不是所有分支严格同序，字幕迟解析单列 | **通过。** 方案不把一次preflight说成工具/材料永久有效。|
| 匿名任务在修复后被自动重新附Cookie | 03 E06固定COOKIE_MODE；C01材料可变而模式不变，对照[enforce_cookie_mode](../../../src/fluentytdl/utils/youtube_request.py#L10)移除三个Cookie参数并清SABR scope | **通过。** 实施仍需保持构建/merge/CLI多门防御；“匿名”在此是Cookie附带模式，不是隐藏所有网络身份。 |
| DownloadPlan与执行投影是否新增重复持久模型 | E06明确只读投影、不另创ExecutionIntent；05表明确消费I01而非复制任务配置 | **通过。** 版本化Plan语义与每run/attempt材料要分开；修复revision只能影响下次attempt，不能改活进程argv。 |
| 进程scope会不会杀共享POT | E03明确POT只有waiter归run scope；C03父owner持Popen/Job、未知端口避让，不按名字/端口广播 | **通过。** Provider是否有nonce/port0仍UNKNOWN；建议没有假设不存在的上游能力。IR-03补的是上层消费结果。 |
| E01终态裁决会不会多次清理/抹去合法重试诊断 | 一处finalizer、纯decision对照、不shadow执行；明确成功run可含旧失败attempt diagnosis | **通过。** 与[finalize_failure](../../../src/fluentytdl/download/staging.py#L1517)committed优先兼容；实际竞态未验证。 |
| E02只加set是否丢掉cancel-before-wait | 新设计永久cancel位、等待generation、锁内谓词发布、循环重读，锁外信号/停止；对照[现有wait](../../../src/fluentytdl/download/workers.py#L1475) | **通过。** 不是“换一种Event就天然安全”；测试明确覆盖发布前/中/后取消。 |
| E03会不会取消VR却降级成功 | 明确VR与Feature普通Exception前透传DownloadCancelled；对照[VR局部catch](../../../src/fluentytdl/download/features.py#L624)和[Feature循环](../../../src/fluentytdl/download/workers.py#L1561) | **通过。** 只登记PID不能解决控制异常吞没，正文已认识此点。 |
| E04共享Runner会不会污染快通道 | 明确Runner不决定期望/format/恢复/重试/commit，标准体积启发式不推广字幕封面 | **通过。** 保留三策略，迁移不双下载；性能收益未测，正文允许延期。 |
| E05暂停承诺是否超过检查点事实 | 明确请求/确认/I/O停止不同；fastpath/VR unsupported，commit不暂停 | **通过。** 与[现有pause](../../../src/fluentytdl/download/workers.py#L765)只在progress等待的边界吻合。 |
| C02是否擅改自管Cookie语义 | 第一批只前移try；managed识别失败与custom区分；新临时目录避开旧GC匹配 | **通过。** 新owner schema与旧版回滚清理责任需真正实现后才可验收，正文保留此限制。 |
| C04诊断是否增加秘密写入或重复outcome | 写者处白名单/脱敏，低频聚合，用既有kind；纯子进程脱敏不初始化服务 | **通过。** 对照[旁路日志](../../../src/fluentytdl/auth/providers/webview2_provider.py#L109)确有必要；regex不能穷尽未知秘密也已注明。 |

## 依赖矩阵待读范围

初次检查时06/07/08尚未完成落盘，因此这里不声称已复核其依赖顺序。需确认：I02→I03在途合并；I01与metadata规格同policy；E03/C02唯一scope；C01一致材料receipt先于需要精确身份的缓存合并；E01/E02及runfile保护区可独立实施，不被整体Plan重构阻塞。按Lead要求仅作此有限读回，不扩大审查。

## 修订读回与有限集成复核

2026-09-19 同轮读回 [05](../05-context-and-runtime.md)：

- **IR-01 已关闭（设计补齐）**。选择结果拆成bytes_committed/metadata_confirmed；新增每平台发布、receipt、副本复制共享短临界区，跨进程同OS互斥协议；明确旧meta或前后revision相等不足证明字节一致。旧材料无法证明时unknown，不把更强不可变版本迁移塞入最小修复。
- **IR-02 已关闭（接口限定）**。字段改为borrowed_runfile_resource_id；ExecutionBinding只借用，ResourceScope唯一close/清理owner。
- **IR-03 已关闭（兼容合同）**。POT enabled且not_ready/timeout仍停preflight；cancelled独立传播，不触发共享重启/恢复，不为减少等待静默去掉POT。

已读 [06总裁决](../06-decisions-and-target-design.md)：21项提案数目与I01–05/E01–06/A01–06/C01–04一致；RunSpec仅运行快照、ProcessScope仅ResourceScope进程部分、WriteReceipt与DeliveryReceipt分开、POT保留原服务owner。E01/E02、I02、I03失效epoch、C02保护区被列为可先行，不被Plan/大重构阻塞。当前有限审查未发现新增owner冲突或需要阻止交付的取舍矛盾。07/08仍待落盘后做相同范围读回，不扩成全系统验证。

### IR-04 · 工作包的资源与身份前置条件（已关闭）

**挑战：** 07初稿W10包含Plan适配和完整ExecutionBinding，但未把后者对W05统一ResourceScope的依赖写明；W12的“各自前置合同”也不足以确保在途合并使用可证明的材料身份，而不是选中账号/旧meta。前者可能临时增设第二close owner，后者可能共享不等价认证上下文。

**对照与裁决：** [05资源合同](../05-context-and-runtime.md)限定Binding借用资源；IR-01源码证据已经表明当前meta不能独立证明复制字节身份。故要求明确依赖，而不是额外创造文件协议与Plan之间的全局依赖。

**2026-09-19读回通过：** [07工作包](../07-roadmap-and-work-orders.md)现将W10分为可先行的Plan适配、另须W05的完整binding；W12在途合并显式依W03 settled/view_epoch/cache_epoch、I05实际runtime身份及C01可证明的材料身份，unknown独立执行不合并。[02 I03](../02-intake-and-preflight.md)同步相同约束，匿名仍须隔离工具/网络/解析上下文。W08文件协议不依赖Plan，W10不依赖新journal；W09是文件/DB协议汇合点。**IR-04已关闭（排期合同补齐），未据此声称代码已实现。**

### 07/08有限覆盖验收

已读[07](../07-roadmap-and-work-orders.md)及[08](../08-validation-and-benchmarks.md)：工作包均未开始；I01–05/E01–06/A01–06/C01–04可映射T01–20，E04按接入策略复用T01/T02/T09/T10；B01–06覆盖准入、合并、runner、writer、文件与预检成本。TTL0未改变；时间/内存阈值待基线，30次与8ms没有被包装成统计充分性或硬实时保证。共享源码有唯一实施owner与协作窗口，不存在要求两个代理同时持有workers.py的排期合同。

06新增的needs_reconciliation仅为内部交付状态；未确认整组交付使用现有failed并附待核对reason、禁止自动重下载，已确认整组交付保持success，终态映射归E01/A01共同评审。这与03终态唯一裁决及04文件事实优先的设计一致，本轮未发现新增终态owner冲突。

**关闭结论：** 本报告IR-01–04必要修订均已读回关闭；有限独立审查没有剩余阻止文档交付的问题。仅复核源码事实、设计合同与测试映射，未执行产品、测试、性能实验或真实材料读取；实施后的并发、进程与持久性保证仍须按08取得对应层级证据。
