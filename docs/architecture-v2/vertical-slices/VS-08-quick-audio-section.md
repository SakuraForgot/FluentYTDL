# 新版架构 V2 · VS-08 快速添加、音频与片段变体

2026-09-19，source/static，无产品执行。本切片补足六入口之外的重要选项变体；它们复用媒体执行链，不另造引擎图。

## 快速添加

[CONFIRMED/static] 用户快速下载→`AppController.handle_quick_add_tasks`建立flow并启动QuickAddWorker；worker逐URL调用`extract_info_for_dialog_sync`、`quick_params_to_opts`，发finished_tasks后controller回调交`handle_add_tasks`。[controller184行](../../../src/fluentytdl/core/controller.py#L184)、[QuickAdd51行](../../../src/fluentytdl/core/quick_add_worker.py#L51)。

| owner/trigger | 前置与实际动作 | resources/side effects | failure/exit |
|---|---|---|---|
| QuickAddWorker | frozen请求选项、urls、params、max_playlist_items=500 | 串行解析；单URL生成tuple；列表按auto threshold决定single_worker/expand_all | 多URL某项失败仅日志并继续；唯一URL失败上抛；全部失败error signal；没有cancel Event/API。 |
| 列表策略 | auto默认threshold50；超过500强制截断逐条 | single_worker写noplaylist=False和playlistend；expand写True逐条 | 下游标准Worker却无条件noplaylist=True，形成静态选项契约冲突。[QuickAdd97行](../../../src/fluentytdl/core/quick_add_worker.py#L97)、[worker1186行](../../../src/fluentytdl/download/workers.py#L1186)。 |
| Controller callback | finished_tasks | UI模型/DB下载任务在此后创建；共享flow | QuickAdd解析不是DownloadWorker run；关闭时不能靠download_manager.stop_all认定已取消。 |

[INFERRED] 快速入口减少逐项确认，但批量部分失败可被用户忽略，列表单worker承诺需修正或运行证明。此处记录冲突，不推断特定URL真实会下载几项。

## 音频变体

[CONFIRMED/static] `quick_params_to_opts`对audio_only使用bestaudio/best；设置音频格式/质量时添加FFmpegExtractAudio处理器；非纯音频同时要求提取时keepvideo=True。列表音频行可显式extract_audio=True。[quick_opts6行](../../../src/fluentytdl/utils/quick_opts.py#L6)、[窗口4530行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4530)。不能把所有“纯音频”理解为必经同一extract_audio布尔分支。

[CONFIRMED/static] 最低执行仍为Worker→Executor→yt-dlp及FFmpeg→Staging。语言/字幕/封面等opts可改变附属结果；音频转码改变文件大小，因此非零退出容忍路径对extract_audio不使用原始流体积总和比例。[executor729行](../../../src/fluentytdl/download/executor.py#L729)。失败/取消/同run重试遵循[VS-02](VS-02-video.md)，已结束重下新run。

## 片段变体

```mermaid
flowchart LR
  U[用户时间范围/coarse或precise] --> O[build_section_opts]
  O --> Q[Manager precise互斥 / 标准队列]
  Q --> Y[yt-dlp download-sections / FFmpeg]
  Y --> P[stdout进度 + parts字节监视]
  P --> S[标准事务提交]
```

[CONFIRMED/static] `build_section_opts`生成download_sections以及内部start/end/duration/cut_mode；precise额外force_keyframes_at_cuts。窗口补stream_layout/estimated_bytes、关闭字幕并加clip文件名后缀。[core/section_download160行](../../../src/fluentytdl/core/section_download.py#L160)、[窗口4232行](../../../src/fluentytdl/ui/components/dialogs/download_config_window.py#L4232)。

| owner / control | precondition→transition | resource与失败/退出 |
|---|---|---|
| 时间范围选择器/窗口 | 无有效range→ValueError，不建任务；有效→opts | 片段仅是选项，不能把同目录的lossless_cut/extract_frame函数默认连入当前主下载链。 |
| DownloadManager | 已有precise worker时跳过另一个precise候选 | 普通任务仍可启动；减少同时重编码资源争用，改变严格FIFO顺序。[376行](../../../src/fluentytdl/download/download_manager.py#L376)。 |
| Executor | 有download_sections及parts_probe | 新daemon section-part-progress每0.75s读.parts字节并回调on_progress；proc结束才退出。[483行](../../../src/fluentytdl/download/executor.py#L483)、[1163行](../../../src/fluentytdl/download/executor.py#L1163)。 |
| Worker/CleanLogger | 标准on_progress共用 | pause可挡回调，不代表OS暂停FFmpeg；两线程可更新同进度对象；取消终止yt-dlp进程树。[worker1276行](../../../src/fluentytdl/download/workers.py#L1276)。 |

上述 [CONFIRMED/static]。片段使最终体积合法小于全片，所以size_changing_opts豁免简单体积比；代价是不能据此证明片段精确起止与质量。[INFERRED] 必须通过实际时长/边界帧验收，进度达到100%并不证明剪切精度。

[UNKNOWN] 未验证QuickAdd取消/部分失败反馈、single_worker契约、音频多产物、precise并发、片段静默进度线程结束和剪切精度。当前所有路径最终交付仍由Staging，不能因变体直接往用户目录写临时文件。
