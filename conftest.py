"""Isolate application state before collection imports any application module."""

import os
import tempfile

os.environ["FLUENTYTDL_DATA_DIR_OVERRIDE"] = tempfile.mkdtemp(prefix="fytdl-pytest-")
os.environ["FLUENTYTDL_LOG_ORIGIN"] = "test"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gc  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _stabilize_qt_stylesheet_registry(request):
    """windows_gui 串行 lane 专用：每个用例前后把 Qt 的样式表注册表冲稳。

    qfluentwidgets 的 `styleSheetManager` 是个 `WeakKeyDictionary`，`setTheme()` 会遍历它。
    上一个用例 `close()` 掉的 `WA_DeleteOnClose` 窗口只排了一个 DeferredDelete —— **没跑
    `app.exec()`** 时普通 `processEvents()` 并不投递顶层栈上的 DeferredDelete，于是那些死
    窗口的弱引用一直挂在注册表里。等到本用例 `setTheme` 遍历途中它们的弱引用回调恰好触发、
    把条目摘掉，就抛 `RuntimeError: dictionary changed size during iteration`。

    这些窗口用例合批跑（CI 的 windows_gui 串行 lane，也包括本地全量 pytest）时死窗口会**跨
    文件累积** —— 单文件、甚至两文件都跑不出来，三文件一凑够量就炸（`test_main_window_
    screen_bounds` + `test_notification_panel_theming` → `test_standalone_window`）。所以修
    在单个文件的 teardown 里堵不住；这里统一在每个 windows_gui 用例**前**把注册表冲干净：
    显式投递 DeferredDelete 真正销毁死窗口、泵事件、gc 回收 Python 侧包装器，让 `setTheme`
    开始遍历时注册表是稳定的。非 windows_gui 用例（含 Linux 并行 lane）直接跳过，零开销、
    不引入 Qt 依赖。
    """
    if request.node.get_closest_marker("windows_gui") is None:
        yield
        return

    from PySide6.QtCore import QEvent
    from PySide6.QtWidgets import QApplication

    def _drain() -> None:
        app = QApplication.instance()
        if app is None:
            return
        # 先把顶层栈上排着的 DeferredDelete 真正投递出去（普通 processEvents 不做这件事），
        # 再泵一轮常规事件，让销毁连锁（子控件注销、弱引用回调）在遍历之外跑完。
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()

    _drain()
    gc.collect()
    _drain()
    yield
    _drain()
    gc.collect()


@pytest.fixture(scope="session", autouse=True)
def _disable_startup_update_check():
    """全测试会话：掐断 MainWindow 启动 3 秒后的自动更新检查。

    `reimagined_main_window` 构造里挂了
    `QTimer.singleShot(3000, self.settings_interface.schedule_startup_update_check)`。
    这是个**不挂在窗口下的独立定时器**，module 级 MainWindow fixture
    （`test_task_header_layout` / `test_main_window_screen_bounds`）活过 3 秒后它必定
    触发，于是 `UpdateCheckerWorker(QThread)` / app-core 清单 fetch 真的去打 GitHub
    API。这些网络线程不受任何用例管辖、会活到后面的文件里，在 SSL 握手途中把进程按
    访问违例段错误掀掉（windows_gui 串行 lane 跑到 `test_task_list_right_click` ~61%
    处 EXIT=139，堆栈全在 `dependency_manager.run` → `update_transport` → `do_handshake`）。

    没有任何用例断言这个启动检查会跑 —— 更新逻辑本身由 `test_component_update_manager`
    / `test_dependency_manager` 直接调 `check_update` / `check_app_update` 配合 mock 传输
    层来测。所以在类上把这个启动触发器换成空操作，是最省、最稳、也不碰被测更新代码
    路径的堵法。session 作用域保证它在任何 module 级窗口 fixture 造 MainWindow 之前就
    位（否则 `singleShot` 那一刻已把原方法绑成 bound method，替换就晚了）。
    """
    from fluentytdl.ui.settings_page import SettingsPage

    original = SettingsPage.schedule_startup_update_check
    SettingsPage.schedule_startup_update_check = lambda self: None
    try:
        yield
    finally:
        SettingsPage.schedule_startup_update_check = original
