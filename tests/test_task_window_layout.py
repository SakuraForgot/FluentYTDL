"""Regression coverage for #105: real layouts, screen bounds and reachable actions."""

import gc
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from PySide6.QtCore import (  # noqa: E402
    QCoreApplication,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    Qt,
)
from PySide6.QtGui import QFont, QFontDatabase, QGuiApplication, QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402
from qfluentwidgets import Theme, qconfig, setTheme  # noqa: E402

from fluentytdl.ui.components.common.adaptive_layout import TaskScrollArea  # noqa: E402
from fluentytdl.ui.components.dialogs.download_config_window import (  # noqa: E402
    DownloadConfigWindow,
)
from fluentytdl.ui.components.dialogs.selection_dialog import PlaylistFormatDialog  # noqa: E402
from fluentytdl.ui.components.platforms.youtube import VideoFormatSelectorWidget  # noqa: E402


@pytest.fixture(scope="module")
def app():
    app = QApplication.instance() or QApplication([])
    previous = app.font()
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    if font_path.exists():
        QFontDatabase.addApplicationFont(str(font_path))
        app.setFont(QFont("Microsoft YaHei UI", 9))
    yield app
    app.setFont(previous)


@pytest.fixture
def dispose_widgets(app):
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()
    gc.collect()


@pytest.fixture(autouse=True)
def isolated_widget_lifetime(dispose_widgets):
    yield


@pytest.fixture
def info():
    return {
        "id": "layout105",
        "title": "Layout probe / 窗口布局测试",
        "duration": 600,
        "extractor_key": "Youtube",
        "formats": [
            {
                "format_id": f"v{i}",
                "height": 1080,
                "width": 1920,
                "ext": "mp4",
                "vcodec": "avc1",
                "acodec": "none",
                "fps": 30,
                "url": "https://example.invalid/video",
            }
            for i in range(20)
        ]
        + [
            {
                "format_id": f"a{i}",
                "ext": "m4a",
                "vcodec": "none",
                "acodec": "mp4a",
                "abr": 128 + i,
                "url": "https://example.invalid/audio",
            }
            for i in range(20)
        ],
    }


class Screen:
    def __init__(self, rect):
        self.rect = rect

    def availableGeometry(self):
        return self.rect


@pytest.fixture
def windows(app, monkeypatch):
    for method in (
        "start_extraction",
        "_run_cookie_precheck",
        "_reload_webview2_account_combo",
        "_setup_scheduler",
        "_on_thumb_init_timeout",
        "_initial_viewport_scan",
    ):
        monkeypatch.setattr(DownloadConfigWindow, method, lambda self: None)
    created = []

    def create(mode, available):
        # The same work area must apply during construction and after show;
        # otherwise constructor sizing accidentally uses offscreen's 800x800.
        monkeypatch.setattr(QGuiApplication.primaryScreen(), "availableGeometry", lambda: available)
        url = (
            "https://www.youtube.com/@layout"
            if mode == "channel"
            else "https://www.youtube.com/watch?v=layout105"
        )
        window = DownloadConfigWindow(url, mode=mode)
        monkeypatch.setattr(window.image_loader, "load", lambda *args, **kwargs: None)
        monkeypatch.setattr(window, "screen", lambda: Screen(available))
        created.append(window)
        window.show()
        return window

    yield create
    for window in created:
        window.close()
    app.processEvents()


def assert_actions_inside(window, available):
    assert available.contains(window.geometry())
    for button in (window.yesButton, window.cancelButton):
        rect = QRect(button.mapToGlobal(QPoint()), button.size())
        assert available.contains(rect)
        assert button.isVisible()
    assert window.childAt(QPoint(20, 16)) is window.titleBar


def _wait_until(predicate, timeout_ms=2000, step_ms=20):
    """Poll `predicate` (pumping the event loop) until true or timeout.

    Fixed `qWait`s race the parse→grow→scroll-range recompute: under full-run
    event-loop load the range update can land well after a 260–400ms sleep, so a
    probe reads a stale `maximum()==0` even though the content overflows by ~300px.
    Polling returns as soon as the layout settles and tolerates the lag.
    """
    elapsed = 0
    while elapsed < timeout_ms:
        if predicate():
            return True
        QTest.qWait(step_ms)
        elapsed += step_ms
    return predicate()


@pytest.mark.parametrize("mode", ["default", "vr", "subtitle", "cover", "playlist", "channel"])
@pytest.mark.parametrize(
    "available", [QRect(0, 0, 1280, 680), QRect(-800, 40, 640, 480), QRect(80, -1240, 720, 1240)]
)
def test_task_actions_remain_on_screen_after_content(windows, info, mode, available):
    window = windows(mode, available)
    if mode in ("playlist", "channel"):
        info = {"_type": "playlist", "title": "Layout list", "entries": [dict(info)]}
    window.on_parse_success(info)
    QTest.qWait(360)  # settle the fade-out → grow → fade-in before asserting bounds
    assert_actions_inside(window, available)
    window._geometry_guard.fit()
    assert_actions_inside(window, available)


