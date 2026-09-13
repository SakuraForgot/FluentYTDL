from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CheckBox,
    ComboBox,
    MessageBoxBase,
    SegmentedWidget,
    SubtitleLabel,
    TableWidget,
)

from ...core.config_manager import config_manager
from ...processing.audio_track_manager import (
    AudioTrack,
    extract_audio_tracks,
)
from ...processing.subtitle_manager import language_display_name
from ...utils.container_compat import check_audio_multistream_container_compat

if TYPE_CHECKING:
    from ...utils.format_scorer import ScoringContext


@dataclass
class AudioPickerResult:
    """音轨精选结果"""

    selected_tracks: list[AudioTrack]  # 用户选中的轨道
    format_ids: list[str]  # 对应的 yt-dlp format_id
    audio_multistreams: bool  # 是否启用了多音轨


class AudioPickerDialog(MessageBoxBase):
    """
    单视频下载时的音轨选择弹窗

    提供音轨多选（语言、码率等展示），以及多音轨容器兼容性提示。
    """

    def __init__(
        self,
        video_info: dict[str, Any],
        container: str | None = None,
        initial_result: AudioPickerResult | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.video_info = video_info
        self._container = container
        self._initial_result = initial_result
        self._all_tracks: list[AudioTrack] = []
        self._checkboxes: list[CheckBox] = []

        # 顶部标题
        self.titleLabel = SubtitleLabel(self.tr("选择音轨"), self)
        self.viewLayout.addWidget(self.titleLabel)

        # 筛选区
        filter_layout = QHBoxLayout()
        filter_layout.setSpacing(12)

        self.lang_segment = SegmentedWidget(self)
        self.lang_segment.addItem("all", self.tr("全部语言"))
        self.lang_segment.currentItemChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.lang_segment)

        filter_layout.addStretch(1)

        filter_layout.addWidget(CaptionLabel(self.tr("编码:"), self))
        self.codec_combo = ComboBox(self)
        self.codec_combo.addItems([self.tr("全部")])
        self.codec_combo.currentIndexChanged.connect(self._on_filter_changed)
        filter_layout.addWidget(self.codec_combo)

        self.viewLayout.addLayout(filter_layout)

        # 筛选只是"看"，不动勾选状态 —— 于是被藏起来的行有可能仍然是勾上的，
        # 而 `_get_selected_tracks()` 照样会把它算进去。不说这一句，用户看到的就是
        # "我只勾了一条，怎么下载了两个音轨"。
        self._filter_label = CaptionLabel("", self)
        self._filter_label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        self._filter_label.hide()
        self.viewLayout.addWidget(self._filter_label)

        # 音轨列表表格
        self.table = TableWidget(self)
        self.table.setColumnCount(6)
        self.table.setHorizontalHeaderLabels(
            [
                self.tr("选择"),
                self.tr("语言"),
                self.tr("类型"),
                self.tr("编码"),
                self.tr("码率"),
                self.tr("大小"),
            ]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setBorderVisible(True)
        self.table.setBorderRadius(8)
        self.table.setWordWrap(False)

        # 列宽设置
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Fixed)

        self.table.setColumnWidth(0, 60)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 90)
        self.table.setColumnWidth(4, 90)
        self.table.setColumnWidth(5, 90)

        self.viewLayout.addWidget(self.table)

        # 容器兼容性提示
        self._compat_label = CaptionLabel("", self)
        self._compat_label.setWordWrap(True)
        self._compat_label.setStyleSheet("color: #E2C08D;")
        self._compat_label.hide()
        self.viewLayout.addWidget(self._compat_label)

        # 初始加载数据
        self._load_tracks()

        # 按钮文本
        self.yesButton.setText(self.tr("确认"))
        self.cancelButton.setText(self.tr("取消"))
        self.widget.setMinimumWidth(650)
        self.widget.setMinimumHeight(450)

    def _build_scoring_ctx(self) -> ScoringContext:
        """按当前设置构建打分上下文。

        `_load_tracks()`（排序）与 `_populate_table()`（默认勾选）**必须用同一份上下文**，
        否则表格第一行和打了 ⭐ 的那行会是两条不同的音轨。
        """
        from ...utils.format_scorer import STRATEGY_ORIGINAL_FIRST, ScoringContext

        return ScoringContext(
            preferred_audio_langs=config_manager.get("preferred_audio_languages", []) or [],
            audio_strategy=config_manager.get("audio_track_strategy", STRATEGY_ORIGINAL_FIRST),
            allow_descriptive=bool(config_manager.get("audio_allow_descriptive", False)),
        )

    def _load_tracks(self):
        """加载与挂载所有音轨数据"""
        self._all_tracks = extract_audio_tracks(self.video_info, self._build_scoring_ctx())
        if not self._all_tracks:
            return

        # 语言与编码两组筛选项都**按 `_all_tracks` 的既有顺序**收集（`extract_audio_tracks`
        # 已按得分降序），这样 tab 的排列就是推荐度排列，第一个非「全部」的 tab 正是赢家的语言。
        # 用 dict 当有序集合，别用 `set` —— 那会让 tab 顺序在每次运行时变。
        langs: dict[str, str] = {}
        codecs: dict[str, str] = {}
        for t in self._all_tracks:
            lang_key = self._lang_key(t)
            if lang_key not in langs:
                langs[lang_key] = self._lang_filter_label(t)
            codec_key = self._codec_key(t)
            if codec_key and codec_key not in codecs:
                codecs[codec_key] = codec_key.upper()

        # 只有一种语言时不加语言 tab：单个「全部语言」的分段控件点不动，纯占地方
        if len(langs) > 1:
            for key, label in langs.items():
                self.lang_segment.addItem(key, label)
        self.lang_segment.setCurrentItem("all")

        for key, label in codecs.items():
            self.codec_combo.addItem(label, userData=key)

        self._populate_table()

    @staticmethod
    def _lang_key(track: AudioTrack) -> str:
        """筛选用的语言键。**不能用 `"all"`** —— 那是「全部语言」这一项的 routeKey。"""
        return (track.language or "").strip().lower() or "unknown"

    def _lang_filter_label(self, track: AudioTrack) -> str:
        """语言 tab 的文案：查得到就用本地化名，查不到用原始语言码。"""
        key = self._lang_key(track)
        if key == "unknown":
            return self.tr("未知")
        return language_display_name(track.language or key)

    @staticmethod
    def _codec_key(track: AudioTrack) -> str:
        """编码筛选键：`mp4a.40.2` → `mp4a`。与表格「编码」列取的是同一段。"""
        return (track.acodec or "").split(".")[0].strip().lower()

    def _populate_table(self):
        """用 _all_tracks 填充表格并自动勾选默认项"""
        # 预先选出最好的 N 个音轨，默认勾选
        # 为了兼容，默认专业模式可以选择 1 个（或全选），这里先按 Top 1 勾选
        from ...processing.audio_track_manager import select_best_n_tracks

        ctx = self._build_scoring_ctx()

        # 如果是专业模式，默认选第一个最好的
        if self._initial_result and self._initial_result.format_ids:
            best_ids = self._initial_result.format_ids
        else:
            best_ids = select_best_n_tracks(self.video_info, n=1, context=ctx)

        self.table.setRowCount(0)
        self._checkboxes.clear()

        for row_idx, track in enumerate(self._all_tracks):
            self.table.insertRow(row_idx)

            # CheckBox
            cb = CheckBox(self.table)
            cb.setChecked(track.format_id in best_ids)
            cb.stateChanged.connect(self._update_compat_hint)

            # 居中 CheckBox
            w = QWidget()
            layout_box = QHBoxLayout(w)
            layout_box.setContentsMargins(0, 0, 0, 0)
            layout_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout_box.addWidget(cb)
            self.table.setCellWidget(row_idx, 0, w)
            self._checkboxes.append(cb)

            # Language
            lang_str = track.display_name or track.language or self.tr("未知/原音")
            item_lang = QTableWidgetItem(lang_str)
            item_lang.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 1, item_lang)

            # Type：四态显式区分。旧实现是 `original` 二选一，而 `audio_track_type`
            # 在 yt-dlp 里根本不存在（恒为 None），所以这一列以前永远显示"配音"。
            item_type = QTableWidgetItem(self._kind_label(track.kind))
            item_type.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 2, item_type)

            # Codec
            codec_str = (track.acodec or "").split(".")[0].upper()
            item_codec = QTableWidgetItem(codec_str)
            item_codec.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 3, item_codec)

            # Bitrate
            br_str = f"{int(track.abr)} kbps" if track.abr else self.tr("未知")
            # 推荐标识：如果它是 best_ids 的一员
            if track.format_id in best_ids:
                br_str += " ⭐"

            item_br = QTableWidgetItem(br_str)
            item_br.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 4, item_br)

            # Size
            size_str = self.tr("未知")
            if track.filesize:
                mb = track.filesize / 1024 / 1024
                size_str = f"{mb:.1f} MB"
            item_size = QTableWidgetItem(size_str)
            item_size.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row_idx, 5, item_size)

        self._update_compat_hint()
        self._on_filter_changed()

    def _on_filter_changed(self, *_args):
        """按语言 tab 与编码下拉框隐藏/显示表格行。

        隐藏而不是重建表格：`_checkboxes[i]` 与 `_all_tracks[i]` 是**下标对齐**的，
        `_get_selected_tracks()` 直接靠这个对应关系取值。重建行会打断对齐，进而让
        筛选一次就丢掉用户已经勾好的选择。

        两个信号形参不同（`currentItemChanged(str)` / `currentIndexChanged(int)`），
        `*_args` 吃掉它们；本方法也被 `_populate_table()` 无参调用。
        """
        lang_key = self.lang_segment.currentRouteKey() or "all"
        codec_key = self.codec_combo.currentData()  # 「全部」那项没有 userData → None

        for row_idx, track in enumerate(self._all_tracks):
            show = (lang_key == "all" or self._lang_key(track) == lang_key) and (
                not codec_key or self._codec_key(track) == codec_key
            )
            self.table.setRowHidden(row_idx, not show)

        self._refresh_filter_notice()

    def _refresh_filter_notice(self):
        """更新筛选提示。勾选变化时也要重跑 —— 勾一条再把它筛掉，提示得跟着变。"""
        hidden = [i for i in range(len(self._all_tracks)) if self.table.isRowHidden(i)]
        if not hidden:
            self._filter_label.hide()
            return

        if any(self._checkboxes[i].isChecked() for i in hidden):
            self._filter_label.setText(
                self.tr("筛选只影响显示；已隐藏的行里仍有勾选，确认时会一并生效。")
            )
        else:
            self._filter_label.setText(self.tr("已隐藏 {0} 条不匹配的音轨。").format(len(hidden)))
        self._filter_label.show()

    def _kind_label(self, kind: str) -> str:
        """音轨类型的显示文案。四种类型来自 `language_preference`，见 `audio_track_kind()`。

        ⚠️ 只有「原音」与「配音」两档在真实视频上见过；「默认」(`5`) 与
        「音频描述」(`-10`) 的文案只在合成数据里显示过（真实 YouTube 探了 9 个视频
        都没有这两档）。文案本身无风险，但如果日后收到"这一列显示得不对"的反馈，
        先怀疑这两档 —— 判定侧的实测覆盖说明在 `format_scorer.AUDIO_DEFAULT` 处。
        """
        return {
            "original": self.tr("原音"),
            "default": self.tr("默认"),
            "dub": self.tr("配音"),
            "descriptive": self.tr("音频描述"),
        }.get(kind, self.tr("未知"))

    def _get_selected_tracks(self) -> list[AudioTrack]:
        selected = []
        for i, cb in enumerate(self._checkboxes):
            if cb.isChecked():
                selected.append(self._all_tracks[i])
        return selected

    def _update_compat_hint(self):
        """联动更新容器兼容性提示"""
        if self._checkboxes:
            self._refresh_filter_notice()

        selected = self._get_selected_tracks()
        count = len(selected)

        container = (self._container or "").lower()
        conflict = check_audio_multistream_container_compat(container, count)

        if conflict:
            self._compat_label.setText(conflict)
            self._compat_label.setStyleSheet("color: #E2C08D;")
            self._compat_label.show()
        else:
            if count > 1 and not container:
                self._compat_label.setText(
                    self.tr(
                        "💡 已选择多个音轨，应用将自动使用 MKV 容器。请确认播放器支持所选音频编码。"
                    )
                )
                self._compat_label.setStyleSheet("color: #8D9BE2;")
                self._compat_label.show()
            elif count > 1 and container == "mp4":
                self._compat_label.setText(
                    self.tr(
                        "MP4 的多音轨切换取决于播放器支持。若目标播放器无法切换音轨，可改用 MKV。"
                    )
                )
                self._compat_label.setStyleSheet("color: #E2C08D;")
                self._compat_label.show()
            else:
                self._compat_label.hide()

    def get_result(self) -> AudioPickerResult:
        tracks = self._get_selected_tracks()
        # 按照 yt-dlp 预期，如果多选，返回所有的 ID
        # 为了保证主要音轨排前面，可以依赖 extract_audio_tracks 时已按 score 排序的顺序
        f_ids = [t.format_id for t in tracks]
        return AudioPickerResult(
            selected_tracks=tracks, format_ids=f_ids, audio_multistreams=len(f_ids) > 1
        )
