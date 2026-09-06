from __future__ import annotations

from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer

from ...download.workers import DownloadWorker
from ...storage.task_db import TERMINAL_STATES, task_db
from .file_probe import FileExistenceProbe
from .task_row import TaskRow


class DownloadListModel(QAbstractListModel):
    """
    纯内存数据层模型，用于在 QListView 中高效渲染上万个下载任务卡片。

    **行有两种来源**，都是 `TaskRow`（见 `ui/models/task_row.py`）：

    * **活任务** —— 由 `add_task()` 从 worker 建行，插在 row 0（新的在最上面）；
    * **历史行** —— 由 `fetchMore()` 从 `tasks` 表按 keyset 分页追加到末尾，每页 100 条，
      只取终态（`TERMINAL_STATES`）。未完成态一律由
      `download_manager.load_unfinished_tasks()` 在启动时注入成活任务，两边不许交叉。

    **`db_id` 是唯一去重键**：一个任务在本次会话里下完，既是活任务又已经落库成终态行。
    `add_task()` 命中 `_by_db_id` 就**就地给已有行挂 worker**而不新增行 —— 融合前
    「同一个任务被列两次」的根因就在这里消除。

    **不持有选中状态**：选中态归 `QItemSelectionModel` 所有（见
    `ui/views/task_list_view.py` 与 `ui/unified_task_list_page.py`）。模型里再存一份
    `is_selected` 会立刻和 proxy 的筛选打架 —— 旧的「全选」就是因此绕过筛选、
    直接把 source 全表标记为选中的。
    """

    # 脏行聚合窗口（毫秒）。前沿触发：第一次标记脏行时启动定时器，窗口内的后续标记
    # 只入集合，到点一次性合并成最少的 dataChanged 区间发射。
    # 与 PlaylistListModel._flush_updates 同一套语义，不要再造第三套节流。
    _UPDATE_INTERVAL_MS = 150

    # 一页历史行。100 行一页与 `query_tasks` 的默认值一致。
    _PAGE_SIZE = 100
    # 单次 fetchMore 最多连续查几页。已经作为活任务在列的行会被跳过，极端情况下一整页
    # 都是重复的（刚恢复了 100 个未完成任务又全下完了），插入 0 行会让视图不再触发
    # fetchMore 而卡住；所以就地续查，但要有上限，别一次把整张表扫完。
    _MAX_PAGE_SCANS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: list[TaskRow] = []

        # db_id -> TaskRow。存**对象**不存行号：新任务插在 row 0 会让所有既有行号平移。
        self._by_db_id: dict[int, TaskRow] = {}

        # id(task_row) -> row。结构变更（插入/删除/清空）只置脏标记，真正重建推迟到
        # 下一次查询 —— 否则「插入到 row 0」会让批量恢复 N 个任务退化成 O(n²)。
        self._row_of: dict[int, int] = {}
        self._row_map_dirty = True

        # thumbnail url -> {id(task_row)}：图片到达时 O(命中数) 定位受影响行，
        # 且同一封面被多行共用时每一行都会刷新。
        self._thumb_owners: dict[str, set[int]] = {}

        # 历史行分页游标：上一页最后一行的 `(updated_at, id)`；None = 还没取过第一页。
        self._page_key: tuple[float, int] | None = None
        self._has_more = True

        # 脏任务缓冲池。存的是 id(task_row) 而不是行号 —— 新任务插入在 row 0 会让
        # 所有既有行号平移，缓存行号必然指向错误的行。
        self._dirty_tasks: set[int] = set()
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(self._UPDATE_INTERVAL_MS)
        self._update_timer.timeout.connect(self._flush_updates)

        # 文件存在性：后台 stat，结论写回 `TaskRow.file_exists`。
        # 绝不在 GUI 线程里逐行 os.path.exists —— 那是历史页「打开就白屏」的原因
        # （`history_service.validated_records`）。
        self._probe = FileExistenceProbe(self)
        self._probe.checked.connect(self._apply_existence)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._tasks)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._tasks):
            return None

        task = self._tasks[index.row()]

        # **约定：`UserRole` 返回整个 `TaskRow` 对象**，不做逐字段的角色拆分。
        # 这条约定来自已删除的 `HistoryListModel.RecordObjectRole`（`UserRole + 1`），
        # 保留它的理由是 delegate 一次 paint 要读十几个字段，逐字段过一遍 `data()`
        # 就是十几次 `index.data()` 往返；而 `dataChanged` 只需带上这一个角色。
        if role == Qt.ItemDataRole.UserRole:
            return task

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # === 行索引维护 ===

    def _row_of_id(self, task_id: int) -> int:
        """O(1) 反查行号；结构变更后的第一次调用才重建映射表。不存在返回 -1。"""
        if self._row_map_dirty:
            self._row_of = {id(t): i for i, t in enumerate(self._tasks)}
            self._row_map_dirty = False
        return self._row_of.get(task_id, -1)

    def row_of_db_id(self, db_id: int) -> int:
        """按 `tasks.id` 反查行号。不在列表里返回 -1。"""
        row_obj = self._by_db_id.get(int(db_id))
        if row_obj is None:
            return -1
        return self._row_of_id(id(row_obj))

    def rows_for_thumbnail(self, url: str) -> list[int]:
        """返回所有使用该缩略图 url 的行号（同一封面可能被多行共用）。"""
        owners = self._thumb_owners.get(url)
        if not owners:
            return []
        return [row for row in (self._row_of_id(tid) for tid in owners) if row >= 0]

    def _register_thumbnail(self, row_obj: TaskRow) -> None:
        url = row_obj.effective_thumbnail
        if url:
            self._thumb_owners.setdefault(url, set()).add(id(row_obj))

    # === 历史行分页（Qt 原生增量加载）===

    def canFetchMore(self, parent: QModelIndex = QModelIndex()) -> bool:  # noqa: B008, N802
        if parent.isValid():
            return False
        return self._has_more

    def fetchMore(self, parent: QModelIndex = QModelIndex()) -> None:  # noqa: B008, N802
        """从 `tasks` 表补一页终态历史行，追加到列表末尾。

        只取 `TERMINAL_STATES`：未完成态归 `download_manager.load_unfinished_tasks()`，
        它会把 running/parsing/queued 一律降级成 paused 再注入，分页要是也取一遍，
        同一个任务就会既是活任务又是历史行。

        已经在列的 `db_id` 直接跳过 —— 本次会话下完的任务此刻两边都算得上，
        以已有的活任务行为准。
        """
        if parent.isValid() or not self._has_more:
            return

        fresh: list[TaskRow] = []
        for _ in range(self._MAX_PAGE_SCANS):
            raw = task_db.query_tasks(
                states=TERMINAL_STATES,
                limit=self._PAGE_SIZE,
                before_key=self._page_key,
            )
            if not raw:
                self._has_more = False
                break
            self._page_key = task_db.page_key(raw[-1])
            if len(raw) < self._PAGE_SIZE:
                self._has_more = False

            for db_row in raw:
                db_id = int(db_row.get("id") or 0)
                if db_id in self._by_db_id:
                    continue
                fresh.append(TaskRow.from_db_row(db_row))

            if fresh or not self._has_more:
                break

        if not fresh:
            return

        start = len(self._tasks)
        self.beginInsertRows(QModelIndex(), start, start + len(fresh) - 1)
        for row_obj in fresh:
            self._tasks.append(row_obj)
            if row_obj.db_id > 0:
                self._by_db_id[row_obj.db_id] = row_obj
            self._register_thumbnail(row_obj)
        self._row_map_dirty = True
        self.endInsertRows()

        # 这一页的文件存在性交给后台线程。分页本身就是惰性的（视图滚到底才补一页），
        # 所以「补进来就查」等价于「只查用户看得到的行」，而且不需要再去盯视口。
        self._probe_rows(fresh)

    # === 数据操作接口 ===

    def add_task(self, worker: DownloadWorker, title: str, thumbnail: str) -> None:
        """登记一个活任务。已有同 `db_id` 的历史行就**就地升级**，不新增行。"""
        db_id = int(getattr(worker, "db_id", 0) or 0)
        existing = self._by_db_id.get(db_id) if db_id > 0 else None
        if existing is not None:
            # 分页先补进了这条历史行，用户随后重试它 —— 挂上 worker 即可，
            # 新增一行就是「同一个任务列两次」。
            existing.attach_worker(worker)
            self._probe.forget(existing.db_id)
            if title:
                existing.title = title
            if thumbnail:
                existing.thumbnail = thumbnail
            self._register_thumbnail(existing)
            self._bind_worker_signals(worker, existing)
            row = self._row_of_id(id(existing))
            if row >= 0:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])
            return

        # 我们插入到头部 (类似于队列，先添加的在最上面)
        row = 0
        self.beginInsertRows(QModelIndex(), row, row)

        row_obj = TaskRow.from_worker(worker, title=title, thumbnail=thumbnail)
        self._tasks.insert(row, row_obj)
        self._row_map_dirty = True
        if row_obj.db_id > 0:
            self._by_db_id[row_obj.db_id] = row_obj
        self._register_thumbnail(row_obj)
        self.endInsertRows()

        # 绑定 Worker 信号到 Model 变动
        self._bind_worker_signals(worker, row_obj)

    def rebind_worker(self, row: int, worker: DownloadWorker) -> None:
        """
        替换某一行的 worker 并重新绑定信号。

        `controller.handle_pause_resume_task` 会因为「已结束的 QThread 不能重启」
        而用 restore_db_id **重建** worker；`handle_start_snapshot` 则是给一条历史行
        第一次挂上 worker（就地升级成活任务）。两种情况 UI 都必须换掉该行的 worker
        并重绑信号，否则该行永远停在旧对象的终态上。
        """
        row_obj = self.get_task(row)
        if row_obj is None:
            return
        row_obj.attach_worker(worker)
        self._probe.forget(row_obj.db_id)
        # restore_db_id 走的是同一个主键，但重建时万一拿到了新的 DB 行，索引要跟着搬。
        new_db_id = int(getattr(worker, "db_id", 0) or 0)
        if new_db_id > 0 and self._by_db_id.get(new_db_id) is not row_obj:
            self._by_db_id.pop(row_obj.db_id, None)
            row_obj.db_id = new_db_id
            self._by_db_id[new_db_id] = row_obj
        self._bind_worker_signals(worker, row_obj)
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])

    def _bind_worker_signals(self, worker: DownloadWorker, row_obj: TaskRow) -> None:
        """
        核心隔离：让底层的 QThread(Worker) 与 Model 的单行数据结构绑定。
        无论 Worker 发出什么信号，都只是让该行进入脏行池，由 150ms 聚合窗口
        合并成最少的 dataChanged 区间后统一发射。
        """
        task_id = id(row_obj)

        def trigger_repaint_throttled(*args, **kwargs):
            """高频信号 → 只入脏行池。不做 O(n) 行号反查，不做逐行发射。"""
            self._dirty_tasks.add(task_id)
            if not self._update_timer.isActive():
                # 前沿触发：仅首次启动，保证聚合窗口内必定刷新一次
                self._update_timer.start()

        def trigger_repaint_immediate(*args, **kwargs):
            """终态信号（completed / error / cancelled）→ 立即重绘，用户期望即时反馈。"""
            row = self._row_of_id(task_id)
            if row < 0:
                return
            # 撤销可能已排队的延迟重绘，避免终态之后再补一次多余的发射
            self._dirty_tasks.discard(task_id)
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])
            # 刚落地的文件当然存在，但这一次 stat 是给「用户稍后去外面删掉它」留的锚：
            # 没有这次记录，重启前这一行的 file_exists 会一直是 None（= 按存在渲染）。
            self._probe_rows([row_obj], force=True)

        # High-frequency signals → throttled
        worker.progress.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        worker.status_msg.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        worker.unified_status.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        # Terminal signals → immediate (user expects instant feedback)
        worker.completed.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)

    def _flush_updates(self) -> None:
        """把窗口内累积的脏行解析成行号，合并为最少的连续区间后发射 dataChanged。"""
        if not self._dirty_tasks:
            return
        pending = self._dirty_tasks
        self._dirty_tasks = set()

        # 已被移除的任务解析为 -1，自然丢弃
        rows = sorted({row for row in (self._row_of_id(tid) for tid in pending) if row >= 0})
        if not rows:
            return

        block_start = block_end = rows[0]
        for row in rows[1:]:
            if row == block_end + 1:
                block_end = row
                continue
            self._emit_row_range(block_start, block_end)
            block_start = block_end = row
        self._emit_row_range(block_start, block_end)

    def _emit_row_range(self, start: int, end: int) -> None:
        self.dataChanged.emit(self.index(start, 0), self.index(end, 0), [Qt.ItemDataRole.UserRole])

    def remove_task(self, row: int) -> None:
        if 0 <= row < len(self._tasks):
            row_obj = self._tasks[row]
            task_id = id(row_obj)
            # 清理索引与脏行池，避免 id 复用后串行到新任务上
            self._dirty_tasks.discard(task_id)
            if row_obj.db_id > 0 and self._by_db_id.get(row_obj.db_id) is row_obj:
                self._by_db_id.pop(row_obj.db_id, None)
            self._probe.forget(row_obj.db_id)
            thumb = row_obj.effective_thumbnail
            owners = self._thumb_owners.get(thumb)
            if owners is not None:
                owners.discard(task_id)
                if not owners:
                    self._thumb_owners.pop(thumb, None)

            self.beginRemoveRows(QModelIndex(), row, row)
            self._tasks.pop(row)
            self._row_map_dirty = True
            self.endRemoveRows()

    def get_task(self, row: int) -> TaskRow | None:
        if 0 <= row < len(self._tasks):
            return self._tasks[row]
        return None

    # === 状态汇总统计 (适配顶部 Pivot) ===

    def get_counts_by_state(self) -> dict[str, int]:
        """按 `effective_state` 计数。**历史行也算**（它们没有 worker，只有快照状态）。"""
        counts = {
            "all": 0,
            "running": 0,
            "queued": 0,
            "paused": 0,
            "quality_guard": 0,
            "completed": 0,
            "error": 0,
            "cancelled": 0,
        }
        for row_obj in self._tasks:
            state = row_obj.effective_state
            counts[state] = counts.get(state, 0) + 1
            counts["all"] += 1

        return counts

    # === 文件存在性（后台 stat）===

    def _probe_rows(self, rows: list[TaskRow], force: bool = False) -> None:
        """把这些行里「已完成且有输出路径」的排进后台检查。

        **只查 `completed`**，不查 `error` / `cancelled`：失败的任务本来就没有成品，
        查出来必然是「不存在」，而 delegate 对 `file_exists is False` 会整行降透明 ——
        那会让每一条失败记录都变成半透明，用户读到的是「这行坏了」而不是「下载失败」。
        未完成态更不能查：文件还在沙盒临时目录里，主路径此刻一定不存在。

        `force=False` 时跳过已有结论的行 —— 分页补进来的每一页只 stat 一次，
        滚动过程中不会反复查同一行。
        """
        items: list[tuple[int, str]] = []
        for row_obj in rows:
            if row_obj.db_id <= 0:
                continue
            if not force and row_obj.file_exists is not None:
                continue
            if row_obj.effective_state != "completed":
                continue
            path = row_obj.effective_output_path
            if not path:
                # 没记下路径的老记录：没法验，也不该显示成「已丢失」，留 None。
                continue
            items.append((row_obj.db_id, path))
        if items:
            self._probe.request(items)

    def rescan_existence(self) -> None:
        """重扫全部已完成行。供「文件丢失」筛选与手动刷新调用（`force=True`）。"""
        self._probe_rows(self._tasks, force=True)

    def _apply_existence(self, result: dict) -> None:
        """把后台结论写回行，并只为**观感真的会变**的行排重绘。

        `None → True` 在渲染上什么都不改（delegate 只对 `is False` 降透明），所以
        补完一页 100 行不该换来 100 行重绘；只有涉及 `False` 的翻转才发 dataChanged
        —— 这条也是「文件丢失」筛选能自动重新过滤的依据。
        """
        marked = False
        for raw_id, exists in result.items():
            row_obj = self._by_db_id.get(int(raw_id))
            if row_obj is None:
                continue
            # 结果下发之后这一行可能已经重新开始下载了（`attach_worker` 把结论清成
            # None）。这时旧结论说的是旧文件，采纳它会让正在下载的行显示「已丢失」。
            if row_obj.effective_state != "completed":
                continue
            old = row_obj.file_exists
            new = bool(exists)
            row_obj.file_exists = new
            if old == new or (old is None and new):
                # 没变，或者是「未知 → 存在」（渲染上等价）：不值得一次重绘。
                continue
            self._dirty_tasks.add(id(row_obj))
            marked = True
        if marked and not self._update_timer.isActive():
            self._update_timer.start()

    def clear(self) -> None:
        self.beginResetModel()
        for row_obj in self._tasks:
            worker = row_obj.worker
            if worker:
                if hasattr(worker, "stop"):
                    worker.stop()
                elif hasattr(worker, "cancel"):
                    worker.cancel()
        self._tasks.clear()
        self._by_db_id.clear()
        self._row_of.clear()
        self._row_map_dirty = True
        self._thumb_owners.clear()
        self._dirty_tasks.clear()
        self._probe.clear()
        # 分页游标一并回到起点：清空通常伴随 DB 行的删除，重新查一遍才是对的。
        self._page_key = None
        self._has_more = True
        self.endResetModel()
