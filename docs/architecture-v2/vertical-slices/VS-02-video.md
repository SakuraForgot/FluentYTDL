# 新版架构 V2 · VS-02 普通视频下载

2026-09-19，源码静态路径；未联网下载。默认单视频是公共链锚点，其他切片引用它时明确差异。

## 用户动作与调用链

[CONFIRMED/static] 用户提交 URL→`show_selection_dialog`→`DownloadConfigWindow.start_extraction`→`InfoExtractWorker.run`→`YoutubeService.extract_info_for_dialog_sync`→CLI JSON解析→窗口显示格式；选择并确认→`get_selected_tasks`→`downloadRequested`→主窗直接`controller.handle_add_tasks`→manager.create/start→Worker.run→Executor.execute/native→yt-dlp Popen→Feature→Staging→信号/DB/outcome。[主窗746行](../../../src/fluentytdl/ui/reimagined_main_window.py#L746)、[窗口1354行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1354)、[worker168行](../../../src/fluentytdl/download/workers.py#L168)、[Executor469行](../../../src/fluentytdl/download/executor.py#L469)。

```mermaid
flowchart LR
  URL[URL/请求快照] --> PARSE[InfoExtractWorker / JSON]
  PARSE --> SELECT[格式与附属选项/质量预检]
  SELECT --> TASK[Controller/DB task/Worker run]
  TASK --> PROC[Executor attempt/yt-dlp]
  PROC --> TX[reconcile / Feature / verify / commit]
  TX --> RESULT[最终路径/DB enqueue/outcome]
```

| owner / trigger | 前置与转换 | 副作用、资源 | 失败、取消、重试、退出 |
|---|---|---|---|
| InfoExtractWorker，用户解析 | 冻结请求选项，flow尚无task | CLI JSON、缓存读取/写入、cancel Event | 取消静默；异常parse diagnosis/error；关窗请求cancel。 |
| 配置窗口，下载按钮 | 有video_info与选定格式；可弹QualityReportDialog | opts、COOKIE_MODE、质量意图、下载目录；发任务tuple | 无任务/用户拒绝预检不建task；构造异常InfoBar。[1088行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L1088)、[4003行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4003)。 |
| Controller/Manager | 标题避让；insert_task或restore id；绑定flow/task | Worker与pending/active；开始时run identity排队写DB | 并发满则queued；精确片段互斥政策不影响普通候选。[controller95行](../../../src/fluentytdl/core/controller.py#L95)、[manager403行](../../../src/fluentytdl/download/download_manager.py#L403)。 |
| Worker标准分支 | 合并基础和任务opts，enforce cookie，强制noplaylist | UUID txn、字幕迟解析、固定Feature配置、emit_expect | 下载前就可能失败；不能把所有错误叫CDN下载失败。[1174行](../../../src/fluentytdl/download/workers.py#L1174)。 |
| Executor attempt | prepare_attempt与最终文件报告位置已定 | Popen、cookie runfile、结构化进度、角色/PP证据 | 非零体积容忍或YtDlpExecutionError；自动/after_fix留同run并增attempt；cancel按PID终止。[1330行](../../../src/fluentytdl/download/workers.py#L1330)。 |
| Staging / Feature | Executor成功返回 | 当前attempt报告主媒体，reconcile/seal；按固定顺序后处理；verify/build_plan/commit | verify不做全部期望减法；取消门后发布不可中断；失败按phase补偿/保留。[1526行](../../../src/fluentytdl/download/workers.py#L1526)。 |
| Worker终结/Manager桥 | committed后确定最终路径 | emit_actual、cleanup、completed、trace.finish；Manager enqueue result/status | postcommit清理异常保持success；DB写入独立，非同步成功保证。[1615行](../../../src/fluentytdl/download/workers.py#L1615)。 |

全部表项 [CONFIRMED/static]。live pause只在标准on_progress检查点等待，resume仍同run；已结束worker再次开始为新run/新txn。shutdown超时会terminate，不能保证这条链走到正常finally。

## 为什么这样处理

[INFERRED] 解析与下载分开让格式选择发生在持久任务之前；最终媒体路径报告单独落文件规避控制台Unicode损坏；事务隔离避免同名任务和旧失败残骸混入新结果。代价是两次选项构建、多个信号/队列和文件事实可能短暂不一致。

[UNKNOWN] 实际验收须同时比对最终文件、journal、UI、tasks.db及一个run的outcome；画质预检基于元数据，未发现post_verify业务调用，所以不得把completed当作真实分辨率/完整解码已验收。细节见 [错误恢复](../10-error-recovery.md)。
