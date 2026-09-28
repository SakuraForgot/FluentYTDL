"""Phase 4 常驻「装配预览」卡片 —— 纯展示层。

宿主页面（`DownloadConfigWindow` 单视频默认模式）在每次「选择 / 字幕开关 / 封面开关」变化时
把重算出的 `ResolvedDownloadPlan` 交给 `render_plan()`，本卡片把 `preview_items()`（已由
`tr_text` 归一 i18n 的结构化「类别 + 彩色徽标 + 明文」条目）逐条渲染成定高的 `_PreviewChip`。

**不算容器、不碰 yt-dlp**：预览与实际任务装配同源于
`models/download_plan.build_download_plan`，这里绝不二次判决（守住「预览==实际装配」不变量）。
每个条目摆进定高、内部 `AlignVCenter` 的芯片，行内竖向居中——根治裸 `BodyLabel` 在流式布局里
的基线漂移（#5「字体偏移不居中」）。徽标复用流表的马卡龙色系（`badges.QualityBadge`），
override_note 说明行整行降级为暗黑对比 `CaptionLabel`，与主行拉开层次。
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QPoint, QPropertyAnimation, QRect, QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLayout,
    QLayoutItem,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import BodyLabel, CaptionLabel, CardWidget, StrongBodyLabel

from fluentytdl.models.download_plan import PreviewItem, ResolvedDownloadPlan
from fluentytdl.ui.components.common.badges import QualityBadge
from fluentytdl.utils.ui_text import tr_text

# CaptionLabel 次要文字的亮/暗双色（全项目既定对比对，见 CLAUDE.md §3）。
_DIM = (QColor(96, 96, 96), QColor(210, 210, 210))


class _ChipFlowLayout(QLayout):
    """自绘的横向流式布局（Qt 官方 FlowLayout 例程）——本预览卡的「特殊容器」。

    为什么不用 qfluentwidgets 的 `FlowLayout`：它把裸 `BodyLabel` 直接摆进去，标签各自的
    基线/自然高度不一，行内不做竖向居中，文字于是看起来「偏移、不居中」（用户所谓 QT 劣根）。
    这里只摆定高的 `_PreviewChip`（芯片内部 `AlignVCenter`），每行等高，基线自然对齐。
    实现 `heightForWidth`，配合 `_FlowHost` 把换行后的真实高度透传给父竖向布局。
    """

    def __init__(
        self, parent: QWidget | None = None, *, h_spacing: int = 10, v_spacing: int = 6
    ) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._h = h_spacing
        self._v = v_spacing
        if parent is not None:
            self.setContentsMargins(0, 0, 0, 0)

    def __del__(self) -> None:  # 释放时排空 item（不删控件，交给 Qt 父子树）。
        while self.takeAt(0) is not None:
            pass

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:  # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        left, top, right, bottom = self.getContentsMargins()
        return size + QSize(left + right, top + bottom)

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        left, top, right, bottom = self.getContentsMargins()
        effective = rect.adjusted(left, top, -right, -bottom)
        x, y, line_height = effective.x(), effective.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + self._v
                next_x = x + hint.width() + self._h
                line_height = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y() + bottom


class _FlowHost(QWidget):
    """承载 `FlowLayout` 的宿主，把「按宽度换行后的总高度」透传给父级竖向布局。

    Qt 的坑：`QWidgetItem::hasHeightForWidth()` 读的是**子控件自身**的
    `sizePolicy().hasHeightForWidth()`，而不是它内层布局的——哪怕内层 `FlowLayout`
    明明 `hasHeightForWidth()==True`。默认 `QWidget` 的 sizePolicy 这一位是 False，
    父 `QVBoxLayout` 便只按 `sizeHint()`（≈单行）给高，换行的芯片行就溢出去遮住别的 UI。
    这里显式打开这一位并把查询转发给内层 FlowLayout，父级才会按真实换行高度分配空间。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        policy = self.sizePolicy()
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt 覆写命名)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        layout = self.layout()
        if layout is not None:
            return layout.heightForWidth(width)
        return super().heightForWidth(width)

    def sizeHint(self) -> QSize:  # noqa: N802
        layout = self.layout()
        if layout is not None:
            width = self.width() or layout.sizeHint().width()
            return QSize(width, layout.heightForWidth(width))
        return super().sizeHint()


