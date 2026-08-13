from __future__ import annotations

from typing import Any

from loguru import logger
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ComboBox,
    FluentIcon,
    IconWidget,
    PushButton,
    RadioButton,
    SegmentedWidget,
    SmoothScrollArea,
    StrongBodyLabel,
    TransparentToolButton,
)

from fluentytdl.ui.components.common.badges import QualityCellWidget

from ....core.config_manager import config_manager
from ....utils.container_compat import choose_lossless_merge_container
from ....utils.format_scorer import ScoringContext, decide_merge_container, score_audio_format


def _get_table_selection_qss() -> str:
    from qfluentwidgets import isDarkTheme

    is_dark = isDarkTheme()
    sel_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "#E8E8E8"
    sel_fg = "#ffffff" if is_dark else "#000000"
    norm_fg = "#ffffff" if is_dark else "#000000"
    sel_bd = "rgba(255, 255, 255, 0.15)" if is_dark else "#C0C0C0"
    hov_bg = "rgba(255, 255, 255, 0.04)" if is_dark else "#F3F3F3"
    border = "rgba(255, 255, 255, 0.06)" if is_dark else "rgba(0, 0, 0, 0.06)"
    hover_border = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.1)"

    header_bg = "transparent"
    header_fg = "#A0A0A0" if is_dark else "#5c5c5c"
    header_border = "rgba(255, 255, 255, 0.08)" if is_dark else "rgba(0, 0, 0, 0.08)"

    return f"""
QTableWidget {{
    background-color: transparent;
    selection-background-color: transparent;
    outline: none;
    border: none;
}}
QHeaderView {{
    background-color: {header_bg};
    border: none;
}}
QHeaderView::section {{
    background-color: {header_bg};
    color: {header_fg};
    font-weight: 600;
    border: none;
    border-bottom: 1px solid {header_border};
    padding-left: 4px;
}}
QTableWidget::item {{
    padding-left: 0px;
    border: 1px solid {border};
    margin-top: 3px;
    margin-bottom: 3px;
    margin-right: 4px;
    border-radius: 6px;
    color: {norm_fg};
}}
QTableWidget::item:selected {{
    background-color: {sel_bg};
    color: {sel_fg};
    border: 1px solid {sel_bd};
    border-radius: 6px;
    font-weight: 600;
}}
QTableWidget::item:hover {{
    background-color: {hov_bg};
    border: 1px solid {hover_border};
    border-radius: 6px;
}}
"""


# 专业模式下表格区（可组装的手风琴 / 整表模式的表格）的高度下限。
# 四种模式共用同一个下限，切换时窗口高度恒定，不随格式条目数变化；
# 页面里多出来的高度由伸缩因子全部交给表格区，不设上限——否则超出上限的
# 部分会沉淀成「已选」与「输出容器」之间的底部留白。
_ADV_TABLE_AREA_H = 288


def _get_split_scroll_qss() -> str:
    """Theme-aware QSS for the advanced-mode accordion scroll area."""
    from qfluentwidgets import isDarkTheme

    is_dark = isDarkTheme()
    handle = "rgba(255, 255, 255, 0.20)" if is_dark else "rgba(0, 0, 0, 0.20)"
    handle_hover = "rgba(255, 255, 255, 0.32)" if is_dark else "rgba(0, 0, 0, 0.32)"

    return f"""
QScrollArea {{
    background-color: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background-color: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {handle};
    border-radius: 4px;
    min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{
    background: {handle_hover};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
"""


def _format_size(value: Any) -> str:
    try:
        n = int(value)
    except Exception:
        return "-"
    if n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            if u in ("B", "KB"):
                return f"{int(round(x))}{u}"
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{n}B"


def _analyze_format_tags(r: dict) -> list[tuple[str, str]]:
    """Generates badge data for format details: [(text, color_style), ...]"""
    from PySide6.QtCore import QCoreApplication

    def tr(text: str) -> str:
        return QCoreApplication.translate("FormatSelector", text)

    tags = []

    # 1. HDR
    dyn = str(r.get("dynamic_range") or "SDR").upper()
    if dyn != "SDR":
        # Usually HDR10, HLG, etc.
        tags.append((dyn, "gold"))

    # 2. FPS
    fps = r.get("fps")
    if fps and fps > 30:
        tags.append((f"{int(fps)}FPS", "red"))

    # 3. Audio Language / Track Type (Multi-Language support)
    lang = str(r.get("language") or "").strip()
    if lang:
        # Check if original / default
        track_type = str(r.get("audio_track_type") or "").lower()
        # Original track usually marked by youtube or has language="original" in yt-dlp
        if track_type == "original" or lang.lower() == "orig" or lang.lower() == "original":
            tags.append((tr("原音"), "green"))
        else:
            tags.append((f"[{lang.upper()}]", "blue"))

    # 4. Codec
    # Video
    vc = str(r.get("vcodec") or "none").lower()
    if "av01" in vc:
        tags.append(("AV1", "blue"))
    elif "vp9" in vc:
        tags.append(("VP9", "green"))
    elif "avc1" in vc or "h264" in vc:
        # Gray for older/compatible codec
        tags.append(("H.264", "gray"))

    # Audio
    ac = str(r.get("acodec") or "none").lower()
    if "opus" in ac:
        tags.append(("Opus", "green"))
    elif "mp4a" in ac or "aac" in ac:
        tags.append(("AAC", "gray"))

    return tags


