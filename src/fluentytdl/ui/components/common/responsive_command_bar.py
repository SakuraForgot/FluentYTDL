"""能被 QHBoxLayout 压缩的 `CommandBar`。

`CommandBar` 本来就**自带溢出能力**：`resizeEvent` → `updateGeometry()` →
`_visibleWidgets()` 会把摆不下的按钮隐藏并弹出「更多」按钮。缺的只是一个
`sizeHint()` —— 它用手工摆位管理子控件、身上没有 QLayout，于是
`QWidget.sizeHint()` 返回无效尺寸 (-1, -1)，`QWidgetItem` 退回
`minimumSizeHint()`（库里返回「更多」按钮的尺寸，约 40px），直接塞进
QHBoxLayout 的话 5 颗按钮会全被折进溢出菜单。

上游给的绕法是 `resizeToSuitableWidth()`，但它是 `setFixedWidth(...)` ——
连**最小宽**一起钉死。行内一旦放不下，`QHBoxLayout` 就只能去压别人：
被压到最小宽以下的控件会被 `QWidget.setGeometry` 反弹回各自的最小宽，
于是控件互相错叠（`SegmentedWidget` 的最小宽是硬的，表现就是 pivot
压住右边的「更多筛选」按钮）。

所以这里把「定宽」换成「首选宽」：`sizeHint()` 给 `suitableWidth()`
（= 全部按钮都摆得开的宽度），最小宽仍是库里的 `minimumSizeHint()`。
宽裕时全展开，紧张时它自己把尾部按钮收进「更多」菜单，而不是逼邻居越界。
"""

from __future__ import annotations

from PySide6.QtCore import QSize
from qfluentwidgets import CommandBar


class ResponsiveCommandBar(CommandBar):
    """首选宽 = 全部按钮摆得开，最小宽 = 只剩「更多」按钮。"""

    def sizeHint(self) -> QSize:
        # `_visibleWidgets()` 判定「够不够摆」用的就是 `suitableWidth() <= width()`，
        # 所以这里给的宽度正好是「一颗都不折叠」的临界值，不用另加余量。
        return QSize(self.suitableWidth(), self.height())
