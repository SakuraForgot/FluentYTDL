from pathlib import Path
from types import SimpleNamespace

from fluentytdl.utils import ytdlp_runtime as runtime


def test_override_wins_without_search(tmp_path):
    custom = tmp_path / "custom.exe"
    custom.touch()
    result = runtime.resolve_runtime(
        str(custom), locate=lambda *a: (_ for _ in ()).throw(AssertionError())
    )
    assert result == runtime.RuntimeIdentity(custom, "custom")


def test_local_then_frozen_legacy_then_path(tmp_path):
    local = tmp_path / "local.exe"
    legacy = tmp_path / "legacy.exe"
    system = tmp_path / "system.exe"
    for p in (local, legacy, system):
        p.touch()
    assert (
        runtime.resolve_runtime(locate=lambda *a: local, which=lambda n: str(system)).path == local
    )

    def missing(*args):
        raise FileNotFoundError

    assert (
        runtime.resolve_runtime(
            locate=missing, frozen=True, legacy=lambda *a: legacy, which=lambda n: str(system)
        ).path
        == legacy
    )
    assert (
        runtime.resolve_runtime(locate=missing, frozen=False, which=lambda n: str(system)).path
        == system
    )


def test_version_shared_and_replaced_binary_invalidates(tmp_path, monkeypatch):
    executable = tmp_path / "yt-dlp.exe"
    executable.write_bytes(b"first")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert "--ignore-config" in argv
        assert not any(arg.startswith("https:") for arg in argv)
        return SimpleNamespace(
            stdout="", stderr="[debug] yt-dlp version nightly@2026.08.30.232658 from test"
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime.invalidate_version_cache()
    assert runtime.probe_version(executable).channel == "nightly"
    runtime.probe_version(executable)
    assert len(calls) == 1
    executable.write_bytes(b"replacement")
    runtime.probe_version(executable)
    assert len(calls) == 2
    assert runtime.probe_version(Path(tmp_path / "missing")).status == "missing"


def test_frozen_bin_wins_over_development_copy(tmp_path, monkeypatch):
    from fluentytdl.utils import paths

    app = tmp_path / "app"
    dev = tmp_path / "dev"
    binary = app / "bin/yt-dlp/yt-dlp.exe"
    old = dev / "src/fluentytdl/assets/bin/yt-dlp/yt-dlp.exe"
    for path in (binary, old):
        path.parent.mkdir(parents=True)
        path.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "is_frozen", lambda: True)
    monkeypatch.setattr(paths, "frozen_app_dir", lambda: app)
    monkeypatch.setattr(paths, "frozen_internal_dir", lambda: app / "_internal")
    monkeypatch.setattr(paths, "project_root", lambda: dev)
    assert runtime.resolve_runtime(frozen=True, which=lambda name: None).path == binary


def test_execution_status_and_startup_share_custom_identity(tmp_path, monkeypatch):
    from fluentytdl.core.config_manager import config_manager
    from fluentytdl.core.dependency_manager import UpdateCheckerWorker, dependency_manager
    from fluentytdl.utils.startup_info import _quick_detect_version, _resolve_component
    from fluentytdl.youtube.yt_dlp_cli import resolve_yt_dlp_exe, run_version

    custom = tmp_path / "custom.exe"
    custom.touch()
    monkeypatch.setitem(config_manager.config, "yt_dlp_exe_path", str(custom))
    assert resolve_yt_dlp_exe() == custom
    assert dependency_manager.resolve_exe("yt-dlp") == (custom, "custom")
    assert _resolve_component("yt-dlp", tmp_path / "managed", "yt-dlp.exe") == (custom, "custom")
    assert dependency_manager.get_exe_path("yt-dlp") != custom
    calls = []

    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            stdout="", stderr="[debug] yt-dlp version nightly@2026.08.30.232658 from test"
        )

    monkeypatch.setattr(runtime.subprocess, "run", run)
    runtime.invalidate_version_cache()
    assert run_version() == "2026.08.30.232658"
    assert "nightly" in _quick_detect_version("yt-dlp", custom)
    checker = SimpleNamespace()
    assert UpdateCheckerWorker._get_local_version(checker, "yt-dlp", custom) == (
        "2026.08.30.232658",
        "nightly",
    )
    assert checker._local_version_status == "ok"
    assert len(calls) == 1


def test_timeout_does_not_use_sidecar_version(tmp_path, monkeypatch):
    import subprocess

    binary = tmp_path / "yt-dlp.exe"
    binary.touch()
    (tmp_path / "manifest.json").write_text('{"version":"wrong", "channel":"stable"}')

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 10)

    runtime.invalidate_version_cache()
    monkeypatch.setattr(runtime.subprocess, "run", timeout)
    result = runtime.probe_version(binary)
    assert (result.status, result.version, result.channel) == ("timeout", "unknown", "")