class SimplePresetWidget(QWidget):
    """简易模式下的预设选项卡片"""

    presetSelected = Signal()
    typeChanged = Signal(str)  # video_audio, video_only, audio_only

    def __init__(self, info: dict[str, Any] | None = None, parent=None):
        super().__init__(parent)
        self.info = info or {}

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 下载类型选择
        type_layout = QHBoxLayout()
        type_layout.addWidget(CaptionLabel(self.tr("下载类型:"), self))
        self._type_combo = ComboBox(self)
        self._type_combo.addItems([self.tr("视频 + 音频"), self.tr("仅视频"), self.tr("仅音频")])
        self._type_combo.currentIndexChanged.connect(self._on_type_changed)
        type_layout.addWidget(self._type_combo, 1)

        # 音轨精选按钮（仅在多语言音轨时显示）
        self._audio_pick_result = None
        self.audio_pick_btn = PushButton(self.tr("选择音轨…"), self)
        self.audio_pick_btn.clicked.connect(self._on_audio_pick_clicked)
        self.audio_pick_btn.setVisible(False)
        try:
            from ....processing.audio_track_manager import has_multi_language_audio

            if has_multi_language_audio(self.info):
                self.audio_pick_btn.setVisible(True)
        except Exception:
            pass
        type_layout.addWidget(self.audio_pick_btn)

        main_layout.addLayout(type_layout)

        # 滚动区域
        self.preset_scroll = SmoothScrollArea(self)
        self.preset_scroll.setStyleSheet(_get_split_scroll_qss())
        self.preset_scroll.setWidgetResizable(True)
        self.preset_scroll.setMaximumHeight(450)

        self.content_widget = QWidget()
        self.content_widget.setObjectName("scroll_widget")
        self.content_widget.setStyleSheet("#scroll_widget { background-color: transparent; }")
        self.v_layout = QVBoxLayout(self.content_widget)
        self.v_layout.setSpacing(12)
        self.v_layout.setContentsMargins(10, 10, 10, 10)

        self.preset_scroll.setWidget(self.content_widget)
        main_layout.addWidget(self.preset_scroll)

        self.btn_group = QButtonGroup(self)
        self.btn_group.buttonClicked.connect(self.presetSelected)
        self.radios = []

        self._all_presets = {
            "video_audio": [
                (
                    "best_mp4",
                    self.tr("🎬 最佳画质"),
                    self.tr("推荐。自动选择最佳画质并封装为选定容器，兼容性最好。"),
                    {"type": "video", "max_height": None},
                ),
                (
                    "best_raw",
                    self.tr("🎯 最佳画质 (原盘)"),
                    self.tr("追求极致画质。通常为 WebM/MKV 格式，适合本地播放。"),
                    {"type": "video", "max_height": None},
                ),
                (
                    "2160p",
                    "📺 2160p 4K",
                    self.tr("限制最高分辨率为 4K，超高清画质。"),
                    {"type": "video", "max_height": 2160},
                ),
                (
                    "1440p",
                    "📺 1440p 2K",
                    self.tr("限制最高分辨率为 2K，高清画质。"),
                    {"type": "video", "max_height": 1440},
                ),
                (
                    "1080p",
                    self.tr("📺 1080p 高清"),
                    self.tr("限制最高分辨率为 1080p，平衡画质与体积。"),
                    {"type": "video", "max_height": 1080},
                ),
                (
                    "720p",
                    self.tr("📺 720p 标清"),
                    self.tr("限制最高分辨率为 720p，适合移动设备。"),
                    {"type": "video", "max_height": 720},
                ),
                (
                    "480p",
                    "📺 480p",
                    self.tr("限制最高分辨率为 480p，节省空间。"),
                    {"type": "video", "max_height": 480},
                ),
                (
                    "360p",
                    "📺 360p",
                    self.tr("限制最高分辨率为 360p，最小体积。"),
                    {"type": "video", "max_height": 360},
                ),
            ],
            "video_only": [
                (
                    "best_video",
                    self.tr("🎬 最佳画质 (无音频)"),
                    self.tr("仅下载视频轨，最高画质。"),
                    {"type": "video_only", "max_height": None},
                ),
                (
                    "1080p_video",
                    self.tr("📺 1080p视频 (无音频)"),
                    self.tr("仅下载1080p视频轨。"),
                    {"type": "video_only", "max_height": 1080},
                ),
            ],
            "audio_only": [
                (
                    "audio_best",
                    self.tr("🎵 最佳音质"),
                    self.tr("下载最高品质的音频流并转码。"),
                    {"type": "audio_only", "quality": "best"},
                ),
                (
                    "audio_high",
                    self.tr("🎵 高品质 (320kbps)"),
                    self.tr("高品质音频压缩。"),
                    {"type": "audio_only", "quality": "320K"},
                ),
                (
                    "audio_std",
                    self.tr("🎵 标准品质 (192kbps)"),
                    self.tr("体积与音质平衡。"),
                    {"type": "audio_only", "quality": "192K"},
                ),
            ],
        }

        self._rebuild_presets("video_audio")

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)

    def _update_style(self):
        if hasattr(self, "preset_scroll"):
            self.preset_scroll.setStyleSheet(_get_split_scroll_qss())

    def _on_type_changed(self, index: int):
        types = ["video_audio", "video_only", "audio_only"]
        selected_type = types[index]
        self._rebuild_presets(selected_type)
        self.typeChanged.emit(selected_type)
        self.presetSelected.emit()

    def _get_max_available_height(self) -> int:
        formats = self.info.get("formats", [])
        if not isinstance(formats, list):
            return 0
        max_h = 0
        for f in formats:
            if not isinstance(f, dict):
                continue
            h = f.get("height")
            if h and isinstance(h, (int, float)) and h > max_h:
                max_h = int(h)
        return max_h

    def _rebuild_presets(self, current_type: str):
        # 清理旧组
        for r in self.radios:
            self.btn_group.removeButton(r)

        while self.v_layout.count():
            item = self.v_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        self.radios.clear()

        max_available_height = self._get_max_available_height()

        presets = self._all_presets.get(current_type, [])
        for i, (pid, title, desc, intent) in enumerate(presets):
            intent_max_h = intent.get("max_height")
            if (
                max_available_height > 0
                and intent_max_h is not None
                and intent_max_h > max_available_height
            ):
                continue

            container = CardWidget(self.content_widget)
            h_layout = QHBoxLayout(container)

            rb = RadioButton(title, container)
            rb.setProperty("preset_id", pid)
            rb.setProperty("intent", intent)

            self.btn_group.addButton(rb, i)
            self.radios.append(rb)

            desc_label = CaptionLabel(desc, container)
            desc_label.setStyleSheet("color: #808080;")
            desc_label.setWordWrap(True)

            h_layout.addWidget(rb)
            h_layout.addWidget(desc_label, 1)

            # 点击卡片任意位置即可选中
            def on_card_clicked(event, r=rb):
                r.click()

            container.mousePressEvent = on_card_clicked
            from PySide6.QtCore import Qt

            container.setCursor(Qt.CursorShape.PointingHandCursor)

            self.v_layout.addWidget(container)

        self.v_layout.addStretch(1)

        if self.radios:
            self.radios[0].setChecked(True)

    def get_current_type(self) -> str:
        types = ["video_audio", "video_only", "audio_only"]
        return types[self._type_combo.currentIndex()]

    def get_current_selection(self) -> dict:
        btn = self.btn_group.checkedButton()
        if not btn:
            return {}
        return {
            "id": btn.property("preset_id"),
            "intent": btn.property("intent"),
        }

    def _on_audio_pick_clicked(self):
        if not self.info:
            return

        from ...dialogs.audio_picker_dialog import AudioPickerDialog

        # 调用时不需要 container 因为目前由 format_selector 后台全盘推断
        dialog = AudioPickerDialog(
            self.info, container=None, initial_result=self._audio_pick_result, parent=self.window()
        )

        if dialog.exec():
            result = dialog.get_result()
            self._audio_pick_result = result
            n = len(result.format_ids)
            if n > 1:
                self.audio_pick_btn.setText(f"已选 {n} 条音轨 ✓")
            elif n == 1:
                self.audio_pick_btn.setText(self.tr("已选 1 条音轨"))
            else:
                self.audio_pick_btn.setText(self.tr("选择音轨…"))

    def get_audio_pick_result(self) -> Any | None:
        return self._audio_pick_result