class _PreviewChip(QWidget):
    """一行「配方」条目的定高芯片：内部 `AlignVCenter`，行内所有元素竖向居中。

    左起：暗色类别小字（`label`）→ 若干 `QualityBadge` 彩色药丸（`badges`）→ 补充明文
    （`text`）。`note=True` 是容器被硬约束改写的说明行——无类别标签、整行暗色小字。
    定高（`_HEIGHT`）+ `AlignVCenter` 是 #5「字体偏移不居中」的正解：不再让裸 `BodyLabel`
    在流式布局里各自漂移基线。竖卡窄列（`wrap=True`）允许折行、放弃定高，让高度自适应。
    """

    _HEIGHT = 26

    def __init__(
        self, item: PreviewItem, parent: QWidget | None = None, *, wrap: bool = False
    ) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        if item.note:
            note_lbl = CaptionLabel(item.text, self)
            note_lbl.setTextColor(*_DIM)
            note_lbl.setWordWrap(wrap)
            lay.addWidget(note_lbl)
        else:
            if item.label:
                cat = CaptionLabel(item.label, self)
                cat.setTextColor(*_DIM)
                lay.addWidget(cat)
            for text, color in item.badges:
                lay.addWidget(QualityBadge(text, color, self, compact=True))
            if item.text:
                detail = BodyLabel(item.text, self)
                detail.setWordWrap(wrap)
                lay.addWidget(detail)
        if not wrap:
            self.setFixedHeight(self._HEIGHT)


