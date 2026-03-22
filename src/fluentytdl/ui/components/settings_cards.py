from __future__ import annotations

import os
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import QFileDialog, QVBoxLayout, QWidget
from qfluentwidgets import (
    CheckBox,
    ComboBox,
    FluentIcon,
    LineEdit,
    MessageBox,
    ProgressBar,
    PushButton,
    SettingCard,
    SpinBox,
    SubtitleLabel,
    SwitchButton,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
)

from ...core.dependency_manager import dependency_manager
from .custom_info_bar import InfoBar

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
        from ...auth.auth_service import auth_service
        from ...auth.cookie_sentinel import cookie_sentinel
        from ...utils.logger import logger

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


class InlineSpinBoxCard(SettingCard):
    """A fluent setting card with a right-aligned SpinBox."""

    def __init__(
        self,
        icon,
        title: str,
        content: str | None,
        min_value: int = 1,
        max_value: int = 100,
        default_value: int = 1,
        parent=None,
    ):
        super().__init__(icon, title, content, parent)
        self.spinBox = SpinBox(self)
        self.spinBox.setRange(min_value, max_value)
        self.spinBox.setValue(default_value)
        self.hBoxLayout.addWidget(self.spinBox, 0, Qt.AlignmentFlag.AlignRight)
        self.hBoxLayout.addSpacing(16)


class LanguageSelectionDialog(MessageBox):
    """语言多选对话框"""

    def __init__(self, languages: list[tuple[str, str]], selected: list[str], parent=None):
        super().__init__("选择字幕语言", "", parent)

        self.languages = languages
        self.selected_languages = selected.copy() if selected else []
        self.checkboxes = {}

        # 创建内容布局
        from PySide6.QtWidgets import QFrame, QGridLayout, QScrollArea

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
        from PySide6.QtWidgets import QHBoxLayout

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
