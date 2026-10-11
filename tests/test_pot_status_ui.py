"""Independent panel lifecycle and recoverable startup prompts."""

import importlib
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QObject, QTimer, QTranslator, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication
from qfluentwidgets import (
    CaptionLabel,
    FluentIcon,
    FluentWindow,
    StrongBodyLabel,
    Theme,
    qconfig,
    setTheme,
)

from fluentytdl.core.dependency_manager import dependency_manager
from fluentytdl.ui.components.settings.component_card import ComponentSettingCard
from fluentytdl.youtube.pot_manager import POTSignals

panel = importlib.import_module("fluentytdl.ui.components.dialogs.pot_status_window")
startup = importlib.import_module("fluentytdl.ui.pot_startup_controller")
component_ui = importlib.import_module("fluentytdl.ui.components.settings.component_card")
dependency = importlib.import_module("fluentytdl.core.dependency_manager")

pytestmark = pytest.mark.windows_gui


@pytest.fixture
def service(monkeypatch):
    app = QApplication.instance() or QApplication([])
    state = dict(
        enabled=True,
        running=False,
        state="unavailable",
        code="pot_timeout",
        phase="token",
        checked_at=0,
        incident=1,
        components={},
        history=[],
        generation=1,
    )
    backend = SimpleNamespace(
        signals=POTSignals(),
        snapshot=lambda: dict(state),
        status_brief=lambda snapshot=None: (snapshot or state)["state"],
        is_running=lambda: bool(state.get("pid")),
        stop_server=Mock(),
    )
    monkeypatch.setattr(panel, "pot_manager", backend)
    monkeypatch.setattr(startup, "pot_manager", backend)
    yield app, backend, state
    window = panel.PotStatusWindow.singleton()
    if window:
        window.close()
    app.processEvents()


def test_panel_replays_status_and_close_preserves_service(service, dispose_qt_widget):
    app, backend, state = service
    window = panel.PotStatusWindow.show_singleton()
    assert panel.PotStatusWindow.show_singleton() is window
    assert window.statusLabel.text() == window.tr("未运行")
    assert panel.failure_text("pot_timeout") in window.reasonLabel.text()
    assert "pot_timeout" in window.reasonLabel.text()
    state.update(pid=1234, running=True)
    backend.signals.state_changed.emit(dict(state))
    app.processEvents()
    assert window.statusLabel.text() == window.tr("运行中")
    retry = []
    backend.signals.retry_requested.connect(lambda: retry.append(True))
    window.restartButton.click()
    assert retry == [True]
    dispose_qt_widget(window)
    backend.stop_server.assert_not_called()


def test_disabled_panel_has_enable_action(service, dispose_qt_widget):
    app, backend, state = service
    state.update(enabled=False, state="stopped")
    values = []
    backend.signals.enabled_requested.connect(values.append)
    window = panel.PotStatusWindow.show_singleton()
    assert not window.checkButton.isEnabled()
    window.restartButton.click()
    assert values == [True]
    dispose_qt_widget(window)


def test_open_during_component_check_shows_busy(service, monkeypatch, dispose_qt_widget):
    monkeypatch.setattr(dependency_manager, "is_checking", lambda key: key == "pot-provider")
    card = ComponentSettingCard("pot-provider", FluentIcon.CERTIFICATE, "POT", "")
    assert not card.repairAction.isEnabled()
    assert not card.actionButton.isEnabled()
    assert card.statusButton.isEnabled()
    dispose_qt_widget(card)


def test_component_card_replays_progress_and_preserves_local_version_on_error(
    service, monkeypatch, dispose_qt_widget
):
    from fluentytdl.core.dependency_manager import ComponentInfo

    info = ComponentInfo("deno", "Deno", "deno.exe")
    info.local_result = {"current": "2.9.7", "source": "managed"}
    info.last_error = {"code": "update_network"}
    info.operation_status = "waiting"
    info.progress = 100
    monkeypatch.setitem(dependency_manager.components, "deno", info)
    monkeypatch.setattr(dependency_manager, "is_installing", lambda k: k == "deno")
    card = ComponentSettingCard("deno", FluentIcon.CODE, "Deno", "")
    assert "2.9.7" in card.currentLabel.text()
    assert card.tr("等待当前任务结束") in card.currentLabel.text()
    assert card.progressBar.value() == 100
    assert not card.importAction.isEnabled()
    assert not card.actionButton.isEnabled()
    assert component_ui.translate("update_network") in card.currentLabel.toolTip()
    dispose_qt_widget(card)


