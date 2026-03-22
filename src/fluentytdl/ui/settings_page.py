from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Literal, cast

from PySide6.QtCore import Qt, QThread, Signal
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
    SubtitleLabel,
    SwitchButton,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
)

from ..core.config_manager import config_manager
from ..core.dependency_manager import dependency_manager
from ..core.hardware_manager import hardware_manager
from ..download.download_manager import download_manager
from ..processing.subtitle_manager import COMMON_SUBTITLE_LANGUAGES
from ..utils.logger import LOG_DIR
from ..utils.paths import find_bundled_executable, is_frozen
from ..youtube.yt_dlp_cli import resolve_yt_dlp_exe, run_version
from .components.custom_info_bar import InfoBar
from .components.smart_setting_card import SmartSettingCard

# ============================================================================
# Cookie 刷新 Worker（使用Qt线程，确保打包后正常工作）
# ============================================================================


class CookieRefreshWorker(QThread):
    """Cookie刷新工作线程（Qt线程，打包后可靠）"""

    finished = Signal(bool, str, bool)  # (成功标志, 消息, 是否需要管理员权限)

    def __init__(self, parent=None):
        super().__init__(parent)

    def run(self):
        """在Qt线程中执行Cookie刷新"""
        from ..auth.auth_service import auth_service
        from ..auth.cookie_sentinel import cookie_sentinel
        from ..utils.logger import logger

        success = False
        message = "未知错误"

        try:
            # 直接刷新（调用前已检查权限，或已是管理员/非Edge/Chrome）
            success, message = cookie_sentinel.force_refresh_with_uac()

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
                        "可能的原因：\n"
                        f"1. {browser_name} 未安装或未登录 YouTube\n"
                        f"2. {browser_name} Cookie 数据库被锁定（请关闭浏览器）\n\n"
                        "建议：完全关闭浏览器后重试"
                    )

                logger.warning(f"[CookieRefreshWorker] 提取失败: {message}")
        except Exception as e:
            success = False
            message = f"刷新异常: {str(e)}"
            logger.error(f"[CookieRefreshWorker] 异常: {e}", exc_info=True)

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

        self.actionButton = PushButton("检查更新", self)
        self.actionButton.clicked.connect(self._on_action_clicked)

        self.importButton = PushButton("手动导入", self, FluentIcon.ADD)
        self.importButton.setToolTip("选择本地文件覆盖当前组件")
        self.importButton.installEventFilter(
            ToolTipFilter(self.importButton, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.importButton.clicked.connect(self._on_import_clicked)

        self.folderButton = ToolButton(FluentIcon.FOLDER, self)
        self.folderButton.setToolTip("打开所在文件夹")
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
        if text == "检查更新":
            dependency_manager.check_update(self.component_key)
        elif text in ("立即更新", "立即安装"):
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
        elif self.component_key == "ytarchive":
            exe_name = "ytarchive.exe"
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
                InfoBar.error("错误", "所选文件为空", parent=self.window())
                return

            shutil.copy2(src, target_path)

            InfoBar.success("导入成功", f"已手动导入 {exe_name}", parent=self.window())
            # Refresh version info
            dependency_manager.check_update(self.component_key)

        except Exception as e:
            InfoBar.error("导入失败", str(e), parent=self.window())

    def _open_folder(self):
        try:
            path = dependency_manager.get_target_dir(self.component_key)
            if path.exists():
                os.startfile(path)
            else:
                InfoBar.warning("目录不存在", f"{path} 尚未创建", parent=self.window())
        except Exception as e:
            InfoBar.error("错误", str(e), parent=self.window())

    def _on_check_started(self, key):
        if key != self.component_key:
            return
        self.actionButton.setText("正在检查...")
        self.actionButton.setEnabled(False)

    def _on_check_finished(self, key, result):
        if key != self.component_key:
            return
        self.actionButton.setEnabled(True)

        curr = result.get("current", "unknown")
        latest = result.get("latest", "unknown")
        has_update = result.get("update_available", False)

        self.setContent(f"当前: {curr}  |  最新: {latest}")

        title_text = self.titleLabel.text()

        if has_update:
            self.actionButton.setText("立即更新")
            InfoBar.info(
                f"发现新版本: {title_text}",
                f"版本 {latest} 可用 (当前: {curr})",
                duration=15000,
                parent=self.window(),
            )
        else:
            if curr == "unknown":
                self.actionButton.setText("立即安装")
            else:
                self.actionButton.setText("检查更新")
                InfoBar.success(
                    "已是最新",
                    f"{title_text} 当前版本 {curr} 已是最新。",
                    duration=5000,
                    parent=self.window(),
                )

    def _on_download_started(self, key):
        if key != self.component_key:
            return
        self.progressBar.setVisible(True)
        self.progressBar.setValue(0)
        self.actionButton.setEnabled(False)
        self.actionButton.setText("正在下载...")

    def _on_download_progress(self, key, percent):
        if key != self.component_key:
            return
        self.progressBar.setValue(percent)

    def _on_download_finished(self, key):
        if key != self.component_key:
            return
        self.actionButton.setText("正在安装...")

    def _on_install_finished(self, key):
        if key != self.component_key:
            return
        self.progressBar.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText("检查更新")
        # Trigger a re-check to update version text
        dependency_manager.check_update(self.component_key)

        title_text = self.titleLabel.text()
        InfoBar.success(
            "安装完成", f"{title_text} 已成功安装/更新。", duration=5000, parent=self.window()
        )

    def _on_error(self, key, msg):
        if key != self.component_key:
            return
        self.progressBar.setVisible(False)
        self.actionButton.setEnabled(True)
        self.actionButton.setText("检查更新")  # Reset

        title_text = self.titleLabel.text()
        InfoBar.error(f"{title_text} 错误", msg, duration=15000, parent=self.window())


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
        super().__init__("选择字幕语言", "", parent)

        self.languages = languages
        self.selected_languages = selected.copy() if selected else []
        self.checkboxes = {}

        # 创建内容布局
        from PySide6.QtWidgets import QFrame, QGridLayout, QScrollArea, QVBoxLayout, QWidget

        content_widget = QWidget(self)
        content_layout = QVBoxLayout(content_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # 添加说明
        hint_label = SubtitleLabel("请选择要下载的字幕语言（可多选）：", content_widget)
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
            checkbox = CheckBox(f"{name} ({code})", checkbox_container)
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
        scroll = QScrollArea(content_widget)
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
        self.selectButton = PushButton("选择语言", self)
        self.selectButton.clicked.connect(self._show_language_dialog)
        self.hBoxLayout.addWidget(self.selectButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        # 更新按钮文本
        self._update_button_text()

    def _update_button_text(self):
        """更新按钮显示文本"""
        if not self.selected_languages:
            self.selectButton.setText("选择语言")
        else:
            # 显示选中的语言名称
            names = []
            for code in self.selected_languages[:3]:  # 最多显示3个
                name = next((n for c, n in self.languages if c == code), code)
                names.append(name)

            text = ", ".join(names)
            if len(self.selected_languages) > 3:
                text += f" 等 {len(self.selected_languages)} 种语言"
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


from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem  # noqa: E402


class AudioLanguageSelectionDialog(MessageBox):
    """音频备选语言提取对话框 (支持排序列表)"""

    def __init__(self, languages: list[tuple[str, str]], selected: list[str], parent=None):
        super().__init__(
            "选择并排序首选音轨语言", "选中的语言越靠前，优先级越高。可拖拽调整顺序。", parent
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
        left_layout.addWidget(SubtitleLabel("可选语言:", content_widget))
        self.available_list = QListWidget(content_widget)
        self.available_list.setMinimumWidth(240)
        self.available_list.setMinimumHeight(250)
        left_layout.addWidget(self.available_list)
        layout.addLayout(left_layout, stretch=1)

        # Middle Layout: Add/Remove buttons
        mid_layout = QVBoxLayout()
        mid_layout.addStretch(1)
        self.btn_add = PushButton("添加 >>", content_widget)
        self.btn_remove = PushButton("<< 移除", content_widget)
        mid_layout.addWidget(self.btn_add)
        mid_layout.addWidget(self.btn_remove)
        mid_layout.addStretch(1)
        layout.addLayout(mid_layout, stretch=0)

        # Right list: Selected
        right_layout = QVBoxLayout()
        right_layout.setSpacing(12)
        right_layout.addWidget(SubtitleLabel("已选排序 (拖拽调整):", content_widget))
        self.selected_list = QListWidget(content_widget)
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
            item = QListWidgetItem(f"{name} ({code})")
            item.setData(Qt.ItemDataRole.UserRole, code)
            self.selected_list.addItem(item)

        # 填充备选
        for code, name in self.languages:
            if code not in self.selected_languages_init:
                item = QListWidgetItem(f"{name} ({code})")
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

        self.selectButton = PushButton("设置首选音轨...", self)
        self.selectButton.clicked.connect(self._show_dialog)
        self.hBoxLayout.addWidget(self.selectButton, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)

        self._update_button_text()

    def _update_button_text(self):
        if not self.selected_languages:
            self.selectButton.setText("选择语言 (未设置)")
        else:
            names = []
            for code in self.selected_languages[:3]:
                name = next((n for c, n in self.languages if c == code), code)
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


from qfluentwidgets import TextEdit  # noqa: E402


class ProbePoolEditorDialog(MessageBox):
    """探针链接池编辑对话框"""

    def __init__(self, current_pool: list[str], parent=None):
        super().__init__(
            "管理探针链接池", "添加或删除风控探测时用于测试的 YouTube 链接（每行一个）。", parent
        )

        self.textEdit = TextEdit(self.widget)
        self.textEdit.setMinimumHeight(250)
        self.textEdit.setMinimumWidth(500)

        # 将现有的列表转成多行文本
        self.textEdit.setPlainText("\n".join(current_pool))

        self.textLayout.addWidget(self.textEdit)
        self.widget.setMinimumWidth(550)

    def get_links(self) -> list[str]:
        """获取编辑后的有效链接列表"""
        text = self.textEdit.toPlainText()
        links = []
        for line in text.split("\n"):
            line = line.strip()
            # 简单的验证，只保留非空且看起来像 url 的行
            if line and line.startswith("http"):
                links.append(line)
        return links


class DLEAccountNameDialog(MessageBox):
    """新增 DLE 账号名称输入对话框（Fluent 风格）。"""

    def __init__(self, parent=None):
        super().__init__("新增 DLE 账号", "请输入账号名称", parent)
        self.nameEdit = LineEdit(self.widget)
        self.nameEdit.setPlaceholderText("例如：A 账号")
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
        ("soft", "软嵌入（推荐） - 封装到容器，可开关，多语言"),
        ("external", "外置文件 - 独立.srt，易编辑，兼容性最佳"),
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


class SettingsPage(QWidget):
    """设置页面：管理下载、网络、核心组件配置 (重构版 - Pivot导航)"""

    clipboardAutoDetectChanged = Signal(bool)

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
        self._cookie_worker = None

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
            text="账号验证",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.generalInterface),
        )
        self.pivot.addItem(
            routeKey="downloadInterface",
            text="下载",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.downloadInterface),
        )
        self.pivot.addItem(
            routeKey="networkInterface",
            text="网络",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.networkInterface),
        )
        self.pivot.addItem(
            routeKey="featuresInterface",
            text="功能",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.featuresInterface),
        )
        self.pivot.addItem(
            routeKey="componentsInterface",
            text="组件",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.componentsInterface),
        )
        self.pivot.addItem(
            routeKey="systemInterface",
            text="系统",
            onClick=lambda: self.stackedWidget.setCurrentWidget(self.systemInterface),
        )

        self.pivot.setCurrentItem("generalInterface")
        self.stackedWidget.setCurrentWidget(self.generalInterface)
        self.stackedWidget.currentChanged.connect(self._on_current_tab_changed)

        # === General Tab ===
        self._init_account_group(self.generalScroll.widget(), self.generalLayout)

        # === Download Tab ===
        self._init_download_group(self.downloadScroll.widget(), self.downloadLayout)

        # === Network Tab ===
        self._init_network_group(self.networkScroll.widget(), self.networkLayout)

        # === Features Tab ===
        self._init_automation_group(self.featuresScroll.widget(), self.featuresLayout)
        self._init_postprocess_group(self.featuresScroll.widget(), self.featuresLayout)
        self._init_subtitle_group(self.featuresScroll.widget(), self.featuresLayout)
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
        self.downloadGroup = SettingCardGroup("下载选项", parent_widget)

        self.downloadFolderCard = PushSettingCard(
            "选择文件夹",
            FluentIcon.FOLDER,
            "默认保存路径",
            str(config_manager.get("download_dir")),
            self.downloadGroup,
        )
        self.downloadFolderCard.clicked.connect(self._select_download_folder)

        self.concurrentFragmentsCard = InlineComboBoxCard(
            FluentIcon.SPEED_HIGH,
            "分片并发数",
            "设置单个视频的分片下载线程数 (默认: 4)",
            [str(i) for i in range(1, 9)],
            self.downloadGroup,
        )
        current_frag = config_manager.get("concurrent_fragments", 4)
        self.concurrentFragmentsCard.comboBox.setCurrentIndex(max(0, min(7, int(current_frag) - 1)))
        self.concurrentFragmentsCard.comboBox.currentIndexChanged.connect(
            self._on_concurrent_fragments_changed
        )

        # Max Concurrent Downloads
        self.maxConcurrentCard = InlineComboBoxCard(
            FluentIcon.ALBUM,
            "最大同时下载数",
            "设置同时进行的下载任务数量 (默认: 3)",
            [str(i) for i in range(1, 11)],
            self.downloadGroup,
        )
        # Select current value
        current_max = config_manager.get("max_concurrent_downloads", 3)
        self.maxConcurrentCard.comboBox.setCurrentIndex(max(0, min(9, int(current_max) - 1)))
        self.maxConcurrentCard.comboBox.currentIndexChanged.connect(self._on_max_concurrent_changed)

        self.downloadGroup.addSettingCard(self.downloadFolderCard)
        self.downloadGroup.addSettingCard(self.concurrentFragmentsCard)
        self.downloadGroup.addSettingCard(self.maxConcurrentCard)
        layout.addWidget(self.downloadGroup)

        # Trigger warning check initially
        self._on_max_concurrent_changed(self.maxConcurrentCard.comboBox.currentIndex())

    def _init_network_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.networkGroup = SettingCardGroup("网络连接", parent_widget)

        self.proxyModeCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            "代理模式",
            "选择网络连接方式",
            ["不使用代理", "使用系统代理", "手动 HTTP 代理", "手动 SOCKS5 代理"],
            self.networkGroup,
        )
        self.proxyModeCard.comboBox.currentIndexChanged.connect(self._on_proxy_mode_changed)

        self.proxyEditCard = InlineLineEditCard(
            FluentIcon.EDIT,
            "自定义代理地址",
            "仅手动代理模式生效 (示例: 127.0.0.1:7890)",
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
        self.accountGroup = SettingCardGroup("账号验证", parent_widget)

        # === Cookie Sentinel 配置组 ===
        self.cookieModeCard = InlineComboBoxCard(
            FluentIcon.PEOPLE,
            "Cookie 来源",
            "选择 Cookie 获取方式（Cookie 卫士会自动维护生命周期）",
            [
                "🚀 自动从本地浏览器提取",
                "🔑 登录获取 (推荐)",
                "📄 手动导入 cookies.txt 文件",
            ],
            self.accountGroup,
        )
        self.cookieModeCard.comboBox.currentIndexChanged.connect(self._on_cookie_mode_changed)

        self.browserCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            "选择浏览器",
            "Chromium 内核需管理员权限，Firefox 内核无需管理员权限",
            [
                "Microsoft Edge",
                "Google Chrome (⚠️不稳定)",
                "Chromium",
                "Brave",
                "Opera",
                "Opera GX",
                "Vivaldi",
                "Arc",
                "Firefox",
                "LibreWolf",
                "百分浏览器 (Cent)",
            ],
            self.accountGroup,
        )
        self.browserCard.comboBox.currentIndexChanged.connect(self._on_cookie_browser_changed)

        # DLE 登录按钮
        self.dleLoginCard = PushSettingCard(
            "登录 YouTube",
            FluentIcon.GLOBE,
            "🔑 账号登录",
            "点击后将打开独立浏览器，请登录 YouTube 账号以自动提取 Cookie",
            self.accountGroup,
        )
        self.dleLoginCard.clicked.connect(self._on_dle_login_clicked)

        # DLE 账号选择
        self._dle_account_ids: list[str] = []
        self.dleAccountCard = InlineComboBoxCard(
            FluentIcon.PEOPLE,
            "DLE 账号",
            "选择当前用于登录提取与下载注入的账号",
            [],
            self.accountGroup,
        )
        self.dleAccountCard.comboBox.currentIndexChanged.connect(self._on_dle_account_changed)

        # 新增 DLE 账号
        self.dleAddAccountCard = PushSettingCard(
            "新增账号",
            FluentIcon.ADD,
            "DLE 账号管理",
            "创建新的 DLE 账号隔离存储（独立 profile 与缓存）",
            self.accountGroup,
        )
        self.dleAddAccountCard.clicked.connect(self._on_add_dle_account_clicked)

        # 删除当前 DLE 账号
        self.dleRemoveAccountCard = PushSettingCard(
            "删除当前账号",
            FluentIcon.DELETE,
            "删除 DLE 账号",
            "删除当前选中的 DLE 账号（至少保留 1 个）",
            self.accountGroup,
        )
        self.dleRemoveAccountCard.clicked.connect(self._on_remove_dle_account_clicked)

        # 手动刷新按钮
        self.refreshCookieCard = PushSettingCard(
            "立即刷新",
            FluentIcon.SYNC,
            "手动刷新 Cookie",
            "从浏览器重新提取 Cookie（可能需要管理员权限）",
            self.accountGroup,
        )
        self.refreshCookieCard.clicked.connect(self._on_refresh_cookie_clicked)

        # Cookie 文件选择
        self.cookieFileCard = PushSettingCard(
            "选择文件",
            FluentIcon.DOCUMENT,
            "Cookie 文件路径",
            "未选择",
            self.accountGroup,
        )
        self.cookieFileCard.clicked.connect(self._select_cookie_file)

        # Cookie 状态显示（带打开位置按钮）
        self.cookieStatusCard = PushSettingCard(
            "打开位置",
            FluentIcon.INFO,
            "Cookie 状态检测",
            "显示当前关联的 Cookie 存活状态",
            self.accountGroup,
        )
        self.cookieStatusCard.clicked.connect(self._open_cookie_location)

        self.accountGroup.addSettingCard(self.cookieModeCard)
        self.accountGroup.addSettingCard(self.browserCard)
        self.accountGroup.addSettingCard(self.dleAccountCard)
        self.accountGroup.addSettingCard(self.dleAddAccountCard)
        self.accountGroup.addSettingCard(self.dleRemoveAccountCard)
        self.accountGroup.addSettingCard(self.dleLoginCard)
        self.accountGroup.addSettingCard(self.refreshCookieCard)
        self.accountGroup.addSettingCard(self.cookieStatusCard)
        self.accountGroup.addSettingCard(self.cookieFileCard)

        # 一键诊断
        self.diagCard = PushSettingCard(
            "一键检测",
            FluentIcon.SEARCH,
            "环境诊断",
            "检测 Cookie、网络连通性、代理和 IP 风控状态",
            self.accountGroup,
        )
        self.diagCard.clicked.connect(self._on_diag_clicked)
        self.accountGroup.addSettingCard(self.diagCard)

        layout.addWidget(self.accountGroup)

        # Make Cookie dependent cards look like "children" of cookie mode card
        self._indent_setting_card(self.browserCard)
        self._indent_setting_card(self.dleAccountCard)
        self._indent_setting_card(self.dleAddAccountCard)
        self._indent_setting_card(self.dleRemoveAccountCard)
        self._indent_setting_card(self.dleLoginCard)
        self._indent_setting_card(self.refreshCookieCard)
        self._indent_setting_card(self.cookieFileCard)
        self._indent_setting_card(self.cookieStatusCard)

    def _init_component_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化核心组件与更新设置组"""
        self.coreGroup = SettingCardGroup("核心组件", parent_widget)

        # Check Updates on Startup
        self.checkUpdatesOnStartupCard = InlineSwitchCard(
            FluentIcon.SYNC,
            "启动时自动检查更新",
            "开启后，每隔 24 小时尝试自动检查 yt-dlp 和 ffmpeg 更新（默认开启）",
            parent=self.coreGroup,
        )
        self.checkUpdatesOnStartupCard.checkedChanged.connect(
            self._on_check_updates_startup_changed
        )

        # Update Source
        self.updateSourceCard = InlineComboBoxCard(
            FluentIcon.GLOBE,
            "组件更新源",
            "选择组件下载和检查更新的网络来源",
            ["GitHub (官方)", "GHProxy (加速镜像)"],
            self.coreGroup,
        )
        self.updateSourceCard.comboBox.currentIndexChanged.connect(self._on_update_source_changed)

        # yt-dlp Update Channel
        self.ytDlpChannelCard = InlineComboBoxCard(
            FluentIcon.SYNC,
            "yt-dlp 更新频道",
            "选择 yt-dlp 版本的更新分支",
            ["Stable (稳定版)", "Nightly (每夜版)", "Master (主线源码)"],
            self.coreGroup,
        )
        self.ytDlpChannelCard.comboBox.currentIndexChanged.connect(self._on_ytdlp_channel_changed)

        # New Component Cards
        self.ytDlpCard = ComponentSettingCard(
            "yt-dlp",
            FluentIcon.DOWNLOAD,
            "yt-dlp 引擎",
            "点击检查更新以获取最新版本",
            self.coreGroup,
        )

        self.ffmpegCard = ComponentSettingCard(
            "ffmpeg", FluentIcon.VIDEO, "FFmpeg 引擎", "点击检查更新以获取最新版本", self.coreGroup
        )

        # JS Runtime (Deno only for auto-update now)
        self.denoCard = ComponentSettingCard(
            "deno",
            FluentIcon.CODE,
            "JS Runtime (Deno)",
            "用于加速 yt-dlp 解析（点击检查更新）",
            self.coreGroup,
        )

        # POT Provider (PO Token 服务)
        self.potProviderCard = ComponentSettingCard(
            "pot-provider",
            FluentIcon.CERTIFICATE,
            "POT Provider",
            "用于绕过 YouTube 机器人检测（点击检查更新）",
            self.coreGroup,
        )

        # AtomicParsley (封面嵌入工具)
        self.atomicParsleyCard = ComponentSettingCard(
            "atomicparsley",
            FluentIcon.PHOTO,
            "AtomicParsley",
            "用于 MP4/M4A 封面嵌入（启用封面嵌入功能需要此工具）",
            self.coreGroup,
        )

        self.jsRuntimeCard = InlineComboBoxCard(
            FluentIcon.CODE,
            "JS Runtime 策略",
            "选择首选的 JavaScript 运行时",
            ["自动(推荐)", "Deno", "Node", "Bun", "QuickJS"],
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
        self.appearanceGroup = SettingCardGroup("外观", parent_widget)

        self.themeModeCard = InlineComboBoxCard(
            FluentIcon.BRUSH,
            "主题模式",
            "选择应用的色彩主题",
            ["跟随系统 (自动)", "浅色模式", "深色模式"],
            self.appearanceGroup,
        )
        self.themeModeCard.comboBox.currentIndexChanged.connect(self._on_theme_mode_changed)

        self.appearanceGroup.addSettingCard(self.themeModeCard)
        layout.addWidget(self.appearanceGroup)

    def _init_advanced_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.advancedGroup = SettingCardGroup("高级", parent_widget)

        self.poTokenCard = SmartSettingCard(
            FluentIcon.CODE,
            "YouTube PO Token(可选)",
            "可留空清除；保存后用于提升可用性（偏极客/实验性）",
            config_key="youtube_po_token",
            parent=self.advancedGroup,
            validator=self._validate_po_token,
            fixer=None,
            prefer_multiline=True,
            dialog_content="粘贴或输入 PO Token。允许留空；非空时将进行简单格式校验。",
        )

        self.jsRuntimePathCard = SmartSettingCard(
            FluentIcon.DOCUMENT,
            "JS Runtime 路径(可选)",
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

        self.advancedGroup.addSettingCard(self.poTokenCard)
        self.advancedGroup.addSettingCard(self.jsRuntimePathCard)
        layout.addWidget(self.advancedGroup)

    def _init_automation_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.automationGroup = SettingCardGroup("自动化", parent_widget)

        self.clipboardDetectCard = InlineSwitchCard(
            FluentIcon.EDIT,
            "剪贴板自动识别",
            "自动识别复制的 YouTube 链接并弹出解析窗口（默认关闭）",
            parent=self.automationGroup,
        )
        self.clipboardDetectCard.checkedChanged.connect(self._on_clipboard_detect_changed)

        self.clipboardActionModeCard = InlineComboBoxCard(
            FluentIcon.PLAY,
            "剪贴板识别默认行为",
            "选择自动识别到链接后的处理方式",
            ["智能识别 (推荐)", "仅普通下载", "仅 VR 下载", "仅下载字幕", "仅下载封面"],
            parent=self.automationGroup,
        )
        self.clipboardActionModeCard.comboBox.currentIndexChanged.connect(
            self._on_clipboard_action_mode_changed
        )

        self.clipboardWindowToFrontCard = InlineSwitchCard(
            FluentIcon.APPLICATION,
            "解析后置顶窗口",
            "识别到链接并弹出解析窗口时，自动将其置于前台（默认开启）",
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
        self.vrGroup = SettingCardGroup("VR / 360°", parent_widget)

        # 硬件状态 Banner
        self.vrHardwareStatusCard = SettingCard(
            FluentIcon.INFO,
            "硬件性能检测",
            "正在检测系统硬件...",
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
            "EAC 自动转码",
            "检测到 YouTube 专用 EAC 投影格式时，自动转换为通用的 Equirectangular 格式（耗时较长）",
            parent=self.vrGroup,
        )
        self.vrEacAutoConvertCard.checkedChanged.connect(self._on_vr_eac_auto_convert_changed)

        # 硬件加速策略
        self.vrHwAccelCard = InlineComboBoxCard(
            FluentIcon.SPEED_HIGH,
            "硬件加速策略",
            "选择转码时的硬件加速模式",
            ["自动 (推荐)", "强制 CPU (慢)", "强制 GPU (快)"],
            self.vrGroup,
        )
        self.vrHwAccelCard.comboBox.currentIndexChanged.connect(self._on_vr_hw_accel_changed)

        # 最大分辨率限制
        self.vrMaxResolutionCard = InlineComboBoxCard(
            FluentIcon.ZOOM,
            "最大转码分辨率",
            "超过此分辨率的视频将跳过转码（防止内存溢出或死机）",
            ["4K (2160p) - 安全", "5K/6K - 警告", "8K (4320p) - 高危"],
            self.vrGroup,
        )
        self.vrMaxResolutionCard.comboBox.currentIndexChanged.connect(
            self._on_vr_max_resolution_changed
        )

        # CPU 占用限制
        self.vrCpuPriorityCard = InlineComboBoxCard(
            FluentIcon.IOT,
            "转码性能模式",
            "控制 CPU 占用率和系统响应速度",
            ["低 (后台不卡顿)", "中 (均衡)", "高 (全速)"],
            self.vrGroup,
        )
        self.vrCpuPriorityCard.comboBox.currentIndexChanged.connect(
            self._on_vr_cpu_priority_changed
        )

        # 保留原片
        self.vrKeepSourceCard = InlineSwitchCard(
            FluentIcon.SAVE,
            "转码后保留原片",
            "防止转码失败导致源文件丢失",
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

            logger.warning("Swallowed exception in settings", exc_info=True)

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
        self.behaviorGroup = SettingCardGroup("行为策略", parent_widget)

        # 音频首选语言 (支持多选排序)
        config = config_manager.get("preferred_audio_languages", ["orig", "zh-Hans", "en"])
        if not isinstance(config, list):
            config = ["orig", "zh-Hans", "en"]

        langs = [
            ("orig", "原音/默认"),
            ("zh-Hans", "中文 (简体)"),
            ("zh-Hant", "中文 (繁体)"),
            ("en", "英语"),
            ("ja", "日语"),
            ("ko", "韩语"),
            ("ru", "俄语"),
            ("fr", "法语"),
            ("de", "德语"),
            ("es", "西班牙语"),
        ]

        self.preferredAudioLanguageCard = AudioLanguageMultiSelectCard(
            FluentIcon.MUSIC,
            "首选音轨语言 (多音轨视频)",
            "当视频包含多个语言配音时，优先下载哪种语言的轨段 (可多选并排序)",
            languages=langs,
            selected_default=config,
            parent=self.behaviorGroup,
        )
        self.preferredAudioLanguageCard.selectionChanged.connect(
            self._on_preferred_audio_language_changed
        )

        self.deletionPolicyCard = InlineComboBoxCard(
            FluentIcon.DELETE,
            "移除任务时的默认行为",
            "选择从列表中删除任务时的文件处理策略",
            ["每次询问 (默认)", "仅移除记录 (保留文件)", "彻底删除 (同时删除文件)"],
            self.behaviorGroup,
        )
        self.deletionPolicyCard.comboBox.currentIndexChanged.connect(
            self._on_deletion_policy_changed
        )

        self.playlistSkipAuthcheckCard = InlineSwitchCard(
            FluentIcon.VIDEO,
            "加速播放列表解析（实验性）",
            "跳过 YouTube 登录验证检查（authcheck）。可加快大列表解析，但可能导致部分受限视频无法解析（默认关闭）",
            parent=self.behaviorGroup,
        )
        self.playlistSkipAuthcheckCard.checkedChanged.connect(
            self._on_playlist_skip_authcheck_changed
        )

        self.autoRiskProbeCard = InlineSwitchCard(
            FluentIcon.CERTIFICATE,
            "自动风控探测",
            "在启动或切页时自动向 YouTube 发送探针请求，以便提前预知 Cookie 失效。开启可能增加 IP 暴露风险（默认关闭）",
            parent=self.behaviorGroup,
        )
        self.autoRiskProbeCard.checkedChanged.connect(self._on_auto_risk_probe_changed)

        self.probePoolCard = SettingCard(
            FluentIcon.LINK,
            "探针链接池管理",
            "管理用于风控探测测试的 YouTube 视频链接池（12小时内不会重复探测同一链接）",
            parent=self.behaviorGroup,
        )
        self.probePoolEditBtn = PushButton("编辑连接池")
        self.probePoolEditBtn.clicked.connect(self._on_probe_pool_clicked)
        self.probePoolCard.hBoxLayout.addWidget(
            self.probePoolEditBtn, 0, Qt.AlignmentFlag.AlignRight
        )
        self.probePoolCard.hBoxLayout.addSpacing(16)

        self.behaviorGroup.addSettingCard(self.preferredAudioLanguageCard)
        self.behaviorGroup.addSettingCard(self.deletionPolicyCard)
        self.behaviorGroup.addSettingCard(self.playlistSkipAuthcheckCard)
        self.behaviorGroup.addSettingCard(self.autoRiskProbeCard)
        self.behaviorGroup.addSettingCard(self.probePoolCard)

        # 使其变为下属卡片的视觉样式
        self._indent_setting_card(self.probePoolCard)
        layout.addWidget(self.behaviorGroup)

    def _init_postprocess_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化后处理设置组（封面嵌入、元数据等）"""
        self.postprocessGroup = SettingCardGroup("后处理", parent_widget)

        # 封面嵌入开关
        self.embedThumbnailCard = InlineSwitchCard(
            FluentIcon.PHOTO,
            "嵌入封面图片",
            "将视频缩略图嵌入到下载文件中作为封面（支持 MP4/MKV/MP3/M4A/FLAC/OGG/OPUS 等格式）",
            parent=self.postprocessGroup,
        )
        self.embedThumbnailCard.checkedChanged.connect(self._on_embed_thumbnail_changed)

        # 元数据嵌入开关
        self.embedMetadataCard = InlineSwitchCard(
            FluentIcon.TAG,
            "嵌入元数据",
            "将视频标题、作者、描述等信息嵌入到下载文件中（推荐开启）",
            parent=self.postprocessGroup,
        )
        self.embedMetadataCard.checkedChanged.connect(self._on_embed_metadata_changed)

        self.postprocessGroup.addSettingCard(self.embedThumbnailCard)
        self.postprocessGroup.addSettingCard(self.embedMetadataCard)

        # === SponsorBlock 广告跳过 ===
        # 主开关
        self.sponsorBlockCard = InlineSwitchCard(
            FluentIcon.CANCEL,
            "SponsorBlock 广告跳过",
            "自动跳过视频中的赞助广告、自我推广等片段（基于社区标注）",
            parent=self.postprocessGroup,
        )
        self.sponsorBlockCard.checkedChanged.connect(self._on_sponsorblock_changed)

        # 类别选择（点击按钮打开对话框）
        self.sponsorBlockCategoriesCard = SettingCard(
            FluentIcon.SETTING,
            "跳过类别设置",
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
        self.subtitleGroup = SettingCardGroup("字幕下载", parent_widget)

        # 字幕启用开关
        self.subtitleEnabledCard = InlineSwitchCard(
            FluentIcon.DOCUMENT,
            "启用字幕下载",
            "自动下载视频字幕（支持多语言、嵌入、双语合成）",
            parent=self.subtitleGroup,
        )
        self.subtitleEnabledCard.checkedChanged.connect(self._on_subtitle_enabled_changed)

        # 语言多选卡片 (NEW)
        config = config_manager.get_subtitle_config()
        current_languages = config.default_languages if config.default_languages else []
        self.subtitleLanguagesCard = LanguageMultiSelectCard(
            FluentIcon.GLOBE,
            "字幕语言",
            "选择要下载的字幕语言（可多选）",
            languages=COMMON_SUBTITLE_LANGUAGES,
            selected_default=current_languages,
            parent=self.subtitleGroup,
        )
        self.subtitleLanguagesCard.selectionChanged.connect(self._on_subtitle_languages_changed)

        # 嵌入类型下拉框卡片 (NEW)
        self.subtitleEmbedTypeCard = EmbedTypeComboCard(
            FluentIcon.VIDEO,
            "嵌入类型",
            "选择字幕的封装方式",
            default=config.embed_type,
            parent=self.subtitleGroup,
        )
        self.subtitleEmbedTypeCard.valueChanged.connect(self._on_subtitle_embed_type_changed)

        # 嵌入模式 (询问/总是/从不)
        self.subtitleEmbedModeCard = InlineComboBoxCard(
            FluentIcon.CHECKBOX,
            "嵌入确认",
            "是否在下载前询问是否嵌入字幕",
            ["总是嵌入", "从不嵌入", "每次询问"],
            parent=self.subtitleGroup,
        )
        self.subtitleEmbedModeCard.comboBox.currentIndexChanged.connect(
            self._on_subtitle_embed_mode_changed
        )

        # 原始：外置字幕格式和保留开关已删除

        # 添加卡片到组
        self.subtitleGroup.addSettingCard(self.subtitleEnabledCard)
        self.subtitleGroup.addSettingCard(self.subtitleLanguagesCard)
        self.subtitleGroup.addSettingCard(self.subtitleEmbedTypeCard)
        self.subtitleGroup.addSettingCard(self.subtitleEmbedModeCard)

        # 缩进依赖项
        self._indent_setting_card(self.subtitleLanguagesCard)
        self._indent_setting_card(self.subtitleEmbedTypeCard)
        self._indent_setting_card(self.subtitleEmbedModeCard)

        layout.addWidget(self.subtitleGroup)

    def _init_about_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        self.aboutGroup = SettingCardGroup("关于", parent_widget)
        self.aboutCard = HyperlinkCard(
            "https://github.com/prideicker/FluentYTDL",
            "访问项目仓库",
            FluentIcon.GITHUB,
            "FluentYTDL",
            "基于 PySide6 & Fluent Design 构建",
            self.aboutGroup,
        )
        self.aboutGroup.addSettingCard(self.aboutCard)
        layout.addWidget(self.aboutGroup)

    def _init_log_group(self, parent_widget: QWidget | None, layout: QVBoxLayout) -> None:
        """初始化日志管理组"""
        self.logGroup = SettingCardGroup("日志管理", parent_widget)

        # 日志管理卡片
        self.logCard = SettingCard(
            FluentIcon.DOCUMENT,
            "运行日志",
            f"日志目录: {LOG_DIR}",
            self.logGroup,
        )

        # 添加按钮到卡片
        self.viewLogBtn = PushButton("查看日志", self.logCard)
        self.viewLogBtn.clicked.connect(self._on_view_log_clicked)

        self.openLogDirBtn = ToolButton(FluentIcon.FOLDER, self.logCard)
        self.openLogDirBtn.setToolTip("打开日志目录")
        self.openLogDirBtn.installEventFilter(
            ToolTipFilter(self.openLogDirBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.openLogDirBtn.clicked.connect(self._on_open_log_dir)

        self.cleanLogBtn = ToolButton(FluentIcon.DELETE, self.logCard)
        self.cleanLogBtn.setToolTip("清理所有日志")
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
        from .components.log_viewer_dialog import LogViewerDialog

        dialog = LogViewerDialog(self.window())
        dialog.exec()

    def _on_open_log_dir(self):
        """打开日志目录"""
        try:
            if os.path.exists(LOG_DIR):
                os.startfile(LOG_DIR)
            else:
                InfoBar.warning("目录不存在", f"{LOG_DIR} 尚未创建", parent=self.window())
        except Exception as e:
            InfoBar.error("错误", str(e), parent=self.window())

    def _on_clean_log_clicked(self):
        """清理所有日志"""
        from qfluentwidgets import MessageBox

        box = MessageBox(
            "确认清理", f"确定要删除所有日志文件吗？\n\n日志目录: {LOG_DIR}", self.window()
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

                            logger.warning("Swallowed exception in settings", exc_info=True)
                    InfoBar.success("清理完成", "已删除所有日志文件", parent=self.window())
                else:
                    InfoBar.info("无需清理", "日志目录不存在", parent=self.window())
            except Exception as e:
                InfoBar.error("清理失败", str(e), parent=self.window())

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

        # 设置 Cookie 模式
        if current_source == AuthSourceType.FILE:
            self.cookieModeCard.comboBox.setCurrentIndex(2)  # 手动文件
            if auth_service._current_file_path:
                self.cookieFileCard.setContent(auth_service._current_file_path)
        elif current_source == AuthSourceType.DLE:
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

        # 加载 DLE 账号列表
        self._reload_dle_account_combo(select_current=True)

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

        # Behavior: auto risk probe
        auto_risk_probe = bool(config_manager.get("auto_risk_probe", False))
        self.autoRiskProbeCard.switchButton.blockSignals(True)
        self.autoRiskProbeCard.switchButton.setChecked(auto_risk_probe)
        self.autoRiskProbeCard.switchButton.blockSignals(False)

        # Postprocess: embed thumbnail
        embed_thumbnail = bool(config_manager.get("embed_thumbnail", True))
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

        # Subtitle: embed type (NEW)
        self.subtitleEmbedTypeCard.comboBox.blockSignals(True)
        self.subtitleEmbedTypeCard.set_value(subtitle_config.embed_type)
        self.subtitleEmbedTypeCard.comboBox.blockSignals(False)

        # Subtitle: embed mode
        embed_mode = str(config_manager.get("subtitle_embed_mode", "always"))
        embed_mode_map = {"always": 0, "never": 1, "ask": 2}
        self.subtitleEmbedModeCard.comboBox.blockSignals(True)
        self.subtitleEmbedModeCard.comboBox.setCurrentIndex(embed_mode_map.get(embed_mode, 0))
        self.subtitleEmbedModeCard.comboBox.blockSignals(False)

        # 原始格式加载与开关状态加载已删除

        # VR Settings
        self.vrEacAutoConvertCard.switchButton.blockSignals(True)
        self.vrEacAutoConvertCard.switchButton.setChecked(
            config_manager.get("vr_eac_auto_convert", False)
        )
        self.vrEacAutoConvertCard.switchButton.blockSignals(False)

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
                f"⚠️ 当前: {val} (高风险! 可能导致 YouTube 封禁 IP 429)"
            )
            self.maxConcurrentCard.setTitle("最大同时下载数 (慎用)")
        else:
            self.maxConcurrentCard.setContent(f"当前: {val}")
            self.maxConcurrentCard.setTitle("最大同时下载数")

        # Immediately apply new limit to pending queue
        download_manager.pump()

    def _on_update_source_changed(self, index: int) -> None:
        source = "ghproxy" if index == 1 else "github"
        config_manager.set("update_source", source)
        InfoBar.success("设置已更新", f"下载源已切换为: {source}", duration=5000, parent=self)

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
            InfoBar.success("设置已更新", f"主题已切换为: {mode}", duration=3000, parent=self)

    def _on_check_updates_startup_changed(self, checked: bool) -> None:
        config_manager.set("check_updates_on_startup", bool(checked))
        InfoBar.success(
            "设置已更新",
            "已开启启动时自动检查更新" if checked else "已关闭启动时自动检查更新",
            duration=5000,
            parent=self,
        )

    def _on_clipboard_detect_changed(self, checked: bool) -> None:
        config_manager.set("clipboard_auto_detect", bool(checked))
        self.clipboardAutoDetectChanged.emit(bool(checked))
        InfoBar.success(
            "设置已更新",
            "剪贴板自动识别已开启" if checked else "剪贴板自动识别已关闭",
            duration=5000,
            parent=self,
        )

    def _on_clipboard_window_to_front_changed(self, checked: bool) -> None:
        config_manager.set("clipboard_window_to_front", bool(checked))
        InfoBar.success(
            "设置已更新",
            "已开启解析后窗口置顶" if checked else "已关闭解析后窗口置顶",
            duration=5000,
            parent=self,
        )

    def _on_clipboard_action_mode_changed(self, index: int) -> None:
        modes = ["smart", "standard", "vr", "subtitle", "cover"]
        if 0 <= index < len(modes):
            mode = modes[index]
            config_manager.set("clipboard_action_mode", mode)
            InfoBar.success(
                "设置已更新", f"剪贴板识别行为已更改为: {mode}", duration=5000, parent=self
            )

    def _on_deletion_policy_changed(self, index: int) -> None:
        # Combo texts order: Ask, KeepFiles, DeleteFiles
        policies = ["AlwaysAsk", "KeepFiles", "DeleteFiles"]
        if 0 <= index < len(policies):
            policy = policies[index]
            config_manager.set("deletion_policy", policy)
            InfoBar.success("设置已更新", f"删除策略已更改为: {policy}", duration=5000, parent=self)

    def _on_playlist_skip_authcheck_changed(self, checked: bool) -> None:
        config_manager.set("playlist_skip_authcheck", bool(checked))
        InfoBar.success(
            "设置已更新",
            "已开启：加速播放列表解析（实验性）"
            if checked
            else "已关闭：加速播放列表解析（实验性）",
            duration=5000,
            parent=self,
        )

    def _on_auto_risk_probe_changed(self, checked: bool) -> None:
        config_manager.set("auto_risk_probe", bool(checked))
        InfoBar.success(
            "设置已更新",
            "已开启自动风控探测" if checked else "已关闭自动风控探测",
            duration=5000,
            parent=self,
        )

    def _on_embed_thumbnail_changed(self, checked: bool) -> None:
        """处理封面嵌入开关变更"""
        config_manager.set("embed_thumbnail", bool(checked))
        InfoBar.success(
            "设置已更新",
            "已开启封面嵌入（支持 MP4/MKV/MP3/M4A/FLAC/OGG/OPUS 等格式）"
            if checked
            else "已关闭封面嵌入",
            duration=5000,
            parent=self,
        )

    def _on_embed_metadata_changed(self, checked: bool) -> None:
        """处理元数据嵌入开关变更"""
        config_manager.set("embed_metadata", bool(checked))
        InfoBar.success(
            "设置已更新",
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
                InfoBar.success(
                    "SponsorBlock 已启用",
                    f"将跳过: {cat_display}",
                    duration=5000,
                    parent=self,
                )
            else:
                InfoBar.warning(
                    "SponsorBlock 已启用",
                    "请在下方选择要跳过的类别",
                    duration=5000,
                    parent=self,
                )
        else:
            InfoBar.info(
                "SponsorBlock 已关闭",
                "视频将保留原始内容",
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
        from .components.sponsorblock_dialog import SponsorBlockCategoriesDialog

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
                InfoBar.success(
                    "类别已更新",
                    f"已选择 {len(selected)} 个类别",
                    duration=3000,
                    parent=self,
                )
            else:
                InfoBar.warning(
                    "未选择类别",
                    "请至少选择一个要跳过的类别",
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
            InfoBar.success(
                "设置已更新",
                f"代理模式已切换为: {self.proxyModeCard.comboBox.currentText()}",
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
            InfoBar.success("保存成功", f"代理已更新为 {new_proxy}", duration=5000, parent=self)
        else:
            InfoBar.info("已清空", "代理地址已清空。", duration=5000, parent=self)

    def _on_probe_pool_clicked(self):
        pool = config_manager.get("probe_link_pool")
        if not isinstance(pool, list):
            pool = []

        dialog = ProbePoolEditorDialog(pool, self.window())
        if dialog.exec():
            new_links = dialog.get_links()
            if not new_links:
                InfoBar.error(
                    "配置错误", "连接池不能为空，至少需要一个有效的探测 URL", parent=self.window()
                )
                return

            config_manager.set("probe_link_pool", new_links)
            # 用户修改了链接池，清除旧的伪随机历史记录
            config_manager.set("probe_link_history", {})

            InfoBar.success(
                "保存成功",
                f"探针链接池已更新，共计 {len(new_links)} 个可用链接",
                parent=self.window(),
            )

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
            self.dleAccountCard.setVisible(False)
            self.dleAddAccountCard.setVisible(False)
            self.dleRemoveAccountCard.setVisible(False)
            self.dleLoginCard.setVisible(False)
            self.refreshCookieCard.setVisible(True)
            self.cookieFileCard.setVisible(False)

            InfoBar.success(
                "已切换到自动提取",
                f"将从 {auth_service.current_source_display} 自动提取 Cookie",
                duration=3000,
                parent=self,
            )

        elif index == 1:
            # DLE 登录获取模式
            auth_service.set_source(AuthSourceType.DLE, auto_refresh=False)

            self._reload_dle_account_combo(select_current=True)

            self.browserCard.setVisible(False)
            self.dleAccountCard.setVisible(True)
            self.dleAddAccountCard.setVisible(True)
            self.dleRemoveAccountCard.setVisible(True)
            self.dleLoginCard.setVisible(True)
            self.refreshCookieCard.setVisible(False)
            self.cookieFileCard.setVisible(False)

            InfoBar.success(
                "已切换到登录获取模式",
                "请点击「登录 YouTube」按钮进行账号认证",
                duration=3000,
                parent=self,
            )

        else:
            # 手动文件模式 (index == 2)
            auth_service.set_source(AuthSourceType.FILE, auto_refresh=False)

            self.browserCard.setVisible(False)
            self.dleAccountCard.setVisible(False)
            self.dleAddAccountCard.setVisible(False)
            self.dleRemoveAccountCard.setVisible(False)
            self.dleLoginCard.setVisible(False)
            self.refreshCookieCard.setVisible(False)
            self.cookieFileCard.setVisible(True)

            InfoBar.info("已切换到手动导入", "请选择 cookies.txt 文件", duration=3000, parent=self)

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

            # DLE 登录按钮在浏览器提取模式下始终隐藏
            self.dleLoginCard.setVisible(False)

            # Chromium 内核浏览器 v130+ 需要管理员权限
            from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

            # DLE 方案不再需要管理员权限 (Edge/Chrome)
            is_dle_supported = source in (AuthSourceType.EDGE, AuthSourceType.CHROME)

            if not is_dle_supported and source in ADMIN_REQUIRED_BROWSERS and not is_admin():
                box = MessageBox(
                    f"{name} 需要管理员权限",
                    f"{name} 使用了 App-Bound 加密保护，\n"
                    f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                    "点击「以管理员身份重启」后将自动完成提取。\n\n"
                    "或者您可以：\n"
                    "• 选择 Firefox/LibreWolf 浏览器（无需管理员权限）\n"
                    "• 手动导出 Cookie 文件",
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
                "正在切换浏览器",
                f"正在从 {name} 提取 Cookies，请稍候...",
                duration=3000,
                parent=self,
            )

            # 清理旧worker
            if self._cookie_worker is not None:
                self._cookie_worker.deleteLater()

            # 创建Qt工作线程
            self._cookie_worker = CookieRefreshWorker(self)

            # 连接信号（自动在主线程执行）
            def on_finished(success: bool, message: str, need_admin: bool = False):
                if success:
                    InfoBar.success(
                        "切换成功", f"已从 {name} 提取 Cookies", duration=8000, parent=self
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
                self._cookie_worker = None

            self._cookie_worker.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
            self._cookie_worker.start()

    def _on_dle_login_clicked(self):
        """DLE 登录按钮点击 - 启动浏览器登录流程"""
        from ..auth.auth_service import AuthSourceType, auth_service

        # 保证处于 DLE 模式
        auth_service.set_source(AuthSourceType.DLE, auto_refresh=False)

        account = auth_service.current_dle_account
        account_name = account.display_name if account else "默认账号"

        self.dleLoginCard.button.setEnabled(False)
        self.dleLoginCard.setContent(
            f"正在后台提取登录态（{account_name}），必要时会自动显示登录窗口..."
        )

        # 执行刷新
        self._do_cookie_refresh()

        # 挂载完成回调（worker 已在 _do_cookie_refresh 中创建）
        if self._cookie_worker:

            def _on_dle_finished(success: bool, message: str, need_admin: bool = False):
                # 恢复按钮状态
                self.dleLoginCard.button.setEnabled(True)

                if success:
                    self.dleLoginCard.setContent("✅ 登录成功，Cookie 已提取")
                    from ..auth.cookie_sentinel import cookie_sentinel

                    current_acc = auth_service.current_dle_account
                    acc_cookie = current_acc.cached_cookie_path if current_acc else "未知"
                    InfoBar.success(
                        "登录成功",
                        f"YouTube Cookie 已成功提取并保存（{account_name}）\n"
                        f"账号文件: {acc_cookie}\n"
                        f"统一文件: {cookie_sentinel.cookie_path}",
                        duration=5000,
                        parent=self,
                    )
                else:
                    self.dleLoginCard.setContent("❌ 登录未完成，请重新点击「登录 YouTube」")
                    # 解析错误消息，去掉「刷新异常:」前缀
                    clean_msg = message
                    if clean_msg.startswith("刷新异常: "):
                        clean_msg = clean_msg[len("刷新异常: ") :]

                    # 显示错误 InfoBar
                    InfoBar.warning(
                        "登录未完成",
                        clean_msg,
                        duration=8000,
                        parent=self,
                    )

            self._cookie_worker.finished.connect(
                _on_dle_finished,
                Qt.ConnectionType.QueuedConnection,
            )

    def _reload_dle_account_combo(self, select_current: bool = True) -> None:
        """刷新 DLE 账号下拉列表"""
        from ..auth.auth_service import auth_service

        accounts = auth_service.list_dle_accounts(platform="youtube")
        self._dle_account_ids = [a.account_id for a in accounts]

        combo = self.dleAccountCard.comboBox
        combo.blockSignals(True)
        combo.clear()

        for acc in accounts:
            label = acc.display_name
            if acc.is_default:
                label += " (默认)"
            combo.addItem(label)

        if select_current and self._dle_account_ids:
            cur = auth_service.current_dle_account_id
            idx = self._dle_account_ids.index(cur) if cur in self._dle_account_ids else 0
            combo.setCurrentIndex(idx)

        combo.blockSignals(False)

    def _on_dle_account_changed(self, index: int) -> None:
        """切换当前 DLE 账号"""
        from ..auth.auth_service import auth_service

        if index < 0 or index >= len(self._dle_account_ids):
            return

        account_id = self._dle_account_ids[index]
        if auth_service.set_current_dle_account(account_id):
            account = auth_service.current_dle_account
            name = account.display_name if account else "未知账号"
            InfoBar.success(
                "已切换 DLE 账号",
                f"当前账号: {name}",
                duration=2500,
                parent=self,
            )
            self._update_cookie_status()

    def _on_add_dle_account_clicked(self) -> None:
        """新增 DLE 账号"""
        from ..auth.auth_service import auth_service

        dialog = DLEAccountNameDialog(self)
        dialog.yesButton.setText("创建")
        dialog.cancelButton.setText("取消")
        if not dialog.exec():
            return

        display_name = dialog.get_account_name()
        if not display_name:
            InfoBar.warning("名称为空", "请输入有效账号名称", duration=2500, parent=self)
            return

        account = auth_service.create_dle_account(display_name=display_name, platform="youtube")
        auth_service.set_current_dle_account(account.account_id)
        self._reload_dle_account_combo(select_current=True)

        InfoBar.success(
            "账号已创建",
            f"已创建并切换到: {account.display_name}",
            duration=3000,
            parent=self,
        )
        self._update_cookie_status()

    def _on_remove_dle_account_clicked(self) -> None:
        """删除当前 DLE 账号"""
        from qfluentwidgets import MessageBox

        from ..auth.auth_service import auth_service

        account = auth_service.current_dle_account
        if not account:
            InfoBar.warning("无可删账号", "当前没有可删除的 DLE 账号", duration=2500, parent=self)
            return

        box = MessageBox(
            "删除当前 DLE 账号",
            f"确定删除账号「{account.display_name}」吗？\n\n至少需要保留 1 个账号。",
            self,
        )
        box.yesButton.setText("删除")
        box.cancelButton.setText("取消")
        if not box.exec():
            return

        if not auth_service.delete_dle_account(account.account_id, remove_storage=False):
            InfoBar.error("删除失败", "至少需要保留一个账号", duration=3000, parent=self)
            return

        self._reload_dle_account_combo(select_current=True)
        InfoBar.success("删除成功", f"已删除: {account.display_name}", duration=3000, parent=self)
        self._update_cookie_status()

    def _on_refresh_cookie_clicked(self):
        """手动刷新 Cookie 按钮点击"""
        from qfluentwidgets import MessageBox

        from ..auth.auth_service import AuthSourceType, auth_service
        from ..utils.admin_utils import is_admin

        current_source = auth_service.current_source

        # 检查是否是 Chromium 内核浏览器且非管理员 - 直接提示重启
        from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

        # DLE 方案不再需要管理员权限 (Edge/Chrome)
        is_dle_supported = current_source in (AuthSourceType.EDGE, AuthSourceType.CHROME)

        if not is_dle_supported and current_source in ADMIN_REQUIRED_BROWSERS and not is_admin():
            browser_name = auth_service.current_source_display

            box = MessageBox(
                f"{browser_name} 需要管理员权限",
                f"{browser_name} 使用了 App-Bound 加密保护，\n"
                f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                "点击「以管理员身份重启」后将自动完成提取。\n\n"
                "或者您可以：\n"
                "• 切换到 Firefox/LibreWolf 浏览器（无需管理员权限）\n"
                "• 手动导出 Cookie 文件",
                self,
            )
            box.yesButton.setText("以管理员身份重启")
            box.cancelButton.setText("取消")

            if box.exec():
                from ..utils.admin_utils import restart_as_admin

                restart_as_admin(f"提取 {browser_name} Cookie")
            return

        # 非 Edge/Chrome 或已是管理员，正常刷新
        self._do_cookie_refresh()

    def _do_cookie_refresh(self):
        """实际执行Cookie刷新（已确认权限或非Edge/Chrome）"""
        # 禁用按钮
        self.refreshCookieCard.setEnabled(False)
        self.refreshCookieCard.button.setText("刷新中...")

        # 显示进度提示
        InfoBar.info("正在刷新 Cookie", "请稍候...", duration=3000, parent=self)

        # 清理旧worker
        if self._cookie_worker is not None:
            self._cookie_worker.deleteLater()

        # 创建Qt工作线程
        self._cookie_worker = CookieRefreshWorker(self)

        # 连接信号（自动在主线程执行）
        def on_finished(success: bool, message: str, need_admin: bool = False):
            # 1. 总是重置按钮状态
            self.refreshCookieCard.setEnabled(True)
            self.refreshCookieCard.button.setText("立即刷新")

            # 2. 显示结果消息
            if success:
                InfoBar.success("刷新成功", message, duration=8000, parent=self)
            else:
                # 显示多行错误消息
                lines = message.split("\n")
                if len(lines) > 1:
                    title = lines[0]
                    content = "\n".join(lines[1:])
                else:
                    title = "Cookie 刷新失败"
                    content = message

                InfoBar.error(title, content, duration=15000, parent=self)

            # 3. 总是更新Cookie状态显示
            try:
                self._update_cookie_status()
            except Exception as e:
                from ..utils.logger import logger

                logger.error(f"更新Cookie状态显示失败: {e}")

            # 清理worker
            self._cookie_worker = None

        self._cookie_worker.finished.connect(on_finished, Qt.ConnectionType.QueuedConnection)
        self._cookie_worker.start()

    def _select_cookie_file(self):
        """选择 Cookie 文件并导入到 bin/cookies.txt"""

        from ..auth.auth_service import AuthSourceType, auth_service

        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择 Cookies 文件", "", "Cookies 文件 (*.txt);;所有文件 (*.*)"
        )

        if file_path:
            # 验证提取写入一条龙导入
            status = auth_service.import_manual_cookie_file(file_path)

            if not status.valid:
                InfoBar.error("文件格式有问题", status.message, duration=5000, parent=self)
                return

            try:
                # 设置为文件模式
                auth_service.set_source(
                    AuthSourceType.FILE, file_path=file_path, auto_refresh=False
                )

                self.cookieFileCard.setContent(f"已导入: {status.cookie_count} 个 Cookie")
                InfoBar.success(
                    "导入成功",
                    f"已导入 {status.cookie_count} 个 Cookie 到 bin/cookies.txt",
                    duration=3000,
                    parent=self,
                )
            except Exception as e:
                InfoBar.error("导入失败", f"复制文件时出错: {e}", duration=5000, parent=self)
                return

            self._update_cookie_status()

    def _open_cookie_location(self):
        """打开 Cookie 文件所在位置"""
        import os
        import subprocess

        from ..auth.cookie_sentinel import cookie_sentinel

        cookie_path = cookie_sentinel.cookie_path

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
                    "目录不存在", f"Cookie 目录尚未创建: {folder}", duration=3000, parent=self
                )

    def _update_cookie_status(self):
        """更新 Cookie 状态显示"""
        try:
            from ..auth.auth_service import AuthSourceType, auth_service
            from ..auth.cookie_sentinel import cookie_sentinel

            current_source = auth_service.current_source
            info = cookie_sentinel.get_status_info()

            # === 特殊模式处理 ===
            if current_source == AuthSourceType.NONE:
                self.cookieStatusCard.contentLabel.setText("⚪ 未启用 Cookie 验证")
                return

            if not info["exists"]:
                if current_source == AuthSourceType.DLE:
                    status_text = "🔑 DLE 模式 — 尚未登录，请点击「登录 YouTube」按钮"
                elif current_source == AuthSourceType.FILE:
                    status_text = "❌ Cookie 文件不存在，请重新选择文件"
                else:
                    status_text = f"❌ 尚无 Cookie — 请点击「立即刷新」从 {auth_service.current_source_display} 提取"
                self.cookieStatusCard.contentLabel.setText(status_text)
                return

            # === 有 Cookie 文件时的详细状态 ===
            age = info["age_minutes"]
            age_str = f"{int(age)} 分钟前" if age is not None else "未知时间"
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
                    source_text = f"{actual_display}（当前配置: {info['source']}）"
                else:
                    source_text = actual_display
            elif info.get("expiring_soon"):
                emoji = "⏳"
                source_text = actual_display
            elif info["is_stale"]:
                emoji = "⚠️"
                source_text = actual_display
            else:
                emoji = "✅"
                source_text = actual_display

            status_text = f"{emoji} {source_text} | 更新于 {age_str} | {cookie_count} 个 Cookie"

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

            self.cookieStatusCard.contentLabel.setText(status_text)

        except Exception as e:
            self.cookieStatusCard.contentLabel.setText(f"状态获取失败: {e}")

    def _on_diag_clicked(self) -> None:
        """一键环境诊断 — 弹出二级诊断对话框"""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal
        from PySide6.QtWidgets import QGridLayout, QLabel, QSizePolicy
        from qfluentwidgets import (
            BodyLabel,
            CaptionLabel,
            IndeterminateProgressRing,
            MessageBoxBase,
            SubtitleLabel,
        )

        STEP_LABELS = {
            1: "Cookie 文件",
            2: "Cookie 有效性",
            3: "YouTube 连通性",
            4: "代理配置",
            5: "Cookie + IP 实测",
            6: "POT 服务",
        }

        # ---- Worker (same logic, moved here for closure) ----
        class _DiagWorker(QThread):
            step_done = QSignal(int, bool, str)
            all_done = QSignal(str)

            def run(self):
                import time as _time

                results = {}
                info = None

                # Step 1: Cookie 文件
                try:
                    from ..auth.cookie_sentinel import cookie_sentinel

                    info = cookie_sentinel.get_status_info()
                    if info["exists"]:
                        count = info.get("cookie_count", 0)
                        source = info.get("actual_source_display") or info.get("source") or "未知"
                        self.step_done.emit(1, True, f"存在 ({count} 个, 来源: {source})")
                        results["file"] = True
                    else:
                        self.step_done.emit(1, False, "Cookie 文件不存在")
                        results["file"] = False
                except Exception as e:
                    self.step_done.emit(1, False, f"检测失败: {e}")
                    results["file"] = False

                # Step 2: Cookie 有效性
                try:
                    if results.get("file") and info is not None:
                        valid = info.get("cookie_valid", False)
                        msg = info.get("cookie_valid_msg", "")
                        if valid:
                            self.step_done.emit(2, True, msg or "Cookie 有效")
                        else:
                            self.step_done.emit(2, False, msg or "Cookie 无效或不完整")
                        results["valid"] = valid
                    else:
                        self.step_done.emit(2, False, "跳过 (无 Cookie 文件)")
                        results["valid"] = False
                except Exception as e:
                    self.step_done.emit(2, False, f"检测失败: {e}")
                    results["valid"] = False

                # Step 3: YouTube 连通性
                try:
                    from ..utils.error_parser import probe_youtube_connectivity

                    t0 = _time.monotonic()
                    reachable = probe_youtube_connectivity(timeout=8.0)
                    latency = int((_time.monotonic() - t0) * 1000)
                    if reachable:
                        self.step_done.emit(3, True, f"可达 (延迟 {latency}ms)")
                    else:
                        self.step_done.emit(3, False, f"不可达 (耗时 {latency}ms)")
                    results["network"] = reachable
                except Exception as e:
                    self.step_done.emit(3, False, f"检测失败: {e}")
                    results["network"] = False

                # Step 4: 代理配置
                try:
                    from ..core.config_manager import config_manager

                    proxy_mode = str(config_manager.get("proxy_mode") or "off").lower().strip()
                    proxy_url = str(config_manager.get("proxy_url", "") or "").strip()
                    if proxy_mode == "manual" and proxy_url:
                        self.step_done.emit(4, True, f"手动代理: {proxy_url}")
                    elif proxy_mode == "system":
                        self.step_done.emit(4, True, "使用系统代理")
                    else:
                        self.step_done.emit(4, False, "未配置代理 (可能无法访问 YouTube)")
                    results["proxy"] = proxy_mode != "off"
                except Exception as e:
                    self.step_done.emit(4, False, f"检测失败: {e}")
                    results["proxy"] = False

                # Step 5: Cookie + IP 综合实测
                try:
                    if results.get("network"):
                        from ..auth.cookie_sentinel import cookie_sentinel as _cs
                        from ..utils.error_parser import probe_cookie_and_ip

                        cpath = _cs.get_cookie_file_path() if results.get("file") else None
                        probe = probe_cookie_and_ip(cookie_file=cpath, timeout=15.0)
                        results["cookie_real"] = probe["cookie_ok"]
                        results["ip_ok"] = probe["ip_ok"]

                        if probe["cookie_ok"] and probe["ip_ok"]:
                            self.step_done.emit(5, True, probe["detail"])
                        elif probe["ip_ok"] and not probe["cookie_ok"]:
                            self.step_done.emit(5, False, probe["detail"])
                        else:
                            self.step_done.emit(5, False, probe["detail"])
                    else:
                        self.step_done.emit(5, False, "跳过 (网络不通)")
                        results["ip_ok"] = False
                        results["cookie_real"] = False
                except Exception as e:
                    self.step_done.emit(5, False, f"检测失败: {e}")
                    results["ip_ok"] = False
                    results["cookie_real"] = False

                # Step 6: POT 服务状态 (L0 + L1 + L2 分层验证)
                try:
                    from ..core.config_manager import config_manager

                    if config_manager.get("pot_provider_enabled", True):
                        from ..youtube.pot_manager import pot_manager

                        if not pot_manager.is_running():
                            # 服务未运行，尝试自动启动
                            pot_manager.start_server()

                        # 用 get_health_status() 做综合诊断
                        health = pot_manager.get_health_status()

                        if health["overall_ok"]:
                            detail_parts = [f"端口 {health['port']}"]
                            detail_parts.append(health["token_detail"])
                            if health["minter_ok"]:
                                detail_parts.append(health["minter_detail"])
                            self.step_done.emit(6, True, " | ".join(detail_parts))
                            results["pot"] = True
                        elif health["running"] and not health["token_ok"]:
                            # 服务在运行但 Token 生成有问题
                            self.step_done.emit(
                                6, False, f"服务运行中但 Token 无效: {health['token_detail']}"
                            )
                            results["pot"] = False
                        else:
                            self.step_done.emit(
                                6,
                                False,
                                health["summary"]
                                or "未运行且启动失败 — 请检查 POT Provider 是否已安装",
                            )
                            results["pot"] = False
                    else:
                        self.step_done.emit(6, True, "已禁用 (不影响基本功能)")
                        results["pot"] = True
                except Exception as e:
                    self.step_done.emit(6, False, f"检测失败: {e}")
                    results["pot"] = False

                # 综合建议
                suggestions = []
                if not results.get("proxy"):
                    suggestions.append("建议在「设置 > 网络连接」中配置代理")
                if not results.get("network"):
                    suggestions.append("网络不通，请检查代理是否运行")
                elif not results.get("ip_ok"):
                    suggestions.append("当前 IP/节点被 YouTube 风控，建议更换代理节点")
                if not results.get("file"):
                    suggestions.append("请先通过登录或浏览器提取获取 Cookie")
                elif results.get("cookie_real") is False and results.get("ip_ok"):
                    suggestions.append("Cookie 已失效 (服务端验证不通过)，请重新获取")
                if not results.get("pot", True):
                    # 区分"未运行"和"运行中但 Token 异常"
                    try:
                        from ..youtube.pot_manager import pot_manager

                        if pot_manager.is_running():
                            suggestions.append(
                                "POT Provider 服务运行中但 Token 生成异常（可能是首次初始化较慢或 BotGuard 被限制）。"
                                "建议重启应用或更新 POT Provider 版本"
                            )
                        else:
                            suggestions.append(
                                "POT Provider 服务未运行，可能导致机器人检测。"
                                "请在「组件管理」中安装或更新 POT Provider"
                            )
                    except Exception:
                        suggestions.append(
                            "POT Provider 服务异常，可能导致机器人检测。"
                            "请在「组件管理」中安装或更新 POT Provider"
                        )
                elif not results.get("valid"):
                    suggestions.append("Cookie 缺少必要字段，请重新获取")

                if not suggestions:
                    self.all_done.emit("✅ 所有检测通过，环境正常")
                else:
                    self.all_done.emit("💡 " + "；".join(suggestions))

        # ---- Dialog ----
        class _DiagDialog(MessageBoxBase):
            def __init__(self, parent):
                super().__init__(parent)
                self.titleLabel = SubtitleLabel("🔍 环境诊断", self)
                self.viewLayout.addWidget(self.titleLabel)

                # 5-row grid: icon | label | result
                grid_widget = QWidget(self)
                self._grid = QGridLayout(grid_widget)
                self._grid.setContentsMargins(0, 12, 0, 12)
                self._grid.setHorizontalSpacing(12)
                self._grid.setVerticalSpacing(8)

                self._icon_labels: dict[int, QLabel] = {}
                self._result_labels: dict[int, CaptionLabel] = {}
                self._spinners: dict[int, IndeterminateProgressRing] = {}

                for step in range(1, 7):
                    row = step - 1
                    # spinner (will be hidden when done)
                    spinner = IndeterminateProgressRing(grid_widget)
                    spinner.setFixedSize(16, 16)
                    self._spinners[step] = spinner
                    self._grid.addWidget(spinner, row, 0)

                    # icon label (hidden initially, shown when done)
                    icon_lbl = BodyLabel("", grid_widget)
                    icon_lbl.setFixedWidth(20)
                    icon_lbl.hide()
                    self._icon_labels[step] = icon_lbl
                    self._grid.addWidget(icon_lbl, row, 0)

                    # step name
                    name_lbl = BodyLabel(STEP_LABELS[step], grid_widget)
                    name_lbl.setFixedWidth(110)
                    self._grid.addWidget(name_lbl, row, 1)

                    # result
                    result_lbl = CaptionLabel("检测中...", grid_widget)
                    result_lbl.setWordWrap(True)
                    result_lbl.setSizePolicy(
                        QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
                    )
                    self._result_labels[step] = result_lbl
                    self._grid.addWidget(result_lbl, row, 2)

                self.viewLayout.addWidget(grid_widget)

                # suggestion row
                self._suggestionLabel = CaptionLabel("", self)
                self._suggestionLabel.setWordWrap(True)
                self._suggestionLabel.hide()
                self.viewLayout.addWidget(self._suggestionLabel)

                # buttons
                self.yesButton.setText("关闭")
                self.cancelButton.hide()

                self.widget.setMinimumWidth(480)

            def update_step(self, step: int, ok: bool, detail: str):
                # hide spinner, show icon
                self._spinners[step].hide()
                icon_lbl = self._icon_labels[step]
                icon_lbl.setText("✅" if ok else "❌")
                icon_lbl.show()
                self._result_labels[step].setText(detail)

            def set_suggestion(self, text: str):
                self._suggestionLabel.setText(text)
                self._suggestionLabel.show()

        # create dialog and worker
        dlg = _DiagDialog(self.window())
        worker = _DiagWorker(dlg)

        def _on_step(step, ok, detail):
            dlg.update_step(step, ok, detail)

        def _on_all_done(suggestion):
            dlg.set_suggestion(suggestion)
            # also update the card description briefly
            self.diagCard.setContent(suggestion)

        worker.step_done.connect(_on_step, _Qt.ConnectionType.QueuedConnection)
        worker.all_done.connect(_on_all_done, _Qt.ConnectionType.QueuedConnection)
        worker.start()

        dlg.exec()

        # cleanup
        try:
            worker.quit()
            worker.wait(2000)
        except Exception:
            from ...utils.logger import logger

            logger.warning("Swallowed exception in settings", exc_info=True)

    def _on_check_updates_startup_changed(self, checked: bool) -> None:
        config_manager.set("check_updates_on_startup", checked)

    def _on_update_source_changed(self, index: int) -> None:
        source_map = {0: "github", 1: "ghproxy"}
        mode = source_map.get(index, "github")
        config_manager.set("update_source", mode)
        InfoBar.success(
            "设置已更新",
            f"组件更新源已切换为: {self.updateSourceCard.comboBox.currentText()}",
            duration=3000,
            parent=self,
        )

    def _on_ytdlp_channel_changed(self, index: int) -> None:
        channel_map = {0: "stable", 1: "nightly", 2: "master"}
        mode = channel_map.get(index, "stable")
        config_manager.set("ytdlp_channel", mode)
        InfoBar.success(
            "设置已更新",
            f"yt-dlp 更新频道已切换为: {self.ytDlpChannelCard.comboBox.currentText()} (下次更新时生效)",
            duration=5000,
            parent=self,
        )

    def _on_js_runtime_changed(self, index: int) -> None:
        mapping = {0: "auto", 1: "deno", 2: "node", 3: "bun", 4: "quickjs"}
        mode = mapping.get(index, "auto")
        config_manager.set("js_runtime", mode)
        InfoBar.success(
            "设置已更新",
            f"JS Runtime 已切换为: {self.jsRuntimeCard.comboBox.currentText()}",
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

            logger.warning("Swallowed exception in settings", exc_info=True)

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
            "选择 yt-dlp.exe",
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
                "路径无效",
                "未找到该文件，已回退为自动检测（优先内置，其次 PATH）。",
                duration=15000,
                parent=self,
            )
            config_manager.set("yt_dlp_exe_path", "")
            try:
                self.ytDlpCard.setValue("")
                self.ytDlpCard.setContent(self._yt_dlp_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)
            return

        config_manager.set("yt_dlp_exe_path", path)
        try:
            self.ytDlpCard.setValue(path)
            self.ytDlpCard.setContent(f"自定义: {path}" if path else self._yt_dlp_status_text())
        except Exception:
            from ...utils.logger import logger

            logger.warning("Swallowed exception in settings", exc_info=True)

    def _yt_dlp_status_text(self) -> str:
        cfg = str(config_manager.get("yt_dlp_exe_path") or "").strip()
        if cfg:
            try:
                if Path(cfg).exists():
                    return "已就绪（手动指定）"
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)

        if is_frozen():
            p = find_bundled_executable(
                "yt-dlp.exe",
                "yt-dlp/yt-dlp.exe",
                "yt_dlp/yt-dlp.exe",
            )
            if p is not None:
                return "已就绪（内置）"

        which = shutil.which("yt-dlp") or shutil.which("yt-dlp.exe")
        if which:
            return "已就绪（环境（PATH））"

        return "未就绪（无法解析/下载）"

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
            "选择 ffmpeg.exe",
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
                    "路径可能无效",
                    "未找到该文件，请确认 ffmpeg.exe 路径是否正确。",
                    duration=15000,
                    parent=self,
                )
            try:
                self.ffmpegCard.setValue(path)
                self.ffmpegCard.setContent(f"自定义: {path}")
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)
        else:
            try:
                self.ffmpegCard.setValue("")
                self.ffmpegCard.setContent(self._ffmpeg_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)

    def _ffmpeg_status_text(self) -> str:
        custom = str(config_manager.get("ffmpeg_path") or "").strip()
        if custom:
            try:
                if Path(custom).exists():
                    return "已就绪（手动指定）"
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)

        # Auto-detect priority: bundled (_internal) > PATH
        bundled = (
            find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe") if is_frozen() else None
        )
        if bundled is not None:
            return "已就绪（内置）"

        which = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        if which:
            return "已就绪（环境（PATH））"

        return "未找到（解决：使用 full 包内置 FFmpeg，或安装 FFmpeg 并加入 PATH，或在此处选择）"

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

            logger.warning("Swallowed exception in settings", exc_info=True)

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
            "自定义": "手动指定",
            "内置": "内置",
            "PATH": "环境（PATH）",
            "winget": "winget",
        }
        source_text = source_map.get(source, source or "")

        if preferred == "auto":
            if exe is None:
                return (
                    "未就绪（解决：使用 full 包内置 Deno，或安装 deno 并加入 PATH，或在此处选择）"
                )
            return f"已就绪（自动：{label} / {source_text or '未知'}）"

        if exe is None:
            preferred_label = {
                "deno": "Deno",
                "node": "Node",
                "bun": "Bun",
                "quickjs": "QuickJS",
            }.get(preferred, preferred)
            return f"未就绪: {preferred_label}（解决：优先使用内置，其次 PATH；也可在此处选择）"
        return f"已就绪（{source_text or '未知'}）"

    def _select_js_runtime_path(self) -> None:
        file, _ = QFileDialog.getOpenFileName(
            self,
            "选择 JS Runtime 可执行文件（可选）",
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
                "路径无效",
                "未找到该文件，已回退为自动检测（优先内置，其次 PATH）。",
                parent=self,
            )
            config_manager.set("js_runtime_path", "")
            try:
                self.jsRuntimePathCard.setValue("")
                self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
            except Exception:
                from ...utils.logger import logger

                logger.warning("Swallowed exception in settings", exc_info=True)
            return

        config_manager.set("js_runtime_path", path)
        try:
            self.jsRuntimePathCard.setValue(path)
            self.jsRuntimePathCard.setContent(self._js_runtime_status_text())
        except Exception:
            from ...utils.logger import logger

            logger.warning("Swallowed exception in settings", exc_info=True)

    def _check_js_runtime(self) -> None:
        rid, exe, source = self._resolve_js_runtime_exe()
        if exe is None:
            InfoBar.warning(
                "未找到 JS Runtime",
                "请安装 deno/node/bun/quickjs 或在此处指定可执行文件路径。",
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

                        logger.warning("Swallowed exception in settings", exc_info=True)
                    try:
                        si = subprocess.STARTUPINFO()  # type: ignore[attr-defined]
                        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW  # type: ignore[attr-defined]
                        si.wShowWindow = 0
                        kwargs["startupinfo"] = si
                    except Exception:
                        from ...utils.logger import logger

                        logger.warning("Swallowed exception in settings", exc_info=True)

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
            f"类型: {label}\n版本: {ver_line}\n路径: {exe}\n来源: {source or '未知'}",
            duration=5000,
            parent=self,
        )
        self.jsRuntimePathCard.setContent(self._js_runtime_status_text())

    def _check_yt_dlp(self) -> None:
        exe = resolve_yt_dlp_exe()
        if exe is None:
            InfoBar.error(
                "未找到 yt-dlp.exe",
                "请在此处选择 yt-dlp.exe，或将 yt-dlp.exe 放入 _internal/yt-dlp/，或加入 PATH。",
                duration=15000,
                parent=self,
            )
            return

        ver = run_version() or "(unknown)"
        InfoBar.info(
            "yt-dlp",
            f"版本: {ver}\n路径: {exe}\n更新方式: 替换该 yt-dlp.exe",
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
        status = "已启用" if checked else "已禁用"
        InfoBar.success("字幕设置", f"字幕下载{status}", duration=3000, parent=self)

    def _on_subtitle_languages_changed(self, languages: list[str]) -> None:
        """语言选择改变回调"""
        if not languages:
            languages = ["zh-Hans", "en"]
        config_manager.set("subtitle_default_languages", languages)
        InfoBar.success(
            "语言设置", f"已选择字幕语言: {', '.join(languages)}", duration=3000, parent=self
        )

    def _on_subtitle_embed_type_changed(self, embed_type: str) -> None:
        """嵌入类型改变回调"""
        if embed_type not in ("soft", "external"):
            embed_type = "soft"
        config = config_manager.get_subtitle_config()
        config.embed_type = cast(Literal["soft", "external"], embed_type)
        config_manager.set_subtitle_config(config)
        type_names = {"soft": "软嵌入", "external": "外置文件"}
        InfoBar.success(
            "嵌入类型",
            f"字幕嵌入类型: {type_names.get(embed_type, embed_type)}",
            duration=3000,
            parent=self,
        )
        # 嵌入类型变更时联动可见性
        self._update_keep_separate_visibility()

    def _on_subtitle_keep_separate_changed(self, checked: bool) -> None:
        """保留外置字幕文件开关改变"""
        config_manager.set("subtitle_write_separate_file", checked)
        status = "保留" if checked else "不保留"
        InfoBar.success("字幕文件", f"嵌入后{status}外置字幕文件", duration=3000, parent=self)

    def _on_subtitle_embed_mode_changed(self, index: int) -> None:
        mode_map = {0: "always", 1: "never", 2: "ask"}
        mode = mode_map.get(index, "always")
        config_manager.set("subtitle_embed_mode", mode)
        display_map = {"always": "总是嵌入", "never": "从不嵌入", "ask": "每次询问"}
        display_text = display_map.get(mode, mode)
        InfoBar.success("嵌入模式", f"字幕嵌入策略: {display_text}", duration=3000, parent=self)

    def _on_subtitle_format_changed(self, index: int) -> None:
        format_map = {0: "srt", 1: "ass", 2: "vtt"}
        fmt = format_map.get(index, "srt")
        config_manager.set("subtitle_format", fmt)
        InfoBar.success("格式设置", f"外置字幕格式: {fmt.upper()}", duration=3000, parent=self)

    def _update_keep_separate_visibility(self) -> None:
        """根据嵌入类型更新「保留外置字幕文件」开关的可见性"""
        # enabled = self.subtitleEnabledCard.switchButton.isChecked() # No longer check enabled
        embed_type = self.subtitleEmbedTypeCard.get_value()
        # 仅软嵌入/硬嵌入时显示此选项（外置模式下字幕文件本身就是产物）
        self.subtitleKeepSeparateCard.setVisible(embed_type == "soft")

    def _update_vr_hardware_status(self) -> None:
        """更新 VR 硬件状态 Banner"""
        self.vrHardwareStatusCard.setContent("检测中...")
        QThread.msleep(100)  # Give UI a chance to update

        # 强制刷新硬件检测缓存，确保能检测到最新的环境变化
        hardware_manager.refresh_hardware_status()

        mem_gb = hardware_manager.get_system_memory_gb()
        has_gpu = hardware_manager.has_dedicated_gpu()
        encoders = hardware_manager.get_gpu_encoders()

        status_text = f"内存: {mem_gb} GB"
        if has_gpu:
            status_text += f" | GPU 加速: 可用 ({', '.join(encoders)})"
            desc = "您的硬件支持 VR 硬件转码。"
            if mem_gb >= 16:
                desc += " (支持 8K 转码)"
            else:
                desc += " (建议限制在 4K/6K)"
        else:
            status_text += " | GPU 加速: 不可用"
            desc = "未检测到硬件编码器，将使用 CPU 转码 (较慢)。"

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
                "耗时操作警告",
                "EAC 转码非常消耗资源。如果没有高性能显卡，8K 视频可能需要数小时。",
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
                    "未检测到 GPU 编码器",
                    "当前系统没有可用的硬件编码器（NVENC/QSV/AMF），\n"
                    "「强制 GPU」将自动回落为 CPU 转码，速度会很慢。\n"
                    "建议改为「自动 (推荐)」。",
                    duration=8000,
                    parent=self,
                )

    def _on_vr_max_resolution_changed(self, index: int) -> None:
        res_map = {0: 2160, 1: 3200, 2: 4320}
        val = res_map.get(index, 2160)
        config_manager.set("vr_max_resolution", val)
        if val >= 4320:
            InfoBar.error(
                "高风险设置",
                "开启 8K 转码极易导致内存溢出或系统卡死。请确保您有 32GB+ 内存和高端显卡。",
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
        self.subtitleEmbedModeCard.setVisible(True)

        embed_type = self.subtitleEmbedTypeCard.get_value()
        self.subtitleEmbedModeCard.setEnabled(embed_type == "soft")
