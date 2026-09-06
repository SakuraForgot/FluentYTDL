from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    SmoothScrollArea,
)

from ....processing.subtitle_manager import (
    SubtitleSourceType,
    SubtitleTrack,
    extract_subtitle_tracks,
)
from ....utils import bcp47


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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        # 1. 字幕列表 (使用 SmoothScrollArea + QGridLayout)
        self.scroll_area = SmoothScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setStyleSheet(
            "QScrollArea { background-color: transparent; border: 1px solid rgba(128,128,128,0.2); border-radius: 8px; } "
            "QWidget#scrollContent { background-color: transparent; }"
        )
        self.scroll_content = QWidget()
        self.scroll_content.setObjectName("scrollContent")
        self.grid_layout = QGridLayout(self.scroll_content)
        self.grid_layout.setContentsMargins(8, 8, 8, 8)
        self.grid_layout.setSpacing(8)
        self.grid_layout.setColumnStretch(0, 0)
        self.grid_layout.setColumnStretch(1, 1)
        self.grid_layout.setColumnStretch(2, 0)
        self.grid_layout.setColumnStretch(3, 0)

        # 添加表头
        self.grid_layout.addWidget(BodyLabel(self.tr("选择")), 0, 0, Qt.AlignmentFlag.AlignCenter)
        self.grid_layout.addWidget(BodyLabel(self.tr("语言")), 0, 1, Qt.AlignmentFlag.AlignLeft)
        self.grid_layout.addWidget(BodyLabel(self.tr("类型")), 0, 2, Qt.AlignmentFlag.AlignCenter)
        self.grid_layout.addWidget(
            BodyLabel(self.tr("原始格式")), 0, 3, Qt.AlignmentFlag.AlignCenter
        )

        # 添加分割线
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        line.setStyleSheet(
            "background-color: rgba(128, 128, 128, 0.2); border: none; max-height: 1px;"
        )
        self.grid_layout.addWidget(line, 1, 0, 1, 4)

        self.scroll_area.setWidget(self.scroll_content)
        layout.addWidget(self.scroll_area)

        # 无字幕提示
        self.noSubtitleLabel = CaptionLabel(self.tr("该视频无可用字幕"), self)
        self.noSubtitleLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.noSubtitleLabel.setStyleSheet("color: #888; margin: 20px;")
        self.noSubtitleLabel.hide()
        layout.addWidget(self.noSubtitleLabel)

        # 2. 底部选项栏
        optRow = QHBoxLayout()
        optRow.setSpacing(16)

        optRow.addStretch()

        # 添加格式选择
        self.formatLabel = CaptionLabel(self.tr("字幕格式:"), self)
        self.formatCombo = ComboBox(self)
        self.formatCombo.addItems(["srt", "ass", "vtt", "lrc", "json3"])

        from ....core.config_manager import config_manager

        out_fmt = config_manager.get("subtitle_output_format", "srt")
        idx = self.formatCombo.findText(out_fmt)
        if idx >= 0:
            self.formatCombo.setCurrentIndex(idx)

        self.formatCombo.currentIndexChanged.connect(
            lambda: config_manager.set("subtitle_output_format", self.formatCombo.currentText())
        )

        optRow.addWidget(self.formatLabel)
        optRow.addWidget(self.formatCombo)

        # 嵌入选项 (默认隐藏，由外部控制显示)
        self.embedCheck = CheckBox(self.tr("嵌入到视频"), self)
        self.embedCheck.setChecked(True)
        self.embedCheck.hide()  # 默认隐藏
        optRow.addWidget(self.embedCheck)

        layout.addLayout(optRow)

    def _preferred_languages(self) -> list[str]:
        """用户配置的字幕语言偏好，用于排序与默认勾选。

        这里读的是设置页那份"字幕语言"（`subtitle_default_languages`）—— 偏好是
        `zh-Hans` / `en` 这样的裸语种码，落到具体视频上要靠 `bcp47` 去命中真实键。
        """
        from ....core.config_manager import config_manager

        prefs = config_manager.get("subtitle_default_languages", ["zh-Hans", "en"])
        if not isinstance(prefs, list):
            return []
        return [str(p) for p in prefs if str(p).strip()]

    def _load_subtitles(self):
        """加载可用字幕列表"""
        self._tracks = extract_subtitle_tracks(self.info)

        if not self._tracks:
            self.scroll_area.hide()
            self.noSubtitleLabel.show()
            return

        self.scroll_area.show()
        self.noSubtitleLabel.hide()

        # 排序：用户偏好命中度 > 匹配紧密度 > 人工/自动 > 代码
        #
        # 老代码是 `priority.index(t.lang_code)` 拿**精确代码**比对一张
        # `["zh-Hans", "zh-Hant", "zh", "en", "ja", "ko"]` 的硬编码表 —— 真实 YouTube
        # 字幕键是 `en-GB` / `zh-Hans-en-GB`，一条都 index 不到，整批落进 100 兜底档，
        # 那张表对真实数据**整体失效**。这里改成按用户配置的偏好逐条匹配。
        #
        # 偏好排在质量前面是刻意的：下面会把命中偏好的行默认勾上，它们必须不用滚动
        # 就能看见。同一偏好内部仍是人工优先（quality_rank）。
        prefs = self._preferred_languages()

        def sort_key(t: SubtitleTrack) -> tuple[int, int, int, str]:
            pref_idx, tier = bcp47.preference_rank(prefs, t.lang_code)
            return pref_idx, tier, t.quality_rank, t.lang_code

        self._tracks.sort(key=sort_key)

        # 默认勾选：每个偏好勾最合适的那一条（排序后第一个命中的即是）。
        #
        # 老逻辑 `idx == 0 and lang_code.startswith("zh") and MANUAL` 只看排序后的第一行、
        # 且只认中文。报告里那条 BBC 视频排完序首行是 `en-GB` 人工字幕，于是**一个都不勾**，
        # 用户点开选择器看到的是全空。
        #
        # 用 set 而不是"跳过已占用"：一个偏好最多贡献一行，勾上的总数不会超过偏好数 ——
        # 这与 `bcp47.resolve_requested(per_pref_limit=1)` 是同一条硬约束，
        # 多勾一条就可能凭空触发 `container_compat` 的 mp4→mkv 升级。
        preselect: set[int] = set()
        for pref in prefs:
            hit = next(
                (i for i, t in enumerate(self._tracks) if bcp47.matches(pref, t.lang_code)),
                None,
            )
            if hit is not None:
                preselect.add(hit)

        # 清理旧的行 (保留表头和分割线)
        while self.grid_layout.count() > 5:
            item = self.grid_layout.takeAt(5)
            if item.widget():
                item.widget().deleteLater()

        self.checkboxes = []

        for idx, track in enumerate(self._tracks):
            row = idx + 2  # 行 0 是表头, 行 1 是分割线

            # 1. Checkbox
            cb = CheckBox()
            if idx in preselect:
                cb.setChecked(True)

            cb.stateChanged.connect(self.selectionChanged)
            cb.setProperty("track_data", track)
            self.grid_layout.addWidget(cb, row, 0, Qt.AlignmentFlag.AlignCenter)
            self.checkboxes.append(cb)

            # 2. Language
            lang_text = f"{track.display_name} ({track.lang_code})"
            lang_label = BodyLabel(lang_text)
            self.grid_layout.addWidget(lang_label, row, 1, Qt.AlignmentFlag.AlignLeft)

            # 3. Type
            if track.source_type == SubtitleSourceType.MANUAL:
                type_text = self.tr("人工")
                color = None
            elif track.source_type == SubtitleSourceType.AUTO_GENERATED:
                type_text = self.tr("自动生成")
                color = "#888888"
            else:
                type_text = self.tr("自动翻译")
                color = None

            type_label = BodyLabel(type_text)
            if color:
                type_label.setStyleSheet(f"color: {color};")
            self.grid_layout.addWidget(type_label, row, 2, Qt.AlignmentFlag.AlignCenter)

            # 4. Format
            fmt_label = BodyLabel(track.ext.upper())
            self.grid_layout.addWidget(fmt_label, row, 3, Qt.AlignmentFlag.AlignCenter)

    def get_selected_tracks(self) -> list[SubtitleTrack]:
        """获取用户选中的轨道"""
        selected = []
        if not hasattr(self, "checkboxes"):
            return selected
        for cb in self.checkboxes:
            if cb.isChecked():
                track = cb.property("track_data")
                if track:
                    selected.append(track)
        return selected

    def get_selected_language_codes(self) -> tuple[list[str], bool, bool]:
        """返回 (语言代码列表, 是否包含人工, 是否包含自动)

        供 SubtitlePickerDialog 使用，不包含 yt-dlp 控制参数。
        """
        tracks = self.get_selected_tracks()
        languages = list(dict.fromkeys(t.lang_code for t in tracks))  # 有序去重
        has_manual = any(t.source_type == SubtitleSourceType.MANUAL for t in tracks)
        has_auto = any(t.source_type != SubtitleSourceType.MANUAL for t in tracks)
        return languages, has_manual, has_auto

    def set_initial_state(self, selected_langs: list[str]):
        """根据之前的选择恢复 checkbox 状态和外部设置"""
        if not selected_langs or not hasattr(self, "checkboxes"):
            return

        for cb in self.checkboxes:
            track = cb.property("track_data")
            if track:
                cb.setChecked(track.lang_code in selected_langs)

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
            if t.source_type != SubtitleSourceType.MANUAL:
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

        out_fmt = self.formatCombo.currentText()
        if out_fmt:
            opts["convertsubtitles"] = out_fmt

        return opts
