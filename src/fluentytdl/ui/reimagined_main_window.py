from __future__ import annotations

import os
from enum import Enum
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QMenu,
    QScrollArea,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    FluentIcon,
    FluentWindow,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    SplashScreen,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ..core.config_manager import config_manager
from ..download.download_manager import download_manager
from ..utils.logger import logger
from ..utils.paths import resource_path
from .components.clipboard_monitor import ClipboardMonitor
from .components.download_config_window import DownloadConfigWindow
from .cover_download_page import CoverDownloadPage
from .help_window import HelpWindow
from .pages.history_page import HistoryPage
from .parse_page import ParsePage
from .settings_page import SettingsPage
from .subtitle_download_page import SubtitleDownloadPage
from .unified_task_list_page import UnifiedTaskListPage
from .vr_parse_page import VRParsePage
from .welcome_wizard import WelcomeWizardDialog


class DeletionPolicy(Enum):
    ALWAYS_ASK = "alwaysask"
    KEEP_FILES = "keep"
    DELETE_FILES = "delete"

    @classmethod
    def from_config_str(cls, raw: Any) -> DeletionPolicy:
        if not raw:
            return cls.ALWAYS_ASK
        s = str(raw).lower().strip()
        if "keep" in s:
            return cls.KEEP_FILES
        if "delete" in s or "remove" in s:
            return cls.DELETE_FILES
        return cls.ALWAYS_ASK


class TaskListPage(QWidget):
    """通用的任务列表页面"""

    def __init__(self, title: str, icon: FluentIcon, parent=None):
        super().__init__(parent)
        self.setObjectName(title.lower().replace(" ", "_"))
        self.page_title = title

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(20, 20, 20, 20)
        self.v_layout.setSpacing(10)

        # 保存游离的后台删除线程
        self._delete_workers: list[QThread] = []

        # === 工具栏 ===
        self.tool_bar = QHBoxLayout()
        self.title_label = SubtitleLabel(self.page_title, self)
        self.tool_bar.addWidget(self.title_label)
        self.tool_bar.addStretch(1)

        # 占位：具体按钮由外部添加或子类实现
        self.action_layout = QHBoxLayout()
        self.tool_bar.addLayout(self.action_layout)

        self.v_layout.addLayout(self.tool_bar)

        # === 列表区域 ===
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setStyleSheet("background: transparent;")

        self.scroll_widget = QWidget()
        self.scroll_widget.setStyleSheet("background: transparent;")
        self.scroll_layout = QVBoxLayout(self.scroll_widget)
        self.scroll_layout.setContentsMargins(0, 0, 0, 0)
        self.scroll_layout.setSpacing(8)
        self.scroll_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll_area.setWidget(self.scroll_widget)
        self.v_layout.addWidget(self.scroll_area)

    def add_card(self, card: QWidget):
        self.scroll_layout.addWidget(card)
        card.show()

    def remove_card(self, card: QWidget):
        self.scroll_layout.removeWidget(card)
        card.setParent(None)  # Important to detach

    def count(self) -> int:
        return self.scroll_layout.count()

    def set_selection_mode(self, enabled: bool):
        for i in range(self.scroll_layout.count()):
            item = self.scroll_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                set_selection_mode = getattr(w, "set_selection_mode", None)
                if callable(set_selection_mode):
                    set_selection_mode(enabled)

    def get_selected_cards(self) -> list[QWidget]:
        selected = []
        for i in range(self.scroll_layout.count()):
            item = self.scroll_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                is_selected = getattr(w, "is_selected", None)
                if callable(is_selected) and is_selected():
                    selected.append(w)
        return selected

    def select_all(self):
        for i in range(self.scroll_layout.count()):
            item = self.scroll_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                select_box = getattr(w, "selectBox", None)
                set_checked = getattr(select_box, "setChecked", None)
                if callable(set_checked):
                    set_checked(True)

    def deselect_all(self):
        for i in range(self.scroll_layout.count()):
            item = self.scroll_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                select_box = getattr(w, "selectBox", None)
                set_checked = getattr(select_box, "setChecked", None)
                if callable(set_checked):
                    set_checked(False)


