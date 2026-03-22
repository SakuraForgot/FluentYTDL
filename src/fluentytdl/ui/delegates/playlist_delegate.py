from typing import Any

from PySide6.QtCore import QPoint, QRect, QSize, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QListView, QStyle, QStyledItemDelegate, QStyleOptionViewItem
from qfluentwidgets import ThemeColor, isDarkTheme, themeColor

from ...models.video_task import VideoTask
from ..models.playlist_model import PlaylistModelRoles

# ── 修复D: 模块级缓存 Fluent 色彩字典，避免每帧重复分配 ────────────────
_cached_fluent_dark: dict[str, QColor] | None = None
_cached_fluent_light: dict[str, QColor] | None = None


def _fluent_colors(is_dark: bool) -> dict[str, QColor]:
    """Return cached Fluent Design System colors for the current theme."""
    global _cached_fluent_dark, _cached_fluent_light
    if is_dark:
        if _cached_fluent_dark is None:
            _cached_fluent_dark = {
                "text_primary": QColor(255, 255, 255),
                "text_secondary": QColor(153, 153, 153),
                "card_default_bg": QColor(255, 255, 255, 7),
                "card_hover_bg": QColor(255, 255, 255, 12),
                "border_default": QColor(255, 255, 255, 14),
                "border_hover": QColor(255, 255, 255, 20),
                "checkbox_border": QColor(255, 255, 255, 140),
                "thumb_placeholder": QColor(255, 255, 255, 15),
                "thumb_border": QColor(255, 255, 255, 30),
                "btn_subtle_bg": QColor(255, 255, 255, 12),
                "btn_border": QColor(255, 255, 255, 15),
                "error_bg": QColor(68, 39, 38),
                "error_fg": QColor(255, 153, 164),
                "muted_fg": QColor(130, 130, 130),
            }
        return _cached_fluent_dark
    if _cached_fluent_light is None:
        _cached_fluent_light = {
            "text_primary": QColor(27, 27, 27),
            "text_secondary": QColor(96, 96, 96),
            "card_default_bg": QColor(255, 255, 255, 170),
            "card_hover_bg": QColor(0, 0, 0, 8),
            "border_default": QColor(0, 0, 0, 10),
            "border_hover": QColor(0, 0, 0, 15),
            "checkbox_border": QColor(0, 0, 0, 110),
            "thumb_placeholder": QColor(0, 0, 0, 15),
            "thumb_border": QColor(0, 0, 0, 30),
            "btn_subtle_bg": QColor(0, 0, 0, 8),
            "btn_border": QColor(0, 0, 0, 20),
            "error_bg": QColor(253, 231, 233),
            "error_fg": QColor(196, 43, 28),
            "muted_fg": QColor(130, 130, 130),
        }
    return _cached_fluent_light


