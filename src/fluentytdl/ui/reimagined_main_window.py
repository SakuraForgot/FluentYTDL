from __future__ import annotations

import os
import time
from collections.abc import Iterable
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
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
    PrimaryPushButton,
    PushButton,
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
from .components.download_item_widget import DownloadItemWidget
from .cover_download_page import CoverDownloadPage
from .help_window import HelpWindow
from .pages.history_page import HistoryPage
from .parse_page import ParsePage
from .settings_page import SettingsPage
from .subtitle_download_page import SubtitleDownloadPage
from .unified_task_list_page import UnifiedTaskListPage
from .vr_parse_page import VRParsePage
from .welcome_wizard import WelcomeWizardDialog


class TaskListPage(QWidget):
    """通用的任务列表页面"""

    def __init__(self, title: str, icon: FluentIcon, parent=None):
        super().__init__(parent)
        self.setObjectName(title.lower().replace(" ", "_"))
        self.page_title = title

        self.v_layout = QVBoxLayout(self)
        self.v_layout.setContentsMargins(20, 20, 20, 20)
        self.v_layout.setSpacing(10)

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
    def __init__(self) -> None:
        super().__init__()

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
        self.parse_page.parse_requested.connect(self.show_selection_dialog)
        self.vr_parse_page.parse_requested.connect(self.show_vr_selection_dialog)
        self.subtitle_page.parse_requested.connect(self.show_subtitle_selection_dialog)
        self.cover_page.parse_requested.connect(self.show_cover_selection_dialog)
        self.settings_interface.clipboardAutoDetectChanged.connect(
            self.set_clipboard_monitor_enabled
        )

        # 统一任务列表页面信号
        self.task_page.card_remove_requested.connect(self.on_remove_card)
        self.task_page.card_resume_requested.connect(self.on_resume_card)

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

        # 新建任务 (Primary Action - 位于任务列表内操作)
        new_task_btn = PrimaryPushButton(FluentIcon.ADD, "新建任务", self)
        new_task_btn.setToolTip("创建下载任务")
        new_task_btn.installEventFilter(
            ToolTipFilter(new_task_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        new_task_btn.clicked.connect(lambda: self.switchTo(self.parse_page))

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
        batch_btn = PushButton(FluentIcon.CHECKBOX, "批量操作", page)
        batch_btn.setToolTip("进入批量操作模式")
        batch_btn.installEventFilter(
            ToolTipFilter(batch_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )

        select_all_btn = PushButton(FluentIcon.ACCEPT, "全选", page)
        select_all_btn.setToolTip("全选")
        select_all_btn.installEventFilter(
            ToolTipFilter(select_all_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        select_all_btn.hide()

        start_sel_btn = PushButton(FluentIcon.PLAY, "开始选中", page)
        start_sel_btn.setToolTip("开始选中任务")
        start_sel_btn.installEventFilter(
            ToolTipFilter(start_sel_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        start_sel_btn.hide()

        pause_sel_btn = PushButton(FluentIcon.PAUSE, "暂停选中", page)
        pause_sel_btn.setToolTip("暂停选中任务")
        pause_sel_btn.installEventFilter(
            ToolTipFilter(pause_sel_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        pause_sel_btn.hide()

        del_sel_btn = PushButton(FluentIcon.DELETE, "删除选中", page)
        del_sel_btn.setToolTip("删除选中任务")
        del_sel_btn.installEventFilter(
            ToolTipFilter(del_sel_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        del_sel_btn.hide()

        def toggle_batch():
            is_batch = getattr(page, "_is_batch_mode", False)
            new_state = not is_batch
            page._is_batch_mode = new_state
            page.set_selection_mode(new_state)

            select_all_btn.setVisible(new_state)
            del_sel_btn.setVisible(new_state)
            start_sel_btn.setVisible(new_state)
            pause_sel_btn.setVisible(new_state)

            if new_state:
                batch_btn.setIcon(FluentIcon.CANCEL)
                batch_btn.setText("退出批量")
                batch_btn.setToolTip("退出批量模式")
            else:
                batch_btn.setIcon(FluentIcon.CHECKBOX)
                batch_btn.setText("批量操作")
                batch_btn.setToolTip("进入批量操作模式")

        batch_btn.clicked.connect(toggle_batch)
        select_all_btn.clicked.connect(page.select_all)
        start_sel_btn.clicked.connect(lambda: self.on_batch_start(page))
        pause_sel_btn.clicked.connect(lambda: self.on_batch_pause(page))
        del_sel_btn.clicked.connect(lambda: self.on_batch_delete(page))

        # 添加到布局 (分组)
        page.action_layout.setSpacing(0)

        # 1. 核心操作
        page.action_layout.addWidget(new_task_btn)

        # 分隔符
        page.action_layout.addSpacing(16)

        # 2. 全局控制
        page.action_layout.addWidget(start_all)
        page.action_layout.addWidget(pause_all)
        page.action_layout.addWidget(open_dir)
        page.action_layout.addWidget(clear_completed)

        # 分隔
        page.action_layout.addSpacing(16)

        # 3. 批量操作 (靠右)
        page.action_layout.addWidget(batch_btn)

        page.action_layout.addSpacing(8)
        page.action_layout.addWidget(select_all_btn)
        page.action_layout.addWidget(start_sel_btn)
        page.action_layout.addWidget(pause_sel_btn)
        page.action_layout.addWidget(del_sel_btn)

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
        self, url: str, mode: str = "default", vr_mode: bool = False, smart_detect: bool = False
    ):
        """通用方法：显示非阻塞的任务配置窗口"""
        try:
            # 创建新窗口实例
            window = DownloadConfigWindow(
                url, self, vr_mode=vr_mode, mode=mode, smart_detect=smart_detect
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

    def show_selection_dialog(self, url: str, smart_detect: bool = False):
        self._show_config_window(url, mode="default", smart_detect=smart_detect)

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
        logger.info(f"[DEBUG] add_tasks called with {len(tasks)} tasks")
        default_dir = config_manager.get("download_dir")

        for i, (t_title, t_url, t_opts, t_thumb) in enumerate(tasks):
            logger.info(f"[DEBUG] Processing task {i + 1}: {t_title[:30]}...")
            # Inject default download directory if not specified
            if default_dir and "paths" not in t_opts:
                outtmpl = t_opts.get("outtmpl")
                if not (isinstance(outtmpl, str) and os.path.isabs(outtmpl)):
                    t_opts["paths"] = {"home": str(default_dir)}

            logger.info(f"[DEBUG] Creating worker for URL: {t_url}")
            worker = download_manager.create_worker(t_url, t_opts)
            logger.info(f"[DEBUG] Worker created: {worker}")

            card = DownloadItemWidget(worker, t_title, t_opts)
            if t_thumb:
                card.load_thumbnail(str(t_thumb))

            logger.info("[DEBUG] Adding card to task_page")
            # 添加到统一任务列表（信号由 UnifiedTaskListPage 内部连接）
            self.task_page.add_card(card)
            logger.info(f"[DEBUG] Card added, task_page count: {self.task_page.count()}")

            # 初始状态
            started = download_manager.start_worker(worker)
            logger.info(f"[DEBUG] Worker started: {started}")
            if started:
                card.set_state("running")
            else:
                card.set_state("queued")

            # 切换到任务列表页
            logger.info("[DEBUG] Switching to task_page")
            self.switchTo(self.task_page)
            logger.info(f"[DEBUG] Task {i + 1} processing complete")

    def on_remove_card(self, card: DownloadItemWidget):
        try:
            # 1. Critical Logs & Config Reading
            raw_policy = config_manager.get("deletion_policy")
            logger.info(f"Triggering delete for card {id(card)}")
            logger.info(f"Current Config Policy Raw Value: {raw_policy} (Type: {type(raw_policy)})")

            # 2. Force Type Conversion & Normalization
            policy = str(raw_policy or "alwaysask").lower().strip()
            logger.info(f"Normalized Policy: '{policy}'")

            # 3. Terminate worker immediately (Robust File Deletion)
            if getattr(card, "worker", None):
                try:
                    if hasattr(card.worker, "stop"):
                        card.worker.stop()
                    else:
                        cancel = getattr(card.worker, "cancel", None)
                        if callable(cancel):
                            cancel()

                    # Force kill process tree
                    force_kill = getattr(card.worker, "force_kill", None)
                    if callable(force_kill):
                        force_kill()

                    # If it's still running, force terminate QThread
                    if hasattr(card.worker, "isRunning") and card.worker.isRunning():
                        if hasattr(card.worker, "terminate"):
                            card.worker.terminate()
                        card.worker.wait(200)
                except Exception as e:
                    logger.error(f"Error stopping worker: {e}")

            # 4. Policy helpers (robust matching + legacy fallbacks)
            def _is_keep(p: str) -> bool:
                return any(tok in p for tok in ("keep", "keepfiles", "keep_files", "keep-files"))

            def _is_delete(p: str) -> bool:
                # accept many synonyms
                return any(
                    tok in p
                    for tok in (
                        "delete",
                        "deletefiles",
                        "delete_files",
                        "delete-files",
                        "remove",
                        "removefiles",
                    )
                )

            def _is_ask(p: str) -> bool:
                return any(tok in p for tok in ("ask", "alwaysask", "always_ask", "always-ask"))

            # Legacy boolean keys fallback: if user enabled old ask flags, prefer ask behavior
            legacy_ask = bool(config_manager.get("remove_task_ask_delete_source") or False) or bool(
                config_manager.get("remove_task_ask_delete_cache") or False
            )

            # 4.a KeepFiles
            if _is_keep(policy):
                logger.info("Policy matched: KeepFiles. Removing card only.")
                self.task_page.remove_card(card)
                return

            # Collect paths for Delete or Ask
            paths_to_delete = set()
            if hasattr(card, "recorded_paths"):
                paths_to_delete.update(card.recorded_paths)
            try:
                current_paths = self._collect_existing_cache_paths([card])
                paths_to_delete.update(current_paths)
            except Exception:
                pass

            valid_paths = [p for p in paths_to_delete if p and os.path.exists(p)]
            cache_paths = [p for p in valid_paths if p.endswith(".part") or p.endswith(".ytdl")]
            source_paths = [p for p in valid_paths if p not in cache_paths]

            # 4.b DeleteFiles (explicit delete preference or legacy config)
            if _is_delete(policy) and not _is_ask(policy) and not legacy_ask:
                logger.info("Policy matched: DeleteFiles. Deleting files silently.")
                if valid_paths:
                    logger.info(f"Deleting {len(valid_paths)} files: {valid_paths}")
                    self._delete_files_best_effort(valid_paths, success_title="已删除相关文件")
                else:
                    logger.info("No files found to delete.")
                self.task_page.remove_card(card)
                return

            # Fallback: AlwaysAsk (or unknown policy)
            if _is_ask(policy) or legacy_ask or (not _is_delete(policy) and not _is_keep(policy)):
                logger.info("Policy matched or falling back to: AlwaysAsk.")
            else:
                # Unknown token combination, but safe fallback to Ask
                logger.warning(f"Unknown deletion policy: '{policy}', falling back to ASK.")

            title = "删除任务"
            content = "确定要从列表中移除此任务吗？"

            box = MessageBox(title, content, self)
            box.yesButton.setText("删除")
            box.cancelButton.setText("取消")

            delete_cache_cb = None
            if cache_paths:
                delete_cache_cb = QCheckBox(f"同时删除 {len(cache_paths)} 个缓存文件", box)
                delete_cache_cb.setChecked(True)
                # MessageBox (qfluentwidgets) uses textLayout for content area
                try:
                    box.textLayout.addWidget(delete_cache_cb)
                except Exception:
                    box.vBoxLayout.addWidget(delete_cache_cb)

            delete_source_cb = None
            if source_paths:
                delete_source_cb = QCheckBox(f"同时删除 {len(source_paths)} 个本地文件", box)
                delete_source_cb.setChecked(False)
                try:
                    box.textLayout.addWidget(delete_source_cb)
                except Exception:
                    box.vBoxLayout.addWidget(delete_source_cb)

            if not box.exec():
                logger.info("User cancelled deletion.")
                return

            final_paths = []
            if delete_cache_cb and delete_cache_cb.isChecked():
                final_paths.extend(cache_paths)
            if delete_source_cb and delete_source_cb.isChecked():
                final_paths.extend(source_paths)

            if final_paths:
                self._delete_files_best_effort(final_paths, success_title="已删除选中文件")

            self.task_page.remove_card(card)
            logger.info("Card removed successfully.")

        except Exception as e:
            logger.exception(f"Critical error in on_remove_card: {e}")
            # Last resort: try to remove the card anyway so UI isn't stuck
            try:
                self.task_page.remove_card(card)
            except Exception:
                pass

    def _delete_files_best_effort(self, paths: list[str], success_title: str = "已删除文件"):
        """Try to delete a list of files/dirs, reporting results via InfoBar."""
        success_count = 0
        errors = []
        for path in paths:
            # Retry loop for file locks
            deleted = False
            last_error = None
            for _ in range(3):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        deleted = True
                        break
                    elif os.path.isdir(path):
                        pass
                    else:
                        # Path doesn't exist?
                        deleted = True  # Treat as success
                        break
                except Exception as e:
                    last_error = e
                    time.sleep(0.5)

            if deleted:
                if os.path.basename(path) not in [".", ".."]:  # Filter trivial
                    success_count += 1
            elif last_error:
                errors.append(f"{os.path.basename(path)}: {last_error}")

        if errors:
            InfoBar.warning(
                "部分文件删除失败",
                "\n".join(errors[:3]) + ("\n..." if len(errors) > 3 else ""),
                duration=5000,
                parent=self,
            )
        elif success_count > 0:
            InfoBar.success(
                success_title, f"成功清理了 {success_count} 个文件。", duration=3000, parent=self
            )

    # --- Helper Methods Copied from Old MainWindow ---
    def _collect_existing_cache_paths(self, cards: Iterable[DownloadItemWidget]) -> list[str]:
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

    def _collect_existing_output_paths(self, cards: Iterable[DownloadItemWidget]) -> list[str]:
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

    def on_resume_card(self, card: DownloadItemWidget):
        # 重启任务逻辑
        # 需要重新创建 worker
        new_worker = download_manager.create_worker(card.url, card.opts)
        # 更新 card 的 worker
        card._bind_worker(new_worker)

        # Update status text immediately to show feedback
        card.update_status("正在恢复下载...")

        download_manager.start_worker(new_worker)
        card.set_state("running")

    def on_batch_start(self, page: Any):
        cards = page.get_selected_cards()
        for card in cards:
            if isinstance(card, DownloadItemWidget):
                if card.state() in {"paused", "error", "queued"}:
                    self.on_resume_card(card)

    def on_batch_pause(self, page: Any):
        cards = page.get_selected_cards()
        for card in cards:
            if isinstance(card, DownloadItemWidget):
                if card.state() == "running":
                    # Use stop() when available; fall back to cancel() for legacy workers.
                    try:
                        if hasattr(card.worker, "stop"):
                            card.worker.stop()
                        else:
                            cancel = getattr(card.worker, "cancel", None)
                            if callable(cancel):
                                cancel()
                    except Exception:
                        # Best-effort; ignore errors to avoid blocking batch operations
                        pass

    def on_batch_delete(self, page: Any):
        cards = page.get_selected_cards()
        if not cards:
            return

        if not MessageBox("批量删除", f"确定要删除选中的 {len(cards)} 个任务吗？", self).exec():
            return

        for card in cards:
            if isinstance(card, DownloadItemWidget):
                self.on_remove_card(card)

    def on_start_all(self):
        for card in list(self.task_page._cards):
            if isinstance(card, DownloadItemWidget) and card.state() in {
                "paused",
                "error",
                "queued",
            }:
                self.on_resume_card(card)

    def on_pause_all(self):
        download_manager.stop_all()

    def on_clear_completed(self):
        completed_cards = [
            c
            for c in list(self.task_page._cards)
            if isinstance(c, DownloadItemWidget) and c.state() == "completed"
        ]
        if not completed_cards:
            return
        if MessageBox(
            "清空记录", "确定要清空所有已完成任务记录吗？\n(不会删除本地文件)", self
        ).exec():
            for card in completed_cards:
                self.task_page.remove_card(card)
                card.deleteLater()

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
