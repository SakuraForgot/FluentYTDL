from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import QCoreApplication, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import QFileDialog, QStackedWidget, QVBoxLayout, QWidget
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    FluentIcon,
    HyperlinkCard,
    LineEdit,
    MessageBox,
    ProgressBar,
    PushButton,
    PushSettingCard,
    ScrollArea,
    SegmentedWidget,
    SettingCard,
    SettingCardGroup,
    SpinBox,
    SubtitleLabel,
    SwitchButton,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
)

from fluentytdl.ui.components.common.custom_info_bar import InfoBar
from fluentytdl.ui.components.settings.app_update_card import AppUpdateSettingCard
from fluentytdl.ui.components.settings.smart_setting_card import SmartSettingCard

from ..core.config_manager import config_manager
from ..core.dependency_manager import dependency_manager
from ..core.hardware_manager import hardware_manager
from ..download.download_manager import download_manager
from ..processing.subtitle_manager import COMMON_SUBTITLE_LANGUAGES
from ..utils.logger import LOG_DIR, logger
from ..utils.paths import find_bundled_executable, is_frozen
from ..youtube.yt_dlp_cli import resolve_yt_dlp_exe, run_version

# ============================================================================
# Cookie 刷新 Worker（使用Qt线程，确保打包后正常工作）
# ============================================================================


class CookieRefreshWorker(QThread):
    """Cookie刷新工作线程（Qt线程，打包后可靠）"""

    finished = Signal(bool, str, bool)  # (成功标志, 消息, 是否需要管理员权限)

    def __init__(self, parent=None, platform: str | None = None):
        super().__init__(parent)
        self.platform = platform

    def run(self):
        """在Qt线程中执行Cookie刷新"""
        from ..auth.auth_service import auth_service
        from ..auth.cookie_sentinel import cookie_sentinel
        from ..utils.logger import logger

        success = False
        message = "未知错误"

        try:
            # 直接刷新（调用前已检查权限，或已是管理员/非Edge/Chrome）
            success, message = cookie_sentinel.force_refresh_with_uac(platform=self.platform)

            if not success:
                # 获取详细状态
                status = auth_service.last_status
                if status and hasattr(status, "message") and status.message:
                    message = status.message

                # 友好的错误引导
                browser_name = auth_service.current_source_display

                # 如果 auth_service 已经提供了关于【提取解密失败】的详细多行指引，则保留其内容
                # 否则，如果是其他诸如“未找到文件”或普通的异常，才覆盖为通用建议
                if "【提取解密失败】" not in message and (
                    "未找到" in message or "not found" in message.lower()
                ):
                    message = (
                        f"无法从 {browser_name} 提取 Cookie\n\n"
                        + self.tr("可能的原因：\n")
                        + f"1. {browser_name} 未安装或未登录相关平台\n"
                        f"2. {browser_name} Cookie 数据库被锁定（请关闭浏览器）\n\n"
                        + self.tr("建议：完全关闭浏览器后重试")
                    )

                logger.warning(f"[CookieRefreshWorker] 提取失败: {message}")
        except Exception as e:
            success = False
            message = f"刷新异常: {str(e)}"
            logger.exception("[CookieRefreshWorker] 异常")

        # 发射信号（线程安全，第三个参数保留但不再使用）
        self.finished.emit(success, message, False)


