"""任务卡片气泡提示的回归。

出过的 bug：鼠标指向卡片标题，弹出来的是**一个纯黑方块**而不是提示文字。

成因是 `TaskListView._show_tooltip` 用了 `QToolTip.showText()` —— 那是裸 Qt 控件，
qfluentwidgets 的样式表不覆盖它（`FluentStyleSheet` 里只有 `TOOL_TIP` 这一项，对应的是
qfluentwidgets 自己的 `ToolTip` 类），于是在暗色主题下它就是无样式的黑底黑字。项目里
另外十几处提示全都走 `ToolTipFilter` → `ToolTip`，只有这里是例外。

用户明确说过这个提示**是功能，不要去掉**（「这个应该是会额外显示标题，不需要去掉」），
所以这里钉的是「同样的触发、同样的文案，换成能看的渲染」：

1. 视图模块里**不许再出现 `QToolTip`** —— 黑方块的唯一来源；
2. 气泡是 qfluentwidgets 的 `ToolTip`，跟随主题；
3. 标题提示给的是**完整标题**（卡片上那份被 elide 过），三颗按钮和复选框各有自己的文案；
4. 锚点是**区域**而不是鼠标点：气泡贴在标题行上方、不遮住它，横向移动鼠标也不跟手
   （裸 `QToolTip` 跟手且落在光标右下方，正是它盖住了进度条那一行）；
5. 六条收起路径都真的收起 —— 气泡是 `WindowStaysOnTopHint` 的独立顶层窗口，
   卡住不消失比早消失难受得多。

需要 `QApplication` 且要真的 `show()`：`visualRect` 只有在视图布局过之后才有几何。
"""

import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 建页面会连带碰 config/DB/日志，数据根照例先指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-tooltip-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE6 = True
try:
    from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, Signal
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication
except ImportError:
    HAS_PYSIDE6 = False

requires_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 required for view tests")

TASK_COUNT = 5

# 卡片上标题一定放不下的长度 —— 提示存在的理由就是把 elide 掉的部分补回来
LONG_TITLE = "非常长的视频标题" * 12


class FakeWorker(QObject):
    """最小 worker：够 `_bind_worker_signals` 连接、够 `TaskRow` 读穿透。

    与 `test_task_list_right_click.py` 里那个同源 —— 项目还没有 conftest.py（见 CLAUDE.md §8），
    共享 fixture 只能靠各自复制。
    """

    progress = Signal(float)
    status_msg = Signal(str)
    unified_status = Signal(str, float, str)
    completed = Signal()
    error = Signal(dict)
    cancelled = Signal()

    def __init__(self, db_id=0, url="", state="paused"):
        super().__init__()
        self.db_id = int(db_id)
        self.url = url
        self.opts = {}
        self.effective_state = state


@pytest.fixture(scope="module")
def qapp():
    """离屏 QApplication。页面是 QWidget，QCoreApplication 不够。"""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def page(qapp, tmp_path, monkeypatch):
    """一个装了 5 行活任务的页面，标题都长到必然被 elide。

    分页和计数都读 `task_db` 单例 —— 必须换成临时库：`migrate_user_data()` 会把真实的
    用户数据根**复制**进 `FLUENTYTDL_DATA_DIR_OVERRIDE`，不换的话真实历史会被分页补进
    列表，这里的行号断言当场失效。
    """
    from fluentytdl.storage import task_db as task_db_mod
    from fluentytdl.storage.task_db import TaskDB
    from fluentytdl.ui.models import download_list_model as dlm_mod
    from fluentytdl.ui.unified_task_list_page import UnifiedTaskListPage

    temp_db = TaskDB(db_path=tmp_path / "tasks.db")
    monkeypatch.setattr(dlm_mod, "task_db", temp_db)  # fetchMore
    monkeypatch.setattr(task_db_mod, "task_db", temp_db)  # 页面 _recount 的局部 import

    p = UnifiedTaskListPage()
    p.resize(1000, 700)
    p.show()
    # add_task 插在第 0 行，所以先加的排在后面
    workers = [FakeWorker(db_id=i + 1, url=f"https://example.com/v{i}") for i in range(TASK_COUNT)]
    for i, worker in enumerate(workers):
        p.add_task(worker, title=f"{LONG_TITLE} {i}", thumbnail="")
    _pump(qapp)
    p._workers = workers  # 保住引用，QObject 被 GC 掉这一行就成了野指针
    yield p
    p.close()
    _pump(qapp)


