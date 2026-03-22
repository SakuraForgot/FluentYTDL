from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    TableWidget,
)

from ...processing.subtitle_manager import (
    SubtitleTrack,
    extract_subtitle_tracks,
)


class SubtitleSelectorWidget(QFrame):
    """
    字幕选择器组件 (Fluent TableWidget 版)

    提供字幕语言多选和格式配置 UI:
    - 表格化展示所有可用字幕
    - 区分人工/自动字幕
    - 格式转换选项
    """

    selectionChanged = Signal()

    def __init__(self, info: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self._tracks: list[SubtitleTrack] = []

        self._init_ui()
        self._load_subtitles()

    def _init_ui(self):
        self.setObjectName("subtitleSelector")
        # 背景透明，边框由 TableWidget 处理
        self.setStyleSheet("#subtitleSelector { background-color: transparent; border: none; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 1. 字幕列表表格
        self.table = TableWidget(self)
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["选择", "语言", "类型", "原始格式"])
        self.table.verticalHeader().setVisible(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)

        # Column setup
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(2, 100)
        self.table.setColumnWidth(3, 100)

        # Disable default selection (we use checkboxes)
        # self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        # TableWidget doesn't easily support NoSelection if we want hover effects,
        # but we can ignore selection.

        layout.addWidget(self.table)

        # 无字幕提示
        self.noSubtitleLabel = CaptionLabel("该视频无可用字幕", self)
        self.noSubtitleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.noSubtitleLabel.setStyleSheet("color: #888; margin: 20px;")
        self.noSubtitleLabel.hide()
        layout.addWidget(self.noSubtitleLabel)

        # 2. 底部选项栏
        optRow = QHBoxLayout()
        optRow.setSpacing(16)

        optRow.addStretch()

        # 嵌入选项 (默认隐藏，由外部控制显示)
        self.embedCheck = CheckBox("嵌入到视频", self)
        self.embedCheck.setChecked(True)
        self.embedCheck.hide()  # 默认隐藏
        optRow.addWidget(self.embedCheck)

        layout.addLayout(optRow)

    def _load_subtitles(self):
        """加载可用字幕列表"""
        self.table.setRowCount(0)
        self._tracks = extract_subtitle_tracks(self.info)

        if not self._tracks:
            self.table.hide()
            self.noSubtitleLabel.show()
            return

        self.table.show()
        self.noSubtitleLabel.hide()

        # 排序：手动 > 自动，然后按语言优先级
        priority = ["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]

        def sort_key(t):
            type_score = 1 if t.is_auto else 0
            try:
                lang_score = priority.index(t.lang_code)
            except ValueError:
                lang_score = 100
            return (type_score, lang_score, t.lang_code)

        self._tracks.sort(key=sort_key)

        self.table.setRowCount(len(self._tracks))

        for row, track in enumerate(self._tracks):
            # 1. Checkbox (Centered)
            cell_widget = QWidget()
            layout = QHBoxLayout(cell_widget)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cb = CheckBox()

            # 默认选中第一个中文手动字幕
            if row == 0 and track.lang_code.startswith("zh") and not track.is_auto:
                cb.setChecked(True)
            elif track.lang_code.startswith("zh") and not track.is_auto:
                pass

            cb.stateChanged.connect(self.selectionChanged)
            layout.addWidget(cb)
            self.table.setCellWidget(row, 0, cell_widget)

            # 2. Language
            lang_text = f"{track.display_name} ({track.lang_code})"
            item_lang = QTableWidgetItem(lang_text)
            item_lang.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 1, item_lang)

            # 3. Type
            type_text = "自动生成" if track.is_auto else "人工"
            item_type = QTableWidgetItem(type_text)
            item_type.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_type.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            if track.is_auto:
                item_type.setForeground(Qt.GlobalColor.gray)
            self.table.setItem(row, 2, item_type)

            # 4. Format
            item_fmt = QTableWidgetItem(track.ext.upper())
            item_fmt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            item_fmt.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            self.table.setItem(row, 3, item_fmt)

            # Store track data
            item_lang.setData(Qt.ItemDataRole.UserRole, track)

    def get_selected_tracks(self) -> list[SubtitleTrack]:
        """获取用户选中的轨道"""
        selected = []
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, 0)
            if not cell_widget:
                continue
            # Use findChildren to be safe
            cbs = cell_widget.findChildren(CheckBox)
            if cbs and cbs[0].isChecked():
                item = self.table.item(row, 1)
                if item is None:
                    continue
                track = item.data(Qt.ItemDataRole.UserRole)
                if track:
                    selected.append(track)
        return selected

    def get_opts(self) -> dict[str, Any]:
        """
        获取 yt-dlp 选项
        """
        selected_tracks = self.get_selected_tracks()
        if not selected_tracks:
            return {}

        languages = set()
        has_manual = False
        has_auto = False

        for t in selected_tracks:
            languages.add(t.lang_code)
            if t.is_auto:
                has_auto = True
            else:
                has_manual = True

        opts = {
            "subtitleslangs": list(languages),
            "skip_download": True,  # Default safety, overridden by parent if needed
        }

        # Explicitly set writesubtitles/writeautomaticsub
        # Note: If has_manual is False, we MUST set writesubtitles=False,
        # otherwise yt-dlp defaults might kick in or it might be ambiguous.
        opts["writesubtitles"] = has_manual
        opts["writeautomaticsub"] = has_auto

        # Embed (usually for video mode)
        if self.embedCheck.isChecked() and self.embedCheck.isVisible():
            opts["embedsubtitles"] = True

        return opts
