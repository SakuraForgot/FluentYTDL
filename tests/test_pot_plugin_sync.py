"""Fresh extraction and component replacement must deploy POT before parsing."""

import importlib
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

cli = importlib.import_module("fluentytdl.youtube.yt_dlp_cli")
pot = importlib.import_module("fluentytdl.youtube.pot_manager")


@pytest.fixture
def plugin_install(tmp_path, monkeypatch):
    source = (
        tmp_path
        / "_internal"
        / "fluentytdl"
        / "yt_dlp_plugins_ext"
        / "yt_dlp_plugins"
        / "extractor"
    )
    source.mkdir(parents=True)
    for name in ("getpot_bgutil.py", "getpot_bgutil_http.py", "getpot_bgutil_cli.py"):
        (source / name).write_text("# plugin source\n", encoding="utf-8")
    exe = tmp_path / "中文 全新解压" / "bin" / "yt-dlp" / "yt-dlp.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: exe)
    monkeypatch.setattr(cli, "_get_pot_plugin_source_dir", lambda: source)
    target = exe.parent / "yt-dlp-plugins" / cli._PLUGIN_PACKAGE_NAME
    return source, target / "yt_dlp_plugins" / "extractor"


def test_first_parse_deploys_before_environment_preparation(plugin_install, monkeypatch):
    from fluentytdl.core.config_manager import config_manager
    from fluentytdl.youtube.youtube_service import YoutubeService

    source, target = plugin_install
    manager = pot.POTManager.__new__(pot.POTManager)
    monkeypatch.setattr(manager, "wait_until_ready", Mock(return_value=True))
    monkeypatch.setattr(
        manager,
        "get_extractor_args",
        Mock(return_value=("youtubepot-bgutilhttp:base_url=http://127.0.0.1:4417")),
    )
    manager._active_port = 4417
    monkeypatch.setattr(pot, "pot_manager", manager)
    monkeypatch.setitem(config_manager.config, "pot_provider_enabled", True)
    monkeypatch.setattr(YoutubeService(), "_pot_plugin_checked", False, raising=False)
    assert not target.exists()
    for _ in range(2):
        opts = YoutubeService().build_ydl_options(url="https://www.youtube.com/watch?v=test")
        assert "youtubepot-bgutilhttp" in opts["extractor_args"]
        for file in source.iterdir():
            assert (target / file.name).read_bytes() == file.read_bytes()
        shutil.rmtree(target)


@pytest.mark.parametrize("damage", ["delete", "truncate"])
def test_cached_sync_repairs_target(plugin_install, damage):
    source, target = plugin_install
    assert cli.sync_pot_plugins_to_ytdlp()
    file = target / "getpot_bgutil_http.py"
    if damage == "delete":
        file.unlink()
    else:
        file.write_bytes(b"")
    assert cli.sync_pot_plugins_to_ytdlp()
    assert file.read_bytes() == (source / file.name).read_bytes()


def test_write_failure_uses_bundled_fallback(plugin_install, monkeypatch):
    source, target = plugin_install
    monkeypatch.setattr(cli, "_write_pot_file", Mock(side_effect=PermissionError("read only")))
    assert not cli.sync_pot_plugins_to_ytdlp()
    assert not target.exists()
    assert cli.pot_plugin_directory() == source.parents[2]
    manager = pot.POTManager.__new__(pot.POTManager)
    assert manager.verify_plugin_loadable()[0]
    command = [
        str(cli.resolve_yt_dlp_exe()),
        "--ignore-config",
        "https://www.youtube.com/watch?v=test",
    ]
    monkeypatch.setenv("YTDLP_NO_PLUGINS", "1")
    env = cli.prepare_yt_dlp_env(command=command)
    assert command[1:4] == ["--no-plugin-dirs", "--plugin-dirs", str(source.parents[2])]
    assert "YTDLP_NO_PLUGINS" not in env


def test_same_size_same_timestamp_corruption_is_repaired(plugin_install):
    source, target = plugin_install
    assert cli.sync_pot_plugins_to_ytdlp()
    path = target / "getpot_bgutil.py"
    stat = path.stat()
    path.write_bytes(b"!" * stat.st_size)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert cli.sync_pot_plugins_to_ytdlp()
    assert path.read_bytes() == (source / path.name).read_bytes()


def test_atomic_replace_retries_without_truncating_old_file(plugin_install, monkeypatch):
    source, target = plugin_install
    assert cli.sync_pot_plugins_to_ytdlp()
    path = target / "getpot_bgutil.py"
    old = path.read_bytes()
    (source / path.name).write_text("# updated plugin\n")
    replace = cli.os.replace
    calls = []

    def locked_once(src, dst):
        calls.append(dst)
        assert path.read_bytes() == old
        if len(calls) < 3:
            raise PermissionError("sharing violation")
        return replace(src, dst)

    monkeypatch.setattr(cli.os, "replace", locked_once)
    assert cli.sync_pot_plugins_to_ytdlp()
    assert len(calls) == 3
    assert path.read_bytes() == (source / path.name).read_bytes()
    assert not list(target.glob("*.tmp"))


def test_permanent_replace_failure_preserves_previous_file(plugin_install, monkeypatch):
    source, target = plugin_install
    assert cli.sync_pot_plugins_to_ytdlp()
    path = target / "getpot_bgutil.py"
    old = path.read_bytes()
    (source / path.name).write_text("# updated plugin\n")
    monkeypatch.setattr(cli.os, "replace", Mock(side_effect=PermissionError("denied")))
    assert not cli.sync_pot_plugins_to_ytdlp()
    assert path.read_bytes() == old
    assert not list(target.glob("*.tmp"))
    assert cli.pot_plugin_directory() == source.parents[2]