def test_pot_menu_action_follows_enabled_state_and_waits_for_stop(service, dispose_qt_widget):
    _, _, state = service
    card = ComponentSettingCard("pot-provider", FluentIcon.CERTIFICATE, "POT", "")
    values = []
    card.enabled_requested.connect(values.append)
    state.update(enabled=True, running=True, state="ready", code="")
    card.set_runtime(dict(state), "ready")
    assert card.enabledAction.text() == card.tr("停用 POT")
    assert not card.enabledAction.isCheckable()
    card.enabledAction.trigger()
    assert values == [False]
    for phase in ("stopping", "waiting"):
        state.update(enabled=False, state=phase)
        card.set_runtime(dict(state), phase)
        assert not card.enabledAction.isEnabled()
    state.update(state="stopped", running=False)
    card.set_runtime(dict(state), "stopped")
    assert card.enabledAction.text() == card.tr("启用 POT")
    assert card.enabledAction.isEnabled()
    card.enabledAction.trigger()
    assert values == [False, True]
    dispose_qt_widget(card)


def test_panel_uses_one_snapshot_and_disables_retry_while_stopping(service, dispose_qt_widget):
    app, backend, state = service
    state.update(enabled=False, running=True, state="stopping")
    window = panel.PotStatusWindow.show_singleton()
    assert window.statusLabel.text() == window.tr("运行中")
    assert window.stateLabel.text() == "stopping"
    assert not window.checkButton.isEnabled()
    assert not window.restartButton.isEnabled()
    state.update(running=False, state="stopped")
    backend.signals.state_changed.emit(dict(state))
    app.processEvents()
    assert window.statusLabel.text() == window.tr("未运行")
    assert window.restartButton.isEnabled()
    assert not window.checkButton.isEnabled()
    dispose_qt_widget(window)


def test_panel_shows_actual_checks_and_separates_verification_from_restart(
    service, dispose_qt_widget
):
    app, backend, state = service
    state.update(
        running=True,
        state="ready",
        code="",
        components=dict(deno="passed", server="passed", plugin="passed", token="passed"),
        token_length=124,
        checked_at=1780000000,
        deno_version="2.9.7",
        version="2.0.2",
        port=12345,
    )
    checks, restarts = [], []
    backend.signals.check_requested.connect(lambda: checks.append(True))
    backend.signals.retry_requested.connect(lambda: restarts.append(True))
    window = panel.PotStatusWindow.show_singleton()
    assert "2.9.7" in window.checkLabels["deno"].text()
    assert "2.0.2" in window.checkLabels["server"].text()
    assert "12345" in window.checkLabels["server"].text()
    assert window.checkLabels["plugin"].text() == window.tr("已由 yt-dlp 加载官方 HTTP 插件")
    assert "124" in window.checkLabels["token"].text()
    assert (
        datetime.fromtimestamp(state["checked_at"]).strftime("%Y-%m-%d %H:%M:%S")
        in window.checkedLabel.text()
    )
    window.checkButton.click()
    assert checks == [True] and not restarts
    assert not window.checkButton.isEnabled() and not window.restartButton.isEnabled()
    backend.signals.state_changed.emit(dict(state))
    app.processEvents()
    window.restartButton.click()
    assert restarts == [True] and checks == [True]
    dispose_qt_widget(window)


