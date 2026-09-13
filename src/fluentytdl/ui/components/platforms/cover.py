"""
FluentYTDL 封面选择器组件

显示视频所有可用封面（缩略图）列表，允许用户选择特定尺寸/格式下载。
"""

from __future__ import annotations

import urllib.request
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ImageLabel,
    TableWidget,
)

from ....utils.image_loader import ImageLoader


class UrlValidatorThread(QThread):
    resultReady = Signal(int, bool)  # row_index, is_valid

    def __init__(self, row: int, url: str, parent=None):
        super().__init__(parent)
        self.row = row
        self.url = url

    def run(self):
        try:
            req = urllib.request.Request(self.url, method="HEAD")
            req.add_header("User-Agent", "Mozilla/5.0")
            with urllib.request.urlopen(req, timeout=5) as resp:
                is_valid = resp.status == 200
                if is_valid and resp.headers.get("Content-Length"):
                    if (
                        int(resp.headers.get("Content-Length")) < 5120
                    ):  # < 5KB is usually the default 120x90 image
                        is_valid = False
            self.resultReady.emit(self.row, is_valid)
        except Exception:
            self.resultReady.emit(self.row, False)


class CoverSelectorWidget(QFrame):
    """
    封面选择器组件

    解析并显示所有可用缩略图，提供预览和选择功能。
    """

    # 选中项变更信号
    selectionChanged = Signal()

    def __init__(self, info: dict[str, Any], parent: QWidget | None = None):
        super().__init__(parent)
        self.info = info
        self._thumbnails: list[dict[str, Any]] = []
        self._selected_url: str | None = None
        self._selected_ext: str = "jpg"
        self._validators = []

        self._init_data()

        # Initialize image loader BEFORE UI setup because _init_ui calls selectRow which triggers loading
        self.image_loader = ImageLoader(self)
        self.image_loader.loaded_with_url.connect(self._on_image_loaded)

        self._init_ui()

    def _init_data(self):
        """解析缩略图数据"""
        raw_thumbs = self.info.get("thumbnails", [])
        if not raw_thumbs:
            # Fallback if no thumbnails list (rare)
            thumb = self.info.get("thumbnail")
            if thumb:
                raw_thumbs = [{"url": thumb, "id": "default", "width": 0, "height": 0}]

        # 整理数据
        processed = []
        for t in raw_thumbs:
            url = t.get("url")
            if not url:
                continue

            width = t.get("width") or 0
            height = t.get("height") or 0
            res = f"{width}x{height}" if width and height else self.tr("未知")
            t_id = t.get("id") or "unknown"

            # 尝试从 URL 推断格式
            ext = "jpg"
            if ".webp" in url:
                ext = "webp"
            elif ".png" in url:
                ext = "png"

            # 估算清晰度分数 (用于排序)
            score = (width or 0) * (height or 0)
            if "maxres" in t_id:
                score += 10000000

            processed.append(
                {
                    "url": url,
                    "res": res,
                    "width": width,
                    "height": height,
                    "id": t_id,
                    "ext": ext,
                    "score": score,
                    "preference": t.get("preference", 0),
                }
            )

        # 按清晰度降序排序
        processed.sort(key=lambda x: (x["score"], x["preference"]), reverse=True)
        self._thumbnails = processed

        if self._thumbnails:
            self._selected_url = self._thumbnails[0]["url"]

    def _init_ui(self):
        self.setObjectName("coverSelector")
        from qfluentwidgets import isDarkTheme

        bg = "rgba(255, 255, 255, 0.05)" if isDarkTheme() else "rgba(255, 255, 255, 0.7)"
        bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.08)"
        self.setStyleSheet(f"""
            #coverSelector {{
                background-color: {bg};
                border: 1px solid {bd};
                border-radius: 8px;
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(12)

        # 标题及提示
        self.titleLabel = BodyLabel(self.tr("🖼️ 封面选择"), self)
        font = self.titleLabel.font()
        font.setBold(True)
        self.titleLabel.setFont(font)
        layout.addWidget(self.titleLabel)

        self.tipLabel = CaptionLabel(
            self.tr(
                "提示：封面尺寸可能来自元数据，实际像素尺寸以下载的图片为准。⚠️ 标记的版本尚未确认可用。"
            ),
            self,
        )
        self.tipLabel.setStyleSheet("color: #888888;")
        layout.addWidget(self.tipLabel)

        # 主内容区：左侧表格，右侧预览
        contentLayout = QHBoxLayout()
        contentLayout.setSpacing(16)

        # 左侧表格
        self.table = TableWidget(self)
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels([self.tr("分辨率"), "ID", self.tr("格式")])
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        # 填充表格
        self.table.setRowCount(len(self._thumbnails))
        for i, t in enumerate(self._thumbnails):
            # 分辨率
            res_str = t["res"]
            if t["width"] and t["height"]:
                res_str += self.tr(" (标称)")

            res_item = QTableWidgetItem(res_str)
            res_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 0, res_item)

            # ID
            id_str = t["id"]
            if "maxres" in id_str:
                id_str += " ⚠️"
            id_item = QTableWidgetItem(id_str)
            if "maxres" in id_str:
                id_item.setToolTip(self.tr("最高画质封面可能不存在，若下载报错请选择其他版本"))
            self.table.setItem(i, 1, id_item)

            # 格式
            ext_item = QTableWidgetItem(t["ext"].upper())
            ext_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(i, 2, ext_item)

            self.table.item(i, 0).setData(Qt.ItemDataRole.UserRole, t["url"])

            # Start validation for maxresdefault or any others
            if "maxres" in t["id"]:
                validator = UrlValidatorThread(i, t["url"], self)
                validator.resultReady.connect(self._on_url_validation_result)
                self._validators.append(validator)
                validator.start()

        contentLayout.addWidget(self.table, stretch=1)

        # 右侧预览区
        previewContainer = QFrame(self)
        previewContainer.setFixedWidth(240)
        previewContainer.setStyleSheet("background-color: rgba(0,0,0,0.03); border-radius: 8px;")
        previewLayout = QVBoxLayout(previewContainer)

        self.previewLabel = ImageLabel(previewContainer)
        self.previewLabel.setFixedSize(220, 124)  # 16:9 ratio approx
        self.previewLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.previewInfo = CaptionLabel(self.tr("预览加载中..."), previewContainer)
        self.previewInfo.setWordWrap(True)
        self.previewInfo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        previewLayout.addWidget(self.previewLabel, alignment=Qt.AlignmentFlag.AlignHCenter)
        previewLayout.addWidget(self.previewInfo, alignment=Qt.AlignmentFlag.AlignHCenter)
        previewLayout.addStretch()

        contentLayout.addWidget(previewContainer)

        layout.addLayout(contentLayout)

        # 选中第一行 (UI 初始化完成后再触发选择)
        if self._thumbnails:
            self.table.selectRow(0)

    def _on_selection_changed(self):
        rows = self.table.selectedItems()
        if not rows:
            return

        row = rows[0].row()
        if 0 <= row < len(self._thumbnails):
            thumb = self._thumbnails[row]
            self._selected_url = thumb["url"]
            self._selected_ext = thumb["ext"]

            # 更新预览信息
            self.previewInfo.setText(f"{thumb['res']} • {thumb['ext'].upper()}\n{thumb['id']}")

            # 加载图片
            self._load_preview(thumb["url"])

            self.selectionChanged.emit()

    def _load_preview(self, url: str):
        # 使用 ImageLoader 异步加载
        self.image_loader.load(url, allow_webp=True)

    def _on_image_loaded(self, url: str, pixmap: QPixmap):
        # Only update if it matches current selection
        if url != self._selected_url:
            return

        if pixmap and not pixmap.isNull():
            # Scale to fit fixed bounding box
            scaled = pixmap.scaled(
                220,
                124,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self.previewLabel.setPixmap(scaled)
        else:
            self.previewLabel.clear()

    def _on_url_validation_result(self, row: int, is_valid: bool):
        if row >= self.table.rowCount():
            return

        id_item = self.table.item(row, 1)
        if not id_item:
            return

        text = id_item.text().replace(" ⚠️", "")
        if is_valid:
            id_item.setText(text + self.tr(" (真实可用)"))
            id_item.setToolTip(self.tr("该分辨率版本经检测实际存在"))
        else:
            id_item.setText(text + " ❌ 404")
            id_item.setToolTip(self.tr("该版本由于 YouTube 服务端未提供导致 404 错误"))

            # Change text color to red or gray to indicate it's invalid
            from PySide6.QtGui import QColor

            font = id_item.font()
            font.setStrikeOut(True)
            id_item.setFont(font)

            for col in range(3):
                item = self.table.item(row, col)
                if item:
                    item.setForeground(QColor(150, 150, 150))

            # Auto fallback selection if this was selected natively
            if self.table.currentRow() == row:
                for r in range(self.table.rowCount()):
                    if r != row and "❌" not in (
                        self.table.item(r, 1).text() if self.table.item(r, 1) else ""
                    ):
                        self.table.selectRow(r)
                        break

    def get_selected_url(self) -> str | None:
        return self._selected_url

    def get_selected_ext(self) -> str:
        return self._selected_ext