@pytest.mark.parametrize("damage", ["missing", "empty", "syntax"])
def test_invalid_sources_fail_before_touching_destination(plugin_install, damage):
    source, target = plugin_install
    file = source / "getpot_bgutil_http.py"
    if damage == "missing":
        file.unlink()
    else:
        file.write_text("" if damage == "empty" else "def broken(")
    assert not cli.sync_pot_plugins_to_ytdlp()
    assert cli.pot_plugin_directory() is None
    assert not target.exists()
    assert not pot.POTManager.__new__(pot.POTManager).verify_plugin_loadable()[0]


def test_concurrent_sync_is_complete(plugin_install):
    source, target = plugin_install
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(lambda _: cli.sync_pot_plugins_to_ytdlp(), range(24)))
    assert not list(target.glob("*.tmp"))
    for file in source.iterdir():
        assert (target / file.name).read_bytes() == file.read_bytes()


def test_switching_executable_deploys_to_new_directory(plugin_install, tmp_path):
    assert cli.sync_pot_plugins_to_ytdlp()
    other = tmp_path / "custom kernel" / "yt-dlp.exe"
    other.parent.mkdir()
    other.touch()
    assert cli.pot_plugin_directory(other) == other.parent / "yt-dlp-plugins"
    assert (
        other.parent
        / "yt-dlp-plugins"
        / cli._PLUGIN_PACKAGE_NAME
        / "yt_dlp_plugins"
        / "extractor"
        / "getpot_bgutil.py"
    ).is_file()


@pytest.mark.parametrize("fallback", [False, True])
def test_real_executable_imports_bundled_providers(plugin_install, tmp_path, monkeypatch, fallback):
    import subprocess
    from pathlib import Path

    bundled_exe = Path(
        os.environ.get("FLUENTYTDL_TEST_YTDLP")
        or (Path(__file__).resolve().parents[1] / "assets" / "bin" / "yt-dlp" / "yt-dlp.exe")
    )
    if not bundled_exe.is_file():
        if os.environ.get("FLUENTYTDL_REQUIRE_BUILD_TESTS"):
            pytest.fail("POT integration test requires a real yt-dlp executable")
        pytest.skip("Bundled yt-dlp executable is not available")
    source, target = plugin_install
    actual_sources = (
        Path(cli.__file__).resolve().parents[1]
        / "yt_dlp_plugins_ext"
        / "yt_dlp_plugins"
        / "extractor"
    )
    for file in actual_sources.glob("getpot_bgutil*.py"):
        shutil.copy2(file, source / file.name)
    exe = cli.resolve_yt_dlp_exe()
    shutil.copy2(bundled_exe, exe)
    if fallback:
        target.mkdir(parents=True)
        (target / "getpot_bgutil_http.py").write_text("raise RuntimeError('stale adjacent plugin')")
        monkeypatch.setattr(cli, "_write_pot_file", Mock(side_effect=PermissionError("read only")))
    probe_root = tmp_path / "offline probe"
    probe = probe_root / "probe" / "yt_dlp_plugins" / "extractor"
    probe.mkdir(parents=True)
    (probe / "sync_probe.py").write_text(
        "import sys\n"
        "from yt_dlp_plugins.extractor.getpot_bgutil_http import BgUtilHTTPPTP\n"
        "from yt_dlp_plugins.extractor.getpot_bgutil_cli import BgUtilCliPTP\n"
        "print('POT_IMPORT_OK:' + BgUtilHTTPPTP.PROVIDER_NAME + ':' + BgUtilCliPTP.PROVIDER_NAME, file=sys.stderr)\n",
        encoding="utf-8",
    )
    command = [str(exe), "--ignore-config", "--plugin-dirs", str(probe_root), "--verbose"]
    env = cli.prepare_yt_dlp_env(command=command)
    env["PATH"] = ""
    env.pop("PYTHONPATH", None)
    result = subprocess.run(command, env=env, capture_output=True, timeout=30)
    stderr = result.stderr.decode("utf-8", errors="replace")
    assert result.returncode == 2, stderr
    assert "POT_IMPORT_OK:bgutil:http:bgutil:cli" in stderr, stderr
    assert "Error" not in stderr and "Traceback" not in stderr, stderr


def test_cli_rejects_unavailable_required_plugins(plugin_install):
    source, _ = plugin_install
    (source / "getpot_bgutil_http.py").unlink()
    command = [
        str(cli.resolve_yt_dlp_exe()),
        "--extractor-args",
        "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4417",
    ]
    with pytest.raises(RuntimeError):
        cli.prepare_yt_dlp_env(command=command)


def test_frozen_source_fallback(plugin_install, monkeypatch):
    source, _ = plugin_install
    monkeypatch.undo()
    monkeypatch.setattr(
        cli, "__file__", str(source.parents[3] / "missing" / "youtube" / "yt_dlp_cli.py")
    )
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(cli, "frozen_internal_dir", lambda: source.parents[3])
    assert cli._get_pot_plugin_source_dir() == source


def test_frozen_build_gate_checks_all_plugin_sources(plugin_install, monkeypatch):
    from fluentytdl.utils import build_selftest

    source, _ = plugin_install
    monkeypatch.setattr(
        build_selftest, "__file__", str(source.parents[2] / "utils" / "build_selftest.py")
    )
    build_selftest.check_pot_sources()
    (source / "getpot_bgutil_cli.py").unlink()
    with pytest.raises(FileNotFoundError):
        build_selftest.check_pot_sources()
