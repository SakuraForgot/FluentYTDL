"""Single-video section download controls with a lightweight dual-handle timeline."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, Qt, Signal
from PySide6.QtGui import QMouseEvent, QPainter, QPalette
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import CaptionLabel, ComboBox, LineEdit, SwitchButton

from ....core.section_download import SectionCutMode, TimeRange, parse_time_range

_QWIDGETSIZE_MAX = 16777215


def _format_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:d}:{secs:02d}"


class _RangeTimeline(QWidget):
    """Theme-aware, no-dependency dual-handle timeline measured in seconds."""

    rangeChanged = Signal(float, float)

    def __init__(self, duration: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._duration = max(1.0, duration)
        self._start = 0.0
        self._end = self._duration
        self._dragging = ""
        self.setMinimumHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_range(self, start: float, end: float, *, emit: bool = False) -> None:
        start = max(0.0, min(float(start), self._duration))
        end = max(start + 0.01, min(float(end), self._duration))
        if (start, end) == (self._start, self._end):
            return
        self._start, self._end = start, end
        self.update()
        if emit:
            self.rangeChanged.emit(start, end)

    def _track_rect(self) -> QRect:
        return QRect(12, self.height() // 2 - 3, max(1, self.width() - 24), 6)

    def _x_for(self, value: float) -> int:
        rect = self._track_rect()
        return rect.left() + round((value / self._duration) * rect.width())

    def _value_for(self, x: int) -> float:
        rect = self._track_rect()
        ratio = (x - rect.left()) / max(1, rect.width())
        return max(0.0, min(self._duration, ratio * self._duration))

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        palette = self.palette()
        rect = self._track_rect()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(palette.color(QPalette.ColorRole.Mid))
        painter.drawRoundedRect(rect, 3, 3)
        left, right = self._x_for(self._start), self._x_for(self._end)
        selected = QRect(left, rect.top(), max(1, right - left), rect.height())
        painter.setBrush(palette.color(QPalette.ColorRole.Highlight))
        painter.drawRoundedRect(selected, 3, 3)
        painter.setBrush(palette.color(QPalette.ColorRole.Base))
        painter.setPen(palette.color(QPalette.ColorRole.Highlight))
        for x in (left, right):
            painter.drawEllipse(QPoint(x, rect.center().y()), 7, 7)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        x = event.position().x()
        self._dragging = (
            "start"
            if abs(x - self._x_for(self._start)) <= abs(x - self._x_for(self._end))
            else "end"
        )
        self._move_handle(x)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._dragging:
            self._move_handle(event.position().x())

    def mouseReleaseEvent(self, _event: QMouseEvent) -> None:
        self._dragging = ""

    def _move_handle(self, x: float) -> None:
        value = self._value_for(int(x))
        if self._dragging == "start":
            self.set_range(min(value, self._end - 0.01), self._end, emit=True)
        else:
            self.set_range(self._start, max(value, self._start + 0.01), emit=True)


class SectionRangeSelector(QWidget):
    """Switchable clip selector used only for finite normal YouTube videos."""

    enabledChanged = Signal(bool)
    selectionChanged = Signal()

    # 选项区淡入/淡出的时长。展开时高度一次性放开（滚动区只重排一次），内容随后
    # 用透明度补间淡入——opacity 动画只触发重绘、不触发布局，避免逐帧重排掉帧。
    # 外层窗口用这个时长（+ 余量）安排「展开后滚动到可见」，见
    # download_config_window._on_section_enabled_changed。
    OPTIONS_ANIM_MS = 160
    OPTIONS_ANIM_EASING = QEasingCurve.Type.OutCubic

    def __init__(
        self, duration: float, parent: QWidget | None = None, *, show_header: bool = True
    ) -> None:
        super().__init__(parent)
        self._duration = max(0.0, float(duration))
        self._updating = False
        self._show_header = show_header
        self._options_anim: QPropertyAnimation | None = None
        self._init_ui()

    def _init_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)
        # enable_switch 始终创建并接线；表头（「视频裁切」标签 + 开关）只在 show_header 时铺。
        # 宿主把开关搬进别处的「下载选项」行时传 show_header=False，避免重复的裁切标签，
        # 展开的选项面板仍留在本控件下方。
        self.enable_switch = SwitchButton(self)
        self.enable_switch.checkedChanged.connect(self._on_enabled_changed)
        if self._show_header:
            header = QHBoxLayout()
            header.addWidget(CaptionLabel(self.tr("视频裁切"), self))
            header.addWidget(self.enable_switch)
            header.addStretch(1)
            root.addLayout(header)

        self.options = QWidget(self)
        options = QVBoxLayout(self.options)
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(8)
        self.timeline = _RangeTimeline(self._duration, self.options)
        self.timeline.rangeChanged.connect(self._on_timeline_changed)
        options.addWidget(self.timeline)

        labels = QHBoxLayout()
        self.start_label = CaptionLabel("0:00", self.options)
        self.end_label = CaptionLabel(_format_time(self._duration), self.options)
        labels.addWidget(self.start_label)
        labels.addStretch(1)
        labels.addWidget(self.end_label)
        options.addLayout(labels)

        times = QGridLayout()
        times.addWidget(CaptionLabel(self.tr("开始"), self.options), 0, 0)
        self.start_edit = LineEdit(self.options)
        self.start_edit.setText("0:00")
        self.start_edit.editingFinished.connect(self._on_text_changed)
        times.addWidget(self.start_edit, 0, 1)
        times.addWidget(CaptionLabel(self.tr("结束"), self.options), 0, 2)
        self.end_edit = LineEdit(self.options)
        self.end_edit.setText(_format_time(self._duration))
        self.end_edit.editingFinished.connect(self._on_text_changed)
        times.addWidget(self.end_edit, 0, 3)
        times.addWidget(CaptionLabel(self.tr("模式"), self.options), 1, 0)
        self.mode_combo = ComboBox(self.options)
        self.mode_combo.addItem(
            self.tr("粗裁剪（快速，切点可能有偏差）"), userData=SectionCutMode.COARSE.value
        )
        self.mode_combo.addItem(
            self.tr("细裁剪（精确，需重编码）"), userData=SectionCutMode.PRECISE.value
        )
        self.mode_combo.currentIndexChanged.connect(self.selectionChanged)
        times.addWidget(self.mode_combo, 1, 1, 1, 3)
        options.addLayout(times)

        self.status_label = CaptionLabel("", self.options)
        self.status_label.setWordWrap(True)
        options.addWidget(self.status_label)
        root.addWidget(self.options)
        # 内容透明度由 graphics effect 驱动淡入/淡出；opacity 只影响重绘，不参与布局，
        # 所以补间期间滚动区不会逐帧重排。
        self._options_opacity = QGraphicsOpacityEffect(self.options)
        self._options_opacity.setOpacity(0.0)
        self.options.setGraphicsEffect(self._options_opacity)
        # 收起态用 maximumHeight=0（而不是仅 hide()）：布局用 qSmartMinSize 把子控件的
        # 最小高度限制在 maximumHeight 内，收起时外层窗口的最小高度才不会把未展开的
        # 选项区也算进去。展开时一次性放开上限（见 _animate_options）。
        self.options.setMaximumHeight(0)
        self.options.hide()

    def _animate_options(self, expand: bool) -> None:
        if self._options_anim is not None:
            self._options_anim.stop()
            self._options_anim = None

        if expand:
            # 一次性放开高度上限：滚动区只重排一次，随后内容以透明度淡入。
            self.options.setMaximumHeight(_QWIDGETSIZE_MAX)
            self.options.show()
        start = self._options_opacity.opacity()
        end = 1.0 if expand else 0.0

        if start == end:
            self._finish_options_anim(expand)
            return

        anim = QPropertyAnimation(self._options_opacity, b"opacity", self)
        anim.setDuration(self.OPTIONS_ANIM_MS)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(self.OPTIONS_ANIM_EASING)
        anim.finished.connect(lambda: self._finish_options_anim(expand))
        self._options_anim = anim
        anim.start()

    def _finish_options_anim(self, expanded: bool) -> None:
        if not expanded:
            # 淡出结束后再释放布局空间，避免收起时的高度骤缩打断淡出。
            self.options.hide()
            self.options.setMaximumHeight(0)
        self._options_anim = None

    def _on_enabled_changed(self, enabled: bool) -> None:
        # 先通知外层：窗口的几何动画要和下面的高度动画同一帧起步
        self.enabledChanged.emit(enabled)
        self._animate_options(enabled)
        self.selectionChanged.emit()

    def _on_timeline_changed(self, start: float, end: float) -> None:
        self._updating = True
        self.start_edit.setText(_format_time(start))
        self.end_edit.setText(_format_time(end))
        self._updating = False
        self._update_status(start, end)
        self.selectionChanged.emit()

    def _on_text_changed(self) -> None:
        if self._updating:
            return
        try:
            time_range = parse_time_range(self.start_edit.text(), self.end_edit.text())
            if time_range.end_seconds is None or time_range.end_seconds > self._duration:
                raise ValueError(self.tr("时间范围必须位于视频时长内"))
        except ValueError as exc:
            self.status_label.setText(str(exc))
            return
        self.timeline.set_range(time_range.start_seconds, time_range.end_seconds, emit=False)
        self._update_status(time_range.start_seconds, time_range.end_seconds)
        self.selectionChanged.emit()

    def _update_status(self, start: float, end: float) -> None:
        self.start_label.setText(_format_time(start))
        self.end_label.setText(_format_time(end))
        self.status_label.setText(self.tr("将下载 {0} 的片段").format(_format_time(end - start)))

    def is_enabled(self) -> bool:
        return self.enable_switch.isChecked()

    def is_valid(self) -> bool:
        return not self.is_enabled() or self.get_time_range() is not None

    def get_time_range(self) -> TimeRange | None:
        if not self.is_enabled():
            return None
        try:
            value = parse_time_range(self.start_edit.text(), self.end_edit.text())
            if value.end_seconds is None or value.end_seconds > self._duration:
                return None
            return value
        except ValueError:
            return None

    def get_cut_mode(self) -> SectionCutMode:
        # QFluentWidgets' ComboBoxBase.currentData() may return None even when
        # userData was supplied to addItem(). Resolve via the selected index.
        value = self.mode_combo.itemData(self.mode_combo.currentIndex())
        return SectionCutMode(value or SectionCutMode.COARSE.value)
