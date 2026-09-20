"""Media cards; raw evidence and export settings are opened only on demand."""

from __future__ import annotations

import json

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    Action,
    CaptionLabel,
    CardWidget,
    CheckBox,
    FluentIcon,
    IconWidget,
    InfoBarPosition,
    MessageBoxBase,
    PlainTextEdit,
    PrimaryPushButton,
    PushButton,
    RoundMenu,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    TransparentDropDownToolButton,
    TransparentToolButton,
)

from ..utils.ui_text import tr_text
from .components.common.custom_info_bar import InfoBar
from .media_info_presenter import MediaCardData, filename, present_media


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


def caption(text, parent):
    label = CaptionLabel(text, parent)
    label.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


class MediaDetailsDialog(MessageBoxBase):
    def __init__(self, title, text, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(title, self))
        self.text = PlainTextEdit(self)
        self.text.setReadOnly(True)
        self.text.setPlainText(text)
        self.text.setMinimumHeight(320)
        self.viewLayout.addWidget(self.text)
        copy = PushButton(tr_text("复制"), self)
        copy.clicked.connect(lambda: QApplication.clipboard().setText(text))
        self.viewLayout.addWidget(copy, alignment=Qt.AlignmentFlag.AlignRight)
        self.yesButton.setText(tr_text("关闭"))
        self.cancelButton.hide()
        self.widget.setMinimumWidth(min(720, max(360, parent.width() - 80)))


class MediaExportDialog(MessageBoxBase):
    def __init__(self, parent):
        super().__init__(parent)
        self.viewLayout.addWidget(SubtitleLabel(tr_text("导出媒体信息"), self))
        self.include_path = CheckBox(tr_text("报告包含完整路径"), self)
        self.include_raw = CheckBox(tr_text("报告包含原始结果"), self)
        self.viewLayout.addWidget(self.include_path)
        self.viewLayout.addWidget(self.include_raw)
        self.yesButton.setText(tr_text("选择保存位置"))
        self.cancelButton.setText(tr_text("取消"))
        self.widget.setMinimumWidth(360)

    def options(self):
        return {
            "include_path": self.include_path.isChecked(),
            "include_raw": self.include_raw.isChecked(),
        }


class MediaDataCard(CardWidget):
    def __init__(self, data: MediaCardData, parent):
        super().__init__(parent)
        self.data = data
        self.setObjectName("mediaCard_" + data.key.replace(":", "_"))
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 18)
        layout.setSpacing(12)
        header = QHBoxLayout()
        kind = data.key.split(":")[0]
        icon = {
            "content": FluentIcon.INFO,
            "video": FluentIcon.VIDEO,
            "audio": FluentIcon.MUSIC,
            "file": FluentIcon.FOLDER,
            "source": FluentIcon.LINK,
            "tags": FluentIcon.TAG,
            "description": FluentIcon.DOCUMENT,
            "subtitles": FluentIcon.FONT,
            "collection": FluentIcon.MUSIC,
            "attachments": FluentIcon.PHOTO,
        }.get(kind, FluentIcon.DOCUMENT)
        image = IconWidget(icon, self)
        image.setFixedSize(18, 18)
        header.addWidget(image)
        header.addSpacing(4)
        header.addWidget(StrongBodyLabel(data.title, self))
        header.addStretch()
        copy = TransparentToolButton(FluentIcon.COPY, self)
        copy.setToolTip(tr_text("复制此卡片"))
        copy.setAccessibleName(tr_text("复制此卡片"))
        copy.clicked.connect(lambda: QApplication.clipboard().setText(data.summary()))
        header.addWidget(copy)
        layout.addLayout(header)
        if data.hero:
            hero = SubtitleLabel(data.hero[:300], self)
            self._configure_text(hero)
            layout.addWidget(hero)
        row_limit = 12 if data.key in {"chapters", "subtitles", "attachments"} else len(data.rows)
        for label, value in data.rows[:row_limit]:
            line = QHBoxLayout()
            line.setSpacing(16)
            name = caption(label, self)
            name.setWordWrap(True)
            name.setFixedWidth(112)
            line.addWidget(name, 0, Qt.AlignmentFlag.AlignTop)
            text = StrongBodyLabel(value[:240] + ("…" if len(value) > 240 else ""), self)
            self._configure_text(text)
            text.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
            text.setToolTip(value[:2000])
            line.addWidget(text, 1)
            layout.addLayout(line)
        if data.body:
            excerpt = "\n".join(data.body.splitlines()[:5])[:500]
            body = StrongBodyLabel(excerpt + ("…" if excerpt != data.body else ""), self)
            self._configure_text(body)
            layout.addWidget(body)
        if (
            data.body
            or len(data.rows) > row_limit
            or len(data.hero) > 300
            or any(len(v) > 240 for _, v in data.rows)
        ):
            more = PushButton(tr_text("查看完整内容"), self)
            more.clicked.connect(self.show_complete)
            layout.addWidget(more, alignment=Qt.AlignmentFlag.AlignLeft)

    @staticmethod
    def _configure_text(label):
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def show_complete(self):
        MediaDetailsDialog(self.data.title, self.data.summary(), self.window()).exec()


