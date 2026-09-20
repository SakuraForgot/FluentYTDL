# 独立复核 · 提交协议、恢复根与终态归属

2026-09-19，复核者为执行/控制章作者，未撰写被审的 [04-artifacts-and-recovery](../04-artifacts-and-recovery.md)。本次只读源码、测试正文和设计文档；没有运行测试、故障注入、产品、构建、网络或真实文件删除。复核结论是设计边界和源码对应关系，不是协议已经实现或运行通过。

`[CONFIRMED/static]` 为现有源码/测试事实；`[INFERRED]` 为风险推导；`[RECOMMENDATION]` 为拟实施约束；`[UNKNOWN]` 为缺少运行证据。评审范围严格限于 A01/A02/A04/A05 的提交/回退安全及与 E01/E02 的接口，不扩为全项目审计。

## 1. 总体结论

[RECOMMENDATION] 04所选的分阶段 journal 确认、未知现场保护、新协议根隔离、异步数据库回执与交付对账方向可作为实施设计基线。其价值在于保持“记录失败不等于文件交付失败”和“无法确认无副作用不等于可删”，而不是增加一个更大的状态机。

不接受把 `_write_journal` 的 catch 直接改为统一 raise 后复用全部现有补偿，也不接受仅增加 version/phase 后声称旧版本安全。这两个反例已经在04明确拒绝。本次三项必要补充均由作者修订后读回关闭，见下表；尚无本次范围内未关闭的文字问题。

## 2. 必要修订及关闭证据

