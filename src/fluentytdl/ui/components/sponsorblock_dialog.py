"""
SponsorBlock 类别选择对话框

提供用户友好的界面来选择要跳过的广告片段类型。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    MessageBoxBase,
    ScrollArea,
    SubtitleLabel,
)


class CategoryCheckBox(QFrame):
    """带描述的类别复选框"""

    def __init__(self, cat_id: str, cat_name: str, cat_desc: str, checked: bool, parent=None):
        super().__init__(parent)
        self.cat_id = cat_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)

        # 复选框（类别名称）
        self.checkbox = CheckBox(cat_name, self)
        self.checkbox.setChecked(checked)
        layout.addWidget(self.checkbox)

        # 描述文字
        desc_label = CaptionLabel(cat_desc, self)
        from qfluentwidgets import isDarkTheme

        color_alpha = "rgba(255, 255, 255, 0.45)" if isDarkTheme() else "rgba(0, 0, 0, 0.45)"
        desc_label.setStyleSheet(f"color: {color_alpha}; margin-left: 28px;")
        layout.addWidget(desc_label)

    def isChecked(self) -> bool:
        return self.checkbox.isChecked()


class SponsorBlockCategoriesDialog(MessageBoxBase):
    """SponsorBlock 类别选择对话框

    使用 Fluent Design 风格的对话框，允许用户选择要跳过的广告类别。
    """

    def __init__(
        self,
        current_categories: list[str],
        parent: QWidget | None = None,
    ) -> None:
        """
        初始化对话框

        Args:
            current_categories: 当前已选中的类别ID列表
            parent: 父窗口
        """
        super().__init__(parent)

        self.selected_categories: list[str] = []

        # 设置对话框属性
        self.viewLayout.setSpacing(16)

        # 标题
        self.titleLabel = SubtitleLabel("选择要跳过的片段类型", self)
        self.viewLayout.addWidget(self.titleLabel)

        # 说明文字
        self.infoLabel = BodyLabel(
            "勾选下方要自动跳过的广告片段类型，这些片段将在下载时自动移除或标记为章节。", self
        )
        self.infoLabel.setWordWrap(True)
        from qfluentwidgets import isDarkTheme

        info_color = "rgba(255, 255, 255, 0.6)" if isDarkTheme() else "rgba(0, 0, 0, 0.6)"
        self.infoLabel.setStyleSheet(f"color: {info_color};")
        self.viewLayout.addWidget(self.infoLabel)

        # === 创建滚动区域（使用 qfluentwidgets 的 ScrollArea） ===
        self.scrollArea = ScrollArea(self)
        self.scrollArea.setStyleSheet(
            "QScrollArea { border: none; background-color: transparent; }"
        )

        # 类别内容容器
        self.categoriesWidget = QWidget()
        self.categoriesWidget.setStyleSheet("background-color: transparent;")
        self.categoriesLayout = QVBoxLayout(self.categoriesWidget)
        self.categoriesLayout.setContentsMargins(0, 0, 8, 0)  # 右边留出滚动条空间
        self.categoriesLayout.setSpacing(4)
        self.categoriesLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

        # 类别定义（分为常用和其他）
        common_categories = [
            ("sponsor", "赞助广告", "视频中的付费推广内容"),
            ("selfpromo", "自我推广", "频道推广、社交媒体链接等"),
            ("interaction", "互动提醒", "订阅、点赞、评论提醒"),
        ]

        other_categories = [
            ("intro", "片头", "视频开头的固定片头"),
            ("outro", "片尾", "视频结尾的固定片尾"),
            ("preview", "预告", "视频中的预告片段"),
            ("filler", "填充内容", "与主题无关的闲聊内容"),
            ("music_offtopic", "非音乐部分", "音乐视频中的非音乐内容"),
        ]

        # 创建复选框
        self.checkboxes: dict[str, CategoryCheckBox] = {}
        current_set = set(current_categories)

        # 常用类别
        common_label = BodyLabel("常用类别", self)
        common_label.setStyleSheet("font-weight: 600; margin-top: 8px;")
        self.categoriesLayout.addWidget(common_label)

        for cat_id, cat_name, cat_desc in common_categories:
            category_widget = CategoryCheckBox(
                cat_id, cat_name, cat_desc, cat_id in current_set, self
            )
            self.categoriesLayout.addWidget(category_widget)
            self.checkboxes[cat_id] = category_widget

        # 其他类别
        other_label = BodyLabel("其他类别", self)
        other_label.setStyleSheet("font-weight: 600; margin-top: 12px;")
        self.categoriesLayout.addWidget(other_label)

        for cat_id, cat_name, cat_desc in other_categories:
            category_widget = CategoryCheckBox(
                cat_id, cat_name, cat_desc, cat_id in current_set, self
            )
            self.categoriesLayout.addWidget(category_widget)
            self.checkboxes[cat_id] = category_widget

        # 设置滚动区域的widget
        self.scrollArea.setWidget(self.categoriesWidget)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setMaximumHeight(380)  # 限制最大高度
        self.viewLayout.addWidget(self.scrollArea)

        # 设置按钮文本
        self.yesButton.setText("确认")
        self.cancelButton.setText("取消")

        # 设置对话框最小宽度
        try:
            self.widget.setMinimumWidth(520)
        except Exception:
            pass

    def accept(self) -> None:  # type: ignore[override]
        """确认按钮点击处理"""
        # 收集选中的类别
        self.selected_categories = [
            cat_id for cat_id, widget in self.checkboxes.items() if widget.isChecked()
        ]
        super().accept()
