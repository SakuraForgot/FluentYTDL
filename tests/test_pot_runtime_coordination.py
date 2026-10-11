"""Real worker/gate interleavings: checks, cancellation, and terminal UI actions."""

import importlib
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from fluentytdl.core import runtime_activity as activity

pot = importlib.import_module("fluentytdl.youtube.pot_manager")
cli = importlib.import_module("fluentytdl.youtube.yt_dlp_cli")
panel = importlib.import_module("fluentytdl.ui.components.dialogs.pot_status_window")
pytestmark = pytest.mark.windows_gui


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(activity, "_condition", threading.Condition(threading.RLock()))
    monkeypatch.setattr(activity, "_processes", set())
    monkeypatch.setattr(activity, "_changing", False)
    monkeypatch.setattr(activity, "_readers", 0)


@pytest.fixture
def manager(monkeypatch, tmp_path, gate):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(pot.POTManager, "_setup_job_object", lambda self: None)
    monkeypatch.setattr(pot.atexit, "register", lambda callback: None)
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", True)
    m = object.__new__(pot.POTManager)
    m.__init__()
    bundle = SimpleNamespace(root=tmp_path, version="2.0.2", bundle_id="official")
    monkeypatch.setattr(pot, "resolve_bundle", lambda **kw: bundle)
    monkeypatch.setattr(pot, "select_verified_deno", lambda *args: None)
    monkeypatch.setattr(
        m,
        "_choose_runtime",
        lambda b: SimpleNamespace(path=tmp_path / "deno.exe", version="2.9.7", source="managed"),
    )
    monkeypatch.setattr(m, "_ping_owned", Mock())
    monkeypatch.setattr(m, "_schedule_warm_retry", Mock())
    m._bundle = bundle

    def start(*args):
        m._process = Mock(pid=1234)
        m._process.poll.return_value = None
        m._is_running = True

    monkeypatch.setattr(m, "_start_owned", Mock(side_effect=start))
    start()
    m._warm_event.set()
    m._last_token_len = 124
    m._state.update(
        state="ready",
        components=dict(deno="passed", server="passed", plugin="passed", token="passed"),
    )
    yield m, app
    pot.config_manager.configChanged.disconnect(m._config_changed)
    m.stop_server()


def test_readonly_check_keeps_ready_and_accepts_new_download(manager, monkeypatch):
    m, _ = manager
    first = Mock()
    first.poll.return_value = None
    activity._processes.add(first)
    entered, release = threading.Event(), threading.Event()

    def probe(**kwargs):
        kwargs["on_plugin"]()
        entered.set()
        assert release.wait(3)
        return True, "ok"

    monkeypatch.setattr(m, "probe_ytdlp_provider", probe)
    monkeypatch.setattr(pot, "pot_manager", m)
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: None)
    monkeypatch.setattr(cli, "prepare_yt_dlp_env", lambda **kwargs: {})
    launch = Mock(return_value=Mock(poll=lambda: None))
    monkeypatch.setattr(activity.subprocess, "Popen", launch)
    original = m._process
    m.request_check()
    worker = m._warm_thread
    try:
        assert entered.wait(2), "verification must not wait for the existing download"
        assert m.is_warm and not activity._changing and activity._readers == 1
        assert activity.ytdlp_popen(["engine", "--extractor-args", m.get_extractor_args()])
        launch.assert_called_once()
    finally:
        release.set()
        worker.join(3)
    assert m.is_warm and m._process is original and activity._readers == 0
    m._start_owned.assert_not_called()


def test_component_activation_waits_for_readonly_check(gate):
    entered, done = threading.Event(), threading.Event()

    def update():
        entered.set()
        with activity.changing_components():
            done.set()

    with activity.reading_components(threading.Event()):
        owner = threading.Thread(target=update)
        owner.start()
        assert entered.wait(2)
        assert not done.wait(0.15)
    owner.join(2)
    assert done.is_set() and activity._readers == 0 and not activity._changing


