"""Official ZIP discovery and custom executable fallback without duplicate plugins."""

import hashlib
import importlib
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluentytdl.core import pot_component

cli = importlib.import_module("fluentytdl.youtube.yt_dlp_cli")


@pytest.fixture
def plugin_install(tmp_path, monkeypatch):
    exe = tmp_path / "中文 路径/bin/yt-dlp/yt-dlp.exe"
    root = exe.parent / "yt-dlp-plugins"
    root.mkdir(parents=True)
    exe.touch()
    archive = root / pot_component.PLUGIN_ASSET
    archive.write_bytes(b"official archive")
    bundle = SimpleNamespace(plugin_root=root, plugin_archive=archive)
    monkeypatch.setattr(cli, "resolve_yt_dlp_exe", lambda: exe)
    monkeypatch.setattr(pot_component, "resolve_bundle", lambda: bundle)
    return exe, archive


def test_managed_executable_uses_default_discovery_without_extra_flags(plugin_install):
    exe, archive = plugin_install
    before = archive.read_bytes()
    command = [str(exe), "--ignore-config", "--verbose"]
    cli.prepare_yt_dlp_env(command=command)
    assert "--plugin-dirs" not in command
    assert "--no-plugin-dirs" not in command
    assert list(archive.parent.iterdir()) == [archive]
    assert archive.read_bytes() == before


def test_custom_executable_uses_the_same_single_archive(plugin_install, tmp_path):
    _, archive = plugin_install
    custom = tmp_path / "custom/yt-dlp.exe"
    custom.parent.mkdir()
    custom.touch()
    command = [str(custom), "--plugin-dirs", str(tmp_path / "user-plugins")]
    cli.prepare_yt_dlp_env(command=command)
    cli.prepare_yt_dlp_env(command=command)
    assert command.count(str(archive.parent)) == 1
    assert command.count(str(tmp_path / "user-plugins")) == 1
    assert not (custom.parent / "yt-dlp-plugins").exists()


def test_missing_required_plugin_stops_request(plugin_install, monkeypatch):
    def missing():
        raise pot_component.ComponentError("pot_package_corrupt")

    monkeypatch.setattr(pot_component, "resolve_bundle", missing)
    exe, _ = plugin_install
    with pytest.raises(RuntimeError):
        cli.prepare_yt_dlp_env(
            command=[
                str(exe),
                "--extractor-args",
                "youtubepot-bgutilhttp:base_url=http://127.0.0.1:4416",
            ]
        )


def test_unknown_legacy_plugin_is_preserved(plugin_install):
    _, archive = plugin_install
    legacy = archive.parent / cli._PLUGIN_PACKAGE_NAME / "yt_dlp_plugins/extractor"
    legacy.mkdir(parents=True)
    unknown = legacy / "getpot_bgutil_http.py"
    unknown.write_text("# custom provider")
    with pytest.raises(pot_component.ComponentError, match="pot_plugin_conflict"):
        cli.legacy_pot_directories(archive.parent)
    assert unknown.read_text() == "# custom provider"


def test_only_verified_hash_directory_is_retired(plugin_install):
    _, archive = plugin_install
    raw = b"# owned plugin"
    identity = hashlib.sha256(raw * 2).hexdigest()[:16]
    directory = archive.parent / f"fluentytdl-pot-{identity}"
    extractor = directory / cli._PLUGIN_PACKAGE_NAME / "yt_dlp_plugins/extractor"
    extractor.mkdir(parents=True)
    for name in pot_component.HTTP_PLUGIN_FILES:
        (extractor / name).write_bytes(raw)
    assert cli.legacy_pot_directories(archive.parent) == [directory]
    (extractor / "user-file.txt").touch()
    with pytest.raises(pot_component.ComponentError):
        cli.legacy_pot_directories(archive.parent)


