"""任务页顶栏那一行的几何回归。

这一行挤了 12 个东西：4 个主 tab 的 pivot、「更多筛选」下拉、可关闭的筛选 chip、
并发数标签 + 下拉框、排序按钮，最后是装着 5 个全局动作的 `CommandBar`。它出过的 bug
是**控件互相错叠** —— 选中一个二级筛选后 chip 出现，pivot 直接画到「更多筛选」按钮上。

成因不是 chip，而是这一行的**最小宽超过了可用宽**：

* `SegmentedWidget.hBoxLayout` 是 `SetMinimumSize`、控件本身是 `QSizePolicy.Minimum`，
  所以逐项 `setMinimumWidth` 会一路传导成 pivot 自己的**硬**最小宽；
* 旧的 `CommandBar` 用 `resizeToSuitableWidth()`（即 `setFixedWidth`）定宽，把最小宽
  一起钉死，于是整行没有任何可压缩的余量；
* 一旦 `header_layout.minimumSize().width()` 超出可用宽，`QHBoxLayout` 会把某个格子压到
  它的最小宽以下，而 `QWidget.setGeometry` 又把控件反弹回最小宽 —— 控件于是画到邻居
  身上。这就是错叠。

所以这里钉的不是「像素长这样」，而是三条结构性质：
1. 在**窗口最小宽**下整行的最小宽仍有余量（错叠的充分必要条件就是这条被破坏）；
2. 任意宽度、有无 chip、导航展开与否，相邻控件都不重叠、也不越过右边距；
3. 行内存在**降级路径**：放不下时由 `CommandBar` 把尾部动作收进「更多」菜单，
   而不是去压邻居。

顺带钉住 pivot 宽度预留「只涨不落」和胶囊（indicator）跟随 —— 计数写在 tab 文字里，
文字一变宽整行就重排，这是上面那条余量最容易被吃掉的地方。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 建 MainWindow 会连带碰 config/DB/日志，数据根照例先指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-header-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE6 = True
try:
    from PySide6.QtCore import QEventLoop, QPropertyAnimation, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    HAS_PYSIDE6 = False

requires_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 required for layout tests")

# `UnifiedTaskListPage.v_layout` 的左右边距，各 20px
PAGE_MARGIN = 20

# 顶栏里按视觉顺序排列的页面级控件（`command_bar` 由主窗口塞进 `action_layout`，单独取）
ROW_WIDGETS = (
    "pivot",
    "more_filter_btn",
    "filter_chip",
    "concurrent_label",
    "concurrent_box",
    "sort_button",
)

# `_format_count` 的显示上限是 999+，给个远超上限的数就能拿到最长的计数文案
OVER_MAX_COUNT = 99999


@pytest.fixture(scope="module")
def qapp():
    """离屏 QApplication。"""
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture(scope="module")
def window(qapp):
    """真实的 MainWindow —— 顶栏的 `CommandBar` 是主窗口塞进来的，页面单独建量不到它。

    页面宽度也只有真窗口才准（导航栏折叠态占 48px 左右），硬编码一个「可用宽」等于
    把这条回归的基准写死成猜测值。
    """
    from fluentytdl.ui.reimagined_main_window import MainWindow

    # 首次运行向导是模态对话框，无头下会把测试挂死
    original_check = MainWindow.check_first_run
    MainWindow.check_first_run = lambda self: None
    try:
        w = MainWindow()
        w.show()
        # FluentWindow 的页面在成为 QStackedWidget 的当前页之前永远不会被布局
        w.switchTo(w.task_page)
        _pump(qapp)
        yield w
        # 无头下 `tray_icon.isVisible()` 恒为真，`w.close()` 会走「隐藏到托盘」分支、
        # 直接 `event.ignore()`，跳过 `closeEvent` 里的 `_stop_theme_listener()`。于是
        # `SystemThemeListener` 这条**原生 QThread**（阻塞在 Win32 注册表通知上，
        # `threading.enumerate()` 根本看不到它）被留到解释器 finalize —— Python 3.10
        # 下一条仍在跑的原生线程会让进程在收尾时段错误（3.12 的 finalize 恰好容忍，
        # 所以这条只在 3.10 的 CI 上炸）。显式停掉它再退出，别把活线程丢给进程终结。
        w._stop_theme_listener()
        w.close()
        _pump(qapp)
    finally:
        MainWindow.check_first_run = original_check


@pytest.fixture
def page(window, qapp):
    """每个用例都从「窗口最小宽 + 导航折叠 + 全部 + 无计数」的干净基线起步。

    几何用例互相之间会串味（宽度、导航展开态、tab 文字都是窗口级状态），基线必须显式
    复位 —— 尤其是导航折叠，它一旦留在展开态，后面每个用例的可用宽都少 142px。
    """
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH

    p = window.task_page
    _set_nav_expanded(window, qapp, False)
    window.resize(MIN_WINDOW_WIDTH, 820)
    p.set_filter("all")
    p._counts = {}
    p._refresh_counts()
    _pump(qapp)
    # 上一个用例可能把顶栏留在降级态；resize 到相同宽度不会触发 resizeEvent，
    # 显式复位一次（放在 pump 之后：判定要读的最小宽是延迟生效的，见
    # `_schedule_responsive_header`）
    p._apply_responsive_header()
    _pump(qapp)
    yield p


def _pump(qapp, times: int = 20) -> None:
    for _ in range(times):
        qapp.processEvents()


def _settle_ani(ani, qapp, timeout_ms: int = 2000) -> None:
    """等一条 QPropertyAnimation 自然结束。

    `processEvents` 不推进 Qt 的动画时钟，光靠泵事件会量到动画中途的几何 ——
    那是采样错误，不是错位。
    """
    if ani.state() != QPropertyAnimation.State.Running:
        return
    loop = QEventLoop()
    ani.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    ani.finished.disconnect(loop.quit)
    _pump(qapp)


def _set_nav_expanded(window, qapp, expanded: bool) -> None:
    """把侧边栏切到指定状态并等它真的到位（展开可以免动画，收起只有动画版）。"""
    panel = window.navigationInterface.panel
    if expanded:
        panel.expand(useAni=False)
    else:
        panel.collapse()
        _settle_ani(panel.expandAni, qapp)
    _pump(qapp)


def _settle_indicator(page, qapp) -> None:
    _settle_ani(page.pivot.slideAni, qapp)


def _x(value) -> float:
    """`SegmentedWidget` 的指示器几何是个裸 x 浮点，`Pivot` 的是 QRectF —— 都收成 x。"""
    return float(value.x() if hasattr(value, "x") else value)


def _boxes(page, window) -> list[tuple[str, int, int]]:
    """顶栏可见控件的 (名字, 左, 右)，按视觉顺序。

    `QWidget.geometry()` 已经是父坐标系里的值，不要再 `mapTo` 一次（会重复计入偏移）。
    """
    out = []
    for name in ROW_WIDGETS:
        widget = getattr(page, name)
        if widget.isVisible():
            geo = widget.geometry()
            out.append((name, geo.x(), geo.right()))
    geo = window.task_command_bar.geometry()
    out.append(("command_bar", geo.x(), geo.right()))
    return out


def _overlaps(boxes: list[tuple[str, int, int]]) -> list[str]:
    return [
        f"{boxes[i][0]}(right={boxes[i][2]}) 压住了 {boxes[i + 1][0]}(x={boxes[i + 1][1]})"
        for i in range(len(boxes) - 1)
        if boxes[i + 1][1] <= boxes[i][2]
    ]


def _available(page) -> int:
    return page.width() - 2 * PAGE_MARGIN


def _assert_row_sane(page, window) -> None:
    boxes = _boxes(page, window)
    assert not _overlaps(boxes), "; ".join(_overlaps(boxes))
    right_limit = page.width() - PAGE_MARGIN
    assert boxes[-1][2] <= right_limit, (
        f"{boxes[-1][0]} 右边缘 {boxes[-1][2]} 越过了页面右边距 {right_limit}"
    )


# === 核心不变量：最小窗口宽下整行的最小宽必须有余量 ===


@requires_qt
@pytest.mark.parametrize("nav_expanded", [False, True], ids=["nav-collapsed", "nav-expanded"])
@pytest.mark.parametrize("filter_key", ["all", "quality_guard"])
def test_header_row_minimum_fits_at_minimum_window_width(
    page, window, qapp, filter_key, nav_expanded
):
    """窗口最小宽下，整行的**最小宽**要塞得进页面可用宽。

    这条一破，`QHBoxLayout` 就只剩「压别人」一条路，错叠随之出现。两个维度都是当初
    把整行推过临界点的因素：`quality_guard` 会额外显示可关闭的筛选 chip，
    而侧边栏展开是**真的占页面宽**的（见 `test_expanded_navigation_costs_page_width`）。
    """
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH

    window.resize(MIN_WINDOW_WIDTH, 820)
    _set_nav_expanded(window, qapp, nav_expanded)
    page.set_filter(filter_key)
    _pump(qapp)

    assert page.filter_chip.isVisible() is (filter_key == "quality_guard")
    minimum = page.header_layout.minimumSize().width()
    assert minimum <= _available(page), (
        f"筛选={filter_key} 导航展开={nav_expanded} 时整行最小宽 {minimum} "
        f"超过可用宽 {_available(page)}"
    )
    _assert_row_sane(page, window)


@requires_qt
@pytest.mark.parametrize("width", [1150, 1280, 1440, 1700])
@pytest.mark.parametrize("filter_key", ["all", "quality_guard"])
def test_header_row_never_overlaps(page, window, qapp, width, filter_key):
    """各种宽度 × 有无 chip，相邻控件都不许重叠。"""
    window.resize(width, 820)
    _pump(qapp)
    page.set_filter(filter_key)
    _pump(qapp)
    _assert_row_sane(page, window)


@requires_qt
def test_expanded_navigation_costs_page_width(page, window, qapp):
    """侧边栏展开会**挤窄页面**，不是浮在上面 —— 宽度预算必须按展开态算。

    钉住这条是因为很容易想反：`NavigationDisplayMode.MENU`（窄窗口）下面板确实是浮层，
    但 `EXPAND` 模式下它占布局宽度。所以顶栏的宽度余量不是「窗口最小宽减折叠导航」，
    而要再减掉 `expandWidth - 48`；量的时候若只泵事件不等动画结束，会误以为页面没变。
    """
    from qfluentwidgets.components.navigation.navigation_panel import NavigationDisplayMode

    from fluentytdl.ui.components.common.interruptible_navigation import COMPACT_WIDTH

    panel = window.navigationInterface.panel
    collapsed_width = page.width()

    _set_nav_expanded(window, qapp, True)
    assert panel.displayMode == NavigationDisplayMode.EXPAND
    cost = collapsed_width - page.width()
    assert cost == panel.expandWidth - COMPACT_WIDTH, (
        f"展开态占用的页面宽是 {cost}，预期 {panel.expandWidth - COMPACT_WIDTH}"
        "（若为 0 说明它变成了浮层，宽度预算的假设需要重新验证）"
    )
    _assert_row_sane(page, window)


# === 降级路径：放不下时由 CommandBar 折叠，而不是压邻居 ===


@requires_qt
def test_command_bar_is_compressible(window):
    """`CommandBar` 必须「首选宽 = 全摆得开、最小宽 = 只剩更多按钮」。

    `resizeToSuitableWidth()` 是 `setFixedWidth`，会让最小宽 == 首选宽，整行再没有
    可压缩的余量。真回归了的话这里会先炸，而不是等到 GUI 上看见错叠。
    """
    bar = window.task_command_bar
    assert bar.sizeHint().width() == bar.suitableWidth(), (
        "首选宽应等于 suitableWidth（一颗都不折叠的临界值）"
    )
    assert bar.minimumSizeHint().width() < bar.sizeHint().width(), "最小宽必须小于首选宽"
    assert bar.minimumWidth() < bar.suitableWidth(), "不要用 setFixedWidth 把最小宽钉死"


@requires_qt
def test_command_bar_folds_when_squeezed(window, qapp):
    """被压窄时应该把尾部动作收进「更多」菜单，压力不传导给邻居。"""
    bar = window.task_command_bar
    original = bar.width()
    try:
        assert not bar._hiddenWidgets, "宽度足够时不该有折叠项"
        bar.resize(bar.minimumSizeHint().width() + 20, bar.height())
        _pump(qapp)
        assert bar._hiddenWidgets, "压到最小宽附近却没有任何动作被折叠"
        assert bar.moreButton.isVisible(), "有折叠项时「更多」按钮必须可见"
    finally:
        bar.resize(original, bar.height())
        _pump(qapp)


# === 计数变化不能吃掉余量、不能让胶囊错位 ===


@requires_qt
def test_pivot_reservation_never_shrinks(page, qapp):
    """预留宽度只涨不落 —— 计数回落时 tab 不该跟着缩，否则整行会来回抖。"""
    from fluentytdl.ui.unified_task_list_page import _MAIN_BUCKETS

    page._counts = dict.fromkeys(_MAIN_BUCKETS, OVER_MAX_COUNT)
    page._refresh_counts()
    _pump(qapp)
    peak = {k: page.pivot.widget(k).width() for k in _MAIN_BUCKETS}

    page._counts = dict.fromkeys(_MAIN_BUCKETS, 3)
    page._refresh_counts()
    _pump(qapp)

    for key in _MAIN_BUCKETS:
        assert page.pivot.widget(key).width() == peak[key], f"{key} 的宽度回落了"
        assert page.pivot.widget(key).minimumWidth() >= peak[key]


@requires_qt
def test_worst_case_counts_still_fit_at_minimum_width(page, window, qapp):
    """最坏情况：窗口最小宽 + 侧边栏展开 + chip 显示 + 4 个 tab 全是 999+。

    这一格是唯一连 `CommandBar` 全折叠都补不上的（还差约 44px），只能由
    `_apply_responsive_header()` 把「并发下载数:」标签让出来 —— 所以这里除了钉住
    「摆得下」，还要钉住**让宽度的是哪一步**，否则将来有人删掉那段降级逻辑，
    这条用例只会以一句「最小宽超了」告终，而看不出是谁的责任。
    """
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH
    from fluentytdl.ui.unified_task_list_page import _MAIN_BUCKETS

    window.resize(MIN_WINDOW_WIDTH, 820)
    _set_nav_expanded(window, qapp, True)
    page.set_filter("quality_guard")
    page._counts = dict.fromkeys(_MAIN_BUCKETS, OVER_MAX_COUNT)
    page._refresh_counts()
    _pump(qapp)

    minimum = page.header_layout.minimumSize().width()
    assert minimum <= _available(page), f"最坏计数下整行最小宽 {minimum} > {_available(page)}"
    assert page._header_label_collapsed, "最坏情况下「并发下载数:」标签本应被收起"
    assert page.concurrent_box.toolTip(), "标签藏了，下拉框必须自带说明"
    _assert_row_sane(page, window)


@requires_qt
def test_concurrent_label_returns_when_there_is_room(page, window, qapp):
    """宽度回来了标签就要回来 —— 降级是临时的，不是单向的。"""
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH
    from fluentytdl.ui.unified_task_list_page import _MAIN_BUCKETS

    window.resize(MIN_WINDOW_WIDTH, 820)
    _set_nav_expanded(window, qapp, True)
    page.set_filter("quality_guard")
    page._counts = dict.fromkeys(_MAIN_BUCKETS, OVER_MAX_COUNT)
    page._refresh_counts()
    _pump(qapp)
    assert page._header_label_collapsed, "前置条件不成立：标签没有被收起"

    window.resize(1700, 820)
    _pump(qapp)

    assert not page._header_label_collapsed, "宽度足够时标签应该恢复"
    _assert_row_sane(page, window)


@requires_qt
def test_indicator_follows_current_tab_when_counts_grow(page, qapp):
    """当前 tab 不是第一个时，前面的 tab 变宽必须把胶囊一起带走。

    `setItemText` 不重新同步 `slideAni`，`setCurrentItem` 遇到同一个 routeKey 又会提前
    return —— 少了 `_resync_pivot_indicator()` 这一刀，胶囊就会停在旧坐标上。
    """
    page.pivot.setCurrentItem("completed")
    _settle_indicator(page, qapp)

    page._counts = {"all": OVER_MAX_COUNT, "active": OVER_MAX_COUNT, "completed": 5, "failed": 0}
    page._refresh_counts()
    _settle_indicator(page, qapp)

    actual = _x(page.pivot.slideAni.value())
    expected = _x(page.pivot.currentIndicatorGeometry())
    assert abs(actual - expected) <= 1, f"胶囊在 x={actual:.0f}，当前 tab 在 x={expected:.0f}"