def _pump(qapp, times: int = 12) -> None:
    for _ in range(times):
        qapp.processEvents()


def _row_rect(page, proxy_row: int = 0) -> QRect:
    index = page.proxy_model.index(proxy_row, 0)
    rect = page.list_view.visualRect(index)
    assert not rect.isEmpty(), f"proxy 行 {proxy_row} 没有几何，视图还没布局"
    return rect


def _hit(page, name: str, proxy_row: int = 0) -> QRect:
    """delegate 自己算的命中区。坐标写死在测试里就没有回归价值了。"""
    return page.delegate._get_hit_rects(_row_rect(page, proxy_row))[name]


def _title_point(page, proxy_row: int = 0, frac: float = 0.25) -> QPoint:
    rect = _row_rect(page, proxy_row)
    title = page.delegate._title_rect(rect, page.delegate._get_hit_rects(rect))
    return QPoint(title.left() + int(title.width() * frac), title.center().y())


def _hover(page, qapp, pos: QPoint) -> None:
    """真的发一个 `QEvent.ToolTip` 到视口 —— 触发时机是交给 Qt 的，别绕过它。"""
    viewport = page.list_view.viewport()
    QApplication.sendEvent(
        viewport, QHelpEvent(QEvent.Type.ToolTip, pos, viewport.mapToGlobal(pos))
    )
    _pump(qapp)


def _tip(page):
    return page.list_view._tooltip


def _visible_box(tip) -> QRect:
    """气泡**可见的**那个圆角框（全局坐标）。

    不是 `tip.geometry()`：`ToolTip` 的外层 layout 留了 (12, 8, 12, 12) 的边距给投影，
    按窗口矩形算位置会得出「浮在半空」的结论。
    """
    box = tip.container
    return QRect(box.mapToGlobal(QPoint(0, 0)), box.size())


def _anchor_global(page, anchor: QRect) -> QRect:
    # anchor 是**视口**坐标（来自 visualRect），必须由 viewport 换算
    return QRect(page.list_view.viewport().mapToGlobal(anchor.topLeft()), anchor.size())


# === 1. 黑方块的根因 ===


@requires_qt
def test_view_module_has_no_raw_qtooltip():
    """视图源码里不许再出现 `QToolTip` **标识符**。

    这就是黑方块本身 —— qfluentwidgets 不给裸 `QToolTip` 上样式表，暗色主题下它是纯黑的。
    钉在源码层而不是行为层：`QToolTip.showText()` 弹出的窗口不属于本进程的控件树，
    没法用断言把它抓出来。

    用 `ast` 而不是逐行找子串：源码里那段「为什么不用 QToolTip」的注释本身就含这个词，
    纯文本扫描会把解释踩成违规，于是唯一的修法变成删掉解释 —— 那不是好交易。
    """
    from fluentytdl.ui.views import task_list_view as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == "QToolTip":
            offenders.append(f"{node.lineno}: 引用了 QToolTip")
        elif isinstance(node, ast.Attribute) and node.attr == "QToolTip":
            offenders.append(f"{node.lineno}: 属性访问 .QToolTip")
        elif isinstance(node, ast.ImportFrom):
            offenders += [
                f"{node.lineno}: from {node.module} import QToolTip"
                for a in node.names
                if a.name == "QToolTip"
            ]
        elif isinstance(node, ast.Import):
            offenders += [
                f"{node.lineno}: import {a.name}" for a in node.names if a.name.endswith("QToolTip")
            ]
    assert not offenders, "裸 QToolTip 又回来了（暗色主题下就是个黑方块）：" + "; ".join(offenders)


@requires_qt
def test_tooltip_is_a_fluent_tooltip(page, qapp):
    """弹出来的必须是 qfluentwidgets 的 `ToolTip` —— 它才跟随 `qconfig.themeChanged`。"""
    from qfluentwidgets import ToolTip

    _hover(page, qapp, _title_point(page))
    assert isinstance(_tip(page), ToolTip)
    assert _tip(page).isVisible()


@requires_qt
def test_tooltip_is_lazy(page):
    """构造时不建气泡：那会儿 `self.window()` 还是页面自己，页面还没挂进 FluentWindow。"""
    assert page.list_view._tooltip is None


