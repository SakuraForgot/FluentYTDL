from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, QTimer, Signal

from ...models.video_task import VideoTask

logger = logging.getLogger(__name__)


class PlaylistModelRoles:
    """定义 Playlist Model 的自定义数据角色常量。"""

    TaskObjectRole = Qt.ItemDataRole.UserRole + 1


class PlaylistListModel(QAbstractListModel):
    """
    分离数据的 QAbstractListModel 层，用来在 QListView 的可见视口中懒渲染大量视频条目。
    完全与 Widget 解耦，UI 逻辑全部转移到此 Model 与 Delegate 中进行组合。
    """

    # 暴露出一些高频信号通知 UI 面板（如顶部的汇总勾选框状态）
    selection_changed = Signal()

    def __init__(self, parent: Any | None = None) -> None:
        super().__init__(parent)
        self._tasks: list[VideoTask] = []

        # 脏行缓冲池 + 防抖定时器：将高频细粒度 dataChanged 聚合为低频批量发射
        self._dirty_rows: set[int] = set()
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.setInterval(200)  # 200ms 聚合窗口（与 UpdateAggregator 设计对齐）
        self._update_timer.timeout.connect(self._flush_updates)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._tasks)

    def addTask(self, task: VideoTask) -> None:
        """在末尾添加一只 Task，这会立刻通知视图层进行行扩容。"""
        self.beginInsertRows(QModelIndex(), len(self._tasks), len(self._tasks))
        self._tasks.append(task)
        self.endInsertRows()

    def addTasks(self, tasks: list[VideoTask]) -> None:
        if not tasks:
            return
        start = len(self._tasks)
        end = start + len(tasks) - 1
        self.beginInsertRows(QModelIndex(), start, end)
        self._tasks.extend(tasks)
        self.endInsertRows()

    def updateTask(self, row: int, info_dict: dict[str, Any] | None = None) -> None:
        """
        当底层 AsyncExtractor 获取到该行的明细数据时（或者报错时），更新指定行的数据，
        然后再发射数据变更信号告知 QListView 的该格重绘。
        """
        if row < 0 or row >= len(self._tasks):
            return

        task = self._tasks[row]

        # 提取或合并数据逻辑
        if info_dict is not None:
            # 此处应该将原始数据防腐化处理
            from ...models.video_task import VideoTask

            updated_task = VideoTask.from_raw_dict(task.url, info_dict)

            # 保留原有的关键状态 (选中状态)
            updated_task.selected = task.selected

            self._tasks[row] = updated_task

        # 标记脏行，由防抖定时器统一刷新
        self.mark_row_dirty(row)

    def markTaskError(self, row: int, error_msg: str) -> None:
        """将某行标记为出错状态并重绘"""
        if row < 0 or row >= len(self._tasks):
            return
        task = self._tasks[row]
        task.has_error = True
        task.error_msg = error_msg
        task.is_parsing = False

        self.mark_row_dirty(row)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        # 只服务自身的第一列（也是唯一一列）的定制角色
        if not index.isValid() or index.row() >= len(self._tasks):
            return None

        task = self._tasks[index.row()]

        if role == PlaylistModelRoles.TaskObjectRole:
            return task

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        # 由于我们使用定制的 Delegate 捕获鼠标事件来切换 Selected 或点击 Action 表单
        # 所以这里开启基础的 Selectable 和 Enabled
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def get_task(self, index: QModelIndex) -> VideoTask | None:
        row = index.row()
        if 0 <= row < len(self._tasks):
            return self._tasks[row]
        return None

    def get_all_tasks(self) -> list[VideoTask]:
        return list(self._tasks)

    def toggle_selection(self, row: int) -> None:
        if 0 <= row < len(self._tasks):
            task = self._tasks[row]
            task.selected = not task.selected
            self.mark_row_dirty(row)
            self.selection_changed.emit()

    def set_all_selected(self, selected: bool) -> None:
        if not self._tasks:
            return

        for t in self._tasks:
            t.selected = selected

        start = self.index(0, 0)
        end = self.index(len(self._tasks) - 1, 0)
        self.dataChanged.emit(start, end, [PlaylistModelRoles.TaskObjectRole])
        self.selection_changed.emit()

    def clear(self) -> None:
        self.beginResetModel()
        self._tasks.clear()
        self._dirty_rows.clear()
        self.endResetModel()

    def mark_row_dirty(self, row: int) -> None:
        """将行标记为脏。使用前沿触发窗口：第一次标记启动定时器，后续标记在窗口内积累，
        定时器触发时统一发射，避免持续更新导致定时器永远无法触发。"""
        if row < 0 or row >= len(self._tasks):
            return
        self._dirty_rows.add(row)
        if not self._update_timer.isActive():
            self._update_timer.start()  # 仅首次启动，确保最多 150ms 内强制刷新一次

    def _flush_updates(self) -> None:
        """定时器触发：将积累的脏行合并为若干连续区间，一次性发射最少量的 dataChanged 信号。"""
        if not self._dirty_rows:
            return
        rows = sorted(self._dirty_rows)
        self._dirty_rows.clear()

        block_start = rows[0]
        block_end = rows[0]
        for row in rows[1:]:
            if row == block_end + 1:
                block_end = row
                continue
            start_idx = self.index(block_start, 0)
            end_idx = self.index(block_end, 0)
            self.dataChanged.emit(start_idx, end_idx, [PlaylistModelRoles.TaskObjectRole])
            block_start = row
            block_end = row

        start_idx = self.index(block_start, 0)
        end_idx = self.index(block_end, 0)
        self.dataChanged.emit(start_idx, end_idx, [PlaylistModelRoles.TaskObjectRole])
