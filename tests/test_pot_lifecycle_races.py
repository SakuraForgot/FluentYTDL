"""Deterministic lifecycle interleavings without real processes or line hooks."""

import importlib
import io
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PySide6.QtWidgets import QApplication

from fluentytdl.core import runtime_activity as activity

pot = importlib.import_module("fluentytdl.youtube.pot_manager")


@pytest.fixture
def manager(monkeypatch, tmp_path):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(activity, "_condition", threading.Condition(threading.RLock()))
    monkeypatch.setattr(activity, "_processes", set())
    monkeypatch.setattr(activity, "_changing", False)
    monkeypatch.setattr(activity, "_readers", 0)
    monkeypatch.setattr(pot.POTManager, "_setup_job_object", lambda self: None)
    monkeypatch.setattr(pot.atexit, "register", lambda callback: None)
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", True)
    m = object.__new__(pot.POTManager)
    m.__init__()
    bundle = SimpleNamespace(root=tmp_path, version="2.0.2", bundle_id="official")
    monkeypatch.setattr(pot, "resolve_bundle", lambda **kwargs: bundle)
    monkeypatch.setattr(pot, "select_verified_deno", lambda *args: None)
    monkeypatch.setattr(
        m,
        "_choose_runtime",
        lambda bundle: SimpleNamespace(
            path=tmp_path / "deno.exe", version="2.9.7", source="managed"
        ),
    )
    monkeypatch.setattr(m, "_ping_owned", Mock())
    monkeypatch.setattr(m, "_schedule_warm_retry", Mock())
    m._bundle = bundle

    def start(*args):
        with m._lock:
            m._process = Mock(pid=1234)
            m._process.poll.return_value = None
            m._is_running = True

    def probe(**kwargs):
        kwargs["on_plugin"]()
        m._last_token_len = 124
        return True, "ok"

    monkeypatch.setattr(m, "_start_owned", start)
    monkeypatch.setattr(m, "probe_ytdlp_provider", probe)
    start()
    m._warm_event.set()
    m._last_token_len = 124
    m._state.update(state="ready")
    yield m
    pot.config_manager.configChanged.disconnect(m._config_changed)
    m.stop_server()
    app.processEvents()


def test_disable_after_readonly_generation_check_waits_for_active_download(manager, monkeypatch):
    m = manager
    active = Mock(poll=lambda: None)
    activity._processes.add(active)
    checked, release = threading.Event(), threading.Event()
    check_current = m._check_current
    checks = []

    def pause_after_valid_check(generation, cancel):
        check_current(generation, cancel)
        checks.append(generation)
        if len(checks) == 1:
            checked.set()
            assert release.wait(3)

    monkeypatch.setattr(m, "_check_current", pause_after_valid_check)
    server = m._process
    m.request_check()
    reader = m._warm_thread
    try:
        assert checked.wait(2)
        assert activity._readers == 1 and not activity._changing
        monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", False)
        m._invalidate()
        release.set()
        reader.join(2)
        assert not reader.is_alive()
        assert m._process is server
        server.terminate.assert_not_called()
    finally:
        release.set()
        active.poll = lambda: 0
        reader.join(2)
        writer = m._warm_thread
        if writer:
            writer.join(2)
    assert m.snapshot()["state"] == "stopped" and not m.is_running()
    server.terminate.assert_called_once()


def test_exit_evidence_revocation_cannot_race_a_restart(manager, monkeypatch):
    m = manager
    old = m._process
    old.stdout = io.BytesIO()
    old.returncode = old.poll.return_value = 1
    revoking, release = threading.Event(), threading.Event()
    restarted, attempted = threading.Event(), threading.Event()
    clear = m._warm_event.clear
    errors = []

    def pause_old_revocation():
        if threading.current_thread() is reader:
            revoking.set()
            assert release.wait(3)
        clear()

    def restart():
        try:
            attempted.set()
            m.request_retry()
            worker = m._warm_thread
            if worker:
                worker.join(2)
            restarted.set()
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=m._read_server, args=(old,))
    requester = threading.Thread(target=restart)
    monkeypatch.setattr(m._warm_event, "clear", pause_old_revocation)
    reader.start()
    try:
        assert revoking.wait(2)
        requester.start()
        assert attempted.wait(2)
        # Without the lifecycle lock, restart completes before the stale clear,
        # and releasing the old callback then revokes the replacement's state.
        restarted.wait(0.2)
    finally:
        release.set()
        reader.join(3)
        if requester.ident is not None:
            requester.join(3)
    assert not errors and not reader.is_alive() and not requester.is_alive()
    assert restarted.is_set() and m._process is not old
    assert m.is_warm and m.snapshot()["state"] == "ready"


def test_late_exit_publication_cannot_replace_new_process_state(manager, monkeypatch):
    m = manager
    old = m._process
    old.stdout = io.BytesIO()
    old.returncode = old.poll.return_value = 1
    publishing, release = threading.Event(), threading.Event()
    publish = m._publish

    def pause_exit_publication(state, **kwargs):
        if state == "unavailable":
            publishing.set()
            assert release.wait(3)
        publish(state, **kwargs)

    monkeypatch.setattr(m, "_publish", pause_exit_publication)
    reader = threading.Thread(target=m._read_server, args=(old,))
    reader.start()
    try:
        assert publishing.wait(2)
        m.request_retry()
        worker = m._warm_thread
        if worker:
            worker.join(2)
        assert m._process is not old and m.is_warm
        release.set()
        reader.join(2)
        assert not reader.is_alive()
        assert m.is_warm and m.snapshot()["state"] == "ready"
        m._schedule_warm_retry.assert_not_called()
    finally:
        release.set()
        reader.join(2)