def test_expansion_and_reparse_preserve_user_geometry_and_directory_access(windows, info):
    available = QRect(0, 0, 1280, 680)
    window = windows("default", available)
    window.on_parse_success(info)
    QTest.qWait(40)
    window.setGeometry(30, 40, 640, 480)
    before = window.geometry()
    window._section_selector.enable_switch.setChecked(True)
    QTest.qWait(280)
    assert window.geometry() == before
    assert_actions_inside(window, available)
    # 目录条现固定在底部页脚（v_layout，不在滚动区里）：结果页里它常驻可见、且落在窗口
    # 可视矩形内（不被裁切），无需再滚动到底才能够到。
    field = window._download_dir_edit
    assert _wait_until(lambda: window._download_dir_bar.isVisible() and field.isVisible())
    field_rect = QRect(field.mapTo(window, QPoint()), field.size())
    assert window.rect().contains(field_rect)
    window.on_parse_success(info)
    QTest.qWait(40)
    assert window.geometry() == before


def test_work_area_change_refits_without_recentering(windows, info):
    screen = QRect(-1000, 30, 1000, 900)
    window = windows("default", screen)
    window.on_parse_success(info)
    QTest.qWait(30)
    window.setGeometry(-900, 80, 760, 780)
    before = window.geometry()
    window._geometry_guard.fit()
    assert window.geometry() == before
    screen.setHeight(500)
    screen.setWidth(640)
    window._geometry_guard.fit()
    QTest.qWait(30)
    assert_actions_inside(window, screen)


def test_format_dialog_has_reachable_actions_on_small_screen(app, info, monkeypatch):
    window = PlaylistFormatDialog(info)
    available = QRect(40, 30, 640, 480)
    monkeypatch.setattr(window, "screen", lambda: Screen(available))
    window.show()
    QTest.qWait(40)
    assert_actions_inside(window, available)
    window.close()
    window.deleteLater()


