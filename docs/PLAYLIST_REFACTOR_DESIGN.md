# FluentYTDL 播放列表架构重构设计方案

本文档对 `FluentYTDL` 当前的播放列表架构及其内置加载队列机制进行深度分析，并提出基于 **MVVM (Model-View-ViewModel)** 模式和 **视口感知调度器 (Viewport-Aware Scheduler)** 的重构方案。

## 1. 现状深度分析 (Current State Analysis)

### 1.1 现有架构概览
当前架构属于 **上帝类 (God Class) 模式** 的变体。`DownloadConfigWindow` (View) 承担了过多的职责，不仅负责 UI 布局和交互，还深度耦合了业务逻辑、队列调度、数据解析和错误处理。

-   **View (`DownloadConfigWindow`)**: 超过 3000 行代码，管理了所有的队列状态 (`_detail_fg_queue`, `_thumb_pending` 等) 和定时器。
-   **Model (`PlaylistListModel`)**: 相对贫血，仅作为 `QAbstractListModel` 的数据容器，缺乏业务逻辑。
-   **Logic (`AsyncExtractManager`)**: 负责底层的 `yt-dlp` 调用，但**调度策略**完全由 View 控制。

### 1.2 内置加载队列机制剖析
当前代码中存在至少 **4 套** 独立且逻辑分散的队列机制，这是导致维护困难和性能问题（如闪烁）的根源。

1.  **构建队列 (Build Chunking)**
    -   **机制**: `_process_next_build_chunk`
    -   **逻辑**: 将 `yt-dlp` 返回的 Flat Playlist 大列表切分为 30 条一组的小块，通过 `QTimer` 递归调用分批插入 Model。
    -   **问题**: 虽然避免了 UI 冻结，但逻辑硬编码在 View 中，且插入期间容易与用户的滚动操作冲突。

2.  **详情获取流水线 (Detail Pipeline)**
    -   **机制**: `_pump_detail_pipeline` + `_bg_crawl_tick`
    -   **结构**: 复杂的四级队列设计。
        -   `_detail_fg_queue`: 前台优先队列（用户点击或视口可见区域）。
        -   `_detail_bg_queue`: 后台爬虫队列（自动遍历）。
        -   `_detail_exec_queue`: 待执行缓冲队列。
        -   `_detail_running_rows`: 正在运行集合。
    -   **问题**: 状态同步极度复杂。View 需要手动管理这些队列的增删改查（`remove`, `discard`, `append`），极易出现状态不一致（如行号偏移导致的数据错乱）。

3.  **缩略图加载队列 (Thumbnail Queue)**
    -   **机制**: `_process_thumb_queue` + `_pick_best_thumb_index`
    -   **逻辑**: 维护 `_thumb_pending` 列表，手动计算每个 URL 对应的行号，并根据视口中心距离计算优先级。
    -   **问题**: 与 `ImageLoader` 的全局单例逻辑部分重叠，且在 View 中实现了复杂的优先级算法，加重了 UI 线程负担。

4.  **UI 更新队列 (Update Throttling)**
    -   **机制**: `_row_update_timer` + `_pending_row_updates`
    -   **逻辑**: 收集脏行号，80ms 后批量刷新。
    -   **问题**: 频率过高（80ms），且缺乏去重和合并逻辑（如连续的状态变更）。

### 1.3 核心痛点
1.  **高耦合**: UI 代码与复杂的队列调度逻辑纠缠不清，修改 UI 容易破坏队列逻辑，反之亦然。
2.  **状态地狱**: 大量的 `bool` 标志位（`_is_closing`, `_build_is_chunking`, `_bg_crawl_active`, `_lazy_paused`）散落在各处，状态流转难以追踪。
3.  **性能瓶颈**: 所有的调度计算（如“寻找离视口最近的缩略图”）都在主 UI 线程执行，当列表达到数千条时，计算开销会导致界面卡顿。
4.  **闪烁问题**: 由于多条队列独立触发更新，Model 频繁接收碎片化的 `dataChanged` 信号，导致 Delegate 重绘不止。

---

## 2. 重构设计方案 (Refactoring Design)

### 2.1 架构模式：MVVM + Service Layer
引入 **MVVM** 模式将逻辑从 View 中剥离。

-   **View (`DownloadConfigWindow`)**: 只负责布局、事件绑定和渲染。不持有任何队列状态。
-   **ViewModel (`PlaylistViewModel`)**: 持有数据状态（`Observable`），暴露命令（Command），处理业务逻辑。
-   **Service (`PlaylistEngine`)**: 核心调度引擎，封装所有的队列管理和后台任务。

### 2.2 核心组件设计

#### A. 视口感知调度器 (`ViewportAwareScheduler`)
这是重构的核心，用于统一管理所有异步任务（详情获取、缩略图加载）。

