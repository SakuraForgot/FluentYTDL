"""Regression tests for failed updates leaving components or controls unusable."""

import importlib
import json
import struct
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, QProcess, Signal
from PySide6.QtWidgets import QApplication

from fluentytdl.core import updater_worker as installer
from fluentytdl.core.pot_component import ComponentError, atomic_json

dep = importlib.import_module("fluentytdl.core.dependency_manager")


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_two_file_replacement_rolls_back_first_on_second_failure(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    target.mkdir()
    files = []
    for name in ("ffmpeg.exe", "ffprobe.exe"):
        source = tmp_path / name
        source.write_bytes(b"new")
        (target / name).write_bytes(b"old-" + name.encode())
        files.append((source, target / name))
    replace = installer.os.replace

    def fail_second(source, destination):
        if Path(destination).name == "ffprobe.exe":
            raise PermissionError("sharing violation")
        return replace(source, destination)

    monkeypatch.setattr(installer.os, "replace", fail_second)
    with pytest.raises(ComponentError, match="worker_commit_failed"):
        installer.install_group(files)
    assert [(target / n).read_bytes() for n in ("ffmpeg.exe", "ffprobe.exe")] == [
        b"old-ffmpeg.exe",
        b"old-ffprobe.exe",
    ]
    assert not (target / ".install-journal.json").exists()


def test_transient_windows_lock_retries_complete_replacement(tmp_path, monkeypatch):
    source = tmp_path / "candidate"
    source.write_bytes(b"new")
    target = tmp_path / "installed/tool.exe"
    replace = installer.os.replace
    attempts = []

    def temporarily_locked(prepared, destination):
        if Path(destination).name == "tool.exe":
            attempts.append(True)
            if len(attempts) < 3:
                raise PermissionError("sharing violation")
        return replace(prepared, destination)

    monkeypatch.setattr(installer.os, "replace", temporarily_locked)
    monkeypatch.setattr(installer.time, "sleep", lambda _: None)
    installer.install_group([(source, target)])
    assert len(attempts) == 3 and target.read_bytes() == b"new"


def test_backup_cleanup_failure_does_not_reverse_success(tmp_path, monkeypatch):
    source = tmp_path / "candidate"
    source.write_bytes(b"new")
    target = tmp_path / "installed/tool.exe"
    remove = installer._remove_entry

    def fail_cleanup(path, root):
        if path.name.startswith(".install-"):
            raise OSError("backup temporarily busy")
        remove(path, root)

    monkeypatch.setattr(installer, "_remove_entry", fail_cleanup)
    installer.install_group([(source, target)])
    assert target.read_bytes() == b"new"
    journal = target.parent / ".install-journal.json"
    assert json.loads(journal.read_text())["committed"]
    monkeypatch.setattr(installer, "_remove_entry", remove)
    installer.recover_install(target.parent)
    assert target.read_bytes() == b"new" and not journal.exists()


def test_restart_recovers_interrupted_group(tmp_path):
    transaction = tmp_path / ".install-123"
    transaction.mkdir()
    (tmp_path / "deno.exe").write_bytes(b"partial-new")
    (transaction / "deno.exe.previous").write_bytes(b"working-old")
    atomic_json(
        tmp_path / ".install-journal.json",
        {
            "directory": transaction.name,
            "committed": False,
            "entries": [
                {
                    "target": "deno.exe",
                    "prepared": "deno.exe",
                    "backup": "deno.exe.previous",
                    "existed": True,
                }
            ],
        },
    )
    installer.recover_install(tmp_path)
    assert (tmp_path / "deno.exe").read_bytes() == b"working-old"
    assert not transaction.exists()


@pytest.mark.parametrize("installed", [False, True])
def test_final_token_failure_restores_server_and_plugin_together(tmp_path, installed):
    root = tmp_path / "bin"
    server = root / "pot-provider/server"
    plugin = root / "yt-dlp/yt-dlp-plugins/provider.zip"
    source = tmp_path / "candidate/server"
    source.mkdir(parents=True)
    (source / "main.ts").write_bytes(b"new-server")
    archive = source.parent / "provider.zip"
    archive.write_bytes(b"new-plugin")
    if installed:
        server.mkdir(parents=True)
        (server / "main.ts").write_bytes(b"old-server")
        plugin.parent.mkdir(parents=True)
        plugin.write_bytes(b"old-plugin")

    def fail():
        assert (server / "main.ts").read_bytes() == b"new-server"
        assert plugin.read_bytes() == b"new-plugin"
        raise RuntimeError("pot_token_rejected")

    with pytest.raises(ComponentError, match="pot_token_rejected"):
        installer.install_group([(source, server), (archive, plugin)], root=root, validate=fail)
    if installed:
        assert (server / "main.ts").read_bytes() == b"old-server"
        assert plugin.read_bytes() == b"old-plugin"
    else:
        assert not server.exists() and not plugin.exists()
    assert not list(root.glob(".install-*"))


def test_zip_missing_ffprobe_keeps_old_group(tmp_path, monkeypatch):
    target = tmp_path / "installed"
    target.mkdir()
    (target / "ffmpeg.exe").write_bytes(b"working")
    archive = tmp_path / "incomplete.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("bin/ffmpeg.exe", b"new")
    monkeypatch.setattr(installer, "validate_executable", lambda p: None)
    with pytest.raises(FileNotFoundError, match="ffprobe"):
        installer.handle_zip(archive, target / "ffmpeg.exe", target, ["ffprobe.exe"])
    assert (target / "ffmpeg.exe").read_bytes() == b"working"


def test_html_cannot_replace_executable(tmp_path):
    source, target = tmp_path / "download.exe", tmp_path / "deno.exe"
    source.write_bytes(b"<html>rate limit</html>")
    target.write_bytes(b"working")
    with pytest.raises(ValueError, match="Invalid Windows executable"):
        installer.safe_install(source, target)
    assert target.read_bytes() == b"working"


def test_probe_has_timeout_and_checks_exit(tmp_path, monkeypatch):
    source = tmp_path / "ffprobe.exe"
    pe = bytearray(72)
    pe[:2] = b"MZ"
    struct.pack_into("<I", pe, 60, 64)
    pe[64:68] = b"PE\0\0"
    source.write_bytes(pe)

    def run(argv, **kw):
        assert argv[-1] == "-version" and kw["timeout"] == 8
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(installer.subprocess, "run", run)
    with pytest.raises(ValueError, match="probe failed"):
        installer.validate_executable(source)


def test_failed_url_recheck_can_be_retried(app, monkeypatch):
    manager = dep.DependencyManager()
    calls = []
    monkeypatch.setattr(manager, "check_update", lambda key: calls.append(key))
    manager.install_component("deno")
    assert manager._url_recheck_pending == {"deno"}
    manager._on_check_error("deno", "update_network")
    assert not manager._url_recheck_pending
    manager.install_component("deno")
    assert calls == ["deno", "deno"]


@pytest.mark.parametrize("local_source", ["", "upstream-plugin.zip"])
def test_missing_pot_runtime_uses_existing_deno_updater_once(
    app, tmp_path, monkeypatch, local_source
):
    from fluentytdl.utils import js_runtime

    manager = dep.DependencyManager()
    monkeypatch.setattr(manager, "get_exe_path", lambda key: tmp_path / key / "tool.exe")
    monkeypatch.setattr(manager, "refresh_local", lambda key: None)
    monkeypatch.setattr(js_runtime, "resolve_deno", lambda: js_runtime.DenoRuntime(None, "missing"))
    launched = []
    monkeypatch.setattr(dep.DownloaderWorker, "start", lambda w: launched.append(w))
    for key in ("deno", "pot-provider"):
        manager.components[key].download_url = "https://example.test/official.zip"
    if local_source:
        manager.import_component("pot-provider", local_source)
    else:
        manager.install_component("pot-provider")
    manager._on_install_error("pot-provider", "pot_runtime_missing")
    assert [w.key for w in launched] == ["pot-provider", "deno"]
    assert manager.is_installing("pot-provider")
    manager.install_component("pot-provider")
    assert len(launched) == 2
    manager._on_install_finished("deno")
    assert [w.key for w in launched] == ["pot-provider", "deno", "pot-provider"]
    assert launched[-1].local_source == local_source
    errors = []
    manager.download_error.connect(lambda key, code: errors.append((key, code)))
    manager._on_install_error("pot-provider", "pot_runtime_incompatible")
    assert not manager.is_installing("pot-provider")
    assert len(launched) == 3
    assert errors == [("pot-provider", "pot_runtime_incompatible")]


@pytest.mark.parametrize("during_check", [False, True])
def test_deno_dependency_failure_releases_pot_controls(app, monkeypatch, during_check):
    manager = dep.DependencyManager()
    manager._pot_after_deno = ""
    errors = []
    manager.download_error.connect(lambda key, code: errors.append((key, code)))
    if during_check:
        manager._on_check_error("deno", "update_network")
    else:
        manager._on_install_error("deno", "worker_hash_mismatch")
    assert not manager.is_installing("pot-provider")
    assert errors[-1][0] == "pot-provider"


def test_bad_custom_deno_does_not_install_a_second_runtime(app, monkeypatch):
    from fluentytdl.utils import js_runtime

    manager = dep.DependencyManager()
    worker = QObject()
    manager._workers["install_pot-provider"] = worker
    monkeypatch.setattr(
        js_runtime, "resolve_deno", lambda: js_runtime.DenoRuntime(Path("custom.exe"), "custom")
    )
    requests = []
    monkeypatch.setattr(manager, "install_component", lambda key: requests.append(key))
    manager._on_install_error("pot-provider", "pot_runtime_invalid")
    assert not requests
    assert not manager.is_installing("pot-provider")


def test_network_failure_keeps_independent_local_identity(app, tmp_path, monkeypatch):
    manager = dep.DependencyManager()
    monkeypatch.setattr(manager, "resolve_exe", lambda key: (tmp_path / "deno.exe", "managed"))
    monkeypatch.setattr(manager, "get_exe_path", lambda key: tmp_path / "deno.exe")
    worker = dep.UpdateCheckerWorker("deno", manager)
    monkeypatch.setattr(worker, "_get_local_version", lambda *a: ("2.9.7", ""))

    def offline(key):
        raise RuntimeError("update_network")

    monkeypatch.setattr(worker, "_get_remote_version", offline)
    worker.local_signal.connect(manager._on_local_result)
    worker.error_signal.connect(manager._on_check_error)
    worker.run()
    assert manager.components["deno"].local_result["current"] == "2.9.7"
    assert manager.components["deno"].last_error["code"] == "update_network"


def test_install_invalidates_inflight_local_version(app):
    manager = dep.DependencyManager()
    manager.components["deno"].local_epoch = 2
    manager._on_local_result("deno", {"local_epoch": 1, "current": "old"})
    assert not manager.components["deno"].local_result
    manager._on_local_result("deno", {"local_epoch": 2, "current": "2.9.7"})
    assert manager.components["deno"].local_result["current"] == "2.9.7"


def test_start_failure_is_not_overwritten_by_started(app, tmp_path, monkeypatch):
    class FailedWorker(QObject):
        progress_signal = Signal(str, int)
        status_signal = Signal(str, str)
        finished_signal = Signal(str)
        error_signal = Signal(str, str)

        def __init__(self, *a, **kw):
            super().__init__()

        def start(self):
            self.error_signal.emit("deno", "component_worker_start_failed")

        def isRunning(self):
            return False

    monkeypatch.setattr(dep, "DownloaderWorker", FailedWorker)
    manager = dep.DependencyManager()
    manager.components["deno"].download_url = "https://example.test/component.zip"
    manager.components["deno"].expected_sha256 = "a" * 64
    monkeypatch.setattr(manager, "get_exe_path", lambda key: tmp_path / "deno.exe")
    events = []
    manager.download_started.connect(lambda k: events.append("started"))
    manager.download_error.connect(lambda k, c: events.append(c))
    manager.install_component("deno")
    assert events == ["started", "component_worker_start_failed"]
    assert not manager.is_installing("deno")


@pytest.mark.parametrize(
    "done,exit_code,expected",
    [
        (False, 0, "component_worker_incomplete"),
        (True, 1, "component_worker_exit_nonzero"),
        (True, 0, "success"),
    ],
)
def test_worker_requires_explicit_done_and_successful_exit(
    app, tmp_path, done, exit_code, expected
):
    worker = dep.DownloaderWorker("ffmpeg", "url", tmp_path / "ffmpeg.exe")
    worker.process = SimpleNamespace(
        readAllStandardOutput=lambda: SimpleNamespace(data=lambda: b""),
        readAllStandardError=lambda: SimpleNamespace(data=lambda: b""),
    )
    worker._done_received = done
    events = []
    worker.finished_signal.connect(lambda k: events.append("success"))
    worker.error_signal.connect(lambda k, c: events.append(c))
    worker._on_finished(exit_code, QProcess.ExitStatus.NormalExit)
    assert events == [expected]
    assert not worker.isRunning()


def test_local_version_probe_cannot_hang(app, tmp_path, monkeypatch):
    exe = tmp_path / "deno.exe"
    exe.touch()

    def timeout(argv, **kw):
        assert kw["timeout"] == 5
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(dep.subprocess, "run", timeout)
    worker = dep.UpdateCheckerWorker("deno", None)
    assert worker._get_local_version("deno", exe) == ("unknown", "")


def test_notifications_distinguish_bundle_revision_and_channel(monkeypatch):
    from fluentytdl.notification import update_notifier as notify

    monkeypatch.setattr(notify, "_notified", set())
    messages = []
    monkeypatch.setattr(notify.notification_center, "push", messages.append)
    base = {
        "update_available": True,
        "latest": "2.0.2",
        "current": "unknown",
        "url": "https://example.test/bundle",
        "latest_channel": "brainicism",
    }
    for revision in (1, 1, 2):
        notify.notify_component_update(
            "pot-provider", {**base, "bundle": {"bundle_id": f"pot-original-v2.0.2-r{revision}"}}
        )
    assert len(messages) == 2


@pytest.mark.parametrize("remote_revision,available", [(1, False), (2, False), (3, True)])
def test_pot_check_does_not_offer_stale_bundle_as_upgrade(
    app, tmp_path, monkeypatch, remote_revision, available
):
    from fluentytdl.core import pot_component

    current = {"upstream_version": "2.0.2", "bundle_revision": 2}
    remote = {
        "upstream_version": "2.0.2",
        "bundle_revision": remote_revision,
        "bundle_id": f"pot-original-v2.0.2-r{remote_revision}",
    }
    monkeypatch.setattr(
        pot_component, "resolve_bundle", lambda **kw: SimpleNamespace(manifest=current)
    )
    worker = dep.UpdateCheckerWorker(
        "pot-provider",
        SimpleNamespace(
            resolve_exe=lambda key: (tmp_path, "managed"),
            get_exe_path=lambda key: tmp_path,
        ),
    )
    monkeypatch.setattr(worker, "_get_local_version", lambda *a: ("2.0.2", "brainicism"))
    monkeypatch.setattr(
        worker,
        "_get_remote_version",
        lambda *a: dep.RemoteVersion(version="2.0.2", bundle=remote, upstream="2.0.2"),
    )
    results = []
    worker.finished_signal.connect(lambda key, result: results.append(result))
    worker.run()
    assert results and results[0]["update_available"] is available


@pytest.mark.parametrize("outcome", ["valid", "word_only", "wrong_nonce", "exit_failure"])
def test_cli_health_requires_nonce_result_not_log_keywords(tmp_path, monkeypatch, outcome):
    from fluentytdl.youtube import pot_probe

    class Process:
        returncode = 1 if outcome == "exit_failure" else 0

        def __init__(self, argv, **kw):
            assert "--cookies" not in argv and "--cookies-from-browser" not in argv
            self.nonce = argv[-1].split(":")[1]

        def communicate(self, **kw):
            value = {"nonce": self.nonce if outcome != "wrong_nonce" else "wrong", "length": 124}
            line = (
                b"bgutil loaded token ready"
                if outcome == "word_only"
                else ("FLUENT_POT_HEALTH " + json.dumps(value)).encode()
            )
            return line, b""

        def poll(self):
            return self.returncode

    monkeypatch.setattr(pot_probe.subprocess, "Popen", Process)
    args = (tmp_path / "yt-dlp.exe", tmp_path, "http://127.0.0.1:1", "")
    if outcome == "valid":
        assert pot_probe.probe_provider(*args, timeout=1, env={}) == 124
    else:
        with pytest.raises(RuntimeError):
            pot_probe.probe_provider(*args, timeout=1, env={})


@pytest.mark.parametrize("timeout", [False, True])
@pytest.mark.parametrize("matching_nonce", [False, True])
def test_cli_plugin_evidence_survives_token_failure_only_with_matching_nonce(
    tmp_path, monkeypatch, timeout, matching_nonce
):
    from fluentytdl.youtube import pot_probe

    class Process:
        returncode = 1

        def __init__(self, argv, **kw):
            self.nonce = argv[-1].split(":")[1]

        def communicate(self, **kw):
            value = {"nonce": self.nonce if matching_nonce else "another-probe", "imported": True}
            output = ("FLUENT_POT_PLUGIN " + json.dumps(value)).encode()
            if timeout:
                raise pot_probe.subprocess.TimeoutExpired("yt-dlp", 1, output=output)
            return output, b"pot_token_rejected"

        def poll(self):
            return self.returncode

    monkeypatch.setattr(pot_probe.subprocess, "Popen", Process)
    loaded = []
    error = pot_probe.subprocess.TimeoutExpired if timeout else RuntimeError
    with pytest.raises(error):
        pot_probe.probe_provider(
            tmp_path / "yt-dlp.exe",
            tmp_path,
            "http://127.0.0.1:1",
            "",
            timeout=1,
            env={},
            on_plugin=lambda: loaded.append(True),
        )
    assert loaded == ([True] if matching_nonce else [])
