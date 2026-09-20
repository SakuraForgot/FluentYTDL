# 下载流程优化方案 · 02 入口、解析与执行前准备

2026-09-19。以新版架构 V2 为索引，重新回查当前源码；本章仅为方案，没有修改业务、运行产品/测试/构建、访问网络或读取真实配置/凭据/任务数据。已读取根 AGENTS.md；当前工作树包含既有源码、翻译、规格及文档改动，不能声称 clean，也不把其归为本方案实施成果。

证据标签：**[CONFIRMED/static]** 为源码存在的行为；**[INFERRED]** 为由代码推导的问题或成本；**[UNKNOWN]** 为未测量；**[RECOMMENDATION]** 为全部新设计、迁移和验收要求。本章的类型名称是建议接口，不表示已经存在。先修正任务语义、终结和失效边界，再根据测量优化性能；不设虚构提速百分比。

## 1. 当前入口并不是六套独立执行器

| 用户动作 | 当前解析与任务生成 | 执行边界及不能丢掉的差异 |
| --- | --- | --- |
| 视频 | 主窗口→配置窗→InfoExtractWorker→Service→CLI；选择格式后生成 tuple | 标准媒体任务；手选格式、音频语言和画质预检属于用户意图。 |
| VR | VRInfoExtractWorker，纯列表可先 flat，单视频 android_vr 解析 | VR格式来源、投影/转换政策不能只压成一个普通format字符串。 |
| 频道 | 优先识别频道；ChannelExtractWorker最多3个tab并发，回到列表 | tab的unsupported/empty/failed不同；一个tab失败不是所有下载任务失败。 |
| Playlist | flat entries→按视口/点击/后台crawl提取详情→逐项选择 | 行身份、重复URL和父列表顺序不能用URL唯一键替代。 |
| 字幕 | 同样先解析/选择，生成skip_download并关闭无关媒体后处理 | 轻量CLI通道；不能把媒体输出路径/质量门一律套到字幕。 |
| 封面 | mode=cover不读解析缓存；选图片URL时cover_direct，否则skip_download+writethumbnail | 图片临时URL具有时效，不能只保存图片URL而丢失来源视频与选择依据。 |
| QuickAdd | 独立QuickAddWorker逐URL解析，然后回controller创建任务 | 不属于上面四类解析worker的统一取消协议；当前single_worker意图与下游noplaylist覆盖冲突。 |

