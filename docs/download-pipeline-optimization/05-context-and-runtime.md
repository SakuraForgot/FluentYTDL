# 认证、POT与诊断支撑链 · 可选择的更优解

本章主张均分清源码事实与建议。目标不是扩大下载主循环，而是让它借用的材料、进程和日志有明确结果。关联[G05/G06/G07](00-goals-and-boundaries.md)。工具身份晚绑定由02的I05负责；这里不增加第二个runtime resolver。

## C01 · 把账户选择、有效材料与单次执行绑定分开

**现状与缘由。[CONFIRMED/static]** [set_current_webview2_account](../../src/fluentytdl/auth/auth_service.py#L1307)先存当前选择，再同步Cookie，忽略同步bool并返回True；[commit gate](../../src/fluentytdl/auth/cookie_sentinel.py#L382)验证失败保留旧源，txt replace与meta写入分两步。[请求模式](../../src/fluentytdl/utils/youtube_request.py#L5)是任务偏好，runfile是CLI启动时材料副本。对应V2 R10。[INFERRED] 保留旧材料避免一次坏提取毁掉原本可用的登录，但返回一个成功值无法说明究竟成功了哪一步。

| 方案 | 收益 | 代价 / 不选理由 |
| --- | --- | --- |
| 保持bool，只改提示文案 | 小改动，可先提示同步失败 | 返回值仍丢失有效来源，调用者易继续误用；可作为第一步修补但不足以长期支撑 |
| **分离AccountSelectionResult和ExecutionBinding** | 明确selected/effective/material_state；任务固定模式，attempt获取实际可用材料 | 要更新调用者和测试；不提供真实账号在线可用性保证 |
| 在入队时永久保存Cookie字节 | 可重现入队瞬间身份 | 持久秘密扩散、材料过期、与刷新/匿名规则冲突；不推荐 |

**推荐接口草案。[RECOMMENDATION]** 在现有AuthService/Sentinel内部拆结果，不新建全局认证manager：

```text
select_account(id) -> {selected, effective_source, bytes_committed,
                      metadata_confirmed, reason}
resolve_binding(task_mode, platform, identity_policy) -> ExecutionBinding
ExecutionBinding = {mode, platform, effective_source_ref, material_revision,
                    borrowed_runfile_resource_id, verification_state}
```

`effective_source_ref`为内部标识，不写原始账户或凭据到公共日志；`material_revision`为非秘密版本标记，不能把Cookie内容哈希当公开身份。meta缺失或txt/meta不一致时标unknown；不得根据旧meta凭空绑定新字节。模式继续在任务边界冻结；默认延续现状“执行时获取有效材料”，同run修复后新attempt允许刷新材料并记录变化，不持久化原始Cookie。用户显式要求某账户时，材料未切换不能悄悄宣称已按该身份运行，应停在可解释的前置状态或由用户选择继续旧源。

**材料一致性补充。[CONFIRMED/static]** [_save_meta](../../src/fluentytdl/auth/cookie_sentinel.py#L180)会吞保存异常，因此meta文件存在且JSON合法仍可能对应旧字节。[RECOMMENDATION] 完整binding阶段需要统一所有受管写入与副本创建的每平台同步边界：验证输入后，在相同短临界区完成txt发布、产生材料receipt并登记其meta结果；创建runfile时，在同一锁内读取已核验receipt并复制对应字节，之后才交给消费者。跨进程调用必须使用相同OS级互斥协议，不能只用某个Python实例锁。receipt以随机material_id和内部完整性绑定证明来源，内部摘要按凭据元数据保护，不入公共日志/导出。旧材料缺证明或meta保存失败时可保留字节，但来源为unknown，不伪造metadata_confirmed。

只在复制前后读两次revision还不够：若写者先replace后更新revision，读者仍可能拿到新字节与旧revision。实施必须证明共享锁或等价版本发布协议覆盖这个窗口；不能把两次相等当作一致性证明。需要更强崩溃一致性时，可另选不可变材料版本+原子发布指针，但迁移成本更高，不作为第一批最小修复前提。

`ExecutionBinding`只借用runfile的资源ID/路径句柄，唯一close与清理owner是C02/E03资源scope；bindings不得各自删除同一个副本。UI通知、状态查询和网络验证不放在材料发布锁内，避免跨服务重入与长时间占锁。

**迁移与回滚。[RECOMMENDATION]** 先增加详细结果并适配设置页提示，旧bool适配器仅表达“选择已保存”，命名和注释不得等同材料成功。旧任务缺identity_policy时按兼容默认解释，不回写Cookie字节。第二步才接入I01计划与attempt绑定。新字段失败可退回旧任务读取，但必须保留“有效源未切换”的真实提示；不能通过回退UI隐藏错误。

**验收。** 用合成YouTube/X材料覆盖账号缺cache、验证拒绝、txt替换失败、meta写失败、匿名任务、切换后旧任务、after_fix刷新；分别断言选择、有效源、runfile字节和UI文案。[test_cookie_truth_source](../../tests/test_cookie_truth_source.py#L118)已测弱回退与平台隔离，[test_youtube_cookie_mode](../../tests/test_youtube_cookie_mode.py#L206)有模式roundtrip；尚不等于测过选择与生效的完整组合。本轮未执行测试。

## C02 · 让Cookie运行副本进入执行资源作用域

**现状。[CONFIRMED/static]** [cookie_runfile](../../src/fluentytdl/auth/cookie_runfile.py#L58)按原始字节复制受管真相源，with退出删除；[识别异常](../../src/fluentytdl/auth/cookie_runfile.py#L37)返回False走自管直通；[启动GC](../../src/fluentytdl/auth/cookie_runfile.py#L91)只有前缀与年龄。[轻量worker](../../src/fluentytdl/download/workers.py#L2052)先取副本，直到2162行才进入覆盖close的try。对应R07/R08。[INFERRED] 隔离CLI回写是正确机制，问题是获取、释放保护区和跨实例归属没有完整表达。

| 方案 | 收益 | 代价 |
| --- | --- | --- |
| 只把try前移 | 最小修复显式释放窗口，优先做 | 不能解决managed识别异常或活副本年龄GC |
| **同一run/attempt scope管理副本与子进程** | 获取即登记清理；退出先等待消费者再删除；可统一初始化失败处理 | 要与E03共用一个scope，不能再建平行清理单例 |
| 所有Cookie文件统一复制 | 表面简单、隔离更广 | 改变用户自管文件回写语义；当前需求不足，不推荐默认改变 |

**推荐分两步。[RECOMMENDATION]** 第一批仅前移保护区，保持原字节复制和自管直通。后续将来源显式标记managed/custom/unknown，来自受管resolver但识别失败时返回前置错误，不能降成custom直通；来自用户自管入口仍保持兼容。scope保存runfile清理回调，子进程仍活着时不删除其文件；若终止超时，报告保留现场而非假装资源已经释放。

新版副本可使用独立于旧`fluentytdl_ck_*.txt`扫描模式的目录和owner记录：进程实例nonce、PID与创建时间、材料归属、创建时刻。清理先确认owner死亡/锁可独占，年龄仅辅助；PID不存在和查不到权限分别处理，nonce文件自身不证明进程仍活。旧无owner文件保留兼容年龄清理政策并明确风险，不能给它们补造所有权。读取失败的私密副本保留有空间/隐私代价，应有明确清理入口和保留上限策略，而非永久积累；具体时限需本地数据政策决定。

**回滚边界。** 新副本位置不能落入旧GC模式；停止创建新副本后先让已注册消费者退出，再由理解新版owner的清理器收尾。程序回退不保证旧版会替新版清理私密临时目录，需要保留维护能力，不将其当普通日志导出。

**验收。** mkstemp后、copy中、POT/env构造、Popen前后逐点抛错；验证原真相源不变且副本只有在消费者结束后删除；另测活owner超龄、PID重用、访问拒绝、双实例。[test_cookie_runfile](../../tests/test_cookie_runfile.py#L60)包含字节隔离、consumer异常和年龄清理，但没有证明全部初始化窗口和跨实例所有权。

## C03 · POT共享服务拥有者与等待者解耦

**现状。[CONFIRMED/static]** [cleanup](../../src/fluentytdl/youtube/pot_manager.py#L151)对默认端口范围发送shutdown；[wait_until_ready](../../src/fluentytdl/youtube/pot_manager.py#L807)join共享预热线程，调用接口无任务cancel；[ensure_warm_async](../../src/fluentytdl/youtube/pot_manager.py#L823)提供去重和30/120/300秒触发式退避。已用匿名Job纳管Provider，不是每任务一份Provider。R03。[INFERRED] 共享预热减少重复工作，但“取消等待”不能等同“关闭共享服务”。

| 方案 | 评价 |
| --- | --- |
| 保持端口广播清理，仅缩短等待 | 容易做，但仍可能影响别的活实例，等待也缺准确阶段结果 |
| **父进程拥有Provider，任务只持等待句柄** | 保留共享预热/退避，任务可取消自身等待；只停止可证明归本实例的进程 |
| 每任务一个Provider | 资源开销与并发端口激增，重复启动/铸token，当前没有必须隔离的证据 |
| 跨实例系统级Broker | 能集中复用，但增加安装、权限、版本兼容和崩溃恢复协议；当前范围不值得引入 |

**推荐协议。[RECOMMENDATION]** 先取消无归属端口shutdown，保留启动碰撞检测与有界选端口重试。由manager记录实际Popen/进程创建身份/Job归属；不能仅凭端口、同名EXE或owner文件发送杀进程指令。未知占用端口避让，不擅自停止。当前Provider是否支持port=0或nonce握手是[UNKNOWN]，方案第一期不依赖不存在的上游能力；选端口→启动仍有竞态，启动失败按有界重试处理，不声称完全消除。

等待方接口拟为`wait_ready(deadline, cancel_token) -> ReadyResult`，采用可取消短等待/状态通知，取消只解除本任务订阅；共享worker继续服务其它任务。ReadyResult区分process_started、port_listening、token_probe、plugin_available和not_ready原因；不能把这些全部投影成一个布尔“网络失败”。探测输出只存安全分类/长度，不存token。全应用退出才由owner停止服务并收割进程。不要把任务共享的cancel_token直接传给全局预热线程。

[CONFIRMED/static] 当前[Service前置门](../../src/fluentytdl/youtube/youtube_service.py#L560)在启用POT且未就绪时阻止继续。[RECOMMENDATION] 保持这个策略：enabled且not_ready/timeout停在明确preflight错误，不能为了降低等待耗时静默移除POT。ReadyResult必须有独立cancelled分支，由调用者作为取消传播；它不能降成普通not_ready再触发全局重启/重试。仅用户明确关闭POT才走相应关闭策略。

**回滚与验收。** 分开“等待接口可回退”和“危险端口广播不可恢复”；新行为异常优先暂停POT自动启动并提示，不能以恢复广播杀端口作为回滚。测两任务一取消、两个应用实例、端口被非本项目服务占用、Job分配失败、预热超时、取消到达等待前后、Provider无输出。现有[test_pot_warm_backoff](../../tests/test_pot_warm_backoff.py#L59)验证模拟时钟退避，[test_pot_job_object](../../tests/test_pot_job_object.py#L51)检查Job标志，不等于真实双实例清理或子进程终结测试。

## C04 · 在写者处保护日志，再按阶段测量

**现状。[CONFIRMED/static]** WebView2的[_log](../../src/fluentytdl/auth/providers/webview2_provider.py#L112)直接写profile，且[proxy_full](../../src/fluentytdl/auth/providers/webview2_provider.py#L157)传入日志；不在公共日志清理链。下载已有trace、raw、diagnosis与outcome，但这些不自动构成可比较的耗时基线。R02。[INFERRED] 旁路日志有冻结无console场景的诊断用途，不能简单删除全部日志；也不能靠公共logger patch保护独立open/write。

| 方案 | 评价 |
| --- | --- |
| 只在导出ZIP时脱敏 | 原文已经落盘，不能解决源头隐私问题 |
| **旁路写者最小白名单+统一脱敏；阶段汇总测量** | 保留阶段和错误分类，避免写代理userinfo、Cookie/token；性能采样受预算约束 |
| 所有stdout/进度全量结构化持久化 | 诊断成本、磁盘写与秘密暴露面上升，也破坏现有事件语义；不推荐 |

**推荐顺序。[RECOMMENDATION]** 第一批将原文代理日志改成mode/是否配置等白名单字段，路径和异常在写入前走可供子进程独立使用的纯脱敏函数；不要为了日志强行import会初始化产品服务的模块。旁路日志另设有限轮转/保留，未知文件不删。合成userinfo、URL查询、嵌套异常必须验证；不能承诺regex可发现所有自由文本秘密。

第二批以monotonic测阶段和取消/保存确认延迟；worker高频进度仍仅供Qt显示，聚合计数在阶段结束或run终结记录一次，用现有signal/stage及安全字段，不新增EventKind，不制造第二个transition/outcome。诊断成功路径依E01先做文件事实仲裁，不能因测量异常抛回下载主链。

**兼容与验收。** 新字段可选，旧日志阅读器应忽略未知字段；去掉字段不恢复原始秘密日志。测量代码异常不能影响任务；比较开启/关闭采样的UI延迟与写入量，结果见[08测量规范](08-validation-and-benchmarks.md)。[test_observability_contract](../../tests/test_observability_contract.py#L300)已有不记录进度事件断言，但不覆盖WebView2独立文件写者。

## 集成到主链的位置

| 接口 | 谁拥有 / 谁借用 | 与其它提案关系 |
| --- | --- | --- |
| DownloadPlan | I01负责不可变用户意图；本章消费其Cookie模式和identity_policy | C01不另建第二份任务配置 |
| ExecutionBinding | run/attempt创建，调用方不持久化秘密；工具身份用I05 | 刷新材料可以变化，模式和显式身份约束不能悄悄变化 |
| ControlToken与ResourceScope | E02/E03唯一owner；runfile作为资源登记 | C02不拥有最终文件提交权；最终文件始终归Staging |
| SharedPOT | POTManager负责进程，任务只借用ReadyResult | C03取消等待不传播成共享服务取消 |
| PhaseMetrics | C04安全聚合，run终结负责最终汇总 | A04/A05确认持久化结果；统计必须区分已提交与仅入队 |

可先实施的三个小边界：前移runfile保护区、暴露账户同步结果、去掉代理原文日志。它们不必等待完整DownloadPlan或ProcessRunner重构。复杂owner文件、身份版本和共享等待接口按07分批推进。
