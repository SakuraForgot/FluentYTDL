# 新版架构 V2 · FluentYTDL 文档入口

> 2026-09-19 源码参考版。新版基于当前工作树重新取证，旧架构书仅供历史比较。

> 3.7.4 准备说明（2026-09-20）：本文保留 9 月 19 日取证基线。后续元数据实现、测试组件接线及版本更新已造成源码哈希变化；新增链路以 [元数据实施记录](../../specs/media-metadata/implementation.md) 为准。本目录不代表 3.7.4 全量重新取证。

正式总入口：[**新版架构与运行机制说明书**](NEW_ARCHITECTURE_REFERENCE.md)。已在章节取证与独立复核后生成，包含15个专题章节、20条纵向切片、29条差异/风险及源码索引。不会复用旧版 `ARCHITECTURE_CN.md`、`ARCHITECTURE_EN.md` 或 `ARCHITECTURE.md` 的书名。

建议先读总入口，再按操作选择切片；核对结论强度时读[验证记录](evidence/verification.md)和[风险登记](13-known-risks.md)。

新版目录为 `docs/architecture-v2/`；此前的 [项目适配方案](../architecture/reconstruction-plan/README.md) 保留为执行依据。各章节标题都带“新版架构 V2”，源码引用均以本次工作树为基线。

旧文档有明显实现漂移，不作为新版事实来源。新版重要结论标记 `[CONFIRMED]`、`[INFERRED]`、`[UNKNOWN]` 或 `[RECOMMENDATION]`，同时区分源码证据与运行验证。源码取证不能证明下载、更新或线上部署已经验收。

本次仅修改文档与文档分析工具。现有业务/翻译修改保留，不执行真实下载、登录、更新、安装/卸载、发布或线上写入。
