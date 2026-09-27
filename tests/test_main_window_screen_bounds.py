"""#105 后续：主窗口自身按屏幕可用区夹取（高 DPI 小屏不溢出、标题栏不被顶出屏）。

主窗口是 issue-105 里唯一没统一改造的窗口 —— 它硬钉 `setMinimumWidth(1150)`、初始高
780，再用不带夹取的 `move()` 居中。在高 DPI 小屏上（1920×1080@150% 逻辑可用约
1280×688、@200% 约 960×500）：780 高超出可用高、居中算出负 y 把标题栏顶到屏外，
1150 的硬最小宽又比屏还宽且缩不回去。修好后钉住三条：

1. 初始几何整体落在可用区内（高度不超屏、标题栏顶边在屏内、宽度不超可用宽）；
2. 最小宽/高按可用区放宽（放得下仍是 1150×600，放不下降到可用宽/高）；
3. 可用区变化（换屏/缩放）时 `WindowGeometryGuard` 持续把窗口夹回去。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 建 MainWindow 会连带碰 config/DB/日志与主题监听线程，数据根先指到临时目录、Qt 走离屏
_ENV_GUARD = tempfile.mkdtemp(prefix="fluentytdl-mainwin-bounds-")
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", _ENV_GUARD)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HAS_PYSIDE6 = True
try:
    from PySide6.QtCore import QEvent, QRect  # noqa: E402
    from PySide6.QtGui import QGuiApplication  # noqa: E402
    from PySide6.QtWidgets import QApplication  # noqa: E402
except ImportError:
    HAS_PYSIDE6 = False

requires_qt = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 required for layout tests")


class _Screen:
    """`window.screen()` 替身：`WindowGeometryGuard.fit()` 只问 availableGeometry。"""

    def __init__(self, rect):
        self._rect = rect

    def availableGeometry(self):
        return self._rect


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication(sys.argv)
    yield app


def _pump(qapp, times: int = 20) -> None:
    for _ in range(times):
        qapp.processEvents()


@pytest.fixture
def make_window(qapp, monkeypatch):
    """在指定「逻辑可用区」下造一个真实 MainWindow，并保证测试结束干净退出。

    可用区必须在**构造期**就生效（初始几何在 __init__ 里就定了），所以先打
    `primaryScreen().availableGeometry` —— 构造时 `self.screen()` 取的就是它；构造完再把
    `window.screen` 换成同一矩形的替身，覆盖 guard 之后 `fit()` 读的那次。
    """
    from fluentytdl.ui.reimagined_main_window import MainWindow

    # 首次运行向导是模态框，无头下会挂死
    monkeypatch.setattr(MainWindow, "check_first_run", lambda self: None)
    created = []

    def create(available):
        monkeypatch.setattr(QGuiApplication.primaryScreen(), "availableGeometry", lambda: available)
        window = MainWindow()
        monkeypatch.setattr(window, "screen", lambda: _Screen(available))
        created.append(window)
        window.show()
        _pump(qapp)
        return window

    yield create

    for window in created:
        # `SystemThemeListener` 是条阻塞在 Win32 注册表通知上的原生 QThread：无头下
        # `close()` 走「隐藏到托盘」分支、跳过 closeEvent 里的停线程逻辑，把活线程留到
        # 解释器 finalize 会让 Python 3.10 段错误。先显式停掉再关。
        window._stop_theme_listener()
        window.close()
        # `close()` 只是隐藏到托盘、并不销毁窗口：合批跑 windows_gui 时这扇类名为
        # "MainWindow" 的窗口会留在 topLevelWidgets() 里，后面 test_standalone_window
        # 的 `find_main_window() is None`（按类名认，见 standalone_window.py）就会撞见它
        # 而失败。deleteLater 排一个 DeferredDelete，但**没跑 app.exec() 时** processEvents
        # 不投递顶层栈上的 DeferredDelete —— 得显式 sendPostedEvents 冲一下才真正销毁。
        window.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    _pump(qapp)


def assert_within(window, available) -> None:
    # 真正占屏的是 frameGeometry（含缩放边框/阴影），不是内容区 geometry()：
    # 内容区夹到刚好贴边时，那圈 ~2px 边框仍会探出每条边，得按外框判定。
    frame = window.frameGeometry()
    assert available.contains(frame), f"窗口外框 {frame} 越出可用区 {available}"
    # 标题栏顶边在屏内：负 y 会把标题栏顶到屏外，非无边框窗口下就再也抓不到拖动
    assert frame.top() >= available.top(), f"标题栏顶到屏外：top={frame.top()} < {available.top()}"
    # 高度不超屏、宽度不离谱（都不超过对应可用尺寸）
    assert frame.height() <= available.height(), (
        f"高 {frame.height()} 超出可用高 {available.height()}"
    )
    assert frame.width() <= available.width(), f"宽 {frame.width()} 超出可用宽 {available.width()}"


# 高 DPI 小屏的逻辑可用区，全部对应矩阵里主窗口原本溢出的格子
SMALL_AREAS = [
    QRect(0, 0, 1280, 688),  # 1920×1080 @150%
    QRect(0, 0, 960, 500),  # 1920×1080 @200%（矩阵里最窄最矮的一格）
    QRect(0, 0, 1280, 720),  # 1280×720 原生小屏
]


@requires_qt
@pytest.mark.parametrize("available", SMALL_AREAS, ids=lambda r: f"{r.width()}x{r.height()}")
def test_initial_geometry_fits_small_screen(make_window, available):
    window = make_window(available)
    assert_within(window, available)
    # 关键：最小宽/高被放宽到不超过可用区，否则窗口比屏还大又缩不回去（原 bug）
    assert window.minimumWidth() <= available.width()
    assert window.minimumHeight() <= available.height()


@requires_qt
def test_guard_attached_with_main_window_floor(make_window):
    from fluentytdl.ui.components.common.adaptive_layout import WindowGeometryGuard
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_HEIGHT, MIN_WINDOW_WIDTH

    window = make_window(QRect(0, 0, 1920, 1040))
    # 主窗口挂的是 1150×600 的下限，不是 guard 默认的 360×240 —— 这是整个适配契约
    assert isinstance(window._geometry_guard, WindowGeometryGuard)
    assert window._geometry_guard._min_size == (MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)


@requires_qt
def test_large_screen_keeps_full_min_width(make_window):
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH

    available = QRect(0, 0, 1920, 1040)
    window = make_window(available)
    # 屏幕放得下：1150 的硬最小宽（防切英文/导航动画自动变宽）原样保留
    assert window.minimumWidth() == MIN_WINDOW_WIDTH
    assert window.width() >= MIN_WINDOW_WIDTH
    assert_within(window, available)


@requires_qt
def test_guard_refits_when_work_area_shrinks(make_window, qapp):
    from fluentytdl.ui.reimagined_main_window import MIN_WINDOW_WIDTH

    window = make_window(QRect(0, 0, 1920, 1040))
    assert window.minimumWidth() == MIN_WINDOW_WIDTH  # 大屏起手仍是完整下限

    # 模拟拖到高缩放小屏 / 工作区缩小：换掉 screen 替身后让 guard 重夹
    shrunk = QRect(0, 0, 960, 500)
    window.screen = lambda: _Screen(shrunk)
    window._geometry_guard.fit()
    _pump(qapp)

    assert_within(window, shrunk)
    # 下限跟着降到可用区（不超过），窗口才缩得进这块更小的屏
    assert window.minimumWidth() <= shrunk.width()
    assert window.minimumHeight() <= shrunk.height()


# CI 分层标记（见 pyproject [tool.pytest.ini_options] markers）；本地全量 pytest 不受影响，仅 CI 的 -m 过滤用到
pytestmark = pytest.mark.windows_gui
