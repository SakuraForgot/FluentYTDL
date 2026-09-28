from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget
from qfluentwidgets import BodyLabel


def _rgba(c: QColor) -> str:
    return f"rgba({c.red()},{c.green()},{c.blue()},{c.alpha()})"


def _with_alpha(c: QColor, alpha: int) -> QColor:
    out = QColor(c)
    out.setAlpha(max(0, min(255, int(alpha))))
    return out


def _hsl_adjust(
    c: QColor, *, hue_shift: float = 0.0, sat_mul: float = 1.0, light_mul: float = 1.0
) -> QColor:
    h, s, light, a = cast(tuple[float, float, float, float], c.getHslF())
    if h < 0:
        h = 0.0
    h = (h + hue_shift) % 1.0
    s = max(0.0, min(1.0, s * sat_mul))
    light = max(0.0, min(1.0, light * light_mul))
    out = QColor()
    out.setHslF(h, s, light, a)
    return out


def _macaron_bg(color_style: str) -> QColor:
    """Macaron background colors (light theme)."""

    # User-specified macaron hex backgrounds.
    bg_hex = {
        "gold": "#FFF4CE",
        "blue": "#CFE2FF",
        "purple": "#E0CFFC",
        "green": "#D1E7DD",
        "orange": "#FFE5D0",
        "red": "#F8D7DA",
        "gray": "#F8F9FA",
    }.get(color_style, "#F8F9FA")

    from qfluentwidgets import isDarkTheme

    if isDarkTheme():
        # Darken the macaron colors for dark mode
        c = QColor(bg_hex)
        return _hsl_adjust(c, sat_mul=0.6, light_mul=0.25)

    return QColor(bg_hex)


class QualityBadge(QLabel):
    """Soft Fluent-style badge.

    Uses macaron background hex colors for visual consistency.
    """

    def __init__(
        self,
        text: str,
        color_style: str = "gray",
        parent: QWidget | None = None,
        *,
        compact: bool = False,
    ):
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFixedHeight(18)
        # compact 只给「装配预览」的芯片用：更窄的最小宽 + 更小的左右内边距，让预览徽标比
        # 流表里的药丸更紧凑（流表不传 compact，尺寸不受影响）。
        self.setMinimumWidth(22 if compact else 30)

        font = self.font()
        base_ps = font.pointSize()
        if not isinstance(base_ps, int) or base_ps <= 0:
            base_ps = 9
        font.setPointSize(max(9, base_ps - 1))
        font.setWeight(QFont.Weight.DemiBold)
        self.setFont(font)

        bg = _macaron_bg(color_style)
        from qfluentwidgets import isDarkTheme

        is_dark = isDarkTheme()
        # Derive border/text from the background itself to keep the palette cohesive.
        border = _with_alpha(_hsl_adjust(bg, sat_mul=1.05, light_mul=1.8 if is_dark else 0.82), 200)
        fg = _hsl_adjust(bg, sat_mul=1.10, light_mul=3.8 if is_dark else 0.28)
        fg.setAlpha(255)

        self.setStyleSheet(
            "QLabel {"
            f"background-color: {bg.name()};"
            f"color: {_rgba(fg)};"
            f"border: 1px solid {_rgba(border)};"
            "border-radius: 4px;"
            f"padding: 0px {4 if compact else 6}px;"
            "}"
        )


class _ElidingLabel(BodyLabel):
    """A BodyLabel that elides its text with `…` to the width the layout grants
    it, instead of overflowing the cell.

    The stream table's detail column is a Stretch column: a centered, non-eliding
    label there floats in the empty middle (drifts as the window resizes) and, when
    the column is squeezed (narrow window / the two-column left panel), spills past
    the visible edge. This label reserves the *full* text width as its preferred
    size (so it never elides when there is room) but has a zero minimum, so a tight
    column shrinks it and it elides cleanly rather than pushing text out of the UI.
    """

    def __init__(self, text: str = "", parent: QWidget | None = None):
        # qfluentwidgets' FluentLabelBase has an overloaded (singledispatchmethod)
        # constructor whose str-branch re-enters ``self.__init__(parent)``. From a
        # subclass that recurses back into THIS __init__ with the parent widget
        # handed in as ``text`` (TypeError: 1..2 positional args but 3 given). So we
        # must construct with parent only and assign the text via our own setText().
        super().__init__(parent)
        self._full_text = text
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        if text:
            self.setText(text)

    def setText(self, text: str) -> None:  # type: ignore[override]
        self._full_text = text
        self.updateGeometry()
        self._apply_elide()

    def sizeHint(self) -> QSize:
        # Preferred width == full (un-elided) text, so the layout hands us that
        # width whenever the column can afford it.
        return QSize(
            self.fontMetrics().horizontalAdvance(self._full_text), super().sizeHint().height()
        )

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_elide()

    def _apply_elide(self) -> None:
        fm = self.fontMetrics()
        elided = fm.elidedText(self._full_text, Qt.TextElideMode.ElideRight, max(0, self.width()))
        super().setText(elided)


class QualityCellWidget(QWidget):
    """Zero-margin, vertically centered badge+text container."""

    def __init__(
        self,
        badges_data: Iterable[tuple[str, str]],
        text: str,
        parent: QWidget | None = None,
        *,
        bold_text: bool = False,
        elide: bool = False,
        alignment: Qt.AlignmentFlag = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
    ):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        # Does the caller want the content block horizontally centered?
        is_center = bool(alignment & Qt.AlignmentFlag.AlignHCenter)

        # A horizontal alignment flag on a QHBoxLayout makes it shrink to the content's
        # size hint and *ignore* stretch — fine for a fixed narrow column, but an eliding
        # label must stay free to fill-and-shrink. So when eliding we keep the layout
        # vertical-only and, if centering is asked for, center the block with a stretch on
        # each side. Those side stretches collapse *before* the label when the column is
        # squeezed, so the label elides instead of spilling past the edge.
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter if elide else alignment)

        if elide and is_center:
            layout.addStretch(1)

        for badge_text, color in badges_data:
            badge = QualityBadge(badge_text, color, self)
            layout.addWidget(badge)

        if text:
            label = _ElidingLabel(text, self) if elide else BodyLabel(text, self)
            if elide:
                label.setAlignment(
                    (Qt.AlignmentFlag.AlignHCenter if is_center else Qt.AlignmentFlag.AlignLeft)
                    | Qt.AlignmentFlag.AlignVCenter
                )
            else:
                label.setAlignment(Qt.AlignmentFlag.AlignVCenter)
            font = label.font()
            base_ps = font.pointSize()
            if not isinstance(base_ps, int) or base_ps <= 0:
                base_ps = 10
            font.setPointSize(max(10, base_ps))
            if bold_text:
                font.setWeight(QFont.Weight.DemiBold)
            label.setFont(font)
            # Left-elide: label fills the cell (stretch 1) and shrinks to elide.
            # Center-elide: label keeps its content width (stretch 0) so the side
            # stretches can center the block; it still shrinks to elide (min width 0).
            layout.addWidget(label, 0 if (elide and is_center) else (1 if elide else 0))

        if elide and is_center:
            layout.addStretch(1)
        elif alignment & Qt.AlignmentFlag.AlignLeft and not elide:
            layout.addStretch(1)
