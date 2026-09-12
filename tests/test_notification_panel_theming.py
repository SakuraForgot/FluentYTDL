"""消息中心（铃铛）的深色模式契约 + 浮窗/独立窗口两个容器。

这一屏的 bug 原先是这样的：未读通知用 `label.setStyleSheet("font-weight: bold;")`
标记。而 `FluentLabelBase._init()` 是靠 `setTextColor()` → `setCustomStyleSheet()` 把
`color:` 塞进标签**自己那张样式表**里的 —— `setStyleSheet()` 把整张表换掉，标签于是
退回 Qt 调色板的默认前景色（黑）。结果：深色模式下未读通知是黑底黑字，一个字都看不见，
而"未读"恰恰是最需要读到的那几条。

所以这里钉的不是"某个像素是什么颜色"，而是四条真正会坏的性质：

1. **两种主题下标题都读得出来** —— 按 `isDarkTheme()` 拿到的胜出颜色，亮度必须和背景
   相反（浅色主题上是暗字、深色主题上是亮字）；
2. **字重和字色是两条独立的路** —— 字重走 `setFont(getFont(...))`，字色走
   `setTextColor(浅, 深)`；样式表里出现 `font-weight` 就是老 bug 又回来了；
3. **已读/未读不靠改字色区分** —— 一旦把已读压暗，就得回答"暗到什么程度还看得见"，
   而这个问题在两种主题下答案不一样；
4. **`severity` 图标是存在的类成员** —— 原来那行写的是 `FluentIcon.ERROR`，而
   `FluentIcon` 根本没有 `ERROR`：库里只要存着一条 `severity="critical"` 的通知，
   构造卡片就抛 `AttributeError`，整个铃铛面板一条都显示不出来。

需要 QApplication，走 offscreen。
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-notif-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from PySide6.QtGui import QColor, QFont  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402
from qfluentwidgets import FluentIconBase, InfoBarIcon, Theme, setTheme  # noqa: E402

from fluentytdl.notification import Notification, notification_center  # noqa: E402
from fluentytdl.ui.components.common.event_timeline import _level_color  # noqa: E402
from fluentytdl.ui.components.common.standalone_window import StandaloneWindow  # noqa: E402
from fluentytdl.ui.notification_panel import (  # noqa: E402
    _SECONDARY_COLORS,
    NotificationCard,
    NotificationFlyoutView,
    NotificationListWidget,
    NotificationWindow,
)


@pytest.fixture(scope="module")
def qt_app():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def clean_db(qt_app):
    notification_center.clear_all()
    yield
    notification_center.clear_all()
    StandaloneWindow._instances.clear()


@pytest.fixture(autouse=True)
def light_theme():
    """每个用例都从浅色开场 —— 主题是全局状态，会漏到下一个用例里。"""
    setTheme(Theme.LIGHT)
    yield
    setTheme(Theme.LIGHT)


def _notif(severity: str = "info", *, is_read: bool = False, title: str = "标题") -> Notification:
    return Notification(
        type="info", title=title, message="正文", severity=severity, is_read=is_read
    )


def _cards(list_widget) -> list[NotificationCard]:
    """当前**活着**的卡片，按布局顺序。

    不用 `findChildren()`：`reload()` 把旧卡片 `takeAt()` 出布局之后走的是
    `deleteLater()`，而延迟销毁要等事件循环真正回到相应层级 —— 在此之前 `findChildren`
    会把已经作废的那一批也捞出来，断言于是读到重建之前的旧状态。
    """
    layout = list_widget.scrollLayout
    return [
        layout.itemAt(i).widget()
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), NotificationCard)
    ]


def _winning_color(label) -> QColor:
    """标签实际生效的字色。

    `setTextColor()` 走 `setCustomStyleSheet()`，它把 `FluentLabelBase{color:#aarrggbb}`
    **追加在主题样式表之后** —— 选择器同级，靠顺序取胜。所以"最后那条规则"就是屏幕上
    看到的颜色，而这也正好是老 bug 的检测点：`setStyleSheet()` 把整张表换掉之后，
    这条追加的规则就没了。
    """
    qss = label.styleSheet()
    marker = "color:#"
    idx = qss.rfind(marker)
    assert idx != -1, f"标签没有任何字色规则：{qss!r}"
    argb = qss[idx + len(marker) :].split("}")[0].strip()
    assert len(argb) == 8, f"不是 #aarrggbb 形式：{argb!r}"
    return QColor(f"#{argb[2:]}")


# ── 深色模式（这次要修的东西）──────────────────────────────────


@pytest.mark.parametrize("is_read", [True, False])
@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_title_stays_legible_in_both_themes(qt_app, theme, is_read):
    """标题在两种主题下都必须和背景反着来。

    这是原 bug 的正面断言：深色模式 + 未读 = 黑底黑字，`lightness()` 会掉到 0。
    """
    setTheme(theme)
    card = NotificationCard(_notif(is_read=is_read))
    try:
        lightness = _winning_color(card.titleLabel).lightness()
        if theme is Theme.DARK:
            assert lightness > 128, "深色底上的标题必须是亮字"
        else:
            assert lightness < 128, "浅色底上的标题必须是暗字"
    finally:
        card.deleteLater()


@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_title_color_survives_a_theme_switch(qt_app, theme):
    """切主题之后颜色要跟着换，而不是停在构造时那一档。

    `setTextColor()` 把两档颜色存进 `lightCustomQss` / `darkCustomQss` 动态属性，由
    `CustomStyleSheetWatcher` 在 `themeChanged` 时重新追加 —— 前提是那张表没被
    `setStyleSheet()` 整片顶掉。
    """
    card = NotificationCard(_notif())
    try:
        assert card.titleLabel.property("lightCustomQss")
        assert card.titleLabel.property("darkCustomQss")

        setTheme(theme)
        QApplication.processEvents()
        expected_dark = theme is Theme.DARK
        assert (_winning_color(card.titleLabel).lightness() > 128) is expected_dark
    finally:
        card.deleteLater()


def test_no_stylesheet_carries_font_weight(qt_app):
    """字重绝不能走样式表 —— 那一句会连带把 `color:` 一起顶掉，正是原来的 bug。"""
    card = NotificationCard(_notif())
    try:
        for label in (card.titleLabel, card.msgLabel, card.timeLabel):
            assert "font-weight" not in label.styleSheet().lower()
    finally:
        card.deleteLater()


def test_read_state_changes_weight_and_dot_but_not_color(qt_app):
    """已读/未读靠圆点 + 字重区分，不靠把字压暗。

    压暗过一次就得回答"暗到什么程度还看得见"，而这个问题在两种主题下答案不一样。
    """
    unread = NotificationCard(_notif(is_read=False))
    read = NotificationCard(_notif(is_read=True))
    try:
        assert _winning_color(unread.titleLabel) == _winning_color(read.titleLabel)
        assert unread.titleLabel.font().weight() > read.titleLabel.font().weight()
        assert not unread.unreadDot.isHidden()
        assert read.unreadDot.isHidden()
    finally:
        unread.deleteLater()
        read.deleteLater()


def test_unread_marker_is_not_weight_only(qt_app):
    """光靠加粗标记未读是不够的：一屏全是未读或全是已读时，加粗没有参照物。"""
    card = NotificationCard(_notif(is_read=False))
    try:
        assert card.unreadDot.isVisible() or not card.unreadDot.isHidden()
        assert card.unreadDot.width() > 0 and card.unreadDot.height() > 0
    finally:
        card.deleteLater()


def test_secondary_text_uses_the_mandated_contrast_pair(qt_app):
    """CLAUDE.md §3：次要文字必须显式给 `(96,96,96)/(210,210,210)`。"""
    card = NotificationCard(_notif())
    try:
        light, dark = _SECONDARY_COLORS
        assert (light.red(), light.green(), light.blue()) == (96, 96, 96)
        assert (dark.red(), dark.green(), dark.blue()) == (210, 210, 210)

        setTheme(Theme.LIGHT)
        assert _winning_color(card.msgLabel) == light
        setTheme(Theme.DARK)
        QApplication.processEvents()
        assert _winning_color(card.msgLabel) == dark
    finally:
        card.deleteLater()


@pytest.mark.parametrize(
    ("severity", "level"),
    [("critical", "ERROR"), ("warning", "WARNING")],
)
@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_severity_colors_match_the_timeline(qt_app, severity, level, theme):
    """同一件事在时间线和消息中心里不该是两种红。"""
    setTheme(theme)
    card = NotificationCard(_notif(severity))
    try:
        assert _winning_color(card.titleLabel) == _level_color(level)
    finally:
        card.deleteLater()


# ── severity 图标（原来那个 AttributeError）────────────────────


@pytest.mark.parametrize(
    ("severity", "expected"),
    [
        ("critical", InfoBarIcon.ERROR),
        ("warning", InfoBarIcon.WARNING),
        ("info", InfoBarIcon.INFORMATION),
        ("unknown-future-value", InfoBarIcon.INFORMATION),
    ],
)
def test_severity_icon_exists_for_every_severity(qt_app, severity, expected):
    """原来写的是 `FluentIcon.ERROR`，而库里没有这个成员。

    唯一的 `severity="critical"` 生产者是 `download/quality_guard.py` 的
    `on_quality_warning()`（队列自动暂停）—— 也就是说队列刚被暂停、最需要看消息的
    那一刻，整个铃铛面板一条都渲染不出来。
    """
    card = NotificationCard(_notif(severity))
    try:
        assert card._severity_icon() is expected
        assert isinstance(card._severity_icon(), FluentIconBase)
    finally:
        card.deleteLater()


def test_severity_icon_is_theme_aware(qt_app):
    """`InfoBarIcon.path()` 按 `isDarkTheme()` 在 `_light`/`_dark` 两版 svg 间切，
    而 `IconWidget.paintEvent` 是绘制时才解析路径的 —— 切主题不用重建卡片。"""
    setTheme(Theme.LIGHT)
    light_path = InfoBarIcon.ERROR.path()
    setTheme(Theme.DARK)
    assert InfoBarIcon.ERROR.path() != light_path


def test_a_critical_notification_renders_in_the_list(qt_app):
    """整条路径的回归守卫：库里有 critical 时列表必须建得起来。"""
    notification_center.push(_notif("critical", title="队列已自动暂停"))
    listWidget = NotificationListWidget()
    try:
        assert listWidget.count() == 1
        assert [c.notif.severity for c in _cards(listWidget)] == ["critical"]
    finally:
        listWidget.deleteLater()


# ── 列表本体 ────────────────────────────────────────────────


def test_empty_state_replaces_the_scroll_area(qt_app):
    listWidget = NotificationListWidget()
    try:
        assert listWidget.count() == 0
        assert listWidget.scrollArea.isHidden()
        assert not listWidget.emptyLabel.isHidden()
    finally:
        listWidget.deleteLater()


def test_a_burst_of_pushes_reloads_once(qt_app):
    """一次批量失败会连推十条通知。

    每条都触发一次 `notification_updated`，直接重载就是把整份列表重建十次 —— 十次
    `deleteLater()` + 十次重建卡片，点着铃铛的时候能看到列表在抖。
    """
    listWidget = NotificationListWidget()
    reloads: list[int] = []
    listWidget.countChanged.connect(reloads.append)
    try:
        for i in range(5):
            notification_center.push(_notif(title=f"第 {i} 条"))
        assert reloads == []  # 还没回到事件循环，一次都没重建

        QApplication.processEvents()
        assert reloads == [5]
    finally:
        listWidget.deleteLater()


# ── 两个容器 ────────────────────────────────────────────────


def test_flyout_fits_content_with_a_screen_height_limit(qt_app):
    view = NotificationFlyoutView()
    try:
        assert view.width() <= view.screen().availableGeometry().width()
        empty_height = view.height()
        for _ in range(3):
            notification_center.push(_notif())
        QApplication.processEvents()
        assert view.height() > empty_height
        for _ in range(20):
            notification_center.push(_notif())
        QApplication.processEvents()
        assert view.height() <= min(560, view.screen().availableGeometry().height())
    finally:
        view.deleteLater()


def test_flyout_height_is_frozen_once_it_is_visible(qt_app):
    """显示之后不再改高度 —— `Flyout` 已按当时的 sizeHint 摆好位置，改高度会让它从
    铃铛底下错开。"""
    view = NotificationFlyoutView()
    view.show()
    try:
        before = view.height()
        for _ in range(3):
            notification_center.push(_notif())
        QApplication.processEvents()
        assert view.height() == before
    finally:
        view.deleteLater()


def test_flyout_asks_the_main_window_to_detach(qt_app):
    """浮窗自己不开窗口：只有主窗口握着 `Flyout` 的句柄，得先关浮窗再开窗口，
    否则新窗口一拿到焦点浮窗就自己消失，看着像闪了一下。"""
    view = NotificationFlyoutView()
    fired: list[bool] = []
    view.detachRequested.connect(lambda: fired.append(True))
    try:
        view.detachBtn.click()
        assert fired == [True]
    finally:
        view.deleteLater()


def test_window_shares_the_list_with_the_flyout(qt_app):
    """同一份列表实现，区别只在容器 —— 两处各写一遍就会各自漂。"""
    anchor = QWidget()
    anchor.resize(1000, 700)
    notification_center.push(_notif("critical"))
    window = NotificationWindow(anchor)
    try:
        assert isinstance(window.listWidget, NotificationListWidget)
        assert window.listWidget.count() == 1
    finally:
        window.close()
        anchor.deleteLater()


def test_window_is_narrower_than_the_default(qt_app):
    """一列卡片，宽了只会让每张卡片中间空出一大片。"""
    assert NotificationWindow.SIZE_BOUNDS[0][1] < StandaloneWindow.SIZE_BOUNDS[0][1]


def test_window_title_bar_and_header_both_say_it(qt_app):
    """`ThemedTitleBar` 刻意不画标题文字，所以窗口名字得在内容区出现一次。"""
    anchor = QWidget()
    anchor.resize(1000, 700)
    window = NotificationWindow(anchor)
    try:
        assert window.windowTitle() == "消息中心"
        assert window.titleLabel.text() == "消息中心"
    finally:
        window.close()
        anchor.deleteLater()


def test_mark_all_as_read_clears_every_dot(qt_app):
    anchor = QWidget()
    anchor.resize(1000, 700)
    for _ in range(3):
        notification_center.push(_notif())
    window = NotificationWindow(anchor)
    try:
        window.clearAllBtn.click()
        QApplication.processEvents()
        cards = _cards(window.listWidget)
        assert cards and all(c.notif.is_read for c in cards)
        assert all(c.unreadDot.isHidden() for c in cards)
    finally:
        window.close()
        anchor.deleteLater()


def test_font_weights_come_from_getfont(qt_app):
    """字号走 `getFont()` 的像素度量，不混磅值 —— 混用会让行高不齐。"""
    card = NotificationCard(_notif(is_read=False))
    try:
        font = card.titleLabel.font()
        assert font.weight() == QFont.Weight.Bold
        assert font.pixelSize() == 14
        assert font.pointSize() == -1  # 没有走磅值那条路
    finally:
        card.deleteLater()


def test_english_long_notifications_fit_narrow_window(qt_app):
    from PySide6.QtCore import QTranslator

    translator = QTranslator()
    assert translator.load(str(Path(__file__).parents[1] / "assets/locales/fluentytdl_en_US.qm"))
    qt_app.installTranslator(translator)
    notification_center.push(
        Notification(
            type="info",
            title="A long notification title with a component version and several words",
            message="A long message with instructions and a URL " + "x" * 180,
        )
    )
    window = NotificationWindow()
    window.resize(380, 420)
    window.show()
    try:
        for _ in range(3):
            qt_app.processEvents()
        button = window.clearAllBtn
        assert button.width() >= button.sizeHint().width()
        card = _cards(window.listWidget)[0]
        assert card.width() <= window.listWidget.scrollArea.viewport().width()
        for label in (card.titleLabel, card.msgLabel):
            assert label.height() >= label.heightForWidth(label.width())
            assert label.geometry().right() < card.width()
        assert window.titleLabel.geometry().bottom() < button.geometry().top()
    finally:
        window.close()
        qt_app.removeTranslator(translator)
