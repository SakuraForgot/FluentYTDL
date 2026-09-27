"""Screen-bounded windows and consistent scrolling for task configuration UIs."""

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPalette, QWheelEvent
from PySide6.QtWidgets import QAbstractScrollArea, QApplication, QStackedWidget, QWidget
from qfluentwidgets import SmoothScrollArea, isDarkTheme, qconfig


def task_surface_color() -> QColor:
    """The existing task-window background, shared by every exposed surface."""
    return QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)


class TaskWindowSurface:
    """Paint the entire backing surface before any translucent child controls."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self._update_surface_palette()
        qconfig.themeChanged.connect(self._update_surface_palette)

    def _update_surface_palette(self):
        # Match Qt's background role before the first native expose/resize,
        # as well as the pixels drawn by paintEvent afterwards.
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, task_surface_color())
        self.setPalette(palette)
        self.update()

    def paintEvent(self, event):
        with QPainter(self) as painter:
            painter.fillRect(self.rect(), task_surface_color())
        super().paintEvent(event)


def bounded_geometry(rect: QRect, available: QRect) -> QRect:
    """Clamp size before position; screen origins may be negative or nonzero."""
    width = max(1, min(rect.width(), available.width()))
    height = max(1, min(rect.height(), available.height()))
    x = max(available.left(), min(rect.x(), available.right() - width + 1))
    y = max(available.top(), min(rect.y(), available.bottom() - height + 1))
    return QRect(x, y, width, height)


class WindowGeometryGuard(QObject):
    """Refit on screen/work-area changes, without recentering ordinary drags."""

    def __init__(self, window, *, min_size: tuple[int, int] = (360, 240)):
        super().__init__(window)
        self.window = window
        # Desired usable minimum. fit() clamps it to the work area, so a screen
        # narrower/shorter than this is never handed an un-shrinkable minimum
        # (e.g. the main window's 1150 floor on a 960-logical-wide 1080p@200%).
        self._min_size = min_size
        self._handle_connected = False
        self.screen = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.fit)
        window.installEventFilter(self)
        self._connect_handle()
        self._screen_changed(window.screen())

    def _connect_handle(self):
        # windowHandle() is None until the native window exists (first show); the
        # eventFilter retries on Show, so a window built before it is shown still
        # refits when later dragged onto a differently-scaled monitor.
        if self._handle_connected:
            return
        handle = self.window.windowHandle()
        if handle is not None:
            handle.screenChanged.connect(self._screen_changed)
            self._handle_connected = True

    def _screen_changed(self, screen):
        if self.screen is not None:
            try:
                self.screen.availableGeometryChanged.disconnect(self.schedule)
                self.screen.logicalDotsPerInchChanged.disconnect(self.schedule)
            except (RuntimeError, TypeError):
                pass
        self.screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self.schedule)
            screen.logicalDotsPerInchChanged.connect(self.schedule)
        self.schedule()

    def schedule(self, *_):
        self.timer.start(0)

    def fit(self):
        window = self.window
        screen = window.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # setGeometry/geometry() speak the client rect, but the on-screen footprint
        # is frameGeometry() — the resize border and drop shadow live outside
        # geometry() yet still consume work-area pixels. Reserve that overhead so a
        # window clamped flush to a small screen keeps its *frame* inside the work
        # area (a bare client-rect clamp lets a ~2px border poke past every edge).
        # Frame margins are 0 until the native window exists, so before first show
        # this is a no-op; the Show-triggered refit tightens it once margins are real.
        geo = window.geometry()
        frame = window.frameGeometry()
        extra_w = max(0, frame.width() - geo.width())
        extra_h = max(0, frame.height() - geo.height())
        usable = QRect(
            available.left(),
            available.top(),
            max(1, available.width() - extra_w),
            max(1, available.height() - extra_h),
        )
        min_w, min_h = self._min_size
        window.setMinimumSize(min(min_w, usable.width()), min(min_h, usable.height()))
        if window.isMaximized() or window.isFullScreen() or window.isMinimized():
            return
        rect = bounded_geometry(geo, usable)
        if rect != geo:
            window.setGeometry(rect)

    def eventFilter(self, obj, event):
        if event.type() in (QEvent.Type.Show, QEvent.Type.WindowStateChange):
            self._connect_handle()
            self.schedule()
        elif event.type() == QEvent.Type.Close:
            self.timer.stop()
        return False


class _ScrollBoundaryFilter(QObject):
    """Forward edge input once; handle touchpad pixel deltas without quantizing."""

    def eventFilter(self, viewport, event):
        if event.type() != QEvent.Type.Wheel:
            return False
        area = self.parent()
        pixel = event.pixelDelta()
        delta = pixel if not pixel.isNull() else event.angleDelta()
        vertical = abs(delta.y()) >= abs(delta.x())
        amount = delta.y() if vertical else delta.x()
        if not amount:
            return False
        bar = area.verticalScrollBar() if vertical else area.horizontalScrollBar()
        at_edge = (amount > 0 and bar.value() == bar.minimum()) or (
            amount < 0 and bar.value() == bar.maximum()
        )
        if at_edge:
            parent = area.parentWidget()
            while parent is not None and not isinstance(parent, QAbstractScrollArea):
                parent = parent.parentWidget()
            if parent is not None:
                forwarded = QWheelEvent(
                    QPointF(parent.viewport().mapFromGlobal(event.globalPosition().toPoint())),
                    event.globalPosition(),
                    pixel,
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                    event.source(),
                )
                QApplication.sendEvent(parent.viewport(), forwarded)
                event.accept()
                return True
        if not pixel.isNull():
            delegate = getattr(area, "scrollDelagate", None) or getattr(area, "delegate", None)
            smooth_bar = delegate.vScrollBar if vertical else delegate.hScrollBar
            smooth_bar.ani.stop()
            bar.setValue(bar.value() - amount)
            event.accept()
            return True
        return False


def configure_scrolling(area):
    """Use one Fluent delegate, same easing, and edge forwarding for every list."""
    from PySide6.QtCore import QEasingCurve

    delegate = getattr(area, "scrollDelagate", None) or getattr(area, "delegate", None)
    delegate.useAni = True
    for bar in (delegate.vScrollBar, delegate.hScrollBar):
        bar.setScrollAnimation(200, QEasingCurve.Type.OutCubic)
    boundary = _ScrollBoundaryFilter(area)
    area.viewport().installEventFilter(boundary)
    area._scroll_boundary_filter = boundary


class TaskScrollArea(SmoothScrollArea):
    def __init__(self, parent=None, *, show_scroll_bars: bool = True):
        super().__init__(parent)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setWidgetResizable(True)
        self.setFrameShape(self.Shape.NoFrame)
        policy = (
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
            if show_scroll_bars
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.setHorizontalScrollBarPolicy(policy)
        self.setVerticalScrollBarPolicy(policy)
        configure_scrolling(self)
        qconfig.themeChanged.connect(self._update_surface)
        QApplication.instance().installEventFilter(self)

    def enableTransparentBackground(self):
        # Fluent's helper applies QWidget{background:transparent} to the entire
        # content subtree, including input controls and nested table viewports.
        # Only the scroll container needs transparency over our backing paint.
        self.setStyleSheet("TaskScrollArea{border:none; background:transparent}")
        if self.widget() is not None:
            self.widget().setAutoFillBackground(False)

    def eventFilter(self, obj, event):
        if (
            isinstance(obj, QWidget)
            and event.type() == QEvent.Type.FocusIn
            and event.reason()
            in (
                Qt.FocusReason.TabFocusReason,
                Qt.FocusReason.BacktabFocusReason,
                Qt.FocusReason.ShortcutFocusReason,
            )
        ):
            self._reveal_focus(obj)
        return super().eventFilter(obj, event)

    def paintEvent(self, event):
        # QAbstractScrollArea routes viewport paint events to the scroll area;
        # overriding the viewport widget's paintEvent would leave it unpainted.
        with QPainter(self.viewport()) as painter:
            painter.fillRect(event.rect(), task_surface_color())
        super().paintEvent(event)

    def _reveal_focus(self, current):
        content = self.widget()
        if (
            current is None
            or content is None
            or not self.isVisible()
            or not content.isAncestorOf(current)
        ):
            return
        rect = QRect(current.mapTo(self.viewport(), QPoint()), current.size())
        visible = self.viewport().rect()
        # Do not recenter a large table which is already partially visible.
        if visible.contains(rect) or (
            rect.height() > visible.height() and visible.intersects(rect)
        ):
            return
        self.ensureWidgetVisible(current, 24, 24)

    def _update_surface(self):
        self.viewport().update()
        if self.widget() is not None:
            self.widget().update()


class CurrentPageStack(QStackedWidget):
    """Hidden format pages must not impose their minimum height on simple mode."""

    def minimumSizeHint(self):
        widget = self.currentWidget()
        return widget.minimumSizeHint() if widget is not None else QSize(0, 0)

    def sizeHint(self):
        widget = self.currentWidget()
        return widget.sizeHint() if widget is not None else QSize(0, 0)
