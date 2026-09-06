from __future__ import annotations

from PySide6.QtCore import QPropertyAnimation, QRect, QSize
from qfluentwidgets import NavigationInterface
from qfluentwidgets.components.navigation.navigation_panel import NavigationPanel

from ....utils.logger import logger

#: 上游 ``NavigationPanel`` 的图标模式宽度，写死在库里（``__initWidget`` 与 ``collapse``）
COMPACT_WIDTH = 48
#: 走完 48px -> ``expandWidth`` 全程的时长，与上游保持一致
FULL_DURATION_MS = 150
#: 短距离反向时的时长下限，避免剩余距离太小时看起来像瞬移
MIN_DURATION_MS = 70


class InterruptibleNavigationPanel(NavigationPanel):
    """可打断、可反向的侧边栏展开/收起动画。

    上游 ``NavigationPanel`` 的动画不接受"中途改主意"，用户反馈的粘滞与不跟手来自
    三处：

    1. ``collapse()`` 在 ``expandAni`` 运行时直接 ``return``。展开动画的 150ms 内点
       汉堡按钮关闭侧边栏，这次点击被静默丢弃——手感上就是"点了没反应"。
    2. ``expand()`` 把动画起点硬编码成 ``QSize(48, height)``，不看当前宽度。收起到
       一半再展开，面板会先瞬移回 48px 再滑出。
    3. ``toggle()`` 依据 ``displayMode`` 决定方向，而 ``displayMode`` 要到收起动画
       结束才变成 ``COMPACT``。收起途中点按钮会再收一次，而不是反向展开。

    这个子类让动画始终从**当前宽度**出发，并把时长按剩余距离等比缩放（全程仍是
    上游的 150ms + OutQuad），于是任何方向的动画都能被随时反向打断。
    """

    def expand(self, useAni: bool = True) -> None:
        if not useAni:
            super().expand(False)
            return

        # 必须在 super() 之前取：上游 expand() 里的 expandAni.start() 会立刻把
        # 起点值（48px）同步应用到面板几何上，之后就读不到真实的当前宽度了
        start_width = self.width()

        # 仍然走一遍上游逻辑，拿到解除 compact、displayMode 判定、MENU 模式下的
        # 亚克力抓图与重挂载等前置处理；它启动的动画随后被 _retarget 覆盖
        super().expand(True)
        self._retarget_expand_ani(start_width, self.expandWidth, expand=True)

    def collapse(self) -> None:
        # 上游 collapse() 见到动画在跑就直接 return，先把动画停在当前宽度绕开它
        self.expandAni.stop()
        start_width = self.width()

        super().collapse()
        self._retarget_expand_ani(start_width, COMPACT_WIDTH, expand=False)

    def toggle(self) -> None:
        if self.expandAni.state() == QPropertyAnimation.State.Running:
            # 收起动画期间 displayMode 仍是 EXPAND，只有动画自己的 expand 属性
            # 记录了真实意图，据此反向
            if self.expandAni.property("expand"):
                self.collapse()
            else:
                self.expand()
            return

        super().toggle()

    def _retarget_expand_ani(self, start_width: int, end_width: int, expand: bool) -> None:
        """把 ``expandAni`` 重定向为 ``start_width -> end_width``，时长按距离缩放。"""
        ani = self.expandAni
        ani.stop()

        full_distance = abs(self.expandWidth - COMPACT_WIDTH) or 1
        ratio = abs(end_width - start_width) / full_distance
        ani.setDuration(max(MIN_DURATION_MS, round(FULL_DURATION_MS * ratio)))

        ani.setStartValue(QRect(self.pos(), QSize(start_width, self.height())))
        ani.setEndValue(QRect(self.pos(), QSize(end_width, self.height())))
        ani.setProperty("expand", expand)
        ani.start()


def install_interruptible_navigation(interface: NavigationInterface) -> bool:
    """把 ``interface`` 的侧边栏动画换成可打断实现，返回是否安装成功。

    ``FluentWindow.__init__`` 内部就把 ``NavigationPanel`` 建好、装进布局并接好了
    信号，没有留下注入子类的口子，所以这里替换单个实例的 ``__class__``。它只影响
    这一个 panel，不像全局 monkey patch 那样波及其他窗口，也不需要重连
    ``menuButton.clicked``——PySide6 对已连接的绑定方法是按实例动态解析的。
    """
    panel = interface.panel

    if isinstance(panel, InterruptibleNavigationPanel):
        return True

    if type(panel) is not NavigationPanel:
        # 上游换了实现（例如 Pro 版自带子类），此时贸然替换会丢掉对方的行为
        logger.warning(
            f"[Navigation] 侧边栏 panel 类型为 {type(panel).__name__}，跳过可打断动画安装"
        )
        return False

    panel.__class__ = InterruptibleNavigationPanel
    return True
