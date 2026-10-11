"""Component checks must produce usable verified assets and read actual installs."""

import hashlib
import importlib
from unittest.mock import Mock

import pytest

from fluentytdl.core.update_transport import Transport, UpdateError
from fluentytdl.utils import component_paths

dep = importlib.import_module("fluentytdl.core.dependency_manager")
updates = importlib.import_module("fluentytdl.core.component_update_manager")
ATOMIC_URL = (
    "https://github.com/wez/atomicparsley/releases/download/"
    "20240608.083822.1ed9031/AtomicParsleyWindows.zip"
)


@pytest.fixture
def atomic_check(monkeypatch):
    monkeypatch.setattr(updates.component_update_manager, "get_manifest_component", lambda _: None)
    check = Mock()
    check.release.return_value = {
        "tag_name": "20240608.083822.1ed9031",
        "assets": [
            {"name": "AtomicParsleyWindows.zip", "browser_download_url": ATOMIC_URL, "digest": None}
        ],
    }
    entry = {
        "version": "20240608.083822.0",
        "url": ATOMIC_URL,
        "sha256": "a" * 64,
    }
    check.manifest.return_value = {"components": {"bin/atomicparsley": entry}}
    return dep.UpdateCheckerWorker("atomicparsley", None, check), check, entry


@pytest.mark.parametrize("digest", [None, "", "None", "sha256:invalid", "sha512:" + "a" * 64, 123])
def test_missing_or_invalid_upstream_digest_uses_exact_manifest_asset(atomic_check, digest):
    worker, check, _ = atomic_check
    check.release.return_value["assets"][0]["digest"] = digest
    result = worker._get_remote_version("atomicparsley")
    assert result == dep.RemoteVersion("20240608.083822", url=ATOMIC_URL, sha256="a" * 64)
    check.manifest.assert_called_once_with("stable")


def test_valid_upstream_digest_does_not_add_a_manifest_request(atomic_check):
    worker, check, _ = atomic_check
    check.release.return_value["assets"][0]["digest"] = "sha256:" + "B" * 64
    assert worker._get_remote_version("atomicparsley").sha256 == "b" * 64
    check.manifest.assert_not_called()


def test_current_cached_manifest_supplies_hash_without_network_fetch(atomic_check, monkeypatch):
    worker, check, entry = atomic_check
    monkeypatch.setattr(updates.component_update_manager, "get_manifest_component", lambda _: entry)
    assert worker._get_remote_version("atomicparsley").sha256 == "a" * 64
    check.manifest.assert_not_called()


def test_missing_hash_fetch_uses_selected_app_channel(atomic_check, monkeypatch):
    worker, check, _ = atomic_check
    monkeypatch.setattr(
        dep.config_manager,
        "get",
        lambda key, default=None: "pre" if key == "app_update_channel" else default,
    )
    worker._get_remote_version("atomicparsley")
    check.manifest.assert_called_once_with("pre")


@pytest.mark.parametrize(
    "change",
    [
        {"version": "20230608.083822.0"},
        {"url": ATOMIC_URL.replace(".zip", "X86.zip")},
        {"url": "https://example.invalid/repacked.zip"},
        {"sha256": None},
        {"sha256": "None"},
        {"sha256": "a" * 63},
    ],
)
def test_unmatched_or_unverified_manifest_cannot_offer_install(atomic_check, change):
    worker, _, entry = atomic_check
    entry.update(change)
    with pytest.raises(UpdateError) as caught:
        worker._get_remote_version("atomicparsley")
    assert caught.value.code == "update_invalid_response"


def test_checksum_metadata_failure_is_not_ignored(atomic_check):
    worker, check, _ = atomic_check
    check.manifest.side_effect = UpdateError("update_network")
    with pytest.raises(UpdateError) as caught:
        worker._get_remote_version("atomicparsley")
    assert caught.value.code == "update_network"


@pytest.mark.parametrize("tampered", [False, True])
def test_fallback_checksum_is_enforced_by_actual_transport(atomic_check, tmp_path, tampered):
    worker, _, entry = atomic_check
    content = b"official release archive bytes"
    entry["sha256"] = hashlib.sha256(content).hexdigest()
    result = worker._get_remote_version("atomicparsley")
    body = b"changed archive bytes" if tampered else content
    response = Mock()
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.headers = {"Content-Length": str(len(body))}
    response.ok = True
    response.iter_content.return_value = [body]
    session = Mock()
    session.get.return_value = response
    transport = Transport("off", session=session)
    destination = tmp_path / "atomic.zip"
    if tampered:
        with pytest.raises(UpdateError) as caught:
            transport.download(result.url, destination, result.sha256)
        assert caught.value.code == "update_hash"
        assert not destination.exists()
    else:
        transport.download(result.url, destination, result.sha256)
        assert destination.read_bytes() == content


@pytest.mark.parametrize(
    "key,files",
    [("ffmpeg", ("ffmpeg.exe", "ffprobe.exe")), ("atomicparsley", ("AtomicParsley.exe",))],
)
@pytest.mark.parametrize("fallback_state", ["empty", "complete", "incomplete"])
def test_local_resolution_reads_complete_candidates_without_write_probe(
    tmp_path, monkeypatch, key, files, fallback_state
):
    primary, fallback = tmp_path / "app/bin", tmp_path / "data/components"
    for root in (primary, fallback):
        (root / key).mkdir(parents=True)
    for name in files:
        (primary / key / name).write_bytes(b"installed")
        if fallback_state == "complete" or (
            fallback_state == "incomplete" and key == "ffmpeg" and name == "ffmpeg.exe"
        ):
            (fallback / key / name).write_bytes(b"new installed")
    monkeypatch.setattr(component_paths, "component_roots", lambda: (primary, fallback))
    monkeypatch.setattr(dep.shutil, "which", lambda _: None)
    monkeypatch.setattr(
        component_paths,
        "component_target",
        lambda _: pytest.fail("Reading an installation must not probe write access"),
    )
    manager = dep.DependencyManager()
    resolved, source = manager.resolve_exe(key)
    expected = fallback if fallback_state == "complete" else primary
    assert resolved == expected / key / files[0]
    assert source == "bundled"
