from dataclasses import dataclass

from PySide6.QtWidgets import QHBoxLayout
from qfluentwidgets import CaptionLabel, ComboBox, MessageBoxBase, SubtitleLabel

from fluentytdl.ui.components.platforms.subtitle import SubtitleSelectorWidget, SubtitleTrack

from ...core.config_manager import config_manager
from ...utils.container_compat import check_subtitle_container_compat


@dataclass
class SubtitlePickerResult:
    """字幕精选结果"""

    selected_tracks: list[SubtitleTrack]  # 用户选中的轨道
    embed_subtitles: bool  # 是否嵌入
    output_format: str  # 转换格式 (srt/ass/vtt)
    override_languages: list[str]  # 语言代码列表 (有序去重)
    has_manual: bool  # 是否包含人工字幕
    has_auto: bool  # 是否包含自动字幕


class SubtitlePickerDialog(MessageBoxBase):
    """单视频下载时的字幕精选弹窗

    复用 SubtitleSelectorWidget 提供按轨道粒度的字幕选择，
    并根据当前容器格式实时显示兼容性提示。
    """

    def __init__(
        self,
        video_info: dict,
        container: str | None = None,
        initial_result: SubtitlePickerResult | None = None,
        parent=None,
    ):
        super().__init__(parent)
        # 标题
        self.titleLabel = SubtitleLabel(self.tr("选择字幕"), self)
        self.viewLayout.addWidget(self.titleLabel)

        # 内嵌 SubtitleSelectorWidget
        self.selector = SubtitleSelectorWidget(video_info, self)
        if initial_result:
            self.selector.set_initial_state(initial_result.override_languages)
        self.viewLayout.addWidget(self.selector)

        # 嵌入选项区域
        self._embed_row = QHBoxLayout()

        # 嵌入模式选项（继承全局设置，允许本次覆盖）
        config = config_manager.get_subtitle_config()
        self._embed_combo = ComboBox(self)
        self._embed_combo.addItems([self.tr("软嵌入到视频"), self.tr("外置字幕文件")])

        if initial_result:
            self._embed_combo.setCurrentIndex(0 if initial_result.embed_subtitles else 1)
        elif not config.embed:
            # 这个 ComboBox 是 XOR，表达不了「都要」/「都不要」——它决定的只是本次
            # 覆盖里的 `embed_subtitles`，另存开关仍由全局 `keep_external` 说话。
            self._embed_combo.setCurrentIndex(1)

        self._embed_row.addWidget(CaptionLabel(self.tr("嵌入方式:"), self))
        self._embed_row.addWidget(self._embed_combo)

        # 输出格式选项
        self._format_combo = ComboBox(self)
        self._format_combo.addItems(["SRT", "ASS", "VTT", "LRC"])

        # 从全局配置或初始结果设置默认值
        if initial_result and initial_result.output_format:
            fmt_mapping = {"srt": 0, "ass": 1, "vtt": 2, "lrc": 3}
            self._format_combo.setCurrentIndex(
                fmt_mapping.get(initial_result.output_format.lower(), 0)
            )
        else:
            fmt_index = {"srt": 0, "ass": 1, "vtt": 2, "lrc": 3}.get(config.output_format, 0)
            self._format_combo.setCurrentIndex(fmt_index)

        self._embed_row.addWidget(CaptionLabel(self.tr("字幕格式:"), self))
        self._embed_row.addWidget(self._format_combo)
        self._embed_row.addStretch(1)

        self.viewLayout.addLayout(self._embed_row)

        # 容器兼容性提示
        self._compat_label = CaptionLabel("", self)
        self._compat_label.setWordWrap(True)
        self._compat_label.setStyleSheet("color: #E2C08D;")
        self._compat_label.hide()
        self.viewLayout.addWidget(self._compat_label)

        # 记住容器格式
        self._container = container

        # 信号连接
        self.selector.selectionChanged.connect(self._update_compat_hint)
        self._embed_combo.currentIndexChanged.connect(self._update_compat_hint)

        # 初始刷新
        self._update_compat_hint()

        # 按钮文本
        self.yesButton.setText(self.tr("确认"))
        self.cancelButton.setText(self.tr("取消"))
        self.widget.setMinimumWidth(600)  # 保证表格有足够宽度

    def _update_compat_hint(self):
        """联动：根据当前选择和容器格式更新兼容性提示"""
        # 获取用户选中的字幕数量 + 当前嵌入选择
        selected = self.selector.get_selected_tracks()
        embed = self._embed_combo.currentIndex() == 0
        lang_count = len(list(dict.fromkeys(t.lang_code for t in selected)))

        container = (self._container or "").lower()
        conflict = check_subtitle_container_compat(container, embed, lang_count)
        if conflict:
            if container == "webm":
                # WebM → 自动切换嵌入为外挂 + 显示警告
                self._compat_label.setText(
                    self.tr("⚠ 本应用不在 WebM 中嵌入字幕。请选择独立字幕文件，或改用 MKV。")
                )
                self._compat_label.setStyleSheet("color: #E2C08D;")
                self._compat_label.show()
                if embed:
                    self._embed_combo.setCurrentIndex(1)
            elif container == "mp4" and lang_count > 1:
                self._compat_label.setText(
                    self.tr(
                        "⚠ MP4 容器使用 mov_text 编码，部分播放器对多轨字幕支持不佳，建议同时使用外置字幕或选择 MKV 容器。"
                    )
                )
                self._compat_label.setStyleSheet("color: #E2C08D;")
                self._compat_label.show()
        else:
            if not self._container:
                self._compat_label.setText(
                    self.tr("💡 将根据字幕需求自动选择最佳容器 (MKV/MP4/WebM)。")
                )
                self._compat_label.setStyleSheet("color: #8D9BE2;")
                self._compat_label.show()
            else:
                self._compat_label.hide()

    def get_result(self) -> SubtitlePickerResult:
        tracks = self.selector.get_selected_tracks()
        languages, has_manual, has_auto = self.selector.get_selected_language_codes()
        embed = self._embed_combo.currentIndex() == 0
        fmt_text = self._format_combo.currentText().lower()
        return SubtitlePickerResult(
            selected_tracks=tracks,
            embed_subtitles=embed,
            output_format=fmt_text,
            override_languages=languages,
            has_manual=has_manual,
            has_auto=has_auto,
        )
