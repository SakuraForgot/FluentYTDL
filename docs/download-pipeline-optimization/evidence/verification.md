# 调查、设计反证与交付验证

2026-09-19。证据等级：源码/测试文本回查、设计相容性复核、文档机械检查。**不包含业务代码实现、产品测试、构建、真实下载、性能实测或部署结果。** 外部访问仅用于核对Python、Qt、SQLite和Windows官方技术资料；没有访问账号、真实Cookie、任务库或用户日志。

## 1. 基线及范围

[baseline.json](baseline.json)对V2库存的366个源码/配置/测试文件重新计算哈希，与架构取证时相比`architecture_drift=[]`。另冻结V2的43份Markdown和现有metadata方案3份Markdown，共412文件。Git HEAD仍为`c441f2cde2e7973fe5902da50358f25c37c6525f`；dirty状态原样记录，既有业务/翻译/规格不属于本次修改。

本轮写入范围仅`docs/download-pipeline-optimization/`。此目录的提案不能被当作V2现状：新DownloadPlan、RunControl、各类Receipt等都是候选设计。只在未来实际实施验收后更新V2。

## 2. 分工与反向复核

| 作者 | 分析责任 | 独立复核 |
| --- | --- | --- |
| Lead | 目标、阶段链、认证/POT/日志、总裁决、路线与验收、工具 | 读回各章源码依据与接口owner，整合跨章依赖；不把自己的检查标成独立审查 |
| Repository subagent | 02入口/解析/准入/缓存/runtime：I01–I05 | [review-intake](review-intake.md)：00/01/03/05及06–08关键协议、依赖与匿名语义 |
| Runtime subagent | 03执行/控制/暂停/重试：E01–E06 | [review-commit-protocol](review-commit-protocol.md)：04发布后失败、receipt owner、新根与跨卷 |
| Resource subagent | 04产物/恢复/DB：A01–A06 | [review-resources](review-resources.md)：03取消线性化、05材料副本同代与资源owner |

独立报告仅对列出的主张通过，不是全量形式化验证或漏洞审计。已经修订的设计仍需后续运行验收。

## 3. 相比架构书新增的分析价值

| 本轮进一步追到的机制 | 对优化选择的影响 |
| --- | --- |
| 默认解析TTL为0；手动clear没有递增generation | 先修失效语义，保留默认关闭；不能声称已有缓存带来某百分比提速 |
| URL→单row、静默取消与active释放依赖结果信号 | UI view_epoch和cache_epoch不能共用；独立settled释放与UI错误展示分离 |
| 批量命名反复listdir、前置避名不是最终文件占有 | 批次索引只优化建议名；O_EXCL/整组命名继续归Staging |
| runtime probe持全局锁执行外部进程 | 按identity合并probe，锁仅保护注册；先测调用成本，不宣称已复现UI卡顿 |
| 部分reserve占位可能尚未进入area记录即抛异常 | A03要求逐项归属登记，注入第二成员异常验证；不能仅靠外层compensate推导全回滚 |
| 最后成员已发布、其published记录失败，与最终committed记录失败是两个窗口 | A01两者均按实际确认的整组交付裁决，不能用所在try代码位置替代文件事实 |
| 测试中跨卷多为mock、writer测试多绕开后台线程 | 将“已有测试资产”与真实Windows/Qt/退出/断电证据分开 |

详细源码行号均在02–05；[source-references.json](source-references.json)提供本目录实际引用反向索引，不假称覆盖全部符号。

Lead另行反查02所述TTL默认0、手动invalidate仅clear、URL到单行映射、controller重复listdir、runtime全局锁内probe，以及04占位局部created到area登记的时序，确认这些限定与当前源码吻合。这里确认的是结构及条件性风险，没有复现卡顿、资源残留或文件损失。

## 4. 设计审查中实际改正的内容

| 发现 | 原方案不足 | 修订结果 |
| --- | --- | --- |
| CP-01 | 只显式保护最终committed journal失败，可能漏最后成员published检查点失败 | 04区分最后成员确认与部分发布；03 E01读取Staging封存交付事实，不按落后磁盘phase误回滚 |
| CP-02 | finalize_delivery名称易被实现成第二个终态出口 | 04明确只移交receipt/DB ack；worker finally与manager仍是既有事件owner |
| CP-03 | 避旧GC的新根可能被误实现到系统盘导致默认跨卷 | 新根优先在各download_dir下不同受控路径；实际卷/reparse另验，隔离不自动跨卷删源 |
| RR-01 | RunControl与旧cancel Event若异步同步，commit gate可能越过已接受取消 | 03要求同一永久谓词直接被gate/进程登记/等待读取；Qt仅通知 |
| RR-02 / IR-01 | 身份信息与runfile复制分离会绑定不同代材料；旧meta合法不代表一致 | 05要求共享每平台发布/receipt/复制同步协议，跨进程一致；bytes/meta结果分开、未知明确处理 |
| IR-02 | ExecutionBinding.owned_runfile可能制造两个清理owner | 改借用resource_id，唯一close归C02/E03 scope |
| IR-03 | POT等待改造可能被实现为超时静默绕过或取消触发共享重启 | 05保留enabled fail-closed，ReadyResult.cancelled独立传播 |
| 路线依赖 | 完整binding和在途请求合并可能先于资源scope/可靠身份实施 | 07补W05与W10完整binding依赖；合并要求settled/epoch/工具/材料身份明确，unknown独立执行 |
| 不确定交付的后续动作 | 仅写待核对文案不能阻止旧重试入口重复下载，后来确认也可能重复发outcome | 06/04要求持久标记与所有重试/恢复门，T17加入验收；后来确认只修历史并signal，不改原run outcome |

各项修订及关闭读回记录保存在对应独立报告。没有通过增加一个抽象名称就宣称安全问题已经修好。

## 5. 文档检查方法

```powershell
.venv/Scripts/python.exe docs/download-pipeline-optimization/tools/verify.py verify
```

该工具只读产品文件，检查本目录Markdown链接目标、显式行号范围、基础标题锚点、围栏与412个基线哈希；生成[checks.json](checks.json)和反向引用。首次`capture`已完成，再次capture会拒绝覆盖，避免用新快照隐藏漂移。

最终检查要求failures为空。它不检测未列入基线的新文件、不证明引用语义完全准确、不执行Mermaid渲染、不执行产品测试或性能测量。外部网页引用的内容由本轮工具读取，不由本地链接检查器验证。

本次最终检查记录：15份Markdown、337个本地链接、4个Mermaid图块、412个冻结文件哈希，引用38个源码/测试文件，failures为空。检查计数以checks.json为准；旧架构V2与本轮开始时的业务源码基线未变化。

## 6. 可交付结论

已形成目标G01–G09、12个概念链路阶段、21项有备选方案的建议、W00–W12实施工作包、T01–T20验收矩阵与B01–B06测量设计。推荐顺序与理由已给出，不用未测量的性能承诺填补证据缺口。

后续是否采用某候选，以工作包的行为、兼容和测量门为准。当前交付是可审阅、可拆任务的分析方案；并非宣称找到了所有情境下的全局最优实现。
