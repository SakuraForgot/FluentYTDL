from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QWidget
from qfluentwidgets import CaptionLabel, PushButton, SettingCard

from fluentytdl.ui.components.dialogs.validated_edit_dialog import (
    Fixer,
    ValidatedEditDialog,
    Validator,
)
from fluentytdl.utils.ui_text import tr_text

from ....core.config_manager import config_manager


class SmartSettingCard(SettingCard):
    """点击后弹出编辑弹窗的设置卡片。

    - 主界面仅展示灰色缩略值（CaptionLabel）
    - 点击“编辑”后弹窗编辑（支持校验/清洗）
    """

    valueChanged = Signal(str)

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        config_key: str,
        parent: QWidget | None = None,
        validator: Validator | None = None,
        fixer: Fixer | None = None,
        prefer_multiline: bool = False,
        empty_text: str | None = None,
        dialog_content: str | None = None,
        pick_file: bool = False,
        file_filter: str | None = None,
    ) -> None:
        super().__init__(icon, title, content, parent)

        if empty_text is None:
            empty_text = self.tr("未设置")

        self._config_key = config_key
        self._validator = validator
        self._fixer = fixer
        self._prefer_multiline = prefer_multiline
        self._empty_text = empty_text
        self._dialog_content = dialog_content
        self._pick_file = pick_file
        self._file_filter = file_filter

        self.valueLabel = CaptionLabel("", self)
        self.valueLabel.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        # Button text: self.tr('选择文件') for file-picker cards, otherwise self.tr('编辑')
        btn_text = self.tr("选择文件") if self._pick_file else self.tr("编辑")
        self.editBtn = PushButton(btn_text, self)
        self.editBtn.setFixedWidth(84)
        self.editBtn.clicked.connect(self._show_edit_dialog)

        self.hBoxLayout.addWidget(self.valueLabel, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(10)
        self.hBoxLayout.addWidget(self.editBtn, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        self.setValue(self._get_value())

    def _get_value(self) -> str:
        return str(config_manager.get(self._config_key) or "")

    def _set_value(self, value: str) -> None:
        config_manager.set(self._config_key, value)

    def setValue(self, value: str) -> None:
        self._update_display(value)

    def _update_display(self, text: str) -> None:
        text = str(text or "")
        if not text:
            self.valueLabel.setText(self._empty_text)
            return

        if len(text) > 48:
            display = f"{text[:24]} … {text[-16:]}"
        else:
            display = text
        self.valueLabel.setText(display)

    def _show_edit_dialog(self) -> None:
        current = self._get_value()
        if self._pick_file:
            # Open file picker; use provided filter if any
            start_dir = str(Path(current).parent) if current else str(Path.home())
            filt = self._file_filter or "All Files (*)"
            path, _ = QFileDialog.getOpenFileName(
                self.window(), tr_text("选择 {0}", self.titleLabel.text()), start_dir, filt
            )
            if not path:
                return
            candidate = path
            if self._fixer:
                try:
                    candidate = self._fixer(candidate)
                except Exception:
                    pass
            if self._validator:
                ok, msg = self._validator(candidate)
                if not ok:
                    # Show a simple rejection Info (do not import qfluentwidgets here to avoid cycles)
                    from qfluentwidgets import InfoBar

                    InfoBar.error(self.window(), self.tr("无效的文件"), msg)
                    return
            self._set_value(candidate)
            self._update_display(candidate)
            self.valueChanged.emit(candidate)
            return

        dialog = ValidatedEditDialog(
            title=tr_text("编辑 {0}", self.titleLabel.text()),
            content=self._dialog_content or self.tr("请输入新的值，系统将自动进行格式检查。"),
            initial_value=current,
            validator=self._validator,
            fixer=self._fixer,
            parent=self.window(),
            prefer_multiline=self._prefer_multiline,
        )

        if dialog.exec():
            new_value = dialog.final_value
            self._set_value(new_value)
            self._update_display(new_value)
            self.valueChanged.emit(new_value)
