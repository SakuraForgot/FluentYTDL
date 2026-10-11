"""Bounded retries, stale completions and readiness are independent of TCP success."""

import importlib
import io
import threading
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pot = importlib.import_module("fluentytdl.youtube.pot_manager")


@pytest.fixture
def manager(monkeypatch):
    monkeypatch.setattr(pot.POTManager, "_setup_job_object", lambda self: None)
    monkeypatch.setattr(pot.atexit, "register", lambda callback: None)
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", True)
    m = object.__new__(pot.POTManager)
    m.__init__()
    yield m
    pot.config_manager.configChanged.disconnect(m._config_changed)
    m._warm_thread = None
    m.stop_server()


@pytest.fixture
def timers(monkeypatch):
    calls = []

    class Timer:
        def __init__(self, delay, callback):
            self.delay, self.callback, self.cancelled = delay, callback, False
            calls.append(self)

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    class Thread:
        def __init__(self, **kw):
            self.options, self.started = kw, False

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

    monkeypatch.setattr(
        pot,
        "threading",
        SimpleNamespace(
            Timer=Timer,
            Thread=Thread,
            Event=threading.Event,
            RLock=threading.RLock,
            current_thread=threading.current_thread,
        ),
    )
    return calls


def test_only_three_automatic_retries(manager, timers):
    for _ in range(5):
        manager._schedule_warm_retry("pot_token_rejected")
    assert [t.delay for t in timers] == [30, 120, 300]
    assert manager._warm_attempts == 3
    assert manager.snapshot()["retry_at"] == 0


def test_retry_timer_wakes_without_another_link(manager, timers, monkeypatch):
    run = Mock()
    monkeypatch.setattr(manager, "ensure_warm_async", run)
    manager._schedule_warm_retry("pot_timeout")
    timers[-1].callback()
    run.assert_called_once()


def test_stale_timer_cannot_restart_new_generation(manager, timers, monkeypatch):
    run = Mock()
    monkeypatch.setattr(manager, "ensure_warm_async", run)
    manager._schedule_warm_retry("pot_timeout")
    manager._generation += 1
    timers[-1].callback()
    run.assert_not_called()


def test_manual_retry_bypasses_backoff_and_reuses_one_worker(manager, timers):
    manager._schedule_warm_retry("pot_timeout")
    manager.ensure_warm_async()
    assert manager._warm_thread is None
    manager.request_retry()
    first = manager._warm_thread
    assert first.is_alive() and manager._warm_attempts == 0
    manager.request_retry()
    assert manager._warm_thread is first


def test_missing_component_waits_for_repair_not_timer(manager, timers):
    manager._schedule_warm_retry("pot_package_missing")
    assert timers == []


def test_explicit_disabled_never_starts(manager, timers, monkeypatch):
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", False)
    manager.request_retry()
    assert manager._warm_thread is None
    assert manager.snapshot()["state"] == "stopped"


@pytest.mark.parametrize("token_ok", [False, True])
def test_listening_server_is_not_readiness(manager, timers, tmp_path, monkeypatch, token_ok):
    monkeypatch.setattr(pot, "active_count", lambda: 0)
    monkeypatch.setattr(pot, "changing_components", lambda cancel: nullcontext())
    bundle = SimpleNamespace(root=tmp_path, bundle_id="pot-original-v2.0.2-r1", version="2.0.2")
    monkeypatch.setattr(pot, "resolve_bundle", lambda **kw: bundle)
    monkeypatch.setattr(
        manager,
        "_choose_runtime",
        lambda b: SimpleNamespace(path=tmp_path / "deno.exe", version="2.9.7", source="managed"),
    )
    monkeypatch.setattr(pot, "select_verified_deno", lambda *a: None)
    monkeypatch.setattr(manager, "_start_owned", lambda *a: None)
    monkeypatch.setattr(manager, "is_running", lambda: True)

    def probe(**kw):
        kw["on_plugin"]()
        return token_ok, "ok" if token_ok else "pot_token_rejected"

    monkeypatch.setattr(manager, "probe_ytdlp_provider", probe)
    manager._warm_worker()
    assert manager.is_warm is token_ok
    assert manager.snapshot()["state"] == ("ready" if token_ok else "unavailable")
    assert manager.snapshot()["components"]["plugin"] == "passed"
    assert manager.snapshot()["checked_at"] > 0
    if not token_ok:
        assert manager.snapshot()["components"]["token"] == "failed"
        assert timers[-1].delay == 30


@pytest.mark.parametrize("healthy", [False, True])
def test_recheck_pings_existing_process_without_restarting(manager, monkeypatch, healthy):
    manager._process = Mock(pid=1234)
    manager._process.poll.return_value = None
    manager._is_running = True
    manager._bundle = SimpleNamespace(version="2.0.2")
    manager._active_port = 12345
    reply = Mock(return_value=io.BytesIO(b'{"version": "2.0.2"}'))
    if not healthy:
        reply.side_effect = OSError("service not responding")
    monkeypatch.setattr(manager, "_local_urlopen", reply)
    if healthy:
        manager._start_owned(pot.time.monotonic() + 10, threading.Event())
    else:
        with pytest.raises(pot.ComponentError, match="pot_local_health_failed"):
            manager._start_owned(pot.time.monotonic() + 10, threading.Event())
    assert reply.call_args.args[0] == "http://127.0.0.1:12345/ping"
    manager._process.terminate.assert_not_called()


