"""右键卡片与勾选卡片的解耦回归。

出过的 bug：右键一张卡片，**右键菜单和勾选同时发生** —— 复选框在每一行冒出来、
批量条滑进来；已经勾了 N 个时右键第 N+1 张卡还会把那 N 个全清掉。

成因是 `TaskListView` 开了 `ListView.setSelectRightClickedRow(True)`：
`ListBase.mousePressEvent` 会把右键也交给 `QListView.mousePressEvent`，于是 Qt 的
`extendedSelectionCommand` 对未选中行返回 `ClearAndSelect`。这是用户明确禁掉过的
「点一个另一个勾选就消失」的同一个毛病，只是换了一扇门进来。

所以这里钉四条性质：

1. **右键绝不动选中集合** —— 无论落在已勾选行、未勾选行还是空白处；
2. 菜单的**作用域**改由 `_menu_scope` 判定：已勾选行/空白 → 整批，未勾选行 → 只它一行；
3. 作用域的第二项是**延迟求值**的解析器，能扛住 `add_task` 往第 0 行插队带来的行号平移
   （`RoundMenu.exec()` 不阻塞，菜单挂着的这段时间行号真的会变）；
4. 被右键的那一行有**独立的视觉焦点通道**（`_context_row`）—— 菜单是抢鼠标的 popup，
   视图一收到 `Leave` 悬停高亮就没了，而菜单里正摆着「连同文件一起删除」。

需要 `QApplication` 且要真的 `show()`：`visualRect` 只有在视图布局过之后才有几何，
而 1、2 两条是靠**真实鼠标事件**验的，不是靠直接调 `selectionModel().select()`。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 建页面会连带碰 config/DB/日志，数据根照例先指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-rclick-test-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE6 = True
try:
    from PySide6.QtCore import QObject, QPoint, Qt, Signal
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
except ImportError:
    HAS_PYSIDE6 = False

requires_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 required for view tests")

TASK_COUNT = 5


class FakeWorker(QObject):
    """最小 worker：够 `_bind_worker_signals` 连接、够 `TaskRow` 读穿透。

    与 `test_task_row_model.py` 里那个同源 —— 项目还没有 conftest.py（见 CLAUDE.md §8），
    共享 fixture 只能靠各自复制。**故意不预置** `progress_val` / `status_text`：真实
    `DownloadWorker` 也要等第一次进度回调才建这些属性。
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
    """一个装了 5 行活任务的页面，行号与 source 行号一一对应。

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
        p.add_task(worker, title=f"标题 {i}", thumbnail="")
    _pump(qapp)
    p._workers = workers  # 保住引用，QObject 被 GC 掉这一行就成了野指针
    yield p
    p.close()
    _pump(qapp)


def _pump(qapp, times: int = 12) -> None:
    for _ in range(times):
        qapp.processEvents()


def _click_point(page, proxy_row: int) -> QPoint:
    """行内一个**不落在复选框/按钮上**的点。

    坐标是猜的就没有回归价值：控件命中区一改，测试要么假绿要么假红。所以这里向
    delegate 求证一次 —— `is_interactive_at` 正是视图自己用来区分「点卡片」和
    「点卡片里的控件」的那个函数。
    """
    index = page.proxy_model.index(proxy_row, 0)
    rect = page.list_view.visualRect(index)
    assert not rect.isEmpty(), f"proxy 行 {proxy_row} 没有几何，视图还没布局"
    point = rect.center()
    assert not page.delegate.is_interactive_at(rect, point), "取样点落在了卡片控件上"
    return point


def _click(page, qapp, proxy_row: int, button) -> None:
    QTest.mouseClick(page.list_view.viewport(), button, pos=_click_point(page, proxy_row))
    _pump(qapp)


def _checked(page) -> list[int]:
    return page._selected_proxy_rows()


# === 1. 右键绝不动选中集合 ===


@requires_qt
def test_select_right_clicked_row_is_off(page):
    """钉住开关本身。这一条一旦被人「顺手打开」，下面三条才会失败得莫名其妙。"""
    assert page.list_view._isSelectRightClickedRow is False


@requires_qt
def test_right_click_unchecked_row_keeps_existing_checks(page, qapp):
    """勾了 3 个再右键第 4 张卡：那 3 个必须**一个不少**，也不许多出第 4 个。

    这就是原 bug 的最短复现 —— `ClearAndSelect` 会把 3 个勾选清成 1 个。
    """
    for row in (0, 1, 2):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)
    assert _checked(page) == [0, 1, 2]

    _click(page, qapp, 3, Qt.MouseButton.RightButton)
    assert _checked(page) == [0, 1, 2]


@requires_qt
def test_right_click_checked_row_keeps_it_checked(page, qapp):
    """右键已勾选的行也不许**取消**它 —— 松开时基类会再问一次 selectionCommand。"""
    for row in (1, 2):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)
    assert _checked(page) == [1, 2]

    _click(page, qapp, 2, Qt.MouseButton.RightButton)
    assert _checked(page) == [1, 2]


@requires_qt
def test_right_click_with_nothing_checked_selects_nothing(page, qapp):
    """空手右键不许开出批量态：`_has_selection` 一翻转复选框就会在每一行冒出来。"""
    _click(page, qapp, 2, Qt.MouseButton.RightButton)
    assert _checked(page) == []
    assert page.list_view._has_selection is False
    assert page.delegate._selection_active is False


@requires_qt
def test_left_click_still_toggles(page, qapp):
    """反面对照：左键仍然是纯粹的勾选/取消，否则上面几条可能只是「点击整个失效了」。"""
    _click(page, qapp, 1, Qt.MouseButton.LeftButton)
    assert _checked(page) == [1]
    _click(page, qapp, 1, Qt.MouseButton.LeftButton)
    assert _checked(page) == []


# === 2. 作用域判定 ===


@requires_qt
def test_scope_on_checked_row_is_whole_selection(page, qapp):
    """右键已勾选的行 → 整批。「勾了 3 个，右键其中一个，对 3 个都生效」。"""
    for row in (0, 2, 4):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)

    scope = page._menu_scope(2)
    assert scope is not None
    rows, resolve = scope
    assert rows == [0, 2, 4]
    assert resolve() == [0, 2, 4]


@requires_qt
def test_scope_on_unchecked_row_is_that_row_only(page, qapp):
    """右键未勾选的行 → 只它一行，且勾选集合一根汗毛都不动。"""
    for row in (0, 1):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)

    scope = page._menu_scope(3)
    assert scope is not None
    rows, resolve = scope
    assert rows == [3]
    assert resolve() == [3]
    # 求作用域这个动作本身也不许碰选中
    assert _checked(page) == [0, 1]


@requires_qt
def test_scope_on_blank_space_uses_selection(page, qapp):
    """空白处右键 → 整批（与批量条上的动作同作用域）。"""
    for row in (1, 3):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)

    scope = page._menu_scope(-1)
    assert scope is not None
    rows, resolve = scope
    assert rows == [1, 3]
    assert resolve() == [1, 3]


@requires_qt
def test_scope_on_blank_space_without_selection_is_none(page):
    """空白 + 一个都没勾 → 没有作用域，菜单干脆不弹（而不是弹一个作用于零行的菜单）。"""
    assert page._menu_scope(-1) is None


# === 3. 解析器抗行号平移 ===


@requires_qt
def test_single_row_resolver_survives_insert(page, qapp):
    """菜单挂着时 `add_task` 插进第 0 行 —— 解析器必须仍然指向**同一个任务**。

    `RoundMenu.exec()` 不阻塞，所以「建菜单时把行号快照下来」在真机上是会指错行的：
    下载完一个、后台又新建一个，行号就整体下移了。
    """
    target = page._row_at(2)
    assert target is not None

    scope = page._menu_scope(2)
    assert scope is not None
    _, resolve = scope
    assert resolve() == [2]

    newcomer = FakeWorker(db_id=99, url="https://example.com/new")
    page._workers.append(newcomer)
    page.add_task(newcomer, title="插队的", thumbnail="")
    _pump(qapp)

    assert resolve() == [3]
    assert page.model.get_task(3) is target


@requires_qt
def test_selection_resolver_survives_insert(page, qapp):
    """整批作用域同理 —— Qt 自己会平移 selection，解析器每次重新问它就够了。"""
    for row in (0, 1):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)
    targets = [page._row_at(row) for row in (0, 1)]

    scope = page._menu_scope(0)
    assert scope is not None
    _, resolve = scope

    newcomer = FakeWorker(db_id=99, url="https://example.com/new")
    page._workers.append(newcomer)
    page.add_task(newcomer, title="插队的", thumbnail="")
    _pump(qapp)

    assert resolve() == [1, 2]
    assert [page.model.get_task(row) for row in resolve()] == targets


@requires_qt
def test_single_row_resolver_empty_after_delete(page, qapp):
    """目标行被删掉 → 解析器返回空表。

    各批量槽都有「空集合直接 return」的守卫（`on_batch_delete` 的 `if not row_objs`、
    `on_batch_start` 的 `if not startable`），所以空表是安全的降级；退化成「行号 2」
    才不是 —— 那会删掉一个无关的任务。
    """
    scope = page._menu_scope(2)
    assert scope is not None
    _, resolve = scope

    page.model.remove_task(2)
    _pump(qapp)

    assert resolve() == []


# === 4. 视觉焦点通道 ===


@requires_qt
def test_context_row_reaches_delegate(page, qapp):
    """`_context_row` 必须落到 delegate 上，否则那一行毫无标记。"""
    page.list_view.set_context_row(3)
    assert page.delegate._context_row == 3
    page.list_view.set_context_row(-1)
    assert page.delegate._context_row == -1


@requires_qt
def test_context_row_is_not_selection(page, qapp):
    """点亮 ≠ 勾选。它只是视觉焦点，不许让批量条滑出来。"""
    page.list_view.set_context_row(3)
    _pump(qapp)
    assert _checked(page) == []
    assert page.list_view._has_selection is False


@requires_qt
def test_context_row_survives_leave_event(page, qapp):
    """菜单是抢鼠标的 popup：视图收到 `Leave` 会把悬停行清成 -1，`_context_row` 不能跟着掉。"""
    from PySide6.QtCore import QEvent

    page.list_view.set_context_row(3)
    page.list_view.leaveEvent(QEvent(QEvent.Type.Leave))
    _pump(qapp)

    assert page.list_view._hover_row == -1
    assert page.delegate._context_row == 3


@requires_qt
def test_context_menu_lights_row_and_clears_on_close(page, qapp):
    """端到端：弹菜单点亮该行，菜单一关就复位。

    `closedSignal` 覆盖 Esc / 点外面 / 点菜单项三条关闭路径（`_hideMenu` 一律走
    `close()`），所以只验其中一条就够。
    """
    page._on_context_menu(2, page.list_view.viewport().mapToGlobal(_click_point(page, 2)))
    _pump(qapp)
    assert page.list_view._context_row == 2
    assert page.delegate._context_row == 2
    # 右键菜单弹出来了，但选中集合仍然是空的 —— 这就是「两者不再同时发生」
    assert _checked(page) == []

    page._context_menu.close()
    _pump(qapp)
    assert page.list_view._context_row == -1
    assert page.delegate._context_row == -1


@requires_qt
def test_context_row_cleared_when_rows_shift(page, qapp):
    """行号平移了就丢掉高亮 —— 指错行比没有高亮危险得多。"""
    page.list_view.set_context_row(2)

    newcomer = FakeWorker(db_id=99, url="https://example.com/new")
    page._workers.append(newcomer)
    page.add_task(newcomer, title="插队的", thumbnail="")
    _pump(qapp)

    assert page.list_view._context_row == -1
    assert page.delegate._context_row == -1
