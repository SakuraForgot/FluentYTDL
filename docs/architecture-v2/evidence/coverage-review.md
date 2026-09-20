# 新版架构 V2 · 原始问题与交付覆盖

此表将用户总提示词的 37 个问题映射到**实际章节与切片**。有文档不等于所有动态场景已验收；未证结果必须随章节的 UNKNOWN 和风险条目一起阅读。

| 原问题 | 新版的回答位置 | 重点边界 |
| --- | --- | --- |
| 1 系统由哪些部分组成 | [00](../00-system-overview.md)、[01](../01-repository-map.md) | 桌面/外部工具/维护/服务分开 |
| 2 模块真实职责 | [02](../02-module-map.md) | 调用、读写、owner、寿命、失败 |
| 3 系统入口 | [01](../01-repository-map.md) | GUI、特殊模式、updater、maintenance、Worker/Admin、发布 |
| 4 用户操作实际组件链 | [03](../03-runtime-architecture.md)、VS-01–20 | 六解析不等于六条执行引擎 |
| 5 谁调用谁 | [02](../02-module-map.md)、[14](../14-code-index.md) | 直接调用与 Signal/CLI/HTTP 不混同 |
| 6 谁依赖谁 | [08](../08-dependency-model.md) | 七类依赖、动态条件、import SCC |
| 7 数据从哪里产生 | [04](../04-data-flow.md) | URL、options、进程输出、远端snapshot |
| 8 数据经过哪些组件 | [04](../04-data-flow.md)、各切片 | 快照/转换/校验/消费 |
| 9 数据存在哪里 | [07](../07-resource-model.md)、[12](../12-deployment.md) | 多数据根、任务与公告DB、认证材料 |
| 10 状态存在哪里 | [06](../06-state-machines.md) | UI/DB/run/phase/event flags |
| 11 谁能修改状态 | [06](../06-state-machines.md)、[02](../02-module-map.md) | manager、worker、restore、writer不同写入点 |
| 12 状态如何转换 | [06](../06-state-machines.md) | 分散状态、部分守卫；不宣称严格FSM |
| 13 长期进程/线程/任务 | [09](../09-concurrency-model.md)、[07](../07-resource-model.md) | 已接通实体与候选未启用代码分开 |
| 14 谁创建资源 | [07](../07-resource-model.md) | spawn/queue/tempfile/connection |
| 15 谁持有资源 | [07](../07-resource-model.md) | owner与借用路径/引用分开 |
| 16 谁释放资源 | [07](../07-resource-model.md)、[10](../10-error-recovery.md) | stop/close尝试不保证完成 |
| 17 跨进程/跨启动存活 | [07](../07-resource-model.md)、VS-11/12/16/19 | 文件、账户、DB、旧事务、更新备份 |
| 18 Retry 在哪里 | [05](../05-control-flow.md)、[10](../10-error-recovery.md)、VS-10 | 内外层预算、after_fix、退避/取消 |
| 19 Recovery 在哪里 | [10](../10-error-recovery.md)、VS-11/14/16 | 自动重试/重启恢复/回滚不能合并 |
| 20 Polling 在哪里 | [05](../05-control-flow.md) | Provider等待、进程/管道、更新watchdog |
| 21 Watcher 在哪里 | [05](../05-control-flow.md)、[09](../09-concurrency-model.md) | cancel watcher已接通；network watchdog未见接线 |
| 22 Callback/Event 在哪里 | [03](../03-runtime-architecture.md)、[09](../09-concurrency-model.md) | connect注册点、接收对象寿命、迟到信号 |
| 23 控制回环有哪些 | [05](../05-control-flow.md) | pump、列表、输出、retry、POT、writer、更新 |
| 24 回环如何退出 | [05](../05-control-flow.md)、[13](../13-known-risks.md) | timeout/cancel/poison/terminate；缺口明确标记 |
| 25 启动发生什么 | [VS-01](../vertical-slices/VS-01-startup-shutdown.md) | 数据根先于单例、迁移、READY、后台初始化 |
| 26 关闭发生什么 | [VS-01](../vertical-slices/VS-01-startup-shutdown.md)、[09](../09-concurrency-model.md) | 托盘/退出不同，收尾尝试与真实排空不同 |
| 27 核心任务生命周期 | [06](../06-state-machines.md)、VS-02/09/10/11 | task/run/attempt/transaction独立 |
| 28 子进程生命周期 | [07](../07-resource-model.md)、[09](../09-concurrency-model.md) | yt-dlp/后处理/POT/WebView2/updater边界 |
| 29 Pause/Resume/Retry/Cancel | VS-09/10/11、[06](../06-state-machines.md) | live pause同run、after_fix等待、不可取消提交 |
| 30 错误如何传播 | [10](../10-error-recovery.md) | stderr/exception→diagnosis→policy→outcome/UI |
| 31 失败 fallback | [10](../10-error-recovery.md)、[08](../08-dependency-model.md) | 缓存/换源/插件/完整性启发式有条件 |
| 32 隐式依赖 | [08](../08-dependency-model.md) | env、路径、命名、端口、PE能力、内部options |
| 33 循环依赖 | [08](../08-dependency-model.md) | 5组静态候选人工区分，非运行失败证明 |
| 34 Race 风险 | [09](../09-concurrency-model.md)、[13](../13-known-risks.md) | 取消等待、端口所有权、迟到缓存、租约 |
| 35 资源泄漏风险 | [07](../07-resource-model.md)、[13](../13-known-risks.md) | 清理保护窗口、队列/句柄/连接未知边界 |
| 36 强耦合模块 | [02](../02-module-map.md)、[08](../08-dependency-model.md) | opts契约、导入单例、worker/Feature、更新握手 |
| 37 行为对应哪段代码 | [14](../14-code-index.md)、[反向引用](code-to-docs.json) | 文件+符号+行号+状态/资源/切片 |

## 阶段验收的实际口径

- Phase 0–2：独立新版工作区、源码快照、仓库/应用入口、逻辑模块档案。
- Phase 3–12：运行/数据/回环/状态/资源/依赖/并发/错误/安全/部署章节，重要边界有源码引用与处理缘由。
- Phase 13–15：20 条切片、双向代码索引、风险分类；风险记录不等于业务修复。
- Phase 16：跨代理反向复核与 Lead 读回，记录具体被挑战结论和修订，不能以链接检查替代。
- Phase 17：复核后生成新版总入口，发布为**存在未决边界的源码参考版**；不会宣称运行或线上全部验收。

逐符号穷尽、全部动态派发、第三方实现内部、所有媒体/语言组合、真实 Windows 停机与断电、线上部署身份仍是明确限制。当前目的为使维护者可沿源码定位并理解机制，而不是用文档替代环境与故障测试。
