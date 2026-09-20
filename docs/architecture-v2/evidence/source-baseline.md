# 新版架构 V2 · 源码基线与证据协议

## 基线

- 调查日期：2026-09-19。
- 客户端：`D:/ALL Projects/YouTube/FluentYTDL`，分支 `release/3.7.3`，HEAD `c441f2cde2e7973fe5902da50358f25c37c6525f`。
- 关联服务：同级 `FluentYTDL-ControlCenter`；不以客户端 HEAD 代表服务版本。
- 版本对象：**当前工作树**，不是干净 Git 提交。既有 locales 六个文件、diagnostics/catalog.py、diagnostics/engine.py、youtube/youtube_service.py 修改及 specs/、test_playlist_authcheck.py 未跟踪项保留。方案创建前状态见 [原始基线](../../architecture/reconstruction-plan/evidence/baseline.json)。
- 新版只新增本目录文档/分析工具，并更新旧书历史提示、项目首页架构导航及原方案的新版落点说明。没有业务修复、代码重构、依赖变更、翻译生成、Git 暂存或提交。

## 快照与派生证据

| 文件 | 记录什么 | 不能证明什么 |
| --- | --- | --- |
| [inventory.json](inventory.json) | 347 个客户端源码/构建/测试文件、19 个服务端文件的哈希；Python AST 定义/导入候选 | 不覆盖运行数据、线上部署、二进制真实身份和未收录新增文件 |
| [dependency-candidates.json](dependency-candidates.json) | 215 个 src Python 模块、903 条 import 候选、5 组 SCC | 不等于调用图；包含 TYPE_CHECKING/局部导入/包级重导出 |
| [runtime-candidates.json](runtime-candidates.json) | process/thread/event/lock/cleanup/retry 词法候选，支持逆向扫描查漏 | 命中注释或未接通代码不证明真实运行，候选数量不能当作回环数量 |
| [code-to-docs.json](code-to-docs.json) | 代码文件→新版文档引用与行号 | 文件级引用，不自动证明每个函数均已解释 |
| [documentation-check.json](documentation-check.json) | 文档、链接、行号、标题锚点、围栏和已记录集合的哈希检查 | 不证明符号语义、Mermaid 可渲染、产品运行或全部测试通过 |

Python AST 由标准库解析，不导入产品模块。原因是当前项目的模块导入可能触发 DB 初始化、单例构造及后台线程；“为了取符号而 import”会改变调查对象。

源码范围：客户端 src/scripts/tests/workflows/installer 和指定根构建配置、Python 入口；服务端 apps/packages/migrations/scripts/tests 与指定配置。没有读取真实 Cookie、config.json、用户 DB、日志、账号 profile、运行 bin、下载目录。内嵌第三方源码按其宿主路径列库存，不当成自主设计模块。

## 标签和动机

- `[CONFIRMED]` 或 `[CONFIRMED/static]`：源码/本次工具输出可直接支持，运行结果未自动获得确认。
- `[INFERRED]`：由代码约束推导的作用、代价或风险；不能写成作者历史决策或已发生故障。
- `[UNKNOWN]`：未证分支/环境/动态结果，需说明缺哪种证据。
- `[RECOMMENDATION]`：改进/补证建议，不进入当前架构图作为已实现节点。

重要处理解释“触发约束→机制→状态/资源影响→为何有用→代价→失败/退出条件”。注释只作为意图证据，与执行代码冲突时以代码为准，并记录差异。

## 文档版本与维护

这套文档的书名是“新版架构 V2”，总入口为 `NEW_ARCHITECTURE_REFERENCE.md`。旧 ARCHITECTURE_CN/EN 已增加历史参考提示，正文保留。新版不使用旧书作为现状权威；旧书里过期的文件夹、接口、状态、恢复保证不能直接继承。

后续变更入口、状态、资源、重试、进程/权限或外部协议时，更新对应章节与切片，再更新索引和源码基线。发生哈希漂移先查变化含义，不能通过覆盖快照消除告警。本次只有源码级确认；产品运行、安装和线上验收须另外记录。