*   **职责**: 接收任务请求，根据**优先级分数**动态调整执行顺序。
*   **优先级算法**:
    *   `Priority = BaseScore + ViewportBonus`
    *   **BaseScore**: 用户手动点击 (100) > 视口内自动加载 (50) > 视口附近预加载 (20) > 全局后台爬虫 (10)。
    *   **ViewportBonus**: 距离视口中心越近，分数越高。
*   **实现**: 使用 `PriorityQueue`，并监听 View 的滚动事件更新视口范围。

#### B. 统一数据仓库 (`PlaylistRepository`)
不再直接操作 UI Model 的行号，而是维护一个以 `VideoID` 或 `URL` 为键的哈希表。

*   **结构**: `Dict[str, VideoEntity]`
*   **优势**: 即使 UI 进行排序、过滤，底层数据的引用保持不变，彻底解决“行号偏移”导致的 Bug。

#### C. 更新聚合器 (`UpdateAggregator`)
专门解决闪烁问题。

*   **机制**: 接收来自各个 Worker 的数据更新信号。
*   **缓冲**: 维护一个 200ms - 300ms 的缓冲区。
*   **合并**: 如果同一 ID 在缓冲区内发生多次状态变更（如 `Pending` -> `Parsing` -> `Done`），只保留最后一次状态。
*   **提交**: 缓冲区满或超时后，一次性提交给 ViewModel 更新 UI。

### 2.3 数据流向图 (New Data Flow)

```mermaid
graph TD
    User[用户交互/滚动] -->|Update Viewport| VM[PlaylistViewModel]
    VM -->|Set Priority Scope| Engine[PlaylistEngine]
    
    subgraph "PlaylistEngine (Service Layer)"
        Scheduler[ViewportAwareScheduler]
        Repo[PlaylistRepository]
        Aggregator[UpdateAggregator]
        
        Scheduler -->|Dispatch| Workers[yt-dlp Workers]
        Workers -->|Result| Repo
        Repo -->|Notify Change| Aggregator
    end
    
    Aggregator -->|Batch Signal (200ms)| VM
    VM -->|Sync| Model[PlaylistListModel]
    Model -->|Repaint| View[QListView]
```

---

## 3. 详细实施方案

### 3.1 第一阶段：提取调度逻辑 (Extract Scheduler)
创建一个独立的 `PlaylistScheduler` 类，将 `DownloadConfigWindow` 中的以下变量和逻辑移入：
-   **移出变量**: `_detail_fg_queue`, `_detail_bg_queue`, `_detail_exec_queue`, `_thumb_pending` 等。
-   **输入接口**: `enqueue_task(url, type)`, `set_viewport_range(start, end)`.
-   **输出接口**: `task_finished(id, data)`, `task_failed(id, error)`.

### 3.2 第二阶段：重构数据模型 (Refactor Model)
将基于 `List` 的索引访问改为基于 `ID` 的访问。
-   **当前**: `_playlist_rows[row]["status"]`
-   **目标**: `_task_map[task_id].status`
-   `PlaylistListModel` 仅作为 `_task_map` 的一个有序视图（View Projection）。

### 3.3 第三阶段：引入更新聚合 (Implement Aggregator)
在 Scheduler 和 Model 之间插入 `UpdateAggregator`。
```python
class UpdateAggregator(QObject):
    updates_ready = Signal(list) # list of task_ids

    def __init__(self):
        self._timer = QTimer()
        self._timer.setInterval(200) # 200ms 批处理
        self._pending_ids = set()

    def on_task_updated(self, task_id):
        self._pending_ids.add(task_id)
        if not self._timer.isActive():
            self._timer.start()
            
    def _flush(self):
        self.updates_ready.emit(list(self._pending_ids))
        self._pending_ids.clear()
```

### 3.4 第四阶段：UI 绘制优化 (Delegate Optimization)
优化 `PlaylistItemDelegate`：
-   **双缓冲**: 在 `paint` 方法中，先将内容绘制到一个 `QPixmap` 缓冲区，然后一次性将 Pixmap 绘制到屏幕。
-   **状态精简**: 移除 "解析中..." 这种中间状态的文字渲染，如果数据未就绪，保持 "待加载" 或显示骨架屏 (Skeleton)，避免文字跳变。

## 4. 预期收益

1.  **代码瘦身**: `DownloadConfigWindow` 代码量预计减少 60%，仅保留 UI 初始化和事件转发。
2.  **彻底解决闪烁**: 通过 `UpdateAggregator` 和去重逻辑，UI 重绘频率将降低 80% 以上。
3.  **响应更丝滑**: 复杂的优先级计算移至后台或独立逻辑类，主 UI 线程不再卡顿。
4.  **易于测试**: 独立的 `PlaylistScheduler` 和 `PlaylistViewModel` 可以很容易地编写单元测试，无需启动 GUI。

## 5. 结论

当前的架构在功能上是完备的，但在非功能属性（性能、可维护性）上存在显著缺陷。采用 MVVM + 独立调度器的重构方案，不仅能解决当前的闪烁和卡顿问题，还能为未来支持更大规模的播放列表（如 5000+ 视频）打下坚实基础。
