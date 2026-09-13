# 日志改良实施记录

本次在现有工作区上增量实施《LOGGING-REVIEW-2026-09-12.md》，没有提交、发布、替换 EXE 或重启正在运行的应用。旧分析文档中的行号和现象保留为实施前证据。

## 已实现的功能与入口

| 功能 | 实施结果 | 主要入口 |
|---|---|---|
| 测试隔离 | 根 conftest 在收集前分配独立数据目录；默认 pytest 仅收集正式 tests；日志标记 app/test/probe 来源 | `conftest.py`、`pyproject.toml` |
| 完成状态可信 | 97% 核对、99% 整理保持 processing；verify/finalize 分阶段记录；封闭 run 的迟到状态被拒绝，新的 run 可重新开始 | `download/workers.py`、`download/download_manager.py` |
| 会话证据 | 无 flow/task 的事件进入 session JSONL；环境、有效配置、工具版本、规则版本/哈希及可用的 BUILD_INFO 字段落盘；源构建资料缺失明确标记 | `utils/startup_info.py`、`observability/sinks.py` |
| 统一脱敏 | 文本、最终异常堆栈、展示元数据、结构化事件、raw dump、诊断包及 Issue 预填日志共用脱敏；磁盘 sink 禁止局部变量诊断输出 | `utils/log_privacy.py`、`utils/log_runtime.py`、`utils/logger.py` |
| 稳定身份 | 文本/元数据/trace/实时事件共用 record ID；保留完整时间；时间线用 session 区分同名 flow | `utils/log_runtime.py`、`utils/log_signal_handler.py` |
| 轮转与健康状态 | 轮转前检查大小，重置计数，纳秒与随机后缀防同秒冲突；分通道安装可重试；异常写入计数通过 Qt 显示 | `observability/sinks.py`、`utils/log_runtime.py` |
| 日志生命周期 | app/display 单片 20 MiB、7 天；trace 单片 10 MiB、14 天；同步错误单片 10 MiB；运行日志整体 200 MiB，启动后台及每 30 分钟清扫 | `utils/log_runtime.py` |
| 历史诊断包 | 支持 task、flow、指定 run/session；反查同一任务相关 flow；收集对应日期及运行时间窗日志、会话快照、raw 输出；后台处理，原子完成 | `observability/bundle.py`、`ui/components/common/log_jobs.py` |
| 导出真实性 | manifest 记录真实写入文件、哈希、缺失原因、截断、隐私策略、源环境与导出环境；支持进度及“部分导出”状态 | `observability/bundle.py`、`ui/components/dialogs/log_viewer_window.py` |
| 查看器 | 默认当前会话，可切全部历史、时间范围、任务/run/错误码搜索、加载更早记录；历史与实时按 ID 合并；后台读取及小批次渲染 | `utils/log_reader.py`、`ui/components/dialogs/log_viewer_window.py` |
| 旧日志兼容 | 临时 SQLite 索引逐条匹配文本与展示元数据；支持同一天部分记录无元数据、压缩文件、多行异常；损坏/超长行记录健康计数 | `utils/log_reader.py` |
| 任务摘要与警告 | 展示已加载范围的终态、耗时、重试、诊断与缺失；相同警告按 attempt 计数；后续重复原文降为 DEBUG；正常关闭字幕嵌入不再 WARNING | `observability/warnings.py`、`ui/components/common/event_timeline.py`、`download/features.py` |
| 本地异常诊断 | 按异常类型/errno/winerror 分类文件不存在、权限与磁盘空间问题；保留修复入口、退出码及 unknown 样本；成功字幕提示不调用失败仲裁 | `diagnostics/engine.py`、`utils/translator.py`、`download/workers.py` |
| 中英文界面 | 新增日志筛选、导出、摘要文案补齐两种语言并编译 QM | `assets/locales/` |

## 设计边界

- 保留原有 EventKind 闭集和文本 sink 格式；transition 仍只有管理器一个生产点，diagnosis 仍只在失败边界发出。没有增加另一套业务终态来源。
- 磁盘记录与显示队列独立。显示缓冲有上限，丢弃显示记录时报告计数；不会因此删除持久事件。文本最多 1000 条、时间线最多 3000 条。
- 搜索和分页针对当前选择的会话/时间范围。摘要明确标记为“已加载范围”，不能将一个分页范围的耗时或重试数当作完整任务统计。
- 运行日志配额不包含用户主动导出的诊断包。清扫跳过活跃文件、未知文件、链接/重解析目录及 bundles；没有清理用户下载目录。
- 单包证据预算 30 MiB，其中为元数据预留空间；优先为关键事件保留预算，超出部分明确记录。历史记录损坏、被清扫或来自旧版本时，不能凭空恢复缺失现场。
- 文件 flush 与进程异常捕获不构成断电零丢失保证。日志失败仍保持 best-effort，并通过独立健康状态暴露。
- 后台读取可取消；读取索引连接在异常时也会关闭。后台文件任务在进程正常退出时完成收尾，避免临时 SQLite 文件被提前清理。

## 验证

最终全量测试：**1863 passed、2 skipped**，耗时 63.60 秒，进程正常退出。完整结果见 `artifacts/logging-review/tests.xml`。新增行为测试位于：

- `tests/test_logging_reliability.py`：会话事件、同秒轮转、各通道脱敏、sink 重试、保留边界、原运行导出、解析失败导出、分页、测试隔离、成功路径不仲裁失败。
- `tests/test_logging_viewer_reliability.py`：历史/实时交接去重、过期查询丢弃、跨会话分组、警告不跨 attempt 合并、显示队列上限与丢弃计数。

另有既有状态、事务、字幕、解析、事件契约、线程异常和界面测试。两个既有跳过项分别为本机不允许创建符号链接，以及测试占位的 `apply_subtitle_delivery()` 尚未存在；没有将它们记为通过。

压力探针可复现：

```powershell
.venv/Scripts/python.exe scripts/check_logging_viewer.py
```

它使用独立临时目录，加载 10,000 条历史记录，再注入 3,000 条实时记录。最终验证结果为：文本 1000 条、时间线 3000 条；10ms 界面定时器 p95 12.28ms、最大间隔 28.89ms。结果和截图位于 `artifacts/logging-review/performance.json`、`viewer.png`。这是 Qt offscreen 的事件循环验证，排除构造窗口时间，不等价于真实下载并发或打包 EXE 的性能验收。

翻译审计和本次修改文件的 Ruff 检查通过。全仓 Ruff 仍报告既有的 `core/config_manager.py:165` 重复键 `skipped_pre_version`；该项不属于本次日志改动，未混入修复。

原始日志验证中，89 份文件保持哈希不变，2 份当天日志由仍在运行的旧应用进程追加公告请求记录。因此最终隔离结论采用独立测试标记验证：标记写入 pytest 临时日志，未出现在项目应用日志；不把并发运行时的全目录哈希宣称为静态不变。

未进行真实网站下载、认证故障复现、安装/便携 EXE 构建与重启验收。当前运行中的旧进程需要在用户下次正常启动应用后才会加载这些源码改动。