@requires_qt
@pytest.mark.parametrize("theme_name", ["LIGHT", "DARK"])
def test_tooltip_is_not_a_black_box(page, qapp, theme_name):
    """数像素：明暗两套主题下气泡都得是「有底色、有对比度」的，不是一块黑。

    前面那些断言都是间接证据（类型对、样式表对）。用户看到的是**颜色**，所以这里把气泡
    渲进 QPixmap 直接量：取出现最多的三种颜色，看背景亮度和最大亮度差。
    黑底黑字的特征就是背景亮度≈0 且几乎没有对比度 —— 那正是修之前裸 `QToolTip` 的样子。
    """
    from PySide6.QtGui import QColor
    from qfluentwidgets import Theme, qconfig, setTheme

    def luminance(c: QColor) -> float:
        return 0.2126 * c.red() + 0.7152 * c.green() + 0.0722 * c.blue()

    view = page.list_view
    original = qconfig.themeMode.value
    try:
        setTheme(getattr(Theme, theme_name))
        _pump(qapp)
        # 样式表跟随 themeChanged，但这里要的是「新主题下从零建一次」的干净结果
        view._tooltip = None
        _hover(page, qapp, _title_point(page))
        tip = _tip(page)
        _pump(qapp)

        img = tip.container.grab().toImage()
        assert img.width() > 0 and img.height() > 0

        hist: dict[tuple[int, int, int], int] = {}
        for y in range(img.height()):
            for x in range(img.width()):
                c = img.pixelColor(x, y)
                key = (c.red(), c.green(), c.blue())
                hist[key] = hist.get(key, 0) + 1
        top = sorted(hist.items(), key=lambda kv: -kv[1])[:3]
        lums = [luminance(QColor(*rgb)) for rgb, _ in top]
        bg, contrast = lums[0], max(lums) - min(lums)

        readable = f"{theme_name}: 背景亮度 {bg:.0f}，对比度 {contrast:.0f}，前三色 {top}"
        assert contrast >= 60, "背景和文字几乎同色，等于没提示 —— " + readable
        assert bg > 12, "背景是纯黑，黑方块又回来了 —— " + readable
        if theme_name == "LIGHT":
            assert bg > 150, "浅色主题下背景应当是亮的 —— " + readable
    finally:
        setTheme(Theme(original) if not isinstance(original, Theme) else original)
        view._tooltip = None
        _pump(qapp)


# === 2. 文案：功能一点没少 ===


@requires_qt
def test_title_tooltip_gives_the_full_title(page, qapp):
    """标题提示给的是完整标题 —— 卡片上那份是 elide 过的，这正是用户说的「额外显示标题」。"""
    rect = _row_rect(page, 0)
    index = page.proxy_model.index(0, 0)
    text, anchor = page.delegate.tooltip_at(rect, _title_point(page), index)

    full = page.model.get_task(0).effective_title
    assert text == full
    assert LONG_TITLE in text
    assert not anchor.isEmpty()

    _hover(page, qapp, _title_point(page))
    assert _tip(page).text() == full
    # 提示存在的理由：卡片上放不下
    assert page.list_view.fontMetrics().horizontalAdvance(full) > anchor.width()


@requires_qt
@pytest.mark.parametrize(
    ("region", "expected"),
    [
        ("checkbox", "选择此任务（可按住 Ctrl / Shift 多选）"),
        ("folder", "打开所在文件夹"),
        ("delete", "删除任务"),
        ("pause", "开始 / 继续"),  # fixture 里的 worker 都是 paused
    ],
)
def test_control_tooltips(page, region, expected):
    """四个控件各有自己的文案，锚点就是各自的命中区。"""
    rect = _row_rect(page, 0)
    index = page.proxy_model.index(0, 0)
    hit = _hit(page, region)

    text, anchor = page.delegate.tooltip_at(rect, hit.center(), index)
    assert text == expected
    assert anchor == hit


@requires_qt
def test_pause_tooltip_follows_state(page, qapp):
    """暂停键的文案随状态走 —— 运行中说「暂停」，已完成说「已完成」。"""
    rect = _row_rect(page, 0)
    index = page.proxy_model.index(0, 0)
    pos = _hit(page, "pause").center()

    page._workers[-1].effective_state = "running"  # 最后加的排在第 0 行
    _pump(qapp)
    assert page.delegate.tooltip_at(rect, pos, index)[0] == "暂停"

    page._workers[-1].effective_state = "completed"
    _pump(qapp)
    assert page.delegate.tooltip_at(rect, pos, index)[0] == "已完成"