@pytest.mark.needs_components
@pytest.mark.parametrize("custom", [False, True])
def test_real_yt_dlp_loads_official_zip(plugin_install, tmp_path, custom):
    exe, archive = plugin_install
    real_bin = Path(__file__).resolve().parents[1] / "assets/bin/yt-dlp"
    official = real_bin / "yt-dlp-plugins" / pot_component.PLUGIN_ASSET
    if not official.is_file():
        pytest.skip("Official ZIP not yet installed in development components")
    shutil.copy2(real_bin / "yt-dlp.exe", exe)
    shutil.copy2(official, archive)
    if custom:
        other = tmp_path / "custom/yt-dlp.exe"
        other.parent.mkdir()
        shutil.copy2(exe, other)
        exe = other
    probe_root = tmp_path / "probe-plugins"
    probe = probe_root / "health/yt_dlp_plugins/extractor/probe.py"
    probe.parent.mkdir(parents=True)
    probe.write_text(
        "import sys\n"
        "from yt_dlp_plugins.extractor.getpot_bgutil_http import BgUtilHTTPPTP\n"
        "print('POT_IMPORT_OK:' + BgUtilHTTPPTP.PROVIDER_NAME, file=sys.stderr)\n",
        encoding="utf-8",
    )
    command = [str(exe), "--ignore-config", "--verbose", "--plugin-dirs", str(probe_root)]
    env = cli.prepare_yt_dlp_env(command=command)
    env.pop("PYTHONPATH", None)
    env["PATH"] = ""
    result = subprocess.run(command, capture_output=True, env=env, timeout=30)
    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 2, stderr
    assert "POT_IMPORT_OK:bgutil:http" in stderr, stderr
    assert pot_component.PLUGIN_ASSET in stderr, stderr
    assert "Traceback" not in stderr, stderr


@pytest.mark.needs_components
@pytest.mark.parametrize("http_ok", [True, False])
def test_real_health_requires_http_provider_despite_successful_fallback(plugin_install, http_ok):
    from fluentytdl.utils.paths import get_clean_env
    from fluentytdl.youtube.pot_probe import probe_provider

    exe, archive = plugin_install
    real_bin = Path(__file__).resolve().parents[1] / "assets/bin"
    official = real_bin / "yt-dlp/yt-dlp-plugins" / pot_component.PLUGIN_ASSET
    if not official.is_file():
        pytest.skip("Official ZIP not yet installed in development components")
    version = json.loads((real_bin / "pot-provider/manifest.json").read_text())["upstream_version"]
    shutil.copy2(real_bin / "yt-dlp/yt-dlp.exe", exe)
    shutil.copy2(official, archive)
    fallback = archive.parent / "fallback/yt_dlp_plugins/extractor/fallback.py"
    fallback.parent.mkdir(parents=True)
    fallback.write_text(
        """from yt_dlp_plugins.extractor.getpot_bgutil import BgUtilPTPBase
from yt_dlp.extractor.youtube.pot.provider import register_provider, register_preference, PoTokenResponse
@register_provider
class TestFallbackPTP(BgUtilPTPBase):
    PROVIDER_NAME = 'test:fallback'
    def is_available(self):
        return True
    def _real_request_pot(self, request):
        return PoTokenResponse(po_token='F' * 64)
@register_preference(TestFallbackPTP)
def preference(provider, request):
    return 200
__all__ = ['TestFallbackPTP', 'preference']
""",
        encoding="utf-8",
    )
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply(200, {"version": version})

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply(200 if http_ok else 503, {"poToken": "H" * 124} if http_ok else {})

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = (exe, archive.parent, f"http://127.0.0.1:{server.server_port}", "")
            loaded = []
            kwargs = dict(
                timeout=15,
                env=get_clean_env(),
                default_plugins=False,
                on_plugin=lambda: loaded.append(True),
            )
            if http_ok:
                assert probe_provider(*args, **kwargs) == 124
            else:
                with pytest.raises(RuntimeError, match="pot_token_rejected"):
                    probe_provider(*args, **kwargs)
            assert loaded == [True]
            assert len(requests) == 1
            assert requests[0]["bypass_cache"] is True
            assert requests[0]["content_binding"] == "jNQXAC9IVRw"
        finally:
            server.shutdown()
            thread.join(timeout=2)
