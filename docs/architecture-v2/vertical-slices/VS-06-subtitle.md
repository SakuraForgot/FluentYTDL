# 新版架构 V2 · VS-06 独立字幕提取

2026-09-19，source/static，无真实字幕请求。独立字幕和“视频附带字幕”共享语言 helper，但不共享完整执行/重试管线。

[CONFIRMED/static] 主窗`show_subtitle_selection_dialog`打开mode=subtitle；仍用普通/列表解析取得字幕信息，选择器生成opts。`get_selected_tasks`强制skip_download=True、禁用封面/嵌入/metadata/SponsorBlock并清postprocessors。manager照常建持久task，Worker进入`_run_lightweight_extract`。[主窗812行](../../../src/fluentytdl/ui/reimagined_main_window.py#L812)、[窗口4023行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4023)、[worker1168行](../../../src/fluentytdl/download/workers.py#L1168)。

```mermaid
flowchart LR
  UI[字幕选择/语言偏好] --> OPT[skip_download opts]
  OPT --> W[DownloadWorker 轻量分支]
  W --> LANG[迟解析真实语言键]
  LANG --> CLI[build_subtitle_args / yt-dlp Popen]
  CLI --> TX[Staging reconcile / verify无media / commit]
  TX --> DONE[completed / outcome / DB桥]
```

| owner/阶段 | trigger/precondition与实际动作 | resources / side effects | failure、取消、重试、退出 |
|---|---|---|---|
| 配置窗口/选择器 | 用户选字幕、格式、来源；单项或逐行任务 | COOKIE_MODE来自解析快照；不应继承视频嵌入处理 | 任务构造失败在窗口提示，不创建下载run。 |
| Worker `_resolve_subtitle_prefs` | 内部字幕偏好存在 | 利用cached_info或service补字幕信息，保存一次解析结果供复用 | 网络解析在CLI提取前可能失败；不能叫字幕文件传输失败。[899行](../../../src/fluentytdl/download/workers.py#L899)、[949行](../../../src/fluentytdl/download/workers.py#L949)。 |
| `_run_lightweight_extract` | skip_download分支 | expect_opts覆写实际无媒体/不嵌入；`_fastpath_open_staging`；共享`build_subtitle_args(allow_embed=False)`；Popen | 不调用Executor/Feature，stdout检查is_cancelled；无标准自动退避/after_fix循环。[1988行](../../../src/fluentytdl/download/workers.py#L1988)、[2085行](../../../src/fluentytdl/download/workers.py#L2085)。 |
| 子进程rc分流 | stdout结束→wait | 非零用完整diag_lines构造YtDlpExecutionError再诊断；零时扫字幕警告并emit_success_signals | 非零直接failed；不应用标准媒体体积宽恕。用户重试需新worker/run。[2251行](../../../src/fluentytdl/download/workers.py#L2251)。 |
| `_fastpath_land` | 引擎返回成功 | reconcile/seal；verify不要求media；group_stem必要时由保留artifact确定；build_plan/commit/actual/cleanup | 没任何可交付成员会被build_plan拒绝；不能沿用注释“零字幕始终成功”的概括。[1871行](../../../src/fluentytdl/download/workers.py#L1871)、[staging1252行](../../../src/fluentytdl/download/staging.py#L1252)。 |
| 收口/恢复 | 内层返回→外层finally | `_finish_run`；Manager接状态/result | 无主媒体所以快通道不设标准output_path；重启skip_download任务标error且不创建执行恢复壳。[manager236行](../../../src/fluentytdl/download/download_manager.py#L236)。 |

以上 [CONFIRMED/static]。正常取消可终止 `_proc_ref` 并交Staging裁决；但loop只在有新输出时检查取消，EOF后的非零分支未先归一取消，最终分类需验证；pause Event未接入轻量输出loop。

[INFERRED] 轻量通道避免纯字幕承担视频格式选择和Feature成本；统一Staging仍保护同名覆盖、半产物和取消清理。代价是重试、暂停、output_path语义与视频不同，UI不能因都叫DownloadWorker就承诺相同能力。

[UNKNOWN] 应验语言别名→实际caption key、仅人工/自动字幕、缺语言、rc零无文件、半份字幕后非零、静默取消、同名提交及跨重启行为。success/degraded必须看实际产物，不只看退出码。