def test_nested_scroll_forwards_edge_wheel_once_and_accepts_pixel_delta(app, info):
    outer = TaskScrollArea()
    selector = VideoFormatSelectorWidget(info)
    outer.setWidget(selector)
    selector._on_mode_changed("advanced")
    outer.resize(600, 350)
    outer.show()
    QTest.qWait(50)
    table = selector.video_table
    inner = table.verticalScrollBar()
    assert inner.maximum() > 0
    inner.setValue(inner.maximum())
    split = selector.split_scroll.verticalScrollBar()
    root = outer.verticalScrollBar()
    split.setValue(0)
    root.setValue(0)
    event = QWheelEvent(
        QPointF(30, 30),
        QPointF(table.viewport().mapToGlobal(QPoint(30, 30))),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(table.viewport(), event)
    QTest.qWait(240)
    assert inner.value() == inner.maximum()
    assert (split.value() > 0) != (root.value() > 0)
    inner.setValue(0)
    pixel_event = QWheelEvent(
        QPointF(30, 30),
        event.globalPosition(),
        QPoint(0, -15),
        QPoint(),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    QApplication.sendEvent(table.viewport(), pixel_event)
    assert inner.value() == 15
    outer.close()
    outer.deleteLater()


def test_hidden_outer_bars_still_scroll_and_reveal_keyboard_focus(windows, info):
    window = windows("default", QRect(0, 0, 640, 480))
    window.on_parse_success(info)
    # Fade-out → grow → fade-in is ~300ms and the scroll-range recompute can lag well
    # past that under full-run load; poll for the settled range instead of a fixed
    # sleep (content overflows the 480px shell, so maximum is reliably >0 once it lands).
    area = window.scrollArea
    assert _wait_until(lambda: area.verticalScrollBar().maximum() > 0)
    assert not area.delegate.vScrollBar.isVisible()
    assert not area.delegate.hScrollBar.isVisible()
    event = QWheelEvent(
        QPointF(10, 10),
        QPointF(area.viewport().mapToGlobal(QPoint(10, 10))),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(area.viewport(), event)
    assert _wait_until(lambda: area.verticalScrollBar().value() > 0)
    assert not area.delegate.vScrollBar.isVisible()
    window.activateWindow()
    QTest.qWait(20)
    # 目录条已移到页脚（不在滚动区内），改用滚动区内的「嵌入元数据」开关验证键盘焦点会把
    # 焦点控件滚入视口——TaskScrollArea.eventFilter 只对其内容子树里的 Tab 焦点生效。
    field = window.metadata_check
    field.setFocus(Qt.FocusReason.TabFocusReason)
    # Keyboard focus scrolls the field into view via ensureWidgetVisible; that scroll
    # settles asynchronously, so poll for the revealed field instead of a fixed wait.
    assert _wait_until(
        lambda: (
            area.viewport()
            .rect()
            .contains(QRect(field.mapTo(area.viewport(), QPoint()), field.size()))
        )
    )
    assert not area.delegate.vScrollBar.isVisible()
    # This preference is scoped to the window shell, not to the stream lists.
    window.selector_widget.view_switcher.setCurrentItem("advanced")
    QTest.qWait(50)
    assert window.selector_widget.video_table.scrollDelagate.vScrollBar.isVisible()


@pytest.mark.parametrize("theme,background", [(Theme.DARK, 32), (Theme.LIGHT, 243)])
def test_resize_frames_keep_themed_surfaces(windows, info, theme, background):
    previous = qconfig.theme
    setTheme(theme, lazy=False)
    window = windows("default", QRect(0, 0, 1280, 900))
    try:
        window.on_parse_success(info)
        QTest.qWait(360)  # settle the parse transition before driving manual resizes
        window.selector_widget.view_switcher.setCurrentItem("advanced")
        card = window.selector_widget.audio_card
        QTest.qWait(50)
        # The accordion is retired — the stream list is fixed-open. What still
        # matters is that the themed surfaces (window gutter, scroll viewport,
        # card padding) hold their exact backing color across a live resize,
        # never flashing black (unpainted) or white in the dark theme.
        for frame in range(16):
            window.resize(640 + frame * 4, 480 + frame * 3)
            QTest.qWait(16)
            surface = window.grab().toImage()
            for y in (40, surface.height() // 2, surface.height() - 12):
                color = surface.pixelColor(8, y)
                assert color.getRgb() == (background, background, background, 255)
            viewport = window.scrollArea.viewport().grab().toImage()
            color = viewport.pixelColor(viewport.width() - 2, 2)
            assert color.getRgb() == (background, background, background, 255)
            card_image = card.grab().toImage()
            color = card_image.pixelColor(5, card_image.height() // 2)
            assert all(abs(value - background) < 20 for value in color.getRgb()[:3])
            assert card.body_widget.isVisible()  # fixed-open, no toggle
    finally:
        setTheme(previous, lazy=False)


@pytest.mark.parametrize("mode", ["default", "vr"])
def test_stream_alternating_rows_do_not_use_native_light_background(windows, info, mode):
    previous = qconfig.theme
    setTheme(Theme.DARK, lazy=False)
    window = windows(mode, QRect(0, 0, 1280, 900))
    try:
        window.on_parse_success(info)
        QTest.qWait(360)  # settle the parse transition before grabbing table pixels
        selector = window.selector_widget
        if mode == "vr":
            selector.mode_seg.setCurrentItem("pro")
            selector = selector.pro_widget
        else:
            selector.view_switcher.setCurrentItem("advanced")
        for theme in (Theme.DARK, Theme.LIGHT, Theme.DARK):
            setTheme(theme, lazy=False)
            QTest.qWait(40)
            for table in (selector.video_table, selector.audio_table):
                image = table.viewport().grab().toImage()
                for row in (0, 1):
                    rect = table.visualRect(table.model().index(row, table.columnCount() - 1))
                    color = image.pixelColor(rect.right() - 12, rect.center().y())
                    # A viewport-only grab retains transparency. Composite on
                    # its actual task surface before checking row contrast.
                    background = 32 if theme == Theme.DARK else 243
                    channels = [
                        (value * color.alpha() + background * (255 - color.alpha())) / 255
                        for value in color.getRgb()[:3]
                    ]
                    if theme == Theme.DARK:
                        assert max(channels) < 100
                    else:
                        assert min(channels) > 200
    finally:
        setTheme(previous, lazy=False)


@pytest.mark.parametrize("theme", [Theme.DARK, Theme.LIGHT])
def test_stream_card_press_does_not_pulse_background(windows, info, theme):
    previous = qconfig.theme
    setTheme(theme, lazy=False)
    window = windows("default", QRect(0, 0, 1280, 900))
    try:
        window.on_parse_success(info)
        window.selector_widget.view_switcher.setCurrentItem("advanced")
        card = window.selector_widget.video_card
        QTest.qWait(260)  # result page must be shown before pressing the card
        normal = card.getBackgroundColor()
        # The stream list is fixed-open; the card is a passive container whose
        # hover/pressed colors are pinned to normal, so pressing it (child
        # labels included) must never pulse its background the way a clickable
        # card would.
        QTest.mousePress(card.title_label, Qt.MouseButton.LeftButton)
        QTest.qWait(140)
        assert card.getBackgroundColor() == normal
        QTest.mouseRelease(card.title_label, Qt.MouseButton.LeftButton)
        QTest.qWait(140)
        assert card.getBackgroundColor() == normal
        assert card.body_widget.isVisible()  # fixed-open, no toggle
    finally:
        setTheme(previous, lazy=False)


def test_pointer_focus_does_not_scroll_outer_body_but_keyboard_focus_does(windows, info):
    window = windows("default", QRect(0, 0, 640, 480))
    window.on_parse_success(info)
    window.activateWindow()
    bar = window.scrollArea.verticalScrollBar()
    # Poll for the settled scroll range before focus-scroll probing — the grow →
    # range recompute lags under full-run load, and a stale maximum()==0 leaves
    # tab-focus with nothing to scroll into view (see _wait_until).
    assert _wait_until(lambda: bar.maximum() > 0)
    window.cancelButton.setFocus()
    bar.setValue(0)
    # The download-dir field now lives in the fixed footer (outside the scroll
    # content), so it can no longer be a focus-scroll probe. metadata_check sits
    # in the in-scroll options row below the fold — a valid interior target.
    window.metadata_check.setFocus(Qt.FocusReason.MouseFocusReason)
    QTest.qWait(40)
    assert bar.value() == 0
    window.cancelButton.setFocus()
    window.metadata_check.setFocus(Qt.FocusReason.TabFocusReason)
    QTest.qWait(40)
    assert bar.value() > 0


def test_parse_result_grows_from_compact_loading_shell(windows, info):
    window = windows("default", QRect(0, 0, 1280, 1040))
    QTest.qWait(40)
    before = window.geometry()
    # Opens as a compact loading shell (thumbnail placeholder + spinner), not
    # the full result size — no big black box with a tiny ring floating in it.
    assert before.width() == 520 and before.height() == 440
    exposed = []

    class ResizeObserver(QObject):
        def eventFilter(self, obj, event):
            if event.type() == QEvent.Type.Resize:
                exposed.append(window.contentWidget.isVisible())
            return False

    observer = ResizeObserver(window)
    window.installEventFilter(observer)
    window.on_parse_success(info)
    QTest.qWait(360)  # clear the fade-out → grow → fade-in transition
    # Grows to the comfortable result size. Default is 880 wide — room for the
    # side-by-side 视频流|音频流 stream tables while staying tighter than the
    # earlier 960 (per「解析页的宽度再缩小」). Height is no longer a fixed 860:
    # it now fits the current sub-mode's natural content (per「上下空太多」), so
    # the single-table modes stay short and nothing over-reserves a table floor.
    # The invariant that proves the fit worked is that the content fits exactly —
    # no bottom whitespace AND no scroll at rest (work area is tall enough here
    # that the fitted height isn't clamped). The range recompute can lag the grow
    # under full-run load, so poll for the settled zero maximum.
    assert window.geometry().width() == 880
    assert window.geometry().height() > before.height()  # grew from the loading shell
    assert _wait_until(lambda: window.scrollArea.verticalScrollBar().maximum() == 0)
    # The result page is exposed before the native window is enlarged, so the
    # grow never reveals unpainted client pixels.
    assert exposed and all(exposed)
    old_selector = window.selector_widget
    window.on_parse_success(info)
    # _clear_content_layout hides the previous page synchronously; deferred
    # deletion must not leave old controls painted over the new page.
    assert not old_selector.isVisible()


def test_desktop_default_shows_both_stream_headers_and_download_directory(windows, info):
    window = windows("default", QRect(0, 0, 1707, 1019))
    window.on_parse_success(info)
    QTest.qWait(360)  # settle the parse transition before measuring the grown layout
    selector = window.selector_widget
    selector.view_switcher.setCurrentItem("advanced")
    QTest.qWait(250)
    # The download-dir field lives in the fixed footer now (pinned with the
    # reparse/cancel/download buttons), not inside the scroll body.
    field = window._download_dir_edit
    assert window._download_dir_bar.isVisible() and field.isVisible()
    field_rect = QRect(field.mapTo(window, QPoint()), field.size())
    assert window.rect().contains(field_rect)
    split_view = selector.split_scroll.viewport()
    for card in (selector.video_card, selector.audio_card):
        header = card.header_widget
        rect = QRect(header.mapTo(split_view, QPoint()), header.size())
        assert split_view.rect().contains(rect)


# CI 分层标记（见 pyproject [tool.pytest.ini_options] markers）；本地全量 pytest 不受影响，仅 CI 的 -m 过滤用到
pytestmark = pytest.mark.windows_gui
