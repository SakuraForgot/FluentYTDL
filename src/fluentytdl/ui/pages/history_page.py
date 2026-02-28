"""
历史记录页面

展示下载历史，支持搜索、文件验证和清理。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    SearchLineEdit,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ...storage.history_service import HistoryRecord, history_service
from ..components.history_item_widget import HistoryItemWidget


class HistoryPage(QWidget):
    """下载历史记录页面"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("historyPage")
        self._cards: list[HistoryItemWidget] = []
        self._init_ui()

        # 延迟加载历史（给 UI 时间渲染）
        QTimer.singleShot(500, self.reload)

    def _init_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # --- 标题行 ---
        self.title_label = SubtitleLabel("下载历史", self)
        layout.addWidget(self.title_label)

        # --- 工具栏: 搜索 + 操作按钮 ---
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)

        self.search_box = SearchLineEdit(self)
        self.search_box.setPlaceholderText("搜索历史记录...")
        self.search_box.setFixedWidth(280)
        self.search_box.textChanged.connect(self._on_search)
        toolbar.addWidget(self.search_box)

        toolbar.addStretch(1)

        # 统计标签
        self.stats_label = BodyLabel("", self)
        self.stats_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        toolbar.addWidget(self.stats_label)

        # 刷新
        refresh_btn = TransparentToolButton(FluentIcon.SYNC, self)
        refresh_btn.setToolTip("刷新列表")
        refresh_btn.installEventFilter(
            ToolTipFilter(refresh_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        refresh_btn.clicked.connect(self.reload)
        toolbar.addWidget(refresh_btn)

        # 清理无效
        clean_btn = TransparentToolButton(FluentIcon.BROOM, self)
        clean_btn.setToolTip("清理文件丢失的记录")
        clean_btn.installEventFilter(
            ToolTipFilter(clean_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        clean_btn.clicked.connect(self._on_clean)
        toolbar.addWidget(clean_btn)

        # 清空全部
        clear_btn = TransparentToolButton(FluentIcon.DELETE, self)
        clear_btn.setToolTip("清空所有历史")
        clear_btn.installEventFilter(
            ToolTipFilter(clear_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        clear_btn.clicked.connect(self._on_clear_all)
        toolbar.addWidget(clear_btn)

        layout.addLayout(toolbar)

        # --- 分割线 ---
        # --- 分割线 ---
        self.line = QFrame(self)
        self.line.setFrameShape(QFrame.Shape.HLine)
        self.line.setFrameShadow(QFrame.Shadow.Plain)
        layout.addWidget(self.line)

        # --- 列表 ScrollArea ---
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.scroll_widget = QWidget()
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(6)
        self.scroll_layout.addStretch(1)

        self.scroll_area.setWidget(self.scroll_widget)
        layout.addWidget(self.scroll_area, 1)

        # --- 空状态 ---
        self.empty_placeholder = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_placeholder)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(16)

        self.empty_icon = QLabel(self.empty_placeholder)
        self.empty_icon.setText("📂")
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_title = SubtitleLabel("暂无历史记录", self.empty_placeholder)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_desc = BodyLabel("下载完成的视频将显示在这里", self.empty_placeholder)
        self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_desc.setTextColor(QColor(96, 96, 96), QColor(206, 206, 206))

        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_icon)
        empty_layout.addWidget(self.empty_title)
        empty_layout.addWidget(self.empty_desc)
        empty_layout.addStretch(1)

        self.empty_placeholder.setVisible(False)
        layout.addWidget(self.empty_placeholder, 1)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        line_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.line.setStyleSheet(f"color: {line_color};")

        icon_color = "rgba(255, 255, 255, 0.1)" if isDarkTheme() else "rgba(0, 0, 0, 0.1)"
        self.empty_icon.setStyleSheet(f"font-size: 64px; color: {icon_color};")

    # ------ 数据操作 ------

    def reload(self) -> None:
        """重新加载历史记录（带文件验证）"""
        records = history_service.validated_records()
        self._populate(records)

    def _populate(self, records: list[HistoryRecord]) -> None:
        """用记录列表填充 UI"""
        # 清空旧卡片
        for card in self._cards:
            self.scroll_layout.removeWidget(card)
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()

        # 创建新卡片
        for rec in records:
            card = HistoryItemWidget(rec, self.scroll_widget)
            card.remove_requested.connect(self._on_remove)
            self._cards.append(card)
            # 插入到 stretch 之前
            self.scroll_layout.insertWidget(self.scroll_layout.count() - 1, card)

        self._update_empty_state()
        self._update_stats()

    def add_record(self, record: HistoryRecord) -> None:
        """实时添加一条新记录（下载完成时调用）"""
        card = HistoryItemWidget(record, self.scroll_widget)
        card.remove_requested.connect(self._on_remove)
        self._cards.insert(0, card)
        self.scroll_layout.insertWidget(0, card)
        self._update_empty_state()
        self._update_stats()

    # ------ 搜索 ------

    def _on_search(self, text: str) -> None:
        kw = text.strip().lower()
        for card in self._cards:
            if not kw:
                card.setVisible(True)
            else:
                card.setVisible(kw in card.record.title.lower())
        self._update_empty_state()

    # ------ 删除 / 清理 ------

    def _on_remove(self, card: HistoryItemWidget) -> None:
        history_service.remove(card.record)
        if card in self._cards:
            self._cards.remove(card)
        self.scroll_layout.removeWidget(card)
        card.setParent(None)
        card.deleteLater()
        self._update_empty_state()
        self._update_stats()

    def _on_clean(self) -> None:
        removed = history_service.remove_missing()
        if removed:
            self.reload()
            InfoBar.success(
                "清理完成",
                f"已移除 {removed} 条无效记录",
                parent=self.window(),
                position=InfoBarPosition.TOP,
                duration=3000,
            )
        else:
            InfoBar.info(
                "无需清理",
                "所有记录对应的文件均存在",
                parent=self.window(),
                position=InfoBarPosition.TOP,
                duration=2000,
            )

    def _on_clear_all(self) -> None:
        if not self._cards:
            return
        box = MessageBox(
            "清空历史记录",
            f"确定清空全部 {len(self._cards)} 条历史记录？\n（不会删除已下载的文件）",
            self.window(),
        )
        if box.exec():
            count = history_service.clear()
            self.reload()
            InfoBar.success(
                "已清空",
                f"已清除 {count} 条历史记录",
                parent=self.window(),
                position=InfoBarPosition.TOP,
                duration=3000,
            )

    # ------ 状态更新 ------

    def _update_empty_state(self) -> None:
        visible = sum(1 for c in self._cards if not c.isHidden())
        self.scroll_area.setVisible(visible > 0)
        self.empty_placeholder.setVisible(visible == 0)

    def _update_stats(self) -> None:
        total = len(self._cards)
        existing = sum(1 for c in self._cards if c.record.file_exists)
        size = history_service.total_size()

        # 格式化大小
        size_str = ""
        if size > 0:
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if size < 1024:
                    size_str = f" · {size:.1f} {unit}"
                    break
                size /= 1024

        if total == existing:
            self.stats_label.setText(f"{total} 条记录{size_str}")
        else:
            self.stats_label.setText(f"{total} 条记录 ({total - existing} 个文件丢失){size_str}")

    def count(self) -> int:
        return len(self._cards)