class _ContainerFormatBar(QFrame):
    """分享于简易与专业模式的输出格式控制栏"""

    formatChanged = Signal()

    def __init__(self, config_prefix: str | None = None, parent=None):
        super().__init__(parent)
        self.config_prefix = config_prefix

        from qfluentwidgets import isDarkTheme

        bg = "rgba(255, 255, 255, 0.03)" if isDarkTheme() else "rgba(0, 0, 0, 0.03)"
        self.setStyleSheet(
            f"._ContainerFormatBar {{ background-color: {bg}; border-radius: 6px; padding: 5px; }}"
        )

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)

        row_layout = QHBoxLayout()
        row_layout.setContentsMargins(0, 0, 0, 0)

        self.container_label = CaptionLabel(self.tr("输出容器:"), self)
        self.container_combo = ComboBox(self)
        self.container_combo.addItems([self.tr("自动推断"), "MP4", "MKV", "WebM"])

        self.audio_label = CaptionLabel(self.tr("输出格式:"), self)
        self.audio_combo = ComboBox(self)
        self.audio_combo.addItems([self.tr("自动推断"), "MP3", "FLAC", "M4A", "WAV", "Opus", "AAC"])

        # 加载记忆偏好
        if self.config_prefix:
            from ....core.config_manager import config_manager

            c_val = config_manager.get(
                f"{self.config_prefix}_container_override", self.tr("自动推断")
            )
            idx = self.container_combo.findText(c_val)
            if idx >= 0:
                self.container_combo.setCurrentIndex(idx)

            a_val = config_manager.get(f"{self.config_prefix}_audio_override", self.tr("自动推断"))
            idx = self.audio_combo.findText(a_val)
            if idx >= 0:
                self.audio_combo.setCurrentIndex(idx)

        self.container_combo.currentIndexChanged.connect(self._on_container_changed)
        self.audio_combo.currentIndexChanged.connect(self._on_audio_changed)

        row_layout.addWidget(self.container_label)
        row_layout.addWidget(self.container_combo)
        row_layout.addWidget(self.audio_label)
        row_layout.addWidget(self.audio_combo)

        # 「已选：...」摘要并排放在输出格式旁边。
        # 原先它单独占一行贴在表格区底部，和上方滚动区糊在一起，显示效果很差。
        # 默认隐藏：只有需要展示选择状态的调用方（专业模式）才会打开。
        row_layout.addSpacing(16)
        self.selection_label = CaptionLabel("", self)
        self.selection_label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self.selection_label.hide()
        row_layout.addWidget(self.selection_label)

        row_layout.addStretch(1)

        main_layout.addLayout(row_layout)

        self.hint_label = CaptionLabel("", self)
        self.hint_label.setStyleSheet("color: #E2C08D;")  # Warning color
        self.hint_label.hide()
        main_layout.addWidget(self.hint_label)

    def _on_container_changed(self):
        if self.config_prefix:
            from ....core.config_manager import config_manager

            config_manager.set(
                f"{self.config_prefix}_container_override", self.container_combo.currentText()
            )
        self.formatChanged.emit()

    def _on_audio_changed(self):
        if self.config_prefix:
            from ....core.config_manager import config_manager

            config_manager.set(
                f"{self.config_prefix}_audio_override", self.audio_combo.currentText()
            )
        self.formatChanged.emit()

    def set_mode(self, mode_str: str):
        if mode_str == "audio_only":
            self.container_label.hide()
            self.container_combo.hide()
            self.audio_label.show()
            self.audio_combo.show()
        elif mode_str == "video_only":
            self.container_label.show()
            self.container_combo.show()
            self.audio_label.hide()
            self.audio_combo.hide()
        else:
            self.container_label.show()
            self.container_combo.show()
            self.audio_label.show()
            self.audio_combo.show()

    def set_hint(self, text: str):
        if text:
            self.hint_label.setText(text)
            self.hint_label.show()
        else:
            self.hint_label.hide()

    def get_container_override(self) -> str | None:
        if self.container_combo.currentIndex() == 0:
            return None
        return self.container_combo.currentText().lower()

    def get_audio_override(self) -> str | None:
        if self.audio_combo.currentIndex() == 0:
            return None
        return self.audio_combo.currentText().lower()


