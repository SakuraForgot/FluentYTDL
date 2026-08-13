from __future__ import annotations

from qfluentwidgets import FluentStyleSheet
from qframelesswindow import TitleBar, TitleBarButton


class ThemedTitleBar(TitleBar):
    """跟随 QFluentWidgets 主题的无边框标题栏。

    qframelesswindow 自带的 ``TitleBar`` 不受 QFluentWidgets 主题控制：
    ``TitleBarButton`` 的 ``_normalColor`` 默认写死为黑色，所以最小化 / 最大化 /
    关闭三个按钮在深色模式下会变成黑底黑图标，几乎看不见。

    ``FluentWindow`` 之所以没这个问题，是因为它用的 ``FluentTitleBar`` 会把
    ``FLUENT_WINDOW`` 样式表贴到自己身上，由样式表里的 ``qproperty-normalColor``
    覆盖按钮默认色。这里对普通 ``TitleBar`` 做同样的事，并额外把样式表单独注册到
    每个按钮上——注册进 ``styleSheetManager`` 后，``setTheme()`` 会自动重新下发
    对应主题的 qss，无需手动刷新颜色。

    保持 32px 高度和纯按钮布局（不加图标/标题），仅修颜色，不改动现有外观。
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.updateStyle()

    def updateStyle(self) -> None:
        """重新应用当前主题的标题栏样式。"""
        FluentStyleSheet.FLUENT_WINDOW.apply(self)
        for button in self.findChildren(TitleBarButton):
            FluentStyleSheet.FLUENT_WINDOW.apply(button)
