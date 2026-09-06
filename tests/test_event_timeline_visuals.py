"""时间线视图的渲染与选中态（`_TimelineDelegate` + 叶子/分组的字体与配色）。

用户的原话是"太丑、窄小、选中状态有点诡异"。前两条是尺寸和字号，改完看一眼就知道
对不对；**"选中状态诡异"这一条改在 delegate 里，而 delegate 的输出是像素** ——
不真跑一遍 `paint()` 的话，哪天 qfluentwidgets 升级动了上游 `paint()` 的顺序，
我们这层补偿会静默失效，而单测全绿。

上游 `TreeItemDelegate` 有三处在这棵五级深的树上会被放大成"诡异"：悬停与选中共用
一个 `QColor(c,c,c,9)`；指示条画在 viewport 的**绝对 x=4**；背景画在文字**之后**。
这里钉住我们的补偿确实生效：

1. 整行底色**只铺一次** —— 逐列各画一个圆角矩形，选中行看着像四颗分开的胶囊；
2. 选中 ≠ 悬停，且选中用强调色；
3. 指示条跟着内容缩进走，不是 viewport 绝对坐标；
4. 交给上游画文字时 `Selected` / `MouseOver` 已被摘掉 —— 摘掉它上游才不会把自己
   那层背景糊到文字上（顺带确认**原 option 没被改**，那是别人的对象）；
5. 缩进必须是 20（qfluentwidgets 把展开箭头的点击区写死了）；
6. 一行里所有列同为 13px —— `setUniformRowHeights(True)` 的前提。

第二轮用户又提"时间线，级别，时间，详情的间隔太大"。那一组落在文件末尾的
「列宽与间距」一节：上游 `QTreeView::item` 的 `padding-left: 20px` 加在**每一列**上、
第 0 列原先按最深的树钉死宽度、「级别」「时间」原先居中。

需要 QApplication（真实 QTreeWidget + `themeColor()`），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-tlvis-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtCore import QRect, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QStyle,
    QStyleOptionViewItem,
)
from qfluentwidgets import Theme, TreeItemDelegate, getFont, setTheme, themeColor  # noqa: E402

from fluentytdl.observability import EVENT_KINDS, FlowTrace, TaskTrace, build_event  # noqa: E402
from fluentytdl.ui.components.common import event_timeline as timeline_mod  # noqa: E402
from fluentytdl.ui.components.common.event_timeline import EventTimelineView  # noqa: E402

#: 模拟一片五级缩进的叶子：第 0 列的 `option.rect` 从这里开始，而不是 0。
#: "指示条画在绝对 x=4"这个 bug 只有在缩进不为 0 时才看得出来。
DEEP_INDENT_X = 100

#: 画布宽。视图没 show 过，`viewport().width()` 是默认宽度，断言只用它自己的值。
CANVAS = (900, 40)


@pytest.fixture(scope="module")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(qt_app):
    v = EventTimelineView()
    yield v
    v.deleteLater()


def _event(kind: str, *, level: str = "INFO", stage: str = "download", trace=None, **fields):
    payload = build_event(kind, trace=trace, stage=stage, fields=fields).as_dict()
    payload["_level"] = level
    payload["_time"] = "12:00:00"
    return payload


def _trace() -> TaskTrace:
    return TaskTrace(flow_id="k72f", task_id="42", run_id="a91c")


def _visible_labels(item, depth=0):
    if item.isHidden():
        return []
    out = [(depth, item.text(0))]
    for i in range(item.childCount()):
        out.extend(_visible_labels(item.child(i), depth + 1))
    return out


def _walk_with_depth(view, item=None, depth=0):
    """整棵树的 `(item, depth)`，深度按 Qt 的口径数（顶层 = 0）。"""
    if item is not None:
        yield item, depth
        children = [item.child(i) for i in range(item.childCount())]
    else:
        children = [view.topLevelItem(i) for i in range(view.topLevelItemCount())]
        depth = -1
    for child in children:
        yield from _walk_with_depth(view, child, depth + 1)


class _ShapeCapture(QPainter):
    """记下 delegate 画了哪些圆角矩形，以及画每一个时的画刷颜色。

    覆写能生效是因为 `_fill_row()` 里那句 `painter.drawRoundedRect(...)` 是**从
    Python 调的**，走属性查找（`QPainter::drawRoundedRect` 在 C++ 侧并不虚）。
    上游 `_drawBackground` / `_drawIndicator` 同样是 Python，所以它们要是被走到了
    这里也会记下来 —— 正好用来证明它们没被走到。
    """

    def __init__(self, device):
        super().__init__(device)
        self.rects: list[tuple[QRectF, QColor]] = []

    def drawRoundedRect(self, *args):  # noqa: N802  Qt 命名
        if args and isinstance(args[0], (QRectF, QRect)):
            self.rects.append((QRectF(args[0]), QColor(self.brush().color())))
        return super().drawRoundedRect(*args)


def _paint(view, item, *, selected=False, hovered=False, columns=(0,)) -> _ShapeCapture:
    """跑一遍真实 `paint()`，返回画笔记录。

    列的 `option.rect` 都从 `DEEP_INDENT_X` 起：Qt 给第 0 列的 rect 本来就是缩进后
    的位置，而"整行底色"和"指示条位置"两件事的正确与否全看这个偏移有没有被用上。
    """
    image = QImage(*CANVAS, QImage.Format.Format_ARGB32)
    painter = _ShapeCapture(image)
    delegate = view.itemDelegate()
    try:
        for col in columns:
            option = QStyleOptionViewItem()
            option.rect = QRect(DEEP_INDENT_X + col * 120, 4, 110, 28)
            option.state = QStyle.StateFlag.State_Enabled
            if selected:
                option.state |= QStyle.StateFlag.State_Selected
            if hovered:
                option.state |= QStyle.StateFlag.State_MouseOver
            delegate.paint(painter, option, view.indexFromItem(item, col))
    finally:
        painter.end()
    return painter


def _row_fill(painter: _ShapeCapture) -> tuple[QRectF, QColor]:
    """整行底色那一笔（宽度远大于一列）。"""
    fills = [(r, c) for r, c in painter.rects if r.width() > 200]
    assert len(fills) == 1, painter.rects
    return fills[0]


# ── 选中 / 悬停 ──────────────────────────────────────────────


def test_row_background_is_one_stroke_across_the_whole_row(view):
    """整行只铺一次底色。

    逐列各画一个圆角矩形的话，选中的行看着像四颗分开的胶囊 —— 上游就是这么画的
    （`_drawBackground` 按列拼圆角），靠 3.5% 的 alpha 才看不出接缝。
    """
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    painter = _paint(view, leaf, selected=True, columns=(0, 1, 2, 3))
    rect, _ = _row_fill(painter)

    assert rect.x() == pytest.approx(4)
    assert rect.width() == pytest.approx(view.viewport().width() - 8)


def test_selection_and_hover_do_not_look_alike(view):
    """上游给选中和悬停的是同一个刷子，点中一行和鼠标扫过一行长得一模一样。"""
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    _, selected = _row_fill(_paint(view, leaf, selected=True))
    _, hovered = _row_fill(_paint(view, leaf, hovered=True))

    assert selected != hovered
    # 选中用强调色（和悬停那层中性灰不是一个色相），且明显更重
    assert selected.rgb() == QColor(themeColor()).rgb()
    assert selected.alpha() > hovered.alpha() * 2


def test_selection_indicator_follows_the_content_indent(view):
    """指示条要贴着该行内容开始的地方。

    上游画在 viewport 的绝对 `x=4`（`_drawIndicator`）。这棵树最深五级、缩进约
    100 px，于是那条 3px 主题色竖条飘在离它所指那行一百像素远的地方 —— 看着像是
    别的行的标记，这正是"选中状态诡异"最刺眼的一处。
    """
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    painter = _paint(view, leaf, selected=True)
    bars = [r for r, _ in painter.rects if r.width() <= 4]

    assert len(bars) == 1, painter.rects
    assert bars[0].x() == pytest.approx(DEEP_INDENT_X + 3)


def test_hover_alone_draws_no_indicator(view):
    """竖条是"选中"的标记。悬停也画的话，鼠标扫过整棵树就像在连续改变选中项。"""
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    painter = _paint(view, leaf, hovered=True)
    assert [r for r, _ in painter.rects if r.width() <= 4] == []


def test_unselected_unhovered_rows_are_not_painted_at_all(view):
    """没选中也没悬停时一笔都不画 —— 否则 3000 行每行一个圆角矩形。"""
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    assert _paint(view, leaf, columns=(0, 1, 2, 3)).rects == []


def test_upstream_draws_text_without_the_selection_bits(view, monkeypatch):
    """交给上游画文字前必须把 `Selected` / `MouseOver` 摘掉。

    上游 `paint()` 的顺序是 `super().paint()`（文字）→ `_drawBackground()`，也就是
    **背景盖在文字上**。它靠 alpha=9 看不出来，可我们把选中态调到了看得见 —— 不摘
    这两个位，字就被自己的底色糊住了。

    顺带钉住"不改别人的 option"：`option` 是 Qt 传进来的，改它会污染同一行后面几列
    的绘制。
    """
    seen: list[tuple[bool, bool]] = []

    def _record(self, painter, option, index):
        seen.append(
            (
                bool(option.state & QStyle.StateFlag.State_Selected),
                bool(option.state & QStyle.StateFlag.State_MouseOver),
            )
        )

    monkeypatch.setattr(TreeItemDelegate, "paint", _record)

    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))

    image = QImage(*CANVAS, QImage.Format.Format_ARGB32)
    painter = _ShapeCapture(image)
    option = QStyleOptionViewItem()
    option.rect = QRect(DEEP_INDENT_X, 4, 110, 28)
    option.state = (
        QStyle.StateFlag.State_Enabled
        | QStyle.StateFlag.State_Selected
        | QStyle.StateFlag.State_MouseOver
    )
    try:
        view.itemDelegate().paint(painter, option, view.indexFromItem(leaf, 0))
    finally:
        painter.end()

    assert seen == [(False, False)]
    assert option.state & QStyle.StateFlag.State_Selected
    assert option.state & QStyle.StateFlag.State_MouseOver


def test_indentation_stays_at_the_value_qfluentwidgets_hardcodes(view):
    """缩进必须是 20，**别改**。

    qfluentwidgets 把展开箭头的点击区写死成 `level*indentation()+20` 起 10 px
    （`TreeWidget.viewportEvent`），而箭头本身画在 `moveLeft(15)`
    （`TreeViewBase.drawBranches`）—— 只有 `indentation()==20` 时两者才对得上。
    曾设成 14：箭头画在 29..43、点击区却是 34..44，点箭头有一半概率只选中不展开，
    在用户眼里就是"选中状态很诡异"。
    """
    assert view.indentation() == 20


# ── 字体 / 配色 ──────────────────────────────────────────────


def test_every_column_uses_the_same_pixel_size(view):
    """行内字号必须一致 —— 这是 `setUniformRowHeights(True)` 成立的前提。

    qfluentwidgets 的 `getFont()` 用的是 `setPixelSize`；等宽列若按**磅值**设字体，
    它会比旁边的列高出一截，行高跟着不齐，整棵树看着发毛。
    """
    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))
    group = view.topLevelItem(0)

    # 第 0/1 列没设 FontRole，上游 `initStyleOption` 会兜底成 `getFont(13)`
    assert leaf.data(0, Qt.ItemDataRole.FontRole) is None
    sizes = {leaf.font(2).pixelSize(), leaf.font(3).pixelSize()}
    sizes.add(group.font(0).pixelSize())
    sizes.add(getFont(13).pixelSize())

    assert sizes == {13}
    assert view.uniformRowHeights()


def test_group_labels_do_not_fall_back_to_a_default_font(view):
    """分组标签的字体必须是显式构造的。

    原先是 `f = item.font(0); f.setBold(True)` —— 而 `item.font(0)` 在没设过字体时
    返回的是**默认构造的 QFont**（系统默认字族 + 默认磅值），装回去就把上游那句
    `index.data(FontRole) or getFont(13)` 的兜底顶掉了。结果分组行和事件行是两套
    字体，这是"太丑"里最说不清但最扎眼的一条。
    """
    view.add_event(_event("stage", trace=_trace()))
    group = view.topLevelItem(0)

    assert group.font(0).pixelSize() == 13
    assert group.font(0).family() == getFont(13).family()
    assert group.font(0).weight() > getFont(13).weight()  # 分组要比正文重


def test_level_column_is_tinted_even_for_info(view):
    """「级别」这一列永远自带颜色，整行却跟着主题走。

    那一列是整棵树的色标带：眼睛顺着它往下扫就能找到 WARNING 在哪几行。要是 INFO
    也跟着主题走，它就退化成一列灰扑扑的英文单词。
    """
    view.add_event(_event("stage", level="INFO", trace=_trace()))
    leaf = next(iter(view._leaves))

    assert leaf.foreground(1).color().isValid()
    # 其余列清掉 role 而不是塞默认 QBrush —— 默认构造的是黑色，深色主题下就是黑字
    for col in (0, 2, 3):
        assert leaf.data(col, Qt.ItemDataRole.ForegroundRole) is None


def test_error_rows_are_tinted_across_the_whole_row(view):
    view.add_event(_event("diagnosis", level="ERROR", trace=_trace(), code="rate_limited_429"))
    leaf = next(iter(view._leaves))

    colors = {leaf.foreground(c).color().name() for c in range(view.columnCount())}
    assert len(colors) == 1  # ERROR 自己有颜色，级别列和整行同色


def test_every_event_kind_has_an_icon():
    """13 种 kind 是**封闭集合**（`docs/RULES.md` §5），图标表必须配全。

    缺一个的话那一行在一列图标里空出一格，比全都没有图标更显眼。
    """
    assert set(timeline_mod._KIND_ICONS) == set(EVENT_KINDS)


def test_leaf_carries_an_icon(view):
    view.add_event(_event("diagnosis", level="ERROR", trace=_trace()))
    assert not next(iter(view._leaves)).icon(0).isNull()


def test_icons_are_cached_per_kind_and_color(view):
    """图标按 `(kind, 颜色)` 缓存。

    `FluentIcon.icon(color=...)` 会**改写 SVG 源再重新解析**。树里可能有 3000 片
    叶子，而 kind 只有 13 种、级别只有 6 档 —— 不缓存就是把同一份 SVG 解析三千遍。
    """
    timeline_mod._icon_cache.clear()
    for _ in range(30):
        view.add_event(_event("stage", level="DEBUG", trace=_trace()))

    assert len(timeline_mod._icon_cache) == 1


def test_theme_switch_recolors_rows_groups_and_icons(view):
    """主题切换要重刷颜色。

    深色下的浅红（`#FF8A80`）在浅色主题里几乎看不见；图标更彻底 —— 它们是把颜色
    **烧进 SVG** 的，不重生成就一直是旧主题的颜色。
    """
    from qfluentwidgets import qconfig

    def _cached_colors(kind: str) -> set[int]:
        # 只数这一种 kind：`qconfig.themeChanged` 上还挂着前面几个测试造的视图
        # （`deleteLater()` 要等事件循环才真销毁），它们也会跟着重刷自己的图标。
        return {color for k, color in timeline_mod._icon_cache if k == kind}

    original = qconfig.theme
    try:
        setTheme(Theme.DARK)
        timeline_mod._icon_cache.clear()
        view.add_event(_event("diagnosis", level="ERROR", trace=_trace()))
        leaf = next(iter(view._leaves))
        group = view.topLevelItem(0)
        dark_row = leaf.foreground(1).color().name()
        assert len(_cached_colors("diagnosis")) == 1

        setTheme(Theme.LIGHT)

        assert leaf.foreground(1).color().name() != dark_row
        # 图标按新的级别色重新生成 —— 缓存键里带 rgba，所以是多出一份而不是改一份
        assert len(_cached_colors("diagnosis")) == 2
        # 重刷不许把 `flow` 那层的强调色清成默认黑
        assert group.data(0, Qt.ItemDataRole.ForegroundRole) is not None
        assert group.foreground(0).color().rgb() == QColor(themeColor()).rgb()
    finally:
        setTheme(original)


def test_group_tiers_are_told_apart_by_color(view):
    """五级分组不能只靠缩进区分 —— 深处的缩进是一堆等宽空白。

    `flow` 拿强调色（它是整条链的锚点），`run` / `attempt` 压成次要色（多半只有
    一份，是结构不是信息），`task` 与 `[stage]` 保持正文色 —— 那两层才是要读的。
    """
    view.add_event(_event("stage", trace=_trace()))
    flow = view.topLevelItem(0)
    task = flow.child(0)
    run = task.child(0)
    attempt = run.child(0)
    stage = attempt.child(0)

    assert flow.foreground(0).color().rgb() == QColor(themeColor()).rgb()
    assert run.foreground(0).color().isValid()
    assert run.foreground(0).color().rgb() == attempt.foreground(0).color().rgb()
    assert run.foreground(0).color().rgb() != QColor(themeColor()).rgb()
    for item in (task, stage):
        assert item.data(0, Qt.ItemDataRole.ForegroundRole) is None


# ── 自动跟随 ────────────────────────────────────────────────


def test_auto_follow_pauses_when_the_user_scrolls_back(view):
    sb = view.verticalScrollBar()
    sb.setRange(0, 1000)

    sb.setValue(1000)
    assert view._auto_scroll is True

    sb.setValue(400)
    assert view._auto_scroll is False

    sb.setValue(1000 - view.AUTO_SCROLL_SLACK + 1)
    assert view._auto_scroll is True


def test_new_events_do_not_yank_the_view_while_reading_back(view, monkeypatch):
    """用户往回翻时新事件不许把视图拽走 —— 连带把选中的行也拽跑。"""
    calls: list[object] = []
    monkeypatch.setattr(
        EventTimelineView, "scrollToItem", lambda self, item, *a, **kw: calls.append(item)
    )

    view.add_event(_event("stage", trace=_trace()))
    assert len(calls) == 1  # 默认跟到最新一条

    view._auto_scroll = False
    view.add_event(_event("stage", trace=_trace()))
    assert len(calls) == 1


def test_filtered_out_events_do_not_scroll_the_view(view, monkeypatch):
    """被筛掉的叶子不值得把视图拽过去 —— 那会滚到一片空白上。"""
    calls: list[object] = []
    monkeypatch.setattr(
        EventTimelineView, "scrollToItem", lambda self, item, *a, **kw: calls.append(item)
    )

    view.apply_filter(lambda level, _text: level == "ERROR")
    view.add_event(_event("stage", level="DEBUG", trace=_trace()))
    assert calls == []

    view.add_event(_event("diagnosis", level="ERROR", trace=_trace()))
    assert len(calls) == 1


def test_filtered_new_events_leave_no_empty_group_shells(view):
    """筛选生效期间被挡掉的事件不许留下一串点开什么都没有的壳子。

    `apply_filter()` 的整树重扫会收拾它们，但它只在用户动筛选条件时跑 —— 实时来的
    事件走的是 `_add_event()` 这条路。
    """
    view.apply_filter(lambda level, _text: level == "ERROR")

    view.add_event(_event("stage", level="DEBUG", trace=_trace()))
    assert _visible_labels(view.topLevelItem(0)) == []

    view.add_event(_event("diagnosis", level="ERROR", trace=_trace()))
    labels = [label for _, label in _visible_labels(view.topLevelItem(0))]
    assert "diagnosis" in labels
    assert "stage" not in labels


def test_flow_only_events_still_group_and_paint(view):
    """解析段没有 task/run（`task=-`），照样要有图标、颜色和分组着色。"""
    view.add_event(
        _event("signal", level="WARNING", stage="parse", trace=FlowTrace(flow_id="k72f"))
    )
    leaf = next(iter(view._leaves))
    flow = view.topLevelItem(0)

    assert not leaf.icon(0).isNull()
    assert leaf.foreground(1).color().isValid()
    assert flow.foreground(0).color().rgb() == QColor(themeColor()).rgb()


# ── 列宽与间距 ──────────────────────────────────────────────
#
# 用户第二轮的原话是"时间线，级别，时间，详情的间隔太大"。三个各自独立的源头：上游
# `QTreeView::item` 的 `padding-left: 20px` 加在**每一列**上（四列 80px 死区）；第 0 列
# 原先钉死 264px —— 那是"最深 5 级 + 最长的 kind"才需要的宽度，而解析段的事件只有两级
# （`task=-`/`run=-` 不建层级），于是「时间线」和「级别」之间空出一百多像素；「级别」
# 「时间」原先居中，短值 `INFO` 浮在列中间、长值 `WARNING` 顶满，每行的空白都不一样宽。
#
# **这一节里的像素数都不能当真**：offscreen 平台一个字体都没有
# （`QFontDatabase.families()` 里既没有 Consolas 也没有 Segoe UI），`QFontMetrics` 退化成
# 每个字符恒进 13px、`fm.height()` 恒为 13。所以下面一律断言**关系**（谁比谁宽、有没有
# 越界、算出来的宽度和 Qt 实际摆的位置对不对得上），绝不断言绝对值。


def test_upstream_cell_padding_is_cut_down(view):
    """我们那条 `padding-left` 必须排在上游那条后面，否则等于没写。

    上游 `tree_view.qss` 写的是 `QTreeView::item { padding: 4px; padding-left: 20px }`。
    Qt 的样式表按**后来者胜**取同名属性，所以覆盖规则必须追加在整片样式的末尾 ——
    这正是走 `setCustomStyleSheet()` 而不是 `setStyleSheet()` 的原因（见下一个测试）。

    这里也顺带盯着上游：哪天 qfluentwidgets 自己把 20px 改了，这条会红，那时该重新量
    一遍而不是继续沿用我们这个补偿值。
    """
    qss = view.styleSheet()

    assert "padding-left: 20px" in qss  # 上游那条还在（补偿的前提）
    ours = f"padding-left: {view.ITEM_PADDING_LEFT}px"
    assert qss.rindex(ours) > qss.rindex("padding-left: 20px")


def test_cell_padding_override_survives_a_theme_switch(view):
    """切主题时 `styleSheetManager` 会把整片样式重写一遍。

    走 `setStyleSheet()` 的话覆盖规则就在第一次切主题时被抹掉 —— 而"切一次主题列间距
    就变回去了"这种 bug 只有在真的去切主题时才看得见。`setCustomStyleSheet()` 把 qss
    存进 `lightCustomQss`/`darkCustomQss` 两个动态属性，重写时由
    `CustomStyleSheetWatcher` 重新追加，所以两种主题下都还在。
    """
    from qfluentwidgets import qconfig

    ours = f"padding-left: {view.ITEM_PADDING_LEFT}px"
    original = qconfig.theme
    try:
        for theme in (Theme.LIGHT, Theme.DARK, Theme.LIGHT):
            setTheme(theme)
            assert ours in view.styleSheet(), f"{theme} 下覆盖规则丢了"
    finally:
        setTheme(original)


def test_cell_padding_still_clears_the_expand_arrow(view):
    """左内边距压不到 10 以下 —— 第 0 列最左那 10px 是展开箭头的地盘。

    实测 depth=d 的 item rect 从 `indentation()*(d+1)` 起（`rootIsDecorated()` 多占一级），
    而 `TreeWidget.viewportEvent` 把箭头的点击区写成 `d*indentation()+20` 起 10px ——
    两者同一个起点。再压下去文字就压在箭头上，那是比"间隔太大"更难看的毛病。
    """
    assert 10 < view.ITEM_PADDING_LEFT < 20


def test_column_zero_indent_formula_matches_where_qt_puts_the_row(view):
    """`_fit_col0()` 里的缩进公式必须和 Qt 实际摆的位置对得上。

    宽度是自己算的（不走 `ResizeToContents`：那会在每次插入时向 delegate 逐行问
    sizeHint，而这棵树能有 `MAX_EVENTS` 行）。既然是自己算，`indentation()*(depth+1)`
    这个式子就得由 Qt 那边的 `visualItemRect()` 来验 —— 多算少算一级缩进，列宽就会
    莫名其妙地差出 20px，而这类偏差看着只像"手感不对"，不会有人想到是公式错了。

    （那个 `+1` 来自 `rootIsDecorated()`：顶层节点自己也占一级缩进放展开箭头。）
    """
    view.add_event(_event("stage", trace=_trace()))

    seen = 0
    for item, depth in _walk_with_depth(view):
        assert view.visualItemRect(item).x() == view.indentation() * (depth + 1)
        seen += 1
    assert seen == 6  # flow / task / run / attempt / [stage] 五级分组 + 叶子


def test_column_zero_hugs_the_widest_row(view):
    """第 0 列的宽度 = 最宽那一行的内容 + 一点呼吸位，不多留。

    原先钉死 264 —— 那是"最深 5 级 + 最长 kind"的宽度，可解析段的事件只有两级，于是
    那些行的「时间线」和「级别」之间空着一百多像素，就是用户看到的"间隔太大"。

    最宽的一行不一定是叶子：分组标签（`flow k72f`）在浅处但字更长，用的还是另一套
    字体（`_tier_font` 是 DemiBold）。所以这里逐行量、取最大，而不是假定叶子最宽。
    """
    view.add_event(_event("signal", stage="parse", trace=FlowTrace(flow_id="k72f")))

    ends = []
    for item, depth in _walk_with_depth(view):
        is_group = item.data(0, timeline_mod._ROLE_PATH) is not None
        metrics = view._tier_metrics if is_group else view._leaf_metrics
        icon = 0 if is_group else view.iconSize().width() + 4
        ends.append(
            view.indentation() * (depth + 1)
            + view.ITEM_PADDING_LEFT
            + icon
            + metrics.horizontalAdvance(item.text(0))
        )

    assert max(ends) + 8 == view.columnWidth(0)


def test_column_zero_grows_for_deeper_trees_and_never_shrinks(view):
    """浅树窄、深树宽，且**只增不减**。

    解析段（`task=-`/`run=-`）的叶子只有两级，下载段有五级；一个宽度伺候两种树，
    要么浅树空一片、要么深树被挤掉。只增不减是刻意的：宽度跟着最新一条事件来回抽，
    读起来比留点空白更难受。
    """
    view.add_event(_event("signal", stage="parse", trace=FlowTrace(flow_id="k72f")))
    shallow = view.columnWidth(0)

    view.add_event(_event("stage", trace=_trace()))
    deep = view.columnWidth(0)
    assert deep > shallow

    # 再来一条浅的，不许缩回去
    view.add_event(_event("config", stage="startup", trace=FlowTrace(flow_id="k72f")))
    assert view.columnWidth(0) == deep


def test_column_zero_stays_inside_its_bounds(view):
    """上限拦住深层树把「详情」列挤没，下限保证空树时几列不挤成一团。"""
    low, high = view.COL0_BOUNDS
    assert view.columnWidth(0) == low  # 空树落在下限

    for kind in EVENT_KINDS:
        view.add_event(_event(kind, trace=_trace()))

    assert low <= view.columnWidth(0) <= high


def test_a_manual_drag_beats_the_auto_fit(view):
    """用户手拖过第 0 列之后就不再自动撑宽 —— 那个宽度是他自己定的。

    自己调 `setColumnWidth()` 也会走 `sectionResized`，靠"新宽度是不是我们刚算出来
    那个"区分：`_fit_col0()` 先赋 `_col0_width` 再调 `setColumnWidth()`。
    """
    view.add_event(_event("signal", stage="parse", trace=FlowTrace(flow_id="k72f")))
    assert view._col0_locked is False

    view.setColumnWidth(0, 300)
    assert view._col0_locked is True

    view.add_event(_event("transition", trace=_trace()))  # 五级深，本来会撑宽
    assert view.columnWidth(0) == 300


def test_resizing_the_view_is_not_mistaken_for_a_manual_drag(view, qt_app):
    """拉窗口只动最后一列（`stretchLastSection`），不该把第 0 列判成"用户拖过"。

    判错的后果是安静的：从此第 0 列再也不跟着内容长，而时间线现在装在一个用户可以
    随手拖大拖小的独立窗口里（`LogViewerWindow`），也就是说一动窗口就废了。
    """
    view.add_event(_event("signal", stage="parse", trace=FlowTrace(flow_id="k72f")))
    before = view.columnWidth(0)

    for width in (700, 1200, 500, 933):
        view.resize(width, 400)
        qt_app.processEvents()

    assert view._col0_locked is False
    assert view.columnWidth(0) == before

    view.add_event(_event("transition", trace=_trace()))
    assert view.columnWidth(0) > before  # 还会跟着内容长


def test_clear_all_returns_column_zero_to_the_floor(view):
    """清屏后要收回下限。

    `_fit_col0()` 只增不减，不收一下的话"清屏"之后那一列还留着上一批深层事件撑出来的
    宽度，空树上就是一片空白 —— 恰好是用户抱怨的那个样子。
    """
    view.add_event(_event("transition", trace=_trace()))
    assert view.columnWidth(0) > view.COL0_BOUNDS[0]

    view.clear_all()
    assert view.columnWidth(0) == view.COL0_BOUNDS[0]


def test_clear_all_respects_a_width_the_user_chose(view):
    """手拖过的宽度连清屏也不该动 —— 那是用户的设置，不是自动算出来的中间状态。"""
    view.setColumnWidth(0, 300)
    view.add_event(_event("transition", trace=_trace()))

    view.clear_all()
    assert view.columnWidth(0) == 300


def test_the_narrow_columns_are_sized_from_their_widest_content(view):
    """「级别」和「时间」按各自最长的内容算，不是随手取的整数。

    「级别」最长是 `CRITICAL`（比例字体，13px Segoe UI 大写约 6.8px/字 ≈ 54px），
    「时间」最长是 `17:31:41`（等宽 Consolas 13px 每字 ≈ 7.15px ≈ 57px）—— 所以
    「时间」比「级别」略宽，这个顺序反过来就说明有人按"看着差不多"改过。
    真实值量不出来（本节开头那段），这里只钉关系。
    """
    assert view.COL_TIME_WIDTH > view.COL_LEVEL_WIDTH
    # 两列都要放得下内容 + 左右内边距，且不能宽到又变成"间隔太大"
    for width in (view.COL_LEVEL_WIDTH, view.COL_TIME_WIDTH):
        assert view.ITEM_PADDING_LEFT + 40 < width < view.COL0_BOUNDS[0]
    assert view.columnWidth(1) == view.COL_LEVEL_WIDTH
    assert view.columnWidth(2) == view.COL_TIME_WIDTH


def test_detail_column_takes_every_pixel_the_others_give_back(view, qt_app):
    """省下来的宽度必须全流进「详情」—— 那一列才是要读的内容。

    原先「详情」被前三列挤到会把 `code=rate_limited_429` 截成 `code=rate_limi…`。
    """
    view.resize(933, 400)
    qt_app.processEvents()
    view.add_event(_event("diagnosis", level="ERROR", trace=_trace()))

    assert view.header().stretchLastSection()
    others = sum(view.columnWidth(i) for i in range(3))
    assert view.columnWidth(3) == view.viewport().width() - others
    assert view.columnWidth(3) > others  # 详情比另外三列加起来还宽


def test_every_column_is_left_aligned(view):
    """四列一律左对齐，包括表头。

    居中过的「级别」/「时间」会在列里浮动 —— `INFO` 短、`WARNING` 长，于是每行的间距
    都不一样宽，扫下来就是"间隔忽大忽小"。左对齐让每列有一条固定的左边缘。
    （`TreeViewBase._initView` 把表头设成了 `AlignCenter`，所以表头这边是在改上游默认值。）
    """
    left = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
    header = view.headerItem()
    for col in range(4):
        assert header.textAlignment(col) == left

    view.add_event(_event("stage", trace=_trace()))
    leaf = next(iter(view._leaves))
    group = view.topLevelItem(0)
    # 单元格不设 TextAlignmentRole：没有覆盖就走视图的默认左对齐，
    # 少一份和表头各说各话的来源
    for item in (leaf, group):
        for col in range(4):
            assert item.data(col, Qt.ItemDataRole.TextAlignmentRole) is None
