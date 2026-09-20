# 方案交付复核与证据边界

日期：2026-09-19。本记录针对**项目适配方案与首轮调查**，不是完整 Phase 16 架构验收，也不是产品测试报告。

## 1. 已执行工作

- 主代理读取用户总提示词、客户端和关联服务约定，确认工作树已有改动，创建专属方案目录。
- 三个 subagent 分别从源码调查仓库/部署、运行/状态、资源/安全；主代理整合阶段计划、20 条切片、处理缘由及验收标准。
- Repository 代理交叉审查非其撰写的运行/资源报告及方案；Runtime 代理交叉审查仓库/ControlCenter 报告与缘由章节。
- 主代理独立读回 DBWriter、POT 启动、CookieRunfile、shutdown、ControlCenter KV/D1 顺序及维护入口，完成以下修订。
- 运行本目录的标准库分析工具，生成选定源码/配置/测试的哈希及 Python AST 索引；检查文档链接、引用行号范围、围栏配对及快照漂移。机器结果见 [documentation-check.json](documentation-check.json)。

## 2. 已采纳的纠偏

| ID | 初稿/历史表述的问题 | 修订后的结论 | 回查依据与状态 |
| --- | --- | --- | --- |
| V-01 | 只看到 TaskDB 共享连接，把独立 writer 队列当未来建议 | TaskDB 共享连接/锁与 TaskDBWriter 独立 daemon 线程同时存在；线程有停止协议，连接 close 仍需查 | [db_writer.py](../../../../src/fluentytdl/storage/db_writer.py#L40)；已改资源报告、缘由章节与覆盖矩阵 |
| V-02 | 误用 POT Node 服务描述 | 当前 POTManager 直接 spawn Provider EXE；JS runtime 是另一依赖 | [pot_manager.py](../../../../src/fluentytdl/youtube/pot_manager.py#L114)、[spawn argv](../../../../src/fluentytdl/youtube/pot_manager.py#L204)；已改主稿 |
| V-03 | 未体现 runfile 获取与 finally 保护区之间的初始化窗口 | 轻量路径先获取 runfile，再进入清理 try；staging/env 初始化异常可能绕过显式 close，实际残留未验证 | [workers.py](../../../../src/fluentytdl/download/workers.py#L2052)、[try 起点](../../../../src/fluentytdl/download/workers.py#L2162)；已补资源报告 |
| V-04 | 将 shutdown 收尾调用顺序写成已完成排空 | worker 强停后短等待和 DBWriter join 超时都不保证完成；更新前只能确认收尾尝试顺序 | [shutdown](../../../../src/fluentytdl/download/download_manager.py#L562)、[flush_and_stop](../../../../src/fluentytdl/storage/db_writer.py#L74)；已收窄运行报告与主稿 |
| V-09 | 托管路径识别异常分支未明示 | 识别异常返回 False，也会直通原 Cookie 路径；隔离是有条件的 | [cookie_runfile.py](../../../../src/fluentytdl/auth/cookie_runfile.py#L37)；已补充 |
| X-01 | 单通道同步失败被概括为保留旧 KV | 上游获取/校验在 KV 写前失败才保留旧快照；KV 成功后 D1 状态失败是部分成功，错误状态记录也可能失败 | [updates.ts](../../../../../FluentYTDL-ControlCenter/apps/worker/src/updates.ts#L74)；Runtime 独立指出，Lead 回查并改仓库报告 |
| X-02 | 公告 snapshot_write_failed 被等同 KV 未写成功 | KV put 与其后的 publication 状态写同处 try；该错误不能唯一定位 KV 写失败 | [announcements.ts](../../../../../FluentYTDL-ControlCenter/apps/worker/src/announcements.ts#L8)；已改仓库报告 |
| X-03 | attempt 仅解释为自动重试，暂停/恢复容易被理解为换 run | after_fix 也递增 attempt；活 worker pause/resume 保持 run，已结束后重建另算执行 | [workers.py](../../../../src/fluentytdl/download/workers.py#L1488)、[controller.py](../../../../src/fluentytdl/core/controller.py#L477)；已改缘由与 VS-09 |
| X-04 | 信号示例写 status_changed | 此状态入库边界真实信号是 unified_status | [manager 回调](../../../../src/fluentytdl/download/download_manager.py#L455)；已改缘由 |
| X-05 | 旧中文架构仍描述 DB 主键沙箱和快速通道绕过沙箱 | 当前事务子目录使用完整 UUID；三执行通道的产物交付接入 Staging | [Staging.create](../../../../src/fluentytdl/download/staging.py#L755)、[独立复核 V-05](05-independent-review.md)；本次记录漂移，不覆盖旧文件 |

独立审查原始判断与修订读回记录保留于 [05-independent-review](05-independent-review.md)。保留审稿时的错误主张是为了追踪修订，不能把表格的“被审主张”列当作当前事实。

## 3. 仍然未决的边界

下列已收进后续方案，未实施业务修复：挂起等待时取消、解析取消后容量释放、关闭强停与提交保护、POT 端口范围 shutdown 的所有权、runfile 年龄清理与初始化保护窗口、配置保存吞错、DB drain 超时、入口声明漂移、D1/KV 部分成功、长操作租约过期。

这些条目有源码可见的触发条件或缺口，但尚无本次运行复现。具体风险标签见专项证据，不能合称“已确认这些功能都会失败”。

## 4. 清单范围与限制

[inventory.json](inventory.json) 覆盖客户端 `src/`、`scripts/`、`tests/`、工作流、安装脚本、根 Python 文件及指定构建配置；ControlCenter 覆盖 apps/packages/migrations/scripts/tests 与指定配置。第三方内嵌 Python 和 POT 插件因位于源码内一并记录，但 AST 导入候选不等于应用运行依赖。

未读取 config.json、真实 Cookie、SQLite 用户数据、日志内容、`dle_profile/`、`bin/` 运行材料。`build/`、`dist/`、`release/`、artifacts 和缓存不作为源码事实；`PyQt-Fluent-Widgets-Source/`、根外置 `yt-dlp-plugins/`、legacy 文档、scratch 等未作为主产品实现深挖，后续若实际解析器/打包引用到它们，再按调用关系纳入。机器清单不是磁盘所有文件清单。

未执行应用启动、真实 yt-dlp/FFmpeg/POT、GUI 自动化、业务单测、构建、安装/卸载、发布或线上请求。Mermaid 仅检查围栏配对，未运行渲染器；源码链接检查保证路径存在和行号有效，不自动证明符号语义。

## 5. 完成判断

本次交付为完整的项目适配**方案包**及支持它的首轮证据，不将未来 20 条切片或 Phase 2–17 标为全部完成。`ARCHITECTURE.md` 留待完整逆向和最终验证通过后生成。

方案之外现有修改不属于本次产物，未暂存或提交。最终文件与检查数量以机器检查报告为准。