class MediaInfoPage(QWidget):
    inspect_requested = Signal(str)
    cancel_requested = Signal()
    export_requested = Signal(object, str, dict)
    back_requested = Signal()
    settings_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("mediaInfoPage")
        self.setStyleSheet("#mediaInfoPage { background: transparent; }")
        self.setAcceptDrops(True)
        self.result = None
        self.path = ""
        self.final = False
        self.export_running = False
        self.export_snapshot = None
        self.cards = []
        self.card_data = []
        self._columns = 0
        self._layout_width = 0
        self._groups = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 18, 24, 12)
        layout.setSpacing(12)
        toolbar = QHBoxLayout()
        self.back_button = TransparentToolButton(FluentIcon.RETURN, self)
        self.back_button.setToolTip(tr_text("返回下载列表"))
        self.back_button.setAccessibleName(tr_text("返回下载列表"))
        self.back_button.clicked.connect(self.back_requested)
        self.back_button.hide()
        toolbar.addWidget(self.back_button)
        self.choose_button = PrimaryPushButton(FluentIcon.FOLDER, tr_text("选择文件"), self)
        self.choose_button.clicked.connect(self.choose_file)
        toolbar.addWidget(self.choose_button)
        self.file_label = caption("", self)
        self.file_label.setMinimumWidth(0)
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(self.file_label, 1)
        self.cancel_button = PushButton(tr_text("取消"), self)
        self.cancel_button.clicked.connect(self.cancel_requested)
        toolbar.addWidget(self.cancel_button)
        self.settings_button = PushButton(tr_text("打开设置"), self)
        self.settings_button.clicked.connect(self.settings_requested)
        self.settings_button.hide()
        toolbar.addWidget(self.settings_button)
        self.more_button = TransparentDropDownToolButton(FluentIcon.MORE, self)
        self.more_button.setToolTip(tr_text("更多操作"))
        self.more_button.setAccessibleName(tr_text("更多操作"))
        menu = RoundMenu(parent=self)
        self.retry_action = Action(FluentIcon.SYNC, tr_text("重新识别"), self)
        self.retry_action.triggered.connect(lambda: self.open_file(self.path, keep_context=True))
        self.copy_action = Action(FluentIcon.COPY, tr_text("复制摘要"), self)
        self.copy_action.triggered.connect(self.copy_summary)
        self.path_action = Action(FluentIcon.FOLDER, tr_text("复制文件路径"), self)
        self.path_action.triggered.connect(lambda: QApplication.clipboard().setText(self.path))
        self.details_action = Action(FluentIcon.INFO, tr_text("查看读取详情"), self)
        self.details_action.triggered.connect(self.show_details)
        self.export_action = Action(FluentIcon.SAVE, tr_text("导出 JSON"), self)
        self.export_action.triggered.connect(self.save_report)
        menu.addActions([self.retry_action, self.copy_action, self.path_action])
        menu.addSeparator()
        menu.addActions([self.details_action, self.export_action])
        self.more_button.setMenu(menu)
        toolbar.addWidget(self.more_button)
        layout.addLayout(toolbar)
        self.status = StrongBodyLabel(self)
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.hide()
        layout.addWidget(self.status)
        self.scroll = ScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("ScrollArea { background: transparent; border: none; }")
        self.canvas = QWidget()
        self.canvas.setObjectName("mediaCardsCanvas")
        self.canvas.setStyleSheet("#mediaCardsCanvas { background: transparent; }")
        self.grid = QVBoxLayout(self.canvas)
        self.grid.setContentsMargins(0, 0, 8, 12)
        self.grid.setSpacing(14)
        self.grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)
        self.empty = StrongBodyLabel(tr_text("拖入音视频文件"), self.canvas)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setMinimumHeight(220)
        self.grid.addWidget(self.empty)
        self.scroll.viewport().installEventFilter(self)
        self.set_busy(False)

    def _set_status(self, text):
        self.status.setText(text)
        self.status.setVisible(bool(text))

    def set_busy(self, busy):
        self.cancel_button.setVisible(busy)
        self.retry_action.setEnabled(bool(self.path) and not busy)
        self.path_action.setEnabled(bool(self.path))
        self.copy_action.setEnabled(self.result is not None)
        self.details_action.setEnabled(self.result is not None)
        self.export_action.setEnabled(
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
        self.settings_button.hide()
        self.file_label.setToolTip(path)
        self._update_filename()
        if not keep_context:
            self.back_button.setVisible(task_title is not None)
        self.render()
        self.scroll.verticalScrollBar().setValue(0)
        self.set_busy(True)
        self._set_status(tr_text("正在检查文件…"))
        self.inspect_requested.emit(path)

    def set_progress(self, stage):
        self._set_status(
            {
                "validating": tr_text("正在检查文件…"),
                "switching": tr_text("正在切换文件…"),
                "probing": tr_text("正在读取技术信息…"),
                "reading_tags": tr_text("正在读取原生标签…"),
            }.get(stage, stage)
        )

    def show_result(self, result, final):
        position = self.scroll.verticalScrollBar().value()
        self.result, self.final = result, final
        self.render()
        self.scroll.verticalScrollBar().setValue(position)
        self.set_busy(not final)
        if final:
            self._set_status(" ".join(issue_text(code) for code in result.issues))

    def show_error(self, code):
        self.final = False
        if self.result is not None:
            self.result.status = "cancelled" if code == "cancelled" else "partial"
            if code not in self.result.issues:
                self.result.issues.append(code)
        self.set_busy(False)
        self._set_status(issue_text(code))
        self.settings_button.setVisible(code in {"tool_unavailable", "configured_probe_missing"})

    def render(self):
        for card in self.cards:
            self.grid.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        self.card_data = present_media(self.result) if self.result else []
        self.empty.setVisible(not self.card_data and self.result is None)
        for data in self.card_data:
            self.cards.append(MediaDataCard(data, self.canvas))
        self._layout_cards(force=True)

    def _layout_cards(self, *, force=False):
        width = self.scroll.viewport().width()
        columns = 2 if width >= 760 else 1
        if columns == self._columns and abs(width - self._layout_width) < 24 and not force:
            return
        self._columns, self._layout_width = columns, width
        # Move cards out before retiring the old column containers. Only layout
        # ownership changes here; the data and current selection remain intact.
        for card in self.cards:
            card.setParent(self.canvas)
        while self.grid.count():
            self.grid.takeAt(0)
        for group in self._groups:
            group.hide()
            group.deleteLater()
        self._groups = []
        self.grid.addWidget(self.empty)
        pending = []

        def flush():
            if not pending:
                return
            group = QWidget(self.canvas)
            group.setObjectName("mediaCardColumns")
            group.setStyleSheet("#mediaCardColumns { background: transparent; }")
            line = QHBoxLayout(group)
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(14)
            stacks = [QVBoxLayout() for _ in range(columns)]
            heights = [0] * columns
            card_width = max(1, (width - 8 - 14 * (columns - 1)) // columns)
            for stack in stacks:
                stack.setSpacing(14)
                stack.setAlignment(Qt.AlignmentFlag.AlignTop)
                line.addLayout(stack, 1)
            for card in pending:
                column = min(range(columns), key=lambda i: heights[i])
                stacks[column].addWidget(card)
                hint = card.layout().heightForWidth(card_width)
                heights[column] += max(hint, card.minimumSizeHint().height()) + 14
                card.show()
            self.grid.addWidget(group)
            self._groups.append(group)
            group.show()
            pending.clear()

        for card in self.cards:
            if card.data.wide:
                flush()
                self.grid.addWidget(card)
                card.show()
            else:
                pending.append(card)
        flush()

    def _update_filename(self):
        self.file_label.setText(
            self.file_label.fontMetrics().elidedText(
                filename(self.path), Qt.TextElideMode.ElideMiddle, max(0, self.file_label.width())
            )
        )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_filename()

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._layout_cards()
            self._update_filename()
        return super().eventFilter(watched, event)

    def copy_summary(self):
        if self.result:
            text = "\n\n".join(card.summary(include_path=False) for card in self.card_data)
            if not self.final or self.result.issues:
                text = self.status.text() + "\n\n" + text
            QApplication.clipboard().setText(text)

    def show_details(self):
        if self.result:
            text = json.dumps(
                self.result.report(include_path=True, include_raw=True),
                ensure_ascii=False,
                indent=2,
            )
            MediaDetailsDialog(tr_text("读取详情"), text, self.window()).exec()

    def save_report(self):
        if not self.result or not self.final or self.export_running:
            return
        options = MediaExportDialog(self.window())
        if not options.exec():
            return
        destination, _ = QFileDialog.getSaveFileName(
            self, tr_text("导出媒体信息"), "media-info.json", "JSON (*.json)"
        )
        if destination:
            self.export_running = True
            self.export_snapshot = self.result
            self.export_action.setEnabled(False)
            self.export_requested.emit(self.result, destination, options.options())

    def export_finished(self, error):
        self.export_running = False
        self.export_action.setEnabled(self.result is not None and self.final)
        if self.result is self.export_snapshot:
            if error:
                self._set_status(issue_text(error))
            else:
                InfoBar.success(
                    title=tr_text("报告已导出"),
                    content="",
                    duration=2000,
                    position=InfoBarPosition.TOP_RIGHT,
                    parent=self,
                )
        self.export_snapshot = None

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()
        else:
            self._set_status(issue_text("not_file"))

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.open_file(urls[0].toLocalFile())
            event.acceptProposedAction()
