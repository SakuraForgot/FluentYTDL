"""Production pruning must preserve runtime resources and reject dirty packages."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from fluentytdl.core.pot_component import ComponentError, digest
from fluentytdl.core.pot_dependencies import (
    prune_dependencies,
    verify_dependencies,
    write_production_lock,
)


def _write(path, content="runtime"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _package(modules, name, **metadata):
    root = modules / name
    _write(root / "package.json", json.dumps({"name": name, "version": "1.0.0", **metadata}))
    _write(root / "index.js")
    return root


@pytest.fixture
def server(tmp_path):
    root = tmp_path / "server"
    _write(root / "package.json", json.dumps({"dependencies": {"runtime": "1.0.0"}}))
    _package(root / "node_modules", "runtime")
    return root


def test_only_development_roots_are_removed_from_temporary_lock(server, tmp_path):
    package = {
        "dependencies": {"runtime": "1", "shared": "1"},
        "devDependencies": {"compiler": "2", "@types/node": "26", "shared": "1"},
    }
    lock = {
        "version": "5",
        "specifiers": {
            "npm:runtime@1": "1.0.0",
            "npm:shared@1": "1.0.0",
            "npm:compiler@2": "2.0.0",
            "npm:@types/node@26": "26.0.0",
        },
        "npm": {"shared@1.0.0": {"integrity": "original-hash"}},
        "workspace": {"packageJson": {"dependencies": ["npm:compiler@2"]}},
    }
    _write(server / "package.json", json.dumps(package))
    _write(server / "deno.lock", json.dumps(lock))
    original = (server / "deno.lock").read_bytes()
    output = tmp_path / "production.lock"
    write_production_lock(server, output)
    projected = json.loads(output.read_text())
    assert projected["specifiers"] == {"npm:runtime@1": "1.0.0", "npm:shared@1": "1.0.0"}
    assert projected["npm"] == lock["npm"]
    assert projected["workspace"] == lock["workspace"]
    assert (server / "deno.lock").read_bytes() == original
    assert json.loads((server / "package.json").read_text()) == package


def test_shared_optional_peer_and_nested_versions_survive(server):
    modules = server / "node_modules"
    runtime = _package(
        modules,
        "runtime",
        dependencies={"shared": "1", "child": "1"},
        optionalDependencies={"platform": "1", "other-platform": "1"},
        peerDependencies={"peer": "1", "optional-peer": "1"},
        peerDependenciesMeta={"optional-peer": {"optional": True}},
    )
    _package(modules, "compiler", dependencies={"shared": "1", "dev-child": "1"})
    for name in ("shared", "dev-child", "platform", "peer"):
        _package(modules, name)
    _package(modules, "child", dependencies={"shared": "2"})
    nested = _package(modules / "child/node_modules", "shared")
    for name in (
        "data/schema.json",
        "extensions/engine.so",
        "build/engine.node",
        "build/runtime.dll",
        "engine.wasm",
        "src/runtime.ts",
        "lib/history.js",
    ):
        _write(runtime / name)
    before = {p: p.read_bytes() for p in runtime.rglob("*") if p.is_file()}
    report = prune_dependencies(server)
    assert report["removed_packages"] == 2
    assert not (modules / "compiler").exists() and not (modules / "dev-child").exists()
    assert nested.exists() and (modules / "shared").exists() and (modules / "peer").exists()
    assert all(p.read_bytes() == data for p, data in before.items())
    verify_dependencies(server)
    assert prune_dependencies(server)["removed_files"] == 0


def test_pot_security_lock_replaces_only_vulnerable_resolution(server, tmp_path):
    lock = {
        "specifiers": {},
        "npm": {
            "axios@1.20.0": {"integrity": "keep", "dependencies": ["follow-redirects@1.16.0"]},
            "follow-redirects@1.16.0": {"integrity": "old"},
        },
    }
    _write(server / "deno.lock", json.dumps(lock))
    output = tmp_path / "secure.lock"
    write_production_lock(server, output)
    patched = json.loads(output.read_text())
    assert "follow-redirects@1.16.0" not in patched["npm"]
    assert patched["npm"]["follow-redirects@1.16.1"]["integrity"].startswith("sha512-")
    assert patched["npm"]["axios@1.20.0"] == {
        "integrity": "keep",
        "dependencies": ["follow-redirects@1.16.1"],
    }
    assert json.loads((server / "deno.lock").read_text()) == lock


def test_replayed_pot_tree_cannot_restore_unsafe_dependency(server):
    _package(server / "node_modules", "follow-redirects")
    with pytest.raises(ComponentError, match="Unsafe POT"):
        verify_dependencies(server)


def test_metadata_cleanup_preserves_licenses_and_exported_example_resources(server):
    runtime = _package(
        server / "node_modules",
        "runtime",
        exports={".": "./examples/main.js", "./extra": "./lib/tests/entry.js"},
    )
    removed = ("index.js.map", "index.d.ts", "tsconfig.json", "CHANGELOG.md", "tests/test.js")
    kept = (
        "LICENSE",
        "docs/NOTICE.txt",
        "tests/LICENSE.md",
        "examples/main.js",
        "examples/data.json",
        "src/main.ts",
        "lib/tests/entry.js",
        "lib/tests/data.json",
    )
    for name in (*removed, *kept):
        _write(runtime / name)
    _write(server / "node_modules/.bin/unused.cmd")
    _write(server / "node_modules/.deno/.setup-cache.bin")
    with pytest.raises(ComponentError, match="non-runtime files"):
        verify_dependencies(server)
    assert all((runtime / name).exists() for name in removed)  # verification is read-only
    prune_dependencies(server)
    assert all(not (runtime / name).exists() for name in removed)
    assert all((runtime / name).exists() for name in kept)
    assert not (server / "node_modules/.bin").exists()
    assert not (server / "node_modules/.deno").exists()


@pytest.mark.parametrize("field", ["dependencies", "peerDependencies"])
def test_missing_runtime_dependencies_fail_before_deletion(server, field):
    _package(server / "node_modules", "runtime", **{field: {"missing": "1"}})
    extra = _package(server / "node_modules", "compiler")
    with pytest.raises(ComponentError, match="Missing POT dependency"):
        prune_dependencies(server)
    assert extra.exists()


def test_junction_cannot_prune_files_outside_candidate(server, tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows junction safety")
    outside = tmp_path / "outside"
    _write(outside / "private.txt")
    link = server / "node_modules/runtime/linked"
    # mklink only creates a junction; removal below is non-recursive.
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True
    )
    assert result.returncode == 0
    try:
        with pytest.raises(ComponentError, match="pot_package_path"):
            prune_dependencies(server)
        assert (outside / "private.txt").read_text() == "runtime"
    finally:
        link.rmdir()


def test_snapshot_replay_rejects_hash_valid_but_unpruned_tree(server, tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import component_snapshot

    snapshot_root = tmp_path / "components"
    destination = snapshot_root / "bin/pot-provider/server"
    import shutil

    shutil.copytree(server, destination)
    _package(destination / "node_modules", "compiler")
    files = {
        p.relative_to(snapshot_root).as_posix(): {"sha256": digest(p), "size": p.stat().st_size}
        for p in destination.rglob("*")
        if p.is_file()
    }
    snapshot = snapshot_root / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tools": {
                    key: {"files": files if key == "pot-provider" else {}}
                    for key in component_snapshot.REPOSITORIES
                },
            }
        )
    )
    with pytest.raises(ComponentError, match="non-runtime files"):
        component_snapshot.replay(snapshot, tmp_path / "replay")
    assert not (tmp_path / "replay").exists()


@pytest.mark.parametrize("dirty_source", [True, False])
def test_packaging_checks_source_and_copied_destination(
    server, tmp_path, monkeypatch, dirty_source
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import shutil

    import build

    tools = tmp_path / "tools"
    payload = tmp_path / "payload"
    shutil.copytree(server, tools / "pot-provider/server")
    dirty = (tools if dirty_source else payload / "bin") / "pot-provider/server/node_modules"
    _package(dirty, "compiler")
    monkeypatch.setattr(build, "ASSETS_BIN", tools)
    builder = build.Builder.__new__(build.Builder)
    with pytest.raises(ComponentError, match="non-runtime files"):
        builder.bundle_tools(payload)
    if dirty_source:
        assert not payload.exists()


def test_packaging_keeps_runtime_cookie_modules_but_excludes_session_data(
    server, tmp_path, monkeypatch
):
    import shutil

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import build

    tools = tmp_path / "tools"
    payload = tmp_path / "payload"
    runtime_file = "pot-provider/server/node_modules/runtime/lib/web/cookies/constants.js"
    _write(server / "node_modules/runtime/lib/web/cookies/constants.js")
    shutil.copytree(server, tools / "pot-provider/server")
    for name in ("dle_user/session.json", "yt-dlp/cookies/session.json"):
        _write(tools / name, "private")
    monkeypatch.setattr(build, "ASSETS_BIN", tools)
    monkeypatch.setattr(build, "ROOT", tmp_path)
    monkeypatch.setattr(build, "LICENSES_DIR", tmp_path / "licenses")
    builder = build.Builder.__new__(build.Builder)
    monkeypatch.setattr(builder, "write_build_info", lambda _path: None)
    builder.bundle_tools(payload)
    assert (payload / "bin" / runtime_file).read_bytes() == (tools / runtime_file).read_bytes()
    assert not (payload / "bin/dle_user").exists()
    assert not (payload / "bin/yt-dlp/cookies").exists()
