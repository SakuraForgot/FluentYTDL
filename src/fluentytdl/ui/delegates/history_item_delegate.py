from __future__ import annotations

import os
from typing import Any

import qfluentwidgets as qfw
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem
from qfluentwidgets import Theme, isDarkTheme

from ...storage.history_service import HistoryRecord
from ...utils.formatters import format_duration, format_size
from ...utils.image_loader import get_image_loader


class HistoryItemDelegate(QStyledItemDelegate):
    """
    高性能的历史记录项渲染器。
    通过 QPainter 手动绘制卡片，而不是实例化 QWidget，解决长列表卡死问题。
    """

    reparse_clicked = Signal(int)
    open_folder_clicked = Signal(int)
    delete_clicked = Signal(int)

    # 模拟 hover 态跟踪
    _hovered_row: int = -1
    _hovered_button: str = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_cache: dict[str, QImage] = {}
        self._pending_urls: set[str] = set()

        self.ITEM_HEIGHT = 100
        self.THUMB_WIDTH = 128
        self.THUMB_HEIGHT = 72

    def set_pixmap(self, url: str, pixmap: QPixmap) -> None:
        if pixmap and not pixmap.isNull():
            self._image_cache[url] = pixmap.toImage()
            self._pending_urls.discard(url)

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        return QSize(option.rect.width(), self.ITEM_HEIGHT)

    def _get_hit_rects(self, option: QStyleOptionViewItem) -> dict[str, QRect]:
        """计算热区位置"""
        rect = option.rect
        r_top = rect.top()
        r_left = rect.left()
        r_width = rect.width()

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
        reparse_rect = QRect(
            folder_rect.left() - spacing - btn_size, folder_rect.top(), btn_size, btn_size
        )

        return {
            "reparse": reparse_rect,
            "folder": folder_rect,
            "delete": delete_rect,
        }

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        is_dark = isDarkTheme()
        rect = option.rect

        record: HistoryRecord | None = index.data(Qt.ItemDataRole.UserRole + 1)
        if not record:
            painter.restore()
            return

        is_hovered_card = option.state & QStyle.StateFlag.State_MouseOver

        # 1. 绘制背景
        bg_color = QColor(255, 255, 255, 9) if is_dark else QColor(0, 0, 0, 5)
        if is_hovered_card:
            bg_color = QColor(255, 255, 255, 13) if is_dark else QColor(0, 0, 0, 9)

        if not record.file_exists:
            # 丢失文件的记录用偏红底色警告
            bg_color = QColor(232, 17, 35, 15) if is_dark else QColor(232, 17, 35, 10)
            if is_hovered_card:
                bg_color = QColor(232, 17, 35, 25) if is_dark else QColor(232, 17, 35, 20)

        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(8, 4, -8, -4), 8, 8)
        painter.fillPath(path, bg_color)

        bd_color = QColor(255, 255, 255, 15) if is_dark else QColor(0, 0, 0, 15)
        painter.strokePath(path, QPen(bd_color, 1))

        # 热区
        hit_rects = self._get_hit_rects(option)

        # 2. 绘制缩略图
        thumb_left = rect.left() + 16
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

        placeholder_color = QColor(255, 255, 255, 10) if is_dark else QColor(0, 0, 0, 10)

        if record.thumbnail_url:
            if record.thumbnail_url in self._image_cache:
                painter.drawImage(thumb_rect, self._image_cache[record.thumbnail_url])
            else:
                painter.fillRect(thumb_rect, placeholder_color)
                if record.thumbnail_url not in self._pending_urls:
                    self._pending_urls.add(record.thumbnail_url)
                    get_image_loader().load(
                        record.thumbnail_url,
                        target_size=(self.THUMB_WIDTH, self.THUMB_HEIGHT),
                        radius=6,
                    )
        else:
            painter.fillRect(thumb_rect, placeholder_color)

        # 如果文件不存在，给缩略图蒙上一层灰
        if not record.file_exists:
            painter.fillRect(
                thumb_rect, QColor(0, 0, 0, 150) if is_dark else QColor(255, 255, 255, 150)
            )

        painter.restore()

        # 3. 绘制文本
        text_left = thumb_rect.right() + 16
        text_right = hit_rects["reparse"].left() - 16
        text_width = max(0, text_right - text_left)

        # 标题
        title_font = QFont(painter.font())
        title_font.setPixelSize(14)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)

        title_color = QColor(255, 255, 255) if is_dark else QColor(0, 0, 0)
        if not record.file_exists:
            title_color = QColor(150, 150, 150)
        painter.setPen(title_color)

        metrics = painter.fontMetrics()
        elided_title = metrics.elidedText(record.title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(
            QRect(text_left, rect.top() + 16, text_width, 24),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided_title,
        )

        # 详细信息
        meta_font = QFont(painter.font())
        meta_font.setPixelSize(12)
        meta_font.setWeight(QFont.Weight.Normal)
        painter.setFont(meta_font)
        painter.setPen(QColor(150, 150, 150) if is_dark else QColor(100, 100, 100))

        if record.file_exists:
            f_size = format_size(record.file_size)
            f_dur = format_duration(record.duration)
            time_str = record.get_local_time_str()
            meta_str = f"{f_size}  •  {f_dur}  •  下载于 {time_str}"
        else:
            meta_str = "⚠️ 文件已被移动或删除"

        painter.drawText(
            QRect(text_left, rect.top() + 44, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta_str,
        )

        # 来源 URL
        # URL
        author_font = QFont("Consolas" if os.name == "nt" else "Monospace")
        author_font.setPixelSize(11)
        painter.setFont(author_font)
        painter.setPen(QColor(120, 120, 120) if is_dark else QColor(140, 140, 140))

        elided_url = painter.fontMetrics().elidedText(
            record.url, Qt.TextElideMode.ElideRight, text_width
        )
        painter.drawText(
            QRect(text_left, rect.top() + 64, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided_url,
        )

        # 4. 绘制按钮
        def draw_button(name: str, btn_rect: QRect, icon_enum: Any, force_disabled: bool = False):
            is_hovered = self._hovered_row == index.row() and self._hovered_button == name

            if is_hovered and not force_disabled:
                hover_color = QColor(255, 255, 255, 15) if is_dark else QColor(0, 0, 0, 10)
                btn_path = QPainterPath()
                btn_path.addRoundedRect(btn_rect, 4, 4)
                painter.fillPath(btn_path, hover_color)

            theme = Theme.DARK if is_dark else Theme.LIGHT
            icon_pixmap = icon_enum.icon(theme=theme).pixmap(16, 16)

            # 手动给被禁用的图标降低对比度 (如果是用 QPainter)
            if force_disabled:
                painter.setOpacity(0.3)

            icon_x = btn_rect.left() + (btn_rect.width() - 16) // 2
            icon_y = btn_rect.top() + (btn_rect.height() - 16) // 2
            painter.drawPixmap(icon_x, icon_y, 16, 16, icon_pixmap)

            if force_disabled:
                painter.setOpacity(1.0)  # 还原

        # 重解析按钮
        draw_button("reparse", hit_rects["reparse"], qfw.FluentIcon.SYNC)
        # 文件夹按钮
        draw_button(
            "folder",
            hit_rects["folder"],
            qfw.FluentIcon.FOLDER,
            force_disabled=not record.file_exists,
        )
        # 删除按钮
        draw_button("delete", hit_rects["delete"], qfw.FluentIcon.DELETE)

        painter.restore()

    def editorEvent(
        self, event: QEvent, model: Any, option: QStyleOptionViewItem, index: Any
    ) -> bool:  # type: ignore[override]
        if event.type() == QEvent.Type.MouseMove:
            pos = event.pos()
            hit_rects = self._get_hit_rects(option)

            hovered = ""
            for name, rect in hit_rects.items():
                if rect.contains(pos):
                    hovered = name
                    break

            if self._hovered_row != index.row() or self._hovered_button != hovered:
                self._hovered_row = index.row()
                self._hovered_button = hovered
                if hasattr(model, "dataChanged"):
                    model.dataChanged.emit(index, index)
            return True

        elif event.type() == QEvent.Type.Leave:
            if self._hovered_row != -1:
                old_row = self._hovered_row
                self._hovered_row = -1
                self._hovered_button = ""
                if hasattr(model, "dataChanged"):
                    idx = model.index(old_row, 0)
                    model.dataChanged.emit(idx, idx)
            return True

        elif event.type() == QEvent.Type.MouseButtonPress:
            pos = event.pos()
            hit_rects = self._get_hit_rects(option)

            record: HistoryRecord | None = index.data(Qt.ItemDataRole.UserRole + 1)
            if not record:
                return False

            if hit_rects["reparse"].contains(pos):
                self.reparse_clicked.emit(index.row())
                return True
            elif hit_rects["folder"].contains(pos) and record.file_exists:
                self.open_folder_clicked.emit(index.row())
                return True
            elif hit_rects["delete"].contains(pos):
                self.delete_clicked.emit(index.row())
                return True

        return super().editorEvent(event, model, option, index)