表为 **[CONFIRMED/static]**；入口与模式证据：[start_extraction](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1354)、[get_selected_tasks](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4003)、[VR解析](../../src/fluentytdl/youtube/youtube_service.py#L1963)、[Channel pool](../../src/fluentytdl/download/workers.py#L360)、[QuickAddWorker.run](../../src/fluentytdl/core/quick_add_worker.py#L51)。关系说明见 [V2 运行架构](../architecture-v2/03-runtime-architecture.md) 与 [VS-08](../architecture-v2/vertical-slices/VS-08-quick-audio-section.md)。

**[CONFIRMED/static]** 当前共享边界是 `(title,url,opts,thumb)`，controller会改路径/标题/模板并立即创建、启动worker；worker再把当时基础配置与任务opts合并并强制noplaylist。不是一个已经冻结的执行计划。[controller.handle_add_tasks](../../src/fluentytdl/core/controller.py#L95)、[Worker.run](../../src/fluentytdl/download/workers.py#L1174)

## 2. 建议的稳定边界与字段归属

**[RECOMMENDATION]** 使用四个不同寿命的对象，避免“冻结所有参数”与“每次都读取全局配置”两种极端。目的：确保用户选的内容不被排队时间改变，同时在真正执行时获取新鲜运行资源。范围：从入口到下载调度准入；箭头是建议的数据交接，不代表新类已经实现。

```mermaid
flowchart LR
  UI[六模式 / QuickAdd 意图] --> IN[DownloadIntent]
  IN --> P[ParseRequest 与 ParseSnapshot]
  P --> CH[选择 / 预检 / 明确降级确认]
  CH --> DP[有版本的 DownloadPlan]
  DP --> AD[准入与任务持久化]
  AD --> RS[运行前绑定 RunSpec]
  RS --> EX[现有三执行通道与 Staging]
```

| 建议对象/字段 | 冻结点与寿命 | 明确不包含 / 处理缘由 |
| --- | --- | --- |
| **[RECOMMENDATION]** DownloadIntent | 输入时：original/source URL、平台、请求模式、用户内容类型、父操作flow | 不把URL规范化失败隐式替换成另一内容；t.co展开属于有超时/取消的请求，不是假装纯CPU验证。 |
| **[RECOMMENDATION]** ParseRequest/ParseSnapshot | request_id、view_epoch、auth/cache环境代次、解析类型、实际runtime指纹、started/finished、结果或结构化失败 | 原始info只作当前解析资料；不持久化Cookie字节、媒体签名URL集合。Preview资料不能冒充新格式结果。 |
| **[RECOMMENDATION]** DownloadPlan | 确认时：schema_version、plan_revision、source媒体身份、父/子项标识、执行通道、format/音轨/字幕/VR/片段等语义、COOKIE_MODE、相对命名意图、目标目录、策略版本、显式值来源 | 不包括PID、txn路径、runfile、POT端口、当时媒体直链。metadata字段引用既有规格的policy，不另造一套同义Policy。 |
| **[RECOMMENDATION]** RunSpec | 每次run准入后绑定：已选runtime身份、网络配置快照、有效credential引用、能力检查结果；attempt可按恢复政策重新绑定短期材料 | 不因刷新凭据/工具而重写用户格式/匿名意图；实际Staging路径仍由事务owner创建。生命周期由执行/资源章承接。 |

**[CONFIRMED/static]** UrlRouter已有 `UrlProcessResult`，包含原URL/规范化URL/平台/类型/accepted/cookie_platform；`process()`可能展开t.co，默认请求timeout3秒。[结果结构](../../src/fluentytdl/utils/url_router.py#L34)、[process](../../src/fluentytdl/utils/url_router.py#L202)、[展开](../../src/fluentytdl/utils/url_router.py#L359)。**[RECOMMENDATION]** 复用它的含义作为入口适配器，保留视频URL中的内容选择参数，不盲目删除list/v等有语义参数。未知平台是否允许交给通用yt-dlp，沿用现有产品准入规则，不借本次优化扩大支持面。

**[CONFIRMED/static]** `specs/media-metadata` 是另项设计而非本章发现的已实现模块；其中已规定 `__fluentytdl_metadata_policy`、schema_version和显式False优先。[既有设计 §3](../../specs/media-metadata/design.md#L59)。**[RECOMMENDATION]** DownloadPlan容纳该policy及其策略版本，复用其冻结/迁移入口；本章不重开容器writer选型、不改变metadata最终处理顺序，也不提前执行该规格。

## I01 · 从可变 options tuple 迁移到语义计划及有来源的覆盖

### 现状、缘由和不足

**[CONFIRMED/static]** 字幕入口强制清理无关PP，封面入口可clear opts后换成图片URL；视频/VR入口再叠加选择器extra_opts。controller修改传入dict并固化title到outtmpl；create_worker只做浅拷贝，补COOKIE_MODE并入DB；Worker运行时deepcopy基础与任务opts后update，随后Feature还会配置。证据：[模式分支](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4023)、[controller覆盖](../../src/fluentytdl/core/controller.py#L145)、[manager冻结现状](../../src/fluentytdl/download/download_manager.py#L403)、[执行合并](../../src/fluentytdl/download/workers.py#L1174)。

**[INFERRED]** 字典适应多模式、特殊引擎参数和旧任务，入口强制清理是为防模式串线；代价是“缺省”“明确False”“模式禁止”“后处理补入”没有统一解释顺序。稳定DownloadPlan的价值主要是阻止语义漂移和提供可审计变更点，不能把它本身当作性能提升。

### 方案比较与推荐

| 方案 | 收益 / 代价 / 裁决 |
| --- | --- |
| **[RECOMMENDATION] A 保持tuple/dict，仅加分支回归** | 最低迁移成本，适合紧急修一个覆盖问题；没有集中检查点，每个新入口继续重复决策。可作短期回滚路径，不作长期唯一接口。 |
| **[RECOMMENDATION] B 语义Plan + 旧opts适配器** | 推荐。UI只输出明确意图；单一compiler产生三通道可消费opts，并保留字段来源/修正规则。新增模式需要更新计划及compiler契约，前期成本较高。 |
| **[RECOMMENDATION] C 排队时冻结完整argv/全部info** | 不选。Cookie/runfile/POT/签名URL有短寿命；运行路径和工具更新也需重新绑定。看似绝对确定，却易制造过期地址和敏感材料持久化。 |

**[RECOMMENDATION]** precedence写成可检查规则：已有受支持版本的冻结policy→任务显式值（包括False/空选择）→确认时全局默认；之后模式安全约束和容器兼容转换必须返回decision及原因，而非无声覆盖。显式禁止与所选能力冲突时在确认前给一次可解释结果；不要把所有矛盾一律“最后一个dict.update胜出”。外部工具参数仅由compiler输出，内部plan字段不得泄漏成CLI选项。

**[RECOMMENDATION]** 元数据遵循已有policy设计；字幕语言冻结用户偏好而实际caption key允许执行前解析；VR保存格式来源；封面保存source URL、候选标识和选择依据，临时图片URL放runtime material。封面候选失效后可重新解析相同source并重新匹配，不能偷偷换成任意封面；匹配不到明确失败/再次选择。

### 迁移、回滚、测试与验收

**[RECOMMENDATION]** 先让compiler在测试夹具和不触发下载的影子比较中输出计划，不改变现有执行；按普通→字幕/封面→VR/列表/QuickAdd逐入口接入。首期仅持久化可由旧opts等价表达的version1语义，同时保留旧字段；有未知schema必须停止该任务的自动解释，保留原JSON并给不兼容提示。回滚只切回旧入口adapter，不删除新字段；若后续加入旧版本无法表达的语义，则必须先增加显式降级工具，不能承诺旧程序可无损执行。

**[CONFIRMED/static]** 现有资产：[Cookie任务mode roundtrip](../../tests/test_youtube_cookie_mode.py#L206)、[现有worker保留mode](../../tests/test_youtube_cookie_mode.py#L79)、[片段options](../../tests/test_section_download.py#L35)。**[RECOMMENDATION]** 新增纯compiler测试（建议文件 `tests/test_download_plan.py`，目前不是既有资产）：六模式×明确True/False/缺省、旧任务迁移、未知schema、format来源、恢复/重试同policy、两次编译不修改输入。验收为等价场景生成相同语义CLI、所有有意差异有decision与用户确认依据；不以“字段都复制了”代替行为验收。

## I02 · 解析终结与迟到回调采用独立身份

### 现状、缘由和不足

**[CONFIRMED/static]** 窗口重新解析先cancel旧worker，随后把新worker信号接到同一成功/失败槽；该连接段没有view generation参数。Scheduler以URL作为task_id，维护URL→单row，reset清映射；AsyncExtractManager只有成功/错误信号清active，EntryDetailWorker取消直接return。证据：[窗口重解析](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1354)、[URL映射](../../src/fluentytdl/ui/playlist_scheduler.py#L297)、[reset](../../src/fluentytdl/ui/playlist_scheduler.py#L128)、[manager终结](../../src/fluentytdl/download/extract_manager.py#L96)、[取消return](../../src/fluentytdl/download/workers.py#L579)。QuickAddWorker没有cancel Event，串行处理全部URL。[QuickAdd](../../src/fluentytdl/core/quick_add_worker.py#L51)

**[INFERRED]** URL键原为避免row重排误投，取消静默是避免用户主动操作制造错误弹窗；但稳定URL仍不能唯一标识重复行和新旧解析请求。静默UI取消与内部释放绑定在一起，可留下容量；Cookie generation防缓存回填不能代替UI generation。

### 方案比较与推荐

| 方案 | 收益 / 代价 / 裁决 |
| --- | --- |
| **[RECOMMENDATION] A 保持现状，关闭/重试就销毁重建manager** | 改动较少；资源取消完成、Qt对象寿命与重复URL仍未闭环，也不解决单窗重解析迟到结果。只作为迁移期间隔离手段。 |
| **[RECOMMENDATION] B request_id + view_epoch + 独立settled** | 推荐。每个实际解析调用有唯一ID，行消费方另有entry_id/view_epoch；成功/失败/取消均在finally触发恰一次内部settled，释放后才分发UI语义。需要调整信号契约及异常/析构时序。 |
| **[RECOMMENDATION] C 每次解析独立进程、窗口关即强杀** | 暂不选。现有Qt池+CLI已有隔离层，再套进程增加IPC/启动成本；不能用强杀替代资源终结协议，也不解决结果归属。 |

**[RECOMMENDATION]** owner在GUI线程持有request registry；worker只报告带ID的result与settled，registry对终结去重并做容量回收。旧view_epoch结果不得写新model；即使结果被丢弃，settled仍必须释放实际请求记录。每个重复URL行有独立entry_id；未来同请求共享解析时，一份请求可有多个明确订阅者，不再URL→单row覆盖。

**[RECOMMENDATION]** QuickAdd接同一CancellationToken，在每URL开始、解析等待和发出任务前检查；返回每项accepted/rejected/cancelled摘要，不能只记录失败URL而UI看不到。取消一个窗口不取消别的订阅者；最后订阅者退出才请求底层取消。这里不新发下载run outcome，保持解析flow事件与下载任务结果的既有区分；只有真实失败边界发diagnosis。

### 迁移、回滚、测试与验收

**[RECOMMENDATION]** 先加入settled并保留旧finished/error信号，再引入带request_id回调，最后替换URL→单row。旧adapter应从新终结记录生成旧信号，不能同时两套释放。回滚仅回退UI路由，保留恰一次终结/释放修复，避免为回滚重新引入槽位泄漏。关闭窗口先标epoch失效、停止派发、请求cancel，实际资源结束仍由registry收尾；不在GUI线程无限wait。

**[CONFIRMED/static]** 现有Cookie代次测试只验证请求模式/缓存：[toggle_during_parse](../../tests/test_youtube_cookie_mode.py#L103)，不是Qt迟到结果测试。**[RECOMMENDATION]** 新增解析生命周期夹具（建议 `tests/test_parse_request_lifecycle.py`）：取消前已完成/取消中/异常/重复settled、同URL两行、新列表复用URL、旧结果比新结果晚到、窗口销毁后settled、QuickAdd多URL中途取消。验收：模拟全部终结后active=0；取消不触发UI错误；旧epoch对model写入次数为0；新的有效请求仍能占用释放槽。实际CLI取消延时作为测量项，不以发Event耗时替代。

## I03 · 保留默认关闭的缓存，先修复失效语义再减少重复解析

### 现状、缘由和不足

**[CONFIRMED/static]** 缓存TTL默认0，get/put均停止；可配置分桶上限：dialog12、playlist_flat8、vr12、entry_detail64、channel_tab9，单info entries超过3000不写。命中/写入deepcopy，POT未warm不写；封面不读但可写。key含URL/mode、Cookie指纹/mode/generation、proxy、extractor_args、format及语言策略。[常量](../../src/fluentytdl/youtube/youtube_service.py#L191)、[TTL](../../src/fluentytdl/youtube/youtube_service.py#L1546)、[key](../../src/fluentytdl/youtube/youtube_service.py#L1586)、[get/put](../../src/fluentytdl/youtube/youtube_service.py#L1654)

**[CONFIRMED/static]** Cookie开关会generation++并clear，put锁内拒绝旧generation；手动`invalidate_parse_cache`只clear不增generation。key目前没有runtime identity；系统代理/TUN的出口变化不能从空proxy字段识别。[Cookie变化](../../src/fluentytdl/youtube/youtube_service.py#L235)、[put代次门](../../src/fluentytdl/youtube/youtube_service.py#L1718)、[手动失效](../../src/fluentytdl/youtube/youtube_service.py#L1734)、[默认关闭测试及理由](../../tests/test_parse_cache.py#L389)

**[INFERRED]** 默认关闭优先保证新鲜格式；分桶防列表详情挤掉选择窗口，copy防UI就地修改。手动清缓存后旧在途结果仍有回填窗口；runtime切换与出口变化也不在完整缓存身份中。不能只拉长TTL宣称“优化”。注释提到的deepcopy耗时不是本轮测量，不引用为现有性能数字。

### 方案比较与推荐

| 方案 | 收益 / 代价 / 裁决 |
| --- | --- |
| **[RECOMMENDATION] A 维持默认TTL0，仅修失效epoch** | 推荐第一阶段。少一次不可靠缓存命中比多一次解析更危险；没有短期复用收益，但兼容当前用户选择与测试。 |
| **[RECOMMENDATION] B A之上增加明确范围的在途请求合并，再按数据类型缓存** | 推荐第二阶段，须I02完成。仅合并相同有效上下文、runtime身份、解析模式、URL的同时请求；订阅取消独立。可以减少重复CLI次数，实际节省量须计数。 |
| **[RECOMMENDATION] C 全局持久缓存完整info、默认长TTL或跨模式复用** | 不选。签名URL、登录/出口/格式能力时效不可从TTL推导；持久化增加隐私和兼容成本，封面会直接用URL执行尤其危险。 |

**[RECOMMENDATION]** 新增cache_epoch，所有语义失效（明确清空、已知网络配置变更、runtime更新、有效凭据版本变更）递增；request捕获epoch，get/put都核对。它与view_epoch、Cookie mode snapshot分别命名和记录，不能一个整数包办。保留用户明确TTL0的含义；在途合并不保留完成结果，不能暗中变成TTL缓存。提供refresh intent，用户明确重试/强制刷新不得加入旧在途请求。

**[RECOMMENDATION]** 在途合并必须等I02终结/订阅协议、I05实际runtime身份以及C01可证明的有效材料身份就绪。选中账号、旧meta或相同Cookie文件路径不构成等价上下文证明；材料来源/版本为unknown时独立解析，不为提高合并率猜测共享。默认匿名模式也要保留runtime、网络政策和解析类型差异，不能只用“无Cookie”作为合并键。

**[RECOMMENDATION]** 如测量证明收益，再把可展示字段与格式/临时URL资料分开：标题/时长/缩略图描述的陈旧结果只能作标明来源的preview，不能直接生成可执行format计划；格式资料要求相同解析上下文并遵循当前新鲜度政策。未知TUN出口无法可靠侦测时继续不承诺跨请求缓存安全，不为查出口额外联网。缓存大小除条目数外记录估算字节、copy耗时；只有证据显示copy为瓶颈才考虑不可变DTO，不贸然去掉copy保护。

### 迁移、回滚、测试与验收

**[RECOMMENDATION]** 先改epoch并保留当前数据结构/TTL默认，再以可关闭选项加入同会话在途合并，最后才引入结果类别。回滚新缓存时清内存并提升epoch，已有Plan不变；关闭合并后新请求独立执行，已运行共享请求仍按旧订阅记录收尾，不能破坏取消归属。

**[CONFIRMED/static]** 现有资产覆盖 [分桶](../../tests/test_parse_cache.py#L68)、[copy隔离](../../tests/test_parse_cache.py#L105)、[POT未warm](../../tests/test_parse_cache.py#L149)、[cover跳读](../../tests/test_parse_cache.py#L356)、[TTL0读写停](../../tests/test_parse_cache.py#L418)。**[RECOMMENDATION]** 补A开始→clear→A返回不回填、runtime变化key变化、等价请求N订阅仅一次CLI、一个订阅取消不杀其余、全部取消释放、refresh绕过、结果不跨Cookie mode/VR/频道tab、失败不长期负缓存。验收为请求次数/命中身份/内存上限可解释，默认TTL0兼容，不能只以命中率上升判成功。

## I04 · 列表形成有界父计划，调度准入与UI分批解耦

### 现状、缘由和不足

**[CONFIRMED/static]** QuickAdd auto阈值默认50，小列表意图single_worker；上限500，超限按分支截断，single_worker写noplaylist=False；Worker无条件覆盖True。controller在同一循环为每项查目录避名、insert/create/start；`physical_exists`每候选名调用os.listdir。Manager最大并发配置只夹到32位整数上限，pending deque本身无显式容量。[QuickAdd策略](../../src/fluentytdl/core/quick_add_worker.py#L97)、[Worker覆盖](../../src/fluentytdl/download/workers.py#L1185)、[目录扫描/建任务](../../src/fluentytdl/core/controller.py#L123)、[并发与pump](../../src/fluentytdl/download/download_manager.py#L334)

**[INFERRED]** controller提前避名改善任务列表可辨认性，manager限制运行数保护同时下载；但运行数限制不限制大量待启动QThread对象/DB插入/GUI同步扫描。对N个任务、K次候选冲突、目录F个名字，重复扫描成本与(N+K)×F有关，且前置检查并非最终占有名字；最终Staging仍要处理并发碰撞。不拿这个复杂度推导实测卡顿毫秒数。

### 方案比较与推荐

| 方案 | 收益 / 代价 / 裁决 |
| --- | --- |
| **[RECOMMENDATION] A 保持即时逐项create/start，只删除QuickAdd single_worker选项** | 能快速消除上游承诺冲突，改动少；仍缺批次部分失败语义与准入预算。可作为先行修正。 |
| **[RECOMMENDATION] B 父BatchPlan + 单媒体子Plan + 有界准入** | 推荐。列表是编排集合，每个子项保持独立task/run/txn；批次progress显示已计划/已接纳/待接纳/失败/取消。支持停止未接纳项，而不伪造已经存在的持久任务。 |
| **[RECOMMENDATION] C 真正支持一个Worker下载整列表** | 暂不选。需要重新定义多媒体Staging主文件、每项失败重试、取消及outcome，不是去掉noplaylist这一行就可安全完成，明显超出入口优化收益。 |

**[RECOMMENDATION]** 先将QuickAdd所有列表规划成独立子项，明确展示截断/缺URL/单项解析失败，不能悄悄宣称全部已排队。频道tab结果保留分类与来源，父计划不能把unsupported当网络失败自动重试。重复URL默认保留独立选择项，是否去重应由用户意图决定，不能为省请求顺带删除下载项。

**[RECOMMENDATION]** 实施顺序：第一步批量验证Plan、按目标目录一次扫描生成展示避名索引、GUI按小块准入并交还事件循环；磁盘最终命名继续由Staging裁决。索引只作建议，不作防覆盖证明。第二步在测得对象/内存压力后，把pending表示为Plan/持久任务引用，真正获运行许可时再造DownloadWorker；取消未执行项直接终结其队列意图，不需要启动线程。父计划中未接纳部分的跨重启需求必须先决定：若首期不持久化，应明确提示其尚未加入任务，不能承诺重启恢复；若需要持久化，另加版本化批次记录及原子子项准入协议。

### 迁移、回滚、测试与验收

**[RECOMMENDATION]** 保留当前下载并发与precise互斥，不在入口章改成未经测量的自适应带宽调度。先只优化目录读与GUI分块，再迁移pending表示；回滚分块时保持已接纳task，不重放整个批次。用稳定batch_id+child_id在准入记录去重，防回调重复导致同一项重复插入；这不是跨用户操作的URL去重。旧single_worker历史任务不可默默扩成许多新任务，应提示重新规划列表并记录父子关系。

**[RECOMMENDATION]** 现有QuickAdd/准入端到端专用测试未在本轮检索确认，应新增建议资产 `tests/test_intake_admission.py`；复用 [片段options](../../tests/test_section_download.py#L35) 和 [Cookie roundtrip](../../tests/test_youtube_cookie_mode.py#L206)。验收覆盖0/1/50/51/500/501项、重复URL、缺URL、各目标目录/同名前缀、第二批次与外部新建文件竞态、取消中间块、DB插入失败重试不重复、precise旁路公平性。性能验收以同一合成目录比较listdir次数：初次批规划每个不同目标目录至多一次，冲突不能重新扫整个目录；最终碰撞交付仍由Staging测试确认。

## I05 · runtime身份按执行绑定，能力预检分级且不阻塞GUI

### 现状、缘由和不足

**[CONFIRMED/static]** `resolve_runtime`优先有效custom，再local/PATH/frozen legacy；RuntimeIdentity只有path/source。解析CLI与下载Executor分别在调用时resolve；版本probe按path+size+mtime+ctime缓存，在全局Lock内启动最长10秒的无URL子进程。managed安装目标与实际执行选择是不同接口，不能更新custom/PATH字节。[resolver/probe](../../src/fluentytdl/utils/ytdlp_runtime.py#L22)、[解析resolve](../../src/fluentytdl/youtube/yt_dlp_cli.py#L1054)、[下载resolve](../../src/fluentytdl/download/executor.py#L373)、[managed目标](../../src/fluentytdl/core/dependency_manager.py#L191)

**[INFERRED]** 共用resolver避免状态显示用错误版本，但解析到排队执行之间仍可能换工具；跨身份缓存及格式选择可能过时。全局probe锁可让另一路身份查询等待慢probe；本轮没有证明调用处一定造成UI冻结，问题应写成边界而非已复现症状。

### 方案比较与推荐

| 方案 | 收益 / 代价 / 裁决 |
| --- | --- |
| **[RECOMMENDATION] A 保持每次resolve，增加身份日志** | 易回滚，保留工具可更新性；只能事后解释身份变化，不能在执行前处理旧格式资料。 |
| **[RECOMMENDATION] B Plan冻结选择政策、RunSpec绑定实际身份并重验** | 推荐。source_policy=custom路径/自动选择由用户意图保存；实际path/stat/version在run开始解析，和ParseSnapshot比较。changed时重新解析必要格式/能力，仍保持原用户质量与匿名意图。 |
| **[RECOMMENDATION] C 为每个排队任务复制并永久锁定exe和全部工具** | 不选。磁盘/更新治理成本高，插件与JS/FFmpeg仍有组合身份；长期旧工具也可能失去网站兼容性。需要严格复现实验时另设受控工具snapshot，不把它作为普通下载默认。 |

**[RECOMMENDATION]** 分两级preflight：纯本地cheap检查（路径、必要工具存在、Plan模式合法、目标目录意图）在准入/后台完成；实际能力probe（版本、插件、JS/POT就绪、特殊FFmpeg能力）只按任务需要执行，并缓存到对应identity。用每identity的在途probe合并代替“持全局锁执行外部进程”；锁只保护登记结果。缺工具是明确preflight失败，probe超时是unknown/timeout，不用sidecar冒充版本，也不偷偷改用户自定义路径。

**[RECOMMENDATION]** RunSpec创建后把同一身份传给当前run各调用，attempt重试如需因更新/失效换身份，必须显式record decision并重建依赖资料，不能一半旧exe一半新插件而日志只有一个“版本”。开始Popen前再次stat比对仅能降低TOCTOU，不是防恶意替换安全保证；不为确定性而全文件哈希每个进度tick。有效credential与POT服务所有权参见总方案对应资源/上下文章，不由runtime resolver接管。

### 迁移、回滚、测试与验收

**[RECOMMENDATION]** 先给ParseSnapshot/RunSpec添加身份并只报告差异，再启用changed→必要重解析；保留当前resolver优先级。版本probe改造必须保持missing/unknown/timeout/error语义。回滚关闭差异门时仍保留真实身份观测；不切换custom工具、不删除用户文件。旧Plan无runtime政策以当前兼容默认迁移并标legacy，无法恢复历史exe身份时明确未知。

**[CONFIRMED/static]** 测试资产：[override优先](../../tests/test_ytdlp_runtime.py#L7)、[替换exe使版本缓存失效](../../tests/test_ytdlp_runtime.py#L41)、[执行状态共用身份](../../tests/test_ytdlp_runtime.py#L83)、[timeout不借sidecar](../../tests/test_ytdlp_runtime.py#L117)。**[RECOMMENDATION]** 新增不同identity并行probe不互锁、同identity同时N请求只一次probe、排队期间切custom/更新managed、identity变化后缓存miss/重解析、missing不建txn、用户明确format缺失不可静默降画质。验收必须能从脱敏身份与decision重建每run/attempt实际用哪个文件；无真实网络与可执行工具调用的单测不能替代之后冻结包验收。

## 3. 可证据化预算，先量什么再决定什么

| 阶段 | 当前可确认常量/结构，不是性能结果 | **[RECOMMENDATION]** 测量与首期验收预算 |
| --- | --- | --- |
| 解析发起→首屏可选择 | 窗口已有`perf_counter`起点；scroll50ms、detail finalize80ms计时器。[窗口](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L486)、[解析起点](../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1358) | 分开queue_wait/URL展开/POT等待/CLI/JSON/转换/model/首屏；先采合成与受控实际样本p50/p95，再定耗时目标。80ms计时器不是80ms响应保证。 |
| 列表详情 | foreground viewport前1后3；crawl400ms；exec_limit默认3；频道pool≤3。[scheduler](../../src/fluentytdl/ui/playlist_scheduler.py#L157)、[频道](../../src/fluentytdl/download/workers.py#L360) | 首阶段不提高并发；记录在途/排队/取消释放/429及各优先级等待。合并请求只减少相同有效请求次数，不扩大请求并发。 |
| 批量准入 | 下载默认并发3但可配置很大；pending无显式容量；GUI同步逐项建task | 先用建议每块最多20项或8ms CPU工作预算中先到者yield，作为待校准起点而非现有指标；单次DB/目录I/O仍可能超预算，应测超限并移出GUI阻塞点，不能靠分块宣称硬上限。 |
| 缓存 | TTL默认0、各mode条目上限、3000 entries防大info | 记录命中/跳过原因、copy_ms、估算bytes、最大entries；保持TTL0不留完成缓存。启用保留的内存预算需实测后确定，不以条目数冒充字节上限。 |
| 取消与迟到 | CLI watcher轮询、Qt信号、pool取消各自独立 | 用可控barrier断言settled恰一次、旧epoch写model=0、结束后active=0；实际取消p95单独测，不承诺统一“立即”。 |
| 工具预检 | version probe timeout10秒；POT任务等待另有45秒路径 | GUI线程外执行；记录probe/cache/inflight wait，不把10秒+45秒机械相加当所有请求固定耗时。真实超时预算按阶段owner分别配置，避免重复做同能力检查。 |

**[RECOMMENDATION]** 只用既有stage/decision/signal等事件kind承载低频阶段指标；计数不能为每progress tick刷结构化日志。统计URL只记录脱敏标识、请求类型和身份指纹，不记录完整Cookie、签名URL或用户路径。没有实际基线时，先交付正确性预算与请求/目录扫描次数，再报告耗时改进；不能以平均值改善掩盖取消或p95回退。

## 4. 落地顺序与边界

1. **[RECOMMENDATION]** I02终结释放、I03清空epoch先行，属于独立可验证的正确性闭环；不依赖全面Plan重构。
2. **[RECOMMENDATION]** I01建立Plan/legacy adapter，跟已有metadata规格对齐；I04先修QuickAdd语义冲突并分批准入，保留三执行通道和Staging合同。
3. **[RECOMMENDATION]** I05把运行身份与解析资料关联；已有上下文/资源方案提供credential/POT生命周期，避免入口层新增全局owner。
4. **[RECOMMENDATION]** 只在基线表明重复CLI或复制/准入成本显著时推进I03在途合并、I04懒建worker。每项可独立关闭；上线阈值由真实测量确定，本文不授权实现或发布。

**[UNKNOWN]** 当前真实请求延迟、TUN切换频率、失败缓存命中影响、Qt队列积压、实际大列表内存与用户操作分布尚无本轮证据。既有测试仅作为将来映射，不列“已通过”。本章由其他代理交叉复核后才能作为实施排期依据。
