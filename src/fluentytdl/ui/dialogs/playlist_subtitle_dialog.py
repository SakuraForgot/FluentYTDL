from PySide6.QtCore import QCoreApplication
from PySide6.QtWidgets import (
    QHBoxLayout,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CheckBox,
    ComboBox,
    MessageBoxBase,
    SmoothScrollArea,
    SubtitleLabel,
    SwitchButton,
)

from ...core.config_manager import config_manager
from ...models.subtitle_config import PlaylistSubtitleOverride

PREDEFINED_LANGS = [
    ("zh-Hans", QCoreApplication.translate("PlaylistSubtitleConfigDialog", "中文 (简体)")),
    ("zh-Hant", QCoreApplication.translate("PlaylistSubtitleConfigDialog", "中文 (繁体)")),
    ("en", "English"),
    ("ja", QCoreApplication.translate("PlaylistSubtitleConfigDialog", "日本語")),
    ("ko", "한국어"),
    ("ru", "Русский"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("th", "ไทย"),
    ("vi", "Tiếng Việt"),
    ("ar", "العربية"),
]


class PlaylistSubtitleConfigDialog(MessageBoxBase):
    """
    播放列表级别的整体字幕配置弹窗。
    让用户可以全局配置播放列表下载任务的目标语言及参数。
    """

    def __init__(self, current_override: PlaylistSubtitleOverride | None = None, parent=None):
        super().__init__(parent)
        self.titleLabel = SubtitleLabel(self.tr("播放列表字幕设置"), self)
        self.viewLayout.addWidget(self.titleLabel)

        config = config_manager.get_subtitle_config()

        # --- 目标语言选取区 ---
        self.viewLayout.addWidget(BodyLabel(self.tr("选择字幕语言 (可多选):"), self))

        from PySide6.QtWidgets import QVBoxLayout, QWidget

        content_widget = QWidget(self)
        checkbox_layout = QVBoxLayout(content_widget)
        checkbox_layout.setContentsMargins(8, 8, 8, 8)
        checkbox_layout.setSpacing(10)

        self.checkboxes = {}

        # 初始语言列表
        init_langs = (
            current_override.target_languages if current_override else config.default_languages
        )

        for code, name in PREDEFINED_LANGS:
            checkbox = CheckBox(f"{name} ({code})", content_widget)
            checkbox.setChecked(code in init_langs)
            checkbox_layout.addWidget(checkbox)
            self.checkboxes[code] = checkbox

        self.lang_scroll = SmoothScrollArea(self)
        self.lang_scroll.setWidget(content_widget)
        self.lang_scroll.setWidgetResizable(True)
        self.lang_scroll.setFixedHeight(180)
        self.lang_scroll.setStyleSheet(
            "QScrollArea { background-color: transparent; border: 1px solid rgba(128,128,128,0.2); border-radius: 4px; } "
            "QWidget { background-color: transparent; }"
        )
        self.viewLayout.addWidget(self.lang_scroll)

        # --- 自动生成字幕 ---
        auto_layout = QHBoxLayout()
        auto_layout.addWidget(BodyLabel(self.tr("自动生成字幕 (当没有提供人工字幕时):"), self))
        auto_layout.addStretch(1)
        self.auto_switch = SwitchButton(self)
        self.auto_switch.setChecked(
            current_override.enable_auto_captions
            if current_override
            else config.enable_auto_captions
        )
        auto_layout.addWidget(self.auto_switch)
        self.viewLayout.addLayout(auto_layout)

        # --- 嵌入选项区域 ---
        self._embed_row = QHBoxLayout()
        self._embed_combo = ComboBox(self)
        self._embed_combo.addItems([self.tr("软嵌入到视频"), self.tr("外置字幕文件")])

        if current_override:
            self._embed_combo.setCurrentIndex(0 if current_override.embed_subtitles else 1)
        else:
            # `PlaylistSubtitleOverride` 只有 `embed_subtitles` 一个布尔，所以这里仍是
            # XOR。它覆盖的也只是「嵌入」这一半，另存由全局 `keep_external` 决定。
            self._embed_combo.setCurrentIndex(0 if config.embed else 1)

        self._embed_row.addWidget(BodyLabel(self.tr("嵌入方式:"), self))
        self._embed_row.addWidget(self._embed_combo)

        # --- 输出格式选项 ---
        self._format_combo = ComboBox(self)
        self._format_combo.addItems(["SRT", "ASS", "VTT", "LRC"])

        fmt_mapping = {"srt": 0, "ass": 1, "vtt": 2, "lrc": 3}
        if current_override and current_override.output_format:
            self._format_combo.setCurrentIndex(
                fmt_mapping.get(current_override.output_format.lower(), 0)
            )
        else:
            self._format_combo.setCurrentIndex(fmt_mapping.get(config.output_format, 0))

        self._embed_row.addSpacing(20)
        self._embed_row.addWidget(BodyLabel(self.tr("字幕格式:"), self))
        self._embed_row.addWidget(self._format_combo)
        self._embed_row.addStretch(1)
        self.viewLayout.addLayout(self._embed_row)

        # 提示
        hint = CaptionLabel(
            self.tr("💡 提示：YT-DLP 将自动下载勾选的所有语言组合。该配置将覆盖全局设置。"), self
        )
        hint.setStyleSheet("color: #8D9BE2;")
        hint.setWordWrap(True)
        self.viewLayout.addWidget(hint)

        self.yesButton.setText(self.tr("确认"))
        self.cancelButton.setText(self.tr("取消"))
        self.widget.setMinimumWidth(450)

    def get_override(self) -> PlaylistSubtitleOverride:
        langs = []
        for code, checkbox in self.checkboxes.items():
            if checkbox.isChecked():
                langs.append(code)

        embed = self._embed_combo.currentIndex() == 0
        fmt = self._format_combo.currentText().lower()
        auto = self.auto_switch.isChecked()
        return PlaylistSubtitleOverride(
            target_languages=langs,
            enable_auto_captions=auto,
            embed_subtitles=embed,
            output_format=fmt,
        )
