# 新版架构 V2 · 独立复核：部署、依赖与模块保证边界

复核人：Runtime 调查代理；2026-09-19。此次对 Repository 代理的 01/08/12、VS-16–20，以及 Lead 的 00/02/13/14 做反向源码抽查。没有执行应用、维护脚本、网络、构建或业务测试；结论层次为 source/static，不是运行或线上验收。本文以重要保证的独立核对为范围，不冒充逐行审计或完整安全审计。

## 1. 已反馈并读回关闭的文字修订

| 项目 | 实际代码 | 修订与复核结果 |
|---|---|---|
| 组件“FFmpeg必有ffprobe”范围 | [CONFIRMED/static] [resolve_exe 220行](../../../src/fluentytdl/core/dependency_manager.py#L220) 发现managed主EXE即返回；[230行](../../../src/fluentytdl/core/dependency_manager.py#L230) 的extra_exes检查仅在PATH分支 | 原VS-17概括过宽；作者已改为managed主EXE存在即返回，仅PATH检查配套。[VS-17](../vertical-slices/VS-17-component-update.md) 已读回，关闭。意义：候选路径存在与完整工具组合健康不同。 |
| 公告分发失败不等于KV未写成 | [CONFIRMED/static] [rebuild 8行](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L8) try含KV put和D1 state；catch中的state本身也可能失败 | 作者已改[VS-18](../vertical-slices/VS-18-announcements.md)：新KV可能已可见，且失败记录/预期503不保证。已读回，关闭。不能以错误码推导跨存储回滚。 |
| 同步失败总保留旧快照 | [CONFIRMED/static] [updates 74行](../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74) KV先成功后D1可能失败，catch无恢复KV操作 | [VS-20](../vertical-slices/VS-20-build-release.md) 作者已修为KV写入前失败通常保留旧快照，写入后D1失败保留新快照；错误状态再次写失败也独立列出。已读回，关闭。 |

## 2. 核心保证的独立核对

| 被审断言 | 回查源码与判断 | 保证上限 |
|---|---|---|
| 应用自动启动完全失败仍提交 | [CONFIRMED/static] [updater 1663行](../../../src/fluentytdl/core/updater.py#L1663) 无launch_mode/PID时调用_commit_update、提示手动启动、自更新updater并返回1；[VS-16](../vertical-slices/VS-16-app-update.md)、[12](../12-deployment.md) 与之吻合 | 非零退出不能推出旧版恢复；新版磁盘提交与新版进程启动是不同结果。[UNKNOWN] 实机恢复可用性。 |
| READY与survival不是相同监护 | [CONFIRMED/static] [decide_watch_outcome 1147行](../../../src/fluentytdl/core/updater.py#L1147) READY校验PID+token；[1162行](../../../src/fluentytdl/core/updater.py#L1162) survival先判elapsed，再判alive | VS-16正确指出宽限到达分支不是最终再次证明存活；匹配READY也不能证明媒体下载。 |
| psutil缺失扩大组件停止范围 | [CONFIRMED/static] [kill_locking_processes 52行](../../../src/fluentytdl/core/updater_worker.py#L52) 缺psutil执行taskkill /F /IM；可用时匹配完整路径/打开文件 | VS-17/13准确。不能把ProcessManager的owner约束推广至该降级分支；正常psutil路径也不是仅限本进程后代，而是目标文件路径使用者。 |
| managed安装目标与有效runtime分离 | [CONFIRMED/static] [get_exe_path 191行](../../../src/fluentytdl/core/dependency_manager.py#L191)、[resolve_exe 197行](../../../src/fluentytdl/core/dependency_manager.py#L197)、[install_component 355行](../../../src/fluentytdl/core/dependency_manager.py#L355) 分别负责目标和候选身份 | 08/VS-17把职责拆开正确；安装成功仍可能继续选custom/PATH工具，不可直接宣称运行版本已切换。 |
| 组件hash与多文件安装边界 | [CONFIRMED/static] [Transport.download 222行](../../../src/fluentytdl/core/update_transport.py#L222) 仅sha256非空时比较；[handle_zip 129行](../../../src/fluentytdl/core/updater_worker.py#L129) 逐成员safe_install，附属成员缺失可跳过 | VS-17正确区分可选hash校验及非整组原子更新；done被忽略，[QProcess finished 982行](../../../src/fluentytdl/core/dependency_manager.py#L982) 才按退出状态/错误发成功，且成功条件没有“必须先收到done”。 |
| 公共端点不回源D1/GitHub | [CONFIRMED/static] [cached 29行](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L29) 只读KV并检查新鲜度/ETag；[44行](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L44) health甚至不读KV，其他public分支走cached | 00/01/12/VS-18主张正确；“public读KV”是元数据端点的简写，不是health调用事实。 |
| D1发布与KV快照分阶段 | [CONFIRMED/static] [announcements 36行](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L36) D1 batch之后[46行](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L46)调用rebuild；不含跨KV原子提交 | D1 batch是源码调用事实；本轮未运行云端事务/故障语义。12/VS-18已将保存、公开可见与状态记录区分。 |
| operation_lock租约不是无限互斥 | [CONFIRMED/static] [locked 22行](../../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22) 240秒租约，token条件释放，无续租/对后续写入fencing | [INFERRED] action超过租约且有另一次同名调用可重叠。01/12/13/VS-18/20没有将该条件性风险冒充线上已发生事故，表述可保留。 |
| 构建目标与公开字节闭环 | [CONFIRMED/static] [TARGET_OUTPUTS 77行](../../../scripts/build.py#L77)、[spec提前返回 818行](../../../scripts/build.py#L818)、[validate_result 66行](../../../scripts/release_pipeline.py#L66)、[publish 175行](../../../scripts/release_pipeline.py#L175) | 01/12/VS-20与实际目标集、hash/size重验、draft后public下载验证、stable latest验证吻合。实现存在不等于本轮执行成功；原文已区分。 |
| 安装维护与组件kill不同 | [CONFIRMED/static] [maintenance Stop 35行](../../../installer/maintenance.ps1#L35) 路径匹配主进程与后代、CreationDate重验；[Clean名单 205行](../../../installer/maintenance.ps1#L205) 不含announcements.sqlite3 | VS-19正确限定Stop；公告DB缺口是清单对比，没有夸大成实际已验证隐私残留。Clean失败可已局部删除，不是事务。 |

## 3. Lead 00/02/13/14 与新增 R25–29

[CONFIRMED/static] 总览和模块图已区分Qt信号与直接方法、TaskDBWriter实际Thread、六入口三执行、live pause同run、after_fix增加attempt、Staging成功边界及强停限制；未将NetworkWatchdog/DiskMonitor的类存在写成生产启动。代码索引是概念入口与文件引用映射，不冒充全量运行调用图。该部分抽查未发现需要阻断发布的过强主张。

以下风险的**源码触发结构已复核**，实际发生概率和运行结果仍为[UNKNOWN]：

| 风险 | 源码依据与结论 |
|---|---|
| R25 journal写失败仍继续提交 | [CONFIRMED/static] [_write_journal 1636行](../../../src/fluentytdl/download/staging.py#L1636) OSError只signal；[commit 1298行](../../../src/fluentytdl/download/staging.py#L1298) 随后继续reserve/publish。把write-ahead顺序当作必落盘准入保证是错误的；13现已写成条件性恢复材料风险。 |
| R26 未知/损坏journal默认删除 | [CONFIRMED/static] [_read_journal 1864行](../../../src/fluentytdl/download/staging.py#L1864) 损坏/不可读/非dict→None；[GC 1807行](../../../src/fluentytdl/download/staging.py#L1807) 只特判两种phase，其他在年龄/live门后[1845行](../../../src/fluentytdl/download/staging.py#L1845) rmtree。13明确未复现损失，表述准确。 |
| R27 快速通道先诊断再成功仲裁 | [CONFIRMED/static] [轻量2310行](../../../src/fluentytdl/download/workers.py#L2310)、[封面2503行](../../../src/fluentytdl/download/workers.py#L2503) 先强制error/diagnose再调用_fastpath_fail判断committed。[INFERRED] postcommit异常可保留success但多发失败诊断/短暂错误状态；13使用条件性语气准确。 |
| R28 EOF后取消可能被非零rc覆盖 | [CONFIRMED/static] [轻量2251行](../../../src/fluentytdl/download/workers.py#L2251)、[封面2464行](../../../src/fluentytdl/download/workers.py#L2464) EOF后wait→rc判定前没有统一取消优先门。[INFERRED] 静默子进程取消可能归failed；不能写成所有取消都错分。 |
| R29 attempt返回与postprocess门之间取消 | [CONFIRMED/static] [1528行](../../../src/fluentytdl/download/workers.py#L1528) is_cancelled可跳过整个后处理；[_finish_run 1123行](../../../src/fluentytdl/download/workers.py#L1123) 未设outcome发signal并落failed。[INFERRED] 指定交错可形成unset记录；13没有冒充运行复现。 |

## 4. 对本代理文档的反向反馈及覆盖限制

Repository独立复核提出正常DBWriter批次上限与shutdown drain不同、单项吞错不保证batch回滚、GC默认分支及journal失败条件。已经回查[db_writer 98行](../../../src/fluentytdl/storage/db_writer.py#L98)/[116行](../../../src/fluentytdl/storage/db_writer.py#L116)/[166行](../../../src/fluentytdl/storage/db_writer.py#L166)与上述Staging代码，并修正03/05/09/10/VS-11。Lead要求的跨章POT、组件watchdog、updater监护、runfile/log/Staging清理、公告timer入口导航已补[05](../05-control-flow.md)。

[UNKNOWN] 未核实最终冻结包依赖是否齐全、psutil是否在实际发行物中可导入、云端bindings/migrations是否部署、KV传播时延、Windows进程句柄和Qt动态线程归属、文件系统故障实际结果。本报告不替代这些接受证据。

[RECOMMENDATION] 最终发布文档前由Lead运行仅文档范围的链接/源码快照检查并登记证据等级；若后续业务修复，按这些最小条件做隔离故障注入，再更新保证表。当前未发现仍待作者修订的高影响文字错误；“未发现”仅限本报告抽查范围。
