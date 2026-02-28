"""
统一任务列表页面

使用 Pivot 过滤器 + 单一 ScrollArea 实现任务管理。
替代原有的四页面分散管理方案。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QLayout,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    FluentIcon,
    SubtitleLabel,
)

if TYPE_CHECKING:
    from .components.download_item_widget import DownloadItemWidget


class UnifiedTaskListPage(QWidget):
    """
    统一任务列表页面

    特性:
    - 单一 ScrollArea 容纳所有任务卡片
    - Pivot 顶部过滤器切换显示
    - 空状态占位符
    - 新任务插入顶部
    """

    # 信号：卡片被请求删除
    card_remove_requested = Signal(object)
    card_resume_requested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("unifiedTaskListPage")

        self._cards: list[DownloadItemWidget] = []
        self._current_filter: str = "all"
        self._is_batch_mode: bool = False

        self._init_ui()

    def _init_ui(self) -> None:
        """初始化 UI 布局"""
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(20, 20, 20, 20)
        self.v_layout.setSpacing(16)

        # === 标题 ===
        self.title_label = SubtitleLabel("任务列表", self)
        self.v_layout.addWidget(self.title_label)

        # === 用于筛选的 SegmentedWidget (胶囊样式) ===
        from PySide6.QtWidgets import QHBoxLayout
        from qfluentwidgets import SegmentedWidget

        self.header_layout = QHBoxLayout()
        self.header_layout.setContentsMargins(0, 0, 0, 0)

        self.pivot = SegmentedWidget(self)
        self.pivot.addItem(routeKey="all", text="全部任务")
        self.pivot.addItem(routeKey="running", text="下载中")
        self.pivot.addItem(routeKey="queued", text="排队中")
        self.pivot.addItem(routeKey="paused", text="已暂停")
        self.pivot.addItem(routeKey="completed", text="已完成")
        self.pivot.setCurrentItem("all")
        self.pivot.currentItemChanged.connect(self._on_pivot_changed)

        self.header_layout.addWidget(self.pivot)
        self.header_layout.addStretch(1)  # 强制左对齐
        self.v_layout.addLayout(self.header_layout)

        # SegmentedWidget 自带容器背景，不再需要额外的分割线，或者保留分割线作为区域划分
        # 用户建议: "去掉下划线...那个蓝绿色的下划线就可以去掉了" -> SegmentedWidget 没有下划线
        # 用户建议: "下方可以有一条贯穿全宽的细分割线" -> 保留分割线作为区域划分

        # === 分割线 (保留以区分区域) ===
        self.pivot_line = QFrame(self)
        self.pivot_line.setFrameShape(QFrame.Shape.HLine)
        self.pivot_line.setFrameShadow(QFrame.Shadow.Plain)
        self.v_layout.addWidget(self.pivot_line)

        # === 任务列表 ScrollArea ===
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet("background: transparent;")

        self.scroll_widget = QWidget()
        self.scroll_widget.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(8)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 性能优化：设置布局约束
        self.scroll_layout.setSizeConstraint(QLayout.SizeConstraint.SetMinAndMaxSize)

        self.scroll_area.setWidget(self.scroll_widget)
        self.v_layout.addWidget(self.scroll_area, 1)

        # === 空状态占位符 (增强版) ===
        from qfluentwidgets import PrimaryPushButton

        self.empty_placeholder = QWidget(self)
        empty_layout = QVBoxLayout(self.empty_placeholder)
        empty_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_layout.setSpacing(16)

        # 使用更大的图标 (FluentIcon.LIBRARY 或自定义图)
        # 这里模拟插画效果，使用较大的 Icon
        self.empty_icon = QLabel(self.empty_placeholder)
        # 实际项目中应加载 SVG/PNG 插画
        # self.empty_icon.setPixmap(...)
        # 暂时用大号 Emoji 或 Icon 替代
        self.empty_icon.setText("🍃")
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)

        text_container = QWidget(self.empty_placeholder)
        text_layout = QVBoxLayout(text_container)
        text_layout.setSpacing(4)

        self.empty_title = SubtitleLabel("暂无任务", text_container)
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_desc = BodyLabel("点击下方按钮新建下载任务", text_container)
        self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_desc.setTextColor(
            QColor(96, 96, 96), QColor(206, 206, 206)
        )  # Secondary text color

        text_layout.addWidget(self.empty_title)
        text_layout.addWidget(self.empty_desc)

        # 行动点按钮 (!Action)
        self.empty_action_btn = PrimaryPushButton(
            FluentIcon.ADD, "新建任务", self.empty_placeholder
        )
        self.empty_action_btn.setFixedWidth(160)
        # 需要连接到 Main Window 的跳转逻辑，这里发射信号或暂留
        # 更好的方式是 MainWindow 监听并连接

        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_icon)
        empty_layout.addWidget(text_container)
        empty_layout.addWidget(self.empty_action_btn)
        empty_layout.addStretch(1)

        self.empty_placeholder.setVisible(False)

        self.v_layout.addWidget(self.empty_placeholder, 1)

        # === 操作按钮区域（由 MainWindow 填充）===
        from PySide6.QtWidgets import QHBoxLayout

        self.action_layout = QHBoxLayout()
        self.action_layout.setContentsMargins(0, 0, 0, 0)
        self.action_layout.setSpacing(8)
        self.action_layout.addStretch()
        self.v_layout.insertLayout(1, self.action_layout)  # 插入到标题下方

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        line_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.pivot_line.setStyleSheet(f"color: {line_color};")

        empty_color = "rgba(255, 255, 255, 0.1)" if isDarkTheme() else "rgba(0, 0, 0, 0.1)"
        self.empty_icon.setStyleSheet(f"font-size: 64px; color: {empty_color};")

    def set_selection_mode(self, enabled: bool) -> None:
        """设置批量选择模式"""
        for card in self._cards:
            if hasattr(card, "set_selection_mode"):
                card.set_selection_mode(enabled)
            if not enabled:
                set_checked = getattr(card, "setChecked", None)
                if callable(set_checked):
                    set_checked(False)

    def select_all(self) -> None:
        """选择所有可见的卡片"""
        for card in self._cards:
            if card.isVisible():
                set_checked = getattr(card, "setChecked", None)
                if callable(set_checked):
                    set_checked(True)

    def get_selected_cards(self) -> list[DownloadItemWidget]:
        """获取所有选中的卡片"""
        return [c for c in self._cards if getattr(c, "isChecked", lambda: False)()]

    def get_visible_cards(self) -> list[DownloadItemWidget]:
        """获取所有可见的卡片"""
        return [c for c in self._cards if c.isVisible()]

    def _on_pivot_changed(self, route_key: str) -> None:
        """Pivot 切换时调用"""
        self.set_filter(route_key)

    def add_card(self, card: DownloadItemWidget) -> None:
        """添加卡片到列表顶部"""
        print(
            f"[DEBUG] UnifiedTaskListPage.add_card: adding card, current_filter={self._current_filter}"
        )

        # 首先设置父组件，确保卡片在正确的 widget 树中
        card.setParent(self.scroll_widget)

        # 连接信号
        card.state_changed.connect(lambda _: self._on_card_state_changed(card))
        card.remove_requested.connect(self._on_card_remove_requested)
        card.resume_requested.connect(self._on_card_resume_requested)

        # 插入到列表顶部
        self._cards.insert(0, card)
        self.scroll_layout.insertWidget(0, card)

        # 强制显示卡片
        card.setVisible(True)
        card.show()

        # 确保 scroll_area 可见
        self.scroll_area.setVisible(True)
        self.empty_placeholder.setVisible(False)

        print(
            f"[DEBUG] UnifiedTaskListPage.add_card: card.state()={card.state()}, card.isVisible()={card.isVisible()}, total cards={len(self._cards)}"
        )

    def remove_card(self, card: DownloadItemWidget) -> None:
        """从列表移除卡片"""
        if card in self._cards:
            self._cards.remove(card)
            self.scroll_layout.removeWidget(card)
            card.setParent(None)
            self._update_empty_state()

    def set_filter(self, status: str) -> None:
        """设置过滤条件"""
        self._current_filter = status

        # 批量更新优化：暂停 UI 更新
        self.scroll_widget.setUpdatesEnabled(False)
        try:
            for card in self._cards:
                self._apply_filter_to_card(card)
        finally:
            self.scroll_widget.setUpdatesEnabled(True)

        self._update_empty_state()

    def _apply_filter_to_card(self, card: DownloadItemWidget) -> None:
        """根据当前过滤器决定卡片可见性"""
        if self._current_filter == "all":
            card.setVisible(True)
        else:
            card.setVisible(card.state() == self._current_filter)

    def _on_card_state_changed(self, card: DownloadItemWidget) -> None:
        """卡片状态变化时重新检查可见性"""
        self._apply_filter_to_card(card)
        self._update_empty_state()

    def _on_card_remove_requested(self, card: DownloadItemWidget) -> None:
        """转发删除请求"""
        self.card_remove_requested.emit(card)

    def _on_card_resume_requested(self, card: DownloadItemWidget) -> None:
        """转发恢复请求"""
        self.card_resume_requested.emit(card)

    def _update_empty_state(self) -> None:
        """检查并更新空状态显示"""
        # Fix: Do not rely on c.isVisible() which might return False if parent is not yet shown.
        # Calculate based on logical state instead.
        if self._current_filter == "all":
            visible_count = len(self._cards)
        else:
            visible_count = sum(1 for c in self._cards if c.state() == self._current_filter)

        print(
            f"[DEBUG] _update_empty_state: filter={self._current_filter}, visible_count={visible_count}"
        )

        if visible_count == 0:
            self.scroll_area.setVisible(False)
            self.empty_placeholder.setVisible(True)

            # 根据当前过滤器显示不同文案
            messages = {
                "all": ("🍃", "暂无任务", "点击「新建任务」开始下载"),
                "running": ("⏳", "没有正在下载的任务", "当前无活跃下载"),
                "queued": ("📋", "没有排队中的任务", "所有任务已开始"),
                "paused": ("⏸️", "没有暂停的任务", "所有任务运行中"),
                "completed": ("✅", "没有已完成的任务", "完成的任务会显示在这里"),
            }
            icon, title, subtitle = messages.get(self._current_filter, ("🍃", "暂无任务", ""))
            self.empty_icon.setText(icon)
            self.empty_title.setText(title)
            self.empty_desc.setText(subtitle)

            # 仅在 'all' 过滤器下显示行动按钮
            self.empty_action_btn.setVisible(self._current_filter == "all")
        else:
            self.scroll_area.setVisible(True)
            self.empty_placeholder.setVisible(False)

    def count(self) -> int:
        """返回卡片总数"""
        return len(self._cards)

    def visible_count(self) -> int:
        """返回当前可见卡片数"""
        return sum(1 for c in self._cards if c.isVisible())

    def get_counts_by_state(self) -> dict[str, int]:
        """获取各状态的任务计数"""
        counts = {"all": 0, "running": 0, "queued": 0, "paused": 0, "completed": 0, "error": 0}
        for card in self._cards:
            state = card.state()
            counts[state] = counts.get(state, 0) + 1
            counts["all"] += 1
        return counts