class AssemblyPreviewCard(CardWidget):
    """「最终会产出什么」的常驻只读卡片。宿主每次重算后调 `render_plan()` 刷新。

    两种排布（同一 `render_plan` 逻辑，只是行容器不同）：
    - 竖卡（`bar=False`，旧双栏右列遗留形态）：`QVBoxLayout` 逐行堆叠，有最大宽度上限。
    - 底栏/顶条（`bar=True`）：`QVBoxLayout` 里标题（`StrongBodyLabel`）在上、`_FlowHost`
      包裹的 `FlowLayout` 芯片行在下；横向 Expanding、无最大宽度上限，随窗口拉伸自适应。
      `_FlowHost` 打开 heightForWidth 透传，父级才会按换行后的真实高度给足空间（否则芯片
      行溢出遮挡其它 UI）。

    每次 `render_plan()` 内容变化都走淡出→换→淡入的过渡（`_on_fade_finished` 驱动的
    QGraphicsOpacityEffect），根治「选择多音轨时预览突兀刷新」；仍是纯展示层，动画绝不触及
    传入的 plan（守「预览==实际装配」不变量）。
    """

    def __init__(self, parent: QWidget | None = None, *, bar: bool = False) -> None:
        super().__init__(parent)
        self._bar = bar

        if bar:
            # 全宽条：横向撑满、纵向按芯片行数（heightForWidth）自适应；不设 maxWidth 上限。
            policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
            # 卡片自身也要开 heightForWidth，父竖向布局才会把 _FlowHost 的换行高度算进来。
            policy.setHeightForWidth(True)
            self.setSizePolicy(policy)
            root = QVBoxLayout(self)
            root.setContentsMargins(16, 12, 16, 12)
            root.setSpacing(8)
            root.addWidget(StrongBodyLabel(tr_text("装配预览"), self))
            self._lines_host = _FlowHost(self)
            self._lines_layout = _ChipFlowLayout(self._lines_host, h_spacing=10, v_spacing=6)
            root.addWidget(self._lines_host)
        else:
            self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
            # 双栏挤压时的最低可读宽度；实际展开宽度由宿主的 stretch(2:1) + maxWidth 决定。
            # 刻意压低（曾为 260）——否则 contentWidget 最小宽把窄窗撑出横向滚动条，破坏
            # 「流体自适应，禁 setFixedSize」，也会让 test_task_window_layout 的暗黑表面探针
            # 落到 top_card 平铺填充而非圆角背景上。160 → cwMin≈496，稳过 592 视口。
            self.setMinimumWidth(160)
            self.setMaximumWidth(360)

            root = QVBoxLayout(self)
            root.setContentsMargins(16, 16, 16, 16)
            root.setSpacing(10)

            root.addWidget(StrongBodyLabel(tr_text("装配预览"), self))

            self._lines_host = QWidget(self)
            self._lines_layout = QVBoxLayout(self._lines_host)
            self._lines_layout.setContentsMargins(0, 0, 0, 0)
            self._lines_layout.setSpacing(6)
            root.addWidget(self._lines_host)
            root.addStretch(1)

        # 过渡动画（Issue：选择多音轨时预览「突兀更新」）：芯片行整体挂一枚
        # QGraphicsOpacityEffect，每次内容变化走「淡出旧内容 → 原地换成新内容 → 淡入」。
        # 内容替换发生在 opacity≈0 的隐身瞬间，高度增删也被藏在这一刻，视觉上只见平滑过渡。
        # 纯展示层：绝不碰 render_plan 收到的 plan（守「预览==实际装配」不变量）。
        # 静止时 setEnabled(False) 关掉效果，让文字回到原生渲染、保持锐利（QGraphicsOpacityEffect
        # 常驻会钝化 ClearType 文字）。首帧不做动画——用户抱怨的是「选择变化时」的突兀更新，
        # 不是窗口初次出现，同时也避开构造期动画撞上布局探针的时序。
        self._opacity = QGraphicsOpacityEffect(self._lines_host)
        self._opacity.setOpacity(1.0)
        self._opacity.setEnabled(False)
        self._lines_host.setGraphicsEffect(self._opacity)
        self._fade = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade.setDuration(140)
        self._fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade.finished.connect(self._on_fade_finished)
        self._fade_phase: str | None = None  # "out" / "in" / None（静止）
        self._pending_items: list[PreviewItem] | None = None
        self._last_sig: tuple | None = None  # 上次渲染的条目签名，去重防无谓闪烁
        self._rendered_once = False

        self._show_placeholder()

    def _show_placeholder(self) -> None:
        ph = CaptionLabel(tr_text("尚未选择"), self)
        ph.setTextColor(*_DIM)
        # 底栏芯片单行紧凑，不换行；竖卡窄列需要自动折行。
        ph.setWordWrap(not self._bar)
        self._lines_layout.addWidget(ph)

    def _clear_lines(self) -> None:
        # 竖卡 QVBoxLayout 与底栏 _ChipFlowLayout 都实现了 count/takeAt，统一一套清空逻辑。
        while self._lines_layout.count():
            item = self._lines_layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()

    def render_plan(self, plan: ResolvedDownloadPlan | None) -> None:
        """用一份计划刷新预览；`None` 或空计划回落到占位提示。

        首帧直接落地；此后每次内容真变化都走淡出→换→淡入的过渡（见 `__init__` 的说明）。
        条目与上次完全一致时直接返回——避免选择在等价状态间切换时无谓地闪一下。
        """
        items = plan.preview_items() if plan is not None else []
        sig = self._plan_signature(items)
        if sig == self._last_sig:
            return
        self._last_sig = sig
        self._pending_items = items

        if not self._rendered_once:
            # 首帧（窗口刚出现）不做动画，避免从空白淡入，也避开构造期时序问题。
            self._rendered_once = True
            self._apply_pending()
            return

        # 后续实时变化：从当前不透明度淡出到 0（被打断的动画也从现值起步，不会突跳）。
        self._opacity.setEnabled(True)
        self._fade_phase = "out"
        self._fade.stop()
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def _plan_signature(self, items: list[PreviewItem]) -> tuple:
        """把条目压成可比对的签名：字段全是 str/bool/嵌套 tuple，天然可哈希、可等值比较。"""
        return tuple((it.label, it.badges, it.text, it.note) for it in items)

    def _apply_pending(self) -> None:
        """在隐身瞬间原地替换芯片内容（清空→按 `_pending_items` 重建，空则回落占位）。"""
        self._clear_lines()
        items = self._pending_items or []
        self._pending_items = None
        if not items:
            self._show_placeholder()
            return
        for item in items:
            # 底栏芯片单行、由 _ChipFlowLayout 横向流式换行；竖卡窄列允许芯片内折行。
            chip = _PreviewChip(item, self._lines_host, wrap=not self._bar)
            self._lines_layout.addWidget(chip)

    def _on_fade_finished(self) -> None:
        """淡出到底 → 换内容 → 淡入；淡入到顶 → 关掉效果让文字回归原生锐利渲染。

        `_pending_items` 取的是最新一份：淡出途中若又来新计划，`render_plan` 已把它更新，
        这里换上的始终是最后到达的内容（后来者胜）。
        """
        if self._fade_phase == "out":
            self._apply_pending()
            self._fade_phase = "in"
            self._fade.stop()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        elif self._fade_phase == "in":
            self._fade_phase = None
            self._opacity.setOpacity(1.0)
            self._opacity.setEnabled(False)
