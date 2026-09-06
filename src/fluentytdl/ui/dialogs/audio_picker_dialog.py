from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import Qt
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
        self.titleLabel = SubtitleLabel(self.tr("精选音轨"), self)
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

        # 提取语言组用于顶部 Filter
        langs = set()
        codecs = set()
        for t in self._all_tracks:
            lang_key = (t.language or "orig").lower()
            langs.add(lang_key)
            if t.acodec:
                # 简化 codec 名字，如 opus, mp4a
                c_name = t.acodec.split(".")[0]
                codecs.add(c_name)

        # 添加 Codec
        for c in sorted(codecs):
            self.codec_combo.addItem(c.upper(), userData=c)

        self._populate_table()

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

    def _on_filter_changed(self):
        """执行表格过滤"""
        # TODO: Implement filtering later if needed
        # Currently just letting the user see all sorted tracks
        pass

    def _kind_label(self, kind: str) -> str:
        """音轨类型的显示文案。四种类型来自 `language_preference`，见 `audio_track_kind()`。"""
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
                    self.tr("💡 由于您选择了多个音轨，输出容器将自动设为 MKV 以保证兼容性。")
                )
                self._compat_label.setStyleSheet("color: #8D9BE2;")
                self._compat_label.show()
            elif count > 1 and container == "mp4":
                self._compat_label.setText(
                    self.tr(
                        "⚠ 警告: MP4 容器对多音轨支持不佳，可能会在部分播放器中无法切换音频或出现异常。\n如果您继续使用 MP4，建议仅供测试使用。"
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