class PlaylistItemDelegate(QStyledItemDelegate):
    """
    负责长列表的高性能渲染：在 QListView 的格子里完全手动绘制复选框、缩略图、文字。
    这避免了 QScrollArea 为每一行分配好几个完整 QWidget 并层叠导致内存占用高、引发假死的原罪。
    """

    # 定义布局区域常量
    MARGIN = 12
    SPACING = 16
    THUMB_WIDTH = 150
    THUMB_HEIGHT = 84
    CHECKBOX_SIZE = 20

    # 暴露一个自定义事件让 View 层能捕捉点击
    # (row, action_type) -> action_type = 'checkbox' | 'format_selector' | 'container'

    def __init__(self, parent: QListView):
        super().__init__(parent)
        self._view = parent

        # 原始图片缓存 (url -> QPixmap)
        self._pixmap_cache: dict[str, QPixmap] = {}
        # 修复A: 预缩放缩略图缓存 (f"{url}_{w}x{h}" -> QPixmap)，避免每帧 SmoothTransformation
        self._scaled_cache: dict[str, QPixmap] = {}

        # 修复D: accent light 缓存，避免每帧 HSL 计算
        self._last_accent_rgb: int = -1
        self._accent_light_cache: QColor | None = None

    def _get_accent_light(self, accent: QColor) -> QColor:
        """Return cached ThemeColor.LIGHT_3 derived color for the given accent."""
        rgb = accent.rgb()
        if rgb != self._last_accent_rgb:
            self._last_accent_rgb = rgb
            try:
                # ThemeColor.color() derives from the global theme accent (no args)
                accent_light = ThemeColor.LIGHT_3.color()
            except Exception:
                accent_light = None
            if accent_light is None or not accent_light.isValid():
                accent_light = QColor(accent)
                accent_light = accent_light.lighter(160)
            self._accent_light_cache = accent_light
        if self._accent_light_cache is None or not self._accent_light_cache.isValid():
            fallback = QColor(accent)
            if not fallback.isValid():
                fallback = QColor(0, 120, 212)
            self._accent_light_cache = fallback.lighter(160)
        return self._accent_light_cache

    def set_pixmap(self, url: str, pixmap: QPixmap) -> None:
        """从外部的异步 Loader 将获取好的图片注册进来后让 View 重绘"""
        if not pixmap or pixmap.isNull():
            return
        existing = self._pixmap_cache.get(url)
        if existing is not None and existing.cacheKey() == pixmap.cacheKey():
            # 相同图片对象，无需清除 scaled 缓存，避免引发额外重绘
            return
        self._pixmap_cache[url] = pixmap
        # 清除该 url 的旧 scaled 缓存条目（新图已到，需要重新缩放一次）
        to_remove = [k for k in self._scaled_cache if k.startswith(url + "_")]
        for k in to_remove:
            del self._scaled_cache[k]

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        """
        必须提供 SizeHint 让 QListView 早于实际数据到来前就能分配视口高度。
        这正是解决 500 项同时去拉取图片的救火员。
        """
        height = self.MARGIN * 2 + self.THUMB_HEIGHT
        return QSize(200, height)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        # 1. 提取任务模型
        task: VideoTask = index.data(PlaylistModelRoles.TaskObjectRole)  # type: ignore
        if not task:
            return

        rect = option.rect  # type: ignore
        painter.save()

        # ── 清底：用窗口背景色填满整行矩形（含圆角卡片外的角落区域）──────────────
        # viewport WA_OpaquePaintEvent=True 要求 delegate 负责覆盖行矩形内所有像素；
        # 填完底色后再在上面叠加渲染圆角卡片、文字、缩略图，既消除脏像素残留，
        # 又不会像 super().paint() 那样画出不透明的 Qt 默认面板而遮挡自定义内容。
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.fillRect(rect, option.palette.window())
        # ────────────────────────────────────────────────────────────────────────

        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        is_dark = isDarkTheme()
        colors = _fluent_colors(is_dark)
        accent = themeColor()

        # 2. Fluent card background – always drawn for a card-like feel
        is_selected = bool(task.selected)
        is_hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)  # type: ignore

        if is_selected:
            accent_light = self._get_accent_light(accent)
            card_bg = QColor(
                accent_light.red(), accent_light.green(), accent_light.blue(), 25 if is_dark else 20
            )
            border_color = QColor(
                accent.red(), accent.green(), accent.blue(), 100 if is_dark else 80
            )
        elif is_hovered:
            card_bg = colors["card_hover_bg"]
            border_color = colors["border_hover"]
        else:
            card_bg = colors["card_default_bg"]
            border_color = colors["border_default"]

        painter.setBrush(QBrush(card_bg))
        painter.setPen(QPen(border_color, 1))
        painter.drawRoundedRect(rect.adjusted(3, 2, -3, -2), 6, 6)

        # 3. 布局计算
        current_x = rect.left() + self.MARGIN
        center_y = rect.top() + (rect.height() // 2)

        # 画左侧 CheckBox
        checkbox_rect = QRect(
            current_x, center_y - self.CHECKBOX_SIZE // 2, self.CHECKBOX_SIZE, self.CHECKBOX_SIZE
        )
        self._draw_checkbox(painter, checkbox_rect, task.selected, accent, colors)

        current_x += self.CHECKBOX_SIZE + self.SPACING

        # 画缩略图
        thumb_rect = QRect(
            current_x, center_y - self.THUMB_HEIGHT // 2, self.THUMB_WIDTH, self.THUMB_HEIGHT
        )
        self._draw_thumbnail(painter, thumb_rect, task.thumbnail_url, colors)

        current_x += self.THUMB_WIDTH + self.SPACING

        # 画右侧格式按钮/Loading状态保留位宽度定为 140 左右，放在最右边
        right_margin = rect.right() - self.MARGIN
        action_width = 140
        action_rect = QRect(right_margin - action_width, center_y - 16, action_width, 32)

        # 中间剩下区域就是标题文本
        text_rect = QRect(
            current_x,
            rect.top() + self.MARGIN,
            action_rect.left() - current_x - self.SPACING,
            rect.height() - 2 * self.MARGIN,
        )

        self._draw_text_info(painter, text_rect, task, colors)
        self._draw_action_btn(painter, action_rect, task, is_dark, accent, colors)

        painter.restore()

    def _draw_checkbox(
        self,
        painter: QPainter,
        rect: QRect,
        checked: bool,
        accent: QColor,
        colors: dict[str, QColor],
    ) -> None:
        """绘制仿 Fluent CheckBox"""
        painter.save()

        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(accent))
        else:
            painter.setPen(QPen(colors["checkbox_border"], 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)

        painter.drawRoundedRect(rect, 4, 4)

        if checked:
            painter.setPen(
                QPen(
                    Qt.GlobalColor.white,
                    2,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                    Qt.PenJoinStyle.RoundJoin,
                )
            )
            path = QPainterPath()
            path.moveTo(rect.left() + 4, rect.top() + 10)
            path.lineTo(rect.left() + 8, rect.top() + 14)
            path.lineTo(rect.left() + 15, rect.top() + 5)
            painter.drawPath(path)

        painter.restore()

    def _draw_thumbnail(
        self,
        painter: QPainter,
        rect: QRect,
        url: str,
        colors: dict[str, QColor],
    ) -> None:
        """尝试从缓存提取图片，若没有则画占位格"""
        painter.save()

        # 裁剪圆角路径
        path = QPainterPath()
        path.addRoundedRect(rect, 6, 6)
        painter.setClipPath(path)

        pixmap = self._pixmap_cache.get(url)
        if pixmap and not pixmap.isNull():
            # 修复A: 使用预缩放缓存，仅首次缩放，后续直接 drawPixmap
            cache_key = f"{url}_{rect.width()}x{rect.height()}"
            scaled = self._scaled_cache.get(cache_key)
            if scaled is None:
                scaled = pixmap.scaled(
                    rect.size(),
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
                self._scaled_cache[cache_key] = scaled

            # 计算居中所需的偏移
            x_offset = (scaled.width() - rect.width()) // 2
            y_offset = (scaled.height() - rect.height()) // 2
            painter.drawPixmap(
                rect.topLeft(), scaled, QRect(x_offset, y_offset, rect.width(), rect.height())
            )
        else:
            painter.fillPath(path, QBrush(colors["thumb_placeholder"]))

        # Draw border
        painter.setClipping(False)
        painter.setPen(QPen(colors["thumb_border"], 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 6, 6)

        painter.restore()

    def _draw_text_info(
        self,
        painter: QPainter,
        rect: QRect,
        task: VideoTask,
        colors: dict[str, QColor],
    ) -> None:
        painter.save()

        # 绘制主标题
        title_font = painter.font()
        title_font.setPixelSize(14)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(colors["text_primary"])

        fm = QFontMetrics(title_font)
        elided_title = fm.elidedText(task.title, Qt.TextElideMode.ElideRight, rect.width())
        title_rect = QRect(rect.left(), rect.top() + 4, rect.width(), fm.height())
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided_title,
        )

        # 绘制底部 Metadata 副标题
        meta_font = painter.font()
        meta_font.setPixelSize(12)
        meta_font.setBold(False)
        painter.setFont(meta_font)
        painter.setPen(colors["text_secondary"])

        meta_str = ""
        if task.has_error:
            painter.setPen(colors["error_fg"])
            meta_str = task.error_msg
        else:
            duration = str(task.duration_str or "").strip()
            if duration:
                meta_str = f"时长: {duration}"
            if task.upload_date and task.upload_date != "-":
                if meta_str:
                    meta_str += f" • 日期: {task.upload_date}"
                else:
                    meta_str = f"日期: {task.upload_date}"
            if not meta_str:
                meta_str = "待加载"

        fm_meta = QFontMetrics(meta_font)
        elided_meta = fm_meta.elidedText(meta_str, Qt.TextElideMode.ElideRight, rect.width())
        meta_rect = QRect(
            rect.left(), rect.bottom() - fm_meta.height() - 4, rect.width(), fm_meta.height()
        )
        painter.drawText(
            meta_rect, int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), elided_meta
        )

        painter.restore()

    def _draw_action_btn(
        self,
        painter: QPainter,
        rect: QRect,
        task: VideoTask,
        is_dark: bool,
        accent: QColor,
        colors: dict[str, QColor],
    ) -> None:
        painter.save()

        # 背景：if error → WinUI3 critical; if not queued yet → muted; else Fluent button
        if task.is_parsing:
            bg = colors["btn_subtle_bg"]
            fg = colors["text_secondary"]
            text = "解析中…"
        elif task.has_error:
            bg = colors["error_bg"]
            fg = colors["error_fg"]
            text = "错误"
        elif task.custom_options.format is None:
            # Not yet enqueued – show neutral "待加载" badge
            bg = QColor(255, 255, 255, 6) if is_dark else QColor(0, 0, 0, 5)
            fg = colors["muted_fg"]
            text = "待加载"
        else:
            # Loaded: keep the visual transition subtle to reduce perceptual flashing.
            bg = QColor(colors["btn_subtle_bg"])
            fg = colors["text_primary"]

            # 格式
            fmt_note = task.custom_options.format if task.custom_options.format else "自动最佳"
            if len(fmt_note) > 12:
                fmt_note = fmt_note[:10] + ".."
            text = fmt_note

        painter.setBrush(QBrush(bg))
        painter.setPen(QPen(colors["btn_border"], 1))

        # 画圆角按钮框
        painter.drawRoundedRect(rect, 5, 5)

        font = painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.setPen(fg)

        painter.drawText(rect, int(Qt.AlignmentFlag.AlignCenter), text)

        painter.restore()

    def hit_test(self, point: QPoint, option: QStyleOptionViewItem) -> str:
        """
        一个实用方法，给 QListView 的 clicked.connect 用的。
        当 View 被点中，它塞一个坐标过来，Delegate 充当判断者帮忙检测命中了哪个区域。
        """
        rect = option.rect  # type: ignore
        current_x = rect.left() + self.MARGIN
        center_y = rect.top() + (rect.height() // 2)

        chk_rect = QRect(
            current_x, center_y - self.CHECKBOX_SIZE // 2, self.CHECKBOX_SIZE, self.CHECKBOX_SIZE
        )
        if chk_rect.contains(point):
            return "checkbox"

        right_margin = rect.right() - self.MARGIN
        action_width = 140
        btn_rect = QRect(right_margin - action_width, center_y - 16, action_width, 32)
        if btn_rect.contains(point):
            return "action_btn"

        # 如果点击其它全区域算是选中这条
        return "row"
