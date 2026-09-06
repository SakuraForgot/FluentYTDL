"""消息中心（标题栏铃铛）的视图层。

四个东西，一份列表实现：

- `NotificationCard` —— 单条通知；
- `NotificationListWidget` —— 列表本体（滚动区 + 空态 + 订阅刷新）；
- `NotificationFlyoutView` —— 铃铛点开的浮窗，**外观和行为保持原样**；
- `NotificationWindow` —— 同一份列表的独立窗口版。

浮窗和窗口两个都留着，因为它们回答的不是同一个问题。浮窗贴在铃铛底下、宽 360、
点窗口外任何地方就消失 —— 这正好是"有几条新消息？"该有的代价。但"二十条下载错误
一条条读完，一边读一边去任务列表点重试"在浮窗里做不到：手一点出去它就没了。
所以浮窗右上角多一个按钮，把同一份列表甩进一个能拖动、能改大小、不会被点掉的窗口。

**深色模式**：这一屏原先所有文字都是 `label.setStyleSheet("font-weight: bold;")`
标记未读的。`FluentLabelBase._init()` 是靠 `setTextColor()` → `setCustomStyleSheet()`
把 `color:` 塞进自己那张样式表里的，而 `setStyleSheet()` 把整张表换掉了 —— 标签于是
退回 Qt 调色板的默认前景色（黑），深色模式下未读通知就是黑底黑字。这里的规矩是：
**字重走 `setFont(getFont(...))`，颜色走 `setTextColor(浅, 深)`，永远不碰
`setStyleSheet()`**。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import QFrame, QHBoxLayout, QVBoxLayout, QWidget
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    CardWidget,
    FluentIcon,
    FluentIconBase,
    FlyoutViewBase,
    IconWidget,
    InfoBarIcon,
    PushButton,
    ScrollArea,
    StrongBodyLabel,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
    getFont,
    themeColor,
)

from ..notification import Notification, notification_center
from .components.common.standalone_window import StandaloneWindow

#: severity → (浅色主题下的字色, 深色主题下的字色)。
#:
#: 一个值不够用：`#D32F2F` 那种深红在 `#202020` 的底上和正文几乎一个亮度，而深色模式
#: 要的浅红放到白底上又发飘。取值与 `components/common/event_timeline._level_color()`
#: 的 ERROR / WARNING 两档一致 —— 同一件事在时间线和消息中心里不该是两种红。
_SEVERITY_COLORS: dict[str, tuple[str, str]] = {
    "critical": ("#C62828", "#FF8A80"),
    "warning": ("#A35B00", "#FFB74D"),
}

#: 正文/时间这类次要文字的深浅两档（CLAUDE.md §3 规定的那一对）。
_SECONDARY_COLORS = (QColor(96, 96, 96), QColor(210, 210, 210))

#: 标题在没有 severity 颜色时的深浅两档。
_TITLE_COLORS = (QColor(0, 0, 0), QColor(255, 255, 255))


class UnreadDot(QWidget):
    """未读标记的小圆点。

    单靠加粗标记未读是不够的：加粗只有和相邻的已读卡片对比才读得出来，一屏全是未读
    或全是已读时等于没有标记。圆点用 `themeColor()`，在两种主题下都是这一屏最亮的
    一点颜色，而且**每次重绘都重新取**，所以切主题不需要额外刷新。
    """

    SIZE = 8

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(themeColor())
        painter.drawEllipse(self.rect())


class NotificationCard(CardWidget):
    """单条通知卡片。

    底改成 `CardWidget`：原先是 `QFrame` + 一段写死 `rgba(0, 0, 0, 0.05)` 的样式表，
    那个边框和悬停底色是照浅色主题调的，深色模式下在 `#282828` 上完全看不见。
    `CardWidget` 自己按 `isDarkTheme()` 画圆角底和边框，切主题不用管。
    """

    def __init__(self, notif: Notification, parent=None):
        super().__init__(parent=parent)
        self.notif = notif
        self.setBorderRadius(6)

        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(12, 12, 12, 12)
        self.vBoxLayout.setSpacing(8)

        # 头部：未读点 + 图标 + 标题 + 时间 + 删除
        self.headerLayout = QHBoxLayout()
        self.headerLayout.setContentsMargins(0, 0, 0, 0)
        self.headerLayout.setSpacing(6)

        self.unreadDot = UnreadDot(self)
        self.unreadDot.setVisible(not notif.is_read)

        self.iconWidget = IconWidget(self._severity_icon(), self)
        self.iconWidget.setFixedSize(16, 16)

        self.titleLabel = StrongBodyLabel(notif.title, self)
        self.titleLabel.setWordWrap(False)

        dt = datetime.fromtimestamp(notif.timestamp)
        self.timeLabel = CaptionLabel(dt.strftime("%m-%d %H:%M"), self)

        self.deleteBtn = TransparentToolButton(FluentIcon.CLOSE, self)
        self.deleteBtn.setFixedSize(24, 24)
        self.deleteBtn.setIconSize(self.deleteBtn.iconSize() * 0.8)
        self.deleteBtn.clicked.connect(self._on_delete)

        self.headerLayout.addWidget(self.unreadDot)
        self.headerLayout.addWidget(self.iconWidget)
        self.headerLayout.addWidget(self.titleLabel, 1)
        self.headerLayout.addWidget(self.timeLabel)
        self.headerLayout.addWidget(self.deleteBtn)

        # 内容
        self.msgLabel = CaptionLabel(notif.message, self)
        self.msgLabel.setWordWrap(True)

        self.vBoxLayout.addLayout(self.headerLayout)
        self.vBoxLayout.addWidget(self.msgLabel)

        self._apply_text_style()

    def _severity_icon(self) -> FluentIconBase:
        """severity → 图标。

        走 `InfoBarIcon` 而不是 `FluentIcon`，有两个理由：

        1. **`FluentIcon.ERROR` 根本不存在**。原来那一行是 `FluentIcon.ERROR if ... else
           FluentIcon.INFO`，只要库里存着一条 `severity="critical"` 的通知，构造这张卡片
           就抛 `AttributeError`，整个铃铃面板一条都显示不出来。
        2. `InfoBarIcon.path()` 按 `isDarkTheme()` 在 `_light` / `_dark` 两版 svg 之间切，
           而 `IconWidget.paintEvent` 是在绘制时才解析路径的 —— 也就是说切主题不用重建
           卡片，图标自己就换了。`FluentIcon` 那套单色图标在深色底上会糊掉。

        `warning` 原先和 `info` 共用一个圆圈 i，这里分出了独立的黄色三角。
        """
        if self.notif.severity == "critical":
            return InfoBarIcon.ERROR
        if self.notif.severity == "warning":
            return InfoBarIcon.WARNING
        return InfoBarIcon.INFORMATION

    def _apply_text_style(self) -> None:
        """标题/正文/时间的字色与字重。

        两条硬规矩（本模块 docstring 里那个 bug 的两半）：

        1. **颜色只走 `setTextColor(浅, 深)`**。它内部用 `setCustomStyleSheet()`，
           把 qss 存进 `lightCustomQss` / `darkCustomQss` 动态属性，切主题时由
           `CustomStyleSheetWatcher` 重新追加 —— 也就是说这条颜色自己会跟着主题走。
           而 `setStyleSheet("color: ...")` 会被 `styleSheetManager` 在第一次切主题时
           整片重写掉。
        2. **字重只走 `setFont(getFont(size, weight))`**，绝不用样式表里的
           `font-weight` —— 那一句会连带把 `color:` 一起顶掉，正是原来的 bug。
           `getFont()` 走 `setPixelSize`，与 `StrongBodyLabel` / `CaptionLabel` 自己的
           默认字体同一套度量，混用磅值会让行高不齐。

        已读/未读**不改字色**，只改圆点和标题字重。把已读压暗过一次就得回答"暗到
        什么程度还看得见"，而这个问题在两种主题下的答案不一样 —— 不如不问。
        """
        severity = _SEVERITY_COLORS.get(self.notif.severity)
        if severity is not None:
            self.titleLabel.setTextColor(QColor(severity[0]), QColor(severity[1]))
        else:
            self.titleLabel.setTextColor(*_TITLE_COLORS)

        weight = QFont.Weight.Bold if not self.notif.is_read else QFont.Weight.DemiBold
        self.titleLabel.setFont(getFont(14, weight))

        self.msgLabel.setTextColor(*_SECONDARY_COLORS)
        self.timeLabel.setTextColor(*_SECONDARY_COLORS)

    def _on_delete(self):
        notification_center.delete_notification(self.notif.id)

    def mousePressEvent(self, event):
        if not self.notif.is_read:
            notification_center.mark_as_read(self.notif.id)
        super().mousePressEvent(event)


class NotificationListWidget(QWidget):
    """通知列表本体：滚动区 + 空态 + 自动刷新。

    浮窗和独立窗口共用它。抽出来时刻意把 `setFixedHeight()` 那段留在浮窗里 ——
    "这个列表该多高"是容器的事（浮窗按条数贴合、窗口跟着用户拖），不是列表的事。
    """

    #: 当前渲染出来的条数。容器拿它决定自己多高 / 要不要显示"全部已读"。
    countChanged = Signal(int)

    MAX_ITEMS = 50

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self._count = 0

        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(0, 0, 0, 0)
        self.vBoxLayout.setSpacing(0)

        self.scrollArea = ScrollArea(self)
        self.scrollArea.setWidgetResizable(True)
        self.scrollArea.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scrollArea.setFrameShape(QFrame.Shape.NoFrame)
        self.scrollArea.setStyleSheet("background: transparent;")

        self.scrollWidget = QWidget()
        self.scrollLayout = QVBoxLayout(self.scrollWidget)
        self.scrollLayout.setContentsMargins(0, 0, 8, 0)
        self.scrollLayout.setSpacing(8)
        self.scrollLayout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scrollArea.setWidget(self.scrollWidget)
        self.vBoxLayout.addWidget(self.scrollArea)

        self.emptyLabel = BodyLabel(self.tr("暂无通知"), self)
        self.emptyLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.emptyLabel.setTextColor(*_SECONDARY_COLORS)
        self.vBoxLayout.addWidget(self.emptyLabel)
        self.emptyLabel.hide()

        # 合并刷新。一次批量失败会连推十条通知，每条都触发一次
        # `notification_updated` —— 直接重载就是把整份列表重建十次。用一个属于自己的
        # `QTimer` 而不是 `QTimer.singleShot(0, self.reload)`：控件销毁时定时器跟着死，
        # 不会有一发已经排好队的回调打到已经被回收的 C++ 对象上。
        self._reloadTimer = QTimer(self)
        self._reloadTimer.setSingleShot(True)
        self._reloadTimer.timeout.connect(self.reload)

        self.reload()

        notification_center.notification_added.connect(self._on_update)
        notification_center.notification_updated.connect(self._on_update)

    def count(self) -> int:
        """最近一次渲染出来的条数。"""
        return self._count

    def reload(self):
        """按数据库现状重建整份列表。"""
        while self.scrollLayout.count():
            item = self.scrollLayout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        notifs = notification_center.get_all(limit=self.MAX_ITEMS)
        self._count = len(notifs)

        if not notifs:
            self.scrollArea.hide()
            self.emptyLabel.show()
        else:
            self.emptyLabel.hide()
            self.scrollArea.show()
            for notif in notifs:
                self.scrollLayout.addWidget(NotificationCard(notif, self.scrollWidget))

        self.countChanged.emit(self._count)

    def _on_update(self, *args):
        self._reloadTimer.start(0)


class NotificationFlyoutView(FlyoutViewBase):
    """通知中心浮窗（铃铛点开的那个，外观保持原样）"""

    #: 请求把这份列表甩到独立窗口里。由主窗口接 —— 只有它握着 `Flyout` 的句柄，
    #: 要先把浮窗关掉再开窗口，否则新窗口一拿到焦点，浮窗就自己消失，看着像闪了一下。
    detachRequested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent=parent)
        self.vBoxLayout = QVBoxLayout(self)
        self.vBoxLayout.setContentsMargins(16, 16, 16, 16)
        self.vBoxLayout.setSpacing(12)
        self.setFixedWidth(360)

        # 头部
        self.headerLayout = QHBoxLayout()
        self.titleLabel = SubtitleLabel(self.tr("消息中心"), self)
        # 不要 `font = self.titleLabel.font(); font.setPixelSize(16)`：`SubtitleLabel`
        # 默认是 `getFont(20, DemiBold)`，改磅值/字号会和同屏其它标签的度量对不上。
        self.titleLabel.setFont(getFont(16, QFont.Weight.DemiBold))

        self.detachBtn = TransparentToolButton(FluentIcon.BACK_TO_WINDOW, self)
        self.detachBtn.setFixedSize(28, 28)
        self.detachBtn.setToolTip(self.tr("在独立窗口中打开"))
        self.detachBtn.installEventFilter(
            ToolTipFilter(self.detachBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.detachBtn.clicked.connect(self.detachRequested)

        self.clearAllBtn = PushButton(self.tr("全部已读"), self)
        self.clearAllBtn.setFixedSize(80, 28)
        self.clearAllBtn.clicked.connect(notification_center.mark_all_as_read)

        self.headerLayout.addWidget(self.titleLabel)
        self.headerLayout.addStretch(1)
        self.headerLayout.addWidget(self.detachBtn)
        self.headerLayout.addWidget(self.clearAllBtn)

        self.vBoxLayout.addLayout(self.headerLayout)

        self.listWidget = NotificationListWidget(self)
        self.listWidget.countChanged.connect(self._fit_height)
        self.vBoxLayout.addWidget(self.listWidget)

        self._fit_height(self.listWidget.count())

    def _fit_height(self, count: int) -> None:
        """按条数贴合高度。

        `isVisible()` 那一层保留：浮窗显示之后 `Flyout` 已经按当时的 sizeHint 摆好了
        位置，这时候改高度会让它从铃铛底下错开。
        """
        if self.isVisible():
            return
        self.setFixedHeight(120 if count == 0 else min(450, 80 + count * 80))


class NotificationWindow(StandaloneWindow):
    """消息中心的独立窗口版。

    和浮窗共用 `NotificationListWidget`，区别只在容器：这个能拖、能改大小、点别处
    不会消失，所以能一条条读完再去操作主窗口。
    """

    #: 比基类窄：这一屏是一列卡片，宽了只会让每张卡片中间空出一大片。
    SIZE_RATIO = 0.5
    SIZE_BOUNDS = ((420, 560), (420, 820))
    MIN_SIZE = (380, 320)

    def __init__(self, anchor: QWidget | None = None):
        super().__init__(anchor=anchor)
        self.setWindowTitle(self.tr("消息中心"))

        self.titleLabel = SubtitleLabel(self.tr("消息中心"), self)

        self.clearAllBtn = PushButton(self.tr("全部已读"), self)
        self.clearAllBtn.setFixedHeight(28)
        self.clearAllBtn.clicked.connect(notification_center.mark_all_as_read)

        self.headerLayout = QHBoxLayout()
        self.headerLayout.setContentsMargins(0, 0, 0, 0)
        self.headerLayout.addWidget(self.titleLabel)
        self.headerLayout.addStretch(1)
        self.headerLayout.addWidget(self.clearAllBtn)
        self.viewLayout.addLayout(self.headerLayout)

        self.listWidget = NotificationListWidget(self)
        self.viewLayout.addWidget(self.listWidget, 1)
