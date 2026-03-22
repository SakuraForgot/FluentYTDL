# 播放列表闪烁问题分析报告

本文档针对 FluentYTDL 中播放列表在 `yt-dlp` 返回新数据时出现的闪烁现象进行深入分析，定位根本原因并提供优化建议。

## 1. 现象描述

当播放列表处于“详情获取”阶段（即后台 `yt-dlp` 进程不断返回视频的分辨率、格式等详细信息）时，列表界面会出现高频的视觉闪烁。这通常表现为：
-   列表项的文本或图标瞬间消失又出现。
-   背景高亮或边框出现抖动。
-   在滚动时感觉界面“跳跃”或不流畅。

## 2. 根本原因分析

经过代码审查，闪烁的核心原因在于 **高频的 Model 更新触发了全量的 Delegate 重绘**，且更新频率与渲染机制之间存在冲突。

### 2.1 数据更新链路
当后台 `AsyncExtractManager` 完成一个视频的解析时，数据流向如下：

1.  **Worker 完成**：`_on_extract_task_finished` 被调用。
2.  **数据回填**：更新内存中的 `_playlist_rows` 数据（缩略图、格式列表）。
3.  **应用预设**：调用 `_auto_apply_row_preset` 计算最佳格式。
4.  **通知更新**：`_PlaylistModelRowProxy` 标记数据脏（Dirty），调用 `_schedule_playlist_row_update`。
5.  **定时器触发**：`_row_update_timer`（80ms 间隔）超时，调用 `_flush_playlist_row_updates`。
6.  **发射信号**：`PlaylistListModel` 发射 `dataChanged` 信号。
7.  **视图重绘**：`QListView` 收到信号，调度 `PlaylistItemDelegate.paint` 重绘受影响的行。

### 2.2 造成闪烁的具体因素

#### A. 更新频率过高 (High Frequency Updates)
-   虽然有 80ms 的节流（Throttling），但如果 `yt-dlp` 并发数较高（默认为 2-4），且解析速度较快，更新可能会恰好卡在定时器触发点，导致连续不断的重绘请求。
-   **双重更新**：
    -   第一次：`_schedule_deferred_parsing_indicator` 在 300ms 时触发，将状态改为“解析中…”。
    -   第二次：解析完成（假设在 400ms），状态更新为具体格式。
    -   这种短时间内的连续状态变化会导致同一行在短时间内被重绘两次。

#### B. 委托重绘机制 (Delegate Repaint)
-   `PlaylistItemDelegate.paint` 方法执行的是**全量绘制**。每次 `dataChanged` 触发时，它会清除背景、重画边框、重画缩略图、重画所有文字。
-   **背景透明度**：列表项的背景色使用了半透明颜色（如 `QColor(255, 255, 255, 7)`）。在 Qt 中，半透明对象的重绘如果涉及到底层缓冲区的清除和合成，且频率极高，容易产生肉眼可见的“闪烁”或混叠伪影。
-   **缩略图重缩放**：虽然有 `_scaled_cache`，但代码逻辑中，当 `set_pixmap` 被调用时（通常伴随着解析完成），会清除旧缓存。如果在绘制周期内缓存恰好被清空，Delegate 可能会绘制一帧空白或占位符，下一帧才绘制图片，导致视觉上的“闪白”。

#### C. 缩略图加载的独立更新
-   除了 `yt-dlp` 的数据返回，`ImageLoader` 也在异步加载缩略图。
-   `_on_thumb_loaded_with_url` 会触发 `_schedule_playlist_row_update`。
-   这意味着一行数据可能因为“详情解析完成”和“缩略图加载完成”被分别触发重绘，加剧了刷新频率。

## 3. 关键代码证据

### 3.1 定时器节流逻辑 (`DownloadConfigWindow.py`)
```python
self._row_update_timer.setInterval(80)  # 80ms 间隔
...
def _flush_playlist_row_updates(self):
    ...
    model.dataChanged.emit(start_idx, end_idx, ...)
```
80ms 意味着每秒最多重绘 12.5 次。对于动画来说这很低，但对于静态列表的“内容突变”来说，如果内容不断跳变，用户会明显感知到。

### 3.2 延迟状态更新 (`DownloadConfigWindow.py`)
```python
def _schedule_deferred_parsing_indicator(self, row: int):
    # 300ms 后强制刷新显示 "解析中..."
    QTimer.singleShot(300, _apply)
```
这个逻辑原本是为了避免快速解析时的闪烁，但对于中等耗时的任务（如 400-500ms），反而人为增加了一次“中间态”的重绘。

## 4. 优化建议

1.  **增加节流阈值**：将 `_row_update_timer` 的间隔从 80ms 增加到 200ms 或 300ms，以减少单位时间内的重绘次数。
2.  **局部更新优化**：在 Delegate 中，尽量避免在 `dataChanged` 时清除缩略图缓存，除非 URL 确实变了。
3.  **消除中间态**：如果可能，移除或延长 `_schedule_deferred_parsing_indicator` 的触发时间（例如 1000ms），让大多数任务直接从“待加载”变为“结果”，跳过“解析中”的绘制。
4.  **双缓冲绘制**：确保 Delegate 绘制到 `QPixmap` 缓冲区，然后一次性贴图，而不是直接在 `QPainter` 上分层绘制（虽然 Qt 自身有双缓冲，但复杂的半透明合成仍可能出问题）。

## 5. 结论

当前播放列表的闪烁主要是由于 **异步数据流（yt-dlp详情 + 图片加载）** 产生的多源高频更新信号，经过一个 **频率较高（80ms）的节流器** 后，驱动 **全量重绘的 Delegate** 所导致的。优化更新策略和合并重绘请求是解决此问题的关键。
