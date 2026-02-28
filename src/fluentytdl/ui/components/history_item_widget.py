"""
历史记录卡片组件

轻量展示：缩略图 + 标题 + 文件信息 + 操作按钮
"""

from __future__ import annotations

import os
import subprocess

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPixmap
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ...storage.history_service import HistoryRecord
from ...utils.image_loader import ImageLoader


def _format_bytes(b: int | float) -> str:
    size = float(b)
    if size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _format_time_ago(ts: float) -> str:
    """时间戳 → '3 分钟前' 之类"""
    import time

    diff = time.time() - ts
    if diff < 60:
        return "刚刚"
    elif diff < 3600:
        return f"{int(diff // 60)} 分钟前"
    elif diff < 86400:
        return f"{int(diff // 3600)} 小时前"
    else:
        days = int(diff // 86400)
        if days == 1:
            return "昨天"
        elif days < 30:
            return f"{days} 天前"
        else:
            from datetime import datetime

            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class HistoryItemWidget(CardWidget):
    """
    单条历史记录卡片

    布局:
    [缩略图] [标题]                          [操作按钮]
             [文件信息: 大小 · 格式 · 时间]
    """

    remove_requested = Signal(object)  # 请求从历史删除
    play_requested = Signal(object)  # 请求播放

    def __init__(self, record: HistoryRecord, parent: QWidget | None = None):
        super().__init__(parent)
        self.record = record

        self.image_loader = ImageLoader(self)
        self.image_loader.loaded.connect(self._on_thumb_loaded)

        self.setFixedHeight(88)

        # --- 主布局 ---
        h = QHBoxLayout(self)
        h.setContentsMargins(12, 10, 12, 10)
        h.setSpacing(14)

        # 1) 缩略图 128×72
        self.thumb = QLabel(self)
        self.thumb.setFixedSize(128, 72)
        self.thumb.setScaledContents(True)
        # Style setup moved to _update_style
        h.addWidget(self.thumb)

        # 2) 信息区
        info = QVBoxLayout()
        info.setSpacing(4)
        info.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        # 标题
        self.title_label = StrongBodyLabel(record.title or "未知标题", self)
        self.title_label.setWordWrap(False)

        # 文件信息行
        meta_parts: list[str] = []
        if record.file_size > 0:
            meta_parts.append(_format_bytes(record.file_size))
        if record.format_note:
            meta_parts.append(record.format_note)
        meta_parts.append(_format_time_ago(record.download_time))

        # 文件状态
        if not record.file_exists:
            meta_parts.append("⚠ 文件丢失")

        self.meta_label = CaptionLabel(" · ".join(meta_parts), self)
        self.meta_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
        font = self.meta_label.font()
        font.setFamily("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.meta_label.setFont(font)

        info.addWidget(self.title_label)
        info.addWidget(self.meta_label)
        h.addLayout(info, 1)

        # 3) 操作按钮
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        # 打开文件夹
        self.folder_btn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.folder_btn.setToolTip("打开文件位置")
        self.folder_btn.installEventFilter(
            ToolTipFilter(self.folder_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.folder_btn.setEnabled(record.file_exists)
        self.folder_btn.clicked.connect(self._open_location)

        # 播放按钮
        self.play_btn = TransparentToolButton(FluentIcon.PLAY, self)
        self.play_btn.setToolTip("播放文件")
        self.play_btn.installEventFilter(
            ToolTipFilter(self.play_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.play_btn.setEnabled(record.file_exists)
        self.play_btn.clicked.connect(self._play_file)

        # 删除记录
        self.del_btn = TransparentToolButton(FluentIcon.DELETE, self)
        self.del_btn.setToolTip("删除记录")
        self.del_btn.installEventFilter(
            ToolTipFilter(self.del_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.del_btn.clicked.connect(lambda: self.remove_requested.emit(self))

        btn_layout.addWidget(self.play_btn)
        btn_layout.addWidget(self.folder_btn)
        btn_layout.addWidget(self.del_btn)
        h.addLayout(btn_layout)

        # 文件丢失时整体降低不透明度
        if not record.file_exists:
            self.setStyleSheet("QWidget { opacity: 0.55; }")
            self.title_label.setTextColor(QColor(160, 160, 160), QColor(100, 100, 100))

        # 加载缩略图
        if record.thumbnail_url:
            self.image_loader.load(record.thumbnail_url)

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        bg = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "rgba(0, 0, 0, 0.03)"
        bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.thumb.setStyleSheet(f"background: {bg}; border-radius: 6px; border: 1px solid {bd};")

    def _on_thumb_loaded(self, pixmap: QPixmap) -> None:
        if pixmap and not pixmap.isNull():
            self.thumb.setPixmap(pixmap)

    def _open_location(self) -> None:
        p = self.record.output_path
        if not p or not os.path.exists(p):
            return
        try:
            if os.name == "nt":
                subprocess.Popen(f'explorer /select,"{os.path.normpath(p)}"')
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(p)])
        except Exception:
            pass

    def _play_file(self) -> None:
        p = self.record.output_path
        if not p or not os.path.exists(p):
            return
        try:
            os.startfile(p)  # type: ignore[attr-defined]  # Windows only
        except Exception:
            try:
                subprocess.Popen(["xdg-open", p])
            except Exception:
                pass

    def refresh_status(self) -> None:
        """重新检查文件存在性并更新 UI"""
        exists = bool(self.record.output_path and os.path.exists(self.record.output_path))
        self.record.file_exists = exists
        self.folder_btn.setEnabled(exists)
        self.play_btn.setEnabled(exists)