def test_token_failure_preserves_confirmed_plugin_and_stopped_state_clears_results(
    service, dispose_qt_widget
):
    app, backend, state = service
    state.update(
        running=True,
        components=dict(deno="passed", server="passed", plugin="passed", token="failed"),
        token_length=124,
    )
    window = panel.PotStatusWindow.show_singleton()
    assert window.checkLabels["plugin"].text() == window.tr("已由 yt-dlp 加载官方 HTTP 插件")
    assert window.checkLabels["token"].text() == window.tr("失败")
    state.update(enabled=False, running=False, state="stopped")
    backend.signals.state_changed.emit(dict(state))
    app.processEvents()
    assert all(label.text() == window.tr("未验证") for label in window.checkLabels.values())
    dispose_qt_widget(window)


@pytest.mark.parametrize("locale", ["zh_CN", "en_US"])
@pytest.mark.parametrize("theme", [Theme.LIGHT, Theme.DARK])
def test_failure_details_fit_minimum_width_without_compressing_checks(
    service, dispose_qt_widget, locale, theme
):
    app, backend, state = service
    previous_font, previous_theme = app.font(), qconfig.theme
    font_ids = []
    for name in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf", "segoeuib.ttf"):
        font_path = Path("C:/Windows/Fonts") / name
        if font_path.exists():
            font_ids.append(QFontDatabase.addApplicationFont(str(font_path)))
    if font_ids:
        assert min(font_ids) >= 0
        app.setFont(QFont("Microsoft YaHei"))
    translator = QTranslator()
    assert translator.load(
        str(Path(__file__).parents[1] / f"assets/locales/fluentytdl_{locale}.qm")
    )
    app.installTranslator(translator)
    setTheme(theme)
    state.update(
        state="ready",
        code="",
        running=True,
        components=dict(deno="passed", server="passed", plugin="passed", token="passed"),
        checked_at=1780000000,
        token_length=124,
        deno_version="2.9.7",
        version="2.0.2",
        port=12345,
    )
    window = panel.PotStatusWindow.show_singleton()
    try:
        window.resize(580, 320)
        app.processEvents()
        state.update(state="unavailable", code="pot_cli_probe_failed")
        state["components"]["token"] = "failed"
        backend.signals.state_changed.emit(dict(state))
        app.processEvents()
        # Narrowing after a status change must account for additional wrapped lines.
        window.resize(500, 320)
        app.processEvents()
        assert window.width() == 500
        assert window.reasonLabel.isVisible()
        for label in window.findChildren(CaptionLabel) + window.findChildren(StrongBodyLabel):
            if not label.isVisible():
                continue
            required = (
                label.heightForWidth(label.width())
                if label.wordWrap()
                else label.sizeHint().height()
            )
            assert label.height() >= required, label.text()
            assert label.geometry().bottom() < window.checkButton.geometry().top(), label.text()
        assert window.restartButton.geometry().bottom() < window.height()

        state.update(state="ready", code="")
        state["components"]["token"] = "passed"
        backend.signals.state_changed.emit(dict(state))
        app.processEvents()
        window.resize(500, 320)
        app.processEvents()
        assert not window.reasonLabel.isVisible()
        assert window.height() == 320  # Recovering does not force an oversized window.
    finally:
        dispose_qt_widget(window)
        app.removeTranslator(translator)
        setTheme(previous_theme)
        app.setFont(previous_font)
        for font_id in font_ids:
            QFontDatabase.removeApplicationFont(font_id)


