"""The installer must consume upstream releases without an app-owned repack."""

import json
import shutil
import zipfile
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from fluentytdl.core import pot_upstream as upstream
from fluentytdl.core.pot_component import ComponentError, digest
from fluentytdl.core.update_transport import CheckSession
from fluentytdl.utils import js_runtime


@pytest.fixture
def release():
    return {
        "tag_name": "2.0.2",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": upstream.PLUGIN_ASSET,
                "size": 8106,
                "digest": "sha256:" + "a" * 64,
                "browser_download_url": f"https://github.com/{upstream.UPSTREAM_REPOSITORY}/releases/download/2.0.2/{upstream.PLUGIN_ASSET}",
            }
        ],
    }


def test_check_uses_upstream_latest_without_app_release_lookup(release):
    transport = Mock()
    transport.get_json.return_value = release
    result = CheckSession("github", transport).pot_release()
    assert result["upstream_version"] == "2.0.2" and result["asset_url"].endswith(
        upstream.PLUGIN_ASSET
    )
    assert transport.get_json.call_count == 1
    assert transport.get_json.call_args.args == (f"{upstream.API}/releases/latest",)


def test_obsolete_bundle_mirror_falls_back_to_upstream(release):
    transport = Mock()
    transport.get_json.side_effect = [
        {
            "schema": 1,
            "implementation": "brainicism",
            "generated_at": datetime.now(UTC).isoformat(),
            "installable_latest": None,
        },
        release,
    ]
    check = CheckSession("cloudflare", transport)
    assert check.pot_release()["upstream_version"] == "2.0.2"
    assert check.source == "github" and transport.get_json.call_count == 2


@pytest.mark.parametrize(
    "change", [{"draft": True}, {"prerelease": True}, {"tag_name": "../../bad"}, {"assets": []}]
)
def test_unusable_releases_are_rejected(release, change):
    with pytest.raises(ComponentError, match="pot_metadata_invalid"):
        upstream.release_metadata({**release, **change})


def test_asset_must_be_upstream_and_have_a_digest(release):
    for change in (
        {"digest": ""},
        {
            "browser_download_url": "https://github.com/SakuraForgot/FluentYTDL/releases/download/custom/package.zip"
        },
    ):
        changed = {**release, "assets": [{**release["assets"][0], **change}]}
        with pytest.raises(ComponentError, match="pot_metadata_invalid"):
            upstream.release_metadata(changed)


@pytest.mark.parametrize("version", ["2.0.0", "2.9.6", "2.9.7"])
def test_installer_requires_the_shared_runtime_to_be_compatible(monkeypatch, tmp_path, version):
    runtime = js_runtime.DenoRuntime(tmp_path / "deno.exe", "managed", version)
    monkeypatch.setattr(js_runtime, "resolve_deno", lambda: runtime)
    monkeypatch.setattr(js_runtime, "probe_deno", lambda _: runtime)
    if version == "2.9.7":
        assert upstream.installation_runtime() == runtime
    else:
        with pytest.raises(ComponentError, match="pot_runtime_incompatible"):
            upstream.installation_runtime()


def test_installer_does_not_override_an_incompatible_custom_runtime(monkeypatch, tmp_path):
    runtime = js_runtime.DenoRuntime(tmp_path / "custom.exe", "custom", "2.0.0")
    monkeypatch.setattr(js_runtime, "resolve_deno", lambda: runtime)
    monkeypatch.setattr(js_runtime, "probe_deno", lambda _: runtime)
    with pytest.raises(ComponentError, match="pot_runtime_incompatible"):
        upstream.installation_runtime()


@pytest.fixture
def sources(tmp_path, release, monkeypatch):
    runtime = js_runtime.DenoRuntime(tmp_path / "shared/deno.exe", "managed", "2.9.7")
    monkeypatch.setattr(upstream, "installation_runtime", lambda _: runtime)
    plugin = b"__version__ = '2.0.2'\n"
    files = {
        "LICENSE": b"license",
        "server/package.json": b'{"version":"2.0.2","dependencies":{"canvas":"3.2.3"}}',
        "server/package-lock.json": b"{}",
        "server/deno.lock": b"{}",
        "server/deno.json": b"{}",
        "server/src/main.ts": b"export {};",
        "server/src/utils.ts": b"export {};",
        **{
            f"plugin/yt_dlp_plugins/extractor/{name}": plugin
            for name in (*upstream.HTTP_PLUGIN_FILES, "getpot_bgutil_script.py")
        },
    }
    source_zip, plugin_zip = tmp_path / "source.zip", tmp_path / "plugin.zip"
    with zipfile.ZipFile(source_zip, "w") as archive:
        for name, raw in files.items():
            archive.writestr("upstream/" + name, raw)
    with zipfile.ZipFile(plugin_zip, "w") as archive:
        for name in (*upstream.HTTP_PLUGIN_FILES, "getpot_bgutil_script.py"):
            archive.writestr("yt_dlp_plugins/extractor/" + name, plugin)
    release["assets"][0].update(
        size=plugin_zip.stat().st_size, digest="sha256:" + digest(plugin_zip)
    )
    transport = Mock()

    def download(url, destination, *_args, **_kwargs):
        shutil.copyfile(
            plugin_zip if url.endswith(upstream.PLUGIN_ASSET) else source_zip, destination
        )

    transport.download.side_effect = download
    return upstream.release_metadata(release), transport, source_zip


