from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

import qfluentwidgets as qfw
from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem
from qfluentwidgets import Theme, ThemeColor, isDarkTheme, qconfig, themeColor

from ...utils.formatters import format_size, format_time_ago
from ...utils.image_loader import get_image_loader
from ..models.task_row import TaskRow
from .row_animator import CH_CHECK, CH_HOVER, CH_PRESS, CH_PROGRESS, CH_SELECT, RowAnimator

#: 「跑到一半」的中途状态。落在这里**且没有活 worker**，就等于「上个会话被打断了」。
#:
#: `downloading` / `parsing` / `processing` 只可能来自 DB 快照 ——
#: `worker.effective_state` 从不返回它们（那边只吐 running / paused / completed /
#: error / cancelled / quality_guard / queued）。`running` 是唯一两边都可能出现的值：
#: 活 worker 正在跑时返回它，而 3.5.5 之前的老库里也可能存着这个已废弃的状态字符串
#: （全项目现已无任何代码发射它，见 `task_db.py` 的所有权注释）。
_INTERRUPTED_STATES = frozenset({"downloading", "parsing", "processing", "running"})


class DownloadItemDelegate(QStyledItemDelegate):
    """
    高性能的下载队列项渲染器
    直接使用 QPainter 绘制，避免上万任务时产生的 QWidget 对象开销。
    """

    # Signals for view to connect to
    pause_resume_clicked = Signal(int)
    open_folder_clicked = Signal(int)
    delete_clicked = Signal(int)
    selection_toggled = Signal(int)

    # 缩略图缓存上限（条）。单张 128x72 ARGB32 约 36KB，512 条约 18MB。
    # 之前整个会话不淘汰：长时间挂着刷几千个任务，缓存会一路涨到几百 MB。
    _IMAGE_CACHE_MAX = 512

    # 卡片圆角。历史页 `CardWidget` 是 5，这里取 8 是有意的 —— 卡片左右各内缩 8px，
    # 呈现的是一张浮起的卡而不是贴边的列表行。「不够圆润」的实际来源是两条重叠描边
    # 被抗锯齿糊在圆角上，不是半径本身。
    CARD_RADIUS = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        # LRU：命中即 move_to_end，超限从最旧端淘汰
        self._image_cache: OrderedDict[str, QImage] = OrderedDict()
        # 正在异步加载中的 url，避免同一张封面被 paint() 反复提交加载
        self._pending_urls: set[str] = set()
        # Track mouse hover for buttons
        self._hovered_row = -1
        self._hovered_button = ""  # "pause", "folder", "delete", "checkbox"
        # 由 TaskListView 通过 setPressedRow / setSelectedRows 写入
        self._pressed_row = -1
        self._selected_rows: set[int] = set()
        # 正被右键菜单操作的行（`TaskListView.set_context_row` 写入）。和 `_hovered_row`
        # 分开存是必须的：菜单是个抢鼠标的 popup，它一弹出视图就收到 `Leave`，
        # `_hovered_row` 当场被清成 -1，而菜单里正摆着「连同文件一起删除」。
        self._context_row = -1

        self.ITEM_HEIGHT = 100
        self.THUMB_WIDTH = 128
        self.THUMB_HEIGHT = 72

        # 「列表里当前是否已有选中项」。常驻多选取代了旧的 _is_batch_mode 模态开关：
        # 复选框不再由一个全局按钮切换，而是在「悬停该行 / 该行已选中 / 已有选中项」
        # 任一成立时淡入。命中区域始终为复选框预留（见 _get_hit_rects），
        # 否则鼠标一进一出就会让缩略图和标题左右平移 36px。
        self._selection_active = False

        # 嫌疑人三修复：预缓存按钮图标 pixmap，避免每帧 icon.pixmap() CPU 密集型调用
        self._icon_cache: dict[str, QPixmap] = {}

        # 悬停 / 按下 / 选中 / 进度的插值器。paint() 里读，RowAnimator 自己驱动重绘请求。
        self.animator = RowAnimator(self)

        # 强调色缓存：themeColor() 每次都要做一遍 HSV 变换，而它在 paint() 里是
        # 每行每帧都要取的。主题或强调色变化时失效即可。
        self._accent_cache: tuple[QColor, QColor] | None = None
        qconfig.themeChanged.connect(self._invalidate_theme_cache)
        qconfig.themeColorChanged.connect(self._invalidate_theme_cache)

    def _invalidate_theme_cache(self, *args) -> None:
        self._accent_cache = None
        self._icon_cache.clear()

    def _accent(self, is_dark: bool) -> QColor:
        """强调色。浅色主题下取 DARK_1 变体，保证在浅底上仍有足够对比度。"""
        if self._accent_cache is None:
            self._accent_cache = (themeColor(), ThemeColor.DARK_1.color())
        return self._accent_cache[0] if is_dark else self._accent_cache[1]

    @staticmethod
    def _blend(c0: QColor, c1: QColor, t: float) -> QColor:
        """两个**叠加色**之间插值，在预乘 alpha 空间做。

        直接插 RGBA 会让「黑@20 → 白@13」中途穿过一段肉眼可见的灰 —— 半透明色的
        RGB 只在乘上自己的 alpha 之后才代表它真正贡献的光。
        """
        if t <= 0.004:
            return c0
        if t >= 0.996:
            return c1
        a0, a1 = c0.alpha(), c1.alpha()
        a = a0 + (a1 - a0) * t
        if a < 0.5:
            return QColor(0, 0, 0, 0)
        ch = []
        for v0, v1 in ((c0.red(), c1.red()), (c0.green(), c1.green()), (c0.blue(), c1.blue())):
            p0, p1 = v0 * a0, v1 * a1
            ch.append(min(255, max(0, round((p0 + (p1 - p0) * t) / a))))
        return QColor(ch[0], ch[1], ch[2], round(a))

    @staticmethod
    def _border_brush(card: QRectF, top: QColor, bottom: QColor) -> QBrush:
        """描边画刷。

        `CardWidget` 是上下两段分开描的，这里换成一条竖向渐变的连续笔 ——
        一条笔才不会在圆角处显出接缝或双线。
        """
        if top == bottom:
            return QBrush(top)
        grad = QLinearGradient(card.topLeft(), card.bottomLeft())
        grad.setColorAt(0.0, top)
        grad.setColorAt(0.75, top)
        grad.setColorAt(1.0, bottom)
        return QBrush(grad)

    def set_selection_active(self, active: bool) -> None:
        """由视图告知「列表里当前是否已有选中项」，用于决定复选框是否常显。

        只读 `_selected_rows` 是不够的：`selectAll()` / `selectionModel().select()`
        这类程序化选择不会走 `ListBase.updateSelectedRows()`，所以 TaskListView 会
        额外接 `selectionModel().selectionChanged` 再调这里。
        """
        self._selection_active = active

    def setHoverRow(self, row: int) -> None:
        """Required by qfluentwidgets ListView"""
        if self._hovered_row != row:
            self._hovered_row = row
            # 离开该行时按钮悬停态必须一起清掉，否则移回来会先闪一下旧按钮的高亮
            self._hovered_button = ""

    def setPressedRow(self, row: int) -> None:
        """Required by qfluentwidgets ListView"""
        self._pressed_row = row

    def set_context_row(self, row: int) -> None:
        """右键菜单正作用于哪一行（-1 = 无）。走 hover 通道渲染，语义是「视觉焦点」。

        右键**不再选中卡片**（见 `TaskListView.__init__`），所以这一行的高亮全靠它 ——
        否则用户面对一个「删除记录 / 连同文件一起删除」的菜单，看不出目标是哪一行。
        """
        self._context_row = row

    def setSelectedRows(self, indexes: Any) -> None:
        """Required by qfluentwidgets ListView（`ListBase._setSelectedRows` 会直接调用）。

        没有这个方法时，启用 ExtendedSelection 后第一次点击就 AttributeError。
        注意 `paint()` **不读** `_selected_rows` —— 选中态的唯一真值是
        `option.state & State_Selected`。这里维护它只为两件事：按下态复位，
        以及裸 `ListView` 场景下也能推出 `_selection_active`。
        """
        self._selected_rows = {index.row() for index in indexes}
        self._selection_active = bool(self._selected_rows)
        if self._pressed_row in self._selected_rows:
            # 与 TableItemDelegate.setSelectedRows 语义一致：选中即视为按下结束
            self._pressed_row = -1

    def _cached_image(self, url: str) -> QImage | None:
        """取缓存并把该条移到 LRU 末端。"""
        img = self._image_cache.get(url)
        if img is not None:
            self._image_cache.move_to_end(url)
        return img

    def set_pixmap(self, url: str, pixmap: QPixmap) -> None:
        """异步图片加载完成后，将预缩放的 image 缓存到 delegate 内部"""
        if pixmap and not pixmap.isNull():
            # 嫌疑人三修复：在加载时一次性缩放好，paint() 中只做贴图
            scaled = pixmap.scaled(
                self.THUMB_WIDTH,
                self.THUMB_HEIGHT,
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._image_cache[url] = scaled.toImage()
            self._image_cache.move_to_end(url)
            while len(self._image_cache) > self._IMAGE_CACHE_MAX:
                self._image_cache.popitem(last=False)
            self._pending_urls.discard(url)

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        return QSize(option.rect.width(), self.ITEM_HEIGHT)

    def _get_hit_rects(self, rect: QRect) -> dict[str, QRect]:
        """Calculate layout rects dynamically based on the row rect.

        接 QRect 而不是 QStyleOptionViewItem：TaskListView 做 tooltip 命中测试时
        手里只有 visualRect()，没有 option。
        """
        r_top = rect.top()
        r_left = rect.left()
        r_width = rect.width()

        # Checkbox —— 列宽**永久预留**。复选框只是「画不画」随悬停/选中变化，
        # 布局绝不能跟着变：否则鼠标划过一行就会让缩略图和标题平移 36px。
        checkbox_rect = QRect(r_left + 16, r_top + (self.ITEM_HEIGHT - 20) // 2, 20, 20)

        # Buttons (Right side)
        btn_size = 32
        spacing = 8
        margin_right = 16

        delete_rect = QRect(
            r_left + r_width - margin_right - btn_size,
            r_top + (self.ITEM_HEIGHT - btn_size) // 2,
            btn_size,
            btn_size,
        )
        folder_rect = QRect(
            delete_rect.left() - spacing - btn_size, delete_rect.top(), btn_size, btn_size
        )
        pause_rect = QRect(
            folder_rect.left() - spacing - btn_size, folder_rect.top(), btn_size, btn_size
        )

        return {
            "checkbox": checkbox_rect,
            "pause": pause_rect,
            "folder": folder_rect,
            "delete": delete_rect,
        }

    def _text_column(self, rect: QRect, hit_rects: dict[str, QRect]) -> tuple[int, int]:
        """标题/元信息文本列的 (left, width)。缩略图右侧到暂停按钮左侧。"""
        thumb_left = hit_rects["checkbox"].right() + 16
        text_left = thumb_left + self.THUMB_WIDTH + 16
        return text_left, max(0, hit_rects["pause"].left() - 16 - text_left)

    def _title_rect(self, rect: QRect, hit_rects: dict[str, QRect]) -> QRect:
        """标题那一行的矩形。`paint` 和 tooltip 的锚点共用同一个来源 ——
        两处各写一遍 `+16 / 20` 迟早会漂移，那时气泡就贴不到标题上了。"""
        text_left, text_width = self._text_column(rect, hit_rects)
        return QRect(text_left, rect.top() + 16, text_width, 20)

    # === 终态行的 meta 文案 ===
    # 融合后「已完成」既可能是本次会话刚下完的活任务，也可能是分页补进来的历史行。
    # 两者渲染同一套文案，用户才看不出「这行是从哪来的」—— 那正是融合要达到的效果。

    def _completed_meta(self, row_obj: TaskRow) -> str:
        """已完成行：`大小 · 分辨率 · 完成时间`，缺项自动省略。

        活任务刚完成时 `updated_at` 还是 0（`from_worker` 不写它，落库由 `db_writer`
        异步补），此时只显示前两项；下次启动分页读回来就三项齐全。
        """
        parts: list[str] = []
        if row_obj.file_exists is False:
            # 文件被用户在外部删了。这是整行降透明之外的文字说明 —— 只靠透明度
            # 用户会以为是渲染 bug。
            parts.append(self.tr("文件已丢失"))
        parts.extend(
            p
            for p in (
                format_size(row_obj.effective_file_size, zero=""),
                row_obj.effective_format_note,
                format_time_ago(row_obj.updated_at),
            )
            if p
        )
        return " · ".join(parts) or self.tr("下载完成")

    def _append_time(self, label: str, row_obj: TaskRow) -> str:
        """失败 / 取消行：状态文案后面补一个「3 天前」，否则历史行读不出时间。"""
        ago = format_time_ago(row_obj.updated_at)
        return f"{label} · {ago}" if ago else label

    def _interrupted_label(self, state: str) -> str:
        """被打断的中途状态 → 文案。分三种是因为它们指向的现场完全不同。

        `processing` 尤其要分出来：那是 FFmpeg 合并/嵌字幕阶段，半成品躺在 sandbox
        临时目录里，和「下载才走到 45%」不是一回事。
        """
        if state == "parsing":
            return self.tr("解析已中断")
        if state == "processing":
            return self.tr("后处理已中断")
        return self.tr("下载已中断")

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        is_dark = isDarkTheme()
        rect = option.rect

        data = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, TaskRow):
            painter.restore()
            return

        # 融合后一行有两种来源：有 worker 的活任务，和只有 DB 快照的历史行。
        # **不许再 `if not worker: return`** —— 那正是历史行被画成空白条的原因。
        # 渲染值一律走 `effective_*`（见 `ui/models/task_row.py`）。
        title = data.effective_title
        thumbnail = data.effective_thumbnail
        row = index.row()
        # 选中态的唯一真值：Qt 的 selectionModel（经 option.state 传进来）。
        # 模型里不再有 is_selected 字段，delegate 也不再读 _selected_rows。
        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # Determine State (canonical single source of truth)
        state = data.effective_state

        status_text = data.effective_status_text
        # `_context_row` 也算「悬停」：菜单一弹出鼠标就被 popup 抢走，State_MouseOver 和
        # `_hovered_row` 双双失效，而那一行恰恰是用户此刻唯一关心的一行。
        is_hovered = (
            bool(option.state & QStyle.StateFlag.State_MouseOver)
            or self._hovered_row == row
            or self._context_row == row
        )

        # 文件已被用户在外部删掉：整行降透明。用 painter 的 opacity 而不是
        # `QGraphicsOpacityEffect`（历史卡片的做法）—— delegate 没有 widget 可以挂 effect，
        # 而且 effect 会给每行多一个离屏缓冲。`None` = 还没检查过，按存在处理。
        if data.file_exists is False:
            painter.setOpacity(0.55)

        # --- 动画通道（指数趋近，改 target 即重定向，不存在被打断的动画对象）---
        # 用 approach_lazy：静息为 0 的通道不建键，1000 行时字典不会白白涨到 7000 项。
        hover_t = self.animator.approach_lazy(row, CH_HOVER, 1.0 if is_hovered else 0.0)
        press_t = self.animator.approach_lazy(
            row, CH_PRESS, 1.0 if self._pressed_row == row else 0.0
        )
        select_t = self.animator.approach_lazy(row, CH_SELECT, 1.0 if is_selected else 0.0)

        # --- Draw Background ---
        # 卡片本体逐字对齐历史页在用的 `CardWidget`
        # （`qfluentwidgets/components/widgets/card_widget.py`）：三档背景 + 一条 1px 描边，
        # 浅色主题下悬停时底边加深。差别只在于这里把 CardWidget 的三档**跳变**换成插值。
        card = QRectF(rect.adjusted(8, 4, -8, -4))
        r = float(self.CARD_RADIUS)

        if is_dark:
            bg = self._blend(
                self._blend(QColor(255, 255, 255, 13), QColor(255, 255, 255, 21), hover_t),
                QColor(255, 255, 255, 8),
                press_t,
            )
            # 暗色下 CardWidget 的上下描边同色
            top_bd = bottom_bd = self._blend(
                self._blend(QColor(0, 0, 0, 20), QColor(255, 255, 255, 13), hover_t),
                QColor(255, 255, 255, 18),
                press_t,
            )
        else:
            bg = self._blend(
                self._blend(QColor(255, 255, 255, 170), QColor(255, 255, 255, 64), hover_t),
                QColor(255, 255, 255, 64),
                press_t,
            )
            top_bd = QColor(0, 0, 0, 15)
            # CardWidget 只在「悬停且未按下」时加深底边 —— 它那一点立体感就来自这里
            bottom_bd = self._blend(top_bd, QColor(0, 0, 0, 27), hover_t * (1.0 - press_t))

        path = QPainterPath()
        path.addRoundedRect(card, r, r)
        painter.fillPath(path, bg)

        # **只描一条边**。原来是 2.5px 外层 +1.2px 内层叠在同一条路径上：两条抗锯齿描边
        # 在圆角处必然错开，显出双线；而暗色下内层是白@30~50，比历史卡片最亮的白@13 还亮
        # 两倍多 —— 那就是「额外多一层白边」和「不够圆润」的全部来源。
        # 路径向内收半像素：1px 笔宽画在路径**中心**，压在整数边界上会被抗锯齿摊成
        # 两个半亮像素，看着就是毛边。
        stroke_path = QPainterPath()
        stroke_path.addRoundedRect(card.adjusted(0.5, 0.5, -0.5, -0.5), r - 0.5, r - 0.5)
        painter.strokePath(stroke_path, QPen(self._border_brush(card, top_bd, bottom_bd), 1.0))

        # 选中态叠一层中性色，取值同 `TableItemDelegate.paint`（selected 静息 17）。
        # 强调色只出现在左侧指示条上 —— Fluent 的列表不会把整行染成强调色。
        if select_t > 0.004:
            c = 255 if is_dark else 0
            painter.fillPath(path, QColor(c, c, c, int(17 * select_t)))

        # --- 左侧强调指示条（移植 ListItemDelegate._drawIndicator，带动画伸展）---
        if select_t > 0.01:
            # 基类语义：按下时指示条变短（ph 变大）
            ph = (0.257 + 0.093 * press_t) * card.height()
            ind_h = (card.height() - 2 * ph) * select_t
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._accent(is_dark))
            # 画在卡片**内侧**，紧贴左描边往里 1px。放在卡片外面（原来是 rect.left()+2）
            # 会变成一根和卡片脱开的浮条。
            painter.drawRoundedRect(
                QRectF(card.left() + 1.0, card.top() + (card.height() - ind_h) / 2, 3.0, ind_h),
                1.5,
                1.5,
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)

        hit_rects = self._get_hit_rects(rect)

        # --- 1. Checkbox ---
        # 常驻多选：不再有模态开关。悬停该行、该行已选中、或列表里已有选中项时淡入。
        cb_rect = hit_rects["checkbox"]
        check_t = self.animator.approach_lazy(
            row, CH_CHECK, 1.0 if (is_hovered or is_selected or self._selection_active) else 0.0
        )

        if check_t > 0.01:
            painter.save()
            painter.setOpacity(check_t)

            cb_color = QColor(0, 0, 0, 0)
            cb_border = QColor(255, 255, 255, 100) if is_dark else QColor(0, 0, 0, 100)

            if is_selected:
                cb_color = self._accent(is_dark)  # 跟随用户主题强调色
                cb_border = cb_color

            if self._hovered_row == row and self._hovered_button == "checkbox":
                cb_border = QColor(255, 255, 255, 150) if is_dark else QColor(0, 0, 0, 150)

            cb_path = QPainterPath()
            cb_path.addRoundedRect(cb_rect, 4, 4)
            painter.fillPath(cb_path, cb_color)
            painter.strokePath(cb_path, QPen(cb_border, 1.5))

            if is_selected:
                # Draw checkmark
                check_pen = QPen(Qt.GlobalColor.white, 2)
                check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
                check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
                painter.setPen(check_pen)
                painter.drawLine(
                    cb_rect.left() + 4, cb_rect.top() + 10, cb_rect.left() + 8, cb_rect.top() + 14
                )
                painter.drawLine(
                    cb_rect.left() + 8, cb_rect.top() + 14, cb_rect.left() + 15, cb_rect.top() + 5
                )

            painter.restore()

        # --- 2. Thumbnail ---
        thumb_left = cb_rect.right() + 16
        thumb_rect = QRect(
            thumb_left,
            rect.top() + (self.ITEM_HEIGHT - self.THUMB_HEIGHT) // 2,
            self.THUMB_WIDTH,
            self.THUMB_HEIGHT,
        )

        painter.save()
        thumb_path = QPainterPath()
        thumb_path.addRoundedRect(thumb_rect, 6, 6)
        painter.setClipPath(thumb_path)

        if thumbnail:
            cached_img = self._cached_image(thumbnail)
            if cached_img is not None:
                painter.drawImage(thumb_rect, cached_img)
            else:
                # Draw placeholder while loading
                painter.fillRect(
                    thumb_rect, QColor(255, 255, 255, 10) if is_dark else QColor(0, 0, 0, 10)
                )
                # Trigger async load — the view must connect loaded_with_url
                # to feed pixmaps back into our cache via set_pixmap()
                if thumbnail not in self._pending_urls:
                    self._pending_urls.add(thumbnail)
                    get_image_loader().load(
                        thumbnail, target_size=(self.THUMB_WIDTH, self.THUMB_HEIGHT), radius=6
                    )
        else:
            painter.fillRect(
                thumb_rect, QColor(255, 255, 255, 10) if is_dark else QColor(0, 0, 0, 10)
            )
        painter.restore()

        # --- 3. Texts & Progress ---
        text_left, text_width = self._text_column(rect, hit_rects)

        # Title (Line 1)
        title_font = QFont(painter.font())
        title_font.setPixelSize(14)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(255, 255, 255) if is_dark else QColor(0, 0, 0))

        fm = painter.fontMetrics()
        elided_title = fm.elidedText(title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(
            self._title_rect(rect, hit_rects),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided_title,
        )

        # --- Progress Bar (Single) ---
        progress = data.effective_progress
        if state == "completed" and progress <= 0:
            # 历史行的 `progress` 列不一定落了 100（终态是由 completed 信号写的，
            # 进度是 CleanLogger 另一路写的，最后一个 tick 可能没排到）。已完成就是满格。
            progress = 100.0
        # 模型侧 150ms 的节流会让进度呈台阶状；这里把它插值成连续运动。
        # 变化量小于 EPS(≈0.4%) 时 approach 直接吸附，不会为不可见的位移空转 60fps。
        shown_progress = (
            self.animator.approach_lazy(row, CH_PROGRESS, float(progress) / 100.0) * 100.0
        )
        bar_y = rect.top() + 46
        self._draw_bar(painter, text_left, bar_y, text_width, 6, shown_progress, state, is_dark)

        if state == "error":
            bar_rect_err = QRect(text_left, bar_y, text_width, 6)
            err_path = QPainterPath()
            err_path.addRoundedRect(bar_rect_err, 3, 3)
            painter.fillPath(err_path, QColor(232, 17, 35))

        # Meta text (below bar - all info combined, grey)
        meta_font = QFont("Consolas" if os.name == "nt" else "Monospace")
        meta_font.setPixelSize(12)
        painter.setFont(meta_font)
        painter.setPen(QColor(150, 150, 150) if is_dark else QColor(100, 100, 100))

        meta_str = ""
        if state == "running" and data.is_live:
            # `and data.is_live`：老库里可能存着已废弃的 `running` 快照，那种行没有
            # worker，落到下面的「已中断」支才对 —— 否则会拿上个会话的瞬时进度冒充实况。
            if status_text:
                # 统一使用 CleanLogger 精心调配的全能字符串
                meta_str = status_text
            elif progress > 0:
                meta_str = f"下载: {progress:.1f}%"
            else:
                meta_str = self.tr("准备下载...")
        elif state == "completed":
            meta_str = self._completed_meta(data)
        elif state == "error":
            meta_str = self._append_time(self.tr("下载失败"), data)
        elif state == "queued":
            meta_str = self.tr("等待下载...")
        elif state == "paused":
            meta_str = self.tr("已暂停")
        elif state == "cancelled":
            meta_str = self._append_time(self.tr("已取消"), data)
        elif state == "quality_guard":
            # download_manager 挂起时会把原因写进 status_text（如「风控防御挂起」），优先展示
            meta_str = status_text or self.tr("已被质量守卫挂起")
        elif state in _INTERRUPTED_STATES:
            # 走到这里 = 中途状态 + 没有活 worker，也就是「上个会话跑到一半、进程没了」。
            # 原先没有这一支，这类行的 meta 是空字符串：重启后看到一根停在 45% 的
            # 进度条，底下一个字都没有 —— 而 `processing`（FFmpeg 合并中被打断）正是
            # 发射次数最多的状态。
            #
            # 刻意不显示落库的 `status_text`（那是 CleanLogger 的「45.2% · 3.2MB/s ·
            # 剩余 00:12」）—— 它是上次会话的瞬时值，摆在一行早就不动了的任务下面，
            # 看着像还在下。百分比已经由进度条表达，这里要说的是「它停了，几时停的」。
            meta_str = self._append_time(self._interrupted_label(state), data)

        painter.drawText(
            QRect(text_left, rect.top() + 66, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta_str,
        )

        # --- 5. Buttons ---
        def draw_button(
            name: str,
            rect: QRect,
            icon_enum: Any,
            hidden: bool = False,
            interactive: bool = True,
            custom_color: QColor | None = None,
        ):
            if hidden:
                return
            is_btn_hovered = (
                self._hovered_row == row and self._hovered_button == name and interactive
            )

            # Hover BG（同样走插值，避免按钮底色单步跳变）
            btn_t = self.animator.approach_lazy(row, f"btn_{name}", 1.0 if is_btn_hovered else 0.0)
            if btn_t > 0.01:
                hover_color = (
                    QColor(255, 255, 255, int(15 * btn_t))
                    if is_dark
                    else QColor(0, 0, 0, int(10 * btn_t))
                )
                btn_path = QPainterPath()
                btn_path.addRoundedRect(rect, 4, 4)
                painter.fillPath(btn_path, hover_color)

            # Draw Icon (使用缓存，避免每帧 icon.pixmap() 热路径)
            theme = Theme.DARK if is_dark else Theme.LIGHT
            color_key = custom_color.name() if custom_color else "default"
            cache_key = f"{icon_enum}_{theme}_{color_key}"

            icon_pixmap = self._icon_cache.get(cache_key)
            if icon_pixmap is None:
                if custom_color:
                    icon_pixmap = icon_enum.icon(color=custom_color).pixmap(16, 16)
                else:
                    icon_pixmap = icon_enum.icon(theme=theme).pixmap(16, 16)
                self._icon_cache[cache_key] = icon_pixmap

            icon_x = rect.left() + (rect.width() - 16) // 2
            icon_y = rect.top() + (rect.height() - 16) // 2
            painter.drawPixmap(icon_x, icon_y, 16, 16, icon_pixmap)

        # Folder button (always show if output_path exists or completed)
        draw_button("folder", hit_rects["folder"], qfw.FluentIcon.FOLDER)

        # Delete button (always show)
        draw_button("delete", hit_rects["delete"], qfw.FluentIcon.DELETE)

        # Pause/Resume button (or completed checkmark)
        icon_enum = qfw.FluentIcon.PLAY
        interactive = True
        custom_color = None

        if state == "running" or state == "queued":
            icon_enum = qfw.FluentIcon.PAUSE
        elif state == "completed":
            icon_enum = qfw.FluentIcon.COMPLETED
            interactive = False
            custom_color = QColor(16, 124, 16) if not is_dark else QColor(114, 204, 114)

        draw_button(
            "pause",
            hit_rects["pause"],
            icon_enum,
            hidden=False,
            interactive=interactive,
            custom_color=custom_color,
        )

        painter.restore()

    def _draw_bar(
        self,
        painter: QPainter,
        x: int,
        y: int,
        width: int,
        height: int,
        progress: float,
        state: str,
        is_dark: bool,
        color: QColor | None = None,
    ) -> None:
        """绘制进度条的通用辅助方法"""
        bar_rect = QRect(x, y, width, height)
        bar_bg = QColor(255, 255, 255, 20) if is_dark else QColor(0, 0, 0, 20)

        # 背景
        bar_path = QPainterPath()
        bar_path.addRoundedRect(bar_rect, height // 2, height // 2)
        painter.fillPath(bar_path, bar_bg)

        # 填充
        if progress > 0 and state != "error" and state != "queued":
            fill_width = max(height, int(width * (progress / 100.0)))
            fill_rect = QRect(x, y, fill_width, height)
            fill_path = QPainterPath()
            fill_path.addRoundedRect(fill_rect, height // 2, height // 2)

            fill_color = color or self._accent(is_dark)
            if state == "completed":
                fill_color = QColor(16, 124, 16)
            elif state == "paused":
                fill_color = QColor(150, 150, 150)

            painter.fillPath(fill_path, fill_color)

    def tooltip_at(self, rect: QRect, pos: Any, index: Any) -> tuple[str, QRect]:
        """给 TaskListView 用的命中区域 →（tooltip 文案, 锚定矩形）。

        delegate 没有子控件，所以 tooltip 只能由视图在 QEvent.ToolTip 时反查。
        第二个返回值是该文案**所属区域**的视口矩形，视图把气泡贴在它上方 —— 效果等同于
        把 `ToolTipFilter(position=TOP)` 挂在一颗真实按钮上。没命中时返回空文案 + 空矩形。
        """
        hit_rects = self._get_hit_rects(rect)

        # tooltip 只在鼠标悬停该行时才可能触发，而悬停就意味着复选框已淡入 —— 所以
        # 这里不需要再判断可见性。
        if hit_rects["checkbox"].contains(pos):
            return self.tr("选择此任务（可按住 Ctrl / Shift 多选）"), hit_rects["checkbox"]
        if hit_rects["folder"].contains(pos):
            return self.tr("打开所在文件夹"), hit_rects["folder"]
        if hit_rects["delete"].contains(pos):
            return self.tr("删除任务"), hit_rects["delete"]

        data = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, TaskRow):
            return "", QRect()
        state = data.effective_state

        if hit_rects["pause"].contains(pos):
            if state == "completed":
                return self.tr("已完成"), hit_rects["pause"]
            if state in ("running", "queued"):
                return self.tr("暂停"), hit_rects["pause"]
            return self.tr("开始 / 继续"), hit_rects["pause"]

        # 标题列：标题被 elide 掉是常态，悬停给出完整标题。
        # 锚点固定在标题那一行，而不是鼠标当前那一点 —— 在文本列里横向移动鼠标时
        # 气泡不该跟着游走（原生 QToolTip 跟手，`ToolTipFilter` 不跟手，这里对齐后者）。
        text_left, text_width = self._text_column(rect, hit_rects)
        if text_width > 0 and text_left <= pos.x() <= text_left + text_width:
            return data.effective_title, self._title_rect(rect, hit_rects)
        return "", QRect()

    def is_interactive_at(self, rect: QRect, pos: Any) -> bool:
        """位置是否落在复选框 / 三颗按钮这类可交互子区域上。

        供 `TaskListView` 区分「点了卡片」和「点了卡片里的控件」—— delegate 没有子控件，
        视图没法靠 childAt() 判断。
        """
        return any(r.contains(pos) for r in self._get_hit_rects(rect).values())

    def editorEvent(
        self, event: QEvent, model: Any, option: QStyleOptionViewItem, index: Any
    ) -> bool:  # type: ignore[override]
        if not index.isValid():
            return False

        if event.type() == QEvent.Type.MouseMove:
            pos = event.pos()
            hit_rects = self._get_hit_rects(option.rect)

            hovered = ""
            for name, rect in hit_rects.items():
                if rect.contains(pos):
                    hovered = name
                    break

            if self._hovered_row != index.row() or self._hovered_button != hovered:
                old_row = self._hovered_row
                self._hovered_row = index.row()
                self._hovered_button = hovered
                # 定向重绘：只刷旧行和新行，不穿透 Model，也不整屏
                self._repaint_rows(option.widget, old_row, index.row())
            # 必须交还事件：返回 True 会让 QAbstractItemView 提前 return，
            # 从而吞掉拖拽框选（阶段 2 的 ExtendedSelection 依赖它）。
            return False

        elif event.type() == QEvent.Type.Leave:
            if self._hovered_row != -1:
                old_row = self._hovered_row
                self._hovered_row = -1
                self._hovered_button = ""
                self._repaint_rows(option.widget, old_row)
            # 同样交还，让 ListBase.leaveEvent 里的 _setHoverRow(-1) 继续生效
            return False

        elif event.type() == QEvent.Type.MouseButtonPress:
            # 只认左键。`QAbstractItemView.mousePressEvent` 是先问 delegate 的 editorEvent
            # 再看按键的，右键按下同样会走到这里（已实测，与 setSelectRightClickedRow 无关）
            # —— 不拦的话右键点在删除按钮上就会真的删除任务，而用户以为只是弹个菜单。
            if event.button() != Qt.MouseButton.LeftButton:
                return False

            pos = event.pos()
            hit_rects = self._get_hit_rects(option.rect)

            task = index.data(Qt.ItemDataRole.UserRole)
            state = task.effective_state if isinstance(task, TaskRow) else ""

            if hit_rects["checkbox"].contains(pos):
                # 返回 True 会让 QAbstractItemView.mousePressEvent 提前 return，
                # 从而跳过它默认的 ClearAndSelect —— 复选框必须是「切换」而不是「独占选中」。
                self.selection_toggled.emit(index.row())
                return True
            elif hit_rects["pause"].contains(pos) and state != "completed":
                self.pause_resume_clicked.emit(index.row())
                return True
            elif hit_rects["folder"].contains(pos):
                self.open_folder_clicked.emit(index.row())
                return True
            elif hit_rects["delete"].contains(pos):
                self.delete_clicked.emit(index.row())
                return True

        return super().editorEvent(event, model, option, index)

    def _repaint_rows(self, view: Any, *rows: int) -> None:
        """按行号定向刷新视口。行号无效或不在视口内时静默跳过。"""
        if view is None or not hasattr(view, "viewport"):
            return
        model = view.model()
        if model is None:
            return
        viewport = view.viewport()
        row_count = model.rowCount()
        for row in rows:
            if row < 0 or row >= row_count:
                continue
            idx = model.index(row, 0)
            if idx.isValid():
                # 卡片整个画在 option.rect 内部（左右内缩 8、上下内缩 4），
                # 所以 ±1 只是抗锯齿的安全余量
                viewport.update(view.visualRect(idx).adjusted(-1, -1, 1, 1))
