from __future__ import annotations

import os
from enum import Enum
from functools import partial
from typing import Any

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSystemTrayIcon,
)
from qfluentwidgets import (
    Action,
    FluentIcon,
    FluentWindow,
    InfoBadge,
    InfoBadgeManager,
    InfoBarIcon,
    InfoBarPosition,
    MessageBox,
    NavigationItemPosition,
    PushButton,
    SplashScreen,
    SystemThemeListener,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from fluentytdl.ui.components.common.clipboard_monitor import ClipboardMonitor
from fluentytdl.ui.components.common.custom_info_bar import InfoBar
from fluentytdl.ui.components.common.interruptible_navigation import (
    install_interruptible_navigation,
)
from fluentytdl.ui.components.common.responsive_command_bar import ResponsiveCommandBar
from fluentytdl.ui.components.dialogs.download_config_window import DownloadConfigWindow
from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..core.config_manager import config_manager
from ..download.download_manager import download_manager
from ..utils.icons import load_app_icon
from ..utils.logger import logger
from .channel_parse_page import ChannelParsePage
from .cover_download_page import CoverDownloadPage
from .help_window import HelpWindow
from .models.task_row import TaskRow
from .parse_page import ParsePage
from .quick_add_panel import QuickAddPanel
from .settings_page import SettingsPage
from .subtitle_download_page import SubtitleDownloadPage
from .unified_task_list_page import UnifiedTaskListPage
from .vr_parse_page import VRParsePage
from .welcome_wizard import WelcomeWizardDialog

TITLE_BAR_BADGE_POSITION = "fluentytdl.titleBarBadge"

# 窗口最小宽度。这不只是个初始尺寸 —— 页面里每一行的**最小宽**都必须能塞进这个宽度，
# 否则 `QHBoxLayout` 只能去压别人，而被压过最小宽的控件会被 `QWidget.setGeometry`
# 反弹回去，表现就是控件互相错叠（见 `unified_task_list_page._reserve_pivot_widths`）。
# 顶栏那一行的回归覆盖在 `tests/test_task_header_layout.py`。
MIN_WINDOW_WIDTH = 1150


@InfoBadgeManager.register(TITLE_BAR_BADGE_POSITION)
class TitleBarBadgeManager(InfoBadgeManager):
    """把未读徽章画在标题栏按钮的右上角「内侧」。

    默认的 `InfoBadgePosition.TOP_RIGHT` 把徽章中心对齐到目标控件的右上角顶点，
    即 `y = target.top() - badge.height() // 2`。标题栏只有 48px 高，按钮 32px
    垂直居中后 `top() == 8`，减掉半个徽章高度仍是负数，上半截会被标题栏裁掉。
    铃铛图标只有 16px 而按钮宽 46px，右上角有足够留白，所以直接把徽章放进按钮内部。
    """

    def position(self) -> QPoint:
        geo = self.target.geometry()
        return QPoint(geo.right() - self.badge.width() - 3, geo.top() + 3)


TASK_NAV_BADGE_POSITION = "fluentytdl.taskNavBadge"

# 「任务」导航徽标的显示上限。折叠态导航项只有 40px 宽，两位数已经贴满图标了。
_NAV_BADGE_MAX = 99


@InfoBadgeManager.register(TASK_NAV_BADGE_POSITION)
class TaskNavBadgeManager(InfoBadgeManager):
    """把活跃任务数钉在导航项右侧，**可见性由我们说了算**。

    库自带的 `InfoBadgePosition.NAVIGATION_ITEM`（`NavigationItemInfoBadgeManager`）
    不能直接用来做「为 0 就隐藏」的徽标：它在 `QEvent.Show` 时无条件
    `badge.show()`，`position()` 里又会 `setVisible(target.isVisible())`；而基类会在
    目标 Resize / Move 时重新调 `position()` —— 于是侧边栏每展开或折叠一次，
    刚因为「活跃数为 0」而隐藏的徽标就会被重新点亮。这里复刻它的几何，但可见性
    只认 `wanted`（由 `_on_task_counts_changed` 设置）。
    """

    def __init__(self, target, badge):
        super().__init__(target, badge)
        # 我们自己的意愿：活跃数 > 0 才允许显示。导航项自身不可见时（折叠动画中间态）
        # 仍然要跟着藏起来，所以最终可见性是 `wanted and target.isVisible()`。
        self.wanted = False

    def _sync_visibility(self) -> None:
        self.badge.setVisible(self.wanted and self.target.isVisible())

    def eventFilter(self, obj, e: QEvent):
        if obj is self.target and e.type() in (
            QEvent.Type.Show,
            QEvent.Type.Resize,
            QEvent.Type.Move,
        ):
            self._sync_visibility()
        return super().eventFilter(obj, e)

    def position(self) -> QPoint:
        """几何与库自带的 `NavigationItemInfoBadgeManager.position()` 保持一致。

        刻意不自创坐标：这样折叠 / 展开、树形子项缩进的手感都和其他 qfluentwidgets
        应用一样，我们只接管可见性。
        """
        target = self.target
        # 折叠态导航项只有 40px 宽，徽标压到图标右上角
        if getattr(target, "isCompacted", False):
            geo = target.geometry()
            x = geo.right() - self.badge.width() - 2
            if x < geo.left():
                # 「99+」比折叠项本身还宽，右对齐会把它甩到面板左边界外面去（x 变负数）。
                # 退化成居中，让溢出对称地分到两侧。
                x = geo.left() + (geo.width() - self.badge.width()) // 2
            return QPoint(x, geo.top() + 2)
        # 展开态：贴右边内缩；非叶子项右侧有展开箭头，得多让 35px 出来
        dx = 10 if getattr(target, "isLeaf", lambda: True)() else 35
        return QPoint(
            target.geometry().right() - self.badge.width() - dx,
            # 垂直对齐**第一行**而不是整体居中：树形项展开后自身会变高
            target.y() + 18 - self.badge.height() // 2,
        )


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

        # 窗口图标：main.py 只设了 QApplication 级别的图标 —— `QWidget.windowIcon()` 会
        # 继承它（所以启动图和托盘一直是对的），但 `windowIconChanged` 信号从没发过，
        # 而 `FluentTitleBar.iconLabel` 只在那个信号里才填 pixmap，于是标题栏左上角
        # 软件名旁边一直是个 18x18 的空白。这里显式设一次，图标才会真的画出来。
        # 素材缺失时不要设空 QIcon —— 那会把继承来的 app 图标一并清掉，
        # 连带 SplashScreen（下面用的 `self.windowIcon()`）和任务栏图标都变空白。
        app_icon = load_app_icon()
        if not app_icon.isNull():
            self.setWindowIcon(app_icon)
        else:
            log_text(logger, "warning", "应用图标资源缺失，标题栏与启动图将没有图标")

        self.resize(MIN_WINDOW_WIDTH, 780)
        # 锁定最小宽度，防止两个 bug 导致的自动变宽：
        # 1. 切换到英文时文本变宽触发的布局最小宽度增长
        # 2. NavigationPanel 展开/收起动画触发的 setFixedWidth 棘轮效应
        # 用户仍可手动拖拽边缘把窗口拉宽，只阻止自动增长
        self.setMinimumWidth(MIN_WINDOW_WIDTH)

        # 居中
        desktop = QApplication.screens()[0].availableGeometry()
        w, h = desktop.width(), desktop.height()
        self.move(w // 2 - self.width() // 2, h // 2 - self.height() // 2)

        # === 启动图：必须在建页面之前 show 出来 ===
        #
        # 下面那一串页面构造是启动耗时的大头（qfluentwidgets 的 SettingCard 很重，
        # 光 SettingsPage 就有 70 多张卡，每张都要 adjustSize + 套样式表），实测约 1.3 秒，
        # 而它**造不快也没法异步** —— QWidget 只能在主线程建。这段时间里事件循环还没开始
        # 转，Windows 给的反馈就是一个转圈的鼠标，用户眼里是「点了图标没反应」。
        #
        # 所以先把窗口和启动图摆出来，再去建页面：等待时长没变，但从「疑似没启动成功」
        # 变成「看得见它在启动」。`processEvents()` 那一格是必须的 —— 只 show() 不给事件
        # 循环喘息，启动图一个像素都不会画出来（和被删掉的那句 `QThread.msleep(100)`
        # 犯的是同一个错，见 `settings_page._update_vr_hardware_status`）。
        #
        # **原实现在 `__init__` 快结束时才 `SplashScreen(...)` 紧接着 `finish()`**，
        # 中间什么都没有：既没 show 也没让出事件循环，等于造一个控件再立刻关掉 ——
        # 用户从来没见过这张启动图，只白花了构造它的时间。
        self.splashScreen = SplashScreen(self.windowIcon(), self)
        self.splashScreen.setIconSize(QSize(106, 106))
        self.show()
        QApplication.processEvents()

        # 活跃的子窗口列表 (防止GC回收)
        self._active_sub_windows = []

        # === 初始化页面 ===
        # 统一任务列表页面（下载中 + 历史记录合流，替代原有的四个分页与「下载历史」页）
        self.task_page = UnifiedTaskListPage(self)

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

        # 信号连接
        self.parse_page.parse_requested.connect(
            lambda url: self.show_selection_dialog(url, smart_detect=False, playlist_flat=True)
        )
        self.quick_parse_page.download_requested.connect(self.handle_quick_download_requested)
        self.vr_parse_page.parse_requested.connect(self.show_vr_selection_dialog)
        self.channel_parse_page.parse_requested.connect(self._show_channel_dialog)
        self.subtitle_page.parse_requested.connect(self.show_subtitle_selection_dialog)
        self.cover_page.parse_requested.connect(self.show_cover_selection_dialog)
        self.settings_interface.clipboardAutoDetectChanged.connect(
            self.set_clipboard_monitor_enabled
        )

        # 统一任务列表页面信号
        self.task_page.card_remove_requested.connect(self.on_remove_task)
        self.task_page.card_resume_requested.connect(self.on_pause_resume_task)
        self.task_page.card_folder_requested.connect(self.on_open_target_folder)
        self.task_page.route_to_parse.connect(lambda: self.switchTo(self.parse_page))
        # 「重新解析」原本是历史页的专属能力，历史页删除后由任务列表的右键菜单承接。
        self.task_page.reparse_requested.connect(
            lambda url: self.show_selection_dialog(url, smart_detect=True)
        )
        # 分桶计数变化 → 刷新「任务」导航项上的活跃数徽标
        self.task_page.counts_changed.connect(self._on_task_counts_changed)

        # 批量操作命令栏信号
        self.task_page.batch_start_requested.connect(self.on_batch_start)
        self.task_page.batch_pause_requested.connect(self.on_batch_pause)
        self.task_page.batch_delete_requested.connect(self.on_batch_delete)
        self.task_page.batch_downgrade_requested.connect(self.on_batch_downgrade)

        # 注：历史记录的「实时更新」曾经挂在 history_service.on_history_added 上，
        # 但那两个 on_history_* 函数体是 `pass` 空桩，回调从未被触发过。
        # 已移除死连接；合流后历史行就是任务列表里的行，实时刷新由模型自己承担。

        # === 标题栏扩展 ===
        self.init_title_bar()

        # === 软件更新通知 ===
        from ..core.component_update_manager import component_update_manager
        from ..notification import install_update_notifier

        # 「有更新」统一写进消息中心（小铃铛），不再各处弹 InfoBar
        install_update_notifier()
        from .announcement_controller import AnnouncementController

        self._announcement_controller = AnnouncementController(self)
        QApplication.instance().aboutToQuit.connect(self._announcement_controller.service.stop)
        component_update_manager.app_update_available.connect(self._on_app_update_available)
        component_update_manager.apply_requested.connect(self._on_update_apply_requested)

        # === 首次启动检测 ===
        QTimer.singleShot(1000, self.check_first_run)

        # === Cookie 启动分级提醒 ===
        # 订阅信号而不是 QTimer 猜时序：以前是「启动 5 秒后去问 cookie 状态」，
        # 静默刷新慢一点就必然读到旧状态，于是"开局始终弹出需要获取 cookie"。
        # 现在没有信号就没有提醒；sentinel 的 finally 保证每次启动恰好发一次。
        # 发出方是后台线程，必须 QueuedConnection 把 slot 拉回 Qt 主线程。
        from ..auth.cookie_sentinel import cookie_sentinel

        cookie_sentinel.startupHealthReady.connect(
            self.check_cookie_status, Qt.ConnectionType.QueuedConnection
        )

        # === 启动更新检查（错峰执行，避免启动瞬间卡顿）===
        QTimer.singleShot(3000, self.settings_interface.schedule_startup_update_check)

        # === 管理员模式：自动刷新 Cookie ===
        if self._is_admin:
            QTimer.singleShot(2000, self.on_admin_mode_cookie_refresh)

        # === 恢复重启前的未完成任务到 UI 层 ===
        self._restore_tasks_to_ui()

        # === 监听 DownloadManager 发出的全局 Worker 错误 ===
        from fluentytdl.download.download_manager import download_manager

        download_manager.worker_error.connect(self.on_worker_error)
        download_manager.worker_warning.connect(self.on_worker_warning)

        # === 收起启动图，露出真正的界面 ===
        # 放在 `__init__` 的最后一行：此刻页面、导航、托盘、信号全部就位，
        # 用户看到界面的第一眼就是可用的界面。
        self.splashScreen.finish()

    def _restore_tasks_to_ui(self) -> None:
        """将 DownloadManager 中恢复的 Worker 同步到 DownloadListModel"""
        restored = 0
        for worker in download_manager.active_workers:
            title = getattr(worker, "v_title", "") or ""
            thumb = getattr(worker, "v_thumbnail", "") or ""
            self.task_page.add_task(worker, title, thumb)
            restored += 1
        if restored > 0:
            log_text(logger, "info", "[MainWindow] 已恢复 {0} 个未完成任务到 UI", restored)
            # UI 初始化完成后触发一次 pump，启动排队中的任务
            QTimer.singleShot(500, download_manager.pump)

    def _on_app_update_available(self, info: dict) -> None:
        """记录一行日志即可 —— 用户可见的提醒由消息中心（小铃铛）负责。

        以前这里也弹一条 InfoBar，而设置页的 `AppUpdateSettingCard._on_update_available`
        同样弹一条，同一件事会出现两条提示；启动自动检查时它还会和 5 个组件的检查结果
        一起炸出来。现在统一走 `notification/update_notifier.py` 写进消息中心。
        """
        version = info.get("version", "?")
        log_text(logger, "info", "[MainWindow] 检测到软件更新: {0}", version)

    def _on_update_apply_requested(self) -> None:
        """后端已批准更新，执行优雅退出。

        `updater.exe` 不在这里启动 —— 它由 `main.py` 在 `app.exec()` 返回之后拉起，
        这样 `quit_app()` 里那些可能耗时数秒的收尾（worker shutdown、db_writer 落盘）
        必然在 updater 开始替换文件之前完成。

        用 singleShot 把退出挪出信号发射栈：此刻我们还在
        `request_app_core_update()` 的 emit 里，不能在这里同步跑完整个 shutdown。
        """
        log_text(logger, "info", "[MainWindow] 收到更新申请，开始优雅退出")
        QTimer.singleShot(0, self.quit_app)

    def init_navigation(self):
        # 减小侧边栏展开时的宽度，避免留白过多
        self.navigationInterface.setExpandWidth(190)
        # 展开/收起动画换成可打断实现，否则动画途中的反向操作会被丢弃或跳变
        install_interruptible_navigation(self.navigationInterface)
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

        # 3. 任务（下载中 + 历史合流的单入口；分桶由页面内部的 Pivot 承担）
        self.task_nav_item = self.addSubInterface(
            self.task_page,
            FluentIcon.DOWNLOAD,
            self.tr("任务"),
            position=NavigationItemPosition.TOP,
        )
        self._init_task_nav_badge()

        self.addSubInterface(
            self.settings_interface,
            FluentIcon.SETTING,
            self.tr("设置"),
            position=NavigationItemPosition.BOTTOM,
        )

    def _init_task_nav_badge(self) -> None:
        """给「任务」导航项挂一枚活跃数徽标。

        代替被放弃的全局状态栏（`init_status_bar` 至今是空桩）：合流之后列表里既有
        正在下载的行也有历史行，用户在别的页面时唯一想知道的就是「还有几个在跑」。

        父控件取导航项**自己的父控件** —— `InfoBadgeManager.position()` 返回的是
        `target.geometry()` 坐标系里的点，也就是导航项父控件的局部坐标；挂到别处
        （比如 `navigationInterface`）会整体偏移一个面板边距。
        """
        item = self.task_nav_item
        # `attension` 级别的底色就是 `themeColor()`，而且是在 `paintEvent` 里现取的 ——
        # 用户改强调色 / 切明暗时徽标自己就跟上了，不能用 `custom()` 把颜色写死。
        self.task_nav_badge = InfoBadge.attension(
            0, item.parent(), target=item, position=TASK_NAV_BADGE_POSITION
        )
        self.task_nav_badge.hide()

    def _on_task_counts_changed(self, counts: dict) -> None:
        """按「下载中」桶的数量刷新导航徽标；为 0 就藏起来。

        用 `active` 而不是 `all`：合流后 `all` 里绝大多数是历史行，把上千条已完成
        永久钉在导航栏上没有信息量。徽标语义 = 「还有几个没下完」。
        """
        badge = getattr(self, "task_nav_badge", None)
        if badge is None:
            return

        active = int(counts.get("active", 0) or 0)
        manager = badge.manager
        if manager is not None:
            manager.wanted = active > 0
        if active <= 0:
            badge.hide()
            return

        # 折叠态的导航项只有 40px 宽，三位数就把图标盖住了 —— 上限 99，超出写「99+」。
        # 不能截成 `99`：那看起来是个精确数字，而实际可能是 1234。
        badge.setText(str(active) if active <= _NAV_BADGE_MAX else f"{_NAV_BADGE_MAX}+")
        # 1 位数 → 2 位数时宽度会变，必须重新 adjustSize + 重新定位（同 notif_badge）
        badge.adjustSize()
        if manager is not None:
            badge.move(manager.position())
        badge.show()
        badge.raise_()

    def init_page_actions(self):
        """把任务页顶部的全局动作装进一个 `CommandBar`。

        原来是 5 颗并排的 `TransparentToolButton`：窗口一窄只能互相挤扁，而 `CommandBar`
        自带溢出（放不下就收进「更多」菜单），并且 `CommandButton` 会自己从 `action.toolTip()`
        接管提示（内置 `CommandToolTipFilter`），不必再逐颗 `installEventFilter`。

        「批量操作」开关整体删除 —— 常驻多选之后没有模式可切，选中任意一行批量条就会滑入。

        两颗清空动作的标签都带「（当前筛选）」：它们的作用域已经从「source 全表」收敛为
        「当前筛选可见项」，标签不写清楚就会出现「在『已完成』页签下点清空全部，
        结果正在下载的任务也被取消」这种事故。
        """
        page = self.task_page

        bar = ResponsiveCommandBar(page)
        # 破坏性动作单独分到分隔符右边，避免和「开始 / 暂停 / 打开目录」混在一起误点
        entries = [
            (FluentIcon.PLAY, self.tr("全部开始"), self.on_start_all),
            (FluentIcon.PAUSE, self.tr("全部暂停"), self.on_pause_all),
            (FluentIcon.FOLDER, self.tr("打开下载目录"), self.on_open_download_dir),
            None,
            (
                FluentIcon.DELETE,
                self.tr("清空已完成/已失败记录（当前筛选）"),
                self.on_clear_completed,
            ),
            (FluentIcon.BROOM, self.tr("清空全部任务（当前筛选）"), self.on_clear_all),
        ]
        for entry in entries:
            if entry is None:
                bar.addSeparator()
                continue
            icon, text, slot = entry
            # 必须是**不可勾选**的 Action：CommandButton 会镜像 action.isCheckable()，
            # 可勾选的话这些一次性动作会变成按下不弹起的开关。
            action = Action(icon, text, self)
            action.triggered.connect(slot)
            bar.addAction(action)

        # `ResponsiveCommandBar` 补上了 `sizeHint()`（库里的 `CommandBar` 没有），所以
        # **不要**再调 `resizeToSuitableWidth()` —— 那是 `setFixedWidth`，会把最小宽一起
        # 钉死，行内放不下时布局只能去压别人，压出来的就是控件互相错叠。
        page.action_layout.setSpacing(0)
        page.action_layout.addWidget(bar)
        self.task_command_bar = bar

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
            log_text(logger, "warning", "托盘图标资源缺失，已跳过系统托盘初始化")
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
        flow=None,
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
                flow=flow,
            )

            # 连接信号。用 partial 把窗口的 flow 绑进槽，而不是加进信号签名 ——
            # 智能检测切换模式会开新窗口，flow 传下去这一整串操作才是时间线上的一条链。
            window.downloadRequested.connect(partial(self.add_tasks, flow=window.trace))
            window.windowClosed.connect(self._cleanup_sub_window)
            window.request_vr_switch.connect(
                partial(self.handle_vr_switch_request, flow=window.trace)
            )
            window.request_normal_switch.connect(
                partial(self.handle_normal_switch_request, flow=window.trace)
            )

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
        flow=None,
    ):
        self._remember_recent_target_url(url)
        self._show_config_window(
            url,
            mode="default",
            smart_detect=smart_detect,
            playlist_flat=playlist_flat,
            target_tab=target_tab,
            preloaded_info=preloaded_info,
            flow=flow,
        )

    def show_vr_selection_dialog(
        self,
        url: str,
        smart_detect: bool = True,
        preloaded_info: dict | None = None,
        flow=None,
    ):
        self._remember_recent_target_url(url)
        self._show_config_window(
            url,
            mode="vr",
            vr_mode=True,
            smart_detect=smart_detect,
            preloaded_info=preloaded_info,
            flow=flow,
        )

    def _show_channel_dialog(self, url: str, target_tab: str = "all") -> None:
        """频道解析入口：规范化 URL 后，走播放列表 flat 解析路径，传递 target_tab。"""
        from ..youtube.youtube_service import YoutubeService

        # 始终传递 "all" 来获取纯净的 base_url，因为具体的 tab 会由 ChannelExtractWorker 拼接
        normalized = YoutubeService._normalize_channel_url(url, "all")
        self.show_selection_dialog(
            normalized, smart_detect=False, playlist_flat=True, target_tab=target_tab
        )

    def handle_vr_switch_request(self, url: str, preloaded_info: dict | None = None, flow=None):
        """响应智能检测的 VR 切换请求。

        preloaded_info 是切换前那一轮已完成解析的结果，仅用于新窗口的首屏预览；
        VR 格式必须由 android_vr 重新解析，不能沿用。

        `flow` 沿用旧窗口的操作链：对用户来说「粘贴链接 → 检测到 VR → 换窗口 → 下载」
        是一次操作，日志里不该断成两条。
        """
        logger.info(f"Switching to VR mode for URL: {url}")
        self.show_vr_selection_dialog(
            url, smart_detect=True, preloaded_info=preloaded_info, flow=flow
        )

    def handle_normal_switch_request(self, url: str, preloaded_info: dict | None = None, flow=None):
        """响应智能检测的普通模式切换请求"""
        logger.info(f"Switching to Normal mode for URL: {url}")
        self.show_selection_dialog(url, smart_detect=True, preloaded_info=preloaded_info, flow=flow)

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

    def add_tasks(self, tasks, flow=None):
        """添加下载任务到统一任务列表

        `flow` 是发起这批任务的那个配置窗口的操作链标识（由 `_show_config_window`
        用 `partial` 绑好）。`downloadRequested` 的签名保持 `Signal(list)` 不变 ——
        往信号里塞一个非 Qt 类型只会逼所有调用点跟着改。
        """
        logger.info(f"[DEBUG] Delegating {len(tasks)} tasks to Controller")
        if self.controller:
            created_workers = self.controller.handle_add_tasks(tasks, flow=flow)
            for worker, t_title, t_thumb in reversed(created_workers):
                self.task_page.add_task(worker, t_title, str(t_thumb) if t_thumb else "")
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
                self._quick_add_tooltip.setContent(
                    tr_text("成功添加 {0} 个任务", len(created_workers))
                )
                self._quick_add_tooltip.setState(True)
                self._quick_add_tooltip = None

            for worker, t_title, t_thumb in created_workers:
                self.task_page.add_task(worker, t_title, str(t_thumb) if t_thumb else "")
            self.switchTo(self.task_page)

        self.controller.handle_quick_add_tasks(
            urls, params, {"progress": on_progress, "error": on_error, "finished": on_finished}
        )

    def on_open_target_folder(self, row: int):
        """在资源管理器里定位该行的产物。**历史行同样可用**。

        路径优先级：`effective_output_path`（worker 的实时路径优先于 DB 快照）→
        worker 的 `_final_filepath` → opts 里的输出目录 → 全局下载目录。
        原实现在没有 worker 时直接 return，融合后那会让「打开文件夹」对绝大多数
        已完成行（都是分页补进来的历史行）失效。
        """
        row_obj = self.task_page.task_row(row)
        if not isinstance(row_obj, TaskRow):
            return
        worker = row_obj.worker

        out_file = row_obj.effective_output_path or getattr(worker, "_final_filepath", "")
        if out_file and os.path.exists(out_file):
            import subprocess

            if os.name == "nt":
                subprocess.run(["explorer", "/select,", os.path.normpath(out_file)])
            else:
                os.startfile(os.path.dirname(out_file))
            return

        # Fallback to output folder
        home_dir = ""
        if worker is not None:
            paths = getattr(worker, "opts", None) or {}
            home_dir = (paths.get("paths") or {}).get("home", "")
        if not home_dir and out_file:
            # 历史行没有 opts，但快照里的文件路径本身就带着目录 —— 文件被删了，
            # 目录通常还在。
            home_dir = os.path.dirname(out_file)
        if not home_dir:
            home_dir = config_manager.get("download_dir") or os.getcwd()
        if home_dir and os.path.exists(home_dir):
            os.startfile(home_dir)

    def _dispatch_remove(self, row_obj: TaskRow, force_delete_files: bool) -> None:
        """一行的删除派发：有 worker 走 worker 通道，历史行走快照通道。

        `controller.handle_remove_task` 在 `worker` 为空时直接 return —— 融合后
        列表里绝大多数终态行是分页补进来的历史行，不派发就等于「删不掉」。
        """
        if not self.controller:
            return
        if row_obj.worker is not None:
            self.controller.handle_remove_task(
                row_obj.worker, force_delete_files=force_delete_files
            )
        else:
            self.controller.handle_remove_snapshots(
                [(row_obj.db_id, row_obj.output_path)], force_delete_files=force_delete_files
            )

    def on_remove_task(self, row: int):
        row_obj = self.task_page.task_row(row)
        if not isinstance(row_obj, TaskRow):
            return
        worker = row_obj.worker

        try:
            state = row_obj.effective_state

            # 历史行永远不活跃：未完成态由 `load_unfinished_tasks()` 注入成活任务，
            # 分页只补终态，两边不交叉（见 `storage/task_db.py` 的状态划分）。
            is_active = state in ("running", "queued", "paused", "downloading")

            # ── 读取设置页的删除策略 ──
            raw_policy = config_manager.get("deletion_policy")
            policy = DeletionPolicy.from_config_str(raw_policy)

            # ── 快速通道：策略为 self.tr("仅移除记录") 且非活跃任务 ──
            if policy == DeletionPolicy.KEEP_FILES and not is_active:
                self._dispatch_remove(row_obj, False)
                self.task_page.remove_row(row)
                return

            # ── 快速通道：策略为 self.tr("彻底删除") 且非活跃任务 ──
            if policy == DeletionPolicy.DELETE_FILES and not is_active:
                self._dispatch_remove(row_obj, True)
                self.task_page.remove_row(row)
                return

            # ── 中途取消的活跃任务：必须强制清理缓存 ──
            if is_active:
                if policy in (DeletionPolicy.KEEP_FILES, DeletionPolicy.DELETE_FILES):
                    # 即使策略是保留文件，中途取消也必须清理 .part/.ytdl 缓存残骸
                    self._dispatch_remove(row_obj, True)
                    self.task_page.remove_row(row)
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

                self._dispatch_remove(row_obj, chk.isChecked())
                self.task_page.remove_row(row)
                return

            # ── 已完成/已出错任务：迅雷/IDM 风格双按钮弹窗 ──
            title = row_obj.effective_title or self.tr("删除任务")
            final_path = row_obj.effective_output_path or getattr(worker, "_final_filepath", "")
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

            self._dispatch_remove(row_obj, force_delete)
            self.task_page.remove_row(row)

        except Exception as e:
            logger.exception(f"Critical error in on_remove_task: {e}")
            try:
                self.task_page.remove_row(row)
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
        box = MessageBox(title, tr_text("即将删除 {0} 个缓存文件，是否继续？", len(paths)), self)
        return bool(box.exec())

    def _prompt_delete_source_files(self, paths: list[str], title: str) -> bool:
        box = MessageBox(title, tr_text("即将删除 {0} 个源文件，是否继续？", len(paths)), self)
        return bool(box.exec())

    def on_pause_resume_task(self, row: int):
        # 暂停/继续任务逻辑委托给 Controller
        row_obj = self.task_page.task_row(row)
        if not isinstance(row_obj, TaskRow) or not self.controller:
            return

        new_worker = self._start_or_toggle(row_obj)
        if new_worker:
            # 已结束的 QThread 不能重启，controller 会重建 worker → 必须换行绑定；
            # 历史行则是第一次拿到 worker（就地升级成活任务）。
            self.task_page.rebind_worker(row, new_worker)

    def _start_or_toggle(self, row_obj: TaskRow, notify: bool = True) -> object | None:
        """一行的「开始 / 暂停」派发；返回需要重新绑定到该行的新 worker（没有则 None）。

        历史行没有 worker，`handle_pause_resume_task` 会直接 return None ——
        对用户就是「点了播放键没反应」。这里改走快照重下：opts 从 `tasks` 表按
        `db_id` 现读，`restore_db_id` 复用同一个主键，所以是就地升级而不是多出一行。

        `notify=False` 给批量用：50 行都缺 opts 会弹 50 个 InfoBar，批量侧自己汇总成一条。
        """
        if not self.controller:
            return None
        if row_obj.worker is not None:
            return self.controller.handle_pause_resume_task(row_obj.worker)

        worker = self.controller.handle_start_snapshot(
            row_obj.db_id,
            row_obj.url,
            title=row_obj.effective_title,
            thumbnail=row_obj.effective_thumbnail,
        )
        if worker is None and notify:
            InfoBar.warning(
                title=self.tr("无法直接重新下载"),
                content=self.tr("这条记录缺少下载参数，请用右键菜单的「重新解析」重建任务"),
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
        return worker

    # === 批量动作：统一的作用域、确认与分块执行 ===

    # 分块节奏。启动每 tick 只处理 2 个 —— 每个都要新建 QThread 并写一次 SQLite；
    # 暂停 / 删除 / 清空轻一些，5 个。间隔 50ms 是留给事件循环处理 QThread 信号和重绘的窗口。
    # **不要调大这几个数**：分块不是为了「显得流畅」，而是为了避免批量写 SQLite 时
    # 长期阻塞主线程引发 Segfault。
    _CHUNK_START = 2
    _CHUNK_BULK = 5
    _CHUNK_INTERVAL_MS = 50

    def _run_chunked(self, items: list, chunk_size: int, handler, on_done=None) -> None:
        """把 items 切块交给 handler，块与块之间让出事件循环，全部处理完再调 on_done。

        所有批量动作（开始 / 暂停 / 删除 / 清空）都走这一个执行器。原来只有开始和删除分块，
        两个清空是同步一把梭的 —— 几百条记录时会长时间冻结 UI 并踩到同一个 SQLite 阻塞问题。

        `items` 本身不被消耗（内部复制一份），调用方可以继续拿它做事后反查。
        """
        pending = list(items)

        def process_chunk():
            if not pending:
                if on_done is not None:
                    on_done()
                return
            chunk = pending[:chunk_size]
            del pending[:chunk_size]
            handler(chunk)
            QTimer.singleShot(self._CHUNK_INTERVAL_MS, process_chunk)

        QTimer.singleShot(0, process_chunk)

    def _rows_at(self, rows: list[int]) -> list[TaskRow]:
        """**source** 行号 → 行对象列表（页面持有模型，这里只是转发）。"""
        return self.task_page.task_rows(rows)

    def _visible_source_rows(self) -> list[int]:
        """当前筛选可见的全部行，映射为 **source** 行号。

        全局动作（全部开始 / 全部暂停 / 两个清空）一律以此为作用域。原来两个清空直接扫
        source 全表，于是在「已完成」页签下点「清空全部任务」会把正在下载的任务一起取消 ——
        看到的和删掉的不是同一批东西。
        """
        return self.task_page.visible_source_rows()

    def _scope_text(self, count: int, from_selection: bool) -> str:
        """确认框里的「作用域」短语。

        同一个 handler 既服务右键菜单 / 批量条（作用域 = 选中集合），也服务顶部 CommandBar
        （作用域 = 当前筛选可见项），不写清楚就会出现「我只选了 3 个，怎么全没了」。
        """
        if from_selection:
            return self.tr("已选择的 {n} 项").format(n=count)
        return self.tr("当前筛选「{name}」的 {n} 项").format(
            name=self.task_page.current_filter_label(), n=count
        )

    def _confirm(self, title: str, content: str) -> bool:
        return bool(MessageBox(title, content, self).exec())

    def _remove_rows(self, row_objs: list[TaskRow], delete_files: bool = False) -> None:
        """分块移除一批行，全部落地之后再把对应行从模型里摘掉。

        每一块内部按来源分流：有 worker 的交给 `handle_batch_remove`（要停线程、清沙盒），
        历史行交给 `handle_remove_snapshots`（只删 DB 行 + 可选删文件）。
        混选是常态 —— 融合后「已完成」页签里既有本次会话刚下完的活任务，也有分页补进来的
        历史行，用户框选时不会区分。

        行号必须**事后反查**：分块执行期间新任务可能插到 row 0，事前记下的行号会整体平移。
        反查用 `id(row_obj)`，而 `row_objs` 列表在整个过程中一直持有强引用，所以 id 不会
        被回收复用（反查本身由页面的 `remove_row_objects` 负责）。
        """
        if not self.controller or not row_objs:
            return

        def handle_chunk(chunk: list[TaskRow]) -> None:
            live = [r.worker for r in chunk if r.worker is not None]
            snaps = [(r.db_id, r.output_path) for r in chunk if r.worker is None]
            if live:
                self.controller.handle_batch_remove(live, force_delete_files=delete_files)
            if snaps:
                self.controller.handle_remove_snapshots(snaps, force_delete_files=delete_files)

        self._run_chunked(
            row_objs,
            self._CHUNK_BULK,
            handle_chunk,
            on_done=lambda: self.task_page.remove_row_objects(row_objs),
        )

    def on_batch_start(self, rows: list[int]):
        """批量开始。`rows` 是 **source** 行号（页面侧已经从 proxy 映射过）。"""
        row_map = {}
        startable: list[TaskRow] = []
        for row in rows:
            row_obj = self.task_page.task_row(row)
            if not isinstance(row_obj, TaskRow):
                continue
            # 历史行也算 —— `error` / `cancelled` 的重下正是这个按钮的语义。
            # 跳过的三种：running / queued 已经在跑或在排队，completed 重下会覆盖
            # 用户已经拿到的文件（沿用 `handle_batch_start` 原有的跳过集合）。
            if row_obj.effective_state in ("running", "queued", "completed"):
                continue
            startable.append(row_obj)
            row_map[id(row_obj)] = row

        if not self.controller or not startable:
            return

        failed: list[TaskRow] = []

        def start_chunk(chunk: list[TaskRow]):
            # 已结束的 QThread 不能重启，controller 会**重建** worker → UI 必须换行绑定，
            # 否则那一行永远停在旧对象的终态上
            for row_obj in chunk:
                new_worker = self._start_or_toggle(row_obj, notify=False)
                if new_worker is None:
                    if row_obj.worker is None:
                        # 历史行缺 ydl_opts，重下不了 —— 逐条弹窗会刷屏，末尾汇总一条
                        failed.append(row_obj)
                    continue
                row = row_map.get(id(row_obj))
                if row is not None:
                    self.task_page.rebind_worker(row, new_worker)
            # 每块结束后推一次队列：`start_worker` 只在有空位时立刻起线程，
            # 其余进 `_pending_workers`。原 `handle_batch_start` 在批次末尾 pump 一次，
            # 分块之后改成每块一次（pump 本身是幂等的）。
            download_manager.pump()

        def report():
            if not failed:
                return
            InfoBar.warning(
                title=self.tr("{n} 项无法直接重新下载").format(n=len(failed)),
                content=self.tr("这些记录缺少下载参数，请用右键菜单的「重新解析」重建任务"),
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

        self._run_chunked(startable, self._CHUNK_START, start_chunk, on_done=report)

    def on_batch_downgrade(self, rows: list[int]):
        """降低画质重试。`rows` 是 **source** 行号。

        这是 `download_card._maybe_handle_format_unavailable` 的替代入口。原实现挂在
        worker 的错误回调上、由每张卡片自己弹窗；虚拟化之后没有卡片了，改成右键菜单里
        的显式动作 —— 顺带治好了原来的毛病：一批任务同时失败会**逐个**弹模态框。

        「手动调整」那一支不在这里重复实现：它就是菜单里的「重新解析」
        （`show_selection_dialog(url, smart_detect=True)`），能重挑格式也能换档位。
        单选时把它做成确认框的取消按钮，多选时那个入口没有意义（一次只能解析一个 url）。
        """
        row_map: dict[int, int] = {}
        row_objs: list[TaskRow] = []
        for row in rows:
            row_obj = self.task_page.task_row(row)
            if not isinstance(row_obj, TaskRow):
                continue
            # 只对 `error` 行开放。降档的前提是「片源没有这个严格档位所以直接失败了」，
            # 其余状态（暂停、排队、已完成）要么还有机会跑，要么已经拿到文件。
            if row_obj.effective_state != "error":
                continue
            row_objs.append(row_obj)
            row_map[id(row_obj)] = row

        if not self.controller or not row_objs:
            return

        single = len(row_objs) == 1
        box = MessageBox(
            self.tr("降低画质重试"),
            self.tr(
                "即将把{scope}的画质预设降低一档后重新下载。\n\n严格档位（如 1080p 严格）在片源没有该档位时会直接失败，降一档通常就能下成。"
            ).format(scope=self._scope_text(len(row_objs), from_selection=True)),
            self,
        )
        box.yesButton.setText(self.tr("自动降档重试"))
        # 取消键在单选时兼作「手动调整」的入口 —— 对齐 download_card 原来的两个按钮
        box.cancelButton.setText(self.tr("手动调整") if single else self.tr("取消"))
        if not box.exec():
            if single and row_objs[0].url:
                self.show_selection_dialog(row_objs[0].url, smart_detect=True)
            return

        skipped: list[str] = []
        done: list[int] = []

        def downgrade_chunk(chunk: list[TaskRow]):
            for row_obj in chunk:
                new_worker, reason, new_height = self.controller.handle_downgrade_quality(
                    row_obj.db_id,
                    row_obj.url,
                    title=row_obj.effective_title,
                    thumbnail=row_obj.effective_thumbnail,
                    worker=row_obj.worker,
                )
                if new_worker is None:
                    skipped.append(reason)
                    continue
                done.append(new_height)
                # 重建 worker 之后这一行必须换绑，否则它永远停在旧对象的终态上
                row = row_map.get(id(row_obj))
                if row is not None:
                    self.task_page.rebind_worker(row, new_worker)
            download_manager.pump()

        def report():
            if done:
                # 多选时各行原档位可能不同，降完也就不是同一个值 —— 去重后一起报
                heights = " / ".join(f"{h}p" for h in dict.fromkeys(sorted(done, reverse=True)))
                InfoBar.success(
                    title=self.tr("已降档重试 {n} 项").format(n=len(done)),
                    content=self.tr("新档位：{heights}").format(heights=heights),
                    duration=4000,
                    position=InfoBarPosition.TOP_RIGHT,
                    parent=self,
                )
            if not skipped:
                return
            if skipped.count("lowest") == len(skipped):
                # 全是「已经最低档」—— 这条提示照搬 download_card 的原文案
                InfoBar.warning(
                    title=self.tr("无法继续降档"),
                    content=self.tr("已是最低预设档位，建议用「重新解析」手动调整格式。"),
                    duration=4000,
                    position=InfoBarPosition.TOP_RIGHT,
                    parent=self,
                )
                return
            InfoBar.warning(
                title=self.tr("{n} 项无法降档").format(n=len(skipped)),
                content=self.tr(
                    "这些任务不是严格画质档位（或已是最低档），请用「重新解析」手动调整"
                ),
                duration=4000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )

        self._run_chunked(row_objs, self._CHUNK_START, downgrade_chunk, on_done=report)

    def on_batch_pause(self, rows: list[int]):
        # 历史行没有线程可暂停，直接过滤掉（不是错误，混选时是常态）
        workers = [r.worker for r in self._rows_at(rows) if r.worker is not None]
        if not self.controller or not workers:
            return
        self._run_chunked(workers, self._CHUNK_BULK, self.controller.handle_batch_pause)

    def on_batch_delete(self, rows: list[int], delete_files: bool = False):
        """删除选中任务。`rows` 是 **source** 行号。"""
        row_objs = self._rows_at(rows)
        if not row_objs:
            return

        scope = self._scope_text(len(row_objs), from_selection=True)
        if delete_files:
            content = self.tr("即将删除{scope}，并一并删除它们已下载的本地文件。\n此操作不可撤销。")
        else:
            content = self.tr("即将删除{scope}的任务记录。\n(不会删除本地文件)")
        if not self._confirm(self.tr("删除任务"), content.format(scope=scope)):
            return

        self._remove_rows(row_objs, delete_files=delete_files)

    def on_start_all(self):
        rows = self._visible_source_rows()
        if not rows:
            return
        self._notify_scope(self.tr("开始"), len(rows))
        self.on_batch_start(rows)

    def on_pause_all(self):
        rows = self._visible_source_rows()
        if not rows:
            return
        self._notify_scope(self.tr("暂停"), len(rows))
        self.on_batch_pause(rows)

    def _notify_scope(self, verb: str, count: int) -> None:
        """开始 / 暂停是非破坏性动作，不弹模态，但作用域仍然要说清楚。

        否则「全部开始」在筛选下只作用于可见项这件事完全不可见 —— 用户会以为它没生效。
        """
        InfoBar.info(
            title=self.tr("{verb}任务").format(verb=verb),
            content=self.tr("作用域：{scope}").format(
                scope=self._scope_text(count, from_selection=False)
            ),
            duration=2000,
            position=InfoBarPosition.TOP_RIGHT,
            parent=self,
        )

    def on_clear_completed(self):
        """清空**当前筛选下**的已完成 / 已失败 / 已取消记录。"""
        rows = self._visible_source_rows()
        clearable: list[TaskRow] = []
        n_completed = 0
        n_error = 0
        for row_obj in self._rows_at(rows):
            # 历史行是这里的**主力**：融合后「已完成」页签下绝大多数行都来自分页，
            # 按 worker 过滤等于「清空按钮对旧记录无效」。
            state = row_obj.effective_state
            if state == "completed":
                n_completed += 1
            elif state in ("error", "cancelled"):
                n_error += 1
            else:
                continue
            clearable.append(row_obj)

        if not clearable:
            InfoBar.info(
                title=self.tr("没有可清空的记录"),
                content=self.tr("当前筛选「{name}」下没有已完成或已失败的任务").format(
                    name=self.task_page.current_filter_label()
                ),
                duration=2000,
                position=InfoBarPosition.TOP_RIGHT,
                parent=self,
            )
            return

        parts = []
        if n_completed:
            parts.append(self.tr("{n} 个已完成").format(n=n_completed))
        if n_error:
            parts.append(self.tr("{n} 个已失败/已取消").format(n=n_error))

        content = self.tr(
            "作用域：{scope}。\n即将清空其中 {parts} 的任务记录。\n(不会删除本地文件)"
        )
        if not self._confirm(
            self.tr("清空记录"),
            content.format(
                scope=self._scope_text(len(rows), from_selection=False),
                parts="、".join(parts),
            ),
        ):
            return

        self._remove_rows(clearable, delete_files=False)

    def on_clear_all(self):
        """清空**当前筛选下**的全部任务。"""
        rows = self._visible_source_rows()
        row_objs = self._rows_at(rows)
        if not row_objs:
            return

        # 整条消息必须是**单个**字面量：pylupdate 对隐式拼接的相邻字符串提取不可靠，
        # 拆成两段有可能整条漏出 assets/locales/*.ts。
        content = self.tr(
            "作用域：{scope}。\n即将清空这些任务的记录，其中正在下载的会被一并取消。\n(不会删除本地文件)"
        )
        if not self._confirm(
            self.tr("清空全部任务"),
            content.format(scope=self._scope_text(len(row_objs), from_selection=False)),
        ):
            return

        self._remove_rows(row_objs, delete_files=False)

    def on_open_download_dir(self):
        # 打开默认下载目录
        path = config_manager.get("download_dir") or os.getcwd()
        if os.path.exists(path):
            os.startfile(path)

    def init_title_bar(self):
        """整理标题栏：右侧放「帮助」和「消息」按钮，左侧把图标和软件名撑开一点。

        `FluentTitleBar` 的布局是 `hBoxLayout = [iconLabel, titleLabel, stretch, vBoxLayout]`
        —— 最小化/最大化/关闭并不直接挂在 hBoxLayout 上，而是嵌在
        `vBoxLayout > buttonLayout` 里，所以 hBoxLayout.count() 恒为 4。
        按 `count() - 3` 算插入位置会得到索引 1，把按钮塞进窗口图标和标题之间
        （3.6.9 的症状）。

        也不能塞进 `buttonLayout`：它 `setAlignment(Qt.AlignTop)`，而系统按钮是 46x32、
        贴着 48px 标题栏的顶边，跟着它对齐会比标题文字高出 8px（看起来"太靠上"）。
        正确的位置是 `hBoxLayout` 里 `vBoxLayout` 之前 —— 那里有完整的 48px 高度，
        `AlignVCenter` 才能和标题文字落在同一条中线上。

        这里还顺手统一了三件事，否则五个按钮并排会明显"不是一套"：

        * 系统按钮改成垂直居中（见下面的 `vBoxLayout` stretch），不再高出 8px；
        * 帮助/铃铛的图标缩到 12px，去贴合系统按钮那 10px 的字形；
        * 左上角的图标和软件名往右挪，别贴着导航栏。
        """
        # 系统按钮的字形只有 10px：最小化是 `drawLine(18, 16, 28, 16)` 的 10px 横线，
        # 最大化是 `drawRect(18, 11, 10, 10)`，关闭是 close.svg 里 3.263/15.875 的 X
        # 渲染到 46x32 上约 9.5px。TransparentToolButton 默认 16px，并排放着大一号，
        # 这就是"格格不入"的来源。12px 与它们同一个重量级，又不至于让铃铛小得看不清。
        glyph_size = QSize(12, 12)

        # Parent MUST be titleBar to ensure correct z-order and event handling
        self.help_btn = TransparentToolButton(FluentIcon.HELP, self.titleBar)
        self.help_btn.setToolTip(self.tr("帮助中心"))
        self.help_btn.installEventFilter(
            ToolTipFilter(self.help_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.help_btn.clicked.connect(self.show_help_window)
        self.help_btn.setFixedSize(46, 32)
        self.help_btn.setIconSize(glyph_size)

        self.notif_btn = TransparentToolButton(FluentIcon.RINGER, self.titleBar)
        self.notif_btn.setToolTip(self.tr("消息中心"))
        self.notif_btn.installEventFilter(
            ToolTipFilter(self.notif_btn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.notif_btn.clicked.connect(self.show_notification_panel)
        self.notif_btn.setFixedSize(46, 32)
        self.notif_btn.setIconSize(glyph_size)

        # 系统按钮默认贴着标题栏顶边：`vBoxLayout = [buttonLayout, stretch]`，那个 stretch
        # 把 32px 高的按钮组顶到 48px 标题栏的最上面，于是它们比标题文字和帮助/铃铛高 8px。
        # 在最前面再插一个 stretch，按钮组就被夹在两个 stretch 中间，五个按钮同一条中线。
        v_box_layout = getattr(self.titleBar, "vBoxLayout", None)
        insert_stretch = getattr(v_box_layout, "insertStretch", None)
        if callable(insert_stretch):
            insert_stretch(0, 1)

        # 插进 hBoxLayout 的最后一项（承载系统按钮的 vBoxLayout）之前：
        # 前面有 stretch，所以会靠右；AlignVCenter 让它和标题文字同一条中线。
        layout = getattr(self.titleBar, "hBoxLayout", None) or self.titleBar.layout()

        # 顺手把左上角撑开：hBoxLayout 的 margins 和 spacing 默认都是 0，图标贴在
        # 窗口最左边缘、标题紧接着图标，挤成一团。而 `FluentWindow.resizeEvent` 已经把
        # 整个标题栏右移了 46px 给导航栏的返回按钮让位，图标就正好卡在导航栏边上。
        # 左边留 20px，图标与标题之间再留 12px。
        # 右边保持 0 —— 系统按钮必须贴着窗口右上角（Windows 的惯例，也便于点击）。
        icon_label = getattr(self.titleBar, "iconLabel", None)
        set_margins = getattr(layout, "setContentsMargins", None)
        index_of = getattr(layout, "indexOf", None)
        insert_spacing = getattr(layout, "insertSpacing", None)
        if icon_label is not None and callable(set_margins) and callable(index_of):
            set_margins(20, 0, 0, 0)
            icon_index = index_of(icon_label)
            if icon_index >= 0 and callable(insert_spacing):
                insert_spacing(icon_index + 1, 12)

        insert_widget = getattr(layout, "insertWidget", None)
        count = getattr(layout, "count", None)
        if callable(insert_widget) and callable(count):
            count_value = count()
            index = max(count_value - 1, 0) if isinstance(count_value, int) else 0
            align = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            insert_widget(index, self.help_btn, 0, align)
            insert_widget(index + 1, self.notif_btn, 0, align)
            # 和系统按钮组之间留一点视觉间隔
            if callable(insert_spacing):
                insert_spacing(index + 2, 6)

        # 未读徽章：必须在按钮进入布局之后创建，才能拿到正确 z-order
        self.notif_badge = InfoBadge.error(
            0, self.titleBar, target=self.notif_btn, position=TITLE_BAR_BADGE_POSITION
        )
        self.notif_badge.hide()

        from ..notification import notification_center

        notification_center.unread_count_changed.connect(self._on_unread_count_changed)

        # 初始化徽章
        self._on_unread_count_changed(notification_center.get_unread_count())

    def _on_unread_count_changed(self, count: int):
        if count > 0:
            self.notif_badge.setNum(count if count < 100 else 99)
            # 1 位数 → 2 位数时宽度会变，必须重新 adjustSize + 重新定位
            self.notif_badge.adjustSize()
            manager = getattr(self.notif_badge, "manager", None)
            if manager is not None:
                self.notif_badge.move(manager.position())
            self.notif_badge.show()
            self.notif_badge.raise_()
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
        view.detachRequested.connect(self.detach_notification_panel)
        self._notif_flyout = Flyout.make(
            view, self.notif_btn, self, aniType=FlyoutAnimationType.PULL_UP
        )

    def detach_notification_panel(self):
        """把消息列表甩进独立窗口。

        必须**先关浮窗再开窗口**：`Flyout` 是靠失焦关闭的，反过来的话新窗口一拿到
        焦点浮窗才消失，用户看到的是浮窗闪了一下 —— 分不清是打开了还是出错了。
        """
        from .notification_panel import NotificationWindow

        flyout = getattr(self, "_notif_flyout", None)
        if flyout is not None:
            try:
                flyout.close()
            except RuntimeError:
                pass
            self._notif_flyout = None

        NotificationWindow.show_singleton(self)

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

        # 注：Cookie 状态检查不在这里触发。它由 cookie_sentinel.startupHealthReady
        # 驱动（连接见 __init__），不再用 QTimer 猜静默刷新什么时候结束。

    def on_admin_mode_cookie_refresh(self):
        """管理员模式启动后自动刷新Cookie"""
        from ..auth.auth_service import AuthSourceType, auth_service
        from ..utils.logger import logger

        # 只在配置了浏览器来源时刷新
        if auth_service.current_source == AuthSourceType.NONE:
            log_text(logger, "info", "[AdminMode] 未配置Cookie来源，跳过自动刷新")
            return

        if auth_service.current_source == AuthSourceType.FILE:
            log_text(logger, "info", "[AdminMode] 手动文件模式，跳过自动刷新")
            return

        if auth_service.current_source == AuthSourceType.WEBVIEW2:
            log_text(logger, "info", "[AdminMode] 登录模式(WebView2)，跳过自动刷新（需要用户交互）")
            return

        browser_name = auth_service.current_source_display
        log_text(logger, "info", "[AdminMode] 以管理员身份自动刷新Cookie: {0}", browser_name)

        InfoBar.info(
            self.tr("管理员模式"),
            tr_text("正在以管理员权限提取 {0} Cookie...", browser_name),
            duration=3000,
            parent=self,
        )

        # 提取要读被锁的 DPAPI 数据库，秒级到十几秒不等。以前是主线程直接调 ——
        # 启动后窗口刚出来就假死，正是用户报的"开局卡住"之一。改走 QThread。
        from .components.common.cookie_refresh_worker import CookieRefreshWorker

        # platform=None：浏览器提取一次就能覆盖两个平台
        worker = CookieRefreshWorker(self)
        self._admin_cookie_worker = worker  # QThread 必须活到 finished

        def _on_done(success: bool, message: str, _needs_admin: bool) -> None:
            if success:
                InfoBar.info(
                    self.tr("Cookie提取成功"),
                    tr_text("已从 {0} 提取 Cookie（管理员权限）", browser_name),
                    duration=5000,
                    parent=self,
                )
                # 自动跳转到设置页显示结果
                QTimer.singleShot(1000, lambda: self.switchTo(self.settings_interface))
            else:
                InfoBar.warning(self.tr("Cookie提取失败"), message, duration=8000, parent=self)

        worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        worker.start()

    def check_cookie_status(self, health: dict | None = None) -> None:
        """
        Cookie 启动分级提醒 —— 由 `cookie_sentinel.startupHealthReady` 驱动。

        `health` 为 None 时现场采样一次，所以这个方法既能连信号也能手动调。

        分级规则（用户要求"静默 + 消息提醒"，启动路径上不弹任何模态框）：

        - `enabled=False` → **完全跳过**。只用 X 的用户不会再被 YouTube 的缺失状态
          误伤，这正是"开局始终弹出需要获取 cookie"的病根。
        - 缺失 / 失效 → `InfoBar.warning` + 「去刷新」。
        - 有效但闸门刚拒过新 Cookie → `InfoBar.info`，告诉用户刷新没生效、仍在用旧文件
          （2.2.1 弱回退机制的可见半边）。
        - 有效但 24 小时内过期 → `InfoBar.info` + 「去刷新」。
        - 有效且干净 → 只记日志，静默。

        每个平台最多一条横幅。
        """
        try:
            from ..auth.auth_service import PLATFORM_LABELS
            from ..auth.cookie_sentinel import cookie_sentinel

            if health is None:
                health = cookie_sentinel.get_startup_health()

            for platform, info in health.items():
                label = PLATFORM_LABELS.get(platform, platform)

                if not info.get("enabled"):
                    log_text(logger, "debug", "[MainWindow] {0} 未启用 Cookie，跳过启动提醒", label)
                    continue

                commit_warning = info.get("commit_warning")

                if not info.get("exists") or not info.get("valid"):
                    # 闸门的拒绝原因比"Cookie 无效"更具体，优先展示
                    reason = commit_warning or info.get("reason") or self.tr("Cookie 无效")
                    log_text(
                        logger, "warning", "[MainWindow] {0} Cookie 不可用: {1}", label, reason
                    )
                    self._show_cookie_health_tip(platform, label, reason, is_warning=True)
                elif commit_warning:
                    log_text(
                        logger,
                        "warning",
                        "[MainWindow] {0} 新 Cookie 被拒绝，仍在使用旧文件",
                        label,
                    )
                    self._show_cookie_health_tip(
                        platform,
                        label,
                        self.tr("新 Cookie 不可用，仍在使用旧文件。原因：") + commit_warning,
                        is_warning=False,
                    )
                elif info.get("expiring_soon"):
                    self._show_cookie_health_tip(
                        platform, label, self._format_expiry_hint(info), is_warning=False
                    )
                else:
                    log_text(logger, "info", "[MainWindow] {0} Cookie 有效，启动静默", label)

        except Exception as e:
            log_text(logger, "error", "[MainWindow] Cookie 状态检查失败: {0}", e)

    def _format_expiry_hint(self, info: dict) -> str:
        """把 expiry_seconds 说成人话。拿不到具体秒数时给个不撒谎的兜底。"""
        remaining = info.get("expiry_seconds")
        if not remaining or remaining <= 0:
            return self.tr("Cookie 即将过期，建议尽快刷新")

        hours = int(remaining // 3600)
        if hours >= 1:
            return self.tr("Cookie 将在约 {} 小时后过期，建议提前刷新").format(hours)
        return self.tr("Cookie 将在约 {} 分钟后过期，建议立即刷新").format(
            max(1, int(remaining // 60))
        )

    def _show_cookie_health_tip(
        self, platform: str, label: str, content: str, is_warning: bool
    ) -> None:
        """一个平台一条 Cookie 横幅，带「去刷新」出口。"""
        title = (
            self.tr("{} Cookie 需要处理").format(label)
            if is_warning
            else self.tr("{} Cookie 提醒").format(label)
        )
        # 先加入按钮再显示，让堆叠管理器按完整高度计算后续卡片的位置。
        bar = InfoBar(
            icon=InfoBarIcon.WARNING if is_warning else InfoBarIcon.INFORMATION,
            title=title,
            content=content,
            # 竖排：这些原因文本往往一两句话，横排会被截在第一行
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            # 比默认 1s 长得多：这是用户唯一能知道"为什么下载会失败"的地方
            duration=12000 if is_warning else 8000,
            parent=self,
        )

        def go_refresh() -> None:
            bar.close()
            self.switchTo(self.settings_interface)

        btn = PushButton(self.tr("去刷新"))
        btn.clicked.connect(go_refresh)
        bar.addWidget(btn)
        bar.show()

    def on_worker_warning(self, warn_data: dict) -> None:
        """任务**成功**了但有该说的话（目前只有字幕三码）：只弹 InfoBar。

        刻意不复用 `on_worker_error`：那条路是 `WorkerErrorDialog.exec()` 模态框，
        把"视频已经下好了、只是少一个 .vtt"升级成一次强制打断，比原来的静默失败更烦人。
        载荷格式与 `worker_error` 相同（都是 `Diagnosis.to_dict()`）。
        """
        title = warn_data.get("user_title") or self.tr("字幕未完全下载")
        content = warn_data.get("user_message") or ""
        bar = InfoBar.warning(
            title=title,
            content=content,
            # 竖排：这段正文有两三句，横排会被 TextWrap 截在第一行
            orient=Qt.Orientation.Vertical,
            isClosable=True,
            position=InfoBarPosition.TOP_RIGHT,
            # 12s 而非默认 1s：这条提示是用户唯一能知道"为什么没有字幕"的地方
            duration=12000,
            parent=self,
        )

        fix_action = warn_data.get("fix_action")
        if not fix_action:
            return

        def run_fix() -> None:
            from fluentytdl.ui.components.settings.fix_registry import execute_fix_action

            bar.close()
            execute_fix_action(fix_action, self)

        btn = PushButton(warn_data.get("recovery_hint") or self.tr("去处理"))
        btn.clicked.connect(run_fix)
        bar.addWidget(btn)

    def on_worker_error(self, err_data: dict) -> None:
        """
        处理后台下载任务发出的错误（支持重试所有挂起任务）
        """
        logger.debug("on_worker_error: code={}", err_data.get("code"))
        if getattr(self, "_worker_error_dialog_showing", False):
            log_text(logger, "debug", "WorkerErrorDialog 已在显示，跳过本次")
            return

        self._worker_error_dialog_showing = True
        try:
            from fluentytdl.ui.components.dialogs.worker_error_dialog import WorkerErrorDialog

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
                        self.tr("已恢复 {0} 个挂起的任务").format(count),
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

            def handle_fix(action: str) -> None:
                """通用修复动作（启用 POT 引擎、安装 JS Runtime、检查代理……）。

                和 `on_worker_warning` 里那条 InfoBar 走同一个 `execute_fix_action`，
                按钮文案由 `recovery_hint` 提供，不在这里重写。
                """
                if not action:
                    return
                from fluentytdl.ui.components.settings.fix_registry import execute_fix_action

                execute_fix_action(action, self)

            dlg.retry_all_requested.connect(handle_retry_all)
            dlg.go_settings_requested.connect(handle_go_settings)
            dlg.fetch_cookie_requested.connect(handle_fetch_cookie)
            dlg.update_ytdlp_requested.connect(handle_update_ytdlp)
            dlg.fix_requested.connect(handle_fix)

            dlg.exec()
        except Exception as e:
            log_text(logger, "error", "[MainWindow] on_worker_error 异常: {0}", e)
            logger.exception(e)
        finally:
            self._worker_error_dialog_showing = False