@pytest.mark.parametrize("action", ["check", "restart"])
def test_terminal_click_is_queued_and_buttons_recover(manager, monkeypatch, action):
    m, app = manager
    terminal, release = threading.Event(), threading.Event()
    probes = []

    def probe(**kwargs):
        probes.append(True)
        kwargs["on_plugin"]()
        m._last_token_len = 124
        return True, "ok"

    monkeypatch.setattr(m, "probe_ytdlp_provider", probe)
    publish = m._publish

    def hold_terminal(state, **kwargs):
        publish(state, **kwargs)
        if state == "ready" and len(probes) == 1:
            terminal.set()
            assert release.wait(3)

    monkeypatch.setattr(m, "_publish", hold_terminal)
    monkeypatch.setattr(panel, "pot_manager", m)
    window = panel.PotStatusWindow()
    try:
        m.request_check()
        assert terminal.wait(2)
        app.processEvents()
        assert window.checkButton.isEnabled() and window.restartButton.isEnabled()
        (window.checkButton if action == "check" else window.restartButton).click()
        assert not window.checkButton.isEnabled()
        release.set()
        deadline = time.monotonic() + 3
        while m._warm_thread is not None and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        app.processEvents()
        assert m._warm_thread is None and len(probes) == 2
        assert m._start_owned.call_count == (1 if action == "restart" else 0)
        assert window.checkButton.isEnabled() and window.restartButton.isEnabled()
    finally:
        release.set()
        window.close()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("when", ["before_wait", "during_wait"])
def test_cancel_component_wait_never_launches(gate, monkeypatch, when):
    cancel, entered = threading.Event(), threading.Event()
    launch = Mock()
    monkeypatch.setattr(activity.subprocess, "Popen", launch)
    errors = []

    def check():
        entered.set()
        return cancel.is_set()

    def request():
        try:
            activity.ytdlp_popen(["engine"], cancel_check=check)
        except activity.YtDlpLaunchCancelled:
            errors.append("cancelled")

    with activity.changing_components():
        if when == "before_wait":
            cancel.set()
        worker = threading.Thread(target=request)
        worker.start()
        assert entered.wait(2)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive(), "cancellation must not wait for the component gate"
    assert errors == ["cancelled"]
    launch.assert_not_called()


def test_cancel_pot_wait_leaves_shared_readiness_worker_running(manager, monkeypatch):
    m, _ = manager
    release, waiting, cancel = threading.Event(), threading.Event(), threading.Event()
    m._warm_event.clear()
    warmer = threading.Thread(target=lambda: release.wait(3))
    warmer.start()
    m._warm_thread = warmer
    monkeypatch.setattr(pot, "pot_manager", m)
    launch = Mock()
    monkeypatch.setattr(activity.subprocess, "Popen", launch)
    errors = []

    def check():
        waiting.set()
        return cancel.is_set()

    def request():
        try:
            activity.ytdlp_popen(["engine", m.get_extractor_args()], cancel_check=check)
        except activity.YtDlpLaunchCancelled:
            errors.append("cancelled")

    worker = threading.Thread(target=request)
    worker.start()
    try:
        assert waiting.wait(2)
        cancel.set()
        worker.join(1)
        assert not worker.is_alive() and warmer.is_alive() and not m._cancel.is_set()
        assert errors == ["cancelled"]
        launch.assert_not_called()
    finally:
        release.set()
        worker.join(2)
        warmer.join(2)
        m._warm_thread = None


def test_cli_maps_prelaunch_cancellation_to_existing_outcome(gate, monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: tmp_path / "yt-dlp.exe")
    monkeypatch.setattr(cli, "ydl_opts_to_cli_args", lambda opts: [])
    monkeypatch.setattr(cli, "prepare_yt_dlp_env", lambda **kw: {})
    launch = Mock()
    monkeypatch.setattr(activity.subprocess, "Popen", launch)
    cancel = threading.Event()
    cancel.set()
    with activity.changing_components(), pytest.raises(cli.YtDlpCancelled):
        cli.run_dump_single_json("https://example.test/video", {}, cancel_event=cancel)
    launch.assert_not_called()


def test_readonly_failure_revokes_previous_ready(manager, monkeypatch):
    m, _ = manager
    monkeypatch.setattr(m, "probe_ytdlp_provider", lambda **kw: (False, "pot_token_rejected"))
    m.request_check()
    m._warm_thread.join(2)
    assert m.snapshot()["state"] == "unavailable"
    assert not m.is_warm and m.snapshot()["token_length"] == 0


def test_readonly_completion_cannot_certify_replaced_process(manager, monkeypatch):
    m, _ = manager

    def replace_process(**kwargs):
        m._process = Mock(poll=lambda: None)
        return True, "ok"

    monkeypatch.setattr(m, "probe_ytdlp_provider", replace_process)
    m.request_check()
    m._warm_thread.join(2)
    assert m.snapshot()["code"] == "pot_process_exited" and not m.is_warm
