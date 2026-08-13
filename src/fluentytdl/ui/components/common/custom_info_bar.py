from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import QWidget
from qfluentwidgets import InfoBar as _FluentInfoBar
from qfluentwidgets import InfoBarIcon, InfoBarPosition, drawIcon, themeColor


class _CompactInfoIcon(QWidget):
    """Severity icon that stays centred at any box size.

    Upstream's InfoIconWidget paints into a hardcoded ``QRectF(10, 10, 15, 15)``,
    which is only centred inside its fixed 36x36 box. Shrinking that box to make
    the bar less tall would shove the glyph towards the bottom-right corner.
    """

    GLYPH = 15

    def __init__(self, icon, size: int, parent=None):
        super().__init__(parent=parent)
        self.setFixedSize(size, size)
        self.icon = icon

    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHints(
            QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform
        )
        offset = (self.width() - self.GLYPH) / 2
        rect = QRectF(offset, offset, self.GLYPH, self.GLYPH)

        if self.icon != InfoBarIcon.INFORMATION:
            drawIcon(self.icon, painter, rect)
        else:
            drawIcon(self.icon, painter, rect, indexes=[0], fill=themeColor().name())


class InfoBar(_FluentInfoBar):
    """InfoBar that hugs its content instead of stretching with its parent.

    Two upstream behaviours make the bar look oversized:

    * ``_adjustText`` derives the line-break budget from the *parent* widget's
      width, so a toast on a wide page runs to 800px+ on one line instead of
      wrapping. The budget is also counted in characters, which does not track
      pixels -- a latin char costs one unit but renders about twice as wide as
      one unit of CJK text.
    * The icon and close button are fixed 36x36 boxes pinned to ``AlignTop``,
      which forces a 48px tall bar and leaves an empty "chin" under one line.

    This subclass budgets in pixels, wraps with Qt's own word wrap, and centres
    the row vertically.
    """

    # Upper bound on the bar's width; shorter messages stay narrower than this.
    MAX_WIDTH = 560

    # Icon / close button box size. Upstream uses 36, which on its own forces a
    # 48px tall bar no matter how little text there is.
    ICON_SIZE = 26

    # Padding above and below the row.
    V_PADDING = 6

    # Narrowest the content label may be squeezed to before it stops yielding
    # width to a long title.
    MIN_CONTENT_WIDTH = 140

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setMaximumWidth(self.MAX_WIDTH)
        self._compactLayout()
        self._adjustText()

    def _compactLayout(self):
        centre = Qt.AlignmentFlag.AlignVCenter

        # Swap in an icon that stays centred at the smaller box size.
        stale = self.iconWidget
        self.iconWidget = _CompactInfoIcon(self.icon, self.ICON_SIZE, self)
        self.hBoxLayout.replaceWidget(stale, self.iconWidget)
        stale.deleteLater()

        self.closeButton.setFixedSize(self.ICON_SIZE, self.ICON_SIZE)

        self.hBoxLayout.setContentsMargins(10, self.V_PADDING, 6, self.V_PADDING)
        self.textLayout.setContentsMargins(2, 0, 0, 0)

        # Let Qt break the lines. Without this a long message reports its full
        # unwrapped width as its minimum size, and because the layout runs under
        # SetMinimumSize that minimum propagates onto the bar and overrides
        # setMaximumWidth entirely.
        self.titleLabel.setWordWrap(True)
        self.contentLabel.setWordWrap(True)

        # Upstream aligns everything to the top, which is what produces the chin.
        self.textLayout.setAlignment(centre)
        self.hBoxLayout.setAlignment(self.iconWidget, centre)
        self.hBoxLayout.setAlignment(self.closeButton, centre)
        self.textLayout.setAlignment(self.titleLabel, centre)
        self.textLayout.setAlignment(self.contentLabel, centre)

        # The labels are added with stretch=1 upstream, so any spare width gets
        # absorbed by them rather than trimmed off the bar.
        self.textLayout.setStretchFactor(self.titleLabel, 0)
        self.textLayout.setStretchFactor(self.contentLabel, 0)

    def _textWidth(self) -> int:
        """Width the two labels currently contribute to the bar."""
        if self.orient == Qt.Orientation.Horizontal:
            return self.titleLabel.width() + self.contentLabel.width()
        return max(self.titleLabel.width(), self.contentLabel.width())

    def _adjustText(self):
        title = self.title or ""
        content = self.content or ""

        self.titleLabel.setText(title)
        self.contentLabel.setText(content)

        natural_title = self._naturalWidth(self.titleLabel, title)
        natural_content = self._naturalWidth(self.contentLabel, content)

        # Pass 1: lay out at natural width so the chrome (icon, close button,
        # margins, layout spacing) can be measured rather than hand-counted.
        self._sizeLabel(self.titleLabel, natural_title)
        self._sizeLabel(self.contentLabel, natural_content)
        self.adjustSize()
        chrome = max(self.width() - self._textWidth(), 0)

        budget = max(self.MAX_WIDTH - chrome, self.MIN_CONTENT_WIDTH)

        # Pass 2: size each label to min(natural width, allowance). Leaving this
        # to the layout does not work: a word-wrapped QLabel reports a tiny
        # minimum width (CJK breaks between any two characters), so a short
        # message would wrap to several lines instead of staying on one.
        if self.orient == Qt.Orientation.Horizontal and title:
            # Title and content share one line; the title never takes more than
            # half the budget, and the content gets whatever is left.
            title_width = min(natural_title, budget // 2)
            content_width = min(natural_content, budget - title_width)
        else:
            title_width = min(natural_title, budget)
            content_width = min(natural_content, budget)

        self._sizeLabel(self.titleLabel, title_width)
        self._sizeLabel(self.contentLabel, content_width)

        self.adjustSize()

    @staticmethod
    def _naturalWidth(label, text: str) -> int:
        """Width the text would take on a single unwrapped line."""
        return label.fontMetrics().horizontalAdvance(text)

    @staticmethod
    def _sizeLabel(label, width: int):
        width = max(width, 0)
        label.setFixedWidth(width)
        # sizeHint() on a word-wrapped label is a heuristic that ignores the
        # width we just assigned, so ask for the real wrapped height.
        label.setMinimumHeight(label.heightForWidth(width) if width else 0)

    @classmethod
    def new(
        cls,
        icon,
        title,
        content,
        orient=Qt.Orientation.Horizontal,
        isClosable=True,
        duration=1000,
        position=InfoBarPosition.TOP_RIGHT,
        parent=None,
    ):
        # Upstream hardcodes the base class here, so success()/error()/warning()
        # would bypass this subclass entirely without this override.
        w = cls(icon, title, content, orient, isClosable, duration, position, parent)
        w.show()
        return w