@pytest.mark.parametrize("key", ["yt-dlp", "ffmpeg", "deno", "pot-provider", "atomicparsley"])
@pytest.mark.parametrize("cached", [False, True])
def test_missing_component_click_resolves_metadata_and_starts_download(
    service, monkeypatch, tmp_path, dispose_qt_widget, key, cached
):
    manager = dependency.DependencyManager()
    monkeypatch.setattr(component_ui, "dependency_manager", manager)
    monkeypatch.setattr(manager, "get_exe_path", lambda k: tmp_path / k / "tool.exe")
    info = manager.components[key]
    info.local_result = {"current": "unknown", "source": ""}
    result = {
        "current": "unknown",
        "latest": "2.0.2",
        "latest_channel": dependency.config_manager.get("ytdlp_channel"),
        "update_available": True,
        "url": "https://example.test/component.zip",
        "expected_sha256": "a" * 64,
    }
    if cached:
        manager._on_check_finished(key, dict(result))
    checks, downloads = [], []

    def check(component):
        checks.append(component)
        manager._workers[f"check_{component}"] = SimpleNamespace(isRunning=lambda: True)
        manager.check_started.emit(component)

    class Download(QObject):
        progress_signal = Signal(str, int)
        status_signal = Signal(str, str)
        finished_signal = Signal(str)
        error_signal = Signal(str, str)

        def __init__(self, component, url, target, **kwargs):
            super().__init__(kwargs["parent"])
            self.component, self.url = component, url

        def start(self):
            downloads.append((self.component, self.url))

    monkeypatch.setattr(manager, "check_update", check)
    monkeypatch.setattr(dependency, "DownloaderWorker", Download)
    card = ComponentSettingCard(key, FluentIcon.DOWNLOAD, info.name, "")
    assert card.actionButton.text() == card.tr("安装组件")
    card.actionButton.click()
    if not cached:
        assert checks == [key] and not downloads
        assert not card.actionButton.isEnabled()
        manager._on_check_finished(key, dict(result))
    assert downloads == [(key, result["url"])]
    assert manager.is_installing(key)
    assert not card.actionButton.isEnabled()
    dispose_qt_widget(card)


def test_failed_first_install_keeps_install_action_for_retry(
    service, monkeypatch, dispose_qt_widget
):
    manager = dependency.DependencyManager()
    monkeypatch.setattr(component_ui, "dependency_manager", manager)
    manager.components["deno"].local_result = {"current": "unknown", "source": ""}
    checks = []
    monkeypatch.setattr(manager, "check_update", checks.append)
    card = ComponentSettingCard("deno", FluentIcon.CODE, "Deno", "")
    card.actionButton.click()
    manager._on_check_error("deno", "update_network")
    assert card.actionButton.isEnabled()
    assert card.actionButton.text() == card.tr("安装组件")
    card.actionButton.click()
    assert checks == ["deno", "deno"]
    assert manager._url_recheck_pending == {"deno"}
    dispose_qt_widget(card)


def test_pot_update_offer_clears_only_after_active_bundle_readback(
    service, monkeypatch, dispose_qt_widget
):
    manager = dependency.DependencyManager()
    monkeypatch.setattr(component_ui, "dependency_manager", manager)
    info = manager.components["pot-provider"]
    info.last_result = {
        "latest": "2.0.2",
        "url": "https://example.test/plugin.zip",
        "update_available": True,
        "bundle": {"bundle_id": "pot-original-v2.0.2-r1"},
    }
    card = ComponentSettingCard("pot-provider", FluentIcon.CERTIFICATE, "POT", "")
    manager._on_local_result("pot-provider", {"current": "unknown", "local_epoch": 0})
    assert info.last_result["update_available"]
    manager._on_local_result(
        "pot-provider",
        {
            "current": "2.0.2",
            "current_bundle_id": "pot-original-v2.0.2-r1",
            "local_epoch": 0,
        },
    )
    assert not info.last_result["update_available"]
    assert card._action == "check"
    assert "→" not in card.currentLabel.text()
    dispose_qt_widget(card)


def test_startup_failure_replay_and_retry_only_once_per_incident(
    service, monkeypatch, dispose_qt_widget
):
    app, backend, state = service
    notifications = []
    monkeypatch.setattr(startup.notification_center, "push", notifications.append)
    window = FluentWindow()
    window.show()
    controller = startup.PotStartupController(window)
    retries = []
    backend.signals.retry_requested.connect(lambda: retries.append(True))
    QTimer.singleShot(
        0, lambda: controller._dialog.yesButton.click() if controller._dialog else None
    )
    controller._show_pending()
    assert retries == [True] and len(notifications) == 1
    controller._on_state(dict(state))
    assert controller._pending is None
    assert notifications[0].metadata["action"] == "manage_pot"
    controller._close()
    dispose_qt_widget(window)
