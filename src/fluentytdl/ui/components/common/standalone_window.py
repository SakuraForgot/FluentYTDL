"""独立非模态窗口的公共外壳。

项目里有几个界面本来是「对话框」，但它们的用途都是**对照着主窗口看**：日志查看器要
盯着某个任务的进度条对时间，消息中心要一条条读完二十个错误再去点重试。遮罩对话框
（`MaskDialogBase` + `exec()`）把主窗口整片盖住并阻塞事件循环，恰好把"对照"变成了
"轮流看"；浮窗（`Flyout`）更极端，点窗口外任何地方就消失。

这个基类提供的就是那几件把 `qframelesswindow.FramelessWindow` 变成一个"能用的窗口"
所必需的、且每个子类都一样的杂活：

1. **跟随主题的标题栏** —— `qframelesswindow.TitleBar` 的按钮色写死为黑，深色模式下
   最小化/关闭是黑底黑图标（见 `ThemedTitleBar`）；
2. **跟随主题的窗口底色** —— `FramelessWindow` 是裸 `QWidget`，不接主题就永远是系统
   默认灰，深色模式下和内容一起糊成一片；
3. **开在该开的地方** —— 以主窗口为参照居中、按比例定尺寸，再夹回屏幕可用区。
   钉死尺寸的做法在 1366 宽的笔记本和 4K 屏上不可能同时合适；
4. **单例复现** —— 再点一次入口是把已经开着的那扇窗**提到前面**，而不是叠第二扇。

子类只管往 `self.viewLayout` 里塞内容。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from qfluentwidgets import isDarkTheme, qconfig
from qframelesswindow import FramelessWindow

from ....utils.icons import load_app_icon
from .themed_title_bar import ThemedTitleBar


def find_main_window() -> QWidget | None:
    """找主窗口。

    不走 `parent()`：这些窗口刻意以 `parent=None` 构造（否则拿不到自己的任务栏条目，
    而且会被 Qt 永远压在主窗口上面）。按 `objectName` 和类名两条都认 —— 测试里构造的
    替身通常两者只有一个。
    """
    for widget in QApplication.topLevelWidgets():
        if widget.objectName() == "MainWindow" or type(widget).__name__ == "MainWindow":
            return widget
    return None


class StandaloneWindow(FramelessWindow):
    """独立、非模态、跟随主题的无边框窗口基类。"""

    #: 初始尺寸相对参照窗口的占比，以及 `((宽下限, 宽上限), (高下限, 高上限))`。
    SIZE_RATIO = 0.86
    SIZE_BOUNDS = ((760, 1500), (520, 980))

    #: 用户能手动缩到的下限。比 `SIZE_BOUNDS` 的下限更小是故意的：那是"我替你选的
    #: 初始大小"，这才是"再小就没法用了"。
    MIN_SIZE = (480, 360)

    #: 内容边距。顶部必须大于标题栏高度（32px）—— 标题栏不进布局，是由
    #: `FramelessWindow.resizeEvent` 直接摆在 (0, 0) 的，边距就是给它留的位置。
    CONTENT_MARGINS = (20, 44, 20, 20)

    #: 每个**子类**各自的活实例。基类持有这张表是为了 `show_singleton()` 能写一次
    #: 就够，键是具体子类，所以日志窗口和消息窗口互不影响。
    _instances: dict[type, StandaloneWindow] = {}

    def __init__(self, title: str = "", anchor: QWidget | None = None):
        # `parent=None`：独立窗口才有自己的任务栏条目，也才不会被 Qt 当成子窗口永远
        # 压在主窗口上面（那样就没法把它拖到第二块屏上并排放）。`anchor` 只用来算初始
        # 位置和尺寸，不建立父子关系。
        super().__init__(parent=None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        # 底色样式表用 `#objectName` 选择器下发：类型选择器要写死类名，在基类里就得靠
        # `type(self).__name__` 拼，而 objectName 是每个实例自己的，不会随继承跑偏。
        self.setObjectName(type(self).__name__)

        self.setTitleBar(ThemedTitleBar(self))

        if title:
            self.setWindowTitle(title)
        # 素材缺失时不要设空 QIcon —— 那会把从 QApplication 继承来的图标一并清掉，
        # 任务栏上就成了一片空白（和主窗口同一处坑，见 `reimagined_main_window`）。
        icon = load_app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self.setMinimumSize(*self.MIN_SIZE)

        self.viewLayout = QVBoxLayout(self)
        self.viewLayout.setContentsMargins(*self.CONTENT_MARGINS)
        self.viewLayout.setSpacing(8)

        self._apply_initial_geometry(anchor)

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()
        self.titleBar.raise_()

    # ── 单例 ────────────────────────────────────────────────

    @classmethod
    def show_singleton(cls, anchor: QWidget | None = None) -> StandaloneWindow:
        """开这扇窗；已经开着就提到前面。

        非模态窗口没有 `exec()` 挡着，入口按钮可以被连点 —— 少了这一层，第二次点会
        再造一扇一模一样的窗口叠在原处，而两扇日志窗口各自订阅一份日志信号。
        """
        existing = StandaloneWindow._instances.get(cls)
        if existing is not None:
            try:
                if existing.isMinimized():
                    existing.showNormal()
                else:
                    existing.show()
                existing.raise_()
                existing.activateWindow()
                return existing
            except RuntimeError:
                # C++ 侧已经被回收（`WA_DeleteOnClose` + `destroyed` 还没跑到）
                StandaloneWindow._instances.pop(cls, None)

        window = cls(anchor=anchor)
        StandaloneWindow._instances[cls] = window
        window.destroyed.connect(lambda: StandaloneWindow._instances.pop(cls, None))
        window.show()
        window.activateWindow()
        return window

    @classmethod
    def singleton(cls) -> StandaloneWindow | None:
        """当前活着的那个实例（没有则 None）。测试和"关掉它"的调用方用。"""
        return StandaloneWindow._instances.get(cls)

    # ── 几何 ────────────────────────────────────────────────

    @classmethod
    def _initial_size(cls, reference: QSize) -> tuple[int, int]:
        """按参照尺寸的比例算，再夹进 `SIZE_BOUNDS`。

        上限存在的理由：4K 屏上铺满 86% 会让一行长到要转头才读得完；
        下限是"再小内容就排不开"。一个钉死的尺寸不可能同时适合两头。
        """
        (w_min, w_max), (h_min, h_max) = cls.SIZE_BOUNDS
        width = max(w_min, min(w_max, int(reference.width() * cls.SIZE_RATIO)))
        height = max(h_min, min(h_max, int(reference.height() * cls.SIZE_RATIO)))
        return width, height

    @staticmethod
    def _anchor_geometry(anchor: QWidget | None) -> QRect:
        """定尺寸/位置的参照系：调用方所在的窗口 → 主窗口 → 主屏可用区。"""
        candidates = [anchor.window() if anchor is not None else None, find_main_window()]
        for candidate in candidates:
            if candidate is None:
                continue
            geo = candidate.geometry()
            if geo.width() > 0 and geo.height() > 0:
                return geo
        screen = QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen is not None else QRect(0, 0, 1024, 768)

    def _apply_initial_geometry(self, anchor: QWidget | None) -> None:
        """居中到参照窗口，然后整体夹回屏幕可用区。

        夹这一步不是保险丝：主窗口可以横跨两块屏、也可以有一半在屏幕外，直接照它的
        中心摆会把新窗口开到看不见的地方 —— 而它是非模态的，用户既看不见也不知道
        为什么点了没反应。
        """
        reference = self._anchor_geometry(anchor)
        screen = None
        if anchor is not None:
            screen = anchor.screen()
        screen = screen or self.screen() or QGuiApplication.primaryScreen()
        avail = screen.availableGeometry() if screen is not None else reference

        width, height = self._initial_size(reference.size())
        width = min(width, avail.width())
        height = min(height, avail.height())

        x = min(max(reference.center().x() - width // 2, avail.left()), avail.right() - width + 1)
        y = min(max(reference.center().y() - height // 2, avail.top()), avail.bottom() - height + 1)
        self.setGeometry(x, y, width, height)

    # ── 主题 ────────────────────────────────────────────────

    def _background_color(self) -> QColor:
        """窗口底色。取值与 `DownloadConfigWindow` 一致 —— 项目里的独立窗口只有一种底。"""
        return QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)

    def _update_style(self) -> None:
        color = self._background_color()
        self.setStyleSheet(f"#{self.objectName()} {{ background-color: {color.name()}; }}")
        # `ThemedTitleBar` 注册进了 `styleSheetManager`，切主题时本该自动重下发；
        # 这一句是给"主题在窗口构造之前就换过"的情形补的，代价一次 qss 应用。
        if hasattr(self.titleBar, "updateStyle"):
            self.titleBar.updateStyle()

    # ── 事件 ────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        # 标题栏不在布局里，z-order 只按创建顺序排 —— 子类在基类 `__init__` 之后建的
        # 控件都排在它上面。内容边距留了 44px 所以通常不重叠，这一句管的是子类自己
        # 往顶部塞了东西的情形。
        self.titleBar.raise_()

    def keyPressEvent(self, event):
        """Esc 关窗。

        这些窗口是从对话框改过来的，用户的手指记着 Esc；而它们全都是"只读地看"，
        没有会被 Esc 丢掉的未保存状态。
        """
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event):
        # 在这里就摘掉，而不是只靠 `destroyed`：`WA_DeleteOnClose` 走的是
        # `deleteLater()`，要等下一轮事件循环才真销毁，中间再点一次入口会拿到一个
        # 正在等死的窗口。
        if StandaloneWindow._instances.get(type(self)) is self:
            StandaloneWindow._instances.pop(type(self), None)
        super().closeEvent(event)
