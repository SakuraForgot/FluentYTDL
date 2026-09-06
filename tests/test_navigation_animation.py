"""可打断侧边栏动画的回归覆盖。

上游 ``NavigationPanel`` 在动画运行期间会丢弃反向请求（``collapse`` 直接 return）、
把展开动画的起点写死成 48px，并依据滞后的 ``displayMode`` 判断 ``toggle`` 方向。
这些行为在 UI 上表现为侧边栏"粘滞、不跟手"，这里锁死修复后的契约。
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

HAS_PYSIDE6 = True
try:
    from PySide6.QtCore import QEventLoop, QPropertyAnimation, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    HAS_PYSIDE6 = False

requires_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 required for navigation tests")

COMPACT_WIDTH = 48
EXPAND_WIDTH = 190


@pytest.fixture(scope="module")
def qapp():
    """离屏 QApplication，供侧边栏动画测试使用。"""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


@pytest.fixture
def panel(qapp):
    """装好可打断动画的侧边栏 panel（不复用，避免动画状态串味）。"""
    from PySide6.QtWidgets import QWidget
    from qfluentwidgets import FluentIcon, FluentWindow, NavigationItemPosition

    from fluentytdl.ui.components.common.interruptible_navigation import (
        install_interruptible_navigation,
    )

    window = FluentWindow()
    window.resize(1150, 780)
    window.setMinimumWidth(1150)
    window.navigationInterface.setExpandWidth(EXPAND_WIDTH)
    for index in range(2):
        page = QWidget()
        page.setObjectName(f"page{index}")
        window.addSubInterface(
            page, FluentIcon.ADD, f"page{index}", position=NavigationItemPosition.TOP
        )

    assert install_interruptible_navigation(window.navigationInterface) is True
    window.show()
    qapp.processEvents()

    yield window.navigationInterface.panel

    window.close()
    qapp.processEvents()


def _seek(panel, ms: int) -> None:
    """把正在跑的动画定位到指定时刻，避免测试依赖真实等待时间。"""
    assert panel.expandAni.state() == QPropertyAnimation.State.Running
    panel.expandAni.setCurrentTime(ms)


def _settle(panel, qapp, timeout_ms: int = 2000) -> None:
    """等动画自然结束。"""
    if panel.expandAni.state() != QPropertyAnimation.State.Running:
        return
    loop = QEventLoop()
    panel.expandAni.finished.connect(loop.quit)
    QTimer.singleShot(timeout_ms, loop.quit)
    loop.exec()
    panel.expandAni.finished.disconnect(loop.quit)


@requires_qt
def test_full_expand_keeps_upstream_timing(panel, qapp):
    """未被打断的全程展开必须保持上游的 150ms，观感不变。"""
    from fluentytdl.ui.components.common.interruptible_navigation import FULL_DURATION_MS

    panel.expand()
    assert panel.expandAni.duration() == FULL_DURATION_MS

    _settle(panel, qapp)
    assert panel.width() == EXPAND_WIDTH


@requires_qt
def test_collapse_during_expand_is_not_dropped(panel, qapp):
    """展开动画进行中请求收起，不能像上游那样静默丢弃。"""
    panel.expand()
    _seek(panel, 75)
    mid_width = panel.width()
    assert COMPACT_WIDTH < mid_width < EXPAND_WIDTH

    panel.collapse()

    ani = panel.expandAni
    assert ani.state() == QPropertyAnimation.State.Running
    assert ani.property("expand") is False
    assert ani.endValue().size().width() == COMPACT_WIDTH

    _settle(panel, qapp)
    assert panel.width() == COMPACT_WIDTH


@requires_qt
def test_expand_during_collapse_starts_from_current_width(panel, qapp):
    """收起动画进行中请求展开，要从当前宽度反向，而不是跳回 48px。"""
    panel.expand()
    _settle(panel, qapp)

    panel.collapse()
    _seek(panel, 75)
    mid_width = panel.width()
    assert COMPACT_WIDTH < mid_width < EXPAND_WIDTH

    panel.expand()

    ani = panel.expandAni
    assert ani.startValue().size().width() == mid_width
    assert ani.endValue().size().width() == EXPAND_WIDTH
    assert ani.property("expand") is True

    _settle(panel, qapp)
    assert panel.width() == EXPAND_WIDTH


@requires_qt
def test_toggle_during_collapse_reverses_to_expand(panel, qapp):
    """收起途中点汉堡按钮应反向展开——此时 displayMode 仍是 EXPAND，不能据它判断。"""
    from qfluentwidgets.components.navigation.navigation_panel import NavigationDisplayMode

    panel.expand()
    _settle(panel, qapp)

    panel.collapse()
    _seek(panel, 75)
    assert panel.displayMode == NavigationDisplayMode.EXPAND

    panel.toggle()
    assert panel.expandAni.property("expand") is True

    _settle(panel, qapp)
    assert panel.width() == EXPAND_WIDTH


@requires_qt
def test_reversal_duration_scales_with_remaining_distance(panel, qapp):
    """反向动画按剩余距离缩短，否则短距离反向会显得拖沓。"""
    from fluentytdl.ui.components.common.interruptible_navigation import (
        FULL_DURATION_MS,
        MIN_DURATION_MS,
    )

    panel.expand()
    _seek(panel, 20)  # 刚起步，离 48px 很近
    panel.collapse()

    duration = panel.expandAni.duration()
    assert MIN_DURATION_MS <= duration < FULL_DURATION_MS

    _settle(panel, qapp)
    assert panel.width() == COMPACT_WIDTH


@requires_qt
def test_repeated_interrupts_end_in_requested_state(panel, qapp):
    """连续反复打断后，最终状态由最后一次请求决定。"""
    panel.expand()
    _settle(panel, qapp)

    for index in range(6):
        panel.collapse() if index % 2 == 0 else panel.expand()
        _seek(panel, 30)

    panel.collapse()
    _settle(panel, qapp)

    assert panel.width() == COMPACT_WIDTH
    assert panel.expandAni.state() != QPropertyAnimation.State.Running


@requires_qt
def test_install_is_idempotent(panel):
    """重复安装应是幂等的，不叠加包装。"""
    from fluentytdl.ui.components.common.interruptible_navigation import (
        InterruptibleNavigationPanel,
        install_interruptible_navigation,
    )

    interface = panel.parent()
    assert install_interruptible_navigation(interface) is True
    assert type(interface.panel) is InterruptibleNavigationPanel
