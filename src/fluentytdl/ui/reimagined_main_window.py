from __future__ import annotations

import os
from enum import Enum
from typing import Any

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtGui import QAction
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
    SystemThemeListener,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from fluentytdl.ui.components.common.clipboard_monitor import ClipboardMonitor
from fluentytdl.ui.components.dialogs.download_config_window import DownloadConfigWindow

from ..core.config_manager import config_manager
from ..download.download_manager import download_manager
from ..utils.icons import load_app_icon
from ..utils.logger import logger
from .channel_parse_page import ChannelParsePage
from .cover_download_page import CoverDownloadPage
from .help_window import HelpWindow
from .pages.history_page import HistoryPage
from .parse_page import ParsePage
from .quick_add_panel import QuickAddPanel
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

        # 设置窗口标题（含版本号，管理员模式添加标识）
        from fluentytdl import __version__

        title = f"FluentYTDL Pro {__version__}"
        if self._is_admin:
            title += self.tr(" (管理员)")
        self.setWindowTitle(title)

        self.resize(1150, 780)
        # 锁定最小宽度，防止两个 bug 导致的自动变宽：
        # 1. 切换到英文时文本变宽触发的布局最小宽度增长
        # 2. NavigationPanel 展开/收起动画触发的 setFixedWidth 棘轮效应
        # 用户仍可手动拖拽边缘把窗口拉宽，只阻止自动增长
        self.setMinimumWidth(1150)

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
        self.quick_parse_page = QuickAddPanel(self)
        self.vr_parse_page = VRParsePage(self)
        self.channel_parse_page = ChannelParsePage(self)
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

        # === 系统主题跟随 ===
        # Theme.AUTO 只在启动时解析一次系统主题，之后系统深浅色切换不会
        # 触发 themeChanged。监听器补上这一环：它在系统主题变化时重发
        # themeChanged，所有已连接 qconfig.themeChanged 的组件随之刷新。
        self.themeListener = SystemThemeListener(self)
        self.themeListener.start()

        # 启动动画
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.finish()

        # 信号连接
        self.parse_page.parse_requested.connect(
            lambda url: self.show_selection_dialog(url, smart_detect=False, playlist_flat=True)
        )
        self.quick_parse_page.download_requested.connect(self.handle_quick_download_requested)
        self.vr_parse_page.parse_requested.connect(self.show_vr_selection_dialog)
        self.channel_parse_page.parse_requested.connect(self._show_channel_dialog)
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

        # === 软件更新通知 ===
        from ..core.component_update_manager import component_update_manager

        component_update_manager.app_update_available.connect(self._on_app_update_available)
        component_update_manager.apply_requested.connect(self._on_update_apply_requested)

        # === 首次启动检测 ===
        QTimer.singleShot(1000, self.check_first_run)

        # === 管理员模式：自动刷新 Cookie ===
        if self._is_admin:
            QTimer.singleShot(2000, self.on_admin_mode_cookie_refresh)

        # === 恢复重启前的未完成任务到 UI 层 ===
        self._restore_tasks_to_ui()

        # === 监听 DownloadManager 发出的全局 Worker 错误 ===
        from fluentytdl.download.download_manager import download_manager

        download_manager.worker_error.connect(self.on_worker_error)

    def _restore_tasks_to_ui(self) -> None:
        """将 DownloadManager 中恢复的 Worker 同步到 DownloadListModel"""
        restored = 0
        for worker in download_manager.active_workers:
            title = getattr(worker, "v_title", "") or ""
            thumb = getattr(worker, "v_thumbnail", "") or ""
            self.task_page.model.add_task(worker, title, thumb)
            restored += 1
        if restored > 0:
            logger.info(f"[MainWindow] 已恢复 {restored} 个未完成任务到 UI")
            # UI 初始化完成后触发一次 pump，启动排队中的任务
            QTimer.singleShot(500, download_manager.pump)

    def _on_app_update_available(self, info: dict) -> None:
        """主窗口顶部弹出 InfoBar，提示软件更新可用。"""
        version = info.get("version", "?")
        is_pre = info.get("is_prerelease", False)
        prefix = self.tr("预发布版本") if is_pre else self.tr("新版本")
        InfoBar.info(
            self.tr("软件更新"),
            f"{prefix} {version} 已可用，前往设置页面更新",
            duration=10000,
            parent=self,
        )

    def _on_update_apply_requested(self) -> None:
        """后端已批准更新，执行优雅退出。

        `updater.exe` 不在这里启动 —— 它由 `main.py` 在 `app.exec()` 返回之后拉起，
        这样 `quit_app()` 里那些可能耗时数秒的收尾（worker shutdown、db_writer 落盘）
        必然在 updater 开始替换文件之前完成。

        用 singleShot 把退出挪出信号发射栈：此刻我们还在
        `request_app_core_update()` 的 emit 里，不能在这里同步跑完整个 shutdown。
        """
        logger.info("[MainWindow] 收到更新申请，开始优雅退出")
        QTimer.singleShot(0, self.quit_app)

    def init_navigation(self):
        # 减小侧边栏展开时的宽度，避免留白过多
        self.navigationInterface.setExpandWidth(190)
        # 1. 新建任务
        self.addSubInterface(
            self.parse_page,
            FluentIcon.ADD,
            self.tr("新建任务"),
            position=NavigationItemPosition.TOP,
        )

        # 1.1 批量快速下载
        self.addSubInterface(
            self.quick_parse_page,
            FluentIcon.ADD_TO,
            self.tr("批量快速下载"),
            position=NavigationItemPosition.TOP,
        )

        # 2. VR 下载
        self.addSubInterface(
            self.vr_parse_page,
            FluentIcon.GAME,
            self.tr("VR 下载"),
            position=NavigationItemPosition.TOP,
        )

        # 2.1 频道下载
        self.addSubInterface(
            self.channel_parse_page,
            FluentIcon.VIDEO,
            self.tr("频道下载"),
            position=NavigationItemPosition.TOP,
        )

        # 2.2 字幕下载
        self.addSubInterface(
            self.subtitle_page,
            FluentIcon.FONT,
            self.tr("字幕下载"),
            position=NavigationItemPosition.TOP,
        )

        # 2.2 封面下载
        self.addSubInterface(
            self.cover_page,
            FluentIcon.PHOTO,
            self.tr("封面下载"),
            position=NavigationItemPosition.TOP,
        )

        # 3. 任务列表（统一页面，内部使用 Pivot 过滤）
        self.addSubInterface(
            self.task_page,
            FluentIcon.DOWNLOAD,
            self.tr("任务列表"),
            position=NavigationItemPosition.TOP,
        )

        # 4. 下载历史
        self.addSubInterface(
            self.history_page,
            FluentIcon.HISTORY,
            self.tr("下载历史"),
            position=NavigationItemPosition.TOP,
        )

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            self.tr("设置"),
            position=NavigationItemPosition.BOTTOM,
        )

    def init_page_actions(self):
        """为统一任务页面设置操作按钮"""
        page = self.task_page

        # 全部开始/暂停按钮 (Secondary Actions)
        start_all = TransparentToolButton(FluentIcon.PLAY, self)
        start_all.setToolTip(self.tr("全部开始"))
        start_all.installEventFilter(
            ToolTipFilter(start_all, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        start_all.clicked.connect(self.on_start_all)

        pause_all = TransparentToolButton(FluentIcon.PAUSE, self)
        pause_all.setToolTip(self.tr("全部暂停"))
        pause_all.installEventFilter(
            ToolTipFilter(pause_all, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        pause_all.clicked.connect(self.on_pause_all)

        # 打开目录
        open_dir = TransparentToolButton(FluentIcon.FOLDER, self)
        open_dir.setToolTip(self.tr("打开下载目录"))
        open_dir.installEventFilter(
            ToolTipFilter(open_dir, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        open_dir.clicked.connect(self.on_open_download_dir)

        # 清空已完成
        clear_completed = TransparentToolButton(FluentIcon.DELETE, self)
        clear_completed.setToolTip(self.tr("清空已完成/已失败记录"))
        clear_completed.installEventFilter(
            ToolTipFilter(clear_completed, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        clear_completed.clicked.connect(self.on_clear_completed)

        # 清空全部
        clear_all = TransparentToolButton(FluentIcon.BROOM, self)
        clear_all.setToolTip(self.tr("清空全部任务"))
        clear_all.installEventFilter(
            ToolTipFilter(clear_all, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        clear_all.clicked.connect(self.on_clear_all)

        # 批量操作按钮
        from qfluentwidgets import TransparentPushButton

        batch_btn = TransparentPushButton(FluentIcon.CHECKBOX, self.tr("批量操作"), page)
        batch_btn.setToolTip(self.tr("进入或退出批量模式"))
        batch_btn.installEventFilter(
            ToolTipFilter(batch_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )

        def toggle_batch():
            is_batch = getattr(page, "_is_batch_mode", False)
            page.set_selection_mode(not is_batch)

        def _on_selection_mode_changed(is_batch: bool):
            if is_batch:
                batch_btn.setIcon(FluentIcon.CANCEL)
                batch_btn.setText(self.tr("退出批量"))
            else:
                batch_btn.setIcon(FluentIcon.CHECKBOX)
                batch_btn.setText(self.tr("批量操作"))

        batch_btn.clicked.connect(toggle_batch)
        page.selection_mode_changed.connect(_on_selection_mode_changed)

        # 添加到布局 (分组)
        page.action_layout.setSpacing(0)

        # 2. 全局控制
        page.action_layout.addWidget(start_all)
        page.action_layout.addWidget(pause_all)
        page.action_layout.addWidget(open_dir)
        page.action_layout.addWidget(clear_completed)
        page.action_layout.addWidget(clear_all)

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
        # 多尺寸图标：Windows 通知区会按当前 DPI 索取 16/20/24 px，
        # 单张 256px 大图缩下去会糊成一团。
        chosen_icon = load_app_icon()
        if chosen_icon.isNull():
            win_icon = self.windowIcon()
            if not win_icon.isNull():
                chosen_icon = win_icon

        if chosen_icon.isNull():
            # 拿不到有效图标时不创建托盘：显示一个占位色块比没有托盘更糟，
            # 用户只会看到右下角一个「坏掉」的图标。
            logger.warning("托盘图标资源缺失，已跳过系统托盘初始化")
            self.tray_icon = None
            return

        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(chosen_icon)
        tray_menu = QMenu()
        show_action = QAction(self.tr("显示主界面"), self)
        show_action.triggered.connect(self.showNormal)
        quit_action = QAction(self.tr("退出"), self)
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

    def closeEvent(self, event):
        """窗口关闭事件：最小化到托盘或优雅退出"""
        if hasattr(self, "tray_icon") and self.tray_icon and self.tray_icon.isVisible():
            self.hide()
            event.ignore()
        else:
            self._stop_theme_listener()
            download_manager.shutdown(grace_ms=2000)
            super().closeEvent(event)

    def _stop_theme_listener(self):
        """停止系统主题监听线程，避免退出时留下悬挂线程。"""
        listener = getattr(self, "themeListener", None)
        if listener is None:
            return
        try:
            listener.terminate()
            listener.deleteLater()
        except Exception:
            pass
        self.themeListener = None

    def quit_app(self):
        self._stop_theme_listener()
        download_manager.shutdown(grace_ms=2000)
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
        is_playlist = "list=" in url
        title_msg = self.tr("检测到 YouTube 播放列表") if is_playlist else self.tr("检测到视频链接")

        if not self.isVisible():
            if self.tray_icon is not None:
                self.tray_icon.showMessage(
                    title_msg,
                    self.tr("点击处理"),
                    QSystemTrayIcon.MessageIcon.Information,
                    2000,
                )
            self.showNormal()
            self.activateWindow()
        else:
            InfoBar.info(
                title=title_msg,
                content=self.tr("正在准备解析..."),
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP_RIGHT,
                duration=2000,
                parent=self,
            )

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
        target_tab: str | None = None,
        preloaded_info: dict | None = None,
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
                target_tab=target_tab,
                preloaded_info=preloaded_info,
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
                title=self.tr("打开窗口失败"),
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
        self,
        url: str,
        smart_detect: bool = False,
        playlist_flat: bool = False,
        target_tab: str | None = None,
        preloaded_info: dict | None = None,
    ):
        self._remember_recent_target_url(url)
        self._show_config_window(
            url,
            mode="default",
            smart_detect=smart_detect,
            playlist_flat=playlist_flat,
            target_tab=target_tab,
            preloaded_info=preloaded_info,
        )

    def show_vr_selection_dialog(
        self,
        url: str,
        smart_detect: bool = True,
        preloaded_info: dict | None = None,
    ):
        self._remember_recent_target_url(url)
        self._show_config_window(
            url,
            mode="vr",
            vr_mode=True,
            smart_detect=smart_detect,
            preloaded_info=preloaded_info,
        )

    def _show_channel_dialog(self, url: str, target_tab: str = "all") -> None:
        """频道解析入口：规范化 URL 后，走播放列表 flat 解析路径，传递 target_tab。"""
        from ..youtube.youtube_service import YoutubeService

        # 始终传递 "all" 来获取纯净的 base_url，因为具体的 tab 会由 ChannelExtractWorker 拼接
        normalized = YoutubeService._normalize_channel_url(url, "all")
        self.show_selection_dialog(
            normalized, smart_detect=False, playlist_flat=True, target_tab=target_tab
        )

    def handle_vr_switch_request(self, url: str, preloaded_info: dict | None = None):
        """响应智能检测的 VR 切换请求。

        preloaded_info 是切换前那一轮已完成解析的结果，仅用于新窗口的首屏预览；
        VR 格式必须由 android_vr 重新解析，不能沿用。
        """
        logger.info(f"Switching to VR mode for URL: {url}")
        self.show_vr_selection_dialog(url, smart_detect=True, preloaded_info=preloaded_info)

    def handle_normal_switch_request(self, url: str, preloaded_info: dict | None = None):
        """响应智能检测的普通模式切换请求"""
        logger.info(f"Switching to Normal mode for URL: {url}")
        self.show_selection_dialog(url, smart_detect=True, preloaded_info=preloaded_info)

    def show_subtitle_selection_dialog(self, url: str):
        self._remember_recent_target_url(url)
        self._show_config_window(url, mode="subtitle")

    def show_cover_selection_dialog(self, url: str):
        self._remember_recent_target_url(url)
        self._show_config_window(url, mode="cover")

    def _remember_recent_target_url(self, url: str) -> None:
        value = str(url or "").strip()
        if not value:
            return
        config_manager.set("recent_target_url", value)

    def add_tasks(self, tasks):
        """添加下载任务到统一任务列表"""
        logger.info(f"[DEBUG] Delegating {len(tasks)} tasks to Controller")
        if self.controller:
            created_workers = self.controller.handle_add_tasks(tasks)
            for worker, t_title, t_thumb in reversed(created_workers):
                self.task_page.model.add_task(worker, t_title, str(t_thumb) if t_thumb else "")
        else:
            logger.error("AppController not provided to MainWindow!")

        # 切换到任务列表页
        logger.info("[DEBUG] Switching to task_page")
        self.switchTo(self.task_page)
        logger.info("[DEBUG] Bulk add processing complete")

    def handle_quick_download_requested(self, urls: list[str], params: Any):
        """处理快速模式下载请求"""
        if not self.controller:
            logger.error("AppController not provided to MainWindow!")
            return

        from qfluentwidgets import StateToolTip

        self._quick_add_tooltip = StateToolTip(
            self.tr("解析中"), self.tr("正在获取资源信息..."), self.window()
        )
        self._quick_add_tooltip.move(self._quick_add_tooltip.getSuitablePos())
        self._quick_add_tooltip.show()

        def on_progress(msg: str):
            if hasattr(self, "_quick_add_tooltip") and self._quick_add_tooltip:
                self._quick_add_tooltip.setContent(msg)

        def on_error(msg: str):
            if hasattr(self, "_quick_add_tooltip") and self._quick_add_tooltip:
                self._quick_add_tooltip.setContent(self.tr("解析失败"))
                self._quick_add_tooltip.setState(True)
                self._quick_add_tooltip = None

            InfoBar.error(
                title=self.tr("快速下载失败"),
                content=msg,
                orient=Qt.Orientation.Horizontal,
                isClosable=True,
                position=InfoBarPosition.TOP,
                duration=5000,
                parent=self,
            )

        def on_finished(created_workers):
            if hasattr(self, "_quick_add_tooltip") and self._quick_add_tooltip:
                self._quick_add_tooltip.setContent(f"成功添加 {len(created_workers)} 个任务")
                self._quick_add_tooltip.setState(True)
                self._quick_add_tooltip = None

            for worker, t_title, t_thumb in created_workers:
                self.task_page.model.add_task(worker, t_title, str(t_thumb) if t_thumb else "")
            self.switchTo(self.task_page)

        self.controller.handle_quick_add_tasks(
            urls, params, {"progress": on_progress, "error": on_error, "finished": on_finished}
        )

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

            # ── 快速通道：策略为 self.tr("仅移除记录") 且非活跃任务 ──
            if policy == DeletionPolicy.KEEP_FILES and not is_active:
                if self.controller:
                    self.controller.handle_remove_task(worker, force_delete_files=False)
                self.task_page.model.remove_task(row)
                return

            # ── 快速通道：策略为 self.tr("彻底删除") 且非活跃任务 ──
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
                title = self.tr("取消下载任务")
                content = self.tr("此任务正在下载中。确定要取消该任务吗？")
                box = MessageBox(title, content, self)
                box.yesButton.setText(self.tr("确定取消"))
                box.cancelButton.setText(self.tr("暂不取消"))

                from qfluentwidgets import CheckBox

                chk = CheckBox(self.tr("同时清理未完成的临时缓存文件"), box)
                chk.setChecked(True)
                box.textLayout.addWidget(chk)

                if not box.exec():
                    return

                force_delete = chk.isChecked()
                if self.controller:
                    self.controller.handle_remove_task(worker, force_delete_files=force_delete)
                self.task_page.model.remove_task(row)
                return

            # ── 已完成/已出错任务：迅雷/IDM 风格双按钮弹窗 ──
            title = task.get("title") or self.tr("删除任务")
            final_path = getattr(worker, "output_path", getattr(worker, "_final_filepath", ""))
            has_local_file = bool(final_path and os.path.exists(str(final_path)))

            if has_local_file:
                content = self.tr("确定要从列表中移除此任务记录吗？")
                box = MessageBox(title, content, self)
                box.yesButton.setText(self.tr("删除"))
                box.cancelButton.setText(self.tr("取消"))

                from qfluentwidgets import CheckBox

                chk = CheckBox(self.tr("同时删除已下载的本地文件"), box)
                chk.setChecked(False)
                box.textLayout.addWidget(chk)

                if not box.exec():
                    return

                force_delete = chk.isChecked()
            else:
                # 没有本地文件，直接确认删除记录
                content = self.tr("确定要从列表中移除此任务记录吗？")
                box = MessageBox(title, content, self)
                box.yesButton.setText(self.tr("删除记录"))
                box.cancelButton.setText(self.tr("取消"))
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
        """批量开始任务"""
        self.task_page.set_selection_mode(False)

        workers_to_start = []
        row_map = {}
        for row in rows:
            task = self.task_page.model.get_task(row)
            if task and task.get("worker"):
                w = task["worker"]
                workers_to_start.append(w)
                row_map[w] = row

        if not self.controller or not workers_to_start:
            return

        # 采用切片（Chunking）方式异步执行，防止批量写入 SQLite 时长期阻塞主线程导致 Segfault
        def process_chunk():
            if not workers_to_start:
                return

            chunk = []
            # 每次处理 2 个，让出控制权让事件循环处理 QThread 和 UI 绘制
            for _ in range(min(2, len(workers_to_start))):
                chunk.append(workers_to_start.pop(0))

            recreated_workers = self.controller.handle_batch_start(chunk)
            for old_w, new_w in recreated_workers:
                r = row_map.get(old_w)
                if r is not None:
                    t = self.task_page.model.get_task(r)
                    if t:
                        t["worker"] = new_w
                        self.task_page.model._bind_worker_signals(new_w, t)
                        idx = self.task_page.model.index(r, 0)
                        self.task_page.model.dataChanged.emit(idx, idx, [Qt.ItemDataRole.UserRole])

            if workers_to_start:
                QTimer.singleShot(50, process_chunk)

        QTimer.singleShot(0, process_chunk)

    def on_batch_pause(self, rows: list[int]):
        workers_to_pause = []
        for row in rows:
            task = self.task_page.model.get_task(row)
            if task and task.get("worker"):
                workers_to_pause.append(task["worker"])

        if self.controller and workers_to_pause:
            self.controller.handle_batch_pause(workers_to_pause)

        self.task_page.set_selection_mode(False)

    def on_batch_delete(self, rows: list[int], delete_files: bool = False):
        if not rows:
            return

        # ── 分类任务 ──
        workers_to_delete = []
        workers_to_delete_set = set()

        for row in rows:
            task = self.task_page.model.get_task(row)
            if not task:
                continue
            worker = task.get("worker")
            if not worker:
                continue

            workers_to_delete.append(worker)
            workers_to_delete_set.add(id(worker))

        if not workers_to_delete:
            return

        # 采用切片（Chunking）方式异步执行，防止批量删除 SQLite/Model 节点时阻塞主线程
        def process_chunk():
            if not workers_to_delete:
                # 任务处理完后，从模型中动态反查要删除的行号（需从底往上删避免索引越界）
                rows_to_remove = []
                for i in range(self.task_page.model.rowCount() - 1, -1, -1):
                    t = self.task_page.model.get_task(i)
                    if t and id(t.get("worker")) in workers_to_delete_set:
                        rows_to_remove.append(i)
                for row in rows_to_remove:
                    try:
                        self.task_page.model.remove_task(row)
                    except Exception:
                        pass
                self.task_page.set_selection_mode(False)
                return

            chunk = []
            for _ in range(min(5, len(workers_to_delete))):
                chunk.append(workers_to_delete.pop(0))

            if self.controller:
                self.controller.handle_batch_remove(chunk, force_delete_files=delete_files)

            if workers_to_delete:
                QTimer.singleShot(50, process_chunk)
            else:
                # 触发模型移除
                QTimer.singleShot(0, process_chunk)

        QTimer.singleShot(0, process_chunk)

    def on_start_all(self):
        rows = []
        for row in range(self.task_page.proxy_model.rowCount()):
            src_idx = self.task_page.proxy_model.mapToSource(
                self.task_page.proxy_model.index(row, 0)
            )
            rows.append(src_idx.row())
        self.on_batch_start(rows)

    def on_pause_all(self):
        rows = []
        for row in range(self.task_page.proxy_model.rowCount()):
            src_idx = self.task_page.proxy_model.mapToSource(
                self.task_page.proxy_model.index(row, 0)
            )
            rows.append(src_idx.row())
        self.on_batch_pause(rows)

    def on_clear_completed(self):
        clearable_rows = []
        for i in range(self.task_page.model.rowCount()):
            task = self.task_page.model.get_task(i)
            if not task:
                continue
            worker = task.get("worker")
            if worker and worker.effective_state in ("completed", "error", "cancelled"):
                clearable_rows.append(i)

        if not clearable_rows:
            return

        n_completed = sum(
            1
            for r in clearable_rows
            if self.task_page.model.get_task(r).get("worker").effective_state == "completed"
        )
        n_error = sum(
            1
            for r in clearable_rows
            if self.task_page.model.get_task(r).get("worker").effective_state
            in ("error", "cancelled")
        )

        parts = []
        if n_completed:
            parts.append(f"{n_completed} 个已完成")
        if n_error:
            parts.append(f"{n_error} 个已失败/已取消")

        if MessageBox(
            self.tr("清空记录"),
            f"确定要清空 {'、'.join(parts)} 的任务记录吗？\n(不会删除本地文件)",
            self,
        ).exec():
            workers_to_remove = []
            for row in clearable_rows:
                task = self.task_page.model.get_task(row)
                if task and task.get("worker"):
                    workers_to_remove.append(task["worker"])

            if self.controller and workers_to_remove:
                self.controller.handle_batch_remove(workers_to_remove, force_delete_files=False)

            for row in sorted(clearable_rows, reverse=True):
                self.task_page.model.remove_task(row)

    def on_clear_all(self):
        if self.task_page.model.rowCount() == 0:
            return

        if MessageBox(
            self.tr("清空全部任务"),
            self.tr(
                "确定要清空所有任务记录吗？\n如果任务正在下载中，也会被一并取消。(不会删除本地文件)"
            ),
            self,
        ).exec():
            all_rows = list(range(self.task_page.model.rowCount()))
            workers_to_remove = []
            for row in all_rows:
                task = self.task_page.model.get_task(row)
                if task and task.get("worker"):
                    workers_to_remove.append(task["worker"])

            if self.controller and workers_to_remove:
                self.controller.handle_batch_remove(workers_to_remove, force_delete_files=False)

            for row in sorted(all_rows, reverse=True):
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
        self.help_btn.setToolTip(self.tr("帮助中心"))
        self.help_btn.installEventFilter(
            ToolTipFilter(self.help_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.help_btn.clicked.connect(self.show_help_window)
        self.help_btn.setFixedSize(46, 32)

        # 在标题栏添加通知按钮
        self.notif_btn = TransparentToolButton(FluentIcon.RINGER, self.titleBar)
        self.notif_btn.setToolTip(self.tr("消息中心"))
        self.notif_btn.installEventFilter(
            ToolTipFilter(self.notif_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.notif_btn.clicked.connect(self.show_notification_panel)
        self.notif_btn.setFixedSize(46, 32)

        from qfluentwidgets import InfoBadge, InfoBadgePosition

        self.notif_badge = InfoBadge.error(
            0, self.titleBar, target=self.notif_btn, position=InfoBadgePosition.TOP_RIGHT
        )
        self.notif_badge.hide()

        from ..notification import notification_center

        notification_center.unread_count_changed.connect(self._on_unread_count_changed)

        # 初始化徽章
        self._on_unread_count_changed(notification_center.get_unread_count())

        # 查找插入位置：尝试插在系统按钮组的最左边
        layout = self.titleBar.layout()
        # Insert the buttons to the left of the system buttons (min/max/close)
        # Assuming system buttons are the last three widgets in the title bar layout
        insert_widget = getattr(layout, "insertWidget", None) if layout else None
        count = getattr(layout, "count", None) if layout else None
        if callable(insert_widget) and callable(count):
            count_value = count()
            if isinstance(count_value, int):
                insert_widget(count_value - 3, self.notif_btn, 0, Qt.AlignmentFlag.AlignRight)
                insert_widget(count_value - 3, self.help_btn, 0, Qt.AlignmentFlag.AlignRight)

        # 给 help_btn 设置右边距，让它离系统按钮远一点
        self.help_btn.setContentsMargins(0, 0, 10, 0)

    def _on_unread_count_changed(self, count: int):
        if count > 0:
            self.notif_badge.setNum(count if count < 100 else 99)
            self.notif_badge.show()
        else:
            self.notif_badge.hide()

    def show_notification_panel(self):
        from qfluentwidgets import Flyout, FlyoutAnimationType

        from .notification_panel import NotificationFlyoutView

        if getattr(self, "_notif_flyout", None):
            try:
                if self._notif_flyout.isVisible():
                    self._notif_flyout.close()
                    return
            except RuntimeError:
                pass

        view = NotificationFlyoutView(self)
        self._notif_flyout = Flyout.make(
            view, self.notif_btn, self, aniType=FlyoutAnimationType.PULL_UP
        )

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
        QTimer.singleShot(5000, lambda: self.check_cookie_status(is_startup=True))

    def on_admin_mode_cookie_refresh(self):
        """管理员模式启动后自动刷新Cookie"""
        from ..auth.auth_service import AuthSourceType, auth_service
        from ..auth.cookie_sentinel import cookie_sentinel
        from ..utils.logger import logger

        # 只在配置了浏览器来源时刷新
        if auth_service.current_source == AuthSourceType.NONE:
            logger.info(self.tr("[AdminMode] 未配置Cookie来源，跳过自动刷新"))
            return

        if auth_service.current_source == AuthSourceType.FILE:
            logger.info(self.tr("[AdminMode] 手动文件模式，跳过自动刷新"))
            return

        if auth_service.current_source == AuthSourceType.WEBVIEW2:
            logger.info(self.tr("[AdminMode] 登录模式(WebView2)，跳过自动刷新（需要用户交互）"))
            return

        browser_name = auth_service.current_source_display
        logger.info(f"[AdminMode] 以管理员身份自动刷新Cookie: {browser_name}")

        # 显示提示
        from qfluentwidgets import InfoBar

        InfoBar.info(
            self.tr("管理员模式"),
            f"正在以管理员权限提取 {browser_name} Cookie...",
            duration=3000,
            parent=self,
        )

        # 执行刷新
        try:
            success, message = cookie_sentinel.force_refresh_with_uac()

            if success:
                InfoBar.info(
                    self.tr("Cookie提取成功"),
                    f"已从 {browser_name} 提取 Cookie（管理员权限）",
                    duration=5000,
                    parent=self,
                )
                # 自动跳转到设置页显示结果
                QTimer.singleShot(1000, lambda: self.switchTo(self.settings_interface))
            else:
                InfoBar.warning(self.tr("Cookie提取失败"), message, duration=8000, parent=self)
        except Exception as e:
            logger.exception(self.tr("[AdminMode] Cookie刷新异常"))
            InfoBar.error(self.tr("Cookie提取异常"), str(e), duration=5000, parent=self)

    def check_cookie_status(self, is_startup: bool = False):
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

            # ── 统一有效性检查（适用于 WebView2 / 浏览器 / 手动导入） ──
            # 只检查两层：
            #   1. cookie_sentinel.exists → Cookie 文件是否存在
            #   2. auth_service.last_status.valid → 关键字段完整性 + 过期检查
            #      (_validate_cookies 会先过滤掉已过期的 Cookie，再检查 SID/HSID 等是否存在)
            is_invalid = False
            reason = ""

            if not cookie_sentinel.exists:
                is_invalid = True
                if current_source == AuthSourceType.WEBVIEW2:
                    reason = self.tr("尚未登录获取 Cookie")
                elif current_source == AuthSourceType.FILE:
                    reason = self.tr("尚未导入 Cookie 文件")
                else:
                    reason = f"尚未从 {source_name} 提取 Cookie"
            elif not auth_service.last_status.valid:
                is_invalid = True
                reason = auth_service.last_status.message or self.tr("Cookie 无效")

            if is_invalid:
                logger.warning(f"[MainWindow] Cookie 无效 ({source_name}): {reason}")

                if is_startup and not cookie_sentinel.exists:
                    # 第一次运行不主动弹强打扰对话框，给个横幅引导即可
                    from qfluentwidgets import InfoBar, InfoBarPosition

                    action = (
                        self.tr("登录")
                        if current_source == AuthSourceType.WEBVIEW2
                        else self.tr("导入")
                    )
                    InfoBar.warning(
                        self.tr("Cookie 未准备就绪"),
                        f"为了保证下载稳定，建议您先前往设置页进行{action}以获取 Cookie",
                        duration=10000,
                        position=InfoBarPosition.TOP_RIGHT,
                        parent=self,
                    )
                    return

                # Chromium 浏览器非管理员 → 特殊处理：提示以管理员重启
                from ..auth.auth_service import ADMIN_REQUIRED_BROWSERS

                if current_source in ADMIN_REQUIRED_BROWSERS and not is_admin():
                    from qfluentwidgets import MessageBox

                    box = MessageBox(
                        f"{source_name} 需要管理员权限",
                        f"检测到您使用 {source_name} 作为 Cookie 来源。\n\n"
                        f"Chromium 内核浏览器使用了加密保护，\n"
                        f"需要以管理员身份运行程序才能提取 Cookie。\n\n"
                        + self.tr("是否以管理员身份重启程序？\n\n")
                        + self.tr("提示：您也可以切换到 Firefox/LibreWolf 浏览器，\n")
                        + self.tr("或使用「登录获取」方式，无需管理员权限。"),
                        self,
                    )
                    box.yesButton.setText(self.tr("以管理员身份重启"))
                    box.cancelButton.setText(self.tr("稍后再说"))

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

        except Exception as e:
            logger.error(f"[MainWindow] Cookie状态检查失败: {e}")

    def on_worker_error(self, err_data: dict) -> None:
        """
        处理后台下载任务发出的错误（支持重试所有挂起任务）
        """
        print(f"[DEBUG] on_worker_error called with err_data: {err_data}")
        if getattr(self, "_worker_error_dialog_showing", False):
            print("[DEBUG] Dialog already showing, skipping.")
            return

        self._worker_error_dialog_showing = True
        try:
            from fluentytdl.ui.components.dialogs.worker_error_dialog import WorkerErrorDialog

            print("[DEBUG] Creating WorkerErrorDialog...")
            dlg = WorkerErrorDialog(err_data, self)

            def handle_retry_all():
                from qfluentwidgets import InfoBar, InfoBarPosition

                from fluentytdl.download.download_manager import download_manager

                count = 0
                for w in download_manager.active_workers:
                    if getattr(w, "is_suspended", False):
                        w.resume_suspension("retry")
                        count += 1
                if count > 0:
                    InfoBar.success(
                        self.tr("操作成功"),
                        self.tr(f"已恢复 {count} 个挂起的任务"),
                        duration=3000,
                        parent=self,
                        position=InfoBarPosition.TOP_RIGHT,
                    )

            def handle_go_settings():
                self.switchTo(self.settings_interface)

            def handle_fetch_cookie():
                from fluentytdl.ui.components.settings.fix_registry import execute_fix_action

                execute_fix_action("extract_cookie", self)

            def handle_update_ytdlp():
                from fluentytdl.ui.components.settings.fix_registry import execute_fix_action

                execute_fix_action("update_component", self)
                # 触发后也尝试重试任务
                handle_retry_all()

            dlg.retry_all_requested.connect(handle_retry_all)
            dlg.go_settings_requested.connect(handle_go_settings)
            dlg.fetch_cookie_requested.connect(handle_fetch_cookie)
            dlg.update_ytdlp_requested.connect(handle_update_ytdlp)

            print("[DEBUG] Executing WorkerErrorDialog...")
            dlg.exec()
            print("[DEBUG] WorkerErrorDialog closed.")
        except Exception as e:
            print(f"[ERROR] Exception in on_worker_error: {e}")
            import traceback

            traceback.print_exc()
        finally:
            self._worker_error_dialog_showing = False

    def _show_cookie_repair(self, source_type, source_name: str, reason: str) -> None:
        """
        弹出 Cookie 修复引导（复用 CookieRepairDialog）

        根据当前验证模式自动调整引导文案和按钮行为。
        """
        from fluentytdl.ui.components.dialogs.cookie_repair_dialog import CookieRepairDialog

        from ..auth.auth_service import AuthSourceType
        from ..auth.cookie_sentinel import cookie_sentinel

        # 映射 auth_source 字符串
        source_map = {
            AuthSourceType.WEBVIEW2: "webview2",
            AuthSourceType.FILE: "file",
        }
        auth_source_str = source_map.get(source_type, "browser")

        dialog = CookieRepairDialog(reason, parent=self, auth_source=auth_source_str)

        # 根据模式定制按钮文案
        if dialog._auth_source == "webview2":
            dialog.yesButton.setText(self.tr("重新登录"))
        elif dialog._auth_source == "local":
            dialog.yesButton.setText(self.tr("重新导入"))
        elif dialog._auth_source == "browser":
            dialog.yesButton.setText(self.tr("重新提取"))
            dialog.setWindowTitle(self.tr("Cookie 文件需要更新"))
        else:
            dialog.yesButton.setText(self.tr("重新提取"))

        # 自动修复信号
        def on_auto_repair():
            if source_type == AuthSourceType.WEBVIEW2:
                # WebView2 → 跳转到设置页面的登录区域
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
