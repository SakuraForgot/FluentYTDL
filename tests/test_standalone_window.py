"""`StandaloneWindow` —— 独立非模态窗口的公共外壳。

日志查看器和消息中心都从"遮罩对话框 / 浮窗"改成了独立窗口。改完之后多出三类**只在
非模态窗口上才存在**的失效方式，而它们在 GUI 里全都是安静的：

1. **连点入口开出两扇窗** —— 对话框有 `exec()` 挡着，非模态窗口没有。两扇日志窗口
   各自订阅一份日志信号，写进去的行数就翻倍；
2. **开到屏幕外面** —— 主窗口可以横跨两块屏、也可以有一半拖出屏幕，照它的中心摆就
   会把新窗口放到看不见的地方。用户既看不见，也不知道为什么点了没反应；
3. **不跟主题** —— `FramelessWindow` 是裸 `QWidget`，不接 `themeChanged` 就永远是系统
   默认灰，深色模式下和内容糊成一片。

需要 QApplication（构造的是真实无边框窗口），走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-stdwin-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtCore import QEvent, QSize, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from qfluentwidgets import Theme, setTheme  # noqa: E402

from fluentytdl.ui.components.common.standalone_window import (  # noqa: E402
    StandaloneWindow,
    find_main_window,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


class _Probe(StandaloneWindow):
    """最小子类。基类本身也能实例化，但单例表是按**子类**分桶的，得有个真实子类才测得到。"""


class _OtherProbe(StandaloneWindow):
    SIZE_RATIO = 0.5
    SIZE_BOUNDS = ((300, 400), (200, 300))
    MIN_SIZE = (200, 150)


@pytest.fixture(autouse=True)
def clean_singletons():
    """每个用例前后都把单例表清空 —— 它是类属性，会漏到下一个用例里。"""
    StandaloneWindow._instances.clear()
    yield
    for window in list(StandaloneWindow._instances.values()):
        window.close()
    StandaloneWindow._instances.clear()
    QApplication.processEvents()


@pytest.fixture
def anchor(qt_app):
    w = QWidget()
    w.resize(1000, 700)
    yield w
    w.deleteLater()


# ── 单例 ────────────────────────────────────────────────────


def test_second_open_reuses_the_same_window(qt_app, anchor):
    """连点入口不许叠出第二扇窗。

    两扇日志窗口各自 `log_signal_handler.log_received.connect(...)`，同一条日志就被
    写两遍；而它们叠在同一个位置，用户看不出有两扇，只看到每行日志都重复。
    """
    first = _Probe.show_singleton(anchor)
    second = _Probe.show_singleton(anchor)

    assert first is second
    assert _Probe.singleton() is first


def test_each_subclass_has_its_own_singleton(qt_app, anchor):
    """单例表按子类分桶：开着日志窗口时点铃铛，不该把日志窗口顶掉。"""
    log_like = _Probe.show_singleton(anchor)
    notif_like = _OtherProbe.show_singleton(anchor)

    assert log_like is not notif_like
    assert _Probe.singleton() is log_like
    assert _OtherProbe.singleton() is notif_like


def test_closing_deregisters_immediately(qt_app, anchor):
    """`closeEvent` 里就摘掉，不能只靠 `destroyed`。

    `WA_DeleteOnClose` 走的是 `deleteLater()`，真正销毁要等下一轮事件循环。中间这段
    时间里再点一次入口，`show_singleton()` 会拿到一个正在等死的窗口 —— `show()` 得
    出来的是一扇随即消失的空窗。
    """
    window = _Probe.show_singleton(anchor)
    window.close()  # 刻意不 processEvents()：正是要验证销毁之前就已经注销了

    assert _Probe.singleton() is None

    reopened = _Probe.show_singleton(anchor)
    assert reopened is not window


def test_reopen_after_the_window_was_destroyed(qt_app, anchor):
    """C++ 侧已经回收、`destroyed` 还没跑到时，`show_singleton()` 要能兜住。

    直接 `show()` 一个壳会抛 `RuntimeError: Internal C++ object already deleted`，
    而这是一条用户点得到的路径。
    """
    window = _Probe(anchor=anchor)
    StandaloneWindow._instances[_Probe] = window
    window.deleteLater()
    QApplication.processEvents()

    fresh = _Probe.show_singleton(anchor)
    assert fresh is not None
    assert _Probe.singleton() is fresh


# ── 几何 ────────────────────────────────────────────────────


def test_initial_size_is_a_ratio_of_the_reference():
    assert _OtherProbe._initial_size(QSize(800, 500)) == (400, 250)


def test_initial_size_is_clamped_at_both_ends():
    """钉死一个尺寸不可能同时适合 1366 宽的笔记本和 4K 屏。"""
    assert _OtherProbe._initial_size(QSize(4000, 3000)) == (400, 300)
    assert _OtherProbe._initial_size(QSize(100, 100)) == (300, 200)


def test_window_is_clamped_onto_the_screen(qt_app):
    """参照窗口跑到屏幕外时，新窗口仍要开在屏幕里。

    非模态窗口没有遮罩兜着：开在看不见的地方就等于点了没反应。
    """
    stray = QWidget()
    stray.setGeometry(-4000, -3000, 900, 600)
    try:
        window = _Probe(anchor=stray)
        avail = window.screen().availableGeometry()
        assert avail.contains(window.geometry())
    finally:
        stray.deleteLater()


def test_minimum_size_is_smaller_than_the_initial_lower_bound():
    """`MIN_SIZE` 与 `SIZE_BOUNDS` 下限是两件事。

    前者是"再小就没法用了"，后者是"我替你选的初始大小"。混成一个的后果是用户没法把
    窗口缩到屏幕角上 —— 而"缩到角上只留时间线"正是并排看的常见摆法。
    """
    assert _OtherProbe.MIN_SIZE[0] < _OtherProbe.SIZE_BOUNDS[0][0]
    assert _OtherProbe.MIN_SIZE[1] < _OtherProbe.SIZE_BOUNDS[1][0]


def test_anchor_falls_back_when_it_has_no_geometry(qt_app):
    """`anchor=None` 也得算出一个能用的参照系（主窗口 → 主屏可用区）。"""
    rect = StandaloneWindow._anchor_geometry(None)
    assert rect.width() > 0 and rect.height() > 0


def test_find_main_window_matches_by_object_name(qt_app):
    """按 `objectName` 找，而不是走 `parent()` —— 这些窗口刻意以 `parent=None` 构造。"""
    assert find_main_window() is None

    fake = QWidget()
    fake.setObjectName("MainWindow")
    try:
        assert find_main_window() is fake
    finally:
        fake.deleteLater()
        QApplication.processEvents()


# ── 主题 ────────────────────────────────────────────────────


def test_background_follows_the_theme(qt_app, anchor):
    """底色跟着主题走，且样式表用 `#objectName` 选择器下发。

    类型选择器在基类里只能靠 `type(self).__name__` 拼，继承一层就写错；objectName 是
    每个实例自己的，不会跑偏。
    """
    original = Theme.LIGHT
    try:
        setTheme(Theme.LIGHT)
        window = _Probe(anchor=anchor)
        assert window.objectName() == "_Probe"
        assert "#_Probe" in window.styleSheet()
        assert "#f3f3f3" in window.styleSheet()

        setTheme(Theme.DARK)
        QApplication.processEvents()
        assert "#202020" in window.styleSheet()
    finally:
        setTheme(original)


# ── 键盘 ────────────────────────────────────────────────────


def test_escape_closes_the_window(qt_app, anchor):
    """这些窗口是从对话框改过来的，用户的手指记着 Esc；而它们全是只读地看，
    没有会被 Esc 丢掉的未保存状态。"""
    window = _Probe.show_singleton(anchor)
    QApplication.sendEvent(
        window, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert _Probe.singleton() is None