class FormatExpandCard(CardWidget):
    def __init__(self, icon: FluentIcon, title: str, parent=None):
        super().__init__(parent)
        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(8, 8, 8, 8)
        self.v_layout.setSpacing(0)

        # Header
        self.header_widget = QWidget(self)
        self.header_widget.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header_layout = QHBoxLayout(self.header_widget)
        self.header_layout.setContentsMargins(4, 4, 4, 4)

        self.icon_widget = IconWidget(icon, self)
        self.icon_widget.setFixedSize(18, 18)

        self.title_label = StrongBodyLabel(title, self)
        self.summary_label = CaptionLabel(self.tr("未选择"), self)
        self.summary_label.setStyleSheet("color: #808080;")

        self.toggle_btn = TransparentToolButton(FluentIcon.DOWN, self)
        self.toggle_btn.setFixedSize(30, 30)
        self.toggle_btn.clicked.connect(self.toggle)

        self.header_layout.addWidget(self.icon_widget)
        self.header_layout.addSpacing(10)
        self.header_layout.addWidget(self.title_label)
        self.header_layout.addSpacing(10)
        self.header_layout.addWidget(self.summary_label)
        self.header_layout.addStretch(1)
        self.header_layout.addWidget(self.toggle_btn)

        self.v_layout.addWidget(self.header_widget)

        # Content body
        self.body_widget = QWidget(self)
        self.body_layout = QVBoxLayout(self.body_widget)
        self.body_layout.setContentsMargins(0, 8, 0, 0)
        self.body_layout.setSpacing(0)

        self.v_layout.addWidget(self.body_widget)

        self.is_expanded = False
        self.body_widget.hide()

        self.header_widget.mouseReleaseEvent = self._on_header_clicked

        self.header_widget.mouseReleaseEvent = self._on_header_clicked

    def _on_header_clicked(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.toggle()

    def toggle(self):
        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self.toggle_btn.setIcon(FluentIcon.UP)
            self.body_widget.show()
        else:
            self.toggle_btn.setIcon(FluentIcon.DOWN)
            self.body_widget.hide()

    def set_content(self, widget: QWidget):
        self.body_layout.addWidget(widget)

    def set_summary(self, text: str):
        self.summary_label.setText(text)


class VideoFormatSelectorWidget(QWidget):
    """
    Encapsulates the logic for selecting video/audio formats.
    Supports "Simple" (presets) and "Advanced" (table) modes.
    """

    selectionChanged = Signal()

    def __init__(self, info: dict[str, Any], parent=None):
        super().__init__(parent)
        self.info = info

        # State for advanced mode
        self._rows: list[dict[str, Any]] = []
        self._selected_video_id: str | None = None
        self._selected_audio_id: str | None = None
        self._selected_audio_ids: list[str] = []
        self._selected_muxed_id: str | None = None

        self._current_mode = "simple"

        self._init_ui()
        self._build_rows(info)
        self._refresh_table()

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)

    def _update_style(self):
        qss = _get_table_selection_qss()
        if hasattr(self, "table"):
            self.table.setStyleSheet(qss)
        if hasattr(self, "video_table"):
            self.video_table.setStyleSheet(qss)
        if hasattr(self, "audio_table"):
            self.audio_table.setStyleSheet(qss)
        if hasattr(self, "split_scroll"):
            self.split_scroll.setStyleSheet(_get_split_scroll_qss())
        if hasattr(self, "table_scroll"):
            self.table_scroll.setStyleSheet(_get_split_scroll_qss())

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Mode Switcher
        self.view_switcher = SegmentedWidget(self)
        self.view_switcher.addItem("simple", self.tr("简易模式"))
        self.view_switcher.addItem("advanced", self.tr("专业模式"))
        layout.addWidget(self.view_switcher)

        # Stack
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)

        # Page 1: Simple
        self.simple_widget = SimplePresetWidget(self.info, self)
        self.simple_widget.presetSelected.connect(self.selectionChanged)
        self.stack.addWidget(self.simple_widget)

        # Page 2: Advanced
        self.advanced_widget = QWidget(self)
        adv_layout = QVBoxLayout(self.advanced_widget)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(10)

        # Mode Combo
        form_layout = QHBoxLayout()
        form_layout.addWidget(CaptionLabel(self.tr("下载模式:"), self.advanced_widget))
        self.mode_combo = ComboBox(self.advanced_widget)
        self.mode_combo.addItems(
            [
                self.tr("音视频（可组装）"),
                self.tr("音视频（整合流）"),
                self.tr("仅视频"),
                self.tr("仅音频"),
            ]
        )
        self.mode_combo.currentIndexChanged.connect(self._refresh_table)
        form_layout.addWidget(self.mode_combo, 1)
        adv_layout.addLayout(form_layout)

        self.hint_label = CaptionLabel(
            self.tr("提示：可组装模式仅显示分离流，分别点选“视频”和“音频”即可组装。"),
            self.advanced_widget,
        )
        adv_layout.addWidget(self.hint_label)

        # --- Tables Area ---

        # 1. Single Table (for modes 1, 2, 3) - wrapped in scroll area
        self.table = self._create_table()
        # 外层滚动区负责滚动，表格自身完整撑开，避免行被从中间截断
        self.table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.table.cellClicked.connect(self._on_table_clicked)

        self.table_scroll = SmoothScrollArea(self.advanced_widget)
        self.table_scroll.setStyleSheet(_get_split_scroll_qss())
        self.table_scroll.setWidget(self.table)
        self.table_scroll.setWidgetResizable(True)
        self.table_scroll.setMinimumHeight(_ADV_TABLE_AREA_H)
        # 伸缩因子 1：页面里多出来的高度全部归表格区，而不是沉淀成底部留白
        adv_layout.addWidget(self.table_scroll, 1)

        # Split Container (for mode 0) - wrapped in scroll area
        self.split_container = QWidget(self.advanced_widget)
        split_layout = QVBoxLayout(self.split_container)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(10)

        # Video Section
        self.video_card = FormatExpandCard(
            FluentIcon.VIDEO, self.tr("视频流"), self.split_container
        )
        self.video_table = self._create_table()
        self.video_table.setMinimumHeight(120)
        self.video_table.setMaximumHeight(280)  # 适当放开高度限制以显示更多元素
        self.video_table.cellClicked.connect(self._on_video_table_clicked)
        self.video_card.set_content(self.video_table)
        self.video_card.toggle()  # 默认展开视频流
        split_layout.addWidget(self.video_card)

        # Audio Section
        self.audio_card = FormatExpandCard(
            FluentIcon.MUSIC, self.tr("音频流 (可多选)"), self.split_container
        )
        self.audio_table = self._create_table(multi_select=True)
        self.audio_table.setMinimumHeight(120)
        self.audio_table.setMaximumHeight(280)  # 同上
        self.audio_table.itemSelectionChanged.connect(self._on_audio_selection_changed)
        self.audio_card.set_content(self.audio_table)
        split_layout.addWidget(self.audio_card)

        split_layout.addStretch(1)

        # Wrap split container in a scroll area to prevent window expansion
        self.split_scroll = SmoothScrollArea(self.advanced_widget)
        self.split_scroll.setStyleSheet(_get_split_scroll_qss())
        self.split_scroll.setWidget(self.split_container)
        self.split_scroll.setWidgetResizable(True)
        self.split_scroll.setMinimumHeight(_ADV_TABLE_AREA_H)
        adv_layout.addWidget(self.split_scroll, 1)

        # hint_label 锁成固定高度：多余空间只能流向上面的表格区，
        # 否则 QVBoxLayout 会把它平摊给标签，文字垂直居中后看起来像在漂移。
        self.hint_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self.stack.addWidget(self.advanced_widget)

        # Format Bar
        self.format_bar = _ContainerFormatBar(config_prefix="single", parent=self)
        self.format_bar.formatChanged.connect(self.selectionChanged)
        layout.addWidget(self.format_bar)

        # 「已选：...」摘要现在住在格式栏里（输出格式旁边），而不是表格区底部。
        # 保留 self.selection_label 这个名字，_update_label / get_summary_text 无需改动。
        self.selection_label = self.format_bar.selection_label

        self.view_switcher.currentItemChanged.connect(self._on_mode_changed)
        self.view_switcher.setCurrentItem("simple")

        self.simple_widget.typeChanged.connect(self._on_simple_type_changed)

    def _create_table(self, multi_select: bool = False):
        t = QTableWidget(self.advanced_widget)
        t.setStyleSheet(_get_table_selection_qss())
        t.setColumnCount(3)
        t.setHorizontalHeaderLabels([self.tr("类型"), self.tr("质量"), self.tr("详情")])
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        if multi_select:
            t.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        else:
            t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        try:
            t.verticalHeader().setDefaultSectionSize(42)
            t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            t.setColumnWidth(0, 60)
            t.setColumnWidth(1, 130)
        except Exception:
            pass
        return t

    def _on_simple_type_changed(self, type_str: str):
        self.format_bar.set_mode(type_str)
        self.selectionChanged.emit()

    def _update_format_bar_visibility(self):
        if self._current_mode == "simple":
            mode_str = self.simple_widget.get_current_type()
        else:
            idx = self.mode_combo.currentIndex()
            if idx == 3:
                mode_str = "audio_only"
            elif idx == 2:
                mode_str = "video_only"
            else:
                mode_str = "video_audio"
        self.format_bar.set_mode(mode_str)

    def get_container_override(self) -> str | None:
        return self.format_bar.get_container_override()

    def get_audio_format_override(self) -> str:
        return self.format_bar.get_audio_override()

    def _on_mode_changed(self, routeKey: str):
        self._current_mode = routeKey
        is_advanced = routeKey != "simple"
        self.stack.setCurrentIndex(1 if is_advanced else 0)
        # 「已选」摘要只在专业模式下有意义：简易模式的预设单选项本身就自述了选择内容
        self.selection_label.setVisible(is_advanced)

    def _build_rows(self, info: dict[str, Any]):
        formats = info.get("formats") or []
        if not isinstance(formats, list):
            return

        candidates = []
        for f in formats:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("format_id") or "").strip()
            if not fid:
                continue

            vcodec = str(f.get("vcodec") or "none")
            acodec = str(f.get("acodec") or "none")
            ext = str(f.get("ext") or "-")
            height = int(f.get("height") or 0)

            video_ext = str(f.get("video_ext") or "none")
            audio_ext = str(f.get("audio_ext") or "none")
            resolution = str(f.get("resolution") or "")

            has_video = vcodec != "none" or (video_ext != "none" and resolution != "audio only")
            has_audio = acodec != "none" or audio_ext != "none"

            kind = "unknown"
            if has_video and has_audio:
                kind = "muxed"
            elif has_video and not has_audio:
                kind = "video"
            elif not has_video and has_audio:
                kind = "audio"
            else:
                continue

            if kind in ("muxed", "video") and height and height < 144:
                continue

            candidates.append(
                {
                    "kind": kind,
                    "format_id": fid,
                    "ext": ext,
                    "height": height,
                    "vcodec": vcodec,
                    "acodec": acodec,
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "fps": f.get("fps"),
                    "vbr": f.get("vbr"),
                    "tbr": f.get("tbr"),
                    "abr": f.get("abr"),
                    "dynamic_range": f.get("dynamic_range"),
                    "language": f.get("language"),
                    "audio_track_type": f.get("audio_track_type"),
                }
            )

        # Sort: muxed first, then video, then audio. Within kind, by height desc.
        candidates.sort(
            key=lambda x: (
                0 if x["kind"] == "muxed" else 1 if x["kind"] == "video" else 2,
                -int(x.get("height") or 0),
            )
        )
        self._rows = candidates

    def _refresh_table_selection_state(self):
        """Reset incompatible selections based on current mode (helper for _refresh_table)."""
        mode = self.mode_combo.currentIndex()
        self.hint_label.setVisible(mode == 0)

        # Clear incompatible selections
        if mode == 0:
            self._selected_muxed_id = None
        if mode == 1:
            self._selected_video_id = None
            self._selected_audio_id = None
        if mode in (2, 3):
            self._selected_muxed_id = None
            if mode == 2:
                self._selected_audio_id = None
            else:
                self._selected_video_id = None

    def _get_best_audio_id(
        self, audio_rows: list[dict], ctx: ScoringContext | None = None
    ) -> str | None:
        """
        自动推断最优的音频流。

        若传入 ctx，直接使用其 preferred_audio_langs；否则从 config_manager 读取。
        评分委托给 format_scorer.score_audio_format，使用等差间距 + BCP-47 别名匹配，
        彻底修复了旧版 10**i 指数间距在第 8 个偏好后 multiplier 归零的精度崩塌问题。
        """
        if not audio_rows:
            return None

        if ctx is None:
            pref_langs = config_manager.get("preferred_audio_languages")
            if not isinstance(pref_langs, list) or not pref_langs:
                pref_langs = ["orig", "zh-Hans", "en"]
            ctx = ScoringContext(preferred_audio_langs=pref_langs)

        best_audio = max(audio_rows, key=lambda r: score_audio_format(r, ctx))
        return best_audio["format_id"]

    def _pick_best_video(self, video_rows: list[dict], intent: dict) -> str | None:
        """
        根据预设意图从分离视频流中挑选最优项。

        修复：容器偏好改为「硬约束先过滤，无结果再降级」，替代原先 +500 分的软偏好
        （+500 在 1080p vs 720p 差异下完全被 h*10000 淹没，实际无效）。
        """
        if not video_rows:
            return None

        max_height = intent.get("max_height")
        prefer_ext = intent.get("prefer_ext")

        # 1. 分辨率上限过滤
        pool = video_rows
        if max_height is not None:
            pool = [r for r in pool if int(r.get("height") or 0) <= max_height]
        if not pool:
            return None

        # 2. 容器硬约束过滤（简易模式 + 有容器偏好时）：先尝试目标容器
        if prefer_ext:
            preferred_pool = [r for r in pool if str(r.get("ext") or "").lower() == prefer_ext]
            if preferred_pool:
                pool = preferred_pool
            # 若目标容器无流，保留全集并由容器决策函数处理结果格式

        # 3. 在约束后的候选集内按分辨率+码率排序
        best = max(
            pool,
            key=lambda r: (
                int(r.get("height") or 0),
                int(r.get("vbr") or r.get("tbr") or 0),
            ),
        )
        return best["format_id"]

    def _pick_best_muxed(self, muxed_rows: list[dict], intent: dict) -> str | None:
        """
        当没有分离流时，从整合流中挑选最优项（容器硬约束策略同 _pick_best_video）。
        """
        if not muxed_rows:
            return None

        max_height = intent.get("max_height")
        prefer_ext = intent.get("prefer_ext")

        pool = muxed_rows
        if max_height is not None:
            pool = [r for r in pool if int(r.get("height") or 0) <= max_height]
        if not pool:
            pool = muxed_rows  # 分辨率门槛无结果时回退全集

        if prefer_ext:
            preferred_pool = [r for r in pool if str(r.get("ext") or "").lower() == prefer_ext]
            if preferred_pool:
                pool = preferred_pool

        best = max(
            pool,
            key=lambda r: (
                int(r.get("height") or 0),
                int(r.get("vbr") or r.get("tbr") or 0),
            ),
        )
        return best["format_id"]

    def _refresh_table(self):
        mode = self.mode_combo.currentIndex()
        self.hint_label.setVisible(mode == 0)

        # Clear incompatible selections
        if mode == 0:
            self._selected_muxed_id = None
        if mode == 1:
            self._selected_video_id = None
            self._selected_audio_id = None
        if mode in (2, 3):
            self._selected_muxed_id = None
            if mode == 2:
                self._selected_audio_id = None
            else:
                self._selected_video_id = None

        if mode == 0:
            # Split View
            self.table_scroll.hide()
            if getattr(self, "split_scroll", None):
                self.split_scroll.show()

            video_rows = [r for r in getattr(self, "_rows", []) if r["kind"] == "video"]
            audio_rows = [r for r in getattr(self, "_rows", []) if r["kind"] == "audio"]

            if not self._selected_audio_id and audio_rows:
                self._selected_audio_id = self._get_best_audio_id(audio_rows)

            if not self._selected_video_id and video_rows:
                self._selected_video_id = video_rows[0]["format_id"]

            if getattr(self, "split_container", None):
                self._populate_table(self.video_table, video_rows, self._selected_video_id)
                self._populate_table(self.audio_table, audio_rows, self._selected_audio_id)

        else:
            # Single View
            if getattr(self, "split_scroll", None):
                self.split_scroll.hide()
            self.table_scroll.show()

            view_rows = []
            for r in self._rows:
                k = r["kind"]
                if mode == 1:
                    if k == "muxed":
                        view_rows.append(r)
                elif mode == 2:
                    if k == "video":
                        view_rows.append(r)
                elif mode == 3:
                    if k == "audio":
                        view_rows.append(r)

            if mode == 3 and not self._selected_audio_id and view_rows:
                self._selected_audio_id = self._get_best_audio_id(view_rows)
            if mode == 2 and not self._selected_video_id and view_rows:
                self._selected_video_id = view_rows[0]["format_id"]
            if mode == 1 and not self._selected_muxed_id and view_rows:
                self._selected_muxed_id = view_rows[0]["format_id"]

            sel_id = self._selected_muxed_id
            if mode == 2:
                sel_id = self._selected_video_id
            elif mode == 3:
                sel_id = self._selected_audio_id

            self._populate_table(self.table, view_rows, sel_id, fill_content=True)

        self._update_label()
        self.selectionChanged.emit()

    def _populate_table(
        self,
        table: QTableWidget,
        rows: list[dict],
        selected_id: str | None,
        fill_content: bool = False,
    ):
        table.setRowCount(len(rows))
        table.setProperty("_rows", rows)

        # 高度策略与可组装模式保持一致：外层滚动区填满可用空间（下限
        # _ADV_TABLE_AREA_H），窗口高度不随行数变化；表格自身按内容完整
        # 撑开，超出部分由滚动区消化，行不会被从中间截断。
        row_height = 42
        header_height = 42 if not table.horizontalHeader().isHidden() else 0
        row_count = max(len(rows), 1)
        visible_rows = row_count if fill_content else min(row_count, 2)
        total_height = header_height + visible_rows * row_height + 2
        table.setMinimumHeight(total_height)
        table.setMaximumHeight(total_height)

        for i, r in enumerate(rows):
            kind = r["kind"]

            icon = FluentIcon.VIDEO if kind in ("muxed", "video") else FluentIcon.MUSIC

            # Use a widget to ensure centering
            container = QWidget()
            container.setStyleSheet("background: transparent;")
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            iw = IconWidget(icon)
            iw.setFixedSize(16, 16)
            layout.addWidget(iw)

            item0 = QTableWidgetItem("")
            table.setItem(i, 0, item0)
            table.setCellWidget(i, 0, container)

            q_text = f"{r.get('height')}p" if r.get("height") else f"{int(r.get('abr') or 0)}kbps"
            # Badges for Quality Column (only HDR)
            q_badges = []
            if r.get("dynamic_range") and "HDR" in str(r.get("dynamic_range")):
                q_badges.append(("HDR", "blue"))

            q_w = QualityCellWidget(
                q_badges, q_text, parent=table, alignment=Qt.AlignmentFlag.AlignCenter
            )
            table.setCellWidget(i, 1, q_w)

            # Detail Column: Tags + Size/Ext
            detail_tags = _analyze_format_tags(r)

            sz = _format_size(r.get("filesize"))
            ext = r.get("ext")

            # Construct main text for details
            detail_text = f"{ext} • {sz}"

            # Use QualityCellWidget for Details too
            # We want left alignment generally for details but user requested centered visuals earlier.
            # However, for badges flow, Left or Center?
            # User said "center alignment to achieve visual optimization" previously.
            # Let's keep Center for consistency.
            d_w = QualityCellWidget(
                detail_tags, detail_text, parent=table, alignment=Qt.AlignmentFlag.AlignCenter
            )

            item2 = QTableWidgetItem("")
            table.setItem(i, 2, item2)
            table.setCellWidget(i, 2, d_w)

        self._highlight_table_rows(table, {selected_id} if selected_id else set())

    def _highlight_table_rows(self, table: QTableWidget, selected_ids: set[str]):
        from qfluentwidgets import isDarkTheme

        is_dark = isDarkTheme()
        sel_bg = QColor(255, 255, 255, 20) if is_dark else QColor("#E8E8E8")
        sel_fg = QColor(255, 255, 255) if is_dark else QColor(0, 0, 0)

        rows = table.property("_rows") or []
        for i in range(table.rowCount()):
            # Reset style
            for j in range(3):
                it = table.item(i, j)
                if it:
                    it.setBackground(QBrush())
                    norm_color = QColor(255, 255, 255) if is_dark else QColor(0, 0, 0)
                    it.setForeground(norm_color)  # Default

            if i < len(rows):
                fid = rows[i]["format_id"]
                if fid in selected_ids and fid:
                    for j in range(3):
                        it = table.item(i, j)
                        if it:
                            it.setBackground(sel_bg)
                            it.setForeground(sel_fg)

    def _on_table_clicked(self, row, col):
        rows = self.table.property("_rows")
        if not rows or row >= len(rows):
            return

        r = rows[row]
        fid = r["format_id"]
        mode = self.mode_combo.currentIndex()

        if mode == 1:
            self._selected_muxed_id = fid
        elif mode == 2:
            self._selected_video_id = fid
        elif mode == 3:
            self._selected_audio_id = fid

        self._highlight_table_rows(self.table, {fid} if fid else set())
        self._update_label()
        self.selectionChanged.emit()

    def _on_video_table_clicked(self, row, col):
        rows = self.video_table.property("_rows")
        if not rows or row >= len(rows):
            return
        self._selected_video_id = rows[row]["format_id"]
        self._highlight_table_rows(
            self.video_table, {self._selected_video_id} if self._selected_video_id else set()
        )
        self._update_label()
        self.selectionChanged.emit()

    def _on_audio_selection_changed(self):
        """Qt MultiSelection 模式下，用户点选/取消行时触发，收集所有已选行"""
        rows_data = self.audio_table.property("_rows") or []
        selected_rows = set()
        for idx in self.audio_table.selectionModel().selectedRows():
            selected_rows.add(idx.row())

        self._selected_audio_ids = []
        for r_idx in sorted(selected_rows):
            if r_idx < len(rows_data):
                self._selected_audio_ids.append(rows_data[r_idx]["format_id"])

        self._selected_audio_id = self._selected_audio_ids[0] if self._selected_audio_ids else None
        self._update_label()
        self.selectionChanged.emit()

    def _update_highlight(self):
        # Deprecated by _highlight_table_rows but kept for safety if called elsewhere (unlikely)
        pass

    def _update_label(self):
        mode = self.mode_combo.currentIndex()
        label = self.selection_label

        if mode == 0:
            v_sum = self.tr("未选择")
            a_sum = self.tr("未选择")
            if self._selected_video_id:
                r = next(
                    (
                        row
                        for row in (self.video_table.property("_rows") or [])
                        if row["format_id"] == self._selected_video_id
                    ),
                    None,
                )
                if r:
                    h = f"{r.get('height')}p" if r.get("height") else ""
                    ext = r.get("ext", "mp4").upper()
                    sz = _format_size(r.get("filesize"))
                    dyn = r.get("dynamic_range", "")
                    dyn_str = f" {dyn}" if dyn and dyn != "SDR" else ""
                    v_sum = f"{h}{dyn_str} {ext} ({sz})"

            sel_a = (
                self._selected_audio_ids
                if getattr(self, "_selected_audio_ids", [])
                else (
                    [self._selected_audio_id] if getattr(self, "_selected_audio_id", None) else []
                )
            )
            if sel_a:
                n = len(sel_a)
                if n == 1:
                    r = next(
                        (
                            row
                            for row in (self.audio_table.property("_rows") or [])
                            if row["format_id"] == sel_a[0]
                        ),
                        None,
                    )
                    if r:
                        ac = (r.get("acodec") or "").split(".")[0].upper()
                        ab = f"{int(r.get('abr') or 0)}kbps"
                        a_sum = f"{ac} {ab}"
                else:
                    a_sum = f"已选 {n} 条音轨"

            self.video_card.set_summary(v_sum)
            self.audio_card.set_summary(a_sum)

        if mode == 1:
            label.setText(
                self.tr("已选：整合流") if self._selected_muxed_id else self.tr("请选择：整合流")
            )
        elif mode == 2:
            label.setText(
                self.tr("已选：视频流") if self._selected_video_id else self.tr("请选择：视频流")
            )
        elif mode == 3:
            label.setText(
                self.tr("已选：音频流") if self._selected_audio_id else self.tr("请选择：音频流")
            )
        else:
            sel_a = (
                self._selected_audio_ids
                if getattr(self, "_selected_audio_ids", [])
                else ([self._selected_audio_id] if self._selected_audio_id else [])
            )

            if self._selected_video_id and sel_a:
                if len(sel_a) > 1:
                    label.setText(f"已选：视频流 + {len(sel_a)} 条音轨")
                else:
                    label.setText(self.tr("已选：视频流 + 音频流"))
            elif self._selected_video_id:
                label.setText(self.tr("已选：视频流（将自动匹配最佳音频）"))
            elif sel_a:
                if len(sel_a) > 1:
                    label.setText(f"已选：{len(sel_a)} 条音轨（请再选择一个视频流）")
                else:
                    label.setText(self.tr("已选：音频流（请再选择一个视频流）"))
            else:
                label.setText(self.tr("未选择"))

    def get_selection_result(self) -> dict:
        """Returns {format: str, extra_opts: dict} or {} if invalid."""
        if getattr(self, "_current_mode", "simple") == "simple":
            sel = self.simple_widget.get_current_selection()
            if not sel:
                return {}

            intent = sel.get("intent") or {}
            rows = getattr(self, "_rows", [])
            video_rows = [r for r in rows if r.get("kind") == "video"]
            audio_rows = [r for r in rows if r.get("kind") == "audio"]
            muxed_rows = [r for r in rows if r.get("kind") == "muxed"]

            # ── 构建打分上下文（整合用户设置 + 预设意图 + 字幕配置）──────────
            pref_langs = config_manager.get("preferred_audio_languages")
            if not isinstance(pref_langs, list) or not pref_langs:
                pref_langs = ["orig", "zh-Hans", "en"]

            # 从字幕配置预填充字幕信息（决策时序：此处在 subtitle_service.apply 之前）
            # 最终容器修正由 _ensure_subtitle_compatible_container 兜底，ctx 值仅作预判
            sub_config = config_manager.get_subtitle_config()
            sub_enabled = (
                sub_config.enabled
                and sub_config.embed_type == "soft"
                and sub_config.embed_mode != "never"
            )
            sub_lang_count = len(sub_config.default_languages) if sub_enabled else 0

            intent.get("type", "video_audio")
            ctx = ScoringContext(
                is_simple_mode=True,
                max_height=intent.get("max_height"),
                prefer_ext=intent.get("prefer_ext"),
                preferred_audio_langs=pref_langs,
                embed_subtitles=sub_enabled,
                subtitle_lang_count=sub_lang_count,
            )

            # --- 纯音频模式 ---
            if intent.get("type") == "audio_only":
                best_aud = self._get_best_audio_id(audio_rows, ctx) if audio_rows else None
                # 尊重用户的音轨选择（如果有）
                audio_pick = getattr(self.simple_widget, "get_audio_pick_result", lambda: None)()
                if audio_pick and getattr(audio_pick, "format_ids", []):
                    best_aud = audio_pick.format_ids[0]
                extra: dict = {
                    "extract_audio": True,
                    "audio_format": self.get_audio_format_override()
                    or intent.get("post_audio_format", "mp3"),
                    "audio_quality": intent.get("quality", "best")
                    if intent.get("quality", "best") != "best"
                    else "320K",
                }
                return {"format": best_aud or "bestaudio/best", "extra_opts": extra}

            # --- 含视频模式：用打分引擎挑选最优视频+音频 ---
            best_vid = self._pick_best_video(video_rows, intent)
            best_aud = self._get_best_audio_id(audio_rows, ctx) if audio_rows else None

            # ── 兜底验证：打分结果是否偏离目标 ──
            if best_vid and intent.get("max_height"):
                selected_row = next((r for r in video_rows if r["format_id"] == best_vid), None)
                if selected_row:
                    actual_h = int(selected_row.get("height") or 0)
                    target_h = intent["max_height"]
                    if actual_h > 0 and target_h > 0 and actual_h <= target_h * 0.5:
                        logger.warning(
                            self.tr("打分引擎选出 {}p (format_id={}), 目标 {}p, 偏差过大"),
                            actual_h,
                            best_vid,
                            target_h,
                        )

            extra_opts: dict = {}

            if best_vid and best_aud:
                # 正常组装：视频+音频，容器由统一决策函数确定
                vid_ext = next(
                    (r.get("ext") for r in video_rows if r["format_id"] == best_vid), "mp4"
                )
                aud_ext = next(
                    (r.get("ext") for r in audio_rows if r["format_id"] == best_aud), "m4a"
                )
                merge_fmt = decide_merge_container(vid_ext, aud_ext, ctx)
                override_fmt = self.get_container_override()
                extra_opts["merge_output_format"] = override_fmt or merge_fmt

                fmt_str = f"{best_vid}+{best_aud}"
                audio_pick = getattr(self.simple_widget, "get_audio_pick_result", lambda: None)()
                if audio_pick and getattr(audio_pick, "format_ids", []):
                    # 用户通过音轨选择器做了明确选择 → 尊重用户选择
                    picked_ids = audio_pick.format_ids
                    if len(picked_ids) > 1:
                        extra_opts["audio_multistreams"] = True
                    extra_opts["__audio_track_count"] = len(picked_ids)
                    fmt_str = f"{best_vid}+" + "+".join(picked_ids)
                    # 重新推断 aud_ext 以用于容器决策（取第一条选中音轨的 ext）
                    first_picked_ext = next(
                        (r.get("ext") for r in audio_rows if r["format_id"] == picked_ids[0]), "m4a"
                    )
                    merge_fmt = decide_merge_container(vid_ext, first_picked_ext, ctx)
                    extra_opts["merge_output_format"] = override_fmt or merge_fmt

                return {"format": fmt_str, "extra_opts": extra_opts}

            elif best_vid:
                # 只有视频没有音频 → 降级找整合流
                best_muxed = self._pick_best_muxed(muxed_rows, intent)
                if best_muxed:
                    return {"format": best_muxed, "extra_opts": extra_opts}
                return {"format": best_vid, "extra_opts": extra_opts}

            elif muxed_rows:
                # 完全没有分离视频流 → 使用整合流
                best_muxed = self._pick_best_muxed(muxed_rows, intent)
                if best_muxed:
                    return {"format": best_muxed, "extra_opts": extra_opts}

            # 兜底
            return {"format": "best", "extra_opts": extra_opts}
        else:
            # Advanced 模式：用户手动选定 format_id，容器仍用无损推断
            v = self._selected_video_id
            a_ids = getattr(self, "_selected_audio_ids", [])
            m = self._selected_muxed_id

            opts = {}
            extra_opts = {}

            if m:
                opts["format"] = m
            elif v and a_ids:
                opts["format"] = f"{v}+" + "+".join(a_ids)
                if len(a_ids) > 1:
                    extra_opts["audio_multistreams"] = True
                    extra_opts["__audio_track_count"] = len(a_ids)
                vext = next((r["ext"] for r in self._rows if r["format_id"] == v), "mp4")
                aext = next((r["ext"] for r in self._rows if r["format_id"] == a_ids[0]), "m4a")
                merge = choose_lossless_merge_container(vext, aext)
                if merge:
                    opts["merge_output_format"] = merge
            elif v:
                opts["format"] = v
            elif a_ids:
                opts["format"] = "+".join(a_ids)
                if len(a_ids) > 1:
                    extra_opts["audio_multistreams"] = True
                    extra_opts["__audio_track_count"] = len(a_ids)
            else:
                return {}

            if "merge_output_format" in opts:
                extra_opts["merge_output_format"] = opts["merge_output_format"]
            override_fmt = self.get_container_override()
            if override_fmt:
                extra_opts["merge_output_format"] = override_fmt
            return {"format": opts["format"], "extra_opts": extra_opts}

    def get_summary_text(self) -> str:
        """Returns a human-readable summary of the current selection."""
        if getattr(self, "_current_mode", "simple") == "simple":
            # Simple mode: use the checked radio button text
            btn = self.simple_widget.btn_group.checkedButton()
            return btn.text() if btn else self.tr("未选择")
        else:
            # Advanced mode: use the label text
            return self.selection_label.text().replace(self.tr("已选："), "")