def test_source_paths_cannot_escape_the_candidate(sources, tmp_path):
    _meta, _transport, source = sources
    with zipfile.ZipFile(source, "a") as archive:
        archive.writestr("upstream/server/src/../../../escaped", b"bad")
    with pytest.raises(ComponentError, match="pot_package_path"):
        upstream._extract_source(source, tmp_path / "extracted")


def test_incomplete_source_cannot_prepare_a_partial_install(tmp_path):
    source = tmp_path / "partial.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("upstream/LICENSE", b"license")
    with pytest.raises(ComponentError, match="pot_package_incomplete"):
        upstream._extract_source(source, tmp_path / "extracted")


def test_dependency_failure_preserves_active_install_and_cleans_candidate(
    sources, tmp_path, monkeypatch
):
    metadata, transport, *_ = sources
    target = tmp_path / "installed"
    target.mkdir()
    active = target / "active.json"
    active.write_text('{"directory":"previous"}')
    monkeypatch.setattr(
        upstream,
        "_install_dependencies",
        Mock(side_effect=ComponentError("pot_dependency_install_failed")),
    )
    with pytest.raises(ComponentError, match="pot_dependency_install_failed"):
        upstream.install_release(metadata, target, transport)
    assert active.read_text() == '{"directory":"previous"}'
    assert list(target.iterdir()) == [active]


@pytest.mark.parametrize("packaged", [False, True])
def test_success_installs_fixed_paths_and_checks_final_location(
    sources, tmp_path, monkeypatch, packaged
):
    metadata, transport, *_ = sources
    target = tmp_path / "installed"

    runtime = upstream.installation_runtime(None)

    def dependencies(bundle, deno, _transport, _status, _temporary):
        assert deno == runtime.path
        canvas = bundle / "server/node_modules/canvas/build/Release/canvas.node"
        canvas.parent.mkdir(parents=True)
        canvas.write_bytes(b"canvas")
        (canvas.parents[2] / "package.json").write_text(
            json.dumps({"name": "canvas", "version": "3.2.3"})
        )
        (canvas.parents[2] / "README.js.map").write_text("unused")

    monkeypatch.setattr(upstream, "_install_dependencies", dependencies)
    probed = []
    monkeypatch.setattr(
        upstream,
        "_accept",
        lambda bundle, runtime, *_a, **_kw: probed.append((bundle.root, runtime.path)),
    )
    bundle = upstream.install_release(metadata, target, transport, packaged=packaged)
    assert upstream.runtime_path(bundle.root) == target
    assert not (target / "pending.json").exists()
    assert not (target / "active.json").exists()
    assert not (target / "versions").exists()
    assert digest(bundle.plugin_archive) == metadata["sha256"]
    assert bundle.manifest["source_provenance"]["plugin_sha256"] == metadata["sha256"]
    assert bundle.manifest["source_provenance"]["tag"] == "2.0.2"
    transport.get_json.assert_not_called()
    assert transport.download.call_args.args[0].endswith("/archive/refs/tags/2.0.2.zip")
    assert len(probed) == 2
    assert probed[0][0] != bundle.root and probed[1] == (bundle.root, runtime.path)
    assert not list(bundle.root.rglob("deno.exe"))
    assert "bundled_deno" not in bundle.manifest
    assert not list(target.rglob("*.map"))
    assert not any(name.endswith(".map") for name in bundle.manifest["files"])
    assert not any(p.name.startswith(".upstream-") for p in target.iterdir())


def test_imported_zip_is_checked_before_executing_dependencies(sources, tmp_path, monkeypatch):
    metadata, transport, *_ = sources
    changed = tmp_path / "changed.zip"
    changed.write_bytes(b"invalid")
    dependencies = Mock()
    monkeypatch.setattr(upstream, "_install_dependencies", dependencies)
    with pytest.raises(ComponentError, match="worker_hash_mismatch"):
        upstream.install_release(
            metadata, tmp_path / "installed", transport, plugin_archive=changed
        )
    dependencies.assert_not_called()
    transport.get_json.assert_not_called()


def test_cleanup_failure_preserves_active_component(sources, tmp_path, monkeypatch):
    metadata, transport, *_ = sources
    target = tmp_path / "installed"
    (target / "server").mkdir(parents=True)
    original = target / "server/previous.txt"
    original.write_text("working component")

    def dependencies(bundle, *_args):
        (bundle / "server/node_modules").mkdir()

    monkeypatch.setattr(upstream, "_install_dependencies", dependencies)
    accept = Mock()
    monkeypatch.setattr(upstream, "_accept", accept)
    with pytest.raises(ComponentError, match="Missing POT dependency"):
        upstream.install_release(metadata, target, transport)
    assert original.read_text() == "working component"
    assert list(target.iterdir()) == [target / "server"]
    accept.assert_not_called()
