"""左键点在卡片之间的间隙 / 列表留白不清空选中的回归。

出过的 bug：勾了好几张卡（批量条已滑出「已选择 N 项」），手一抖点在两张卡片之间那道
8px 间隙上，整批勾选**一次清光**、批量条缩回去。成因在 `TaskListView.selectionCommand`：
间隙 / 留白落不到任何一行（`indexAt` 无效），旧代码直接把无效 index 交回基类，而
`ExtendedSelection` 的原生语义是资源管理器式的「点空白即清空」，于是 `Clear` 把选中抹平
（且松开时基类会因 `noSelectionOnMousePress` 再问一次，那条路同样返回 `Clear`）。

修好之后：空白处左键单击一动不动，取消整批只走显式手势（批量条「取消选择」、逐张再点、
Ctrl 点选）。这里钉四条：间隙不清、留白不清、空手点间隙不平白开出批量态，以及反面对照——
卡片本体上的左键仍然照常勾选/取消。

`visualRect` 要视图布局过才有几何，所以照例需要 `QApplication` 且真的 `show()`；间隙点是
**向 `visualRect` 求两行之间的空当算出来的**，并当场 assert `indexAt` 落空 —— 坐标猜错会红。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 建页面会连带碰 config/DB/日志，数据根照例先指到临时目录
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-gapclick-test-")
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

# __APPEND_1__


class FakeWorker(QObject):
    """最小 worker：够 `_bind_worker_signals` 连接、够 `TaskRow` 读穿透。

    与 `test_task_list_right_click.py` 里那个同源 —— 项目还没有 conftest.py（见 CLAUDE.md §8），
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


# __APPEND_2__


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
    # 比 5 张卡（≈540px）高出一截，好让「最后一张卡下方还有留白」这条能真的跑起来
    p.resize(1000, 900)
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


# __APPEND_3__


def _pump(qapp, times: int = 12) -> None:
    for _ in range(times):
        qapp.processEvents()


def _click_point(page, proxy_row: int) -> QPoint:
    """行内一个**不落在复选框/按钮上**的点（向 delegate 求证命中区）。"""
    index = page.proxy_model.index(proxy_row, 0)
    rect = page.list_view.visualRect(index)
    assert not rect.isEmpty(), f"proxy 行 {proxy_row} 没有几何，视图还没布局"
    point = rect.center()
    assert not page.delegate.is_interactive_at(rect, point), "取样点落在了卡片控件上"
    return point


def _gap_point(page, upper_row: int, lower_row: int) -> QPoint:
    """两行之间那道间隙的正中。当场 assert `indexAt` 落空 —— 否则坐标算错，测试没有回归价值。"""
    upper = page.list_view.visualRect(page.proxy_model.index(upper_row, 0))
    lower = page.list_view.visualRect(page.proxy_model.index(lower_row, 0))
    assert not upper.isEmpty() and not lower.isEmpty(), "视图还没布局"
    point = QPoint(upper.center().x(), (upper.bottom() + lower.top()) // 2)
    assert not page.list_view.indexAt(point).isValid(), "取样点没落在间隙里（indexAt 命中了某行）"
    return point


def _below_point(page, last_row: int) -> QPoint:
    """最后一张卡下方的留白。列表撑满视口时没有留白可点，直接 skip。"""
    last = page.list_view.visualRect(page.proxy_model.index(last_row, 0))
    assert not last.isEmpty(), "视图还没布局"
    y = last.bottom() + 12
    if y >= page.list_view.viewport().height():
        pytest.skip("视口下方没有留白可点（列表撑满了）")
    point = QPoint(last.center().x(), y)
    assert not page.list_view.indexAt(point).isValid(), "取样点没落在留白里"
    return point


def _click_gap(page, qapp, point: QPoint) -> None:
    QTest.mouseClick(page.list_view.viewport(), Qt.MouseButton.LeftButton, pos=point)
    _pump(qapp)


def _click(page, qapp, proxy_row: int, button) -> None:
    QTest.mouseClick(page.list_view.viewport(), button, pos=_click_point(page, proxy_row))
    _pump(qapp)


def _checked(page) -> list[int]:
    return page._selected_proxy_rows()


# __APPEND_4__


@requires_qt
def test_left_click_gap_between_cards_keeps_selection(page, qapp):
    """核心复现：勾了 3 张，点两卡之间的间隙 —— 3 张必须一张不少，批量态不许塌。

    修复前：间隙 index 无效 → 基类 `Clear` → 选中被抹平成 []；这条断言当场变红。
    """
    for row in (0, 1, 2):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)
    assert _checked(page) == [0, 1, 2]

    _click_gap(page, qapp, _gap_point(page, 1, 2))
    assert _checked(page) == [0, 1, 2]
    assert page.list_view._has_selection is True


@requires_qt
def test_left_click_below_last_card_keeps_selection(page, qapp):
    """列表下方的大片留白同理不清空 —— 与间隙同一条路径（`indexAt` 落空）。"""
    for row in (0, 3):
        _click(page, qapp, row, Qt.MouseButton.LeftButton)
    assert _checked(page) == [0, 3]

    _click_gap(page, qapp, _below_point(page, TASK_COUNT - 1))
    assert _checked(page) == [0, 3]


@requires_qt
def test_left_click_gap_with_nothing_selected_stays_empty(page, qapp):
    """没勾任何东西时点间隙：仍然是空的，也不许平白翻出批量态（复选框在每行冒出来）。"""
    _click_gap(page, qapp, _gap_point(page, 1, 2))
    assert _checked(page) == []
    assert page.list_view._has_selection is False
    assert page.delegate._selection_active is False


@requires_qt
def test_left_click_card_still_toggles(page, qapp):
    """反面对照：卡片本体上的左键仍是纯粹勾选/取消，别把「所有左键」一起改死了。"""
    _click(page, qapp, 1, Qt.MouseButton.LeftButton)
    assert _checked(page) == [1]
    _click(page, qapp, 1, Qt.MouseButton.LeftButton)
    assert _checked(page) == []


# CI 分层标记（见 pyproject [tool.pytest.ini_options] markers）；本地全量 pytest 不受影响，仅 CI 的 -m 过滤用到
pytestmark = pytest.mark.windows_gui
