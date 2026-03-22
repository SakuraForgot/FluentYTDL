from __future__ import annotations

import logging
from typing import Any

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt

from ...storage.history_service import HistoryRecord, on_history_updated

logger = logging.getLogger(__name__)


class HistoryModelRoles:
    """定义 History Model 的自定义数据角色常量。"""

    RecordObjectRole = Qt.ItemDataRole.UserRole + 1


class HistoryListModel(QAbstractListModel):
    """
    分离数据的 QAbstractListModel 层，用来在 QListView 中展示历史记录。
    支持底层的分页或完整加载，解决 QScrollArea 卡死问题。
    """

    def __init__(self, parent: Any | None = None) -> None:
        super().__init__(parent)
        self._records: list[HistoryRecord] = []
        on_history_updated(self._on_record_updated)

    def _on_record_updated(self, record: HistoryRecord) -> None:
        """接收后台 I/O 校验后的状态变更，局部无损刷新 UI"""
        try:
            row = self._records.index(record)
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [HistoryModelRoles.RecordObjectRole])
        except ValueError:
            pass

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        if parent.isValid():
            return 0
        return len(self._records)

    def set_records(self, records: list[HistoryRecord]) -> None:
        """重置整个列表的数据"""
        self.beginResetModel()
        self._records = list(records)
        self.endResetModel()

    def add_record(self, record: HistoryRecord) -> None:
        """在最前面插入一条新记录"""
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._records.insert(0, record)
        self.endInsertRows()

    def remove_record(self, record: HistoryRecord) -> bool:
        """从模型中移除一条记录"""
        try:
            row = self._records.index(record)
        except ValueError:
            return False

        self.beginRemoveRows(QModelIndex(), row, row)
        self._records.pop(row)
        self.endRemoveRows()
        return True

    def get_record(self, index: QModelIndex) -> HistoryRecord | None:
        row = index.row()
        if 0 <= row < len(self._records):
            return self._records[row]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or index.row() >= len(self._records):
            return None

        record = self._records[index.row()]

        if role == HistoryModelRoles.RecordObjectRole:
            return record

        return None

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags

        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def total_size(self) -> int:
        return sum(r.file_size for r in self._records if r.file_exists)

    def existing_count(self) -> int:
        return sum(1 for r in self._records if r.file_exists)

    def clear(self) -> None:
        self.beginResetModel()
        self._records.clear()
        self.endResetModel()
