"""Read-only media inspection view; file operations belong to the service."""

from __future__ import annotations

import json

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QFileDialog, QHBoxLayout, QVBoxLayout
from qfluentwidgets import (
    CardWidget,
    CheckBox,
    ComboBox,
    LineEdit,
    Pivot,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    StrongBodyLabel,
    SubtitleLabel,
    TableView,
)

from ..utils.ui_text import tr_text


def display(value):
    if value is None or value == "" or value == []:
        return "—"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "\n".join(value)
    if isinstance(value, dict) and set(value) == {"fraction", "fps"}:
        return f"{value['fps']:.6g} fps ({value['fraction']})"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, indent=2)
    return str(value)


def field_names():
    return {
        "container": tr_text("容器"),
        "size": tr_text("文件大小"),
        "duration": tr_text("时长"),
        "total_bitrate": tr_text("总平均码率"),
        "title": tr_text("标题"),
        "artist": tr_text("作者 / 艺术家"),
        "album_artist": tr_text("专辑艺术家"),
        "album": tr_text("专辑"),
        "date": tr_text("日期"),
        "year": tr_text("年份"),
        "genre": tr_text("流派"),
        "comment": tr_text("备注"),
        "description": tr_text("描述"),
        "authors_json": tr_text("作者列表"),
        "channel": tr_text("频道"),
        "uploader": tr_text("上传者"),
        "webpage_url": tr_text("来源链接"),
        "codec_name": tr_text("编码"),
        "width": tr_text("宽度"),
        "height": tr_text("高度"),
        "average_rate": tr_text("平均帧率"),
        "reference_rate": tr_text("参考帧率"),
        "reported_frames": tr_text("容器报告帧数"),
        "reported_bitrate": tr_text("轨道码率"),
        "sample_rate": tr_text("采样率"),
        "channels": tr_text("声道数"),
        "channel_layout": tr_text("声道布局"),
        "frame_rate_mode": tr_text("恒定或可变帧率"),
    }


def source_name(source):
    return {
        "probe": "FFprobe",
        "native_tag": tr_text("原生标签"),
        "filesystem": tr_text("文件系统"),
        "derived": tr_text("按文件大小和时长估算（含容器开销）"),
    }.get(source, source)


def issue_text(code):
    return {
        "cancelled": tr_text("已取消；已有结果仅供参考。"),
        "path_missing": tr_text("任务未记录成品路径，请手动选择文件。"),
        "file_missing": tr_text("文件不存在或已移动，请重新选择文件。"),
        "file_denied": tr_text("无法读取文件，请检查访问权限。"),
        "not_file": tr_text("请选择单个音视频文件。"),
        "empty_file": tr_text("文件为空，无法识别。"),
        "local_file_only": tr_text("仅支持本地文件，请勿输入网址或网络路径。"),
        "unsupported_media": tr_text("该文件不是支持的音视频文件。"),
        "tool_unavailable": tr_text("未找到 FFprobe，请在设置中安装或配置 FFmpeg。"),
        "configured_probe_missing": tr_text("配置目录中缺少 FFprobe，请检查 FFmpeg 设置。"),
        "reader_timeout": tr_text("读取超时，请重试或选择其他文件。"),
        "output_limit": tr_text("读取结果超过限制，仅保留已获取的信息。"),
        "native_limit": tr_text("原生标签超过读取限制，显示已有技术信息。"),
        "native_failed": tr_text("原生标签读取失败，显示已有技术信息。"),
        "file_changed": tr_text("文件在读取期间发生变化，请重新识别。"),
        "export_is_source": tr_text("不能用报告覆盖媒体源文件。"),
        "export_failed": tr_text("导出失败，请检查目标位置和写入权限。"),
    }.get(code, tr_text("读取失败，请检查文件或重试。") + f" ({code})")


