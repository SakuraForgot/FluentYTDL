"""Paired component identities, safe staging, migration and runtime ownership."""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fluentytdl.core import pot_component as component
from fluentytdl.utils import component_paths, js_runtime, paths


@pytest.fixture
def bundle(tmp_path):
    root = tmp_path / "source"
    required = (
        "server/src/main.ts",
        "server/src/utils.ts",
        "server/package.json",
        "server/package-lock.json",
        "server/node_modules/follow-redirects/package.json",
        "server/node_modules/canvas/build/Release/canvas.node",
    )
    for name in required:
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"# test\n" if name.endswith(".py") else b"test")
    (root / "server/node_modules/follow-redirects/package.json").write_text('{"version":"1.16.1"}')
    archive = component.PotBundle(root, {}).plugin_archive
    archive.parent.mkdir(parents=True)
    archive.write_bytes(b"official zip fixture")
    manifest = dict(
        schema=2,
        plugin_archive={"size": archive.stat().st_size, "sha256": component.digest(archive)},
        implementation="brainicism",
        bundle_id="pot-original-v2.0.2-r1",
        upstream_version="2.0.2",
        bundle_revision=1,
        upstream_commit="a" * 40,
        server_version="2.0.2",
        plugin_version="2.0.2",
        platform="windows",
        architecture="x64",
        entrypoint="server/src/main.ts",
        min_app_version="3.7.7-rc.1",
        deno_compatibility={"minimum": "2.9.7", "before": "3.0.0"},
        files={
            name: {"size": (root / name).stat().st_size, "sha256": component.digest(root / name)}
            for name in required
        },
    )
    component.atomic_json(root / "manifest.json", manifest)
    return root, manifest


def test_plugin_archive_is_verified_independently(bundle):
    root, _ = bundle
    installed = component.read_bundle(root, verify=True)
    installed.plugin_archive.write_bytes(b"changed official zip")
    with pytest.raises(component.ComponentError, match="pot_package_corrupt"):
        component.read_bundle(root)


def test_retained_pot_requires_security_repair_before_launch(bundle):
    root, manifest = bundle
    metadata = root / "server/node_modules/follow-redirects/package.json"
    metadata.write_text('{"version":"1.16.0"}')
    with pytest.raises(component.ComponentError, match="pot_package_incompatible"):
        component.PotBundle(root, manifest).command(Path("deno.exe"), 1234)


@pytest.mark.parametrize("damage", ["corrupt", "missing", "unlisted_manifest", "pair", "app"])
def test_bad_candidate_never_changes_active(bundle, tmp_path, damage):
    root, manifest = bundle
    target = tmp_path / "installed"
    target.mkdir()
    pointer = target / "active.json"
    pointer.write_text('{"directory":"old"}')
    if damage == "corrupt":
        (root / "server/src/main.ts").write_text("broken")
    elif damage == "missing":
        (root / "server/node_modules/canvas/build/Release/canvas.node").unlink()
    elif damage == "unlisted_manifest":
        (root / "server/manifest.json").write_text("{}")
    else:
        manifest["plugin_version" if damage == "pair" else "min_app_version"] = "99.0.0"
        component.atomic_json(root / "manifest.json", manifest)
    with pytest.raises(component.ComponentError):
        component.read_bundle(root, verify=True)
    assert pointer.read_text() == '{"directory":"old"}'
    assert not (target / "pending.json").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "folder/../escape",
        "CON.txt",
        "folder/file.",
        "foo//bar",
    ],
)
def test_manifest_paths_cannot_escape_or_alias_names(tmp_path, name):
    with pytest.raises(component.ComponentError, match="pot_package_path"):
        component.contained(tmp_path, name)


def test_manifest_rejects_windows_backslash(tmp_path):
    with pytest.raises(component.ComponentError, match="pot_package_path"):
        component.contained(tmp_path, "foo\\bar")


@pytest.mark.parametrize("source", ["managed", "custom", "path"])
def test_pot_reuses_selected_deno_and_never_chooses_a_private_runtime(
    tmp_path, monkeypatch, source
):
    pot = importlib.import_module("fluentytdl.youtube.pot_manager")
    manager = object.__new__(pot.POTManager)
    runtime = js_runtime.DenoRuntime(tmp_path / "deno.exe", source, "2.9.7")
    monkeypatch.setattr(pot, "resolve_deno", lambda: runtime)
    monkeypatch.setattr(pot, "probe_deno", lambda x, **kw: x)
    bundle = SimpleNamespace(
        manifest={"deno_compatibility": {"minimum": "2.9.7", "before": "3.0.0"}}
    )
    assert manager._choose_runtime(bundle) == runtime
    runtime = js_runtime.DenoRuntime(runtime.path, source, "2.1.0")
    with pytest.raises(component.ComponentError, match="pot_runtime_incompatible"):
        manager._choose_runtime(bundle)


