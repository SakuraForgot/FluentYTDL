"""The default bundled core must match the selected, verified SABR release."""

import hashlib
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import component_snapshot as snapshots  # noqa: E402
import fetch_tools  # noqa: E402
import generate_manifest  # noqa: E402


def _payload(overrides=None):
    content = b"verified SABR executable"
    info = {
        "release_repository": fetch_tools.SABR_REPOSITORY,
        "tag": "sabr-new",
        "architecture": "Windows x64",
        "variant": "sabr-test",
        "source_sha": "a" * 40,
        "exe_sha256": hashlib.sha256(content).hexdigest(),
        "version": "2026.09.28",
        **(overrides or {}),
    }
    files = {
        "yt-dlp.exe": content,
        "BUILD_INFO.json": json.dumps(info).encode(),
        "LICENSE": b"core license",
        "THIRD_PARTY_LICENSES.txt": b"dependency licenses",
    }
    files["SHA256SUMS.txt"] = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()
    ).encode()
    return files


def _release(tag="sabr-new", published="2026-09-28T12:00:00Z", **extra):
    files = _payload()
    return {
        "id": 12,
        "tag_name": tag,
        "published_at": published,
        "prerelease": True,
        "assets": [
            {
                "id": index,
                "name": name,
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "browser_download_url": f"https://github.com/{fetch_tools.SABR_REPOSITORY}/releases/download/{tag}/{name}",
            }
            for index, (name, content) in enumerate(files.items(), start=1)
        ],
        **extra,
    }


def test_sabr_snapshot_and_manifest_share_verified_release(monkeypatch, tmp_path):
    api = Mock(
        return_value=[
            _release("sabr-old", "2026-09-27T00:00:00Z"),
            _release("sabr-draft", "2026-10-01T00:00:00Z", draft=True),
            _release("sabr-incomplete", "2026-10-02T00:00:00Z", assets=[]),
            _release(),
        ]
    )
    monkeypatch.setattr(fetch_tools, "github_api", api)
    files = _payload()
    urls = []

    def download(url, destination, *args, **kwargs):
        urls.append(url)
        destination.write_bytes(files[url.rsplit("/", 1)[-1]])

    monkeypatch.setattr(fetch_tools, "download_file", download)
    monkeypatch.setattr(fetch_tools, "probe_version", lambda *a: "2026.09.28")
    key, subdir, fetcher, filenames, _probe = fetch_tools.FETCHERS[0]
    entry = snapshots._resolve_component(tmp_path, {}, key, subdir, fetcher, filenames)
    api.assert_called_once_with(f"/repos/{fetch_tools.SABR_REPOSITORY}/releases?per_page=100")
    assert all("/download/sabr-new/" in url for url in urls)
    assert entry["channel"] == "sabr"
    assert entry["release_tag"] == "sabr-new"
    assert entry["build_info"]["source_sha"] == "a" * 40
    assert set(entry["files"]) == {f"bin/yt-dlp/{name}" for name in fetch_tools.SABR_FILES}
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(json.dumps({"tools": {"yt-dlp": entry}}), encoding="utf-8")
    monkeypatch.setenv("FLUENTYTDL_COMPONENT_SNAPSHOT", str(snapshot))
    manifest = generate_manifest.generate_manifest("3.7.7-rc.1", tmp_path, "https://example.com")
    core = manifest["components"]["bin/yt-dlp"]
    assert core["repo"] == fetch_tools.SABR_REPOSITORY
    assert core["sha256"] == entry["build_info"]["exe_sha256"]
    assert core["url"].endswith("/sabr-new/yt-dlp.exe")


def test_no_complete_sabr_release_never_queries_official(monkeypatch):
    api = Mock(return_value=[_release(draft=True), _release(assets=[])])
    monkeypatch.setattr(fetch_tools, "github_api", api)
    with pytest.raises(RuntimeError, match="complete SABR"):
        snapshots.ReleaseSource(fetch_tools.SABR_REPOSITORY)
    assert api.call_count == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"tag": "sabr-other"},
        {"release_repository": "yt-dlp/yt-dlp"},
        {"architecture": "Linux"},
        {"variant": "stable"},
        {"source_sha": "invalid"},
        {"version": "unknown"},
        {"exe_sha256": "0" * 64},
    ],
)
def test_sabr_identity_and_hash_rejected_before_execution(monkeypatch, tmp_path, overrides):
    files = _payload(overrides)
    probe = Mock(side_effect=AssertionError("Unverified executable must not run"))
    monkeypatch.setattr(fetch_tools, "probe_version", probe)
    with pytest.raises(RuntimeError):
        fetch_tools.fetch_sabr_yt_dlp(
            tmp_path / "bin",
            release_tag="sabr-new",
            download=lambda url, dest: dest.write_bytes(files[url.rsplit("/", 1)[-1]]),
        )
    probe.assert_not_called()
    assert not (tmp_path / "bin").exists()


@pytest.mark.parametrize("failure", ["checksum", "version", "variant"])
def test_sabr_rejects_checksum_or_executable_identity_mismatch(monkeypatch, tmp_path, failure):
    files = _payload()
    if failure == "checksum":
        files["yt-dlp.exe"] += b"tampered"
    probe = Mock(side_effect=["wrong" if failure == "version" else "2026.09.28", "official"])
    monkeypatch.setattr(fetch_tools, "probe_version", probe)
    with pytest.raises(RuntimeError):
        fetch_tools.fetch_sabr_yt_dlp(
            tmp_path / "bin",
            release_tag="sabr-new",
            download=lambda url, dest: dest.write_bytes(files[url.rsplit("/", 1)[-1]]),
        )
    if failure == "checksum":
        probe.assert_not_called()
    assert not (tmp_path / "bin").exists()
