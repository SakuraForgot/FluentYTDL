"""Resolve latest upstream releases once, then build from verified local bytes."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

import fetch_tools

REPOSITORIES = {
    "yt-dlp": "yt-dlp/yt-dlp",
    "ffmpeg": "yt-dlp/FFmpeg-Builds",
    "deno": "denoland/deno",
    "atomicparsley": "wez/atomicparsley",
    "pot-provider": "jim60105/bgutil-ytdlp-pot-provider-rs",
    "7zip": "ip7z/7zip",
}


class ReleaseSource:
    def __init__(self, repository: str):
        endpoint = f"/repos/{repository}/releases/" + (
            "tags/latest" if repository == REPOSITORIES["ffmpeg"] else "latest"
        )
        self.release = fetch_tools.github_api(endpoint)
        if not self.release.get("id") or not self.release.get("assets"):
            raise RuntimeError(f"Cannot resolve latest assets: {repository}")
        self.repository = repository
        self.assets = {a["name"]: a for a in self.release["assets"]}
        self.downloads: dict[str, dict] = {}
        self._download = fetch_tools.download_file

    def download(self, url: str, dest: Path, *args, **kwargs) -> None:
        name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        asset = self.assets.get(name)
        if asset is None:
            raise RuntimeError(f"Required latest asset missing: {self.repository}/{name}")
        resolved = asset["browser_download_url"]
        self._download(resolved, dest, *args, **kwargs)
        actual = fetch_tools.sha256_file(dest)
        if dest.stat().st_size != asset["size"]:
            raise RuntimeError(f"Asset size changed after resolution: {name}")
        digest = asset.get("digest")
        if digest and digest.startswith("sha256:") and digest[7:].lower() != actual:
            raise RuntimeError(f"Upstream asset digest mismatch: {name}")
        self.downloads[name] = {
            "id": asset["id"],
            "url": resolved,
            "sha256": actual,
            "size": dest.stat().st_size,
            "verification": "github-sha256"
            if digest and digest.startswith("sha256:")
            else "observed-sha256",
        }

    def metadata(self) -> dict:
        return {
            "repo": self.repository,
            "release_id": self.release["id"],
            "release_tag": self.release["tag_name"],
            "assets": self.downloads,
        }


def prepare(destination: Path) -> Path:
    """Always fetch latest; no source-controlled file is changed."""
    destination.mkdir(parents=True, exist_ok=False)
    snapshot = {
        "schema_version": 1,
        "policy": "latest",
        "resolved_at": datetime.now(UTC).isoformat(),
        "tools": {},
    }
    baseline_path = Path(__file__).resolve().parent / "TOOLS.lock.json"
    try:
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["tools"]
    except (OSError, ValueError, KeyError):
        baseline = {}
    snapshot["baseline_sha256"] = (
        fetch_tools.sha256_file(baseline_path) if baseline_path.is_file() else None
    )
    for key, subdir, fetcher, filenames, _probe in fetch_tools.FETCHERS:
        source = ReleaseSource(REPOSITORIES[key])
        original_download, original_meta = fetch_tools.download_file, fetch_tools.asset_meta
        try:
            fetch_tools.download_file = source.download
            fetch_tools.asset_meta = lambda url, path, source=source: source.downloads[
                unquote(urlparse(url).path.rsplit("/", 1)[-1])
            ]
            version, files, asset = fetcher(destination / "bin" / subdir)
        finally:
            fetch_tools.download_file, fetch_tools.asset_meta = original_download, original_meta
        if not version or version.lower() in ("unknown", "n/a"):
            raise RuntimeError(f"Cannot probe downloaded {key} version")
        if any(not files.get(name) or not files[name].is_file() for name in filenames):
            raise RuntimeError(f"Incomplete {key} payload")
        entry = {
            **source.metadata(),
            "version": version,
            "files": {
                path.relative_to(destination).as_posix(): {
                    "sha256": fetch_tools.sha256_file(path),
                    "size": path.stat().st_size,
                }
                for path in files.values()
            },
        }
        if asset is not None:
            if key in ("yt-dlp", "deno"):
                asset["verification"] = "upstream-checksum"
            entry["asset"] = asset
        previous = baseline.get(key)
        if previous is None:
            entry["drift"] = "new-component"
        elif previous.get("version") != version:
            entry["drift"] = "version-change"
        elif any(
            previous.get("files", {}).get(name, {}).get("sha256") != fetch_tools.sha256_file(path)
            for name, path in files.items()
        ):
            entry["drift"] = "same-version-content-change"
        else:
            entry["drift"] = "unchanged"
        snapshot["tools"][key] = entry
    from prepare_7zip import prepare as prepare_7zip

    sevenzip = prepare_7zip(destination / "7zip", allow_environment=False)
    entry = json.loads((sevenzip / "version.json").read_text(encoding="utf-8"))
    entry["files"] = {
        p.relative_to(destination).as_posix(): {
            "sha256": fetch_tools.sha256_file(p),
            "size": p.stat().st_size,
        }
        for p in sevenzip.iterdir()
        if p.is_file()
    }
    snapshot["tools"]["7zip"] = entry
    path = destination / "snapshot.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def verify(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise RuntimeError("Unsupported component snapshot schema")
    if set(data["tools"]) != set(REPOSITORIES):
        raise RuntimeError("Snapshot must contain all six component groups")
    listed = {path.name}
    for entry in data["tools"].values():
        for relative, expected in entry["files"].items():
            listed.add(relative)
            candidate = (path.parent / relative).resolve()
            if not candidate.is_relative_to(path.parent.resolve()) or not candidate.is_file():
                raise RuntimeError(f"Invalid snapshot file: {relative}")
            if (
                candidate.stat().st_size != expected["size"]
                or fetch_tools.sha256_file(candidate) != expected["sha256"]
            ):
                raise RuntimeError(f"Snapshot hash mismatch: {relative}")
    actual = {p.relative_to(path.parent).as_posix() for p in path.parent.rglob("*") if p.is_file()}
    if actual != listed:
        raise RuntimeError("Unlisted or missing files in component snapshot")
    return data


def replay(path: Path, destination: Path) -> Path:
    verify(path)
    shutil.copytree(path.parent, destination)
    return destination / path.name
