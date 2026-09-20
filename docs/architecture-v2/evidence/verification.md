# 新版架构 V2 · 集成验证与反向复核记录

2026-09-19；本报告验收文档及源码证据，范围为当前工作树。**没有执行产品测试、构建、下载、登录、更新、安装/卸载或线上请求。** 未读取真实 Cookie、用户配置、日志或数据库。源码风险的记录不构成业务修复。

## 1. 分工与独立性

| 角色 | 编写责任 | 独立复核范围与记录 |
| --- | --- | --- |
| Lead | 00/02/13/14、基线、覆盖映射、工具、总入口 | 统合各代理证据；读回关键代码、保证上限与修订；检查新旧书边界 |
| Repository subagent | 01/08/12、VS-16–20 | [运行与资源复核](review-runtime-resources.md)：回查其他作者的03–11及VS-01–15关键主张 |
| Runtime subagent | 03/05/06/09/10、VS-01–11 | [部署与模块复核](review-deployment-modules.md)：回查Repository及Lead章节，补终结竞争 |
| Resource subagent | 04/07/11、VS-12–15 | [边界复核](review-boundaries.md)：回查Lead风险、模块和Repository发行链 |

作者自己的链接检查不计为独立审查；每份审查报告限定已回查的主张，不宣称逐行证明所有代码。

## 2. 反向验证方法

先由用户动作正向追踪调用，再从状态写入、进程产生、资源清理和错误终结向上找调用者。机器候选仅用于查漏，最终以读取实现和注册/调用点为准：

- [库存](inventory.json)记录347个客户端文件和19个关联服务文件；Python解析无错误。
- [依赖候选](dependency-candidates.json)收录215个src Python模块、903条import、5组SCC；08章人工区分TYPE_CHECKING、局部导入、重导出和实际初始化约束。
- [运行候选](runtime-candidates.json)含1334个词法匹配，不等于1334个活动回环。NetworkWatchdog、DiskMonitor、完整后验QualityGuard经反查未找到所述生产接线，因此没有画成默认运行能力。
- [代码反向引用](code-to-docs.json)从实际文档源码链接生成，配合14章符号入口使用。它是文件级引用索引，不是全程序调用图。
- [37问题映射](coverage-review.md)逐项给出实际章节与切片，不使用“目录已存在”替代机制解释。

## 3. 已挑战并修订的关键结论

| 原先过强或不完整的说法 | 本版最终限定 | 复核依据 |
| --- | --- | --- |
| 同步失败就保持旧公开快照、保证记录失败状态 | KV写前失败、KV后D1失败、错误状态再写失败是三种结果 | review-boundaries RB-01；VS-18/20、12及R21 |
| 公告已读与确认 | 实际字段为已通知/已确认，不等于TaskDB通知is_read | review-boundaries RB-02；02/VS-19 |
| DBWriter批次均最多200条，批内出错必整批重试 | 正常_collect有上限，退出drain没有；单项吞错未必触发外层fallback | 07/09/10；R13/R14 |
| 所有未知或损坏事务现场均保留 | GC只对可识别committing/rollback_failed保留，未知/坏journal满足门限后删除 | 10/VS-11；R26 |
| 写前日志调用保证可恢复后才动文件 | journal OSError被记录后可继续commit，不保证磁盘已持久保存 | 10/VS-11；R25 |
| FFmpeg附加组件均通过版本验证 | 更新后extra的ffprobe存在PATH检查，不等于完整版本/功能测试 | VS-17 |
| 取消、错误信号与最终outcome总是一致 | 等待唤醒、EOF、Executor返回及postcommit均有不同仲裁边界 | 10/VS-09–11；R11/R27–29 |
| CLI取消watcher使用done_event | 实际读取proc.poll/cancel_event并有界join，不存在该done_event变量 | 09；Resource交叉反馈、Runtime源码读回 |

这些修订调整文档主张，不改产品实现。独立报告保留发现和读回结果，便于后续维护者理解为什么收窄措辞。

## 4. 机械检查与最终门槛

从仓库根运行文档分析工具：

```powershell
.venv/Scripts/python.exe docs/architecture-v2/tools/reference.py index
.venv/Scripts/python.exe docs/architecture-v2/tools/reference.py verify
```

最终数值以[documentation-check.json](documentation-check.json)为准：检查全部新版Markdown的本地链接、源码行号范围、标题锚点和代码围栏，并对366个已记录源码文件进行哈希比对。最终交付要求failures为空；发生漂移先调查，不重抓基线掩盖差异。

本次最终检查：43份Markdown、1374个本地链接、30个Mermaid图块、366个源码文件哈希，`failures=[]`；反向索引涉及108个被正文实际引用的源码文件。三份独立报告的必要文档修订已读回关闭。

`capture`仅用于明确建立新调查基线；`index`更新派生索引；`verify`不导入产品。图直接以Mermaid源码内嵌相应章节，避免另存图副本漂移。本轮只检查围栏/统计图块，**未执行Mermaid渲染**。

## 5. 完成范围与保留项

完成的是Phase 0–17的**源码参考版交付**：仓库与模块、运行/控制/数据、状态/资源、依赖/并发、错误/安全/部署、20条用户纵向切片、风险和双向定位、跨代理反向复核，最后汇总为独立新版总入口。

仍未证明：真实Windows强停与断电耐久性、所有Qt动态线程归属、真实下载/认证/POT可用性、全部媒体/语言组合、线上部署与本地源码的一致性、第三方内部机制、所有符号穷尽，以及未纳入库存的新增文件。补证场景见13章各条目；不能把静态检查通过宣传为产品功能或发布验收通过。