@requires_qt
def test_no_tooltip_on_thumbnail(page):
    """缩略图那一段没有文案 —— 返回空是 `_show_tooltip` 收起气泡的信号。"""
    rect = _row_rect(page, 0)
    index = page.proxy_model.index(0, 0)
    thumb_x = _hit(page, "checkbox").right() + 20

    text, anchor = page.delegate.tooltip_at(rect, QPoint(thumb_x, rect.center().y()), index)
    assert text == ""
    assert anchor.isEmpty()


# === 3. 锚点：区域，不是鼠标点 ===


@requires_qt
def test_title_anchor_is_the_title_row_not_the_cursor(page):
    """在标题列里横向挪鼠标，锚点必须一动不动。

    裸 `QToolTip` 是跟手的（气泡落在光标右下方，所以会盖住下面的进度条那一行）；
    `ToolTipFilter` 不跟手。这里对齐后者。
    """
    rect = _row_rect(page, 0)
    index = page.proxy_model.index(0, 0)
    expected = page.delegate._title_rect(rect, page.delegate._get_hit_rects(rect))

    anchors = [
        page.delegate.tooltip_at(rect, _title_point(page, frac=frac), index)[1]
        for frac in (0.05, 0.4, 0.9)
    ]
    assert anchors == [expected, expected, expected]


@requires_qt
def test_title_rect_matches_what_paint_draws(page):
    """`_title_rect` 是 `paint` 和锚点的**同一个**来源。

    两处各写一遍 `+16 / 20` 迟早会漂移，那时气泡就贴不到标题上了 —— 所以这里核对它
    确实落在文本列内、且在行的上半部（标题行的位置）。
    """
    rect = _row_rect(page, 0)
    hits = page.delegate._get_hit_rects(rect)
    title = page.delegate._title_rect(rect, hits)
    text_left, text_width = page.delegate._text_column(rect, hits)

    assert (title.left(), title.width()) == (text_left, text_width)
    assert rect.top() < title.top() < rect.center().y()
    assert title.right() < hits["pause"].left()


@requires_qt
def test_tooltip_does_not_cover_its_anchor(page, qapp):
    """气泡贴在标题行**外面**，不许盖住它。

    黑方块当时就压在第一张卡片的进度条上 —— 一个提示遮住它所描述的东西是最糟的形态。
    """
    rect = _row_rect(page, 0)
    anchor = page.delegate._title_rect(rect, page.delegate._get_hit_rects(rect))
    _hover(page, qapp, _title_point(page))

    box = _visible_box(_tip(page))
    target = _anchor_global(page, anchor)
    assert not box.intersects(target), f"气泡 {box} 压在标题 {target} 上"
    # 上方（放不下时翻到下方），并且是**贴着**的，不是浮在半空
    above = target.top() - box.bottom()
    below = box.top() - target.bottom()
    assert 0 <= above <= 8 or 0 <= below <= 8, f"上间距 {above} / 下间距 {below} 都不合理"
    # 横向对着标题列
    assert target.left() <= box.center().x() <= target.right()


@requires_qt
def test_tooltip_stays_on_screen(page, qapp):
    """夹回屏幕可用区 —— 顶层窗口跑到屏幕外就等于没提示。"""
    _hover(page, qapp, _title_point(page))
    tip = _tip(page)
    bounds = (QApplication.screenAt(tip.pos()) or QApplication.primaryScreen()).availableGeometry()
    assert bounds.contains(tip.geometry()) or bounds.intersects(tip.geometry())
    assert bounds.left() <= tip.x() and tip.x() + tip.width() - 1 <= bounds.right()
    assert bounds.top() <= tip.y() and tip.y() + tip.height() - 1 <= bounds.bottom()


# === 4. 宽度：折行，绝不比屏幕还宽 ===
#
# 这一节钉的是修黑方块时**顺带发现的第二个真 bug**：`ToolTip` 的 label 默认不折行，
# 而标题动辄上百字 —— 实测 98 个汉字排成 1218px 宽的一条，比 1024 宽的屏幕还长，
# `_tooltip_pos` 的夹取对「比屏幕还宽的窗口」无能为力，右半截直接跑到屏幕外。
# 项目里其余十几处提示都是短文案，所以上游从来没碰上这件事。


@requires_qt
def test_long_title_wraps_instead_of_becoming_a_ribbon(page, qapp):
    """长标题折行收进宽度上限，而不是排成一条横穿屏幕的带子。"""
    view = page.list_view
    _hover(page, qapp, _title_point(page))
    tip = _tip(page)

    assert tip.width() <= view.TOOLTIP_MAX_WIDTH, f"气泡宽 {tip.width()}，上限 480"
    assert tip.label.wordWrap(), "长文案必须折行"
    # 真的排成了多行（单行高度 × 1.5 是个宽松的下界，不同字体行距不同）
    assert tip.label.height() > tip.label.fontMetrics().height() * 1.5