| ID | 原设计需补的边界 | 独立源证据与推导 | 读回结果 |
|---|---|---|---|
| CP-01 最后成员 checkpoint | “部分发布写失败”与“最终committed记录失败”之间，需要单列最后成员已物理发布、内存指纹确认，但该成员published记录失败 | `[CONFIRMED/static]` [1394–1398](../../../src/fluentytdl/download/staging.py#L1394) 的成员成功登记仍在_publish_group中；[1300–1304](../../../src/fluentytdl/download/staging.py#L1300)外层catch会调用补偿。`[INFERRED]` 若统一抛journal错误，将可能撤回实际已全组交付的文件，和成功优先目标相悖 | **关闭。** [04 A01阶段表](../04-artifacts-and-recovery.md#L54)明确全组已确认交付时不再部分补偿；执行/归属不确定仍needs_reconciliation。验收分开最后member检查点与最终committed检查点两处，不以现有postcommit测试代替。 |
| CP-02 receipt不拥有run终态 | finalize_delivery命名容易被实施者理解成另一终态/事件生产者 | `[CONFIRMED/static]` [Worker finally收口](../../../src/fluentytdl/download/workers.py#L1123)、[manager状态桥](../../../src/fluentytdl/download/download_manager.py#L455)与拟议E01各有职责；DB ack可能迟于文件成功 | **关闭。** [04 A05](../04-artifacts-and-recovery.md#L204)明确只持久化/移交receipt与DB ack，E01读取交付结果，现有worker/manager出口不增加producer，迟到ack只改变记录同步展示。 |
| CP-03 新根不是默认跨卷搬迁 | 避开旧GC的新私有根若被实施为系统盘统一缓存，会改变原本常见同卷交付的空间/失败模型 | `[CONFIRMED/static]` [create 757行](../../../src/fluentytdl/download/staging.py#L757)在download_dir下建固定根；[GC 1770行](../../../src/fluentytdl/download/staging.py#L1770)扫描同一固定根。只换受控根名即可隔离旧枚举，不必换卷。[跨卷1403行](../../../src/fluentytdl/download/staging.py#L1403)具有额外copy/校验/临时文件边界 | **关闭。** [04 A02](../04-artifacts-and-recovery.md#L100)明确优先各download_dir下新受控根，实际卷身份/无reparse验证；确实跨卷显式走A03并验空间与复制故障，未知现场不自动跨卷隔离后删源。 |

## 3. 已独立核对的实现边界

| 主题 | 源码/测试核对 | 对提案的约束 |
|---|---|---|
| journal当前不是硬准入门 | `[CONFIRMED/static]` [_write_journal 1630行](../../../src/fluentytdl/download/staging.py#L1630)捕获OSError只signal；[commit 1298行](../../../src/fluentytdl/download/staging.py#L1298)随后reserve/publish；[1316行](../../../src/fluentytdl/download/staging.py#L1316)最终phase写入在补偿try之外 | A01必须区分“无外部副作用”“部分发布”“全部确认交付”，不能以函数异常位置自动代表文件结果；记录耐久需单独证据。 |
| 部分占位归属窗口 | `[CONFIRMED/static]` [_reserve_group 1340行](../../../src/fluentytdl/download/staging.py#L1340)先记局部created，非EEXIST/EACCES异常立即抛；[1364行](../../../src/fluentytdl/download/staging.py#L1364)整组后才转area | A03逐项登记和失败finally有实际依据；现有错误不是已复现的残留事故，文中保持推断口径正确。 |
| 补偿只处理可核对归属 | `[CONFIRMED/static]` [_compensate 1438行](../../../src/fluentytdl/download/staging.py#L1438)检查published和fingerprint，失败保留；[1476行](../../../src/fluentytdl/download/staging.py#L1476)完成实际补偿后才改安全phase | 新结果类型不可把uncertain压成rolled_back；不确定目标不盲删，新版本回退不写假的prepared。 |
| 旧GC不理解新version | `[CONFIRMED/static]` [1807行](../../../src/fluentytdl/download/staging.py#L1807)只分committing/rollback_failed；[_read_journal 1864行](../../../src/fluentytdl/download/staging.py#L1864)坏/不可读/非dict为None；[1845行](../../../src/fluentytdl/download/staging.py#L1845)默认删除 | A02版本根隔离是必要回滚设计；同名旧根中写version=2或unknown phase不足以保护现场。旧v1迁移也须有独占/身份证据。 |
| DB ack与回执不是进度更新同义词 | `[CONFIRMED/static]` [DBWriter 166行](../../../src/fluentytdl/storage/db_writer.py#L166)吞单项异常；[74行](../../../src/fluentytdl/storage/db_writer.py#L74)join限时不回传成功；[116行](../../../src/fluentytdl/storage/db_writer.py#L116)drain不限普通batch容量 | A04 admission/commit receipt/封口分离合理，不能仅延长join或把线程改non-daemon就称已排空；异步Qt桥不能被主线程阻塞等待自己消费。 |
| 现有测试主要证明局部性质 | `[CONFIRMED/static]` [journal顺序856行](../../../tests/test_download_staging.py#L856)、[补偿安全顺序1604行](../../../tests/test_download_staging.py#L1604)、[postcommit1532行](../../../tests/test_download_staging.py#L1532)分别验证顺序与当前Staging行为；[writer fixture78行](../../../tests/test_db_writer_batch.py#L78)绕过后台线程init | 04没有把这些测试资产称作新journal协议、真实断电、Qt→DB→文件对账或线程退出通过；仍需新增实际故障窗口测试。 |

## 4. 反向反馈：E02与Staging gate共用取消谓词

Resource对[03 E02](../03-execution-and-control.md)提出新的cancel_requested若仅通过Qt信号晚更新旧Event，会破坏E01取消顺序。独立回查：[标准Staging create](../../../src/fluentytdl/download/workers.py#L1248)与[fastpath create](../../../src/fluentytdl/download/workers.py#L1848)目前均读取_cancel_event.is_set，而Executor回调还读取is_cancelled。

[RECOMMENDATION] 03已补明确合同：cancel接受同步更新同一个永久谓词；Staging gate、进程登记、所有等待都直接读取该谓词，旧Event只能作为同源适配。UI信号仅通知，不能承担两个取消状态之间的延迟复制。此项已在本代理授权文件修改，不修改04或业务。

## 5. 未被此次复核证明的内容

[UNKNOWN] 新协议尚未实现；没有测试/构建/性能或真实卷结果。fsync/目录元数据/磁盘缓存耐久、跨进程owner锁、Windows链接竞态、旧版真实扫描行为、新根卸载发现、多卷空间压力、Qt在途消息和DB故障回执均需后续隔离验收。

[RECOMMENDATION] 实施时将CP-01两种最终记录失败、CP-03新根回退旧版/再升级、receipt重复认领和取消门同源纳入验收矩阵；以文件事实和状态证据共同裁决。图和接口建议不能提升源码尚未达到的保证，也不能据此自动授权业务变更。