def test_new_verification_clears_old_token_and_restart_intent_is_explicit(manager, timers):
    manager._last_token_len = 124
    manager._state["components"] = {"token": "passed"}
    manager.request_check()
    assert manager._warm_thread.options["args"][-1] is False
    assert manager.snapshot()["token_length"] == 0
    assert manager.snapshot()["components"] == {}
    manager._warm_thread = None
    manager.request_retry()
    assert manager._warm_thread.options["args"][-1] is True


def test_stop_does_not_claim_a_new_verification(manager, monkeypatch):
    manager._last_token_len = 124
    manager._state.update(checked_at=12345, components={"token": "passed"})
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", False)
    manager._warm_worker()
    state = manager.snapshot()
    assert state["checked_at"] == 12345
    assert state["components"] == {} and state["token_length"] == 0


def test_unexpected_exit_invalidates_service_and_token_evidence(manager, timers):
    process = Mock(stdout=io.BytesIO(), returncode=1)
    process.poll.return_value = 1
    manager._process, manager._is_running = process, True
    manager._last_token_len = 124
    manager._state.update(
        state="ready",
        checked_at=12345,
        components=dict(deno="passed", server="passed", plugin="passed", token="passed"),
    )
    manager._read_server(process)
    state = manager.snapshot()
    assert state["code"] == "pot_process_exited" and not state["running"]
    assert state["components"]["server"] == "failed"
    assert state["components"]["token"] == "unchecked" and state["token_length"] == 0
    assert state["checked_at"] == 12345


def test_old_completion_does_not_overwrite_new_state(manager):
    manager._generation = 3
    manager._publish("ready", generation=3)
    manager._publish("unavailable", generation=2, code="pot_timeout")
    assert manager.snapshot()["state"] == "ready"


def test_repeated_failure_is_one_incident_until_recovery(manager):
    manager._publish("unavailable", code="pot_timeout")
    incident = manager.snapshot()["incident"]
    manager._publish("checking")
    manager._publish("unavailable", code="pot_timeout")
    assert manager.snapshot()["incident"] == incident
    manager._publish("ready")
    manager._publish("unavailable", code="pot_process_exited")
    assert manager.snapshot()["incident"] == incident + 1


def test_reapplying_enabled_setting_does_not_restart_ready_service(manager, monkeypatch):
    save = Mock()
    monkeypatch.setattr(pot.config_manager, "set", save)
    manager.set_enabled(True)
    save.assert_not_called()
    manager.set_enabled(False)
    save.assert_called_once_with("pot_provider_enabled", False)


def test_disable_cancels_backoff_and_reports_stopping_during_probe(manager, timers, monkeypatch):
    manager._schedule_warm_retry("pot_timeout")
    timer = timers[-1]
    manager._process = Mock(pid=1234)
    manager._process.poll.return_value = None
    manager._is_running = True
    manager._warm_event.set()
    manager._warm_thread = SimpleNamespace(is_alive=lambda: True)
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", False)
    manager._config_changed("pot_provider_enabled", False)
    assert timer.cancelled and manager._retry_timer is None
    assert manager._cancel.is_set() and not manager.is_warm
    state = manager.snapshot()
    assert state["state"] == "stopping" and state["running"] and not state["enabled"]
    assert state["retry_at"] == 0
    manager._schedule_warm_retry("pot_process_exited")
    assert len(timers) == 1


def test_disable_waits_for_active_request_before_stopping_owned_server(manager, monkeypatch):
    from fluentytdl.core import runtime_activity as activity

    active = Mock()
    active.poll.return_value = None
    monkeypatch.setattr(activity, "_condition", threading.Condition(threading.RLock()))
    monkeypatch.setattr(activity, "_processes", {active})
    monkeypatch.setattr(activity, "_changing", False)
    waiting = threading.Event()

    @contextmanager
    def gate(cancel):
        waiting.set()
        with activity.changing_components(cancel):
            yield

    monkeypatch.setattr(pot, "changing_components", gate)
    server = Mock(pid=1234)
    server.poll.return_value = None
    manager._process, manager._is_running = server, True
    monkeypatch.setitem(pot.config_manager.config, "pot_provider_enabled", False)
    manager._config_changed("pot_provider_enabled", False)
    worker = manager._warm_thread
    try:
        assert waiting.wait(2)
        assert manager.snapshot()["state"] == "waiting"
        assert manager.is_running()
        server.terminate.assert_not_called()
    finally:
        active.poll.return_value = 0
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert manager.snapshot()["state"] == "stopped"
    assert not manager.is_running() and not manager._shutdown
    server.terminate.assert_called_once()
