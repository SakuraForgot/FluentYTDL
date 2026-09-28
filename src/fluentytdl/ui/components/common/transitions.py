"""快照淡出式过渡：在不钝化文字的前提下给页面 / 内容切换加一层交叉淡化。

`QGraphicsOpacityEffect` 常驻会钝化 ClearType 文字（见 `dialogs/assembly_preview.py`
的同类注释与 CLAUDE.md §3）。所以这里**不给活动控件挂效果**，而是抓一张切换前的
快照浮在原位、只对这张快照做淡出：活动控件全程原生渲染、文字锐利，旧内容淡出后自然
露出下方已就位的新内容。变更在快照遮挡的瞬间同步完成，高度 / 布局的突变一并被藏起，
视觉上就是一次平滑的交叉淡化——`qfluentwidgets.TransitionStackedWidget` 的同一套哲学，
但不绑定 QStackedWidget，可直接套在「就地重建内容」的滚动区 / 表格区上。
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QObject, QPoint, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPixmap, QRegion
from PySide6.QtWidgets import QGraphicsOpacityEffect, QLabel, QWidget

from fluentytdl.ui.components.common.adaptive_layout import task_surface_color


class RegionFader(QObject):
    """给某个区域控件的「就地内容切换」加快照交叉淡化。

    用法：`fader.run(apply_change)`——`apply_change` 是真正执行切换的回调（换栈页、
    重建预设、刷新表格……）。`run` 会在调用**前**抓好旧快照，随后**同步**执行
    `apply_change`（信号、时序与不加动画时完全一致——包括其中的 `selectionChanged.emit()`
    与延迟 `doItemsLayout()` 定时器），再把旧快照淡出。同步执行是刻意的：宿主依赖这些副作用
    的既有时序，动画只该改变「看起来怎样」，绝不改变「发生了什么、按什么顺序」。

    只有正在淡出的快照挂 `QGraphicsOpacityEffect`；淡完即 `hide()`，活动控件回到原生渲染，
    文字保持锐利。快照是 target 的**兄弟**（挂在 `target.parentWidget()` 上、盖住 target 的
    geometry），所以给 target 拍快照不会把上一张残留快照也抓进去；连续快速切换时每次都从
    最新的活动内容重新抓拍并重启淡出，天然「后来者胜」。
    """

    def __init__(
        self,
        target: QWidget,
        *,
        duration: int = 240,
        background: QColor | Callable[[], QColor] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent or target)
        self._target = target
        self._duration = duration
        # 快照必须合成在「正确的底色」上。目标区域（分页栈 / 预设滚动区）自身透明，真正的
        # 底色是祖先画的 task_surface_color；而 QWidget.grab() 会拿控件调色板的 Window 角色
        # （浅色主题近白）当根背景填底，于是淡出时先闪一下白块——正是用户看到的「闪白」。
        # 改由 _snapshot 自己按 task_surface_color 打底再叠画子控件，快照与屏幕逐像素一致。
        # background 可覆盖：卡片等非 surface 底色的区域传入自己的底色（QColor 或取色回调）。
        self._background = background
        self._enabled = True
        self._overlay: QLabel | None = None
        self._effect: QGraphicsOpacityEffect | None = None
        self._ani: QPropertyAnimation | None = None

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 (Qt 风格命名)
        """关掉动画：切换将即时发生（用于测试或用户偏好「减少动态效果」）。"""
        self._enabled = enabled

    def run(self, apply_change: Callable[[], None]) -> None:
        target = self._target
        host = target.parentWidget() or target
        # 不适合动画的场景一律直接应用，绝不吞掉变更：被禁用 / 未显示（含构建期，
        # 首帧本就不该动画）/ 尺寸为空（还没布局）。
        if not self._enabled or not target.isVisible() or target.size().isEmpty():
            apply_change()
            return

        pixmap = self._snapshot(target)
        if pixmap.isNull() or pixmap.size().isEmpty():
            apply_change()
            return

        overlay = self._ensure_overlay(host)
        # target 极少换父，但保险起见每次校正快照的父子关系与几何。
        if overlay.parentWidget() is not host:
            overlay.setParent(host)
        overlay.setPixmap(pixmap)
        overlay.setGeometry(target.geometry())
        self._effect.setEnabled(True)
        self._effect.setOpacity(1.0)
        overlay.show()
        overlay.raise_()

        # 在旧快照遮挡下同步换内容——高度突变、表格重排都被这一层盖住。
        apply_change()

        ani = self._ani
        ani.stop()
        ani.setStartValue(1.0)
        ani.setEndValue(0.0)
        ani.start()

    def _snapshot(self, target: QWidget) -> QPixmap:
        """把 target 合成到一张**不透明**、以真实底色打底的快照上。

        不用 `target.grab()`：grab 对「透明、靠祖先画底」的控件会用调色板 Window 角色
        （浅色主题下近白）当根背景填底，快照底色便与屏幕实际的 `task_surface_color` 不符，
        淡出时闪一下白。这里先用正确底色 `fill()`，再以 `DrawChildren`（**不含**
        `DrawWindowBackground`，免得那层近白底又盖回来）叠画控件与子树——透明处露出的正是
        我们填的底色，与屏幕所见逐像素一致。DPR 与 grab 同：物理像素建图、回填逻辑倍率。
        """
        bg = self._background() if callable(self._background) else self._background
        if bg is None:
            bg = task_surface_color()
        dpr = target.devicePixelRatioF()
        size = target.size()
        pixmap = QPixmap(round(size.width() * dpr), round(size.height() * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(bg)
        target.render(pixmap, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        return pixmap

    def _ensure_overlay(self, host: QWidget) -> QLabel:
        if self._overlay is None:
            overlay = QLabel(host)
            # 淡出途中活动控件在下方原生响应，快照本身不该拦截鼠标。
            overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
            overlay.setScaledContents(False)  # 快照 1:1，绝不拉伸
            effect = QGraphicsOpacityEffect(overlay)
            overlay.setGraphicsEffect(effect)
            ani = QPropertyAnimation(effect, b"opacity", self)
            ani.setDuration(self._duration)
            ani.setEasingCurve(QEasingCurve.Type.OutCubic)
            ani.finished.connect(self._on_finished)
            self._overlay = overlay
            self._effect = effect
            self._ani = ani
        return self._overlay

    def _on_finished(self) -> None:
        # 淡完藏起快照并关掉效果，活动控件回到原生锐利渲染。
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay.clear()
        if self._effect is not None:
            self._effect.setEnabled(False)