class MediaInfoPage(CardWidget):
    inspect_requested = Signal(str)
    cancel_requested = Signal()
    export_requested = Signal(object, str, dict)
    back_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mediaInfoPage")
        self.setAcceptDrops(True)
        self.result = None
        self.path = ""
        self.tab = "overview"
        self.final = False
        self.export_running = False
        self.export_snapshot = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(12)
        layout.addWidget(SubtitleLabel(tr_text("媒体信息"), self))
        self.context = StrongBodyLabel(
            tr_text("选择或拖入一个本地音视频文件，只读取，不修改。"), self
        )
        self.context.setWordWrap(True)
        self.context.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.context)
        toolbar = QHBoxLayout()
        self.choose_button = PrimaryPushButton(tr_text("选择文件"), self)
        self.choose_button.clicked.connect(self.choose_file)
        self.retry_button = PushButton(tr_text("重新识别"), self)
        self.retry_button.clicked.connect(lambda: self.open_file(self.path, keep_context=True))
        self.cancel_button = PushButton(tr_text("取消"), self)
        self.cancel_button.clicked.connect(self.cancel_requested)
        self.back_button = PushButton(tr_text("返回下载列表"), self)
        self.back_button.clicked.connect(self.back_requested)
        self.back_button.hide()
        self.settings_button = PushButton(tr_text("打开设置"), self)
        self.settings_button.clicked.connect(self.settings_requested)
        self.settings_button.hide()
        for button in (
            self.choose_button,
            self.retry_button,
            self.cancel_button,
            self.back_button,
            self.settings_button,
        ):
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        self.path_label = StrongBodyLabel(self)
        self.path_label.setWordWrap(True)
        self.path_label.setTextFormat(Qt.TextFormat.PlainText)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)
        self.status = StrongBodyLabel(tr_text("尚未选择文件"), self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(self.status)
        self.pivot = Pivot(self)
        for key, title in (
            ("overview", tr_text("概览")),
            ("video", tr_text("视频")),
            ("audio", tr_text("音频")),
            ("metadata", tr_text("元数据")),
            ("chapters", tr_text("章节与附件")),
            ("raw", tr_text("原始读取结果")),
        ):
            self.pivot.addItem(routeKey=key, text=title, onClick=lambda k=key: self.set_tab(k))
        self.pivot.setCurrentItem("overview")
        layout.addWidget(self.pivot)
        self.stream_choice = ComboBox(self)
        self.stream_choice.currentIndexChanged.connect(self.render)
        self.stream_choice.hide()
        layout.addWidget(self.stream_choice)
        self.search = LineEdit(self)
        self.search.setPlaceholderText(tr_text("筛选当前视图的字段、值或来源"))
        layout.addWidget(self.search)
        self.table = TableView(self)
        self.table.setEditTriggers(TableView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(TableView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(TableView.SelectionMode.SingleSelection)
        self.model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setFilterRole(Qt.ItemDataRole.UserRole)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.table.setModel(self.proxy)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().hide()
        self.table.clicked.connect(self.show_detail)
        self.table.selectionModel().currentChanged.connect(self.show_detail)
        self.search.textChanged.connect(self.proxy.setFilterFixedString)
        layout.addWidget(self.table, 1)
        self.detail = PlainTextEdit(self)
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(110)
        self.detail.setPlaceholderText(tr_text("选中一行查看完整值；可选中文字复制。"))
        layout.addWidget(self.detail)
        options = QHBoxLayout()
        self.include_path = CheckBox(tr_text("报告包含完整路径"), self)
        self.include_raw = CheckBox(tr_text("报告包含原始结果"), self)
        self.copy_button = PushButton(tr_text("复制摘要"), self)
        self.copy_button.clicked.connect(self.copy_summary)
        self.export_button = PushButton(tr_text("导出 JSON"), self)
        self.export_button.clicked.connect(self.save_report)
        for widget in (self.include_path, self.include_raw):
            options.addWidget(widget)
        options.addStretch()
        layout.addLayout(options)
        report_actions = QHBoxLayout()
        report_note = StrongBodyLabel(tr_text("报告包含文件中的描述、备注等标签内容。"), self)
        report_note.setWordWrap(True)
        report_actions.addWidget(report_note, 1)
        report_actions.addWidget(self.copy_button)
        report_actions.addWidget(self.export_button)
        layout.addLayout(report_actions)
        self.set_busy(False)

    def set_busy(self, busy):
        self.cancel_button.setEnabled(busy)
        self.retry_button.setEnabled(bool(self.path) and not busy)
        self.copy_button.setEnabled(self.result is not None)
        self.export_button.setEnabled(
            self.result is not None and self.final and not busy and not self.export_running
        )

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, tr_text("选择音视频文件"), "", tr_text("所有文件 (*)")
        )
        if path:
            self.open_file(path)

    def open_file(self, path, *, task_title=None, keep_context=False):
        self.path = path
        self.result = None
        self.final = False
        self.path_label.setText(path)
        self.detail.clear()
        self.settings_button.hide()
        if not keep_context:
            self.back_button.setVisible(task_title is not None)
            self.context.setText(
                tr_text("来自下载任务的主成品：{0}", task_title)
                if task_title is not None
                else tr_text("选择或拖入一个本地音视频文件，只读取，不修改。")
            )
        self.render()
        self.set_busy(True)
        self.inspect_requested.emit(path)

    def set_progress(self, stage):
        self.status.setText(
            {
                "validating": tr_text("正在检查文件…"),
                "switching": tr_text("正在切换文件…"),
                "probing": tr_text("正在读取技术信息…"),
                "reading_tags": tr_text("正在读取原生标签…"),
            }.get(stage, stage)
        )

    def show_result(self, result, final):
        self.result = result
        self.final = final
        self.set_tab(self.tab)
        self.set_busy(not final)
        if final:
            notes = [issue_text(code) for code in result.issues]
            notes.append(tr_text("FFprobe 展示读取器可识别的字段；原始视图不代表文件的全部字节。"))
            if result.coverage.get("native") == "unsupported":
                notes.append(tr_text("此容器暂不支持独立原生标签读取。"))
            self.status.setText(tr_text("读取完成") + " · " + " ".join(notes))

    def show_error(self, code):
        self.final = False
        self.set_busy(False)
        self.status.setText(issue_text(code))
        self.settings_button.setVisible(code in {"tool_unavailable", "configured_probe_missing"})

    def set_tab(self, key):
        self.tab = key
        self.pivot.setCurrentItem(key)
        self.stream_choice.blockSignals(True)
        previous = self.stream_choice.currentData()
        self.stream_choice.clear()
        if self.result and key in {"video", "audio"}:
            for stream in self.result.streams:
                if stream.get("codec_type") == key and not stream.get("disposition", {}).get(
                    "attached_pic"
                ):
                    self.stream_choice.addItem(
                        f"#{stream['index']} · {stream.get('codec_name', '—')}",
                        userData=stream["index"],
                    )
            index = self.stream_choice.findData(previous)
            if index >= 0:
                self.stream_choice.setCurrentIndex(index)
        self.stream_choice.blockSignals(False)
        self.stream_choice.setVisible(key in {"video", "audio"})
        self.render()

    def render(self, *_):
        self.model.clear()
        self.detail.clear()
        self.model.setHorizontalHeaderLabels(
            [tr_text("字段"), tr_text("值"), tr_text("来源 / 范围")]
        )
        if not self.result:
            return
        result, rows, names = self.result, [], field_names()
        if self.tab in {"overview", "metadata"}:
            fields = result.fields[:4] if self.tab == "overview" else result.fields[4:]
            if self.tab == "metadata":
                fields = list(fields)
                for key in (
                    "title",
                    "artist",
                    "authors_json",
                    "channel",
                    "uploader",
                    "date",
                    "webpage_url",
                ):
                    if not any(f["key"] == key for f in fields):
                        fields.append({"key": key, "value": None, "source": "", "scope": ""})
            for item in fields:
                value = display(item["value"])
                if item["value"] is None:
                    value = tr_text("未读取到（不代表文件中一定不存在）")
                annotation = ""
                if item.get("state") == "conflict":
                    annotation = tr_text("候选值存在差异")
                if item.get("role"):
                    annotation += " · " + (
                        tr_text("角色未标注")
                        if item["role"] == "unspecified"
                        else tr_text("角色由文件声明")
                    )
                if item.get("unit") and item["value"] is not None:
                    value += " " + item["unit"]
                rows.append(
                    (
                        names.get(item["key"], item["key"]),
                        value,
                        " · ".join(
                            filter(
                                None,
                                (
                                    source_name(item["source"]),
                                    item["scope"],
                                    item.get("native_key", ""),
                                    annotation,
                                ),
                            )
                        ),
                    )
                )
            if self.tab == "overview":
                rows += [
                    (tr_text("读取时间"), result.read_at, ""),
                    (tr_text("读取器版本"), display(result.reader_versions), ""),
                    (tr_text("字段覆盖范围"), display(result.coverage), ""),
                ]
                rows += [
                    (
                        f"stream:{s.get('index')}",
                        display(
                            {
                                k: s[k]
                                for k in ("codec_type", "codec_name", "disposition", "tags")
                                if k in s
                            }
                        ),
                        "FFprobe",
                    )
                    for s in result.streams
                ]
        elif self.tab in {"video", "audio"}:
            stream = next(
                (s for s in result.streams if s.get("index") == self.stream_choice.currentData()),
                {},
            )
            keys = ["codec_name", "duration", "reported_bitrate"]
            keys += (
                [
                    "width",
                    "height",
                    "average_rate",
                    "reference_rate",
                    "reported_frames",
                    "frame_rate_mode",
                    "pix_fmt",
                    "color_space",
                    "color_transfer",
                ]
                if self.tab == "video"
                else ["sample_rate", "channels", "channel_layout", "sample_fmt"]
            )
            for key in keys:
                value = stream.get(key)
                if key == "frame_rate_mode" and stream:
                    value = tr_text("未进行逐帧分析")
                unit = {"duration": " s", "reported_bitrate": " bps", "sample_rate": " Hz"}.get(
                    key, ""
                )
                rows.append(
                    (
                        names.get(key, key),
                        display(value) + (unit if value is not None else ""),
                        "FFprobe",
                    )
                )
        elif self.tab == "chapters":
            rows += [(f"chapter:{c.get('id')}", display(c), "FFprobe") for c in result.chapters]
            rows += [
                (f"stream:{s.get('index')}", display(s), "FFprobe")
                for s in result.streams
                if s.get("codec_type") == "attachment"
                or s.get("disposition", {}).get("attached_pic")
            ]
            rows += [
                (t["key"], display(t["values"]), tr_text("原生标签"))
                for t in result.tags
                if t.get("binary")
            ]
        else:
            rows += [
                (t["key"], display(t["values"]), f"{t['reader']} · {t['scope']}")
                for t in result.tags
            ]
            rows.append((tr_text("FFprobe 原始 JSON"), display(result.raw), "FFprobe"))
        if not rows:
            rows.append((tr_text("无可展示的信息"), "—", ""))
        for row in rows:
            items = []
            for text in row:
                item = QStandardItem(
                    text[:500].replace("\n", " ") + ("…" if len(text) > 500 else "")
                )
                item.setData(text, Qt.ItemDataRole.UserRole)
                item.setToolTip(text[:2048])
                items.append(item)
            self.model.appendRow(items)
        self.table.setColumnWidth(0, 170)
        self.table.setColumnWidth(1, 360)

    def show_detail(self, index, *_):
        if not index.isValid():
            return
        source = self.proxy.mapToSource(index)
        column = 2 if source.column() == 2 else 1
        self.detail.setPlainText(
            self.model.item(source.row(), column).data(Qt.ItemDataRole.UserRole)
        )

    def copy_summary(self):
        if self.result:
            text = json.dumps(self.result.report(), ensure_ascii=False, indent=2)
            QApplication.clipboard().setText(text)

    def save_report(self):
        if not self.result or not self.final:
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, tr_text("导出媒体信息"), "media-info.json", "JSON (*.json)"
        )
        if destination:
            self.export_running = True
            self.export_snapshot = self.result
            self.export_button.setEnabled(False)
            self.export_requested.emit(
                self.result,
                destination,
                {
                    "include_path": self.include_path.isChecked(),
                    "include_raw": self.include_raw.isChecked(),
                },
            )

    def export_finished(self, error):
        self.export_running = False
        self.export_button.setEnabled(self.result is not None and self.final)
        if self.result is self.export_snapshot:
            self.status.setText(issue_text(error) if error else tr_text("报告已导出"))
        self.export_snapshot = None

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            self.status.setText(issue_text("not_file"))

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.open_file(urls[0].toLocalFile())
            event.acceptProposedAction()
