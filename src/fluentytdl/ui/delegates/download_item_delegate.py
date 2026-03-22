from __future__ import annotations

import os
from typing import Any

import qfluentwidgets as qfw
from PySide6.QtCore import QEvent, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem
from qfluentwidgets import Theme, isDarkTheme

from ...download.workers import DownloadWorker
from ...utils.image_loader import get_image_loader


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

    def __init__(self, parent=None):
        super().__init__(parent)
        self._image_cache: dict[str, QImage] = {}
        # Track mouse hover for buttons
        self._hovered_row = -1
        self._hovered_button = ""  # "pause", "folder", "delete", "checkbox"

        self.ITEM_HEIGHT = 100
        self.THUMB_WIDTH = 128
        self.THUMB_HEIGHT = 72

        # Batch Mode Control
        self._is_batch_mode = False

        # 嫌疑人三修复：预缓存按钮图标 pixmap，避免每帧 icon.pixmap() CPU 密集型调用
        self._icon_cache: dict[str, QPixmap] = {}

    def set_selection_mode(self, enabled: bool) -> None:
        self._is_batch_mode = enabled

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
            if hasattr(self, "_pending_urls"):
                self._pending_urls.discard(url)

    def sizeHint(self, option: QStyleOptionViewItem, index: Any) -> QSize:
        return QSize(option.rect.width(), self.ITEM_HEIGHT)

    def _get_hit_rects(self, option: QStyleOptionViewItem) -> dict[str, QRect]:
        """Calculate layout rects dynamically based on option.rect"""
        rect = option.rect
        r_top = rect.top()
        r_left = rect.left()
        r_width = rect.width()

        # Checkbox
        checkbox_rect = QRect()
        if self._is_batch_mode:
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

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: Any) -> None:
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)

        is_dark = isDarkTheme()
        rect = option.rect

        data = index.data(Qt.ItemDataRole.UserRole)
        if not isinstance(data, dict):
            painter.restore()
            return

        worker: DownloadWorker | None = data.get("worker")
        if not worker:
            painter.restore()
            return

        title = data.get("title", "")
        thumbnail = data.get("thumbnail", "")
        is_selected = data.get("is_selected", False)

        # Determine State (canonical single source of truth)
        state = worker.effective_state

        status_text = getattr(worker, "status_text", "")
        is_hovered = option.state & QStyle.StateFlag.State_MouseOver

        # --- Draw Background ---
        # Draw card background
        bg_color = QColor(255, 255, 255, 9) if is_dark else QColor(0, 0, 0, 5)
        if is_hovered:
            bg_color = QColor(255, 255, 255, 13) if is_dark else QColor(0, 0, 0, 9)
        # Add selection tint
        if is_selected:
            sel_tint = QColor(255, 255, 255, 20) if is_dark else QColor(0, 0, 0, 20)
            bg_color = sel_tint

        path = QPainterPath()
        path.addRoundedRect(rect.adjusted(8, 4, -8, -4), 8, 8)
        painter.fillPath(path, bg_color)

        # Double-layered Border (Simulating shadow/depth)
        shadow_color = QColor(0, 0, 0, 20) if is_dark else QColor(0, 0, 0, 10)
        painter.strokePath(path, QPen(shadow_color, 2.5))  # Outer soft layer

        border_opacity = 50 if is_hovered else 30
        bd_color = (
            QColor(255, 255, 255, border_opacity) if is_dark else QColor(0, 0, 0, border_opacity)
        )
        painter.strokePath(path, QPen(bd_color, 1.2))  # Inner sharp border

        hit_rects = self._get_hit_rects(option)

        # --- 1. Checkbox ---
        cb_rect = hit_rects["checkbox"]

        # Only draw if we are in batch mode
        if self._is_batch_mode:
            cb_color = QColor(0, 0, 0, 0)
            cb_border = QColor(255, 255, 255, 100) if is_dark else QColor(0, 0, 0, 100)

            if is_selected:
                cb_color = QColor(0, 120, 212) if is_dark else QColor(0, 90, 158)  # Primary color
                cb_border = cb_color

            if self._hovered_row == index.row() and self._hovered_button == "checkbox":
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

        # --- 2. Thumbnail ---
        thumb_left = cb_rect.right() + 16 if self._is_batch_mode else rect.left() + 16
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
            if thumbnail in self._image_cache:
                img = self._image_cache[thumbnail]
                painter.drawImage(thumb_rect, img)
            else:
                # Draw placeholder while loading
                painter.fillRect(
                    thumb_rect, QColor(255, 255, 255, 10) if is_dark else QColor(0, 0, 0, 10)
                )
                # Trigger async load — the view must connect loaded_with_url
                # to feed pixmaps back into our cache via set_pixmap()
                loader = get_image_loader()
                if not hasattr(self, "_pending_urls"):
                    self._pending_urls = set()
                if thumbnail not in self._pending_urls:
                    self._pending_urls.add(thumbnail)
                    loader.load(
                        thumbnail, target_size=(self.THUMB_WIDTH, self.THUMB_HEIGHT), radius=6
                    )
        else:
            painter.fillRect(
                thumb_rect, QColor(255, 255, 255, 10) if is_dark else QColor(0, 0, 0, 10)
            )
        painter.restore()

        # --- 3. Texts & Progress ---
        text_left = thumb_rect.right() + 16
        text_right = hit_rects["pause"].left() - 16
        text_width = max(0, text_right - text_left)

        # Title (Line 1)
        title_font = QFont(painter.font())
        title_font.setPixelSize(14)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(QColor(255, 255, 255) if is_dark else QColor(0, 0, 0))

        fm = painter.fontMetrics()
        elided_title = fm.elidedText(title, Qt.TextElideMode.ElideRight, text_width)
        painter.drawText(
            QRect(text_left, rect.top() + 16, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            elided_title,
        )

        # --- Progress Bar (Single) ---
        progress = getattr(worker, "progress_val", 0.0)
        bar_y = rect.top() + 46
        self._draw_bar(painter, text_left, bar_y, text_width, 6, progress, state, is_dark)

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
        if state == "running":
            if status_text:
                # 统一使用 CleanLogger 精心调配的全能字符串
                meta_str = status_text
            elif progress > 0:
                meta_str = f"下载: {progress:.1f}%"
            else:
                meta_str = "准备下载..."
        elif state == "completed":
            meta_str = "下载完成"
        elif state == "error":
            meta_str = "下载失败"
        elif state == "queued":
            meta_str = "等待下载..."
        elif state == "paused":
            meta_str = "已暂停"

        painter.drawText(
            QRect(text_left, rect.top() + 66, text_width, 20),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            meta_str,
        )

        # --- 5. Buttons ---
        def draw_button(name: str, rect: QRect, icon_enum: Any, hidden: bool = False):
            if hidden:
                return
            is_btn_hovered = self._hovered_row == index.row() and self._hovered_button == name

            # Hover BG
            if is_btn_hovered:
                hover_color = QColor(255, 255, 255, 15) if is_dark else QColor(0, 0, 0, 10)
                btn_path = QPainterPath()
                btn_path.addRoundedRect(rect, 4, 4)
                painter.fillPath(btn_path, hover_color)

            # Draw Icon (使用缓存，避免每帧 icon.pixmap() 热路径)
            theme = Theme.DARK if is_dark else Theme.LIGHT
            cache_key = f"{icon_enum}_{theme}"
            icon_pixmap = self._icon_cache.get(cache_key)
            if icon_pixmap is None:
                icon_pixmap = icon_enum.icon(theme=theme).pixmap(16, 16)
                self._icon_cache[cache_key] = icon_pixmap

            icon_x = rect.left() + (rect.width() - 16) // 2
            icon_y = rect.top() + (rect.height() - 16) // 2
            painter.drawPixmap(icon_x, icon_y, 16, 16, icon_pixmap)

        # Folder button (always show if output_path exists or completed)
        draw_button("folder", hit_rects["folder"], qfw.FluentIcon.FOLDER)

        # Delete button (always show)
        draw_button("delete", hit_rects["delete"], qfw.FluentIcon.DELETE)

        # Pause/Resume button
        icon_enum = qfw.FluentIcon.PLAY
        if state == "running" or state == "queued":
            icon_enum = qfw.FluentIcon.PAUSE
        draw_button("pause", hit_rects["pause"], icon_enum)

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

            fill_color = color or (QColor(0, 120, 212) if is_dark else QColor(0, 90, 158))
            if state == "completed":
                fill_color = QColor(16, 124, 16)
            elif state == "paused":
                fill_color = QColor(150, 150, 150)

            painter.fillPath(fill_path, fill_color)

    def editorEvent(
        self, event: QEvent, model: Any, option: QStyleOptionViewItem, index: Any
    ) -> bool:  # type: ignore[override]
        if not index.isValid():
            return False

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
                # 局部重绘：通过 View 层直接刷新，不穿透 Model
                view = option.widget
                if view and hasattr(view, "viewport"):
                    view.viewport().update(option.rect)
            return True

        elif event.type() == QEvent.Type.Leave:
            if self._hovered_row != -1:
                self._hovered_row = -1
                self._hovered_button = ""
                view = option.widget
                if view and hasattr(view, "viewport"):
                    view.viewport().update()
            return True

        elif event.type() == QEvent.Type.MouseButtonPress:
            pos = event.pos()
            hit_rects = self._get_hit_rects(option)
            if hit_rects["checkbox"].contains(pos):
                self.selection_toggled.emit(index.row())
                return True
            elif hit_rects["pause"].contains(pos):
                self.pause_resume_clicked.emit(index.row())
                return True
            elif hit_rects["folder"].contains(pos):
                self.open_folder_clicked.emit(index.row())
                return True
            elif hit_rects["delete"].contains(pos):
                self.delete_clicked.emit(index.row())
                return True

        return super().editorEvent(event, model, option, index)
