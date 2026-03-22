"""
FluentYTDL 片段下载卡片组件

提供时间范围下载配置 UI:
- 开始/结束时间输入
- 格式验证
- 时长显示
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    LineEdit,
    SwitchButton,
)

from ...core.section_download import (
    TimeRange,
    build_section_opts,
    parse_time_range,
)


class SectionDownloadCard(QFrame):
    """
    片段下载卡片组件

    允许用户指定下载的时间范围。
    """

    selectionChanged = Signal()

    def __init__(self, info: Any, parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        if hasattr(info, "duration"):
            self._duration = info.duration or 0
        else:
            self._duration = info.get("duration", 0) or 0
        self._time_range: TimeRange | None = None

        self._init_ui()

    def _init_ui(self):
        self.setObjectName("sectionDownloadCard")
        # Style setup moved to _update_style

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 标题行
        header = QHBoxLayout()
        self.titleLabel = BodyLabel("✂️ 片段下载", self)
        self.titleLabel.setStyleSheet("font-weight: 600;")
        header.addWidget(self.titleLabel)

        self.enableSwitch = SwitchButton(self)
        self.enableSwitch.setChecked(False)
        self.enableSwitch.checkedChanged.connect(self._on_enabled_changed)
        header.addWidget(self.enableSwitch)
        header.addStretch()
        layout.addLayout(header)

        # 选项区 (默认隐藏)
        self.optionsWidget = QWidget(self)
        self.optionsLayout = QVBoxLayout(self.optionsWidget)
        self.optionsLayout.setContentsMargins(0, 0, 0, 0)
        self.optionsLayout.setSpacing(8)

        # 提示文本
        self.hintLabel = CaptionLabel(
            "仅下载指定时间段。格式: 1:30 或 1m30s 或 90 (秒)", self.optionsWidget
        )
        # Hint label style moved to _update_style
        self.optionsLayout.addWidget(self.hintLabel)

        # 时间输入行
        timeRow = QHBoxLayout()
        timeRow.setSpacing(12)

        # 开始时间
        timeRow.addWidget(BodyLabel("开始:", self.optionsWidget))
        self.startEdit = LineEdit(self.optionsWidget)
        self.startEdit.setPlaceholderText("0:00")
        self.startEdit.setFixedWidth(100)
        self.startEdit.editingFinished.connect(self._on_time_changed)
        timeRow.addWidget(self.startEdit)

        # 结束时间
        timeRow.addWidget(BodyLabel("结束:", self.optionsWidget))
        self.endEdit = LineEdit(self.optionsWidget)
        self.endEdit.setPlaceholderText("留空=到结束")
        self.endEdit.setFixedWidth(100)
        self.endEdit.editingFinished.connect(self._on_time_changed)
        timeRow.addWidget(self.endEdit)

        timeRow.addStretch()
        self.optionsLayout.addLayout(timeRow)

        # 状态行
        self.statusLabel = CaptionLabel("", self.optionsWidget)
        self.statusLabel.setStyleSheet("color: #0078D4;")
        self.optionsLayout.addWidget(self.statusLabel)

        # 错误提示
        self.errorLabel = CaptionLabel("", self.optionsWidget)
        self.errorLabel.setStyleSheet("color: #D13438;")
        self.errorLabel.hide()
        self.optionsLayout.addWidget(self.errorLabel)

        layout.addWidget(self.optionsWidget)
        self.optionsWidget.hide()

        # 显示视频总时长
        if self._duration > 0:
            m = int(self._duration // 60)
            s = int(self._duration % 60)
            self.hintLabel.setText(f"视频总时长: {m}:{s:02d}。格式: 1:30 或 1m30s 或 90 (秒)")

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        bg_color = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "rgba(255, 255, 255, 0.7)"
        border_color = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.setStyleSheet(
            f"#sectionDownloadCard {{ background-color: {bg_color}; border: 1px solid {border_color}; border-radius: 8px; }}"
        )
        self.hintLabel.setStyleSheet("color: #AAA;" if isDarkTheme() else "color: #666;")

    def _on_enabled_changed(self, enabled: bool):
        """开关变更"""
        self.optionsWidget.setVisible(enabled)
        if enabled:
            self._validate_times()
        self.selectionChanged.emit()

    def _on_time_changed(self):
        """时间输入变化"""
        self._validate_times()
        self.selectionChanged.emit()

    def _validate_times(self) -> bool:
        """验证时间输入"""
        self.errorLabel.hide()
        self.statusLabel.setText("")
        self._time_range = None

        start_text = self.startEdit.text().strip()
        end_text = self.endEdit.text().strip()

        if not start_text:
            start_text = "0"

        try:
            self._time_range = parse_time_range(start_text, end_text or None)

            # 显示时长
            if self._time_range.duration_seconds is not None:
                dur = self._time_range.duration_seconds
                m = int(dur // 60)
                s = int(dur % 60)
                self.statusLabel.setText(f"将下载 {m}:{s:02d} 的片段")
            else:
                self.statusLabel.setText(f"从 {self._time_range.start_str} 到结束")

            return True

        except ValueError as e:
            self.errorLabel.setText(str(e))
            self.errorLabel.show()
            return False

    def is_enabled(self) -> bool:
        """是否启用片段下载"""
        return self.enableSwitch.isChecked()

    def get_time_range(self) -> TimeRange | None:
        """获取时间范围"""
        if not self.is_enabled():
            return None
        return self._time_range

    def get_opts(self) -> dict[str, Any]:
        """
        获取 yt-dlp 选项

        Returns:
            yt-dlp 选项字典
        """
        if not self.is_enabled():
            return {}

        if self._time_range is None:
            self._validate_times()

        if self._time_range is None:
            return {}

        return build_section_opts(self._time_range)

    def is_valid(self) -> bool:
        """输入是否有效"""
        if not self.is_enabled():
            return True
        return self._time_range is not None
