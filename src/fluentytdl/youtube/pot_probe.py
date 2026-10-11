"""Exercise the real yt-dlp POT director without cookies or a video extraction."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from ..core.pot_component import PLUGIN_ASSET
from ..utils.owned_process import attach_job, close_job


def probe_provider(
    exe: Path,
    plugin_root: Path,
    base_url: str,
    proxy: str,
    *,
    timeout: float,
    env: dict,
    on_process=None,
    on_plugin=None,
    token_required: bool = True,
    default_plugins: bool = True,
) -> int:
    nonce = uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="pot-cli-") as directory:
        root = Path(directory)
        extractor = root / "health/yt_dlp_plugins/extractor/pot_health.py"
        extractor.parent.mkdir(parents=True)
        # This extractor is injected only into this one CLI invocation. No tokens,
        # cookies or provider request/response bodies are emitted to stdout.
        extractor.write_text(
            """import json
import os
import sys
from yt_dlp.extractor.common import InfoExtractor

class FluentPotHealthIE(InfoExtractor):
    IE_NAME = 'fluentytdl-pot-health'
    _VALID_URL = r'fluentytdl-pot-health:(?P<id>[a-f0-9]+)'

    def _real_extract(self, url):
        from yt_dlp.extractor.youtube import YoutubeIE
        from yt_dlp.extractor.youtube._base import INNERTUBE_CLIENTS
        from yt_dlp.extractor.youtube.pot._director import initialize_pot_director
        from yt_dlp.extractor.youtube.pot.provider import PoTokenContext, PoTokenRequest
        from yt_dlp_plugins.extractor import getpot_bgutil, getpot_bgutil_http
        from yt_dlp.utils import ExtractorError
        archive = """
            + repr(str(plugin_root / PLUGIN_ASSET))
            + """
        # A sibling package can shadow either module while remaining below the
        # same search root. Both must come from the verified official ZIP.
        for module, member in (
            (getpot_bgutil, 'getpot_bgutil.py'),
            (getpot_bgutil_http, 'getpot_bgutil_http.py'),
        ):
            expected = os.path.normcase(os.path.realpath(os.path.join(
                archive, 'yt_dlp_plugins', 'extractor', member)))
            actual = os.path.normcase(os.path.realpath(getattr(module, '__file__', '') or ''))
            if actual != expected:
                raise ExtractorError('pot_plugin_conflict', expected=True)
        director = initialize_pot_director(YoutubeIE(self._downloader))
        # This temporary probe must exercise our HTTP service, never a fallback.
        director.providers = {
            key: provider for key, provider in director.providers.items()
            if type(provider) is getpot_bgutil_http.BgUtilHTTPPTP
        }
        if not director.providers:
            raise ExtractorError('pot_plugin_incompatible', expected=True)
        self.to_screen('FLUENT_POT_PLUGIN ' + json.dumps({'nonce': self._match_id(url), 'imported': True}))
        sys.stdout.flush()
        if not """
            + repr(token_required)
            + """:
            self.to_screen('FLUENT_POT_HEALTH ' + json.dumps({'nonce': self._match_id(url), 'length': 0, 'imported': True}))
            return self.playlist_result([], 'pot-health')
        token = director.get_po_token(PoTokenRequest(
            context=PoTokenContext.PLAYER,
            innertube_context=INNERTUBE_CLIENTS['web']['INNERTUBE_CONTEXT'],
            internal_client_name='web', video_id='jNQXAC9IVRw', bypass_cache=True,
        ))
        if not token:
            raise ExtractorError('pot_token_rejected', expected=True)
        self.to_screen('FLUENT_POT_HEALTH ' + json.dumps({'nonce': self._match_id(url), 'length': len(token)}))
        return self.playlist_result([], 'pot-health')

__all__ = ['FluentPotHealthIE']
""",
            encoding="utf-8",
        )
        command = [
            str(exe),
            "--ignore-config",
            "--no-plugin-dirs",
            "--plugin-dirs",
            str(plugin_root),
            "--plugin-dirs",
            str(root),
            "--no-color",
            "--skip-download",
            "--no-progress",
            "--proxy",
            proxy,
            "--extractor-args",
            f"youtubepot-bgutilhttp:base_url={base_url}",
            f"fluentytdl-pot-health:{nonce}",
        ]
        if default_plugins:
            command[1:1] = ["--plugin-dirs", "default"]
            # --no-plugin-dirs must precede all explicit directories.
            command.remove("--no-plugin-dirs")
            command.insert(1, "--no-plugin-dirs")
        env = {**env, "PYTHONDONTWRITEBYTECODE": "1"}
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        attach_job(process)
        if on_process:
            on_process(process)

        def report_plugin(output):
            if on_plugin is None:
                return
            for line in (output or b"").decode("utf-8", "replace").splitlines():
                if "FLUENT_POT_PLUGIN " not in line:
                    continue
                try:
                    value = json.loads(line.split("FLUENT_POT_PLUGIN ", 1)[1])
                except ValueError:
                    continue
                if (
                    isinstance(value, dict)
                    and value.get("nonce") == nonce
                    and value.get("imported") is True
                ):
                    on_plugin()
                    return

        try:
            try:
                output, errors = process.communicate(timeout=max(0.1, timeout))
            except subprocess.TimeoutExpired as error:
                report_plugin(error.output)
                raise
            report_plugin(output)
        finally:
            close_job(process)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
            if on_process:
                on_process(None)
        if process.returncode:
            # Never propagate arbitrary CLI output (it may contain credentials).
            code = next(
                (
                    code
                    for marker, code in (
                        (b"pot_plugin_conflict", "pot_plugin_conflict"),
                        (b"pot_token_rejected", "pot_token_rejected"),
                        (b"pot_plugin_incompatible", "pot_plugin_incompatible"),
                        (b"No module named", "pot_plugin_incompatible"),
                        (b"cannot import name", "pot_plugin_incompatible"),
                    )
                    if marker in errors
                ),
                "pot_cli_probe_failed",
            )
            raise RuntimeError(code)
        for line in output.decode("utf-8", "replace").splitlines():
            if "FLUENT_POT_HEALTH " in line:
                value = json.loads(line.split("FLUENT_POT_HEALTH ", 1)[1])
                if (
                    value.get("nonce") == nonce
                    and isinstance(value.get("length"), int)
                    and (value["length"] >= 16 if token_required else value.get("imported") is True)
                ):
                    return value["length"]
        raise RuntimeError("pot_cli_probe_unconfirmed")
