# 新版架构 V2 · VS-18 公告发布、缓存与逐修订确认

证据：2026-09-19 source/static；未访问后台、未发布公告、未读真实 D1/KV/本地 DB，未运行测试。CONFIRMED 是源码、INFERRED 是分析、UNKNOWN 未证、RECOMMENDATION 是验收建议。

## 1. 两种用户动作与数据路径

[CONFIRMED] 维护者在 Vue Admin 保存/发布公告；普通用户由桌面启动后 5 秒、15 分钟 timer 或消息中心手动刷新取公告。两者通过 public snapshot 协议衔接，不共享数据库连接：[Admin api helper，L21](../../../../FluentYTDL-ControlCenter/apps/admin/src/App.vue#L21)、[AnnouncementService.__init__，L155–168](../../../src/fluentytdl/notification/announcement_service.py#L155)。

用途：解释写入事实与对外可见性。范围：公告主干；箭头表示数据投影或请求，没有跨库原子含义。

```mermaid
flowchart LR
  A[Admin save/publish] --> W[Access鉴权与Origin检查]
  W --> D[D1 draft/published/history]
  D --> R[rebuild]
  R --> K[KV announcements]
  K --> P[公开GET快照]
  P --> F[客户端Fetch线程]
  F --> S[本地snapshot SQLite]
  S --> E[版本/语言/过期过滤]
  E --> N[消息中心与强制确认弹窗]
  N --> C[id+revision确认记录]
```

## 2. 后台正常与异常路径

| 环节 | [CONFIRMED] 实现 | 结果与所有权 |
|---|---|---|
| 输入/鉴权 | [Worker.authenticate，L9–21](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L9) 校验 JWT issuer/audience/email；管理写检查 Origin L80；[shared schema，L6](../../../../FluentYTDL-ControlCenter/packages/shared/src/index.ts#L6) 校验内容 | 本地 bypass 仅 localhost；public GET 不等于可管理。 |
| 保存草稿 | [editAnnouncement，L22–25](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L22) 只改 draft/updated_at | 既有 published 不随未发布编辑改变。 |
| 发布 | [L28–39](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L28) 校验 Markdown/时间，critical 强制 require_ack，revision+1，D1 batch 更新 published 并追加 history | 修订是可追溯内容快照；客户端按 id+revision 确认。 |
| 下线 | [L40–46](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L40) archived 并追加历史，随后 rebuild | 已保留历史与对外快照分开；下线不是删除全部修订记录。 |
| 分发失败 | [rebuild，L4–15](../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L4) catch 同时覆盖 KV put 与随后 D1 state；尝试记 snapshot_write_failed，成功后抛503 | KV put 失败时旧快照可仍可见；KV 成功后 state 失败则新快照已发布。catch 中的 state 也可再失败，故不保证错误状态保存或预期503。republish 重建而非新增修订。 |
| 并发 | [types.locked，L22–27](../../../../FluentYTDL-ControlCenter/apps/worker/src/types.ts#L22) publication 锁 240 秒，不续租，token 释放 | 超租约后旧新 action 可能重叠；token 防旧 finally 删除新锁，不保证 action 绝不并发。 |

[CONFIRMED] public `/v1/announcements` 只 KV get，generated_at 超 86400 秒拒绝、ETag/304、max-age=60；public 失败不会回 D1/GitHub：[index.cached/handle，L29–47](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L29)。cron 每 15 分钟重建公告：[wrangler，L11](../../../../FluentYTDL-ControlCenter/wrangler.toml#L11)、[scheduled，L116](../../../../FluentYTDL-ControlCenter/apps/worker/src/index.ts#L116)。[UNKNOWN] 生产 cron 是否按时执行、KV传播延迟和 D1 schema 是否当前均未读回。

## 3. 桌面接收、过滤与确认

[CONFIRMED] `AnnouncementFetch(QThread)` 通过 configured Transport 获取快照，validate 后保存本地 snapshot，再 emit。失败先发 failed 信号，再尝试仍能 validate 的本地缓存；finally 关闭 HTTP session：[Fetch.run，L130–144](../../../src/fluentytdl/notification/announcement_service.py#L130)。缓存也受新鲜度验证，不是无限期离线展示。

[CONFIRMED] 本地 `announcements.sqlite3` 独立于 TaskDB；revisions 主键 `(id,revision)`，列 notified/acknowledged，snapshot 只有 id=1：[AnnouncementStore，L77–118](../../../src/fluentytdl/notification/announcement_service.py#L77)。eligible 按版本/语言/expiry 过滤，通知成功 push 才记 notified，require_ack 且未确认才推 pending：[receive，L180–211](../../../src/fluentytdl/notification/announcement_service.py#L180)。清空消息中心不清该确认数据库。

[CONFIRMED] AnnouncementController 一次只开一个 dialog、窗口隐藏不弹，按 pending 逐项展示；强制确认 dialog reject 被禁止，accept 后发 acknowledge_requested；新快照不再包含当前修订时解除 mandatory 并关闭：[announcement_controller，L14/L58](../../../src/fluentytdl/ui/announcement_controller.py#L14)。已下线公告通过旧消息详情打开会提示 inactive，不因此再强制确认。

[CONFIRMED] 主窗口 aboutToQuit 连接 service.stop；停止 timer、置 transport.cancel 并 wait(30000)，没有在此检查超时结果：[MainWindow，L302–305](../../../src/fluentytdl/ui/reimagined_main_window.py#L302)、[service.stop，L216](../../../src/fluentytdl/notification/announcement_service.py#L216)。[UNKNOWN] 共享服务引用/SQLite connection 的实际回收时间和退出超时场景未验证；数据库 `with connection` 是事务上下文，代码未显式定义整个 store 的 close owner。

## 4. 处理缘由、代价与跨模块缺口

[INFERRED] 草稿/发布分离防编辑时泄漏半成品；修订主键防修改重要内容后沿用旧确认；KV 公读隔离 D1/GitHub 负载；本地缓存和 notified 去重减少重复打扰。代价是 D1、KV、客户端 snapshot、消息记录、ack 之间有多种暂时不一致，必须说清“已保存”“已分发”“已展示”“已确认”。

[CONFIRMED] 当前卸载维护列表未包括数据根的 `announcements.sqlite3`：[maintenance，L205](../../../installer/maintenance.ps1#L205)。[INFERRED] 可能保留已读/确认和内容缓存，应与卸载切片联合验收，不能宣称所有应用数据已清。

[RECOMMENDATION] 最小场景：编辑不改变 published；发布 revision 单调；D1成功/KV失败后 republish；超 240 秒与第二请求交错；public 只读 KV；过期缓存拒绝；语言/版本边界；重要公告逐修订确认；窗口隐藏/下线时弹窗变化；退出取消/超时；卸载数据库发现。现有 [worker.test.ts](../../../../FluentYTDL-ControlCenter/tests/worker.test.ts)、[test_announcements](../../../tests/test_announcements.py)、[test_announcement_dialog](../../../tests/test_announcement_dialog.py) 是测试资产，不是本次已通过结果。
