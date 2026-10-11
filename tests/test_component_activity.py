"""Updates wait only at activation, and never interrupt owned active requests."""

import io
import json
import threading
from types import SimpleNamespace

import pytest

from fluentytdl.core import runtime_activity as activity
from fluentytdl.core import updater_worker as installer
from fluentytdl.utils.diagnostic_text import redact_detail


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(activity, "_condition", threading.Condition(threading.RLock()))
    monkeypatch.setattr(activity, "_processes", set())
    monkeypatch.setattr(activity, "_changing", False)


def test_activation_drains_current_process_and_holds_new_launch(gate, monkeypatch):
    class Process:
        returncode = None

        def poll(self):
            return self.returncode

    monkeypatch.setattr(activity.subprocess, "Popen", lambda *a, **kw: Process())
    first = activity.ytdlp_popen(["engine", "url"])
    acquired, release, launched = threading.Event(), threading.Event(), threading.Event()

    def update():
        with activity.changing_components():
            acquired.set()
            assert release.wait(3)

    owner = threading.Thread(target=update)
    owner.start()
    assert not acquired.wait(0.15)
    assert first.returncode is None
    second = threading.Thread(
        target=lambda: (activity.ytdlp_popen(["engine", "next"]), launched.set())
    )
    second.start()
    try:
        first.returncode = 0
        assert acquired.wait(2)
        assert not launched.wait(0.15)
    finally:
        release.set()
        owner.join(3)
        second.join(3)
    assert launched.is_set() and not activity._changing


def test_cancelled_activation_releases_gate_without_killing_task(gate):
    process = type("Active", (), {"poll": lambda self: None})()
    activity._processes.add(process)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(InterruptedError), activity.changing_components(cancel):
        pytest.fail("The task is still running")
    assert activity._changing is False
    assert process in activity._processes


@pytest.mark.parametrize("grant", [True, False])
def test_worker_download_precedes_activation_and_rechecks_compatibility(
    tmp_path, monkeypatch, grant
):
    from fluentytdl.core import update_transport

    target = tmp_path / "deno.exe"
    target.write_bytes(b"working-old")
    events = []

    class Transport:
        def __init__(self, *a, **kw):
            self.session = SimpleNamespace(close=lambda: None)

        def download(self, url, payload, sha, progress):
            events.append("download")
            payload.write_bytes(b"candidate")

    config = dict(
        key="deno",
        target_exe=str(target),
        url="https://example.test/deno.exe",
        expected_sha256="a" * 64,
        activation_gate=True,
    )
    monkeypatch.setattr(
        installer.sys,
        "stdin",
        io.StringIO(json.dumps(config) + "\n" + json.dumps({"activate": grant}) + "\n"),
    )
    monkeypatch.setattr(update_transport, "Transport", Transport)
    monkeypatch.setattr(installer, "print_message", lambda kind, **kw: events.append(kind))
    monkeypatch.setattr(installer, "validate_executable", lambda *a: None)
    monkeypatch.setattr(
        installer, "validate_pot_dependency", lambda *a: events.append("compatibility")
    )
    monkeypatch.setattr(installer, "install_group", lambda *a: events.append("commit"))
    assert installer.run_worker() == (0 if grant else 1)
    assert events.index("download") < events.index("activation_request")
    if grant:
        assert (
            events.index("activation_request")
            < events.index("compatibility")
            < events.index("commit")
        )
    else:
        assert "commit" not in events and "compatibility" not in events
    assert target.read_bytes() == b"working-old"


def test_manual_ffmpeg_import_requires_both_files(tmp_path, monkeypatch):
    source = tmp_path / "source/ffmpeg.exe"
    source.parent.mkdir()
    source.write_bytes(b"candidate")
    target = tmp_path / "installed/ffmpeg.exe"
    target.parent.mkdir()
    target.write_bytes(b"working-old")
    config = dict(
        key="ffmpeg", target_exe=str(target), local_source=str(source), extra_exes=["ffprobe.exe"]
    )
    monkeypatch.setattr(installer.sys, "stdin", io.StringIO(json.dumps(config)))
    monkeypatch.setattr(installer, "validate_executable", lambda *a: None)
    assert installer.run_worker() == 1
    assert target.read_bytes() == b"working-old"


def test_diagnostics_do_not_expose_credentials_or_tokens():
    message = 'http://user:password@proxy:8080/?token=secret\nCookie: a=private; b=hidden\nAuthorization: Bearer credential\npoToken: test-token\n{"integrityToken":"token-data"}'
    result = redact_detail(message)
    for secret in (
        "password",
        "secret",
        "private",
        "hidden",
        "credential",
        "test-token",
        "token-data",
    ):
        assert secret not in result
    assert "proxy:8080" in result
    assert "secret-pot" not in redact_detail("[bgutil:http] Generated POT: secret-pot")


def test_downloader_does_not_acquire_gate_before_download(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication

    from fluentytdl.core.dependency_manager import DownloaderWorker

    app = QApplication.instance() or QApplication([])
    worker = DownloaderWorker("deno", "url", tmp_path / "deno.exe")
    launches = []
    monkeypatch.setattr(worker, "_launch", lambda: launches.append(True))
    monkeypatch.setattr(worker, "_guarded_activate", lambda: pytest.fail("Premature activation"))
    worker.start()
    assert launches == [True] and worker.isRunning()
    worker._cancel_worker()
    worker.deleteLater()
    app.processEvents()
