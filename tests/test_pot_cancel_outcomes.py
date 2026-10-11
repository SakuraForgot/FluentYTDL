"""Cancelled POT/component waits retain each worker's existing terminal semantics."""

import importlib
import threading
from unittest.mock import Mock

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication

from fluentytdl.core import runtime_activity as activity
from fluentytdl.core.config_manager import config_manager
from fluentytdl.download import workers
from fluentytdl.utils.youtube_request import COOKIE_MODE

pot = importlib.import_module("fluentytdl.youtube.pot_manager")
cli = importlib.import_module("fluentytdl.youtube.yt_dlp_cli")


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(activity, "_condition", threading.Condition(threading.RLock()))
    monkeypatch.setattr(activity, "_processes", set())
    monkeypatch.setattr(activity, "_changing", False)
    monkeypatch.setattr(activity, "_readers", 0)
    for key, value in {
        "pot_provider_enabled": True,
        "youtube_cookies_enabled": False,
        "download_dir": str(tmp_path),
    }.items():
        monkeypatch.setitem(config_manager.config, key, value)
    monkeypatch.setattr(
        type(workers.youtube_service), "_maybe_configure_youtube_js_runtime", lambda *args: None
    )
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: tmp_path / "yt-dlp.exe")
    monkeypatch.setattr(cli, "prepare_yt_dlp_env", lambda **kwargs: {})
    launch, diagnosis = Mock(), Mock()
    monkeypatch.setattr(activity.subprocess, "Popen", launch)
    monkeypatch.setattr(workers, "_emit_failure_diagnosis", diagnosis)
    return app, launch, diagnosis


def _download_worker(monkeypatch, mode):
    opts = {COOKIE_MODE: False}
    if mode == "light":
        opts.update(skip_download=True, writesubtitles=True)
    elif mode == "cover":
        opts["__fluentytdl_is_cover_direct"] = True
    worker = workers.DownloadWorker("https://www.youtube.com/watch?v=jNQXAC9IVRw", opts)
    monkeypatch.setattr(worker, "_resolve_subtitle_prefs", lambda opts: None)
    monkeypatch.setattr(worker, "_fastpath_open_staging", lambda opts: None)
    monkeypatch.setattr(worker, "_fastpath_paths_args", lambda opts: [])
    monkeypatch.setattr(worker, "_finalize_staging_cancel", Mock())
    return worker


def _capture(worker):
    errors, completed, cancelled = [], [], []
    worker.error.connect(errors.append)
    if isinstance(worker, workers.DownloadWorker):
        worker.completed.connect(lambda: completed.append(True))
        worker.cancelled.connect(lambda: cancelled.append(True))
    else:
        worker.finished_all.connect(completed.append)
    return errors, completed, cancelled


@pytest.mark.parametrize("mode", ["channel", "full", "light"])
def test_real_pot_wait_cancellation_reaches_worker_without_failure(isolated, monkeypatch, mode):
    app, launch, diagnosis = isolated
    entered, release = threading.Event(), threading.Event()
    provider = object.__new__(pot.POTManager)
    provider._lock = threading.RLock()
    provider._warm_event, provider._cancel = threading.Event(), threading.Event()
    provider._warm_thread = threading.Thread(target=lambda: release.wait(5))
    monkeypatch.setattr(provider, "is_running", lambda: False)
    monkeypatch.setattr(provider, "ensure_warm_async", Mock())
    monkeypatch.setattr(pot, "pot_manager", provider)

    def cancellation_check(check):
        entered.set()
        activity.check_launch_cancelled(check)

    monkeypatch.setattr(pot, "check_launch_cancelled", cancellation_check)
    worker = (
        workers.ChannelExtractWorker("https://www.youtube.com/@example", ["videos"])
        if mode == "channel"
        else _download_worker(monkeypatch, mode)
    )
    errors, completed, cancelled = _capture(worker)
    thread = threading.Thread(target=worker.run)
    provider._warm_thread.start()
    thread.start()
    try:
        assert entered.wait(2), "the real option builder must reach the POT wait"
        worker.cancel()
        thread.join(1)
        app.processEvents()
        assert not thread.is_alive(), "request cancellation must leave the shared wait promptly"
        assert provider._warm_thread.is_alive() and not provider._cancel.is_set()
        assert errors == [] and completed == []
        diagnosis.assert_not_called()
        launch.assert_not_called()
        if mode == "channel":
            assert cancelled == []  # Closing a parse dialog is deliberately silent.
        else:
            assert worker._run_outcome == "cancelled" and cancelled == [True]
            worker._finalize_staging_cancel.assert_called_once()
    finally:
        release.set()
        provider._warm_thread.join(2)
        thread.join(2)
        worker.deleteLater()
        app.sendPostedEvents(worker, QEvent.Type.DeferredDelete)


@pytest.mark.parametrize("mode", ["light", "cover"])
def test_real_component_gate_cancellation_preserves_fastpath_outcome(isolated, monkeypatch, mode):
    app, launch, diagnosis = isolated
    monkeypatch.setattr(type(workers.youtube_service), "build_ydl_options", lambda *args, **kw: {})
    worker = _download_worker(monkeypatch, mode)
    errors, completed, cancelled = _capture(worker)
    waiting = threading.Event()

    def tracked_launch(command, **kwargs):
        check = kwargs.pop("cancel_check")
        calls = 0

        def checked():
            nonlocal calls
            calls += 1
            if calls >= 2:
                waiting.set()  # The second call is inside the component-change wait.
            return check()

        return activity.ytdlp_popen(command, cancel_check=checked, **kwargs)

    monkeypatch.setattr(workers, "ytdlp_popen", tracked_launch)
    thread = threading.Thread(target=worker.run)
    try:
        with activity.changing_components():
            thread.start()
            assert waiting.wait(2)
            worker.cancel()
            thread.join(1)
            assert not thread.is_alive(), "cancellation must not await component activation"
        app.processEvents()
        assert worker._run_outcome == "cancelled" and cancelled == [True]
        assert errors == [] and completed == []
        worker._finalize_staging_cancel.assert_called_once()
        diagnosis.assert_not_called()
        launch.assert_not_called()
    finally:
        worker.cancel()
        if thread.ident is not None:
            thread.join(2)
        worker.deleteLater()
        app.sendPostedEvents(worker, QEvent.Type.DeferredDelete)