# ==============================================================================
# 无状态的全局格式推断核心逻辑 (用于播放列表高级预设静默解析)
# ==============================================================================


def resolve_global_format(info: dict | None, override: Any) -> tuple[str, dict]:
    """
    根据给定的全局格式覆盖配置，为指定视频 info 推断最优 format 及 extra_opts。
    override: PlaylistGlobalFormatOverride
    返回: (format_str, extra_opts_dict)
    """
    if not info or not isinstance(info.get("formats"), list):
        return _fallback_global_format_str(override)

    from ....core.config_manager import config_manager
    from ....utils.format_scorer import ScoringContext, decide_merge_container, score_audio_format

    formats = info["formats"]
    candidates = []

    # 建立精简属性表 (复用 _build_rows)
    for f in formats:
        if not isinstance(f, dict):
            continue
        fid = str(f.get("format_id") or "").strip()
        if not fid:
            continue

        vcodec = str(f.get("vcodec") or "none")
        acodec = str(f.get("acodec") or "none")
        ext = str(f.get("ext") or "-")
        height = int(f.get("height") or 0)

        kind = "unknown"
        if vcodec != "none" and acodec != "none":
            kind = "muxed"
        elif vcodec != "none" and acodec == "none":
            kind = "video"
        elif vcodec == "none" and acodec != "none":
            kind = "audio"
        else:
            continue

        if kind in ("muxed", "video") and height and height < 144:
            continue

        candidates.append(
            {
                "kind": kind,
                "format_id": fid,
                "ext": ext,
                "height": height,
                "vcodec": vcodec,
                "acodec": acodec,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "fps": f.get("fps"),
                "vbr": f.get("vbr"),
                "tbr": f.get("tbr"),
                "abr": f.get("abr"),
                "dynamic_range": f.get("dynamic_range"),
                "language": f.get("language"),
                "audio_track_type": f.get("audio_track_type"),
            }
        )

    video_rows = [r for r in candidates if r["kind"] == "video"]
    audio_rows = [r for r in candidates if r["kind"] == "audio"]
    muxed_rows = [r for r in candidates if r["kind"] == "muxed"]

    intent = override.preset_intent or {}
    download_type = override.download_type

    pref_langs = config_manager.get("preferred_audio_languages")
    if not isinstance(pref_langs, list) or not pref_langs:
        pref_langs = ["orig", "zh-Hans", "en"]

    sub_config = config_manager.get_subtitle_config()
    sub_enabled = (
        sub_config.enabled and sub_config.embed_type == "soft" and sub_config.embed_mode != "never"
    )

    ctx = ScoringContext(
        is_simple_mode=True,
        max_height=intent.get("max_height"),
        prefer_ext=intent.get("prefer_ext"),
        preferred_audio_langs=pref_langs,
        embed_subtitles=sub_enabled,
        subtitle_lang_count=len(sub_config.default_languages) if sub_enabled else 0,
    )

    # 推断最佳音轨
    best_aud = None
    if audio_rows:
        best_aud = max(audio_rows, key=lambda r: score_audio_format(r, ctx))["format_id"]

    # 推断最佳画质
    def pick_best(pool, intent_max_height, intent_prefer_ext):
        if not pool:
            return None
        p = (
            [r for r in pool if int(r.get("height") or 0) <= intent_max_height]
            if intent_max_height is not None
            else pool
        )
        if not p:
            p = pool

        if intent_prefer_ext:
            pp = [r for r in p if str(r.get("ext") or "").lower() == intent_prefer_ext]
            if pp:
                p = pp

        return max(
            p, key=lambda r: (int(r.get("height") or 0), int(r.get("vbr") or r.get("tbr") or 0))
        )["format_id"]

    best_vid = pick_best(video_rows, intent.get("max_height"), intent.get("prefer_ext"))

    if download_type == "audio_only":
        extra = {
            "extract_audio": True,
            "audio_format": override.audio_format_override
            or intent.get("post_audio_format", "mp3"),
            "audio_quality": intent.get("quality", "best")
            if intent.get("quality", "best") != "best"
            else "320K",
        }
        return best_aud or "bestaudio/best", extra

    extra_opts = {}
    if best_vid and best_aud:
        vid_ext = next((r.get("ext") for r in video_rows if r["format_id"] == best_vid), "mp4")
        aud_ext = next((r.get("ext") for r in audio_rows if r["format_id"] == best_aud), "m4a")

        merge_fmt = decide_merge_container(vid_ext, aud_ext, ctx)
        extra_opts["merge_output_format"] = override.container_override or merge_fmt
        return f"{best_vid}+{best_aud}", extra_opts

    elif best_vid:
        best_muxed = pick_best(muxed_rows, intent.get("max_height"), intent.get("prefer_ext"))
        return best_muxed or best_vid, extra_opts

    elif muxed_rows:
        best_muxed = pick_best(muxed_rows, intent.get("max_height"), intent.get("prefer_ext"))
        return best_muxed or "best", extra_opts

    return _fallback_global_format_str(override)


def _fallback_global_format_str(override: Any) -> tuple[str, dict]:
    intent = override.preset_intent or {}
    download_type = override.download_type
    opts = {}

    if download_type == "audio_only":
        format_str = "bestaudio/best"
        opts["extract_audio"] = True
        opts["audio_format"] = override.audio_format_override or intent.get(
            "post_audio_format", "mp3"
        )
        opts["audio_quality"] = (
            intent.get("quality", "best") if intent.get("quality", "best") != "best" else "320K"
        )
    elif download_type == "video_only":
        h = intent.get("max_height")
        format_str = f"bv*[height<={h}]" if h else "bestvideo/best"
    else:
        h = intent.get("max_height")
        format_str = f"bv*[height<={h}]+ba/b[height<={h}]" if h else "bestvideo+bestaudio/best"

    if download_type != "audio_only" and override.container_override:
        opts["merge_output_format"] = override.container_override

    return format_str, opts