class ComponentSettingCard(SettingCard):
    """Card for managing an external component (check update, install)."""

    def __init__(
        self,
        component_key: str,
        icon: FluentIcon,
        title: str,
        content: str,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.component_key = component_key

        # UI Elements
        self.progressBar = ProgressBar(self)
        self.progressBar.setRange(0, 100)
        self.progressBar.setValue(0)
        self.progressBar.setFixedWidth(120)
        self.progressBar.setVisible(False)

        self.actionButton = PushButton(self.tr("检查更新"), self)
        self.actionButton.clicked.connect(self._on_action_clicked)

        self.importButton = PushButton(self.tr("手动导入"), self, FluentIcon.ADD)
        self.importButton.setToolTip(self.tr("选择本地文件覆盖当前组件"))
        self.importButton.installEventFilter(
            ToolTipFilter(self.importButton, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.importButton.clicked.connect(self._on_import_clicked)

        self.folderButton = ToolButton(FluentIcon.FOLDER, self)
        self.folderButton.setToolTip(self.tr("打开所在文件夹"))
        self.folderButton.installEventFilter(
            ToolTipFilter(self.folderButton, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.folderButton.clicked.connect(self._open_folder)

        # Layout
        self.hBoxLayout.addWidget(self.progressBar, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(10)
        self.hBoxLayout.addWidget(self.actionButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(5)
        self.hBoxLayout.addWidget(self.importButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(5)
        self.hBoxLayout.addWidget(self.folderButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # Connect signals
        dependency_manager.check_started.connect(self._on_check_started)
        dependency_manager.check_finished.connect(self._on_check_finished)
        dependency_manager.check_error.connect(self._on_error)

        dependency_manager.download_started.connect(self._on_download_started)
        dependency_manager.download_progress.connect(self._on_download_progress)
        dependency_manager.download_finished.connect(self._on_download_finished)
        dependency_manager.download_error.connect(self._on_error)
        dependency_manager.install_finished.connect(self._on_install_finished)

    def _on_action_clicked(self):
        text = self.actionButton.text()
        if text == self.tr("检查更新"):
            dependency_manager.check_update(self.component_key)
        elif text in (self.tr("立即更新"), self.tr("立即安装")):
            dependency_manager.install_component(self.component_key)

    def _on_import_clicked(self):
        # Filter based on component type
        exe_name = "yt-dlp.exe"
        if self.component_key == "ffmpeg":
            exe_name = "ffmpeg.exe"
        elif self.component_key == "deno":
            exe_name = "deno.exe"
        elif self.component_key == "pot-provider":
            exe_name = "bgutil-pot-provider.exe"
        elif self.component_key == "atomicparsley":
            exe_name = "AtomicParsley.exe"

        file, _ = QFileDialog.getOpenFileName(
            self.window(), f"选择 {exe_name}", "", f"Executables ({exe_name});;All Files (*)"
        )

        if not file:
            return

        try:
            src = Path(file)
            if not src.exists():
                return

            target_dir = dependency_manager.get_target_dir(self.component_key)
            target_path = target_dir / exe_name

            # Simple check
            if src.stat().st_size == 0:
                InfoBar.error(self.tr("错误"), self.tr("所选文件为空"), parent=self.window())
                InfoBar.error(self.tr("错误"), self.tr("所选文件为空"), parent=self.window())
                return

            shutil.copy2(src, target_path)

            InfoBar.info(
                self.tr("导入成功"), self.tr("已手动导入 {}").format(exe_name), parent=self.window()
            )
            # Refresh version info
            dependency_manager.check_update(self.component_key)

        except Exception as e:
            InfoBar.error(self.tr("导入失败"), str(e), parent=self.window())

    def _open_folder(self):
        try:
            path = dependency_manager.get_target_dir(self.component_key)
            if path.exists():
                import os

                os.startfile(path)
            else:
                InfoBar.warning(
                    self.tr("目录不存在"), self.tr("{} 尚未创建").format(path), parent=self.window()
                )
        except Exception as e:
            InfoBar.error(self.tr("错误"), str(e), parent=self.window())

    def _on_check_started(self, key):
        if key != self.component_key:
            return
        self.actionButton.setText(self.tr("正在检查..."))
        self.actionButton.setEnabled(False)

    def _on_check_finished(self, key, result):
        if key != self.component_key:
            return
        self.actionButton.setEnabled(True)

        curr = result.get("current", "unknown")
        latest = result.get("latest", "unknown")
        has_update = result.get("update_available", False)

        self.setContent(self.tr("当前: {}  |  最新: {}").format(curr, latest))

        title_text = self.titleLabel.text()

        if has_update:
            self.actionButton.setText(self.tr("立即更新"))
            InfoBar.info(
                self.tr("发现新版本: {}").format(title_text),
                self.tr("版本 {} 可用 (当前: {})").format(latest, curr),
                duration=15000,
                parent=self.window(),
            )
        else:
            if latest == "unknown":
                self.actionButton.setText(self.tr("检查更新"))
                InfoBar.error(
                    self.tr("检查失败"),
                    self.tr("无法获取 {} 的最新版本信息，请检查网络连接或更换镜像源。").format(
                        title_text
                    ),
                    duration=5000,
                    parent=self.window(),
                )
            elif curr == "unknown":
                self.actionButton.setText(self.tr("立即安装"))
            else:
                self.actionButton.setText(self.tr("检查更新"))
                InfoBar.info(
                    self.tr("已是最新"),
                    self.tr("{} 当前版本 {} 已是最新。").format(title_text, curr),
                    duration=5000,
                    parent=self.window(),
                )

    def _on_download_started(self, key):
        if key != self.component_key:
            return
        self.progressBar.setVisible(True)
        self.progressBar.setValue(0)
        self.actionButton.setEnabled(False)
        self.actionButton.setText(self.tr("正在下载..."))

    def _on_download_progress(self, key, percent):
        if key != self.component_key:
            return
        self.progressBar.setValue(percent)

    def _on_download_finished(self, key):
        if key != self.component_key:
            return
        self.actionButton.setText(self.tr("正在安装..."))

    def _on_install_finished(self, key):
        if key != self.component_key:
            return
        self.progressBar.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("检查更新"))
        # Trigger a re-check to update version text
        dependency_manager.check_update(self.component_key)

        title_text = self.titleLabel.text()
        InfoBar.info(
            self.tr("安装完成"),
            self.tr("{} 已成功安装/更新。").format(title_text),
            duration=5000,
            parent=self.window(),
        )

    def _on_error(self, key, msg):
        if key != self.component_key:
            return
        self.progressBar.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText(self.tr("检查更新"))  # Reset

        title_text = self.titleLabel.text()
        InfoBar.error(
            self.tr("{} 错误").format(title_text), msg, duration=15000, parent=self.window()
        )


class InlineComboBoxCard(SettingCard):
    """A fluent setting card with a right-aligned ComboBox.

    We intentionally avoid QFluentWidgets' ComboBoxSettingCard because it is
    tightly coupled to qconfig persistence.
    """

    def __init__(self, icon, title: str, content: str | None, texts: list[str], parent=None):
        super().__init__(icon, title, content, parent)
        self.comboBox = ComboBox(self)
        self.hBoxLayout.addWidget(self.comboBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        for text in texts:
            self.comboBox.addItem(text)


class InlineSpinBoxCard(SettingCard):
    """A fluent setting card with a right-aligned SpinBox."""

    def __init__(
        self, icon, title: str, content: str | None, min_val: int, max_val: int, parent=None
    ):
        super().__init__(icon, title, content, parent)
        self.spinBox = SpinBox(self)
        self.spinBox.setRange(min_val, max_val)
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)


class InlineLineEditCard(SettingCard):
    """A fluent setting card with a right-aligned LineEdit."""

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        placeholder: str | None = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        if placeholder is not None:
            self.lineEdit.setPlaceholderText(placeholder)
        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)


class LanguageSelectionDialog(MessageBox):
    """语言多选对话框"""

    def __init__(self, languages: list[tuple[str, str]], selected: list[str], parent=None):
        super().__init__(self.tr("选择字幕语言"), "", parent)

        self.languages = languages
        self.selected_languages = selected.copy() if selected else []
        self.checkboxes = {}

        # 创建内容布局
        from PySide6.QtWidgets import QFrame, QGridLayout, QVBoxLayout, QWidget
        from qfluentwidgets import SmoothScrollArea

        content_widget = QWidget(self)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # 添加说明
        hint_label = SubtitleLabel(self.tr("请选择要下载的字幕语言（可多选）："), content_widget)
        content_layout.addWidget(hint_label)
        content_layout.addSpacing(12)

        # 创建复选框容器
        checkbox_container = QFrame(content_widget)
        checkbox_layout = QGridLayout(checkbox_container)
        checkbox_layout.setContentsMargins(8, 8, 8, 8)
        checkbox_layout.setSpacing(12)

        # 创建复选框（2列网格，更易读）
        row = 0
        col = 0
        for code, name in languages:
            from PySide6.QtCore import QCoreApplication

            display_name = QCoreApplication.translate("Subtitle", name)
            checkbox = CheckBox(f"{display_name} ({code})", checkbox_container)
            checkbox.setChecked(code in self.selected_languages)
            checkbox.setMinimumWidth(280)  # 确保复选框有足够宽度显示完整文本
            checkbox_layout.addWidget(checkbox, row, col)
            self.checkboxes[code] = checkbox

            col += 1
            if col >= 2:  # 2列布局
                col = 0
                row += 1

        # 设置列宽度均匀分布
        checkbox_layout.setColumnStretch(0, 1)
        checkbox_layout.setColumnStretch(1, 1)

        # 添加滚动区域
        scroll = SmoothScrollArea(content_widget)
        checkbox_container.setObjectName("checkboxContainer")
        scroll.setObjectName("scrollArea")
        scroll.setStyleSheet(
            "QScrollArea#scrollArea { background-color: transparent; border: none; } QWidget#checkboxContainer { background-color: transparent; }"
        )
        scroll.setWidget(checkbox_container)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(250)
        scroll.setMaximumHeight(400)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content_layout.addWidget(scroll)

        # 将内容添加到对话框
        self.textLayout.addWidget(content_widget)

        # 设置对话框大小（更宽以容纳2列布局）
        self.widget.setMinimumWidth(700)
        self.widget.setMaximumWidth(800)

    def get_selected_languages(self) -> list[str]:
        """获取选中的语言代码列表"""
        return [code for code, checkbox in self.checkboxes.items() if checkbox.isChecked()]


class LanguageMultiSelectCard(SettingCard):
    """语言多选卡片 - 按钮弹出对话框"""

    selectionChanged = Signal(list)  # 选中语言列表变化信号

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        languages: list[tuple[str, str]],  # [(code, name), ...]
        selected_default: list[str] | None = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        self.languages = languages
        self.selected_languages = selected_default if selected_default else []

        # 创建按钮显示当前选择
        self.selectButton = PushButton(self.tr("选择语言"), self)
        self.selectButton.clicked.connect(self._show_language_dialog)
        self.hBoxLayout.addWidget(self.selectButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 更新按钮文本
        self._update_button_text()

    def _update_button_text(self):
        """更新按钮显示文本"""
        if not self.selected_languages:
            self.selectButton.setText(self.tr("选择语言"))
        else:
            # 显示选中的语言名称
            names = []
            for code in self.selected_languages[:3]:  # 最多显示3个
                name = next((n for c, n in self.languages if c == code), code)
                from PySide6.QtCore import QCoreApplication

                name = QCoreApplication.translate("Subtitle", name)
                names.append(name)

            text = ", ".join(names)
            if len(self.selected_languages) > 3:
                text += self.tr(" 等 {} 种语言").format(len(self.selected_languages))
            self.selectButton.setText(text)

    def _show_language_dialog(self):
        """显示语言选择对话框"""
        dialog = LanguageSelectionDialog(self.languages, self.selected_languages, self.window())
        if dialog.exec():
            # 用户点击确定
            new_selection = dialog.get_selected_languages()
            if new_selection != self.selected_languages:
                self.selected_languages = new_selection
                self._update_button_text()
                self.selectionChanged.emit(self.selected_languages)

    def get_selected_languages(self) -> list[str]:
        """获取选中的语言代码列表"""
        return self.selected_languages.copy()

    def set_selected_languages(self, codes: list[str]):
        """设置选中的语言"""
        self.selected_languages = codes.copy() if codes else []
        self._update_button_text()


from PySide6.QtWidgets import QAbstractItemView, QListWidgetItem  # noqa: E402


class AudioLanguageSelectionDialog(MessageBox):
    """音频备选语言提取对话框 (支持排序列表)"""

    def __init__(self, languages: list[tuple[str, str]], selected: list[str], parent=None):
        super().__init__(
            self.tr("选择并排序首选音轨语言"),
            self.tr("选中的语言越靠前，优先级越高。可拖拽调整顺序。"),
            parent,
        )
        self.languages = languages
        self.selected_languages_init = selected.copy() if selected else []

        # UI
        from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

        content_widget = QWidget(self)
        layout = QHBoxLayout(content_widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Left list: Available
        left_layout = QVBoxLayout()
        left_layout.setSpacing(12)
        left_layout.addWidget(SubtitleLabel(self.tr("可选语言:"), content_widget))
        from qfluentwidgets import ListWidget

        self.available_list = ListWidget(content_widget)
        self.available_list.setMinimumWidth(240)
        self.available_list.setMinimumHeight(250)
        left_layout.addWidget(self.available_list)
        layout.addLayout(left_layout, stretch=1)

        # Middle Layout: Add/Remove buttons
        mid_layout = QVBoxLayout()
        mid_layout.addStretch(1)
        self.btn_add = PushButton(self.tr("添加 >>"), content_widget)
        self.btn_remove = PushButton(self.tr("<< 移除"), content_widget)
        mid_layout.addWidget(self.btn_add)
        mid_layout.addWidget(self.btn_remove)
        mid_layout.addStretch(1)
        layout.addLayout(mid_layout, stretch=0)

        # Right list: Selected
        right_layout = QVBoxLayout()
        right_layout.setSpacing(12)
        right_layout.addWidget(SubtitleLabel(self.tr("已选排序 (拖拽调整):"), content_widget))
        self.selected_list = ListWidget(content_widget)
        self.selected_list.setMinimumWidth(240)
        self.selected_list.setMinimumHeight(250)
        self.selected_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        right_layout.addWidget(self.selected_list)
        layout.addLayout(right_layout, stretch=1)

        self.textLayout.addWidget(content_widget)
        self.widget.setMinimumWidth(650)
        self.widget.setMinimumHeight(450)

        # Signals
        self.btn_add.clicked.connect(self._on_add)
        self.btn_remove.clicked.connect(self._on_remove)

        # Populate
        self._populate()

    def _populate(self):
        # 建立快速查找表
        lang_dict = {code: name for code, name in self.languages}

        # 填充已选
        for code in self.selected_languages_init:
            name = lang_dict.get(code, code)
            from PySide6.QtCore import QCoreApplication

            display_name = QCoreApplication.translate("Subtitle", name)
            item = QListWidgetItem(f"{display_name} ({code})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.selected_list.addItem(item)

        # 填充备选
        for code, name in self.languages:
            if code not in self.selected_languages_init:
                from PySide6.QtCore import QCoreApplication

                display_name = QCoreApplication.translate("Subtitle", name)
                item = QListWidgetItem(f"{display_name} ({code})")
                item.setData(Qt.ItemDataRole.UserRole, code)
                self.available_list.addItem(item)

    def _on_add(self):
        for item in self.available_list.selectedItems():
            row = self.available_list.row(item)
            self.available_list.takeItem(row)
            self.selected_list.addItem(item)

    def _on_remove(self):
        for item in self.selected_list.selectedItems():
            row = self.selected_list.row(item)
            self.selected_list.takeItem(row)
            self.available_list.addItem(item)

    def get_selected_languages(self) -> list[str]:
        res = []
        for i in range(self.selected_list.count()):
            item = self.selected_list.item(i)
            res.append(item.data(Qt.ItemDataRole.UserRole))
        return res


class AudioLanguageMultiSelectCard(SettingCard):
    """支持顺位排序的音频语言多选卡片"""

    selectionChanged = Signal(list)

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        languages: list[tuple[str, str]],
        selected_default: list[str] | None = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.languages = languages
        self.selected_languages = selected_default if selected_default else []

        self.selectButton = PushButton(self.tr("设置首选音轨..."), self)
        self.selectButton.clicked.connect(self._show_dialog)
        self.hBoxLayout.addWidget(self.selectButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        self._update_button_text()

    def _update_button_text(self):
        if not self.selected_languages:
            self.selectButton.setText(self.tr("选择语言 (未设置)"))
        else:
            names = []
            for code in self.selected_languages[:3]:
                name = next((n for c, n in self.languages if c == code), code)
                from PySide6.QtCore import QCoreApplication

                name = QCoreApplication.translate("Subtitle", name)
                names.append(name)
            text = " > ".join(names)
            if len(self.selected_languages) > 3:
                text += " ..."
            self.selectButton.setText(text)

    def _show_dialog(self):
        dialog = AudioLanguageSelectionDialog(
            self.languages, self.selected_languages, self.window()
        )
        if dialog.exec():
            new_val = dialog.get_selected_languages()
            if new_val != self.selected_languages:
                self.selected_languages = new_val
                self._update_button_text()
                self.selectionChanged.emit(self.selected_languages)

    def set_selected_languages(self, codes: list[str]):
        self.selected_languages = codes.copy() if codes else []
        self._update_button_text()


class WebView2AccountNameDialog(MessageBox):
    """新增 WebView2 账号名称输入对话框（Fluent 风格）。"""

    def __init__(self, parent=None):
        super().__init__(self.tr("新增 WebView2 账号"), self.tr("请输入账号名称"), parent)
        self.nameEdit = LineEdit(self.widget)
        self.nameEdit.setPlaceholderText(self.tr("例如：A 账号"))
        self.nameEdit.setMinimumWidth(360)
        self.textLayout.addWidget(self.nameEdit)
        self.widget.setMinimumWidth(420)

    def get_account_name(self) -> str:
        return (self.nameEdit.text() or "").strip()


class EmbedTypeComboCard(SettingCard):
    """嵌入类型下拉框卡片"""

    valueChanged = Signal(str)  # soft/external

    # 嵌入类型映射
    EMBED_TYPES = [
        (
            "soft",
            QCoreApplication.translate(
                "EmbedTypeComboCard", "软嵌入（推荐） - 封装到容器，可开关，多语言"
            ),
        ),
        (
            "external",
            QCoreApplication.translate(
                "EmbedTypeComboCard", "外置文件 - 独立.srt，易编辑，兼容性最佳"
            ),
        ),
    ]

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        default: str = "soft",
        parent=None,
    ):
        super().__init__(icon, title, content, parent)

        # 创建下拉框
        self.comboBox = ComboBox(self)
        self.comboBox.setMinimumWidth(280)

        # 添加选项
        for code, display_text in self.EMBED_TYPES:
            self.comboBox.addItem(display_text, userData=code)

        # 设置默认值
        self.set_value(default)

        # 连接信号
        self.comboBox.currentIndexChanged.connect(self._on_selection_changed)

        # 添加到布局
        self.hBoxLayout.addWidget(self.comboBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

    def _on_selection_changed(self, index: int):
        """下拉框选择改变"""
        value = self.comboBox.itemData(index)
        if value:
            self.valueChanged.emit(value)

    def get_value(self) -> str:
        """获取当前选中的值"""
        current_index = self.comboBox.currentIndex()
        return self.comboBox.itemData(current_index) or "soft"

    def set_value(self, value: str):
        """设置选中的值"""
        for i in range(self.comboBox.count()):
            if self.comboBox.itemData(i) == value:
                self.comboBox.setCurrentIndex(i)
                break


class InlinePathPickerCard(SettingCard):
    """A fluent setting card with a right-aligned LineEdit + pick button."""

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        button_text: str = "选择",
        placeholder: str | None = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        if placeholder is not None:
            self.lineEdit.setPlaceholderText(placeholder)

        self.pickButton = PushButton(button_text, self)

        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.pickButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)


class InlinePathPickerActionCard(SettingCard):
    """A fluent setting card with a right-aligned LineEdit + pick button + action button."""

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        pick_text: str = "选择",
        action_text: str = "检查",
        placeholder: str | None = None,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.lineEdit = LineEdit(self)
        if placeholder is not None:
            self.lineEdit.setPlaceholderText(placeholder)

        self.pickButton = PushButton(pick_text, self)
        self.actionButton = PushButton(action_text, self)

        self.hBoxLayout.addWidget(self.lineEdit, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.pickButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.actionButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)


class InlineSwitchCard(SettingCard):
    """A fluent setting card with a right-aligned SwitchButton."""

    checkedChanged = Signal(bool)

    def __init__(self, icon, title: str, content: str | None, parent=None):
        super().__init__(icon, title, content, parent)
        self.switchButton = SwitchButton(self)
        self.hBoxLayout.addWidget(self.switchButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self.switchButton.checkedChanged.connect(self.checkedChanged)


class InlineSwitchActionCard(SettingCard):
    """A fluent setting card with a right-aligned action button + SwitchButton."""

    checkedChanged = Signal(bool)
    actionClicked = Signal()

    def __init__(self, icon, title: str, content: str | None, action_text: str, parent=None):
        super().__init__(icon, title, content, parent)
        self.actionButton = PushButton(action_text, self)
        self.actionButton.clicked.connect(self.actionClicked)
        self.switchButton = SwitchButton(self)
        self.hBoxLayout.addWidget(self.actionButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(8)
        self.hBoxLayout.addWidget(self.switchButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)
        self.switchButton.checkedChanged.connect(self.checkedChanged)


class PotDiagnoseWorker(QThread):
    """POT 一键检测：全部网络往返都在子线程里，UI 永不阻塞。

    `get_health_status()` 会真去铸一次 Token（最坏 15s），`probe_ytdlp_provider()`
    还要起一个 yt-dlp 子进程 —— 这两件事任何一件放在 UI 线程都会把窗口冻住。
    """

    finished = Signal(dict)

    def __init__(self, parent=None, *, recover: bool = False, probe: bool = True):
        super().__init__(parent)
        self.recover = recover
        self.probe = probe

    def run(self):
        report: dict[str, Any] = {"recovered": None}
        try:
            from ..youtube.pot_manager import pot_manager

            if self.recover:
                report["recovered"] = pot_manager.try_recover()

            report["health"] = pot_manager.get_health_status()
            report["plugin_ok"], report["plugin_detail"] = pot_manager.verify_plugin_loadable()
            report["deno_ok"] = pot_manager._probe_deno()

            # 主动探测只在服务确实活着时才跑：服务没起来时 base_url 都拿不到，
            # 白起一个 yt-dlp 子进程只会让用户多等几十秒看一句废话。
            if self.probe and report["health"].get("running"):
                report["probe_ok"], report["probe_detail"] = pot_manager.probe_ytdlp_provider()
            else:
                report["probe_ok"] = None
                report["probe_detail"] = "服务未运行，跳过主动探测"
        except Exception as e:
            report["error"] = str(e)
            logger.exception("[POT][Diagnose] 检测异常")
        self.finished.emit(report)


class SettingsPage(QWidget):
    """设置页面：管理下载、网络、核心组件配置 (重构版 - Pivot导航)"""

    clipboardAutoDetectChanged = Signal(bool)

    # 「解析结果保留时间」下拉框各档位对应的秒数，顺序必须与卡片里的文案一一对应。
    # 首档 0 = 不保留：youtube_service._parse_cache_ttl() <= 0 时读写一并停掉。
    PARSE_CACHE_TTL_CHOICES: tuple[int, ...] = (0, 300, 900, 1800, 3600, 7200)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPage")
        self.setStyleSheet("SettingsPage, #settingsPage { background: transparent; }")

        # Main Layout
        self.mainLayout = QVBoxLayout(self)
        self.mainLayout.setContentsMargins(0, 0, 0, 0)
        self.mainLayout.setSpacing(0)

        # Pivot Navigation (SegmentedWidget for smaller text & rounded look)
        self.pivotContainer = QWidget(self)
        self.pivotContainer.setStyleSheet("background: transparent;")
        self.pivotLayout = QVBoxLayout(self.pivotContainer)
        self.pivot = SegmentedWidget(self)
        self.pivotLayout.addWidget(self.pivot)
        self.pivotLayout.setContentsMargins(30, 15, 30, 5)  # Align with content margins

        self.mainLayout.addWidget(self.pivotContainer)

        # Content Stack
        self.stackedWidget = QStackedWidget(self)
        self.stackedWidget.setStyleSheet("background: transparent;")
        self.mainLayout.addWidget(self.stackedWidget)

        # Cookie刷新worker引用（防止垃圾回收）
        self._active_workers = set()
        self._webview2_login_in_progress: str | None = None  # 记录当前正在登录的平台

        # Init Pages
        self.generalInterface, self.generalScroll, self.generalLayout = self._create_page(
            "generalInterface"
        )
        self.downloadInterface, self.downloadScroll, self.downloadLayout = self._create_page(
            "downloadInterface"
        )
        self.networkInterface, self.networkScroll, self.networkLayout = self._create_page(
            "networkInterface"
        )
        self.featuresInterface, self.featuresScroll, self.featuresLayout = self._create_page(
            "featuresInterface"
        )
        self.componentsInterface, self.componentsScroll, self.componentsLayout = self._create_page(
            "componentsInterface"
        )
        self.systemInterface, self.systemScroll, self.systemLayout = self._create_page(
            "systemInterface"
        )

        # Add pages to stack
        self.stackedWidget.addWidget(self.generalInterface)
        self.stackedWidget.addWidget(self.downloadInterface)
        self.stackedWidget.addWidget(self.networkInterface)
        self.stackedWidget.addWidget(self.featuresInterface)
        self.stackedWidget.addWidget(self.componentsInterface)
        self.stackedWidget.addWidget(self.systemInterface)

        # Setup Pivot items
        self.pivot.addItem(
            routeKey="generalInterface",
            text=self.tr("账号验证"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.generalInterface),
        )
        self.pivot.addItem(
            routeKey="downloadInterface",
            text=self.tr("下载"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.downloadInterface),
        )
        self.pivot.addItem(
            routeKey="networkInterface",
            text=self.tr("网络"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.networkInterface),
        )
        self.pivot.addItem(
            routeKey="featuresInterface",
            text=self.tr("功能"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.featuresInterface),
        )
        self.pivot.addItem(
            routeKey="componentsInterface",
            text=self.tr("更新"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.componentsInterface),
        )
        self.pivot.addItem(
            routeKey="systemInterface",
            text=self.tr("系统"),
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.systemInterface),
        )

        self.pivot.setCurrentItem("generalInterface")
        self.stackedWidget.setCurrentWidget(self.generalInterface)
        self.stackedWidget.currentChanged.connect(self._on_current_tab_changed)

        # === General Tab ===
        self._init_account_group(self.generalScroll.widget(), self.generalLayout)

        # === Download Tab ===
        self._init_download_group(self.downloadScroll.widget(), self.downloadLayout)
        self._init_audio_track_group(self.downloadScroll.widget(), self.downloadLayout)
        self._init_subtitle_group(self.downloadScroll.widget(), self.downloadLayout)
        self._init_format_memory_group(self.downloadScroll.widget(), self.downloadLayout)
        self._init_quality_guard_group(self.downloadScroll.widget(), self.downloadLayout)
        self._init_quick_mode_group(self.downloadScroll.widget(), self.downloadLayout)

        # === Network Tab ===
        self._init_network_group(self.networkScroll.widget(), self.networkLayout)

        # === Features Tab ===
        self._init_automation_group(self.featuresScroll.widget(), self.featuresLayout)
        self._init_postprocess_group(self.featuresScroll.widget(), self.featuresLayout)
        self._init_vr_group(self.featuresScroll.widget(), self.featuresLayout)

        # === Components Tab ===
        self._init_component_group(self.componentsScroll.widget(), self.componentsLayout)

        # === System Tab ===
        self._init_appearance_group(self.systemScroll.widget(), self.systemLayout)
        self._init_advanced_group(self.systemScroll.widget(), self.systemLayout)
        self._init_behavior_group(self.systemScroll.widget(), self.systemLayout)
        self._init_log_group(self.systemScroll.widget(), self.systemLayout)
        self._init_about_group(self.systemScroll.widget(), self.systemLayout)

        self._load_settings_to_ui()
        config_manager.configChanged.connect(self._on_global_config_changed)

    def _on_global_config_changed(self, key: str, value: Any):
        if key == "single_container_override":
            idx = self.singleContainerCard.comboBox.findText(value)
            if idx >= 0 and self.singleContainerCard.comboBox.currentIndex() != idx:
                self.singleContainerCard.comboBox.setCurrentIndex(idx)
        elif key == "single_audio_override":
            idx = self.singleAudioCard.comboBox.findText(value)
            if idx >= 0 and self.singleAudioCard.comboBox.currentIndex() != idx:
                self.singleAudioCard.comboBox.setCurrentIndex(idx)
        elif key == "playlist_container_override":
            idx = self.playlistContainerCard.comboBox.findText(value)
            if idx >= 0 and self.playlistContainerCard.comboBox.currentIndex() != idx:
                self.playlistContainerCard.comboBox.setCurrentIndex(idx)
        elif key == "playlist_audio_override":
            idx = self.playlistAudioCard.comboBox.findText(value)
            if idx >= 0 and self.playlistAudioCard.comboBox.currentIndex() != idx:
                self.playlistAudioCard.comboBox.setCurrentIndex(idx)
        elif key == "network_retries":
            val = int(value)
            if self.networkRetriesCard.spinBox.value() != val:
                self.networkRetriesCard.spinBox.setValue(val)

    def _create_page(self, object_name: str):
        page = QWidget()
        page.setObjectName(object_name)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = ScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setObjectName(f"{object_name}Scroll")
        scroll.setStyleSheet("background: transparent; border: none;")
        scrollWidget = QWidget()
        scrollWidget.setObjectName(f"{object_name}ScrollWidget")
        scrollWidget.setStyleSheet("background: transparent;")
        expandLayout = QVBoxLayout(scrollWidget)
        expandLayout.setSpacing(20)
        expandLayout.setContentsMargins(30, 20, 30, 20)
        scroll.setWidget(scrollWidget)
        layout.addWidget(scroll)
        return page, scroll, expandLayout

    def _on_current_tab_changed(self, index):
        widget = self.stackedWidget.widget(index)
        if widget:
            self.pivot.setCurrentItem(widget.objectName())

    def showEvent(self, event):
        """页面显示时更新Cookie状态"""
        super().showEvent(event)
        # 每次显示设置页面时刷新Cookie状态
        self._update_cookie_status()

    def _init_download_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.downloadGroup = SettingCardGroup(self.tr("下载选项"), parent_widget)

        self.downloadFolderCard = PushSettingCard(
            self.tr("选择文件夹"),
            FluentIcon.FOLDER,
            self.tr("默认保存路径"),
            str(config_manager.get("download_dir")),
            self.downloadGroup,
        )
        self.downloadFolderCard.clicked.connect(self._select_download_folder)

        self.concurrentFragmentsCard = InlineComboBoxCard(
            FluentIcon.SPEED_HIGH,
            self.tr("分片并发数"),
            self.tr("设置单个视频的分片下载线程数 (默认: 4)"),
            [str(i) for i in range(1, 9)],
            self.downloadGroup,
        )
        current_frag = config_manager.get("concurrent_fragments", 4)
        self.concurrentFragmentsCard.comboBox.setCurrentIndex(max(0, min(7, int(current_frag) - 1)))
        self.concurrentFragmentsCard.comboBox.currentIndexChanged.connect(
            self._on_concurrent_fragments_changed
        )

        self.networkRetriesCard = InlineSpinBoxCard(
            FluentIcon.SYNC,
            self.tr("网络重试次数"),
            self.tr("请求失败或切片断连时的最大重试次数，网络较差时建议调高"),
            1,
            200,
            self.downloadGroup,
        )
        current_retries = config_manager.get("network_retries", 10)
        self.networkRetriesCard.spinBox.setValue(int(current_retries))
        self.networkRetriesCard.spinBox.valueChanged.connect(self._on_network_retries_changed)

        # Max Concurrent Downloads
        self.maxConcurrentCard = InlineComboBoxCard(
            FluentIcon.ALBUM,
            self.tr("最大同时下载数"),
            self.tr("设置同时进行的下载任务数量 (默认: 3)"),
            [str(i) for i in range(1, 11)],
            self.downloadGroup,
        )
        # Select current value
        current_max = config_manager.get("max_concurrent_downloads", 3)
        self.maxConcurrentCard.comboBox.setCurrentIndex(max(0, min(9, int(current_max) - 1)))
        self.maxConcurrentCard.comboBox.currentIndexChanged.connect(self._on_max_concurrent_changed)

        # Playlist Extract Concurrency
        self.playlistExtractConcurrencyCard = InlineComboBoxCard(
            FluentIcon.SYNC,
            self.tr("播放列表解析并发"),
            self.tr(
                "控制进入解析页后同时获取视频详情的数量。过高可能触发 YouTube IP 限制 (默认: 2)"
            ),
            ["1", "2", "3", "5", "8", "12", "16"],
            self.downloadGroup,
        )
        current_extract_concurrency = config_manager.get("playlist_extract_concurrency", 2)
        mapping_ext = {1: 0, 2: 1, 3: 2, 5: 3, 8: 4, 12: 5, 16: 6}
        self.playlistExtractConcurrencyCard.comboBox.setCurrentIndex(
            mapping_ext.get(int(current_extract_concurrency), 1)
        )
        self.playlistExtractConcurrencyCard.comboBox.currentIndexChanged.connect(
            self._on_playlist_extract_concurrency_changed
        )

        # 解析结果保留时间（对应 parse_cache_ttl_seconds）。
        # 文案刻意不出现 "TTL"/"缓存过期" 这类术语；"不保留" 即等于整体关闭。
        self.parseCacheTtlCard = InlineComboBoxCard(
            FluentIcon.HISTORY,
            self.tr("解析结果保留时间"),
            self.tr("同一链接在此时间内再次解析会直接复用上次结果，不再重新请求（默认: 30 分钟）"),
            [
                self.tr("不保留"),
                self.tr("5 分钟"),
                self.tr("15 分钟"),
                self.tr("30 分钟"),
                self.tr("1 小时"),
                self.tr("2 小时"),
            ],
            self.downloadGroup,
        )
        current_ttl = config_manager.get("parse_cache_ttl_seconds", 1800)
        try:
            current_ttl = int(current_ttl)
        except (TypeError, ValueError):
            current_ttl = 1800
        # 落在选项之外的历史值（旧版本手改过 config.json）就近显示为默认档，
        # 但**不写回配置** —— 用户没动这张卡片就不该被静默改掉。
        self.parseCacheTtlCard.comboBox.setCurrentIndex(
            self.PARSE_CACHE_TTL_CHOICES.index(current_ttl)
            if current_ttl in self.PARSE_CACHE_TTL_CHOICES
            else self.PARSE_CACHE_TTL_CHOICES.index(1800)
        )
        self.parseCacheTtlCard.comboBox.currentIndexChanged.connect(
            self._on_parse_cache_ttl_changed
        )

        self.failedTaskRetentionCard = InlineComboBoxCard(
            FluentIcon.HISTORY,
            self.tr("失败任务保留时间"),
            self.tr("设置下载失败的任务记录自动清理时间 (默认: 3 天)"),
            [
                self.tr("1 天"),
                self.tr("3 天"),
                self.tr("7 天"),
                self.tr("15 天"),
                self.tr("30 天"),
                self.tr("永久保留"),
            ],
            self.downloadGroup,
        )
        current_retention = config_manager.get("failed_task_retention_days", 3)
        mapping = {1: 0, 3: 1, 7: 2, 15: 3, 30: 4, -1: 5}
        self.failedTaskRetentionCard.comboBox.setCurrentIndex(
            mapping.get(int(current_retention), 1)
        )
        self.failedTaskRetentionCard.comboBox.currentIndexChanged.connect(
            self._on_retention_days_changed
        )

        self.downloadGroup.addSettingCard(self.downloadFolderCard)
        self.downloadGroup.addSettingCard(self.concurrentFragmentsCard)
        self.downloadGroup.addSettingCard(self.networkRetriesCard)
        self.downloadGroup.addSettingCard(self.maxConcurrentCard)
        self.downloadGroup.addSettingCard(self.playlistExtractConcurrencyCard)
        self.downloadGroup.addSettingCard(self.parseCacheTtlCard)
        self.downloadGroup.addSettingCard(self.failedTaskRetentionCard)
        layout.addWidget(self.downloadGroup)

    def _init_audio_track_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.audioTrackGroup = SettingCardGroup(self.tr("音轨下载"), parent_widget)

        # 音频首选语言 (支持多选排序)
        config = config_manager.get("preferred_audio_languages", ["zh-Hans", "en", "orig"])
        if not isinstance(config, list):
            config = ["zh-Hans", "en", "orig"]

        langs = [
            ("orig", self.tr("原音 (视频原生语言配音)")),
            ("zh-Hans", self.tr("中文 (简体)")),
            ("zh-Hant", self.tr("中文 (繁体)")),
            ("en", self.tr("英语")),
            ("ja", self.tr("日语")),
            ("ko", self.tr("韩语")),
            ("ru", self.tr("俄语")),
            ("fr", self.tr("法语")),
            ("de", self.tr("德语")),
            ("es", self.tr("西班牙语")),
        ]

        self.preferredAudioLanguageCard = AudioLanguageMultiSelectCard(
            FluentIcon.MUSIC,
            self.tr("首选音轨语言 (多音轨视频)"),
            self.tr("当视频包含多个语言配音时，优先下载哪种语言的轨段 (可多选并排序)"),
            languages=langs,
            selected_default=config,
            parent=self.audioTrackGroup,
        )
        self.preferredAudioLanguageCard.selectionChanged.connect(
            self._on_preferred_audio_language_changed
        )

        self.audioTrackGroup.addSettingCard(self.preferredAudioLanguageCard)
        layout.addWidget(self.audioTrackGroup)

        # Trigger warning check initially
        self._on_max_concurrent_changed(self.maxConcurrentCard.comboBox.currentIndex())

    def _init_format_memory_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.formatMemoryGroup = SettingCardGroup(self.tr("输出偏好记忆"), parent_widget)

        self.singleContainerCard = InlineComboBoxCard(
            FluentIcon.MOVIE,
            self.tr("单视频容器默认"),
            self.tr("在单视频解析模式下的默认视频封装容器"),
            [self.tr("自动推断"), "MP4", "MKV", "WebM"],
            self.formatMemoryGroup,
        )
        c_val = config_manager.get("single_container_override", "自动推断")
        idx = self.singleContainerCard.comboBox.findText(
            self.tr(c_val) if c_val == "自动推断" else c_val
        )
        if idx >= 0:
            self.singleContainerCard.comboBox.setCurrentIndex(idx)
        self.singleContainerCard.comboBox.currentIndexChanged.connect(
            lambda i: config_manager.set(
                "single_container_override",
                "自动推断"
                if self.singleContainerCard.comboBox.currentText() == self.tr("自动推断")
                else self.singleContainerCard.comboBox.currentText(),
            )
        )

        self.singleAudioCard = InlineComboBoxCard(
            FluentIcon.MUSIC,
            self.tr("单视频音频默认"),
            self.tr("在单视频解析模式下的默认纯音频格式"),
            [self.tr("自动推断"), "MP3", "FLAC", "M4A", "WAV", "Opus", "AAC"],
            self.formatMemoryGroup,
        )
        a_val = config_manager.get("single_audio_override", "自动推断")
        idx = self.singleAudioCard.comboBox.findText(
            self.tr(a_val) if a_val == "自动推断" else a_val
        )
        if idx >= 0:
            self.singleAudioCard.comboBox.setCurrentIndex(idx)
        self.singleAudioCard.comboBox.currentIndexChanged.connect(
            lambda i: config_manager.set(
                "single_audio_override",
                "自动推断"
                if self.singleAudioCard.comboBox.currentText() == self.tr("自动推断")
                else self.singleAudioCard.comboBox.currentText(),
            )
        )

        self.playlistContainerCard = InlineComboBoxCard(
            FluentIcon.FOLDER,
            self.tr("播放列表容器默认"),
            self.tr("在播放列表高级格式设置中的默认容器"),
            [self.tr("自动推断"), "MP4", "MKV", "WebM"],
            self.formatMemoryGroup,
        )
        pc_val = config_manager.get("playlist_container_override", "自动推断")
        idx = self.playlistContainerCard.comboBox.findText(
            self.tr(pc_val) if pc_val == "自动推断" else pc_val
        )
        if idx >= 0:
            self.playlistContainerCard.comboBox.setCurrentIndex(idx)
        self.playlistContainerCard.comboBox.currentIndexChanged.connect(
            lambda i: config_manager.set(
                "playlist_container_override",
                "自动推断"
                if self.playlistContainerCard.comboBox.currentText() == self.tr("自动推断")
                else self.playlistContainerCard.comboBox.currentText(),
            )
        )

        self.playlistAudioCard = InlineComboBoxCard(
            FluentIcon.ALBUM,
            self.tr("播放列表音频默认"),
            self.tr("在播放列表高级格式设置中的默认音频格式"),
            [self.tr("自动推断"), "MP3", "FLAC", "M4A", "WAV", "Opus", "AAC"],
            self.formatMemoryGroup,
        )
        pa_val = config_manager.get("playlist_audio_override", "自动推断")
        idx = self.playlistAudioCard.comboBox.findText(pa_val)
        if idx >= 0:
            self.playlistAudioCard.comboBox.setCurrentIndex(idx)
        self.playlistAudioCard.comboBox.currentIndexChanged.connect(
            lambda i: config_manager.set(
                "playlist_audio_override", self.playlistAudioCard.comboBox.currentText()
            )
        )

        self.formatMemoryGroup.addSettingCard(self.singleContainerCard)
        self.formatMemoryGroup.addSettingCard(self.singleAudioCard)
        self.formatMemoryGroup.addSettingCard(self.playlistContainerCard)
        self.formatMemoryGroup.addSettingCard(self.playlistAudioCard)
        layout.addWidget(self.formatMemoryGroup)

    def _init_quality_guard_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.qualityGuardGroup = SettingCardGroup(self.tr("下载质量风控"), parent_widget)

        self.qualityGuardModeCard = InlineComboBoxCard(
            FluentIcon.CERTIFICATE,
            self.tr("质量偏差拦截策略"),
            self.tr("当实际下载画质无法达到预期目标时的处理方式"),
            [self.tr("仅警告 (默认)"), self.tr("阻止并挂起"), self.tr("忽略差异")],
            self.qualityGuardGroup,
        )
        mode = str(config_manager.get("quality_guard_mode", "warn"))
        mode_idx = {"warn": 0, "block": 1, "ignore": 2}.get(mode, 0)
        self.qualityGuardModeCard.comboBox.setCurrentIndex(mode_idx)
        self.qualityGuardModeCard.comboBox.currentIndexChanged.connect(
            self._on_quality_guard_mode_changed
        )

        self.qualityGuardThresholdCard = InlineComboBoxCard(
            FluentIcon.STOP_WATCH,
            self.tr("风控熔断阈值"),
            self.tr("连续出现多少个质量异常任务后，自动暂停排队任务"),
            ["1", "2", "3", "5", "10"],
            self.qualityGuardGroup,
        )
        threshold = str(config_manager.get("quality_guard_suspend_threshold", 3))
        try:
            t_idx = ["1", "2", "3", "5", "10"].index(threshold)
        except ValueError:
            t_idx = 2
        self.qualityGuardThresholdCard.comboBox.setCurrentIndex(t_idx)
        self.qualityGuardThresholdCard.comboBox.currentIndexChanged.connect(
            self._on_quality_guard_threshold_changed
        )

        self.qualityGuardFfprobeCard = InlineSwitchCard(
            FluentIcon.VIDEO,
            self.tr("FFprobe 精准物理核验"),
            self.tr(
                "当系统无法从下载日志中提取实际分辨率时，强制调用 ffprobe 探测已下载视频文件的物理尺寸"
            ),
            parent=self.qualityGuardGroup,
        )
        self.qualityGuardFfprobeCard.switchButton.setChecked(
            bool(config_manager.get("quality_guard_ffprobe", False))
        )
        self.qualityGuardFfprobeCard.checkedChanged.connect(
            lambda checked: config_manager.set("quality_guard_ffprobe", checked)
        )

        self.qualityGuardGroup.addSettingCard(self.qualityGuardModeCard)
        self.qualityGuardGroup.addSettingCard(self.qualityGuardThresholdCard)
        self.qualityGuardGroup.addSettingCard(self.qualityGuardFfprobeCard)
        layout.addWidget(self.qualityGuardGroup)

    def _init_quick_mode_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.quickModeGroup = SettingCardGroup(self.tr("快速模式策略"), parent_widget)

        self.quickPlaylistExpandThresholdCard = InlineComboBoxCard(
            FluentIcon.FOLDER,
            self.tr("自动策略展开阈值"),
            self.tr(
                "当使用“自动判断”策略时，播放列表视频数超过此阈值将强制逐条展开，否则作为一个单任务"
            ),
            ["10", "30", "50", "100", "200"],
            self.quickModeGroup,
        )
        threshold = str(config_manager.get("quick_playlist_expand_threshold", 50))
        try:
            th_idx = ["10", "30", "50", "100", "200"].index(threshold)
        except ValueError:
            th_idx = 2
        self.quickPlaylistExpandThresholdCard.comboBox.setCurrentIndex(th_idx)
        self.quickPlaylistExpandThresholdCard.comboBox.currentIndexChanged.connect(
            self._on_quick_playlist_expand_threshold_changed
        )

        self.quickMaxTotalTasksCard = InlineComboBoxCard(
            FluentIcon.TILES,
            self.tr("任务入队数安全上限"),
            self.tr(
                "限制单词快速添加能塞入队列的最大任务数量，防止因过多任务导致卡死或触发严重风控"
            ),
            ["100", "300", "500", "1000", self.tr("无限制")],
            self.quickModeGroup,
        )
        max_tasks = str(config_manager.get("quick_max_total_tasks", 500))
        try:
            mt_idx = ["100", "300", "500", "1000", "无限制"].index(max_tasks)
        except ValueError:
            mt_idx = 2
        self.quickMaxTotalTasksCard.comboBox.setCurrentIndex(mt_idx)
        self.quickMaxTotalTasksCard.comboBox.currentIndexChanged.connect(
            self._on_quick_max_total_tasks_changed
        )

        self.quickModeGroup.addSettingCard(self.quickPlaylistExpandThresholdCard)
        self.quickModeGroup.addSettingCard(self.quickMaxTotalTasksCard)
        layout.addWidget(self.quickModeGroup)

    def _on_quick_playlist_expand_threshold_changed(self, index: int) -> None:
        val = int(["10", "30", "50", "100", "200"][index])
        config_manager.set("quick_playlist_expand_threshold", val)
        config_manager.save()

    def _on_quick_max_total_tasks_changed(self, index: int) -> None:
        val_str = ["100", "300", "500", "1000", "无限制"][index]
        val = 99999 if val_str == "无限制" else int(val_str)
        config_manager.set("quick_max_total_tasks", val)
        config_manager.save()

    def _on_quality_guard_mode_changed(self, index: int) -> None:
        mode = {0: "warn", 1: "block", 2: "ignore"}.get(index, "warn")
        config_manager.set("quality_guard_mode", mode)
        config_manager.save()

    def _on_quality_guard_threshold_changed(self, index: int) -> None:
        threshold = int(["1", "2", "3", "5", "10"][index])
        config_manager.set("quality_guard_suspend_threshold", threshold)
        config_manager.save()

    def _on_retention_days_changed(self, index: int):
        days = [1, 3, 7, 15, 30, -1][index]
        config_manager.set("failed_task_retention_days", days)
        from ..utils.logger import logger

        logger.info(self.tr("失败任务保留天数已更新为: {}").format(days))

    def _init_network_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.networkGroup = SettingCardGroup(self.tr("网络连接"), parent_widget)

        self.proxyModeCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            self.tr("代理模式"),
            self.tr("选择网络连接方式"),
            [
                self.tr("不使用代理"),
                self.tr("使用系统代理"),
                self.tr("手动 HTTP 代理"),
                self.tr("手动 SOCKS5 代理"),
            ],
            self.networkGroup,
        )
        self.proxyModeCard.comboBox.currentIndexChanged.connect(self._on_proxy_mode_changed)

        self.proxyEditCard = InlineLineEditCard(
            FluentIcon.EDIT,
            self.tr("自定义代理地址"),
            self.tr("仅手动代理模式生效 (示例: 127.0.0.1:7890)"),
            placeholder="127.0.0.1:7890",
            parent=self.networkGroup,
        )
        self.proxyEditCard.lineEdit.setText(
            str(config_manager.get("proxy_url") or "127.0.0.1:7890")
        )
        self.proxyEditCard.lineEdit.editingFinished.connect(self._on_proxy_url_edited)

        self.networkGroup.addSettingCard(self.proxyModeCard)
        self.networkGroup.addSettingCard(self.proxyEditCard)
        layout.addWidget(self.networkGroup)

    def _init_account_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化账号与认证设置组"""
        self.accountGroup = SettingCardGroup(self.tr("账号验证"), parent_widget)

        # === Cookie Sentinel 配置组 ===
        self.cookieModeCard = InlineComboBoxCard(
            FluentIcon.PEOPLE,
            self.tr("Cookie 来源"),
            self.tr("选择 Cookie 获取方式（Cookie 卫士会自动维护生命周期）"),
            [
                self.tr("🚀 自动从本地浏览器提取"),
                self.tr("🔑 登录获取 (推荐)"),
                self.tr("📄 手动导入 cookies.txt 文件"),
            ],
            self.accountGroup,
        )
        self.cookieModeCard.comboBox.currentIndexChanged.connect(self._on_cookie_mode_changed)

        self.browserCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            self.tr("选择浏览器"),
            self.tr("Chromium 内核需管理员权限，Firefox 内核无需管理员权限"),
            [
                self.tr("Microsoft Edge"),
                self.tr("Google Chrome (⚠️不稳定)"),
                self.tr("Chromium"),
                self.tr("Brave"),
                self.tr("Opera"),
                self.tr("Opera GX"),
                self.tr("Vivaldi"),
                self.tr("Arc"),
                self.tr("Firefox"),
                self.tr("LibreWolf"),
                self.tr("百分浏览器 (Cent)"),
            ],
            self.accountGroup,
        )
        self.browserCard.comboBox.currentIndexChanged.connect(self._on_cookie_browser_changed)

        self.browserRefreshCard = PushSettingCard(
            self.tr("一键提取"),
            FluentIcon.SYNC,
            self.tr("提取所有支持的平台"),
            self.tr("自动从选定的本地浏览器提取 YouTube 和 X 平台 Cookie，并进行规范化处理"),
            self.accountGroup,
        )
        self.browserRefreshCard.clicked.connect(self._on_browser_refresh_clicked)

        # 多平台 WebView2 账号手风琴
        from fluentytdl.ui.components.settings.platform_auth_card import PlatformAuthExpandCard

        self.youtubeAuthCard = PlatformAuthExpandCard(
            "youtube",
            FluentIcon.GLOBE,
            self.tr("YouTube 登录"),
            self.tr("管理 YouTube 平台的 WebView2 账号"),
            self.accountGroup,
        )
        self.youtubeAuthCard.loginClicked.connect(self._on_webview2_login_clicked)
        self.youtubeAuthCard.accountChanged.connect(self._on_webview2_account_changed)
        self.youtubeAuthCard.addAccountClicked.connect(self._on_add_webview2_account_clicked)
        self.youtubeAuthCard.removeAccountClicked.connect(self._on_remove_webview2_account_clicked)
        self.youtubeAuthCard.refreshCookieClicked.connect(self._on_refresh_cookie_clicked)
        self.youtubeAuthCard.openCookieLocationClicked.connect(self._open_cookie_location)

        self.twitterAuthCard = PlatformAuthExpandCard(
            "twitter",
            FluentIcon.GITHUB,  # fallback icon for X
            self.tr("X (Twitter) 登录"),
            self.tr("管理 X (Twitter) 平台的 WebView2 账号"),
            self.accountGroup,
        )
        self.twitterAuthCard.loginClicked.connect(self._on_webview2_login_clicked)
        self.twitterAuthCard.accountChanged.connect(self._on_webview2_account_changed)
        self.twitterAuthCard.addAccountClicked.connect(self._on_add_webview2_account_clicked)
        self.twitterAuthCard.removeAccountClicked.connect(self._on_remove_webview2_account_clicked)
        self.twitterAuthCard.refreshCookieClicked.connect(self._on_refresh_cookie_clicked)
        self.twitterAuthCard.openCookieLocationClicked.connect(self._open_cookie_location)

        # Cookie 文件选择
        self.cookieFileCard = PushSettingCard(
            self.tr("选择文件"),
            FluentIcon.DOCUMENT,
            self.tr("Cookie 文件路径"),
            self.tr("未选择"),
            self.accountGroup,
        )
        self.cookieFileCard.clicked.connect(self._select_cookie_file)

        # Cookie 清洗开关
        self.cookieCleaningCard = InlineSwitchCard(
            FluentIcon.BROOM,
            self.tr("Cookie 合规清洗"),
            self.tr(
                "开启后仅保留 YouTube 核心 Cookie（关闭可支持其他平台，但可能暴露更多隐私数据）"
            ),
            parent=self.accountGroup,
        )
        self.cookieCleaningCard.checkedChanged.connect(self._on_cookie_cleaning_changed)

        self.accountGroup.addSettingCard(self.cookieModeCard)
        self.accountGroup.addSettingCard(self.browserCard)
        self.accountGroup.addSettingCard(self.browserRefreshCard)
        self.accountGroup.addSettingCard(self.cookieCleaningCard)
        self.accountGroup.addSettingCard(self.youtubeAuthCard)
        self.accountGroup.addSettingCard(self.twitterAuthCard)
        self.accountGroup.addSettingCard(self.cookieFileCard)

        # 一键诊断

        layout.addWidget(self.accountGroup)

        # Make Cookie dependent cards look like "children" of cookie mode card
        self._indent_setting_card(self.browserCard)
        self._indent_setting_card(self.browserRefreshCard)
        self._indent_setting_card(self.cookieFileCard)
        self._indent_setting_card(self.cookieCleaningCard)
        self._indent_setting_card(self.youtubeAuthCard)
        self._indent_setting_card(self.twitterAuthCard)

    def _init_component_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化软件更新与核心组件设置组"""

        # ── 软件更新组 ──
        self.appUpdateGroup = SettingCardGroup(self.tr("软件更新"), parent_widget)
        self.appUpdateCard = AppUpdateSettingCard(self.appUpdateGroup)
        self.appUpdateGroup.addSettingCard(self.appUpdateCard)
        layout.addWidget(self.appUpdateGroup)

        # ── 核心组件组 ──
        self.coreGroup = SettingCardGroup(self.tr("核心组件"), parent_widget)

        # Check Updates on Startup
        self.checkUpdatesOnStartupCard = InlineSwitchCard(
            FluentIcon.SYNC,
            self.tr("启动时自动检查更新"),
            self.tr("开启后，每隔 24 小时自动检查所有组件更新（默认开启）"),
            parent=self.coreGroup,
        )
        self.checkUpdatesOnStartupCard.checkedChanged.connect(
            self._on_check_updates_startup_changed
        )

        # Update Source
        self.updateSourceCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            self.tr("组件更新源"),
            self.tr("选择组件下载和检查更新的网络来源"),
            [self.tr("GitHub (官方)"), self.tr("GHProxy (加速镜像)")],
            parent=self.coreGroup,
        )
        self.updateSourceCard.comboBox.currentIndexChanged.connect(self._on_update_source_changed)

        # yt-dlp Update Channel
        self.ytDlpChannelCard = InlineComboBoxCard(
            FluentIcon.SYNC,
            self.tr("yt-dlp 更新频道"),
            self.tr("选择 yt-dlp 版本的更新分支"),
            [self.tr("Nightly (每夜版)"), self.tr("Stable (稳定版)"), self.tr("Master (主线)")],
            parent=self.coreGroup,
        )
        self.ytDlpChannelCard.comboBox.currentIndexChanged.connect(self._on_ytdlp_channel_changed)

        # Component Cards
        self.ytDlpCard = ComponentSettingCard(
            "yt-dlp",
            FluentIcon.DOWNLOAD,
            self.tr("yt-dlp 引擎"),
            self.tr("点击检查更新以获取最新版本"),
            self.coreGroup,
        )

        self.ffmpegCard = ComponentSettingCard(
            "ffmpeg",
            FluentIcon.VIDEO,
            self.tr("FFmpeg 引擎"),
            self.tr("点击检查更新以获取最新版本"),
            self.coreGroup,
        )

        self.denoCard = ComponentSettingCard(
            "deno",
            FluentIcon.CODE,
            self.tr("JS Runtime (Deno)"),
            self.tr("用于加速 yt-dlp 解析（点击检查更新）"),
            self.coreGroup,
        )

        self.potProviderCard = ComponentSettingCard(
            "pot-provider",
            FluentIcon.CERTIFICATE,
            self.tr("POT Provider"),
            self.tr("用于绕过 YouTube 机器人检测（点击检查更新）"),
            self.coreGroup,
        )

        self.atomicParsleyCard = ComponentSettingCard(
            "atomicparsley",
            FluentIcon.PHOTO,
            self.tr("AtomicParsley"),
            self.tr("用于 MP4/M4A 封面嵌入（启用封面嵌入功能需要此工具）"),
            self.coreGroup,
        )

        self.jsRuntimeCard = InlineComboBoxCard(
            FluentIcon.CODE,
            self.tr("JS Runtime 策略"),
            self.tr("选择首选的 JavaScript 运行时"),
            [self.tr("自动(推荐)"), "Deno", "Node", "Bun", "QuickJS"],
            self.coreGroup,
        )
        self.jsRuntimeCard.comboBox.currentIndexChanged.connect(self._on_js_runtime_changed)

        self.coreGroup.addSettingCard(self.checkUpdatesOnStartupCard)
        self.coreGroup.addSettingCard(self.updateSourceCard)
        self.coreGroup.addSettingCard(self.ytDlpChannelCard)
        self.coreGroup.addSettingCard(self.ytDlpCard)
        self.coreGroup.addSettingCard(self.ffmpegCard)
        self.coreGroup.addSettingCard(self.denoCard)
        self.coreGroup.addSettingCard(self.potProviderCard)
        self.coreGroup.addSettingCard(self.atomicParsleyCard)
        self.coreGroup.addSettingCard(self.jsRuntimeCard)
        layout.addWidget(self.coreGroup)

    def _init_appearance_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.appearanceGroup = SettingCardGroup(self.tr("外观"), parent_widget)

        self.appLanguageCard = InlineComboBoxCard(
            FluentIcon.LANGUAGE,
            self.tr("界面语言 (Language)"),
            self.tr("选择应用的界面语言（重启后生效）"),
            [self.tr("跟随系统 (Auto)"), "简体中文 (zh_CN)", "English (en_US)"],
            self.appearanceGroup,
        )
        lang_val = config_manager.get("app_language", "auto")
        lang_idx_map = {"auto": 0, "zh_CN": 1, "en_US": 2}
        self.appLanguageCard.comboBox.setCurrentIndex(lang_idx_map.get(lang_val, 0))
        self.appLanguageCard.comboBox.currentIndexChanged.connect(self._on_app_language_changed)
        self.appearanceGroup.addSettingCard(self.appLanguageCard)

        self.themeModeCard = InlineComboBoxCard(
            FluentIcon.BRUSH,
            self.tr("主题模式"),
            self.tr("选择应用的色彩主题"),
            [self.tr("跟随系统 (自动)"), self.tr("浅色模式"), self.tr("深色模式")],
            self.appearanceGroup,
        )
        self.themeModeCard.comboBox.currentIndexChanged.connect(self._on_theme_mode_changed)

        self.appearanceGroup.addSettingCard(self.themeModeCard)
        layout.addWidget(self.appearanceGroup)

    def _on_app_language_changed(self, index: int):
        val_map = {0: "auto", 1: "zh_CN", 2: "en_US"}
        val = val_map.get(index, "auto")
        old_val = config_manager.get("app_language", "auto")
        if old_val != val:
            config_manager.set("app_language", val)
            msg_box = MessageBox(
                self.tr("需要重启"),
                self.tr("语言设置已更改，请重启应用以使更改生效。\n是否立即重启？"),
                self.window(),
            )
            msg_box.yesButton.setText(self.tr("立即重启"))
            msg_box.cancelButton.setText(self.tr("稍后"))
            if msg_box.exec():
                import subprocess
                import sys

                from PySide6.QtWidgets import QApplication

                if getattr(sys, "frozen", False):
                    cmd = [sys.executable] + sys.argv[1:]
                else:
                    cmd = [sys.executable] + sys.argv

                subprocess.Popen(cmd)
                QApplication.quit()

    def _init_advanced_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.advancedGroup = SettingCardGroup(self.tr("高级"), parent_widget)

        self.potProviderEnabledCard = InlineSwitchActionCard(
            FluentIcon.CERTIFICATE,
            self.tr("POT 验证引擎 (实验性)"),
            self.tr("正在读取状态…"),
            self.tr("一键检测"),
            parent=self.advancedGroup,
        )
        self.potProviderEnabledCard.switchButton.setChecked(
            config_manager.get("pot_provider_enabled", False)
        )
        self.potProviderEnabledCard.checkedChanged.connect(self._on_pot_provider_toggled)
        self.potProviderEnabledCard.actionClicked.connect(self._on_pot_diagnose_clicked)

        # 开关旁的实时健康摘要。只读内存状态（零网络 I/O），所以可以放心定时刷。
        self._pot_health_timer = QTimer(self)
        self._pot_health_timer.setInterval(2000)
        self._pot_health_timer.timeout.connect(self._refresh_pot_health_content)
        self._pot_health_timer.start()
        self._refresh_pot_health_content()

        self.poTokenCard = SmartSettingCard(
            FluentIcon.CODE,
            self.tr("YouTube PO Token(可选)"),
            self.tr("可留空清除；保存后用于提升可用性（偏极客/实验性）"),
            config_key="youtube_po_token",
            parent=self.advancedGroup,
            validator=self._validate_po_token,
            fixer=None,
            prefer_multiline=True,
            dialog_content="粘贴或输入 PO Token。允许留空；非空时将进行简单格式校验。",
        )

        self.jsRuntimePathCard = SmartSettingCard(
            FluentIcon.DOCUMENT,
            self.tr("JS Runtime 路径(可选)"),
            self._js_runtime_status_text(),
            config_key="js_runtime_path",
            parent=self.advancedGroup,
            validator=self._validate_optional_exe_path,
            fixer=self._fix_windows_path,
            empty_text="",
            dialog_content="请输入 JS Runtime 可执行文件路径（可留空）。支持粘贴带引号的路径。",
            pick_file=True,
            file_filter="Executable Files (*.exe);;All Files (*)",
        )
        self.jsRuntimePathCard.valueChanged.connect(
            lambda _: self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
        )

        self.advancedGroup.addSettingCard(self.potProviderEnabledCard)
        self.advancedGroup.addSettingCard(self.poTokenCard)
        self.advancedGroup.addSettingCard(self.jsRuntimePathCard)
        layout.addWidget(self.advancedGroup)

    def _init_automation_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.automationGroup = SettingCardGroup(self.tr("自动化"), parent_widget)

        self.clipboardDetectCard = InlineSwitchCard(
            FluentIcon.EDIT,
            self.tr("剪贴板自动识别"),
            self.tr("自动识别复制的视频链接并弹出解析窗口（默认关闭）"),
            parent=self.automationGroup,
        )
        self.clipboardDetectCard.checkedChanged.connect(self._on_clipboard_detect_changed)

        self.clipboardActionModeCard = InlineComboBoxCard(
            FluentIcon.PLAY,
            self.tr("剪贴板识别默认行为"),
            self.tr("选择自动识别到链接后的处理方式"),
            [
                self.tr("智能识别 (推荐)"),
                self.tr("仅普通下载"),
                self.tr("仅 VR 下载"),
                self.tr("仅下载字幕"),
                self.tr("仅下载封面"),
            ],
            parent=self.automationGroup,
        )
        self.clipboardActionModeCard.comboBox.currentIndexChanged.connect(
            self._on_clipboard_action_mode_changed
        )

        self.clipboardWindowToFrontCard = InlineSwitchCard(
            FluentIcon.APPLICATION,
            self.tr("解析后置顶窗口"),
            self.tr("识别到链接并弹出解析窗口时，自动将其置于前台（默认开启）"),
            parent=self.automationGroup,
        )
        self.clipboardWindowToFrontCard.checkedChanged.connect(
            self._on_clipboard_window_to_front_changed
        )

        self.automationGroup.addSettingCard(self.clipboardDetectCard)
        self.automationGroup.addSettingCard(self.clipboardActionModeCard)
        self.automationGroup.addSettingCard(self.clipboardWindowToFrontCard)
        layout.addWidget(self.automationGroup)

    def _init_vr_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化 VR / 360° 设置组"""
        self.vrGroup = SettingCardGroup(self.tr("VR / 360°"), parent_widget)

        # 硬件状态 Banner
        self.vrHardwareStatusCard = SettingCard(
            FluentIcon.INFO,
            self.tr("硬件性能检测"),
            self.tr("正在检测系统硬件..."),
            self.vrGroup,
        )
        self.vrHardwareStatusCard.hBoxLayout.addSpacing(16)

        # 刷新按钮
        self.vrRefreshHardwareBtn = ToolButton(FluentIcon.SYNC, self.vrHardwareStatusCard)
        self.vrRefreshHardwareBtn.setToolTip("重新检测硬件")
        self.vrRefreshHardwareBtn.clicked.connect(self._update_vr_hardware_status)
        self.vrHardwareStatusCard.hBoxLayout.addWidget(
            self.vrRefreshHardwareBtn, 0, Qt.AlignmentFlag.AlignRight
        )
        self.vrHardwareStatusCard.hBoxLayout.addSpacing(16)

        # EAC 自动转码开关
        self.vrEacAutoConvertCard = InlineSwitchCard(
            FluentIcon.VIDEO,
            self.tr("EAC 自动转码"),
            self.tr(
                "检测到 YouTube 专用 EAC 投影格式时，自动转换为通用的 Equirectangular 格式（耗时较长）"
            ),
            parent=self.vrGroup,
        )
        self.vrEacAutoConvertCard.checkedChanged.connect(self._on_vr_eac_auto_convert_changed)

        # 硬件加速策略
        self.vrHwAccelCard = InlineComboBoxCard(
            FluentIcon.SPEED_HIGH,
            self.tr("硬件加速策略"),
            self.tr("选择转码时的硬件加速模式"),
            [self.tr("自动 (推荐)"), self.tr("强制 CPU (慢)"), self.tr("强制 GPU (快)")],
            self.vrGroup,
        )
        self.vrHwAccelCard.comboBox.currentIndexChanged.connect(self._on_vr_hw_accel_changed)

        # 最大分辨率限制
        self.vrMaxResolutionCard = InlineComboBoxCard(
            FluentIcon.ZOOM,
            self.tr("最大转码分辨率"),
            self.tr("超过此分辨率的视频将跳过转码（防止内存溢出或死机）"),
            [self.tr("4K (2160p) - 安全"), self.tr("5K/6K - 警告"), self.tr("8K (4320p) - 高危")],
            self.vrGroup,
        )
        self.vrMaxResolutionCard.comboBox.currentIndexChanged.connect(
            self._on_vr_max_resolution_changed
        )

        # CPU 占用限制
        self.vrCpuPriorityCard = InlineComboBoxCard(
            FluentIcon.IOT,
            self.tr("转码性能模式"),
            self.tr("控制 CPU 占用率和系统响应速度"),
            [self.tr("低 (后台不卡顿)"), self.tr("中 (均衡)"), self.tr("高 (全速)")],
            self.vrGroup,
        )
        self.vrCpuPriorityCard.comboBox.currentIndexChanged.connect(
            self._on_vr_cpu_priority_changed
        )

        # 保留原片
        self.vrKeepSourceCard = InlineSwitchCard(
            FluentIcon.SAVE,
            self.tr("转码后保留原片"),
            self.tr("防止转码失败导致源文件丢失"),
            parent=self.vrGroup,
        )
        self.vrKeepSourceCard.checkedChanged.connect(self._on_vr_keep_source_changed)

        self.vrGroup.addSettingCard(self.vrHardwareStatusCard)
        self.vrGroup.addSettingCard(self.vrEacAutoConvertCard)
        self.vrGroup.addSettingCard(self.vrHwAccelCard)
        self.vrGroup.addSettingCard(self.vrMaxResolutionCard)
        self.vrGroup.addSettingCard(self.vrCpuPriorityCard)
        self.vrGroup.addSettingCard(self.vrKeepSourceCard)
        layout.addWidget(self.vrGroup)

        # 初始化状态
        self._update_vr_hardware_status()

    def _indent_setting_card(self, card: QWidget, left: int = 32) -> None:
        """Indent a setting card to visually indicate it depends on another option."""
        try:
            layout = getattr(card, "hBoxLayout", None) or card.layout()
            if not layout:
                return
            m = layout.contentsMargins()
            layout.setContentsMargins(left, m.top(), m.right(), m.bottom())
        except Exception:
            from ...utils.logger import logger

            logger.exception("Swallowed exception in settings")

    @staticmethod
    def _fix_windows_path(text: str) -> str:
        """去除复制路径时常见的引号并清理空白。"""
        s = str(text or "").strip()
        # remove surrounding and embedded quotes
        s = s.replace('"', "").replace("'", "").strip()
        s = os.path.expandvars(s)
        return s

    @staticmethod
    def _validate_optional_exe_path(text: str) -> tuple[bool, str]:
        """校验可选的可执行文件路径：允许为空；非空则必须存在。

        Windows 上额外要求 .exe 结尾（避免把目录/文本误当成可执行文件）。
        """
        s = str(text or "").strip()
        if not s:
            return True, ""
        s = os.path.expandvars(s)
        if not os.path.exists(s):
            return False, "文件不存在，请检查路径是否正确"
        if os.name == "nt" and not s.lower().endswith(".exe"):
            return False, "这看起来不是一个 .exe 文件"
        return True, ""

    def _on_pot_provider_toggled(self, checked: bool) -> None:
        """开关切换后立即在后台预热/停服，无需重启（预热不阻塞 UI）。"""
        config_manager.set("pot_provider_enabled", checked)
        try:
            from fluentytdl.youtube.pot_manager import pot_manager

            if checked:
                pot_manager.ensure_warm_async()
            else:
                pot_manager.stop_server()
        except Exception as e:
            logger.warning(f"[POT] 开关切换处理失败: {e}")
        self._refresh_pot_health_content()

    _POT_CARD_HINT = "后台预热，不阻塞启动与解析；未就绪时自动降级为无 POT 解析。默认关闭。"

    def _refresh_pot_health_content(self) -> None:
        """把 POT 实时状态摘要写进卡片描述。

        只调 `status_brief()`（纯内存读），绝不调 `get_health_status()` —— 后者会
        真去铸一次 Token，最坏 15s，放在定时器里等于每 2 秒冻一次 UI。

        单行输出：`SettingCard` 在构造时就 `setFixedHeight(70)`，多行会被裁掉。
        """
        card = getattr(self, "potProviderEnabledCard", None)
        if card is None:
            return
        if not card.isVisible() and getattr(self, "_pot_health_seen", False):
            return
        try:
            from fluentytdl.youtube.pot_manager import pot_manager

            brief = pot_manager.status_brief()
        except Exception as e:
            brief = f"状态不可用（{e}）"
        self._pot_health_seen = True
        text = f"状态：{brief} · {self._POT_CARD_HINT}"
        if card.contentLabel.text() != text:
            card.setContent(text)

    def _on_pot_diagnose_clicked(self) -> None:
        """一键检测：健康状态 + 插件就位 + deno + `-v` 主动探测，全在子线程跑。"""
        if getattr(self, "_pot_diagnose_worker", None) is not None:
            return
        if not config_manager.get("pot_provider_enabled", False):
            InfoBar.warning(
                self.tr("POT 未启用"),
                self.tr("请先打开 POT 验证引擎开关，等待预热后再检测。"),
                duration=5000,
                parent=self,
            )
            return
        self._start_pot_diagnose(recover=False)

    def _start_pot_diagnose(self, *, recover: bool) -> None:
        card = self.potProviderEnabledCard
        card.actionButton.setEnabled(False)
        card.actionButton.setText(self.tr("修复中…") if recover else self.tr("检测中…"))
        InfoBar.info(
            self.tr("正在检测 POT"),
            self.tr("会实际铸一次 Token 并跑一次带 -v 的 yt-dlp 探测，可能需要几十秒。"),
            duration=5000,
            parent=self,
        )

        worker = PotDiagnoseWorker(self, recover=recover)
        self._pot_diagnose_worker = worker
        worker.finished.connect(self._on_pot_diagnose_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()

    def _on_pot_diagnose_finished(self, report: dict) -> None:
        card = self.potProviderEnabledCard
        card.actionButton.setEnabled(True)
        card.actionButton.setText(self.tr("一键检测"))
        worker = getattr(self, "_pot_diagnose_worker", None)
        self._pot_diagnose_worker = None
        if worker is not None:
            worker.deleteLater()
        self._refresh_pot_health_content()

        if report.get("error"):
            InfoBar.error(self.tr("检测失败"), str(report["error"]), duration=15000, parent=self)
            return

        health = report.get("health") or {}
        lines = self._format_pot_report(report, health)
        overall_ok = bool(health.get("overall_ok")) and bool(report.get("plugin_ok"))
        probe_ok = report.get("probe_ok")
        if probe_ok is False:
            overall_ok = False

        title = self.tr("POT 检测通过") if overall_ok else self.tr("POT 检测发现问题")
        box = MessageBox(title, "\n".join(lines), self.window())
        box.cancelButton.setText(self.tr("关闭"))
        if overall_ok:
            box.yesButton.setText(self.tr("好的"))
            box.exec()
            return

        box.yesButton.setText(self.tr("尝试修复"))
        if box.exec():
            self._start_pot_diagnose(recover=True)

    def _format_pot_report(self, report: dict, health: dict) -> list[str]:
        """把诊断结果拼成人类可读的报告。只出现端口/长度/条目数，绝不含 Token 明文。

        每行都控制在 ~110 字符内：`MessageBox` 内部会对 content 再跑一遍 TextWrap，
        超长行会被折得七零八落。完整探测输出走日志，不塞进对话框。
        """

        def mark(ok: object) -> str:
            if ok is None:
                return "—"
            return "✓" if ok else "✗"

        def clip(s: str, n: int = 110) -> str:
            s = s.strip()
            return s if len(s) <= n else s[: n - 1] + "…"

        lines: list[str] = []
        if report.get("recovered") is not None:
            ok = report["recovered"]
            lines.append(f"{mark(ok)} 自动修复：" + ("成功" if ok else "失败，见下方明细"))
            lines.append("")

        port = health.get("port") or 0
        running = health.get("running")
        lines.append(
            f"{mark(running)} 服务进程："
            + (f"运行中（127.0.0.1:{port}）" if running and port else "未运行")
        )
        lines.append(
            f"{mark(health.get('token_ok'))} Token 生成：{clip(health.get('token_detail') or '—')}"
        )
        lines.append(
            f"{mark(health.get('minter_ok'))} Minter 缓存：{clip(health.get('minter_detail') or '—')}"
        )
        lines.append(
            f"{mark(report.get('plugin_ok'))} yt-dlp 插件：{clip(report.get('plugin_detail') or '—')}"
        )
        lines.append(
            f"{mark(report.get('deno_ok'))} JS Runtime (Deno)："
            + (
                "已就位"
                if report.get("deno_ok")
                else "未找到 —— POT 可能铸不出 Token，请在「核心组件」安装"
            )
        )

        probe_ok = report.get("probe_ok")
        detail = str(report.get("probe_detail") or "")
        lines.append("")
        lines.append(f"{mark(probe_ok)} yt-dlp 主动探测（-v，验证插件被加载且 provider 被选中）：")
        if probe_ok:
            head = [ln for ln in detail.splitlines() if "bgutil" in ln.lower()][:3]
            lines.extend(f"  {clip(ln)}" for ln in (head or detail.splitlines()[:3]))
            lines.append("  完整输出见日志。")
        else:
            lines.extend(f"  {clip(ln)}" for ln in detail.splitlines()[:6])

        cache_size = health.get("minter_cache_size")
        if cache_size is not None:
            lines.append("")
            lines.append(f"交叉验证：minter 缓存现有 {cache_size} 条。")
            lines.append("多次解析后它始终不涨，说明 Token 根本没被 yt-dlp 请求。")

        logger.info(
            "[POT][Diagnose] running={} token_ok={} minter_ok={} plugin_ok={} deno={} probe={}",
            health.get("running"),
            health.get("token_ok"),
            health.get("minter_ok"),
            report.get("plugin_ok"),
            report.get("deno_ok"),
            probe_ok,
        )
        if detail:
            logger.info("[POT][Diagnose] 探测输出:\n{}", detail[:4000])
        return lines

    @staticmethod
    def _validate_po_token(text: str) -> tuple[bool, str]:
        """PO Token 简单格式校验：允许为空；非空时做保守检查。"""
        s = str(text or "").strip()
        if not s:
            return True, ""
        low = s.lower()
        if "mweb" not in low and "visitor" not in low:
            return False, "Token 格式看起来不对（通常包含 'mweb' 或 'visitor'）"
        return True, ""

    def _init_behavior_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.behaviorGroup = SettingCardGroup(self.tr("行为策略"), parent_widget)

        self.deletionPolicyCard = InlineComboBoxCard(
            FluentIcon.DELETE,
            self.tr("移除任务时的默认行为"),
            self.tr("选择从列表中删除任务时的文件处理策略"),
            [
                self.tr("每次询问 (默认)"),
                self.tr("仅移除记录 (保留文件)"),
                self.tr("彻底删除 (同时删除文件)"),
            ],
            self.behaviorGroup,
        )
        self.deletionPolicyCard.comboBox.currentIndexChanged.connect(
            self._on_deletion_policy_changed
        )

        self.playlistSkipAuthcheckCard = InlineSwitchCard(
            FluentIcon.VIDEO,
            self.tr("加速播放列表解析（实验性）"),
            self.tr(
                "跳过 YouTube 登录验证检查（authcheck）。可加快大列表解析，但可能导致部分受限视频无法解析（默认关闭）"
            ),
            parent=self.behaviorGroup,
        )
        self.playlistSkipAuthcheckCard.checkedChanged.connect(
            self._on_playlist_skip_authcheck_changed
        )

        self.behaviorGroup.addSettingCard(self.deletionPolicyCard)
        self.behaviorGroup.addSettingCard(self.playlistSkipAuthcheckCard)

        layout.addWidget(self.behaviorGroup)

    def _init_postprocess_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化后处理设置组（封面嵌入、元数据等）"""
        self.postprocessGroup = SettingCardGroup(self.tr("后处理"), parent_widget)

        # 独立封面下载开关
        self.downloadThumbnailCard = InlineSwitchCard(
            FluentIcon.PHOTO,
            self.tr("独立封面"),
            self.tr("将视频封面作为独立的图片文件保存"),
            parent=self.postprocessGroup,
        )
        self.downloadThumbnailCard.checkedChanged.connect(self._on_download_thumbnail_changed)

        # 封面嵌入开关
        self.embedThumbnailCard = InlineSwitchCard(
            FluentIcon.PHOTO,
            self.tr("嵌入封面图片"),
            self.tr(
                "将视频缩略图嵌入到下载文件中作为封面（支持 MP4/MKV/MP3/M4A/FLAC/OGG/OPUS 等格式）"
            ),
            parent=self.postprocessGroup,
        )
        self.embedThumbnailCard.checkedChanged.connect(self._on_embed_thumbnail_changed)

        # 元数据嵌入开关
        self.embedMetadataCard = InlineSwitchCard(
            FluentIcon.TAG,
            self.tr("嵌入元数据"),
            self.tr("将视频标题、作者、描述等信息嵌入到下载文件中（推荐开启）"),
            parent=self.postprocessGroup,
        )
        self.embedMetadataCard.checkedChanged.connect(self._on_embed_metadata_changed)

        self.postprocessGroup.addSettingCard(self.downloadThumbnailCard)
        self.postprocessGroup.addSettingCard(self.embedThumbnailCard)
        self.postprocessGroup.addSettingCard(self.embedMetadataCard)

        # === SponsorBlock 广告跳过 ===
        # 主开关
        self.sponsorBlockCard = InlineSwitchCard(
            FluentIcon.CANCEL,
            self.tr("SponsorBlock 广告跳过"),
            self.tr("自动跳过视频中的赞助广告、自我推广等片段（基于社区标注）"),
            parent=self.postprocessGroup,
        )
        self.sponsorBlockCard.checkedChanged.connect(self._on_sponsorblock_changed)

        # 类别选择（点击按钮打开对话框）
        self.sponsorBlockCategoriesCard = SettingCard(
            FluentIcon.SETTING,
            self.tr("跳过类别设置"),
            self._get_sponsorblock_categories_text(),
            parent=self.postprocessGroup,
        )

        # 添加选择按钮
        self._sponsorBlockCategoriesBtn = PushButton("选择类别")
        self._sponsorBlockCategoriesBtn.clicked.connect(self._show_sponsorblock_categories_dialog)
        self.sponsorBlockCategoriesCard.hBoxLayout.addWidget(self._sponsorBlockCategoriesBtn)
        self.sponsorBlockCategoriesCard.hBoxLayout.addSpacing(16)

        # 类别复选框容器（用于对话框）
        self._sponsorblock_checkboxes: dict[str, CheckBox] = {}

        # 添加到组
        self.postprocessGroup.addSettingCard(self.sponsorBlockCard)
        self.postprocessGroup.addSettingCard(self.sponsorBlockCategoriesCard)

        # 缩进类别卡片
        self._indent_setting_card(self.sponsorBlockCategoriesCard)

        layout.addWidget(self.postprocessGroup)

    def _init_subtitle_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化字幕配置组"""
        self.subtitleGroup = SettingCardGroup(self.tr("字幕下载"), parent_widget)

        # 字幕启用开关
        self.subtitleEnabledCard = InlineSwitchCard(
            FluentIcon.DOCUMENT,
            self.tr("启用字幕下载"),
            self.tr("自动下载视频字幕（支持多语言、嵌入、双语合成）"),
            parent=self.subtitleGroup,
        )
        self.subtitleEnabledCard.checkedChanged.connect(self._on_subtitle_enabled_changed)

        # 语言多选卡片 (NEW)
        config = config_manager.get_subtitle_config()
        current_languages = config.default_languages if config.default_languages else []
        self.subtitleLanguagesCard = LanguageMultiSelectCard(
            FluentIcon.GLOBE,
            self.tr("字幕语言"),
            self.tr("选择要下载的字幕语言（可多选）"),
            languages=COMMON_SUBTITLE_LANGUAGES,
            selected_default=current_languages,
            parent=self.subtitleGroup,
        )
        self.subtitleLanguagesCard.selectionChanged.connect(self._on_subtitle_languages_changed)

        # 字幕类型偏好 (NEW)
        self.subtitleTypePrefCard = InlineComboBoxCard(
            FluentIcon.FILTER,
            self.tr("字幕类型偏好"),
            self.tr("自动选择字幕时的策略"),
            [
                self.tr("仅手动上传的字幕"),
                self.tr("手动字幕优先，自动生成字幕垫底"),
                self.tr("所有类型（含自动翻译）"),
            ],
            parent=self.subtitleGroup,
        )
        self.subtitleTypePrefCard.comboBox.currentIndexChanged.connect(
            self._on_subtitle_type_pref_changed
        )

        # 嵌入类型下拉框卡片 (NEW)
        self.subtitleEmbedTypeCard = EmbedTypeComboCard(
            FluentIcon.VIDEO,
            self.tr("嵌入类型"),
            self.tr("选择字幕的封装方式"),
            default=config.embed_type,
            parent=self.subtitleGroup,
        )
        self.subtitleEmbedTypeCard.valueChanged.connect(self._on_subtitle_embed_type_changed)

        # 字幕输出格式
        self.subtitleFormatCard = InlineComboBoxCard(
            FluentIcon.FONT,
            self.tr("字幕输出格式"),
            self.tr("所有字幕（嵌入/外置/纯字幕下载）的默认转换目标格式"),
            [self.tr("SRT (推荐)"), self.tr("ASS (支持样式)"), self.tr("VTT (Web原生)")],
            parent=self.subtitleGroup,
        )
        self.subtitleFormatCard.comboBox.currentIndexChanged.connect(
            self._on_subtitle_format_changed
        )

        self.subtitleGroup.addSettingCard(self.subtitleEnabledCard)
        self.subtitleGroup.addSettingCard(self.subtitleTypePrefCard)
        self.subtitleGroup.addSettingCard(self.subtitleLanguagesCard)
        self.subtitleGroup.addSettingCard(self.subtitleEmbedTypeCard)

        self.subtitleGroup.addSettingCard(self.subtitleFormatCard)

        # 缩进依赖项
        self._indent_setting_card(self.subtitleTypePrefCard)
        self._indent_setting_card(self.subtitleLanguagesCard)
        self._indent_setting_card(self.subtitleEmbedTypeCard)

        self._indent_setting_card(self.subtitleFormatCard)

        layout.addWidget(self.subtitleGroup)

    def _init_about_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.aboutGroup = SettingCardGroup(self.tr("关于"), parent_widget)
        self.aboutCard = HyperlinkCard(
            "https://github.com/SakuraForgot/FluentYTDL",
            self.tr("访问项目仓库"),
            FluentIcon.GITHUB,
            "FluentYTDL",
            self.tr("基于 PySide6 & Fluent Design 构建"),
            self.aboutGroup,
        )
        self.aboutGroup.addSettingCard(self.aboutCard)
        layout.addWidget(self.aboutGroup)

    def _init_log_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化日志管理组"""
        self.logGroup = SettingCardGroup(self.tr("日志管理"), parent_widget)

        # 日志管理卡片
        self.logCard = SettingCard(
            FluentIcon.DOCUMENT,
            self.tr("运行日志"),
            self.tr("日志目录: {}").format(LOG_DIR),
            self.logGroup,
        )

        # 添加按钮到卡片
        self.viewLogBtn = PushButton(self.tr("查看日志"), self.logCard)
        self.viewLogBtn.clicked.connect(self._on_view_log_clicked)

        self.openLogDirBtn = ToolButton(FluentIcon.FOLDER, self.logCard)
        self.openLogDirBtn.setToolTip(self.tr("打开日志目录"))
        self.openLogDirBtn.installEventFilter(
            ToolTipFilter(self.openLogDirBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.openLogDirBtn.clicked.connect(self._on_open_log_dir)

        self.cleanLogBtn = ToolButton(FluentIcon.DELETE, self.logCard)
        self.cleanLogBtn.setToolTip(self.tr("清理所有日志"))
        self.cleanLogBtn.installEventFilter(
            ToolTipFilter(self.cleanLogBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.cleanLogBtn.clicked.connect(self._on_clean_log_clicked)

        self.logCard.hBoxLayout.addWidget(self.viewLogBtn, 0, Qt.AlignmentFlag.AlignRight)
        self.logCard.hBoxLayout.addSpacing(8)
        self.logCard.hBoxLayout.addWidget(self.openLogDirBtn, 0, Qt.AlignmentFlag.AlignRight)
        self.logCard.hBoxLayout.addSpacing(8)
        self.logCard.hBoxLayout.addWidget(self.cleanLogBtn, 0, Qt.AlignmentFlag.AlignRight)
        self.logCard.hBoxLayout.addSpacing(16)

        self.logGroup.addSettingCard(self.logCard)
        layout.addWidget(self.logGroup)

    def _on_view_log_clicked(self):
        """打开日志查看器"""
        from fluentytdl.ui.components.dialogs.log_viewer_dialog import LogViewerDialog

        dialog = LogViewerDialog(self.window())
        dialog.exec()

    def _on_open_log_dir(self):
        """打开日志目录"""
        try:
            if os.path.exists(LOG_DIR):
                os.startfile(LOG_DIR)
            else:
                InfoBar.warning(
                    self.tr("目录不存在"),
                    self.tr("{} 尚未创建").format(LOG_DIR),
                    parent=self.window(),
                )
        except Exception as e:
            InfoBar.error(self.tr("错误"), str(e), parent=self.window())

    def _on_clean_log_clicked(self):
        """清理所有日志"""
        from qfluentwidgets import MessageBox

        box = MessageBox(
            self.tr("确认清理"),
            self.tr("确定要删除所有日志文件吗？\n\n日志目录: {}").format(LOG_DIR),
            self.window(),
        )
        if box.exec():
            try:
                if os.path.exists(LOG_DIR):
                    import shutil

                    for f in os.listdir(LOG_DIR):
                        fp = os.path.join(LOG_DIR, f)
                        try:
                            if os.path.isfile(fp):
                                os.remove(fp)
                            elif os.path.isdir(fp):
                                shutil.rmtree(fp)
                        except Exception:
                            from ...utils.logger import logger

                            logger.exception("Swallowed exception in settings")
                    InfoBar.info(
                        self.tr("清理完成"), self.tr("已删除所有日志文件"), parent=self.window()
                    )
                else:
                    InfoBar.info(
                        self.tr("无需清理"), self.tr("日志目录不存在"), parent=self.window()
                    )
            except Exception as e:
                InfoBar.error(self.tr("清理失败"), str(e), parent=self.window())

    def _load_settings_to_ui(self) -> None:
        # Download paths
        self.downloadFolderCard.setContent(str(config_manager.get("download_dir")))

        # Concurrent Fragments
        current_frag = config_manager.get("concurrent_fragments", 4)
        self.concurrentFragmentsCard.comboBox.blockSignals(True)
        self.concurrentFragmentsCard.comboBox.setCurrentIndex(max(0, min(7, int(current_frag) - 1)))
        self.concurrentFragmentsCard.comboBox.blockSignals(False)

        # Update Source
        src = str(config_manager.get("update_source") or "github")
        src_idx = 1 if src == "ghproxy" else 0
        self.updateSourceCard.comboBox.blockSignals(True)
        self.updateSourceCard.comboBox.setCurrentIndex(src_idx)
        self.updateSourceCard.comboBox.blockSignals(False)

        # yt-dlp Update Channel
        channel_map = ["stable", "nightly", "master"]
        current_ch = str(config_manager.get("ytdlp_channel", "stable")).strip().lower()
        ch_idx = channel_map.index(current_ch) if current_ch in channel_map else 0
        self.ytDlpChannelCard.comboBox.blockSignals(True)
        self.ytDlpChannelCard.comboBox.setCurrentIndex(ch_idx)
        self.ytDlpChannelCard.comboBox.blockSignals(False)

        # Auto update switch
        auto_check = bool(config_manager.get("check_updates_on_startup", True))
        self.checkUpdatesOnStartupCard.switchButton.blockSignals(True)
        self.checkUpdatesOnStartupCard.switchButton.setChecked(auto_check)
        self.checkUpdatesOnStartupCard.switchButton.blockSignals(False)

        # Clipboard action mode
        action_mode = str(config_manager.get("clipboard_action_mode", "smart"))
        action_idx_map = {"smart": 0, "standard": 1, "vr": 2, "subtitle": 3, "cover": 4}
        self.clipboardActionModeCard.comboBox.blockSignals(True)
        self.clipboardActionModeCard.comboBox.setCurrentIndex(action_idx_map.get(action_mode, 0))
        self.clipboardActionModeCard.comboBox.blockSignals(False)

        # Clipboard window to front
        to_front = bool(config_manager.get("clipboard_window_to_front", True))
        self.clipboardWindowToFrontCard.switchButton.blockSignals(True)
        self.clipboardWindowToFrontCard.switchButton.setChecked(to_front)
        self.clipboardWindowToFrontCard.switchButton.blockSignals(False)

        # Proxy mode -> combobox index
        proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
        proxy_index_map = {"off": 0, "system": 1, "http": 2, "socks5": 3}
        self.proxyModeCard.comboBox.blockSignals(True)
        self.proxyModeCard.comboBox.setCurrentIndex(proxy_index_map.get(proxy_mode, 0))
        self.proxyModeCard.comboBox.blockSignals(False)
        self._update_proxy_edit_visibility()
        self.proxyEditCard.lineEdit.setText(
            str(config_manager.get("proxy_url") or "127.0.0.1:7890")
        )

        # Cookie 配置从 auth_service 加载
        from ..auth.auth_service import AuthSourceType, auth_service

        current_source = auth_service.current_source

        self.cookieModeCard.comboBox.blockSignals(True)
        self.browserCard.comboBox.blockSignals(True)

        # 设置 Cookie 清洗开关
        cleaning_enabled = bool(config_manager.get("cookie_cleaning_enabled", True))
        self.cookieCleaningCard.switchButton.blockSignals(True)
        self.cookieCleaningCard.switchButton.setChecked(cleaning_enabled)
        self.cookieCleaningCard.switchButton.blockSignals(False)

        # 设置 Cookie 模式
        if current_source == AuthSourceType.FILE:
            self.cookieModeCard.comboBox.setCurrentIndex(2)  # 手动文件
            if auth_service._current_file_path:
                self.cookieFileCard.setContent(auth_service._current_file_path)
        elif current_source == AuthSourceType.WEBVIEW2:
            self.cookieModeCard.comboBox.setCurrentIndex(1)  # 登录获取
        else:
            self.cookieModeCard.comboBox.setCurrentIndex(0)  # 自动提取

            # 设置浏览器（顺序与UI一致）
            browser_map = {
                AuthSourceType.EDGE: 0,
                AuthSourceType.CHROME: 1,
                AuthSourceType.CHROMIUM: 2,
                AuthSourceType.BRAVE: 3,
                AuthSourceType.OPERA: 4,
                AuthSourceType.OPERA_GX: 5,
                AuthSourceType.VIVALDI: 6,
                AuthSourceType.ARC: 7,
                AuthSourceType.FIREFOX: 8,
                AuthSourceType.LIBREWOLF: 9,
                AuthSourceType.CENT: 10,
            }
            browser_idx = browser_map.get(current_source, 0)
            self.browserCard.comboBox.setCurrentIndex(browser_idx)

        self.cookieModeCard.comboBox.blockSignals(False)
        self.browserCard.comboBox.blockSignals(False)

        # 加载 WebView2 账号列表
        self._reload_webview2_account_combo(select_current=True)

        # 触发可见性更新 (Cookie sub-options)
        self._on_cookie_mode_changed(self.cookieModeCard.comboBox.currentIndex())

        self.poTokenCard.setValue(str(config_manager.get("youtube_po_token") or ""))

        # Automatic update check (frequency control)
        # Only check if enabled in settings
        if config_manager.get("check_updates_on_startup", True):
            last_check = float(config_manager.get("last_update_check") or 0)
            now = time.time()
            # Check if 24 hours (86400 seconds) have passed.
            if now - last_check > 86400:
                # 检查 app-core 更新（通过 ComponentUpdateManager）
                self.appUpdateCard.check_for_update()
                # 检查 bin/ 工具更新
                dependency_manager.check_update("yt-dlp")
                dependency_manager.check_update("ffmpeg")
                dependency_manager.check_update("deno")
                dependency_manager.check_update("pot-provider")
                config_manager.set("last_update_check", now)

        self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
        self.jsRuntimePathCard.setValue(str(config_manager.get("js_runtime_path") or ""))

        # JS runtime -> combobox index
        js_runtime = str(config_manager.get("js_runtime") or "auto").lower().strip()
        js_index_map = {"auto": 0, "deno": 1, "node": 2, "bun": 3, "quickjs": 4}
        self.jsRuntimeCard.comboBox.blockSignals(True)
        self.jsRuntimeCard.comboBox.setCurrentIndex(js_index_map.get(js_runtime, 0))
        self.jsRuntimeCard.comboBox.blockSignals(False)

        # Theme Mode -> combobox index
        theme_mode = str(config_manager.get("theme_mode") or "Auto")
        theme_index_map = {"Auto": 0, "Light": 1, "Dark": 2}
        self.themeModeCard.comboBox.blockSignals(True)
        self.themeModeCard.comboBox.setCurrentIndex(theme_index_map.get(theme_mode, 0))
        self.themeModeCard.comboBox.blockSignals(False)

        # Clipboard auto-detect
        enabled = bool(config_manager.get("clipboard_auto_detect") or False)
        self.clipboardDetectCard.switchButton.blockSignals(True)
        self.clipboardDetectCard.switchButton.setChecked(enabled)
        self.clipboardDetectCard.switchButton.blockSignals(False)

        # Deletion Policy
        policy = str(config_manager.get("deletion_policy") or "AlwaysAsk")
        # Combo box texts order: ["每次询问 (默认)", "仅移除记录 (保留文件)", "彻底删除 (同时删除文件)"]
        # Map config values to the correct indices
        policy_map = {"AlwaysAsk": 0, "KeepFiles": 1, "DeleteFiles": 2}
        self.deletionPolicyCard.comboBox.blockSignals(True)
        self.deletionPolicyCard.comboBox.setCurrentIndex(policy_map.get(policy, 0))
        self.deletionPolicyCard.comboBox.blockSignals(False)

        # Preferred Audio Languages (Array Selection)
        audio_langs = config_manager.get("preferred_audio_languages")
        if not isinstance(audio_langs, list):
            audio_langs = ["orig", "zh-Hans", "en"]
        self.preferredAudioLanguageCard.set_selected_languages(audio_langs)

        # Playlist: skip authcheck
        skip_authcheck = bool(config_manager.get("playlist_skip_authcheck") or False)
        self.playlistSkipAuthcheckCard.switchButton.blockSignals(True)
        self.playlistSkipAuthcheckCard.switchButton.setChecked(skip_authcheck)
        self.playlistSkipAuthcheckCard.switchButton.blockSignals(False)

        # Postprocess: download thumbnail
        download_thumbnail = bool(config_manager.get("download_thumbnail", False))
        self.downloadThumbnailCard.switchButton.blockSignals(True)
        self.downloadThumbnailCard.switchButton.setChecked(download_thumbnail)
        self.downloadThumbnailCard.switchButton.blockSignals(False)

        # Postprocess: embed thumbnail
        embed_thumbnail = bool(config_manager.get("embed_thumbnail", False))
        self.embedThumbnailCard.switchButton.blockSignals(True)
        self.embedThumbnailCard.switchButton.setChecked(embed_thumbnail)
        self.embedThumbnailCard.switchButton.blockSignals(False)

        # Postprocess: embed metadata
        embed_metadata = bool(config_manager.get("embed_metadata", True))
        self.embedMetadataCard.switchButton.blockSignals(True)
        self.embedMetadataCard.switchButton.setChecked(embed_metadata)
        self.embedMetadataCard.switchButton.blockSignals(False)

        # SponsorBlock: enabled switch
        sponsorblock_enabled = bool(config_manager.get("sponsorblock_enabled", False))
        self.sponsorBlockCard.switchButton.blockSignals(True)
        self.sponsorBlockCard.switchButton.setChecked(sponsorblock_enabled)
        self.sponsorBlockCard.switchButton.blockSignals(False)

        # SponsorBlock: 更新类别卡片描述和可见性
        self.sponsorBlockCategoriesCard.setContent(self._get_sponsorblock_categories_text())
        self._update_sponsorblock_categories_visibility(sponsorblock_enabled)

        # Subtitle: enabled switch
        subtitle_enabled = bool(config_manager.get("subtitle_enabled", False))
        self.subtitleEnabledCard.switchButton.blockSignals(True)
        self.subtitleEnabledCard.switchButton.setChecked(subtitle_enabled)
        self.subtitleEnabledCard.switchButton.blockSignals(False)

        # Subtitle: languages (NEW - 加载到多选卡片)
        subtitle_config = config_manager.get_subtitle_config()
        subtitle_languages = (
            subtitle_config.default_languages
            if subtitle_config.default_languages
            else ["zh-Hans", "en"]
        )
        # 不需要阻塞信号，因为 set_selected_languages 不会触发信号
        self.subtitleLanguagesCard.set_selected_languages(subtitle_languages)

        # Subtitle: type preference
        type_pref = subtitle_config.type_preference.value
        pref_idx_map = {"manual_only": 0, "manual_and_asr": 1, "all": 2}
        self.subtitleTypePrefCard.comboBox.blockSignals(True)
        self.subtitleTypePrefCard.comboBox.setCurrentIndex(pref_idx_map.get(type_pref, 1))
        self.subtitleTypePrefCard.comboBox.blockSignals(False)

        # Subtitle: embed type (NEW)
        self.subtitleEmbedTypeCard.comboBox.blockSignals(True)
        self.subtitleEmbedTypeCard.set_value(subtitle_config.embed_type)
        self.subtitleEmbedTypeCard.comboBox.blockSignals(False)

        # Subtitle: output format
        output_format = str(config_manager.get("subtitle_output_format", "vtt"))
        format_idx_map = {"srt": 0, "ass": 1, "vtt": 2}
        self.subtitleFormatCard.comboBox.blockSignals(True)
        self.subtitleFormatCard.comboBox.setCurrentIndex(format_idx_map.get(output_format, 0))
        self.subtitleFormatCard.comboBox.blockSignals(False)

        # VR Settings
        self.vrEacAutoConvertCard.switchButton.blockSignals(True)
        self.vrEacAutoConvertCard.switchButton.setChecked(
            config_manager.get("vr_eac_auto_convert", False)
        )
        self.vrEacAutoConvertCard.switchButton.blockSignals(False)

        # Quick Mode Settings
        quick_thresh = str(config_manager.get("quick_playlist_expand_threshold", 50))
        try:
            th_idx = ["10", "30", "50", "100", "200"].index(quick_thresh)
        except ValueError:
            th_idx = 2
        self.quickPlaylistExpandThresholdCard.comboBox.blockSignals(True)
        self.quickPlaylistExpandThresholdCard.comboBox.setCurrentIndex(th_idx)
        self.quickPlaylistExpandThresholdCard.comboBox.blockSignals(False)

        quick_max = str(config_manager.get("quick_max_total_tasks", 500))
        try:
            mt_idx = ["100", "300", "500", "1000", "无限制"].index(quick_max)
        except ValueError:
            mt_idx = 2
        self.quickMaxTotalTasksCard.comboBox.blockSignals(True)
        self.quickMaxTotalTasksCard.comboBox.setCurrentIndex(mt_idx)
        self.quickMaxTotalTasksCard.comboBox.blockSignals(False)

        vr_hw_mode = str(config_manager.get("vr_hw_accel_mode", "auto"))
        hw_mode_map = {"auto": 0, "cpu": 1, "gpu": 2}
        self.vrHwAccelCard.comboBox.blockSignals(True)
        self.vrHwAccelCard.comboBox.setCurrentIndex(hw_mode_map.get(vr_hw_mode, 0))
        self.vrHwAccelCard.comboBox.blockSignals(False)

        vr_max_res = int(config_manager.get("vr_max_resolution", 2160))
        res_map = {2160: 0, 3200: 1, 4320: 2}
        self.vrMaxResolutionCard.comboBox.blockSignals(True)
        self.vrMaxResolutionCard.comboBox.setCurrentIndex(res_map.get(vr_max_res, 0))
        self.vrMaxResolutionCard.comboBox.blockSignals(False)

        vr_cpu_pri = str(config_manager.get("vr_cpu_priority", "low"))
        cpu_map = {"low": 0, "medium": 1, "high": 2}
        self.vrCpuPriorityCard.comboBox.blockSignals(True)
        self.vrCpuPriorityCard.comboBox.setCurrentIndex(cpu_map.get(vr_cpu_pri, 0))
        self.vrCpuPriorityCard.comboBox.blockSignals(False)

        self.vrKeepSourceCard.switchButton.blockSignals(True)
        self.vrKeepSourceCard.switchButton.setChecked(config_manager.get("vr_keep_source", True))
        self.vrKeepSourceCard.switchButton.blockSignals(False)

        # Update subtitle settings visibility
        self._update_subtitle_settings_visibility(subtitle_enabled)

    def _on_max_concurrent_changed(self, index: int):
        val = index + 1
        config_manager.set("max_concurrent_downloads", val)

        # Risk warning
        if val > 3:
            self.maxConcurrentCard.setContent(
                self.tr("⚠️ 当前: {} (高风险! 可能导致 YouTube 封禁 IP 429)").format(val)
            )
            self.maxConcurrentCard.setTitle(self.tr("最大同时下载数 (慎用)"))
        else:
            self.maxConcurrentCard.setContent(self.tr("当前: {}").format(val))
            self.maxConcurrentCard.setTitle(self.tr("最大同时下载数"))

        # Immediately apply new limit to pending queue
        download_manager.pump()

    def _on_playlist_extract_concurrency_changed(self, index: int) -> None:
        vals = [1, 2, 3, 5, 8, 12, 16]
        if 0 <= index < len(vals):
            new_val = vals[index]
            config_manager.set("playlist_extract_concurrency", new_val)
            from ...download.extract_manager import extract_manager

            extract_manager.set_concurrency(new_val)
            # update tooltip / warning text
            if new_val > 5:
                self.playlistExtractConcurrencyCard.setContent(
                    self.tr("⚠️ 当前: {} (高风险! 极易导致 429 请求过多)").format(new_val)
                )
                self.playlistExtractConcurrencyCard.setTitle("播放列表解析并发 (慎用)")
            else:
                self.playlistExtractConcurrencyCard.setContent(self.tr("当前: {}").format(new_val))
                self.playlistExtractConcurrencyCard.setTitle("播放列表解析并发")

    def _on_parse_cache_ttl_changed(self, index: int) -> None:
        if not 0 <= index < len(self.PARSE_CACHE_TTL_CHOICES):
            return
        new_val = self.PARSE_CACHE_TTL_CHOICES[index]
        config_manager.set("parse_cache_ttl_seconds", new_val)

        if new_val <= 0:
            # 改成「不保留」只是让后续读写都短路，已经存在的条目还占着内存，
            # 顺手清掉才是用户点这一档时期待的结果。
            from ..youtube.youtube_service import youtube_service

            youtube_service.invalidate_parse_cache("解析结果保留时间已关闭")
        # 缩短保留时间不需要清缓存：_parse_cache_get 每次读都拿当前时长比对年龄，
        # 超时的条目会在下一次读取时自然淘汰。

    def _on_update_source_changed(self, index: int) -> None:
        source = "ghproxy" if index == 1 else "github"
        config_manager.set("update_source", source)
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("下载源已切换为: {}").format(source),
            duration=5000,
            parent=self,
        )

    def _on_theme_mode_changed(self, index: int) -> None:
        modes = ["Auto", "Light", "Dark"]
        if 0 <= index < len(modes):
            mode = modes[index]
            config_manager.set("theme_mode", mode)
            import qfluentwidgets

            if mode == "Light":
                qfluentwidgets.setTheme(qfluentwidgets.Theme.LIGHT)
            elif mode == "Dark":
                qfluentwidgets.setTheme(qfluentwidgets.Theme.DARK)
            else:
                qfluentwidgets.setTheme(qfluentwidgets.Theme.AUTO)
            # Immediate UI refresh triggered automatically by setTheme in qfluentwidgets.
            InfoBar.info(
                self.tr("设置已更新"),
                self.tr("主题已切换为: {}").format(mode),
                duration=3000,
                parent=self,
            )

    def _on_check_updates_startup_changed(self, checked: bool) -> None:
        config_manager.set("check_updates_on_startup", bool(checked))
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("已开启启动时自动检查更新") if checked else self.tr("已关闭启动时自动检查更新"),
            duration=5000,
            parent=self,
        )

    def _on_clipboard_detect_changed(self, checked: bool) -> None:
        config_manager.set("clipboard_auto_detect", bool(checked))
        self.clipboardAutoDetectChanged.emit(bool(checked))
        InfoBar.info(
            self.tr("设置已更新"),
            "剪贴板自动识别已开启" if checked else "剪贴板自动识别已关闭",
            duration=5000,
            parent=self,
        )

    def _on_clipboard_window_to_front_changed(self, checked: bool) -> None:
        config_manager.set("clipboard_window_to_front", bool(checked))
        InfoBar.info(
            self.tr("设置已更新"),
            "已开启解析后窗口置顶" if checked else "已关闭解析后窗口置顶",
            duration=5000,
            parent=self,
        )

    def _on_clipboard_action_mode_changed(self, index: int) -> None:
        modes = ["smart", "standard", "vr", "subtitle", "cover"]
        if 0 <= index < len(modes):
            mode = modes[index]
            config_manager.set("clipboard_action_mode", mode)
            InfoBar.info(
                self.tr("设置已更新"),
                self.tr("剪贴板识别行为已更改为: {}").format(mode),
                duration=5000,
                parent=self,
            )

    def _on_deletion_policy_changed(self, index: int) -> None:
        # Combo texts order: Ask, KeepFiles, DeleteFiles
        policies = ["AlwaysAsk", "KeepFiles", "DeleteFiles"]
        if 0 <= index < len(policies):
            policy = policies[index]
            config_manager.set("deletion_policy", policy)
            InfoBar.info(
                self.tr("设置已更新"),
                self.tr("删除策略已更改为: {}").format(policy),
                duration=5000,
                parent=self,
            )

    def _on_playlist_skip_authcheck_changed(self, checked: bool) -> None:
        config_manager.set("playlist_skip_authcheck", bool(checked))
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("已开启：加速播放列表解析（实验性）")
            if checked
            else "已关闭：加速播放列表解析（实验性）",
            duration=5000,
            parent=self,
        )

    def _on_download_thumbnail_changed(self, checked: bool) -> None:
        """处理独立封面下载开关变更"""
        config_manager.set("download_thumbnail", bool(checked))
        if checked and hasattr(self, "embedThumbnailCard"):
            self.embedThumbnailCard.switchButton.setChecked(False)

    def _on_embed_thumbnail_changed(self, checked: bool) -> None:
        """处理封面嵌入开关变更"""
        config_manager.set("embed_thumbnail", bool(checked))
        if checked and hasattr(self, "downloadThumbnailCard"):
            self.downloadThumbnailCard.switchButton.setChecked(False)

        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("已开启封面嵌入（支持 MP4/MKV/MP3/M4A/FLAC/OGG/OPUS 等格式）")
            if checked
            else "已关闭封面嵌入",
            duration=5000,
            parent=self,
        )

    def _on_subtitle_type_pref_changed(self, index: int) -> None:
        from ..models.subtitle_config import SubtitleTypePreference

        modes = [
            SubtitleTypePreference.MANUAL_ONLY,
            SubtitleTypePreference.MANUAL_AND_ASR,
            SubtitleTypePreference.ALL,
        ]
        if 0 <= index < len(modes):
            config = config_manager.get_subtitle_config()
            config.type_preference = modes[index]
            config_manager.set_subtitle_config(config)
            InfoBar.info(
                self.tr("设置已更新"), self.tr("字幕类型偏好已保存"), duration=3000, parent=self
            )

    def _on_embed_metadata_changed(self, checked: bool) -> None:
        """处理元数据嵌入开关变更"""
        config_manager.set("embed_metadata", bool(checked))
        InfoBar.info(
            self.tr("设置已更新"),
            "已开启元数据嵌入（标题、作者、描述等）" if checked else "已关闭元数据嵌入",
            duration=5000,
            parent=self,
        )

    def _on_sponsorblock_changed(self, checked: bool) -> None:
        """处理 SponsorBlock 开关变更"""
        config_manager.set("sponsorblock_enabled", bool(checked))
        self._update_sponsorblock_categories_visibility(checked)

        if checked:
            raw_categories = config_manager.get("sponsorblock_categories", [])
            categories = [c for c in raw_categories if isinstance(c, str) and c]
            if categories:
                cat_names = {
                    "sponsor": "赞助广告",
                    "selfpromo": "自我推广",
                    "interaction": "互动提醒",
                    "intro": "片头",
                    "outro": "片尾",
                    "preview": "预告",
                    "filler": "填充内容",
                    "music_offtopic": "非音乐部分",
                }
                cat_display = ", ".join(cat_names.get(c, c) for c in categories[:3])
                if len(categories) > 3:
                    cat_display += f" 等 {len(categories)} 项"
                InfoBar.info(
                    self.tr("SponsorBlock 已启用"),
                    f"将跳过: {cat_display}",
                    duration=5000,
                    parent=self,
                )
            else:
                InfoBar.warning(
                    self.tr("SponsorBlock 已启用"),
                    self.tr("请在下方选择要跳过的类别"),
                    duration=5000,
                    parent=self,
                )
        else:
            InfoBar.info(
                self.tr("SponsorBlock 已关闭"),
                self.tr("视频将保留原始内容"),
                duration=3000,
                parent=self,
            )

    def _update_sponsorblock_categories_visibility(self, visible: bool) -> None:
        """更新 SponsorBlock 类别卡片的可见性"""
        self.sponsorBlockCategoriesCard.setVisible(visible)

    def _get_sponsorblock_categories_text(self) -> str:
        """获取当前选中的 SponsorBlock 类别的描述文本"""
        raw_categories = config_manager.get(
            "sponsorblock_categories", ["sponsor", "selfpromo", "interaction"]
        )
        categories = [c for c in raw_categories if isinstance(c, str) and c]
        cat_names = {
            "sponsor": "赞助广告",
            "selfpromo": "自我推广",
            "interaction": "互动提醒",
            "intro": "片头",
            "outro": "片尾",
            "preview": "预告",
            "filler": "填充内容",
            "music_offtopic": "非音乐部分",
        }
        if not categories:
            return "未选择任何类别"
        names = [cat_names.get(c, c) for c in categories]
        if len(names) <= 3:
            return "已选择: " + ", ".join(names)
        return f"已选择 {len(names)} 个类别: " + ", ".join(names[:2]) + " 等"

    def _show_sponsorblock_categories_dialog(self) -> None:
        """显示 SponsorBlock 类别选择对话框"""
        from fluentytdl.ui.components.dialogs.sponsorblock_dialog import (
            SponsorBlockCategoriesDialog,
        )

        # 获取当前选中的类别
        current_categories = config_manager.get("sponsorblock_categories", [])

        # 创建并显示对话框
        dialog = SponsorBlockCategoriesDialog(current_categories, self)

        if dialog.exec():
            # 保存选中的类别
            selected = dialog.selected_categories
            config_manager.set("sponsorblock_categories", selected)

            # 更新卡片描述
            self.sponsorBlockCategoriesCard.setContent(self._get_sponsorblock_categories_text())

            if selected:
                InfoBar.info(
                    self.tr("类别已更新"),
                    f"已选择 {len(selected)} 个类别",
                    duration=3000,
                    parent=self,
                )
            else:
                InfoBar.warning(
                    self.tr("未选择类别"),
                    self.tr("请至少选择一个要跳过的类别"),
                    duration=5000,
                    parent=self,
                )

    def _on_proxy_mode_changed(self, index: int) -> None:
        modes = ["off", "system", "http", "socks5"]
        if 0 <= index < len(modes):
            mode = modes[index]
            config_manager.set("proxy_mode", mode)
            # Backward-compat shadow key
            config_manager.set("proxy_enabled", mode in {"http", "socks5"})
            InfoBar.info(
                self.tr("设置已更新"),
                self.tr("代理模式已切换为: {}").format(self.proxyModeCard.comboBox.currentText()),
                duration=5000,
                parent=self,
            )
            self._update_proxy_edit_visibility()

    def _update_proxy_edit_visibility(self) -> None:
        idx = int(self.proxyModeCard.comboBox.currentIndex())
        self.proxyEditCard.setVisible(idx in (2, 3))

    def _on_proxy_url_edited(self) -> None:
        new_proxy = (self.proxyEditCard.lineEdit.text() or "").strip()
        config_manager.set("proxy_url", new_proxy)
        if new_proxy:
            InfoBar.info(
                self.tr("保存成功"),
                self.tr("代理已更新为 {}").format(new_proxy),
                duration=5000,
                parent=self,
            )
        else:
            InfoBar.info(self.tr("已清空"), self.tr("代理地址已清空。"), duration=5000, parent=self)

    def _on_cookie_mode_changed(self, index: int) -> None:
        """Cookie 模式切换：0=浏览器提取, 1=DLE登录获取, 2=手动文件"""
        from ..auth.auth_service import AuthSourceType, auth_service

        if index == 0:
            # 浏览器提取模式
            browser_index = self.browserCard.comboBox.currentIndex()
            browser_map = [
                AuthSourceType.EDGE,
                AuthSourceType.CHROME,
                AuthSourceType.CHROMIUM,
                AuthSourceType.BRAVE,
                AuthSourceType.OPERA,
                AuthSourceType.OPERA_GX,
                AuthSourceType.VIVALDI,
                AuthSourceType.ARC,
                AuthSourceType.FIREFOX,
                AuthSourceType.LIBREWOLF,
                AuthSourceType.CENT,
            ]
            source = (
                browser_map[browser_index]
                if 0 <= browser_index < len(browser_map)
                else AuthSourceType.EDGE
            )
            auth_service.set_source(source, auto_refresh=True)

            self.browserCard.setVisible(True)
            self.browserRefreshCard.setVisible(True)
            self.youtubeAuthCard.setVisible(False)
            self.twitterAuthCard.setVisible(False)
            self.cookieFileCard.setVisible(False)

            InfoBar.info(
                self.tr("已切换到自动提取"),
                f"将从 {auth_service.current_source_display} 自动提取 Cookie",
                duration=3000,
                parent=self,
            )

        elif index == 1:
            # WebView2 登录获取模式
            auth_service.set_source(AuthSourceType.WEBVIEW2, auto_refresh=False)

            self._reload_webview2_account_combo(select_current=True)

            self.browserCard.setVisible(False)
            self.browserRefreshCard.setVisible(False)
            self.youtubeAuthCard.setVisible(True)
            self.twitterAuthCard.setVisible(True)
            self.cookieFileCard.setVisible(False)

            InfoBar.info(
                self.tr("已切换到登录获取模式"),
                self.tr("请点击「启动安全登录」按钮进行账号认证"),
                duration=3000,
                parent=self,
            )

        else:
            # 手动文件模式 (index == 2)
            auth_service.set_source(AuthSourceType.FILE, auto_refresh=False)

            self.browserCard.setVisible(False)
            self.browserRefreshCard.setVisible(False)
            self.youtubeAuthCard.setVisible(False)
            self.twitterAuthCard.setVisible(False)
            self.cookieFileCard.setVisible(True)

            InfoBar.info(
                self.tr("已切换到手动导入"),
                self.tr("请选择 cookies.txt 文件"),
                duration=3000,
                parent=self,
            )

        self._update_cookie_status()

    def _on_cookie_browser_changed(self, index: int) -> None:
        """浏览器选择变化 - 自动提取新浏览器的 Cookies"""
        from qfluentwidgets import MessageBox

        from ..auth.auth_service import AuthSourceType, auth_service
        from ..utils.admin_utils import is_admin

        # 顺序与UI一致
        browser_map = [
            (AuthSourceType.EDGE, "Edge"),
            (AuthSourceType.CHROME, "Chrome"),
            (AuthSourceType.CHROMIUM, "Chromium"),
            (AuthSourceType.BRAVE, "Brave"),
            (AuthSourceType.OPERA, "Opera"),
            (AuthSourceType.OPERA_GX, "Opera GX"),
            (AuthSourceType.VIVALDI, "Vivaldi"),
            (AuthSourceType.ARC, "Arc"),
            (AuthSourceType.FIREFOX, "Firefox"),
            (AuthSourceType.LIBREWOLF, "LibreWolf"),
            (AuthSourceType.CENT, "百分浏览器 (Cent)"),
        ]

        if 0 <= index < len(browser_map):
            source, name = browser_map[index]

            # WebView2 登录卡片在浏览器提取模式下由 _on_cookie_mode_changed 控制

            # Chromium 内核浏览器 v130+ 需要管理员权限
            from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

            if source in ADMIN_REQUIRED_BROWSERS and not is_admin():
                box = MessageBox(
                    f"{name} 需要管理员权限",
                    f"{name} 使用了 App-Bound 加密保护，\n"
                    f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                    + self.tr("点击「以管理员身份重启」后将自动完成提取。\n\n")
                    + self.tr("或者您可以：\n")
                    + self.tr("• 选择 Firefox/LibreWolf 浏览器（无需管理员权限）\n")
                    + self.tr("• 手动导出 Cookie 文件"),
                    self,
                )
                box.yesButton.setText("以管理员身份重启")
                box.cancelButton.setText("取消")

                if box.exec():
                    # 先保存选择
                    auth_service.set_source(source, auto_refresh=True)
                    from ..utils.admin_utils import restart_as_admin

                    restart_as_admin(f"提取 {name} Cookie")
                return

            # Firefox/Brave 或已是管理员，正常切换
            auth_service.set_source(source, auto_refresh=True)

            InfoBar.info(
                self.tr("正在切换浏览器"),
                f"正在从 {name} 提取 Cookies，请稍候...",
                duration=3000,
                parent=self,
            )

            # 不再清理旧worker，允许并发提取
            # 创建Qt工作线程
            worker = CookieRefreshWorker(self)
            self._active_workers.add(worker)

            # 连接信号（自动在主线程执行）
            def on_finished(success: bool, message: str, need_admin: bool = False):
                if success:
                    InfoBar.info(
                        self.tr("切换成功"),
                        self.tr("已从 {} 提取 Cookies").format(name),
                        duration=8000,
                        parent=self,
                    )
                else:
                    # 显示多行错误消息
                    lines = message.split("\n")
                    if len(lines) > 1:
                        title = f"{name} - {lines[0]}"
                        content = "\n".join(lines[1:])
                    else:
                        title = f"{name} 提取失败"
                        content = message

                    # 如果需要管理员权限，显示带重启按钮的对话框
                    if need_admin:
                        from qfluentwidgets import MessageBox

                        box = MessageBox(f"{name} 需要管理员权限", content, self)
                        box.yesButton.setText("以管理员身份重启")
                        box.cancelButton.setText("取消")

                        if box.exec():
                            from ..utils.admin_utils import restart_as_admin

                            restart_as_admin(f"提取 {name} Cookie")
                    else:
                        InfoBar.error(title, content, duration=15000, parent=self)

                # 总是更新Cookie状态显示
                try:
                    self._update_cookie_status()
                except Exception as e:
                    from ..utils.logger import logger

                    logger.error(f"更新Cookie状态显示失败: {e}")

                # 清理worker
                self._active_workers.discard(worker)
                worker.deleteLater()

            worker.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
            worker.start()

    def _on_browser_refresh_clicked(self):
        """刷新 Cookie — 一次提取两平台 (自动从本地浏览器提取)"""
        self.browserRefreshCard.button.setEnabled(False)
        from ..auth.auth_service import auth_service

        def progress_callback(platform_label: str, status_msg: str):
            """更新状态提示"""
            InfoBar.info(
                self.tr("提取进度"),
                self.tr(f"正在提取 {platform_label} Cookie... {status_msg}"),
                duration=3000,
                parent=self,
            )

        def _do_refresh_all_platforms():
            try:
                results = auth_service.extract_browser_cookies_all_platforms(progress_callback)

                yt_ok = results.get("youtube") is not None
                x_ok = results.get("twitter") is not None
                msg = f"YouTube: {'✅' if yt_ok else '❌'} | X: {'✅' if x_ok else '❌'}"

                if yt_ok or x_ok:
                    InfoBar.success(self.tr("提取完成"), msg, duration=5000, parent=self)
                else:
                    InfoBar.error(
                        self.tr("提取失败"),
                        self.tr("未提取到任何有效 Cookie。"),
                        duration=8000,
                        parent=self,
                    )
                self._update_cookie_status()
            except Exception as e:
                InfoBar.error(self.tr("提取异常"), str(e), duration=8000, parent=self)
            finally:
                self.browserRefreshCard.button.setEnabled(True)

        # 使用后台线程避免阻塞 UI
        import threading

        thread = threading.Thread(
            target=_do_refresh_all_platforms, daemon=True, name="BrowserCookieExtract"
        )
        thread.start()

    def _on_webview2_login_clicked(self, platform: str):
        """WebView2 登录按钮点击 - 启动浏览器登录流程"""
        from ..auth.auth_service import AuthSourceType, auth_service

        # 保证处于 WebView2 模式
        auth_service.set_source(AuthSourceType.WEBVIEW2, auto_refresh=False)

        # 并发登录互斥：防止 YouTube/X 同时启动 WebView2 子进程导致闪退
        if getattr(self, "_webview2_login_in_progress", None):
            InfoBar.warning(
                self.tr("登录进行中"),
                self.tr("请等待 {} 平台登录完成后再操作").format(
                    "YouTube" if self._webview2_login_in_progress == "youtube" else "X"
                ),
                duration=4000,
                parent=self,
            )
            return
        self._webview2_login_in_progress = platform

        if platform == "twitter":
            InfoBar.warning(
                self.tr("登录方式提示"),
                self.tr(
                    "受沙箱安全限制，X 平台目前无法使用「通过 Google / Apple 登录」。\n请在弹出的界面中使用「手机号/用户名/邮箱 + 密码」直接登录，否则会出现白屏！"
                ),
                duration=12000,
                parent=self,
            )

        account = auth_service.get_current_webview2_account(platform=platform)
        account_name = account.localized_name if account else self.tr("默认账号")

        # 同时禁用两个平台的登录按钮
        self.youtubeAuthCard.set_login_button_enabled(False)
        self.twitterAuthCard.set_login_button_enabled(False)

        card = self.youtubeAuthCard if platform == "youtube" else self.twitterAuthCard
        card.set_content(
            self.tr("正在后台提取登录态（{}），必要时会自动显示登录窗口...").format(account_name)
        )

        # 执行刷新
        worker = self._do_cookie_refresh(platform=platform)

        # 挂载完成回调（worker 已在 _do_cookie_refresh 中创建并返回）
        if worker:

            def _on_webview2_finished(success: bool, message: str, need_admin: bool = False):
                self._webview2_login_in_progress = None
                # 恢复两个平台的按钮状态
                self.youtubeAuthCard.set_login_button_enabled(True)
                self.twitterAuthCard.set_login_button_enabled(True)

                if success:
                    card.set_content(self.tr("✔ 登录成功，Cookie 已提取"))
                    from ..auth.cookie_sentinel import cookie_sentinel

                    current_acc = auth_service.get_current_webview2_account(platform=platform)
                    acc_cookie = current_acc.cached_cookie_path if current_acc else "未知"
                    InfoBar.info(
                        self.tr("登录成功"),
                        self.tr("Cookie 已成功提取并保存（{}）\n账号文件: {}\n统一文件: {}").format(
                            account_name,
                            acc_cookie,
                            cookie_sentinel.get_cookie_path_for_platform(platform),
                        ),
                        duration=5000,
                        parent=self,
                    )
                else:
                    card.set_content("❌ 登录未完成，请重新点击「点击登录」")
                    # 解析错误消息，去掉「刷新异常:」前缀
                    clean_msg = message
                    if clean_msg.startswith("刷新异常: "):
                        clean_msg = clean_msg[len("刷新异常: ") :]

                    # 显示错误 InfoBar
                    InfoBar.warning(
                        self.tr("登录未完成"),
                        clean_msg,
                        duration=8000,
                        parent=self,
                    )

            worker.finished.connect(
                _on_webview2_finished,
                Qt.ConnectionType.QueuedConnection,
            )

    def _reload_webview2_account_combo(self, select_current: bool = True) -> None:
        """全局刷新所有的 WebView2 账号下拉列表（向后兼容，通常不需要被调用了）"""
        self.youtubeAuthCard.reload_accounts(select_current)
        self.twitterAuthCard.reload_accounts(select_current)

    def _on_webview2_account_changed(self, platform: str, account_id: str) -> None:
        """切换当前 WebView2 账号"""
        from ..auth.auth_service import auth_service

        if auth_service.set_current_webview2_account(account_id):
            account = auth_service._webview2_accounts.get(account_id)
            name = account.localized_name if account else self.tr("未知账号")
            plat_name = (
                "YouTube"
                if platform == "youtube"
                else ("X" if platform == "twitter" else platform.capitalize())
            )
            InfoBar.info(
                self.tr("已切换 WebView2 账号"),
                f"当前 {plat_name} 账号: {name}",
                duration=2500,
                parent=self,
            )
            self._update_cookie_status()

    def _on_add_webview2_account_clicked(self, platform: str) -> None:
        """新增 WebView2 账号"""
        from ..auth.auth_service import auth_service

        dialog = WebView2AccountNameDialog(self)
        dialog.yesButton.setText("创建")
        dialog.cancelButton.setText("取消")
        if not dialog.exec():
            return

        display_name = dialog.get_account_name()
        if not display_name:
            InfoBar.warning(
                self.tr("名称为空"), self.tr("请输入有效账号名称"), duration=2500, parent=self
            )
            return

        try:
            account = auth_service.create_webview2_account(
                display_name=display_name, platform=platform
            )
            auth_service.set_current_webview2_account(account.account_id)
        except ValueError as e:
            InfoBar.error(self.tr("创建失败"), str(e), duration=3500, parent=self)
            return

        card = self.youtubeAuthCard if platform == "youtube" else self.twitterAuthCard
        card.reload_accounts(select_current=True)

        InfoBar.info(
            self.tr("账号已创建"),
            self.tr("已创建并切换到: {}").format(account.localized_name),
            duration=3000,
            parent=self,
        )
        self._update_cookie_status()

    def _on_remove_webview2_account_clicked(self, platform: str) -> None:
        """删除当前 WebView2 账号"""
        from qfluentwidgets import MessageBox

        from ..auth.auth_service import auth_service

        account_id = auth_service.get_current_webview2_account_id(platform=platform)
        account = auth_service._webview2_accounts.get(account_id) if account_id else None

        if not account:
            InfoBar.warning(
                self.tr("无可删账号"),
                self.tr("当前没有可删除的 WebView2 账号"),
                duration=2500,
                parent=self,
            )
            return

        box = MessageBox(
            self.tr("删除当前 WebView2 账号"),
            self.tr("确定删除账号「{}」吗？\n\n至少需要保留 1 个账号。").format(
                account.localized_name
            ),
            self,
        )
        box.yesButton.setText("删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        if not auth_service.delete_webview2_account(account.account_id, remove_storage=False):
            InfoBar.error(
                self.tr("删除失败"), self.tr("至少需要保留一个账号"), duration=3000, parent=self
            )
            return

        card = self.youtubeAuthCard if platform == "youtube" else self.twitterAuthCard
        card.reload_accounts(select_current=True)
        InfoBar.info(
            self.tr("删除成功"),
            self.tr("已删除: {}").format(account.localized_name),
            duration=3000,
            parent=self,
        )
        self._update_cookie_status()

    def _on_refresh_cookie_clicked(self, platform: str):
        """手动刷新 Cookie 按钮点击"""
        from qfluentwidgets import MessageBox

        from ..auth.auth_service import auth_service
        from ..utils.admin_utils import is_admin

        current_source = auth_service.current_source

        # 检查是否是 Chromium 内核浏览器且非管理员 - 直接提示重启
        from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

        if current_source in ADMIN_REQUIRED_BROWSERS and not is_admin():
            browser_name = auth_service.current_source_display

            box = MessageBox(
                f"{browser_name} 需要管理员权限",
                f"{browser_name} 使用了 App-Bound 加密保护，\n"
                f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                + self.tr("点击「以管理员身份重启」后将自动完成提取。\n\n")
                + self.tr("或者您可以：\n")
                + self.tr("• 切换到 Firefox/LibreWolf 浏览器（无需管理员权限）\n")
                + self.tr("• 手动导出 Cookie 文件"),
                self,
            )
            box.yesButton.setText(self.tr("以管理员身份重启"))
            box.cancelButton.setText(self.tr("取消"))

            if box.exec():
                from ..utils.admin_utils import restart_as_admin

                restart_as_admin(f"提取 {browser_name} Cookie")
            return

        # 非 Edge/Chrome 或已是管理员，正常刷新
        self._do_cookie_refresh(platform=platform)

    def _do_cookie_refresh(self, platform: str | None = None):
        """实际执行Cookie刷新（已确认权限或非Edge/Chrome）"""
        # 禁用按钮
        if platform:
            card = self.youtubeAuthCard if platform == "youtube" else self.twitterAuthCard
            card.refreshCookieCard.setEnabled(False)
            card.refreshCookieCard.button.setText(self.tr("刷新中..."))

        # 显示进度提示
        InfoBar.info(self.tr("正在刷新 Cookie"), self.tr("请稍候..."), duration=3000, parent=self)

        # 不再清理旧worker，允许并发
        # 创建Qt工作线程
        worker = CookieRefreshWorker(self, platform=platform)
        self._active_workers.add(worker)

        # 连接信号（自动在主线程执行）
        def on_finished(success: bool, message: str, need_admin: bool = False):
            # 1. 总是重置按钮状态
            if platform:
                card.refreshCookieCard.setEnabled(True)
                card.refreshCookieCard.button.setText(self.tr("立即刷新"))

            # 2. 显示结果消息
            if success:
                InfoBar.info(self.tr("刷新成功"), message, duration=8000, parent=self)
            else:
                # 显示多行错误消息
                lines = message.split("\n")
                if len(lines) > 1:
                    title = lines[0]
                    content = "\n".join(lines[1:])
                else:
                    title = self.tr("Cookie 刷新失败")
                    content = message

                InfoBar.error(title, content, duration=15000, parent=self)

            # 3. 总是更新Cookie状态显示
            try:
                self._update_cookie_status()
            except Exception as e:
                from ..utils.logger import logger

                logger.error(f"更新Cookie状态显示失败: {e}")

            # 清理worker
            self._active_workers.discard(worker)
            worker.deleteLater()

        worker.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()
        return worker

    def _select_cookie_file(self):
        """选择 Cookie 文件并导入到相应平台的 cookies.txt"""

        from ..auth.auth_service import AuthSourceType, auth_service
        from .dialogs.platform_selector_dialog import PlatformSelectorDialog

        dialog = PlatformSelectorDialog(self.window())
        if not dialog.exec():
            return

        platform = dialog.get_selected_platform()

        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 Cookies 文件", "", "Cookies 文件 (*.txt);;所有文件 (*.*)"
        )

        if file_path:
            # 验证提取写入一条龙导入
            status = auth_service.import_manual_cookie_file(file_path, platform=platform)

            if not status.valid:
                InfoBar.error(self.tr("文件格式有问题"), status.message, duration=5000, parent=self)
                return

            try:
                # 设置为文件模式
                auth_service.set_source(
                    AuthSourceType.FILE, file_path=file_path, auto_refresh=False
                )

                self.cookieFileCard.setContent(f"已导入: {status.cookie_count} 个 Cookie")
                InfoBar.info(
                    self.tr("导入成功"),
                    self.tr("已导入 {} 个 Cookie 到 {} 平台").format(
                        status.cookie_count, platform.capitalize()
                    ),
                    duration=3000,
                    parent=self,
                )
            except Exception as e:
                InfoBar.error(
                    self.tr("导入失败"),
                    self.tr("复制文件时出错: {}").format(e),
                    duration=5000,
                    parent=self,
                )
                return

            self._update_cookie_status()

    def _open_cookie_location(self, platform: str | None = None):
        """打开 Cookie 文件所在位置"""
        import os
        import subprocess
        from pathlib import Path

        from ..auth.cookie_sentinel import cookie_sentinel

        if platform:
            from ..auth.auth_service import auth_service

            acc = auth_service.get_current_webview2_account(platform=platform)
            cookie_path_str = acc.cached_cookie_path if acc else cookie_sentinel.cookie_path
        else:
            cookie_path_str = cookie_sentinel.cookie_path

        cookie_path = Path(cookie_path_str) if cookie_path_str else None

        if not cookie_path:
            return

        if cookie_path.exists():
            # Windows: 使用 explorer 选中文件
            subprocess.run(["explorer", "/select,", str(cookie_path)])
        else:
            # 文件不存在，打开目录
            folder = cookie_path.parent
            if folder.exists():
                os.startfile(str(folder))
            else:
                InfoBar.warning(
                    self.tr("目录不存在"),
                    self.tr("Cookie 目录尚未创建: {}").format(folder),
                    duration=3000,
                    parent=self,
                )

    def _get_status_text_for_platform(self, platform: str) -> str:
        from ..auth.auth_service import AuthSourceType, auth_service
        from ..auth.cookie_sentinel import cookie_sentinel

        current_source = auth_service.current_source
        info = cookie_sentinel.get_status_info(platform)

        if current_source == AuthSourceType.NONE:
            return self.tr("⚪ 未启用 Cookie 验证")

        if not info["exists"]:
            if current_source == AuthSourceType.WEBVIEW2:
                return self.tr("🔑 WebView2 模式 — 尚未登录，请点击「启动安全登录」按钮")
            elif current_source == AuthSourceType.FILE:
                return self.tr("❌ Cookie 文件不存在，请重新选择文件")
            else:
                return self.tr("❌ 尚无 Cookie — 请点击「立即刷新」从 {} 提取").format(
                    auth_service.current_source_display
                )

        # === 有 Cookie 文件时的详细状态 ===
        age = info["age_minutes"]
        age_str = self.tr("{} 分钟前").format(int(age)) if age is not None else self.tr("未知时间")
        cookie_count = info["cookie_count"]
        cookie_valid = info.get("cookie_valid", False)
        cookie_valid_msg = info.get("cookie_valid_msg", "")
        actual_display = info.get("actual_source_display") or info["source"]

        # 决定主 emoji 和来源文字
        if not cookie_valid:
            emoji = "❌"
            source_text = actual_display
        elif info.get("using_fallback") or info.get("source_mismatch"):
            emoji = "⚠️"
            if info.get("source_mismatch") and info.get("actual_source_display"):
                source_text = self.tr("{}（当前配置: {}）").format(actual_display, info["source"])
            else:
                source_text = actual_display
        elif info.get("expiring_soon"):
            emoji = "⏳"
            source_text = actual_display
        elif info["is_stale"]:
            emoji = "⚠️"
            source_text = actual_display
        else:
            emoji = "✔"
            source_text = actual_display

        status_text = self.tr("{} {} | 更新于 {} | {} 个 Cookie").format(
            emoji, source_text, age_str, cookie_count
        )

        # 即将过期预警
        earliest = info.get("earliest_expiry")
        if info.get("expiring_soon") and earliest is not None:
            if earliest <= 0:
                status_text += "\n⚠️ 关键 Cookie 已过期，请立即刷新"
            else:
                mins = int(earliest / 60)
                status_text += f"\n⏳ 关键 Cookie 将在 {mins} 分钟后过期，建议尽快刷新"

        # 有效性说明（仅在失效时显示）
        if not cookie_valid and cookie_valid_msg:
            status_text += f"\n{cookie_valid_msg}"

        # 回退警告
        if info.get("fallback_warning"):
            status_text += f"\n⚠️ {info['fallback_warning']}"

        return status_text

    def _update_cookie_status(self):
        """更新 Cookie 状态显示"""
        try:
            yt_text = self._get_status_text_for_platform("youtube")
            self.youtubeAuthCard.cookieStatusCard.setContent(yt_text)
        except Exception as e:
            self.youtubeAuthCard.cookieStatusCard.setContent(f"状态获取失败: {e}")

        try:
            tw_text = self._get_status_text_for_platform("twitter")
            self.twitterAuthCard.cookieStatusCard.setContent(tw_text)
        except Exception as e:
            self.twitterAuthCard.cookieStatusCard.setContent(f"状态获取失败: {e}")

    def _on_check_updates_startup_changed(self, checked: bool) -> None:
        config_manager.set("check_updates_on_startup", checked)

    def _on_cookie_cleaning_changed(self, checked: bool) -> None:
        from ..core.config_manager import config_manager

        config_manager.set("cookie_cleaning_enabled", checked)

    def _on_update_source_changed(self, index: int) -> None:
        source_map = {0: "github", 1: "ghproxy"}
        mode = source_map.get(index, "github")
        config_manager.set("update_source", mode)
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("组件更新源已切换为: {}").format(self.updateSourceCard.comboBox.currentText()),
            duration=3000,
            parent=self,
        )

    def _on_ytdlp_channel_changed(self, index: int) -> None:
        channel_map = {0: "stable", 1: "nightly", 2: "master"}
        mode = channel_map.get(index, "stable")
        config_manager.set("ytdlp_channel", mode)
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("yt-dlp 更新频道已切换为: {} (下次更新时生效)").format(
                self.ytDlpChannelCard.comboBox.currentText()
            ),
            duration=5000,
            parent=self,
        )

    def _on_js_runtime_changed(self, index: int) -> None:
        mapping = {0: "auto", 1: "deno", 2: "node", 3: "bun", 4: "quickjs"}
        mode = mapping.get(index, "auto")
        config_manager.set("js_runtime", mode)
        InfoBar.info(
            self.tr("设置已更新"),
            self.tr("JS Runtime 已切换为: {}").format(self.jsRuntimeCard.comboBox.currentText()),
            duration=5000,
            parent=self,
        )
        self.jsRuntimePathCard.setContent(self._js_runtime_status_text())

    def _on_po_token_edited(self) -> None:
        # Legacy no-op: PO Token is now edited via SmartSettingCard dialog.
        val = str(config_manager.get("youtube_po_token") or "").strip()
        try:
            self.poTokenCard.setValue(val)
        except Exception:
            from ...utils.logger import logger

            logger.exception("Swallowed exception in settings")

    def _on_network_retries_changed(self, value: int) -> None:
        config_manager.set("network_retries", value)

    def _on_concurrent_fragments_changed(self, index: int) -> None:
        val = index + 1
        config_manager.set("concurrent_fragments", val)

    def _select_download_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择下载目录")
        if folder:
            config_manager.set("download_dir", folder)
            self.downloadFolderCard.setContent(folder)

    def _select_yt_dlp_path(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 yt-dlp.exe"),
            "",
            "Executables (*.exe);;All Files (*)",
        )
        if file:
            path = self._fix_windows_path(file)
            config_manager.set("yt_dlp_exe_path", path)
            self._on_yt_dlp_path_edited()

    def _on_yt_dlp_path_edited(self) -> None:
        path = self._fix_windows_path(str(config_manager.get("yt_dlp_exe_path") or ""))
        if path and not Path(path).exists():
            InfoBar.warning(
                self.tr("路径无效"),
                self.tr("未找到该文件，已回退为自动检测（优先内置，其次 PATH）。"),
                duration=15000,
                parent=self,
            )
            config_manager.set("yt_dlp_exe_path", "")
            try:
                self.ytDlpCard.setValue("")
                self.ytDlpCard.setContent(self._yt_dlp_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")
            return

        config_manager.set("yt_dlp_exe_path", path)
        try:
            self.ytDlpCard.setValue(path)
            self.ytDlpCard.setContent(f"自定义: {path}" if path else self._yt_dlp_status_text())
        except Exception:
            from ...utils.logger import logger

            logger.exception("Swallowed exception in settings")

    def _yt_dlp_status_text(self) -> str:
        cfg = str(config_manager.get("yt_dlp_exe_path") or "").strip()
        if cfg:
            try:
                if Path(cfg).exists():
                    return self.tr("已就绪（手动指定）")
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")

        if is_frozen():
            p = find_bundled_executable(
                "yt-dlp.exe",
                "yt-dlp/yt-dlp.exe",
                "yt_dlp/yt-dlp.exe",
            )
            if p is not None:
                return self.tr("已就绪（内置）")

        which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if which:
            return self.tr("已就绪（环境（PATH））")

        return self.tr("未就绪（无法解析/下载）")

    @staticmethod
    def _quick_check_cookiefile_format(path: str) -> tuple[bool, bool]:
        """Return (header_ok, newline_ok) for Netscape cookie files."""

        try:
            p = Path(path)
            head = p.read_bytes()[:4096]
            first_line = (
                head.splitlines()[0].decode("utf-8", errors="ignore").strip() if head else ""
            )

            header_ok = first_line.startswith(
                "# Netscape HTTP Cookie File"
            ) or first_line.startswith("# HTTP Cookie File")

            # Heuristic: if file contains any '\n' but no '\r\n', it is likely LF-only.
            has_lf = b"\n" in head
            has_crlf = b"\r\n" in head
            newline_ok = (not has_lf) or has_crlf
            return header_ok, newline_ok
        except Exception:
            return True, True

    @staticmethod
    def _is_probably_json_cookie_file(path: str) -> bool:
        try:
            p = Path(path)
            head = p.read_bytes()[:2048]
            text = head.decode("utf-8", errors="ignore").lstrip()
            return bool(text) and text[0] in "[{"
        except Exception:
            return False

    def _select_ffmpeg_path(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 ffmpeg.exe"),
            "",
            "Executables (*.exe);;All Files (*)",
        )
        if file:
            path = self._fix_windows_path(file)
            config_manager.set("ffmpeg_path", path)
            self._on_ffmpeg_path_edited()

    def _on_ffmpeg_path_edited(self) -> None:
        path = self._fix_windows_path(str(config_manager.get("ffmpeg_path") or ""))
        config_manager.set("ffmpeg_path", path)

        if path:
            if not Path(path).exists():
                InfoBar.warning(
                    self.tr("路径可能无效"),
                    self.tr("未找到该文件，请确认 ffmpeg.exe 路径是否正确。"),
                    duration=15000,
                    parent=self,
                )
            try:
                self.ffmpegCard.setValue(path)
                self.ffmpegCard.setContent(f"自定义: {path}")
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")
        else:
            try:
                self.ffmpegCard.setValue("")
                self.ffmpegCard.setContent(self._ffmpeg_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")

    def _ffmpeg_status_text(self) -> str:
        custom = str(config_manager.get("ffmpeg_path") or "").strip()
        if custom:
            try:
                if Path(custom).exists():
                    return self.tr("已就绪（手动指定）")
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")

        # Auto-detect priority: bundled (_internal) > PATH
        bundled = (
            find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe") if is_frozen() else None
        )
        if bundled is not None:
            return self.tr("已就绪（内置）")

        which = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if which:
            return self.tr("已就绪（环境（PATH））")

        return self.tr(
            "未找到（解决：使用 full 包内置 FFmpeg，或安装 FFmpeg 并加入 PATH，或在此处选择）"
        )

    def _resolve_js_runtime_bundled(self, runtime_id: str) -> Path | None:
        if not is_frozen():
            return None
        if runtime_id == "deno":
            return find_bundled_executable("deno.exe", "js/deno.exe", "deno/deno.exe")
        if runtime_id == "node":
            return find_bundled_executable("node.exe", "js/node.exe", "node/node.exe")
        if runtime_id == "bun":
            return find_bundled_executable("bun.exe", "js/bun.exe", "bun/bun.exe")
        if runtime_id == "quickjs":
            return find_bundled_executable("qjs.exe", "js/qjs.exe", "quickjs/qjs.exe")
        return None

    def _js_runtime_text(self) -> str:
        mode = str(config_manager.get("js_runtime") or "auto").lower()
        label_map = {
            "auto": "自动(推荐)",
            "deno": "Deno",
            "node": "Node",
            "bun": "Bun",
            "quickjs": "QuickJS",
        }
        return label_map.get(mode, mode)

    def _resolve_js_runtime_exe(self) -> tuple[str, Path | None, str]:
        """Return (runtime_id, exe_path, source_text)."""

        preferred = str(config_manager.get("js_runtime") or "auto").strip().lower()
        custom = str(config_manager.get("js_runtime_path") or "").strip()

        if preferred in {"deno", "node", "bun", "quickjs"}:
            if custom and Path(custom).exists():
                return preferred, Path(custom), "自定义"

            bundled = self._resolve_js_runtime_bundled(preferred)
            if bundled is not None:
                return preferred, bundled, "内置"

            if preferred == "deno":
                which = shutil.which("deno")
                return preferred, Path(which) if which else None, "PATH"
            if preferred == "node":
                which = shutil.which("node") or shutil.which("node.exe")
                return preferred, Path(which) if which else None, "PATH"
            if preferred == "bun":
                which = shutil.which("bun") or shutil.which("bun.exe")
                return preferred, Path(which) if which else None, "PATH"
            if preferred == "quickjs":
                which = (
                    shutil.which("qjs")
                    or shutil.which("qjs.exe")
                    or shutil.which("quickjs")
                    or shutil.which("quickjs.exe")
                )
                return preferred, Path(which) if which else None, "PATH"

        # auto: prefer bundled deno (full package), then PATH deno/node/bun/quickjs
        bundled_deno = self._resolve_js_runtime_bundled("deno")
        if bundled_deno is not None:
            return "deno", bundled_deno, "内置"

        deno = shutil.which("deno")
        if deno:
            return "deno", Path(deno), "PATH"

        # winget deno heuristic
        try:
            local_app_data = Path(os.environ.get("LOCALAPPDATA") or "")
            if local_app_data:
                winget_packages = local_app_data / "Microsoft" / "WinGet" / "Packages"
                if winget_packages.exists():
                    matches = list(winget_packages.glob("DenoLand.Deno_*\\deno.exe"))
                    if matches:
                        return "deno", matches[0], "winget"
        except Exception:
            from ...utils.logger import logger

            logger.exception("Swallowed exception in settings")

        node = shutil.which("node") or shutil.which("node.exe")
        if node:
            return "node", Path(node), "PATH"
        bun = shutil.which("bun") or shutil.which("bun.exe")
        if bun:
            return "bun", Path(bun), "PATH"
        qjs = (
            shutil.which("qjs")
            or shutil.which("qjs.exe")
            or shutil.which("quickjs")
            or shutil.which("quickjs.exe")
        )
        if qjs:
            return "quickjs", Path(qjs), "PATH"

        return "auto", None, ""

    def _js_runtime_status_text(self) -> str:
        preferred = str(config_manager.get("js_runtime") or "auto").strip().lower()
        rid, exe, source = self._resolve_js_runtime_exe()
        label = {"deno": "Deno", "node": "Node", "bun": "Bun", "quickjs": "QuickJS"}.get(rid, rid)

        source_map = {
            "自定义": self.tr("手动指定"),
            "内置": self.tr("内置"),
            "PATH": self.tr("环境（PATH）"),
            "winget": "winget",
        }
        source_text = source_map.get(source, source or "")

        if preferred == "auto":
            if exe is None:
                return self.tr(
                    "未就绪（解决：使用 full 包内置 Deno，或安装 deno 并加入 PATH，或在此处选择）"
                )
            return self.tr("已就绪（自动：{} / {}）").format(label, source_text or self.tr("未知"))

        if exe is None:
            preferred_label = {
                "deno": "Deno",
                "node": "Node",
                "bun": "Bun",
                "quickjs": "QuickJS",
            }.get(preferred, preferred)
            return self.tr("未就绪: {}（解决：优先使用内置，其次 PATH；也可在此处选择）").format(
                preferred_label
            )
        return self.tr("已就绪（{}）").format(source_text or self.tr("未知"))

    def _select_js_runtime_path(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 JS Runtime 可执行文件（可选）"),
            "",
            "Executables (*.exe);;All Files (*)",
        )
        if file:
            path = self._fix_windows_path(file)
            config_manager.set("js_runtime_path", path)
            self._on_js_runtime_path_edited()

    def _on_js_runtime_path_edited(self) -> None:
        path = self._fix_windows_path(str(config_manager.get("js_runtime_path") or ""))
        if path and not Path(path).exists():
            InfoBar.warning(
                self.tr("路径无效"),
                self.tr("未找到该文件，已回退为自动检测（优先内置，其次 PATH）。"),
                parent=self,
            )
            config_manager.set("js_runtime_path", "")
            try:
                self.jsRuntimePathCard.setValue("")
                self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.exception("Swallowed exception in settings")
            return

        config_manager.set("js_runtime_path", path)
        try:
            self.jsRuntimePathCard.setValue(path)
            self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
        except Exception:
            from ...utils.logger import logger

            logger.exception("Swallowed exception in settings")

    def _check_js_runtime(self) -> None:
        rid, exe, source = self._resolve_js_runtime_exe()
        if exe is None:
            InfoBar.warning(
                self.tr("未找到 JS Runtime"),
                self.tr("请安装 deno/node/bun/quickjs 或在此处指定可执行文件路径。"),
                duration=15000,
                parent=self,
            )
            return

        candidates: list[list[str]] = [[str(exe), "--version"], [str(exe), "-v"], [str(exe), "-V"]]
        out = ""
        for cmd in candidates:
            try:
                kwargs: dict[str, Any] = {}
                if os.name == "nt":
                    try:
                        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
                    except Exception:
                        from ...utils.logger import logger

                        logger.exception("Swallowed exception in settings")
                    try:
                        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
                        si.wShowWindow = 0
                        kwargs["startupinfo"] = si
                    except Exception:
                        from ...utils.logger import logger

                        logger.exception("Swallowed exception in settings")

                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **kwargs,
                )
                out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
                if proc.returncode == 0 and out:
                    break
            except Exception:
                continue

        label = {"deno": "Deno", "node": "Node", "bun": "Bun", "quickjs": "QuickJS"}.get(rid, rid)
        ver_line = out.splitlines()[0].strip() if out else "(unknown)"
        InfoBar.info(
            "JS Runtime",
            self.tr("类型: {}\n版本: {}\n路径: {}\n来源: {}").format(
                label, ver_line, exe, source or self.tr("未知")
            ),
            duration=5000,
            parent=self,
        )
        self.jsRuntimePathCard.setContent(self._js_runtime_status_text())

    def _check_yt_dlp(self) -> None:
        exe = resolve_yt_dlp_exe()
        if exe is None:
            InfoBar.error(
                self.tr("未找到 yt-dlp.exe"),
                self.tr(
                    "请在此处选择 yt-dlp.exe，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。"
                ),
                duration=15000,
                parent=self,
            )
            return

        ver = run_version() or "(unknown)"
        InfoBar.info(
            "yt-dlp",
            self.tr("版本: {}\n路径: {}\n更新方式: 替换该 yt-dlp.exe").format(ver, exe),
            duration=5000,
            parent=self,
        )
        self.ytDlpCard.setContent(self._yt_dlp_status_text())

    def _on_preferred_audio_language_changed(self, languages: list[str]) -> None:
        """多音轨语言偏好改变时"""
        if not languages:
            languages = ["orig", "zh-Hans", "en"]
        config_manager.set("preferred_audio_languages", languages)

    def _on_subtitle_enabled_changed(self, checked: bool) -> None:
        config_manager.set("subtitle_enabled", checked)
        self._update_subtitle_settings_visibility(checked)
        status = self.tr("已启用") if checked else self.tr("已禁用")
        InfoBar.info(
            self.tr("字幕设置"), self.tr("字幕下载{}").format(status), duration=3000, parent=self
        )

    def _on_subtitle_languages_changed(self, languages: list[str]) -> None:
        """语言选择改变回调"""
        if not languages:
            languages = ["zh-Hans", "en"]
        config_manager.set("subtitle_default_languages", languages)
        from PySide6.QtCore import QCoreApplication

        names = [
            QCoreApplication.translate("Subtitle", n)
            for c, n in COMMON_SUBTITLE_LANGUAGES
            if c in languages
        ]
        InfoBar.info(
            self.tr("语言设置"),
            self.tr("已选择字幕语言: {}").format(", ".join(names)),
            duration=3000,
            parent=self,
        )

    def _on_subtitle_embed_type_changed(self, embed_type: str) -> None:
        """嵌入类型改变回调"""
        if embed_type not in ("soft", "external"):
            embed_type = "soft"
        config = config_manager.get_subtitle_config()
        config.embed_type = cast(Literal["soft", "external"], embed_type)
        config_manager.set_subtitle_config(config)
        type_names = {"soft": self.tr("软嵌入"), "external": self.tr("外置文件")}
        InfoBar.info(
            self.tr("嵌入类型"),
            self.tr("字幕嵌入类型: {}").format(type_names.get(embed_type, embed_type)),
            duration=3000,
            parent=self,
        )

    def _on_subtitle_format_changed(self, index: int) -> None:
        format_map = {0: "srt", 1: "ass", 2: "vtt"}
        fmt = format_map.get(index, "srt")
        config_manager.set("subtitle_output_format", fmt)
        InfoBar.info(
            self.tr("格式设置"),
            self.tr("字幕输出格式: {}").format(fmt.upper()),
            duration=3000,
            parent=self,
        )

    def _update_vr_hardware_status(self) -> None:
        """更新 VR 硬件状态 Banner"""
        self.vrHardwareStatusCard.setContent(self.tr("检测中..."))
        QThread.msleep(100)  # Give UI a chance to update

        # 强制刷新硬件检测缓存，确保能检测到最新的环境变化
        hardware_manager.refresh_hardware_status()

        mem_gb = hardware_manager.get_system_memory_gb()
        has_gpu = hardware_manager.has_dedicated_gpu()
        encoders = hardware_manager.get_gpu_encoders()

        status_text = self.tr("内存: {} GB").format(mem_gb)
        if has_gpu:
            status_text += self.tr(" | GPU 加速: 可用 ({})").format(", ".join(encoders))
            desc = self.tr("您的硬件支持 VR 硬件转码。")
            if mem_gb >= 16:
                desc += self.tr(" (支持 8K 转码)")
            else:
                desc += self.tr(" (建议限制在 4K/6K)")
        else:
            status_text += self.tr(" | GPU 加速: 不可用")
            desc = self.tr("未检测到硬件编码器，将使用 CPU 转码 (较慢)。")

        self.vrHardwareStatusCard.setTitle(status_text)
        self.vrHardwareStatusCard.setContent(desc)
        # B2: 无 GPU 时将「强制 GPU」选项置灰，防止用户选择后静默回落 CPU
        combo = self.vrHwAccelCard.comboBox
        try:
            # qfluentwidgets ComboBox 提供 setItemEnabled(index, bool)
            combo.setItemEnabled(2, has_gpu)  # index 2 = "强制 GPU (快)"
        except Exception:
            pass  # 若版本不支持则跳过，不影响主逻辑
        if not has_gpu and combo.currentIndex() == 2:
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
            config_manager.set("vr_hw_accel_mode", "auto")
        # TODO: Update icon if possible, currently SettingCard doesn't support changing icon easily

    def _on_vr_eac_auto_convert_changed(self, checked: bool) -> None:
        config_manager.set("vr_eac_auto_convert", checked)
        if checked:
            InfoBar.warning(
                self.tr("耗时操作警告"),
                self.tr("EAC 转码非常消耗资源。如果没有高性能显卡，8K 视频可能需要数小时。"),
                duration=5000,
                parent=self,
            )

    def _on_vr_hw_accel_changed(self, index: int) -> None:
        mode_map = {0: "auto", 1: "cpu", 2: "gpu"}
        mode = mode_map.get(index, "auto")
        config_manager.set("vr_hw_accel_mode", mode)
        if mode == "gpu":
            from ..core.hardware_manager import hardware_manager

            encoders = hardware_manager.get_gpu_encoders()
            if not encoders:
                InfoBar.warning(
                    self.tr("未检测到 GPU 编码器"),
                    self.tr("当前系统没有可用的硬件编码器（NVENC/QSV/AMF），\n")
                    + self.tr("「强制 GPU」将自动回落为 CPU 转码，速度会很慢。\n")
                    + self.tr("建议改为「自动 (推荐)」。"),
                    duration=8000,
                    parent=self,
                )

    def _on_vr_max_resolution_changed(self, index: int) -> None:
        res_map = {0: 2160, 1: 3200, 2: 4320}
        val = res_map.get(index, 2160)
        config_manager.set("vr_max_resolution", val)
        if val >= 4320:
            InfoBar.error(
                self.tr("高风险设置"),
                self.tr(
                    "开启 8K 转码极易导致内存溢出或系统卡死。请确保您有 32GB+ 内存和高端显卡。"
                ),
                duration=5000,
                parent=self,
            )

    def _on_vr_cpu_priority_changed(self, index: int) -> None:
        pri_map = {0: "low", 1: "medium", 2: "high"}
        config_manager.set("vr_cpu_priority", pri_map.get(index, "low"))

    def _on_vr_keep_source_changed(self, checked: bool) -> None:
        config_manager.set("vr_keep_source", checked)

    def _update_subtitle_settings_visibility(self, enabled: bool) -> None:
        # 用户希望关闭字幕下载时，依然保留选项显示以便修改
        # 这样即使全局关闭，用户在单次下载中想开启时，配置已经是预期的
        self.subtitleLanguagesCard.setVisible(True)
        self.subtitleEmbedTypeCard.setVisible(True)