class MainWindow(FluentWindow):
    def __init__(self, app_controller=None) -> None:
        super().__init__()
        self.controller = app_controller

        # 检查管理员模式
        from ..utils.admin_utils import is_admin

        self._is_admin = is_admin()

        # 设置窗口标题（管理员模式添加标识）
        title = "FluentYTDL Pro"
        if self._is_admin:
            title += " (管理员)"
        self.setWindowTitle(title)

        self.resize(1150, 780)

        # 居中
        desktop = QApplication.screens()[0].availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        # 活跃的子窗口列表 (防止GC回收)
        self._active_sub_windows = []

        # === 初始化页面 ===
        # 统一任务列表页面（替代原有的四个分页）
        self.task_page = UnifiedTaskListPage(self)
        self.history_page = HistoryPage(self)

        self.parse_page = ParsePage(self)
        self.vr_parse_page = VRParsePage(self)
        self.subtitle_page = SubtitleDownloadPage(self)
        self.cover_page = CoverDownloadPage(self)
        self.settings_interface = SettingsPage(self)

        # === 初始化导航 ===
        self.init_navigation()

        # === 初始化工具栏按钮 ===
        self.init_page_actions()

        # === 状态栏 ===
        self.init_status_bar()

        # === 系统组件 ===
        self.init_system_tray()
        self.init_clipboard_monitor()

        # 启动动画
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.finish()

        # 信号连接
        self.parse_page.parse_requested.connect(
            lambda url: self.show_selection_dialog(url, smart_detect=False, playlist_flat=True)
        )
        self.vr_parse_page.parse_requested.connect(self.show_vr_selection_dialog)
        self.subtitle_page.parse_requested.connect(self.show_subtitle_selection_dialog)
        self.cover_page.parse_requested.connect(self.show_cover_selection_dialog)
        self.history_page.reparse_requested.connect(
            lambda url: self.show_selection_dialog(url, smart_detect=True)
        )
        self.settings_interface.clipboardAutoDetectChanged.connect(
            self.set_clipboard_monitor_enabled
        )

        # 统一任务列表页面信号
        self.task_page.card_remove_requested.connect(self.on_remove_task)
        self.task_page.card_resume_requested.connect(self.on_pause_resume_task)
        self.task_page.card_folder_requested.connect(self.on_open_target_folder)
        self.task_page.route_to_parse.connect(lambda: self.switchTo(self.parse_page))

        # 批量操作命令栏信号
        self.task_page.batch_start_requested.connect(self.on_batch_start)
        self.task_page.batch_pause_requested.connect(self.on_batch_pause)
        self.task_page.batch_delete_requested.connect(self.on_batch_delete)

        # 历史记录实时更新
        from ..storage.history_service import on_history_added

        on_history_added(self._on_history_record_added)

        # === 标题栏扩展 ===
        self.init_title_bar()

        # === 首次启动检测 ===
        QTimer.singleShot(1000, self.check_first_run)

        # === 管理员模式：自动刷新 Cookie ===
        if self._is_admin:
            QTimer.singleShot(2000, self.on_admin_mode_cookie_refresh)

    def init_navigation(self):
        # 1. 新建任务
        self.addSubInterface(
            self.parse_page, FluentIcon.ADD, "新建任务", position=NavigationItemPosition.TOP
        )

        # 2. VR 下载
        self.addSubInterface(
            self.vr_parse_page, FluentIcon.GAME, "VR 下载", position=NavigationItemPosition.TOP
        )

        # 2.1 字幕下载
        self.addSubInterface(
            self.subtitle_page, FluentIcon.FONT, "字幕下载", position=NavigationItemPosition.TOP
        )

        # 2.2 封面下载
        self.addSubInterface(
            self.cover_page, FluentIcon.PHOTO, "封面下载", position=NavigationItemPosition.TOP
        )

        # 3. 任务列表（统一页面，内部使用 Pivot 过滤）
        self.addSubInterface(
            self.task_page, FluentIcon.DOWNLOAD, "任务列表", position=NavigationItemPosition.TOP
        )

        # 4. 下载历史
        self.addSubInterface(
            self.history_page, FluentIcon.HISTORY, "下载历史", position=NavigationItemPosition.TOP
        )

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            "设置",
            position=NavigationItemPosition.BOTTOM,
        )

    def init_page_actions(self):
        """为统一任务页面设置操作按钮"""
        page = self.task_page

        # 全部开始/暂停按钮 (Secondary Actions)
        start_all = TransparentToolButton(FluentIcon.PLAY, self)
        start_all.setToolTip("全部开始")
        start_all.installEventFilter(
            ToolTipFilter(start_all, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        start_all.clicked.connect(self.on_start_all)

        pause_all = TransparentToolButton(FluentIcon.PAUSE, self)
        pause_all.setToolTip("全部暂停")
        pause_all.installEventFilter(
            ToolTipFilter(pause_all, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        pause_all.clicked.connect(self.on_pause_all)

        # 打开目录
        open_dir = TransparentToolButton(FluentIcon.FOLDER, self)
        open_dir.setToolTip("打开下载目录")
        open_dir.installEventFilter(
            ToolTipFilter(open_dir, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        open_dir.clicked.connect(self.on_open_download_dir)

        # 清空已完成
        clear_completed = TransparentToolButton(FluentIcon.DELETE, self)
        clear_completed.setToolTip("清空已完成记录")
        clear_completed.installEventFilter(
            ToolTipFilter(clear_completed, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        clear_completed.clicked.connect(self.on_clear_completed)

        # 批量操作按钮
        from qfluentwidgets import TransparentPushButton

        batch_btn = TransparentPushButton(FluentIcon.CHECKBOX, "批量操作", page)
        batch_btn.setToolTip("进入或退出批量模式")
        batch_btn.installEventFilter(
            ToolTipFilter(batch_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )

        def toggle_batch():
            is_batch = getattr(page, "_is_batch_mode", False)
            page.set_selection_mode(not is_batch)

        def _on_selection_mode_changed(is_batch: bool):
            if is_batch:
                batch_btn.setIcon(FluentIcon.CANCEL)
                batch_btn.setText("退出批量")
            else:
                batch_btn.setIcon(FluentIcon.CHECKBOX)
                batch_btn.setText("批量操作")

        batch_btn.clicked.connect(toggle_batch)
        page.selection_mode_changed.connect(_on_selection_mode_changed)

        # 添加到布局 (分组)
        page.action_layout.setSpacing(0)

        # 2. 全局控制
        page.action_layout.addWidget(start_all)
        page.action_layout.addWidget(pause_all)
        page.action_layout.addWidget(open_dir)
        page.action_layout.addWidget(clear_completed)

        # 分隔
        page.action_layout.addSpacing(16)

        # 3. 批量模式触发器 (靠右)
        page.action_layout.addWidget(batch_btn)

    def init_status_bar(self):
        # FluentWindow 没有原生 statusBar，我们手动添加到底部
        # 注意：FluentWindow 的布局是 stackedWidget，我们需要修改主布局
        # 但 FluentWindow 封装较深，通常建议在各个 Page 底部加，或者使用 InfoBar
        # 这里我们尝试在 NavigationInterface 下方或者整个 Window 底部加
        # 简单起见，我们在每个 Page 底部加？不，那样不全局。
        # 我们可以使用 overlay 或者修改 FluentWindow 的 layout。
        # 鉴于时间，我们暂时略过全局状态栏，或者只在 DownloadingPage 显示。
        # 用户需求：全局状态栏。
        # 我们可以创建一个 QWidget 作为底部条，添加到 self.layout() (如果是 QVBoxLayout)
        # FluentWindow 的 layout 是 QHBoxLayout (Nav + Stack)。
        # 我们可以把 Stack 换成 VBox(Stack + StatusBar)。
        pass

    # ... (系统托盘、剪贴板逻辑复用 main_window.py) ...
    def init_system_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        icon_path = resource_path("assets", "logo.png")
        chosen_icon = None
        try:
            if icon_path.exists():
                chosen_icon = QIcon(str(icon_path))
            else:
                win_icon = self.windowIcon()
                if not win_icon.isNull():
                    chosen_icon = win_icon
        except Exception:
            chosen_icon = None

        # Fallback: generate a simple colored pixmap to avoid "No Icon set" warnings
        if chosen_icon is None or chosen_icon.isNull():
            try:
                pix = QPixmap(16, 16)
                pix.fill(QColor(64, 120, 230))
                chosen_icon = QIcon(pix)
            except Exception:
                chosen_icon = QIcon()

        self.tray_icon.setIcon(chosen_icon)
        tray_menu = QMenu()
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self.quit_app)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        tray_menu.addAction(quit_action)
        self.tray_icon.setContextMenu(tray_menu)
        # Show only after a valid icon has been set to avoid Qt warning
        try:
            self.tray_icon.show()
        except Exception:
            pass
        self.tray_icon.activated.connect(self._on_tray_icon_activated)

    def _on_tray_icon_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.showNormal()
            self.activateWindow()

    def quit_app(self):
        download_manager.stop_all()
        QApplication.quit()

    def init_clipboard_monitor(self):
        enabled = bool(config_manager.get("clipboard_auto_detect") or False)
        self.set_clipboard_monitor_enabled(enabled)

    def set_clipboard_monitor_enabled(self, enabled: bool):
        if not enabled:
            mon = getattr(self, "clipboard_monitor", None)
            if mon:
                try:
                    mon.youtube_url_detected.disconnect(self.on_youtube_url_detected)
                    mon.deleteLater()
                except Exception:
                    pass
                self.clipboard_monitor = None
            return
        if getattr(self, "clipboard_monitor", None) is None:
            self.clipboard_monitor = ClipboardMonitor()
            self.clipboard_monitor.youtube_url_detected.connect(self.on_youtube_url_detected)

    def on_youtube_url_detected(self, url: str):
        if not self.isVisible():
            self.tray_icon.showMessage(
                "检测到视频链接",
                "点击处理",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )
            self.showNormal()
            self.activateWindow()

        action = config_manager.get("clipboard_action_mode", "smart")

        if action == "vr":
            self.show_vr_selection_dialog(url)
        elif action == "subtitle":
            self.show_subtitle_selection_dialog(url)
        elif action == "cover":
            self.show_cover_selection_dialog(url)
        elif action == "standard":
            self.show_selection_dialog(url, smart_detect=False)
        else:  # smart
            self.show_selection_dialog(url, smart_detect=True)

    def _show_config_window(
        self,
        url: str,
        mode: str = "default",
        vr_mode: bool = False,
        smart_detect: bool = False,
        playlist_flat: bool = False,
    ):
        """通用方法：显示非阻塞的任务配置窗口"""
        try:
            # 创建新窗口实例
            window = DownloadConfigWindow(
                url,
                self,
                vr_mode=vr_mode,
                mode=mode,
                smart_detect=smart_detect,
                playlist_flat=playlist_flat,
            )

            # 连接信号
            window.downloadRequested.connect(self.add_tasks)
            window.windowClosed.connect(self._cleanup_sub_window)
            window.request_vr_switch.connect(self.handle_vr_switch_request)
            window.request_normal_switch.connect(self.handle_normal_switch_request)

            # 添加到活跃列表防止GC
            self._active_sub_windows.append(window)

            # 显示窗口
            window.show()

            # 根据配置决定是否置顶
            if config_manager.get("clipboard_window_to_front", True):
                window.activateWindow()
                window.raise_()

        except Exception as e:
            logger.error(f"Failed to open config window: {e}")
            InfoBar.error(
                title="打开窗口失败",
                content=str(e),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=3000,
                parent=self,
            )

    def _cleanup_sub_window(self, window):
        """清理已关闭的子窗口引用"""
        if window in self._active_sub_windows:
            self._active_sub_windows.remove(window)
            logger.info(f"Closed sub-window. Active windows: {len(self._active_sub_windows)}")

    def show_selection_dialog(
        self, url: str, smart_detect: bool = False, playlist_flat: bool = False
    ):
        self._show_config_window(
            url, mode="default", smart_detect=smart_detect, playlist_flat=playlist_flat
        )

    def show_vr_selection_dialog(self, url: str, smart_detect: bool = True):
        self._show_config_window(url, mode="vr", vr_mode=True, smart_detect=smart_detect)

    def handle_vr_switch_request(self, url: str):
        """响应智能检测的 VR 切换请求"""
        logger.info(f"Switching to VR mode for URL: {url}")
        self.show_vr_selection_dialog(url, smart_detect=True)

    def handle_normal_switch_request(self, url: str):
        """响应智能检测的普通模式切换请求"""
        logger.info(f"Switching to Normal mode for URL: {url}")
        self.show_selection_dialog(url, smart_detect=True)

    def show_subtitle_selection_dialog(self, url: str):
        self._show_config_window(url, mode="subtitle")

    def show_cover_selection_dialog(self, url: str):
        self._show_config_window(url, mode="cover")

    def add_tasks(self, tasks):
        """添加下载任务到统一任务列表"""
        logger.info(f"[DEBUG] Delegating {len(tasks)} tasks to Controller")
        if self.controller:
            created_workers = self.controller.handle_add_tasks(tasks)
            for worker, t_title, t_thumb in created_workers:
                self.task_page.model.add_task(worker, t_title, str(t_thumb) if t_thumb else "")
        else:
            logger.error("AppController not provided to MainWindow!")

        # 切换到任务列表页
        logger.info("[DEBUG] Switching to task_page")
        self.switchTo(self.task_page)
        logger.info("[DEBUG] Bulk add processing complete")

    def on_open_target_folder(self, row: int):
        task = self.task_page.model.get_task(row)
        if not task:
            return
        worker = task.get("worker")
        if not worker:
            return

        out_file = getattr(worker, "_final_filepath", "")
        if out_file and os.path.exists(out_file):
            import subprocess

            if os.name == "nt":
                subprocess.run(["explorer", "/select,", os.path.normpath(out_file)])
            else:
                os.startfile(os.path.dirname(out_file))
        else:
            # Fallback to output folder
            paths = worker.opts.get("paths", {})
            home_dir = paths.get("home", config_manager.get("download_dir") or os.getcwd())
            if os.path.exists(home_dir):
                os.startfile(home_dir)

    def on_remove_task(self, row: int):
        task = self.task_page.model.get_task(row)
        if not task:
            return
        worker = task.get("worker")
        if not worker:
            return

        try:
            state = worker.effective_state

            is_active = state in ("running", "queued", "paused", "downloading")

            # ── 读取设置页的删除策略 ──
            raw_policy = config_manager.get("deletion_policy")
            policy = DeletionPolicy.from_config_str(raw_policy)

            # ── 快速通道：策略为 "仅移除记录" 且非活跃任务 ──
            if policy == DeletionPolicy.KEEP_FILES and not is_active:
                if self.controller:
                    self.controller.handle_remove_task(worker, force_delete_files=False)
                self.task_page.model.remove_task(row)
                return

            # ── 快速通道：策略为 "彻底删除" 且非活跃任务 ──
            if policy == DeletionPolicy.DELETE_FILES and not is_active:
                if self.controller:
                    self.controller.handle_remove_task(worker, force_delete_files=True)
                self.task_page.model.remove_task(row)
                return

            # ── 中途取消的活跃任务：必须强制清理缓存 ──
            if is_active:
                if policy == DeletionPolicy.KEEP_FILES:
                    # 即使策略是保留文件，中途取消也必须清理 .part/.ytdl 缓存残骸
                    if self.controller:
                        self.controller.handle_remove_task(worker, force_delete_files=True)
                    self.task_page.model.remove_task(row)
                    return

                if policy == DeletionPolicy.DELETE_FILES:
                    if self.controller:
                        self.controller.handle_remove_task(worker, force_delete_files=True)
                    self.task_page.model.remove_task(row)
                    return

                # AlwaysAsk: 提示用户中途取消的双项选择
                title = "取消下载任务"
                content = (
                    "此任务正在下载中。请选择取消方式：\n（未完成的临时缓存文件将会被自动清理）"
                )
                box = MessageBox(title, content, self)
                box.yesButton.setText("🗑️ 移除并删除文件")
                box.cancelButton.setText("取消")

                from qfluentwidgets import PushButton

                keep_btn = PushButton("📋 仅移除记录", box)
                keep_btn.setFixedHeight(box.yesButton.height())
                self._delete_dialog_keep_clicked = False

                def _on_keep():
                    self._delete_dialog_keep_clicked = True
                    box.accept()

                keep_btn.clicked.connect(_on_keep)
                try:
                    box.buttonLayout.insertWidget(1, keep_btn)
                except Exception:
                    box.buttonGroup.layout().insertWidget(1, keep_btn)
                if not box.exec():
                    return

                force_delete = not self._delete_dialog_keep_clicked
                if self.controller:
                    self.controller.handle_remove_task(worker, force_delete_files=force_delete)
                self.task_page.model.remove_task(row)
                return

            # ── 已完成/已出错任务：迅雷/IDM 风格双按钮弹窗 ──
            title = task.get("title") or "删除任务"
            final_path = getattr(worker, "output_path", getattr(worker, "_final_filepath", ""))
            has_local_file = bool(final_path and os.path.exists(str(final_path)))

            if has_local_file:
                content = "请选择删除方式："
                box = MessageBox(title, content, self)
                box.yesButton.setText("🗑️ 删除记录并删除文件")
                box.cancelButton.setText("取消")

                # 插入一个 "仅删除记录" 按钮在 yes 和 cancel 之间
                from qfluentwidgets import PushButton

                keep_btn = PushButton("📋 仅删除记录", box)
                keep_btn.setFixedHeight(box.yesButton.height())
                self._delete_dialog_keep_clicked = False

                def _on_keep():
                    self._delete_dialog_keep_clicked = True
                    box.accept()

                keep_btn.clicked.connect(_on_keep)
                try:
                    box.buttonLayout.insertWidget(1, keep_btn)
                except Exception:
                    box.buttonGroup.layout().insertWidget(1, keep_btn)

                if not box.exec():
                    return

                force_delete = not self._delete_dialog_keep_clicked
            else:
                # 没有本地文件，直接确认删除记录
                content = "确定要从列表中移除此任务记录吗？"
                box = MessageBox(title, content, self)
                box.yesButton.setText("删除记录")
                box.cancelButton.setText("取消")
                if not box.exec():
                    return
                force_delete = False

            if self.controller:
                self.controller.handle_remove_task(worker, force_delete_files=force_delete)
            self.task_page.model.remove_task(row)

        except Exception as e:
            logger.exception(f"Critical error in on_remove_task: {e}")
            try:
                self.task_page.model.remove_task(row)
            except Exception:
                pass

    # --- Helper Methods Copied from Old MainWindow ---
    def _collect_existing_cache_paths(self, cards) -> list[str]:
        paths = []
        for card in cards:
            if not getattr(card, "worker", None):
                continue
            try:
                # 1. Collect from worker.dest_paths (parsed from stdout)
                dest_paths = getattr(card.worker, "dest_paths", set())

                # 2. Also check output_path if available
                output_path = getattr(card.worker, "output_path", None)
                if output_path:
                    dest_paths.add(output_path)

                # 3. Scan directory for .part/.ytdl files if we have a download_dir
                # This helps if stdout was garbled or incomplete.
                download_dir = getattr(card.worker, "download_dir", None)
                if download_dir and os.path.isdir(download_dir):
                    # Try to match files that look like they belong to this task.
                    # If we have a video ID in the URL or title, we can use it.
                    # But worker.url might be a full URL.
                    # Let's try to find files that contain the video ID if possible,
                    # or just scan for .part files that were recently modified?
                    # Scanning all .part files is risky if there are multiple downloads.
                    # Better strategy: If we have output_path, use its basename (without ext) to find parts.

                    # If output_path is known, we can look for output_path + ".part"
                    # Common patterns: filename.mp4.part, filename.f137.mp4.part
                    # We can scan the dir for files starting with the stem of the output filename?
                    # Fallback: If dest_paths is empty, we might have missed it.
                    # But without a reliable ID/Filename, scanning is dangerous.
                    pass

                for p in dest_paths:
                    if not p:
                        continue

                    # If p is a cache file itself
                    if p.endswith(".part") or p.endswith(".ytdl"):
                        if os.path.isfile(p):
                            paths.append(p)
                    else:
                        # If p is the target file, check for .part/.ytdl variants
                        part = p + ".part"
                        if os.path.isfile(part):
                            paths.append(part)
                        ytdl = p + ".ytdl"
                        if os.path.isfile(ytdl):
                            paths.append(ytdl)
            except Exception:
                continue
        return paths

    def _collect_existing_output_paths(self, cards) -> list[str]:
        paths = []
        for card in cards:
            if not getattr(card, "worker", None):
                continue
            try:
                p = getattr(card.worker, "output_path", None)
                if p and os.path.isfile(p):
                    paths.append(p)
            except Exception:
                continue
        return paths

    def _prompt_delete_cache_files(self, paths: list[str], title: str) -> bool:
        box = MessageBox(title, f"即将删除 {len(paths)} 个缓存文件，是否继续？", self)
        return bool(box.exec())

    def _prompt_delete_source_files(self, paths: list[str], title: str) -> bool:
        box = MessageBox(title, f"即将删除 {len(paths)} 个源文件，是否继续？", self)
        return bool(box.exec())

    def on_pause_resume_task(self, row: int):
        # 暂停/继续任务逻辑委托给 Controller
        task_data = self.task_page.model.get_task(row)
        if not task_data:
            return

        worker = task_data.get("worker")
        if not worker:
            return

        if self.controller:
            new_worker = self.controller.handle_pause_resume_task(worker)
            if new_worker:
                task_data["worker"] = new_worker
                self.task_page.model._bind_worker_signals(new_worker, task_data)
                idx = self.task_page.model.index(row, 0)
                self.task_page.model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])

    def on_batch_start(self, rows: list[int]):
        for row in rows:
            self.on_pause_resume_task(row)
        self.task_page.set_selection_mode(False)

    def on_batch_pause(self, rows: list[int]):
        for row in rows:
            task = self.task_page.model.get_task(row)
            if task and task.get("worker"):
                worker = task["worker"]
                if worker.isRunning():
                    if hasattr(worker, "pause"):
                        worker.pause()
                    else:
                        if hasattr(worker, "stop"):
                            worker.stop()
                        elif hasattr(worker, "cancel"):
                            worker.cancel()

    def on_batch_delete(self, rows: list[int]):
        if not rows:
            return

        raw_policy = config_manager.get("deletion_policy")
        policy = DeletionPolicy.from_config_str(raw_policy)

        # ── 分类：活跃任务 vs 已完成任务 ──
        active_workers: list = []  # 需要取消的活跃任务
        finished_workers: list = []  # 已完成/出错任务
        rows_to_remove: set[int] = set()

        for row in rows:
            task = self.task_page.model.get_task(row)
            if not task:
                continue
            worker = task.get("worker")
            if not worker:
                continue

            rows_to_remove.add(row)

            state = worker.effective_state

            if state in ("running", "queued", "paused", "downloading"):
                active_workers.append(worker)
            else:
                finished_workers.append(worker)

        if not rows_to_remove:
            return

        n_active = len(active_workers)
        n_finished = len(finished_workers)
        n_total = len(rows_to_remove)

        # 检查已完成任务中有多少有本地文件
        n_with_files = 0
        for w in finished_workers:
            fp = getattr(w, "output_path", getattr(w, "_final_filepath", ""))
            if fp and os.path.exists(str(fp)):
                n_with_files += 1

        # ── 快速通道：仅移除记录 + 无活跃任务 ──
        if policy == DeletionPolicy.KEEP_FILES and n_active == 0:
            for w in finished_workers:
                if self.controller:
                    self.controller.handle_remove_task(w, force_delete_files=False)
            for row in sorted(list(rows_to_remove), reverse=True):
                try:
                    self.task_page.model.remove_task(row)
                except Exception:
                    pass
            self.task_page.set_selection_mode(False)
            return

        # ── 快速通道：彻底删除 + 无活跃任务 ──
        if policy == DeletionPolicy.DELETE_FILES and n_active == 0:
            for w in finished_workers:
                if self.controller:
                    self.controller.handle_remove_task(w, force_delete_files=True)
            for row in sorted(list(rows_to_remove), reverse=True):
                try:
                    self.task_page.model.remove_task(row)
                except Exception:
                    pass
            self.task_page.set_selection_mode(False)
            return

        # ── 构造提示文案 ──
        desc_parts = []
        if n_active > 0:
            desc_parts.append(f"{n_active} 个下载中的任务（将自动取消并清理缓存）")
        if n_finished > 0:
            if n_with_files > 0:
                desc_parts.append(f"{n_finished} 个已完成任务（其中 {n_with_files} 个有本地文件）")
            else:
                desc_parts.append(f"{n_finished} 个已完成任务")

        title = f"批量删除 {n_total} 个任务"
        content = "选中的任务包含：\n" + "\n".join(f"• {p}" for p in desc_parts)

        if n_with_files > 0 or n_active > 0:
            # 迅雷/IDM 风格双按钮
            content += "\n\n请选择删除方式："
            box = MessageBox(title, content, self)
            if n_with_files > 0:
                box.yesButton.setText(f"🗑️ 删除记录并删除文件 ({n_with_files} 个)")
            else:
                box.yesButton.setText("🗑️ 移除并删除文件")
            box.cancelButton.setText("取消")

            from qfluentwidgets import PushButton

            keep_btn = PushButton("📋 仅删除记录", box)
            keep_btn.setFixedHeight(box.yesButton.height())
            self._batch_delete_keep_clicked = False

            def _on_keep():
                self._batch_delete_keep_clicked = True
                box.accept()

            keep_btn.clicked.connect(_on_keep)
            try:
                box.buttonLayout.insertWidget(1, keep_btn)
            except Exception:
                box.buttonGroup.layout().insertWidget(1, keep_btn)

            if not box.exec():
                return

            force_delete_finished = not self._batch_delete_keep_clicked
        else:
            # 没有本地文件的情况，只需确认
            box = MessageBox(title, content, self)
            box.yesButton.setText("确认删除")
            box.cancelButton.setText("取消")
            if not box.exec():
                return
            force_delete_finished = False

        # ── 执行 ──
        # 活跃任务/已完成任务：共同遵循用户的双项选择
        for w in active_workers:
            try:
                if self.controller:
                    self.controller.handle_remove_task(w, force_delete_files=force_delete_finished)
            except Exception:
                pass

        # 已完成任务：根据用户选择
        for w in finished_workers:
            try:
                if self.controller:
                    self.controller.handle_remove_task(w, force_delete_files=force_delete_finished)
            except Exception:
                pass

        for row in sorted(list(rows_to_remove), reverse=True):
            try:
                self.task_page.model.remove_task(row)
            except Exception:
                pass

        self.task_page.set_selection_mode(False)

    def on_start_all(self):
        for i in range(self.task_page.model.rowCount()):
            task = self.task_page.model.get_task(i)
            if not task:
                continue

            worker = task.get("worker")
            if not worker:
                continue

            s = worker.effective_state
            if s in {"paused", "error", "queued"}:
                self.on_resume_task(i)

    def on_pause_all(self):
        download_manager.stop_all()

    def on_clear_completed(self):
        # Gather rows that are completed
        completed_rows = []
        for i in range(self.task_page.model.rowCount()):
            task = self.task_page.model.get_task(i)
            if not task:
                continue
            worker = task.get("worker")
            if worker and worker.effective_state == "completed":
                completed_rows.append(i)

        if not completed_rows:
            return
        if MessageBox(
            "清空记录", "确定要清空所有已完成任务记录吗？\n(不会删除本地文件)", self
        ).exec():
            for row in sorted(completed_rows, reverse=True):
                self.task_page.model.remove_task(row)

    def on_open_download_dir(self):
        # 打开默认下载目录
        path = config_manager.get("download_dir") or os.getcwd()
        if os.path.exists(path):
            os.startfile(path)

    def _on_history_record_added(self, record) -> None:
        """历史记录新增时实时更新历史页面"""
        try:
            self.history_page.add_record(record)
        except Exception:
            pass

    def init_title_bar(self):
        # 在标题栏添加帮助按钮
        # Parent MUST be titleBar to ensure correct z-order and event handling
        self.help_btn = TransparentToolButton(FluentIcon.HELP, self.titleBar)
        self.help_btn.setToolTip("帮助中心")
        self.help_btn.installEventFilter(
            ToolTipFilter(self.help_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.help_btn.clicked.connect(self.show_help_window)
        self.help_btn.setFixedSize(46, 32)

        # 查找插入位置：尝试插在系统按钮组的最左边
        layout = self.titleBar.layout()
        # Insert the help button to the left of the system buttons (min/max/close)
        # Assuming system buttons are the last three widgets in the title bar layout
        insert_widget = getattr(layout, "insertWidget", None) if layout else None
        count = getattr(layout, "count", None) if layout else None
        if callable(insert_widget) and callable(count):
            count_value = count()
            if isinstance(count_value, int):
                insert_widget(count_value - 3, self.help_btn, 0, Qt.AlignmentFlag.AlignRight)

        # 给 help_btn 设置右边距，让它离系统按钮远一点
        self.help_btn.setContentsMargins(0, 0, 10, 0)

    def show_help_window(self):
        if not getattr(self, "_help_window", None):
            self._help_window = HelpWindow()
        self._help_window.show()
        self._help_window.activateWindow()

    def check_first_run(self):
        """Check if welcome guide should be shown based on version."""
        from fluentytdl import __version__

        # Get the current major version (e.g., "1" from "1.0.16")
        current_major = __version__.split(".")[0] if __version__ else "0"

        # Get the version when user last saw the guide
        shown_for_version = config_manager.get("welcome_guide_shown_for_version", "")
        shown_major = shown_for_version.split(".")[0] if shown_for_version else ""

        # Show welcome guide if:
        # 1. Never shown before (empty version)
        # 2. Major version has changed (e.g., 0.x.x -> 1.x.x)
        should_show = not shown_for_version or (shown_major != current_major)

        if should_show:
            logger.info(
                f"Showing Welcome Wizard (current: {__version__}, last shown: {shown_for_version})"
            )
            w = WelcomeWizardDialog(self)
            w.exec()
            # Record the full version when guide was shown
            config_manager.set("welcome_guide_shown_for_version", __version__)
            config_manager.set("has_shown_welcome_guide", True)

        # 检查Cookie状态（延迟5秒，让启动时的静默刷新完成）
        QTimer.singleShot(5000, self.check_cookie_status)

    def on_admin_mode_cookie_refresh(self):
        """管理员模式启动后自动刷新Cookie"""
        from ..auth.auth_service import AuthSourceType, auth_service
        from ..auth.cookie_sentinel import cookie_sentinel
        from ..utils.logger import logger

        # 只在配置了浏览器来源时刷新
        if auth_service.current_source == AuthSourceType.NONE:
            logger.info("[AdminMode] 未配置Cookie来源，跳过自动刷新")
            return

        if auth_service.current_source == AuthSourceType.FILE:
            logger.info("[AdminMode] 手动文件模式，跳过自动刷新")
            return

        if auth_service.current_source == AuthSourceType.DLE:
            logger.info("[AdminMode] 登录模式(DLE)，跳过自动刷新（需要用户交互）")
            return

        browser_name = auth_service.current_source_display
        logger.info(f"[AdminMode] 以管理员身份自动刷新Cookie: {browser_name}")

        # 显示提示
        from qfluentwidgets import InfoBar

        InfoBar.info(
            "管理员模式",
            f"正在以管理员权限提取 {browser_name} Cookie...",
            duration=3000,
            parent=self,
        )

        # 执行刷新
        try:
            success, message = cookie_sentinel.force_refresh_with_uac()

            if success:
                InfoBar.success(
                    "Cookie提取成功",
                    f"已从 {browser_name} 提取 Cookie（管理员权限）",
                    duration=5000,
                    parent=self,
                )
                # 自动跳转到设置页显示结果
                QTimer.singleShot(1000, lambda: self.switchTo(self.settings_interface))
            else:
                InfoBar.warning("Cookie提取失败", message, duration=8000, parent=self)
        except Exception as e:
            logger.error(f"[AdminMode] Cookie刷新异常: {e}", exc_info=True)
            InfoBar.error("Cookie提取异常", str(e), duration=5000, parent=self)

    def check_cookie_status(self):
        """
        统一 Cookie 有效性检查（适用于所有验证模式）

        三层检查:
        1. cookie_sentinel.exists → Cookie 文件是否存在
        2. cookie_sentinel.is_stale → Cookie 是否已过期（基于 SID/HSID expires）
        3. auth_service.last_status.valid → 关键字段完整性（SID/HSID/SSID 等）

        当检测到问题时，弹出 CookieRepairDialog 引导用户修复。
        """
        try:
            from ..auth.auth_service import AuthSourceType, auth_service
            from ..auth.cookie_sentinel import cookie_sentinel
            from ..utils.admin_utils import is_admin

            current_source = auth_service.current_source

            # 未启用验证，无需检查
            if current_source == AuthSourceType.NONE:
                return

            source_name = auth_service.current_source_display

            # ── 统一有效性检查（适用于 DLE / 浏览器 / 手动导入） ──
            # 只检查两层：
            #   1. cookie_sentinel.exists → Cookie 文件是否存在
            #   2. auth_service.last_status.valid → 关键字段完整性 + 过期检查
            #      (_validate_cookies 会先过滤掉已过期的 Cookie，再检查 SID/HSID 等是否存在)
            is_invalid = False
            reason = ""

            if not cookie_sentinel.exists:
                is_invalid = True
                if current_source == AuthSourceType.DLE:
                    reason = "尚未登录获取 Cookie"
                elif current_source == AuthSourceType.FILE:
                    reason = "尚未导入 Cookie 文件"
                else:
                    reason = f"尚未从 {source_name} 提取 Cookie"
            elif not auth_service.last_status.valid:
                is_invalid = True
                reason = auth_service.last_status.message or "Cookie 无效"

            if is_invalid:
                logger.warning(f"[MainWindow] Cookie 无效 ({source_name}): {reason}")

                # Chromium 浏览器非管理员 → 特殊处理：提示以管理员重启
                from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

                if current_source in ADMIN_REQUIRED_BROWSERS and not is_admin():
                    from qfluentwidgets import MessageBox

                    box = MessageBox(
                        f"{source_name} 需要管理员权限",
                        f"检测到您使用 {source_name} 作为 Cookie 来源。\n\n"
                        f"Chromium 内核浏览器使用了加密保护，\n"
                        f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                        "是否以管理员身份重启程序？\n\n"
                        "提示：您也可以切换到 Firefox/LibreWolf 浏览器，\n"
                        "或使用「登录获取」方式，无需管理员权限。",
                        self,
                    )
                    box.yesButton.setText("以管理员身份重启")
                    box.cancelButton.setText("稍后再说")

                    if box.exec():
                        from ..utils.admin_utils import restart_as_admin

                        restart_as_admin(f"提取 {source_name} Cookie")
                else:
                    # 所有模式通用：使用 CookieRepairDialog 引导修复
                    self._show_cookie_repair(current_source, source_name, reason)
            else:
                logger.info(
                    f"[MainWindow] Cookie 有效 ({source_name}，"
                    f"{auth_service.last_status.cookie_count} 个 Cookie)"
                )

            # 所有模式统一：延迟服务端探测
            QTimer.singleShot(10000, self._startup_cookie_probe)

        except Exception as e:
            logger.error(f"[MainWindow] Cookie状态检查失败: {e}")

    def _show_cookie_repair(self, source_type, source_name: str, reason: str) -> None:
        """
        弹出 Cookie 修复引导（复用 CookieRepairDialog）

        根据当前验证模式自动调整引导文案和按钮行为。
        """
        from ..auth.auth_service import AuthSourceType
        from ..auth.cookie_sentinel import cookie_sentinel
        from .components.cookie_repair_dialog import CookieRepairDialog

        # 映射 auth_source 字符串
        source_map = {
            AuthSourceType.DLE: "dle",
            AuthSourceType.FILE: "file",
        }
        auth_source_str = source_map.get(source_type, "browser")

        dialog = CookieRepairDialog(reason, parent=self, auth_source=auth_source_str)

        # 根据模式定制按钮文案
        if source_type == AuthSourceType.DLE:
            dialog.repair_btn.setText("重新登录")
            dialog.setWindowTitle("需要重新登录 YouTube")
        elif source_type == AuthSourceType.FILE:
            dialog.repair_btn.setText("重新导入")
            dialog.setWindowTitle("Cookie 文件需要更新")
        else:
            dialog.repair_btn.setText("重新提取")

        # 自动修复信号
        def on_auto_repair():
            if source_type == AuthSourceType.DLE:
                # DLE → 跳转到设置页面的登录区域
                dialog.accept()
                self.switchTo(self.settings_interface)
            elif source_type == AuthSourceType.FILE:
                # 手动导入 → 跳转到设置页面
                dialog.accept()
                self.switchTo(self.settings_interface)
            else:
                # 浏览器提取 → 直接自动修复
                success, message = cookie_sentinel.force_refresh_with_uac()
                dialog.show_repair_result(success, message)

        dialog.repair_requested.connect(on_auto_repair)

        # 手动导入信号 → 跳转设置页
        def on_manual_import():
            self.switchTo(self.settings_interface)

        dialog.manual_import_requested.connect(on_manual_import)

        dialog.exec()

    def _startup_cookie_probe(self) -> None:
        """启动后延迟执行一次带节流的 Cookie+IP 探测"""
        try:
            from ..auth.cookie_probe_throttle import cookie_probe_throttle

            if not cookie_probe_throttle.should_probe():
                return

            import threading

            def _probe():
                result = cookie_probe_throttle.probe_if_allowed()
                if not result:
                    return

                if result.get("cookie_ok"):
                    return  # 一切正常

                detail = result.get("detail", "")
                ip_ok = result.get("ip_ok", True)
                logger.warning(f"[StartupProbe] 服务端探测异常: {detail}")

                # 准备 UI 提示内容
                if not ip_ok:
                    title = "⚠️ IP 可能被 YouTube 限制"
                    content = "服务端检测到当前网络节点触发了风控，建议更换代理节点。"
                else:
                    title = "⚠️ Cookie 可能已失效"
                    content = "服务端检测到 Cookie 无法通过验证，建议前往设置页刷新。"

                # 回到主线程显示 UI（使用 QTimer.singleShot 线程安全）
                QTimer.singleShot(0, lambda t=title, c=content: self._show_probe_warning(t, c))

            threading.Thread(target=_probe, daemon=True, name="StartupCookieProbe").start()
        except Exception:
            pass

    def _show_probe_warning(self, title: str, content: str) -> None:
        """在主线程显示服务端探测警告"""
        InfoBar.warning(
            title,
            content,
            duration=8000,
            parent=self,
            position=InfoBarPosition.TOP,
        )
