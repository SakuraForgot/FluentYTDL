"""The real CLI must load both bgutil modules from the verified release ZIP."""

import json
import shutil
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from fluentytdl.core.pot_component import PLUGIN_ASSET
from fluentytdl.utils.paths import get_clean_env
from fluentytdl.youtube.pot_probe import probe_provider


@pytest.mark.needs_components
@pytest.mark.parametrize("token_required", [False, True])
@pytest.mark.parametrize("shadow_module", [None, "getpot_bgutil_http.py", "getpot_bgutil.py"])
def test_real_probe_rejects_same_root_module_shadows(tmp_path, shadow_module, token_required):
    real_bin = Path(__file__).resolve().parents[1] / "assets/bin"
    source_exe = real_bin / "yt-dlp/yt-dlp.exe"
    source_archive = real_bin / "yt-dlp/yt-dlp-plugins" / PLUGIN_ASSET
    if not source_exe.is_file() or not source_archive.is_file():
        pytest.skip("The real yt-dlp and official plugin ZIP are required")
    version = json.loads((real_bin / "pot-provider/manifest.json").read_text())["upstream_version"]
    plugin_root = tmp_path / "中文 路径/bin/yt-dlp/yt-dlp-plugins"
    plugin_root.mkdir(parents=True)
    exe = plugin_root.parent / "yt-dlp.exe"
    shutil.copy2(source_exe, exe)

    # Create the sibling before the ZIP so real default discovery chooses it.
    shadow = None
    if shadow_module:
        shadow = plugin_root / "000-shadow/yt_dlp_plugins/extractor" / shadow_module
        shadow.parent.mkdir(parents=True)
        with zipfile.ZipFile(source_archive) as archive:
            shadow.write_bytes(archive.read(f"yt_dlp_plugins/extractor/{shadow_module}"))
        if shadow_module == "getpot_bgutil_http.py":
            # Previously this same-root substitute was accepted as our HTTP
            # provider even though its token never came from the HTTP service.
            shadow.write_text(
                """from yt_dlp_plugins.extractor.getpot_bgutil import BgUtilPTPBase
from yt_dlp.extractor.youtube.pot.provider import register_provider, register_preference, PoTokenResponse
@register_provider
class BgUtilHTTPPTP(BgUtilPTPBase):
    PROVIDER_NAME = 'bgutil:http'
    def is_available(self):
        return True
    def _real_request_pot(self, request):
        return PoTokenResponse(po_token='S' * 64)
@register_preference(BgUtilHTTPPTP)
def preference(provider, request):
    return 130
__all__ = ['BgUtilHTTPPTP', 'preference']
""",
                encoding="utf-8",
            )
    installed_archive = plugin_root / PLUGIN_ASSET
    shutil.copy2(source_archive, installed_archive)
    original_zip = installed_archive.read_bytes()
    original_shadow = shadow.read_bytes() if shadow else None
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.reply({"version": version})

        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.reply({"poToken": "H" * 124})

        def reply(self, value):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    loaded = []
    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            args = (exe, plugin_root, f"http://127.0.0.1:{server.server_port}", "")
            kwargs = dict(
                timeout=15,
                env=get_clean_env(),
                token_required=token_required,
                default_plugins=False,
                on_plugin=lambda: loaded.append(True),
            )
            if shadow_module:
                with pytest.raises(RuntimeError, match="^pot_plugin_conflict$"):
                    probe_provider(*args, **kwargs)
                assert loaded == [] and requests == []
                assert shadow.read_bytes() == original_shadow
            else:
                assert probe_provider(*args, **kwargs) == (124 if token_required else 0)
                assert loaded == [True]
                assert len(requests) == int(token_required)
            assert installed_archive.read_bytes() == original_zip
        finally:
            server.shutdown()
            thread.join(timeout=2)