@requires_qt
def test_wrapped_tooltip_shows_every_line(page, qapp):
    """折行后 label 的高度当场就要够 —— 不靠「等布局跑一遍」来补。

    wordWrap + 固定宽度下 label 的 `sizeHint()` 走 `sizeForWidth(-1)` 的黄金比例猜测，
    只 `setFixedWidth` 的话它自报一行高（实测 14px，实际需要 3 行 40px），要等布局激活
    才补上。窗口高度倒是 `adjustSize()` 算对的、位置不受影响，所以这里断言的是
    **label 自己的几何在 `_set_tooltip_text` 返回时就自洽**：不赌 `QHBoxLayout` 照顾
    `heightForWidth`，换 Qt 版本也不会悄悄变成裁掉一行。
    """
    view = page.list_view
    tip = view._tooltip_widget()
    view._set_tooltip_text(tip, LONG_TITLE)  # 直接调：要的是「布局跑之前」这一刻

    def _need(label):
        return label.fontMetrics().boundingRect(
            QRect(0, 0, label.width(), 1 << 16), Qt.TextFlag.TextWordWrap, label.text()
        )

    need = _need(tip.label)
    assert need.height() > tip.label.fontMetrics().height() * 1.5, "样本没折成多行，测试就白测了"
    assert tip.label.height() >= need.height(), (
        f"标签高 {tip.label.height()}，排版需要 {need.height()}"
    )
    assert tip.label.width() >= need.width()

    # 走一遍真实路径，确认显示出来的也够高
    _hover(page, qapp, _title_point(page))
    label = _tip(page).label
    assert label.height() >= _need(label).height()


@requires_qt
def test_short_text_after_long_shrinks_back(page, qapp):
    """长 → 短切换后气泡要缩回去，不许留一个空荡荡的大框。

    `setFixedWidth` 之后 `sizeHint()` 被夹住，所以量下一条文案的「自然宽度」之前必须先把
    上一次的约束清掉；不清就会拿着上一条长标题的宽度去排「删除任务」四个字。
    """
    _hover(page, qapp, _title_point(page))
    wide = _tip(page).width()

    _hover(page, qapp, _hit(page, "delete").center())
    tip = _tip(page)
    assert tip.text() == "删除任务"
    assert not tip.label.wordWrap()
    assert tip.width() < wide // 2, f"短文案还占着 {tip.width()}（之前 {wide}）"
    # 宽度就该是这四个字的自然宽度，误差留给 chrome
    natural = tip.label.fontMetrics().horizontalAdvance("删除任务")
    assert tip.width() <= natural + 64


@requires_qt
def test_unbreakable_run_falls_back_to_elide(page, qapp):
    """连续无断点的长串折不开，退回单行省略号 —— 至少不溢出屏幕。

    `Qt::TextWordWrap` 拆不开一个 400 字符的 token：实测排版需要 4800px，硬折行只会
    横向溢出。这种文案本来也没法在一个气泡里读完。
    """
    view = page.list_view
    tip = view._tooltip_widget()
    raw = "a" * 400
    view._set_tooltip_text(tip, raw)

    assert tip.width() <= view.TOOLTIP_MAX_WIDTH
    assert not tip.label.wordWrap()
    assert tip.text() != raw and tip.text().rstrip("…") in raw, "应当是原文的前缀 + 省略号"
    # 单行：没有被裁掉的第二行
    assert tip.label.width() >= tip.label.fontMetrics().horizontalAdvance(tip.text())


@requires_qt
def test_text_width_cap_yields_to_the_screen(page):
    """上限本身还要让位给屏幕 —— 离屏平台的屏幕只有 800px 宽，正好能验到这条。"""
    view = page.list_view
    view._tooltip_widget()  # 先量 chrome
    screen = view.screen() or QApplication.primaryScreen()

    limit = view._tooltip_text_width()
    assert limit + view._tooltip_chrome <= view.TOOLTIP_MAX_WIDTH
    assert limit + view._tooltip_chrome <= screen.availableGeometry().width()


# === 5. 收起路径 ===
#
# 气泡是 `WindowStaysOnTopHint` 的独立顶层窗口：视图隐藏、页面切走、菜单弹出都不会
# 带着它一起消失。4000ms 的自动隐藏只是最后一道网，事件驱动的这几条才是主力。