def test_verified_deno_is_shared_but_custom_setting_still_wins(tmp_path):
    selected = js_runtime.DenoRuntime(tmp_path / "deno.exe", "managed", "2.9.7")
    selected.path.touch()
    config = {"js_runtime": "auto"}
    try:
        js_runtime.select_verified_deno(selected, config)
        assert js_runtime.resolve_deno(config) == selected
        config["js_runtime_path"] = str(tmp_path / "custom.exe")
        config["js_runtime"] = "deno"
        assert js_runtime.resolve_deno(config).source == "custom"
    finally:
        js_runtime.clear_deno_selection()


def test_fallback_install_is_the_runtime_used(tmp_path, monkeypatch):
    primary, fallback = tmp_path / "app/bin", tmp_path / "data/components"
    monkeypatch.setattr(component_paths, "component_roots", lambda: (primary, fallback))
    old, new = primary / "yt-dlp/yt-dlp.exe", fallback / "yt-dlp/yt-dlp.exe"
    for p in (old, new):
        p.parent.mkdir(parents=True)
        p.touch()
    assert component_paths.component_target("yt-dlp") == new.parent
    assert paths.locate_runtime_tool("yt-dlp.exe") == new


def test_app_core_uses_existing_managed_component(bundle, monkeypatch):
    root, _ = bundle
    monkeypatch.setattr(
        component_paths,
        "managed_candidates",
        lambda key, name: () if name == "active.json" else (root / "manifest.json",),
    )
    assert component.runtime_path(component.resolve_bundle(verify=True).root) == root


def test_offline_probe_forbids_dependency_downloads(bundle):
    root, _ = bundle
    parsed = component.read_bundle(root)
    argv = parsed.command(Path("deno.exe"), 1234, offline=True)
    assert {
        "--cached-only",
        "--no-remote",
        "--node-modules-dir=manual",
        "--allow-net=127.0.0.1",
    } <= set(argv)


@pytest.mark.parametrize("key", ["deno", "yt-dlp"])
@pytest.mark.parametrize("damage", ["canvas", "plugin", "dependency", "manifest", "missing"])
def test_damaged_pot_does_not_block_base_component_repair(bundle, monkeypatch, key, damage):
    from fluentytdl.core import updater_worker
    from fluentytdl.youtube import pot_probe

    root, manifest = bundle
    if damage == "canvas":
        (root / "server/node_modules/canvas/build/Release/canvas.node").unlink()
    elif damage == "plugin":
        component.PotBundle(root, manifest).plugin_archive.write_bytes(b"corrupt")
    elif damage == "dependency":
        # Non-entrypoint npm files need the full integrity check, too.
        manifest["files"]["server/node_modules/example/index.js"] = {
            "size": 10,
            "sha256": "a" * 64,
        }
        component.atomic_json(root / "manifest.json", manifest)
    elif damage == "manifest":
        component.atomic_json(root / "manifest.json", [])
    else:
        (root / "manifest.json").unlink()
    monkeypatch.setattr(component, "resolve_bundle", lambda **kw: component.read_bundle(root, **kw))

    def unexpected(*args, **kwargs):
        pytest.fail("A damaged POT must not be used to validate an executable candidate")

    monkeypatch.setattr(component, "local_probe", unexpected)
    monkeypatch.setattr(pot_probe, "probe_provider", unexpected)
    monkeypatch.setattr(js_runtime, "probe_deno", unexpected)
    updater_worker.validate_pot_dependency(key, root / f"{key}.exe")
    # Repairing the base component does not waive POT's own readiness checks.
    with pytest.raises(component.ComponentError):
        component.resolve_bundle(verify=True)


@pytest.mark.parametrize("failure", ["deno_version", "deno_start", "yt-dlp_plugin"])
def test_healthy_pot_still_rejects_incompatible_base_component(bundle, monkeypatch, failure):
    from fluentytdl.core import updater_worker
    from fluentytdl.youtube import pot_probe

    root, _ = bundle
    monkeypatch.setattr(component, "resolve_bundle", lambda **kw: component.read_bundle(root, **kw))
    monkeypatch.setattr(
        js_runtime,
        "probe_deno",
        lambda runtime: js_runtime.DenoRuntime(
            runtime.path, "candidate", "2.1.0" if failure == "deno_version" else "2.9.7"
        ),
    )

    def rejected(*args, **kwargs):
        raise component.ComponentError("candidate_rejected")

    monkeypatch.setattr(component, "local_probe", rejected)
    monkeypatch.setattr(pot_probe, "probe_provider", rejected)
    key = "yt-dlp" if failure == "yt-dlp_plugin" else "deno"
    expected = "pot_runtime_incompatible" if failure == "deno_version" else "candidate_rejected"
    with pytest.raises(component.ComponentError, match=expected):
        updater_worker.validate_pot_dependency(key, root / f"{key}.exe")
