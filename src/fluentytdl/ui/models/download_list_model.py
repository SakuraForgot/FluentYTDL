from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer, Signal

from ...download.workers import DownloadWorker

logger = logging.getLogger(__name__)


class DownloadListModel(QAbstractListModel):
    """
    纯内存数据层模型，用于在 QListView 中高效渲染上万个下载任务卡片。
    """

    selection_changed = Signal()

    # Minimum interval (seconds) between dataChanged emissions for the same row
    _REPAINT_INTERVAL = 0.20

    def __init__(self, parent=None):
        super().__init__(parent)
        # List of internal task dictionaries:
        # {"worker": DownloadWorker, "title": str, "thumbnail": str, "is_selected": bool}
        self._tasks: list[dict[str, Any]] = []
        # Throttle: tracks last dataChanged emit time per task_data id
        self._last_repaint: dict[int, float] = {}
        # Pending rows that need a deferred repaint
        self._pending_repaint: set[int] = set()
        self._repaint_timer = QTimer(self)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(200)
        self._repaint_timer.timeout.connect(self._flush_pending_repaints)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._tasks)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._tasks):
            return None

        task = self._tasks[index.row()]

        if role == Qt.ItemDataRole.UserRole:
            return task

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    # === 数据操作接口 ===

    def add_task(self, worker: DownloadWorker, title: str, thumbnail: str) -> None:
        """从主程序追加一个全新的任务进入列队末尾或头部"""
        # 我们插入到头部 (最新任务在最上面)
        row = 0
        self.beginInsertRows(QModelIndex(), row, row)

        task_data = {"worker": worker, "title": title, "thumbnail": thumbnail, "is_selected": False}
        self._tasks.insert(row, task_data)
        self.endInsertRows()

        # 绑定 Worker 信号到 Model 变动
        self._bind_worker_signals(worker, task_data)

    def _bind_worker_signals(self, worker: DownloadWorker, task_data: dict[str, Any]) -> None:
        """
        核心隔离：让底层的 QThread(Worker) 与 Model 的单行数据结构绑定。
        无论 Worker 发出什么信号，都只是强制该行产生 dataChanged 局部重绘。
        高频进度信号被节流至最多每 200ms 一次以避免 UI 闪烁。
        """
        task_id = id(task_data)

        def trigger_repaint_throttled(*args, **kwargs):
            """Throttled repaint for high-frequency signals (progress, status_msg)."""
            try:
                row = self._tasks.index(task_data)
            except ValueError:
                return
            now = time.monotonic()
            last = self._last_repaint.get(task_id, 0.0)
            if now - last >= self._REPAINT_INTERVAL:
                self._last_repaint[task_id] = now
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])
            else:
                # Defer the repaint; the timer will flush it
                self._pending_repaint.add(row)
                if not self._repaint_timer.isActive():
                    self._repaint_timer.start()

        def trigger_repaint_immediate(*args, **kwargs):
            """Immediate repaint for terminal signals (completed, error, cancelled)."""
            try:
                row = self._tasks.index(task_data)
                self._last_repaint[task_id] = time.monotonic()
                self._pending_repaint.discard(row)
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])
            except ValueError:
                pass

        # High-frequency signals → throttled
        worker.progress.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        worker.status_msg.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        worker.unified_status.connect(trigger_repaint_throttled, Qt.ConnectionType.QueuedConnection)
        # Terminal signals → immediate (user expects instant feedback)
        worker.completed.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)
        worker.error.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)
        worker.cancelled.connect(trigger_repaint_immediate, Qt.ConnectionType.QueuedConnection)

    def _flush_pending_repaints(self) -> None:
        """Emit deferred dataChanged for rows that were throttled."""
        rows = [r for r in self._pending_repaint if 0 <= r < len(self._tasks)]
        self._pending_repaint.clear()
        for r in rows:
            idx = self.index(r, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])

    def remove_task(self, row: int) -> None:
        if 0 <= row < len(self._tasks):
            task = self._tasks[row]
            # Clean up throttle state
            task_id = id(task)
            self._last_repaint.pop(task_id, None)
            self._pending_repaint.discard(row)

            self.beginRemoveRows(QModelIndex(), row, row)
            self._tasks.pop(row)
            self.endRemoveRows()

            # 停止它的活性
            worker = task.get("worker")
            if worker and hasattr(worker, "stop"):
                worker.stop()
            elif worker and hasattr(worker, "cancel"):
                worker.cancel()

    def get_task(self, row: int) -> dict[str, Any] | None:
        if 0 <= row < len(self._tasks):
            return self._tasks[row]
        return None

    # === 多选交互接口 ===

    def toggle_selection(self, row: int) -> None:
        if 0 <= row < len(self._tasks):
            self._tasks[row]["is_selected"] = not self._tasks[row]["is_selected"]
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])
            self.selection_changed.emit()

    def set_all_selected(self, selected: bool) -> None:
        if not self._tasks:
            return
        for t in self._tasks:
            t["is_selected"] = selected

        start = self.index(0, 0)
        end = self.index(len(self._tasks) - 1, 0)
        self.dataChanged.emit(start, end, [Qt.ItemDataRole.UserRole])
        self.selection_changed.emit()

    def get_selected_tasks(self) -> list[dict[str, Any]]:
        return [t for t in self._tasks if t.get("is_selected", False)]

    def get_selected_rows(self) -> list[int]:
        return [i for i, t in enumerate(self._tasks) if t.get("is_selected", False)]

    # === 状态汇总统计 (适配顶部 Pivot) ===

    def get_counts_by_state(self) -> dict[str, int]:
        counts = {"all": 0, "running": 0, "queued": 0, "paused": 0, "completed": 0, "error": 0}
        for task in self._tasks:
            worker = task.get("worker")
            if not worker:
                continue

            # 推断当前状态
            state = "queued"
            if worker.isRunning():
                state = "running"
            elif worker.isFinished():
                state = getattr(worker, "_final_state", "completed")
            else:
                s = getattr(worker, "_final_state", "queued")
                if s in ("paused", "error"):
                    state = s

            counts[state] = counts.get(state, 0) + 1
            counts["all"] += 1

        return counts

    def clear(self) -> None:
        self.beginResetModel()
        for task in self._tasks:
            worker = task.get("worker")
            if worker:
                if hasattr(worker, "stop"):
                    worker.stop()
                elif hasattr(worker, "cancel"):
                    worker.cancel()
        self._tasks.clear()
        self.endResetModel()