@requires_qt
def test_hidden_when_moving_to_a_textless_region(page, qapp):
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    rect = _row_rect(page, 0)
    _hover(page, qapp, QPoint(_hit(page, "checkbox").right() + 20, rect.center().y()))
    assert not _tip(page).isVisible()


@requires_qt
def test_hidden_on_blank_space(page, qapp):
    """空白处（最后一行下面）没有 index，也要收起。"""
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    last = _row_rect(page, TASK_COUNT - 1)
    blank = QPoint(last.center().x(), last.bottom() + 40)
    assert not page.list_view.indexAt(blank).isValid()
    _hover(page, qapp, blank)
    assert not _tip(page).isVisible()


@requires_qt
def test_hidden_on_leave(page, qapp):
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    page.list_view.leaveEvent(QEvent(QEvent.Type.Leave))
    _pump(qapp)
    assert not _tip(page).isVisible()


@requires_qt
def test_hidden_on_scroll(page, qapp):
    """滚动一下行就从鼠标底下跑掉了，锚点当场失效。"""
    extra = [FakeWorker(db_id=100 + i, url=f"https://example.com/x{i}") for i in range(20)]
    page._workers.extend(extra)
    for i, worker in enumerate(extra):
        page.add_task(worker, title=f"{LONG_TITLE} extra {i}", thumbnail="")
    _pump(qapp)

    bar = page.list_view.verticalScrollBar()
    assert bar.maximum() > 0, "视图没能滚动，这条测试什么都没验到"

    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    bar.setValue(bar.value() + 60)
    _pump(qapp)
    assert not _tip(page).isVisible()


@requires_qt
def test_hidden_on_mouse_press(page, qapp):
    """按下之后接着可能是勾选、框选或弹菜单，那时提示只是挡视线。"""
    from PySide6.QtTest import QTest

    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    QTest.mousePress(page.list_view.viewport(), Qt.MouseButton.LeftButton, pos=_title_point(page))
    _pump(qapp)
    assert not _tip(page).isVisible()
    QTest.mouseRelease(page.list_view.viewport(), Qt.MouseButton.LeftButton, pos=_title_point(page))


@requires_qt
def test_hidden_when_rows_shift(page, qapp):
    """`add_task` 插进第 0 行 —— 锚点是当时那一行的 visualRect，行号一平移它就指向别人了。"""
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    newcomer = FakeWorker(db_id=99, url="https://example.com/new")
    page._workers.append(newcomer)
    page.add_task(newcomer, title="插队的", thumbnail="")
    _pump(qapp)
    assert not _tip(page).isVisible()


@requires_qt
def test_hidden_when_view_hides(page, qapp):
    """切筛选 Tab / 切页面时视图被隐藏，气泡不会跟着走。"""
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    page.list_view.hide()
    _pump(qapp)
    assert not _tip(page).isVisible()
    page.list_view.show()


@requires_qt
def test_hidden_on_context_menu(page, qapp):
    """右键菜单也是顶层 popup，气泡留着会浮在它上面。"""
    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()

    page.list_view.hide_tooltip()  # contextMenuEvent 走的就是这一条
    _pump(qapp)
    assert not _tip(page).isVisible()

    from PySide6.QtGui import QContextMenuEvent

    _hover(page, qapp, _title_point(page))
    assert _tip(page).isVisible()
    pos = _title_point(page)
    page.list_view.contextMenuEvent(
        QContextMenuEvent(
            QContextMenuEvent.Reason.Mouse, pos, page.list_view.viewport().mapToGlobal(pos)
        )
    )
    _pump(qapp)
    assert not _tip(page).isVisible()


@requires_qt
def test_autohide_timer_is_armed(page, qapp):
    """最后一道网：上面那些全漏了也要自己消失。"""
    _hover(page, qapp, _title_point(page))
    tip = _tip(page)
    assert tip.duration() == page.list_view.TOOLTIP_DURATION_MS > 0
    assert tip.timer.isActive()


@requires_qt
def test_switching_text_restarts_the_fade(page, qapp):
    """换文案要先 hide 再 show：`show()` 对已显示的窗口不再走 showEvent，
    自动隐藏计时器和 150ms 淡入都不会重置。"""
    _hover(page, qapp, _title_point(page))
    first = _tip(page).text()

    _hover(page, qapp, _hit(page, "delete").center())
    tip = _tip(page)
    assert tip.text() != first
    assert tip.text() == "删除任务"
    assert tip.isVisible()
    assert tip.timer.isActive()
