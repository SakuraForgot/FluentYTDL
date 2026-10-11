"""Install the original provider directly from its upstream release and source tag.

Upstream's release ZIP is the Python plugin, not the HTTP server. The server is
prepared with our Deno using upstream's frozen lockfile, then activated with the
same recoverable component transaction as a packaged installation.
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .pot_component import (
    BUNDLE_PREFIX,
    HTTP_PLUGIN_FILES,
    PLUGIN_ASSET,
    UPSTREAM_REPOSITORY,
    ComponentError,
    atomic_json,
    contained,
    digest,
    filesystem_path,
    local_probe,
    read_bundle,
    runtime_path,
)
from .pot_dependencies import prune_dependencies, write_production_lock

API = f"https://api.github.com/repos/{UPSTREAM_REPOSITORY}"


def release_metadata(release: dict) -> dict:
    """Accept the actual upstream asset, never an application-owned repack URL."""
    tag = str(release.get("tag_name", ""))
    version = tag.removeprefix("v")
    assets = [a for a in release.get("assets", []) if a.get("name") == PLUGIN_ASSET]
    if (
        not re.fullmatch(r"v?\d+\.\d+\.\d+", tag)
        or release.get("draft")
        or release.get("prerelease")
        or len(assets) != 1
    ):
        raise ComponentError("pot_metadata_invalid")
    asset = assets[0]
    return validate_release_metadata(
        {
            "schema": 2,
            "implementation": "brainicism",
            "release_tag": tag,
            "upstream_version": version,
            "bundle_id": f"{BUNDLE_PREFIX}{version}-r1",
            "bundle_revision": 1,
            "asset_url": asset.get("browser_download_url"),
            "sha256": str(asset.get("digest", "")).removeprefix("sha256:"),
            "size": asset.get("size"),
        }
    )


def validate_release_metadata(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ComponentError("pot_metadata_invalid")
    tag = str(value.get("release_tag", ""))
    version = tag.removeprefix("v")
    url = f"https://github.com/{UPSTREAM_REPOSITORY}/releases/download/{tag}/{PLUGIN_ASSET}"
    if (
        value.get("schema") != 2
        or value.get("implementation") != "brainicism"
        or not re.fullmatch(r"v?\d+\.\d+\.\d+", tag)
        or value.get("upstream_version") != version
        or value.get("bundle_id") != f"{BUNDLE_PREFIX}{version}-r1"
        or value.get("bundle_revision") != 1
        or value.get("asset_url") != url
        or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", "")))
        or type(value.get("size")) is not int
        or not 0 < value["size"] <= 16 * 1024 * 1024
    ):
        raise ComponentError("pot_metadata_invalid")
    return value


def local_plugin_release(archive: Path, transport) -> dict:
    """Read the version as data; never import code from a selected plugin ZIP."""
    with zipfile.ZipFile(archive) as package:
        entry = package.getinfo("yt_dlp_plugins/extractor/getpot_bgutil.py")
        if entry.file_size > 256 * 1024:
            raise ComponentError("pot_package_size")
        match = re.search(rb"(?m)^__version__ = ['\"](\d+\.\d+\.\d+)['\"]\s*$", package.read(entry))
    if not match:
        raise ComponentError("pot_package_identity")
    return release_metadata(transport.get_json(f"{API}/releases/tags/{match[1].decode()}"))


def installation_runtime(path: Path | None = None):
    """Use the shared Deno component; its normal updater owns installation."""
    from ..utils.js_runtime import DenoRuntime, probe_deno, resolve_deno

    selected = DenoRuntime(path, "selected") if path else resolve_deno()
    try:
        runtime = probe_deno(selected)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise ComponentError(
            "pot_runtime_missing" if isinstance(error, FileNotFoundError) else "pot_runtime_invalid"
        ) from error
    # The relocatable hoisted npm layout requires Deno 2.9.7. Upstream's
    # ordinary linked development installation supports earlier Deno releases.
    if not (2, 9, 7) <= tuple(map(int, runtime.version.split("."))) < (3, 0, 0):
        raise ComponentError("pot_runtime_incompatible")
    return runtime


def _extract_source(archive: Path, destination: Path) -> None:
    """Extract the matching official tag, keeping only server and plugin sources."""
    fixed = {
        "LICENSE",
        "server/package.json",
        "server/package-lock.json",
        "server/deno.lock",
        "server/deno.json",
    }
    extracted = set()
    total = 0
    with zipfile.ZipFile(archive) as source:
        for entry in source.infolist():
            parts = PurePosixPath(entry.filename).parts
            name = "/".join(parts[1:])
            if entry.is_dir() or not (
                name in fixed or name.startswith(("server/src/", "plugin/yt_dlp_plugins/"))
            ):
                continue
            path = contained(destination, name)
            total += entry.file_size
            if name.casefold() in extracted or stat.S_ISLNK(entry.external_attr >> 16):
                raise ComponentError("pot_package_path")
            if total > 32 * 1024 * 1024:
                raise ComponentError("pot_package_size")
            raw = source.read(entry)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            extracted.add(name.casefold())
    if not {name.casefold() for name in fixed} <= extracted:
        raise ComponentError("pot_package_incomplete")


def _prepare_plugins(archive: Path, source: Path, bundle: Path) -> None:
    from .pot_component import PotBundle

    destination = PotBundle(bundle, {}).plugin_archive
    destination.parent.mkdir(parents=True)
    with zipfile.ZipFile(archive) as package:
        if len(package.namelist()) != len(set(package.namelist())):
            raise ComponentError("pot_package_path")
        for name in (*HTTP_PLUGIN_FILES, "getpot_bgutil_script.py"):
            relative = f"yt_dlp_plugins/extractor/{name}"
            raw = package.read(relative)
            if raw != (source / "plugin" / relative).read_bytes():
                raise ComponentError("pot_package_identity")
            compile(raw, name, "exec")
    shutil.copyfile(archive, destination)


def _install_dependencies(bundle: Path, deno: Path, transport, status, temporary: Path) -> None:
    from ..utils.diagnostic_text import redact_detail
    from ..utils.owned_process import attach_job, close_job
    from ..utils.paths import get_clean_env

    server = runtime_path(bundle / "server")
    production_lock = temporary / "production.deno.lock"
    write_production_lock(server, production_lock)
    lock_hash = digest(production_lock)
    command = [
        str(runtime_path(deno)),
        "install",
        "--prod",
        "--frozen",
        f"--lock={runtime_path(production_lock)}",
        "--allow-scripts=npm:canvas",
        "--node-modules-linker=hoisted",
    ]
    env = get_clean_env()
    for key in tuple(env):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}:
            env.pop(key)
    proxy = transport.session.proxies.get("https", "")
    if transport.proxy_mode == "system":
        proxy = urllib.request.getproxies().get("https", "")
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, NO_PROXY="127.0.0.1,localhost")
    # Dependency setup uses only Deno, never a developer's global Node/npm.
    env.update(
        PATH="",
        DENO_DIR=str(runtime_path(temporary / "deno-cache")),
        DENO_NO_UPDATE_CHECK="1",
        DENO_NO_PROMPT="1",
    )
    status("preparing")
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            command,
            cwd=server,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        attach_job(process)
        try:
            process.wait(timeout=180)
            if process.returncode:
                log.seek(0)
                raise ComponentError(
                    "pot_dependency_install_failed",
                    redact_detail(log.read().decode("utf-8", "replace")[-2000:]),
                )
            if digest(production_lock) != lock_hash:
                raise ComponentError(
                    "pot_package_identity", "Production lock changed during install"
                )
        except subprocess.TimeoutExpired as error:
            raise ComponentError("pot_dependency_install_timeout") from error
        finally:
            close_job(process)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def _accept(bundle, runtime, exe, transport, *, packaged=False):
    """Validate the provider and official ZIP using the actual selected CLI."""
    from ..utils.paths import get_clean_env
    from ..youtube.pot_probe import probe_provider

    if not exe or not exe.is_file():
        raise ComponentError("pot_ytdlp_missing")
    env = get_clean_env()
    for key in tuple(env):
        if key.lower().endswith("_proxy"):
            env.pop(key)
    proxy = transport.session.proxies.get("https", "")
    if transport.proxy_mode == "system":
        proxy = urllib.request.getproxies().get("https", "")
    if proxy:
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy)
    env["NO_PROXY"] = "127.0.0.1,localhost"

    def check(url):
        # A transient token failure in this single window would otherwise roll back a
        # fully-built component; retry the probe, but never a structural plugin fault.
        for attempt in range(3):
            try:
                return probe_provider(
                    runtime_path(exe),
                    runtime_path(bundle.plugin_root),
                    url,
                    proxy,
                    timeout=25,
                    env=env,
                    default_plugins=False,
                    token_required=not packaged,
                )
            except RuntimeError as error:
                if attempt == 2 or str(error) in {
                    "pot_plugin_conflict",
                    "pot_plugin_incompatible",
                }:
                    raise
                time.sleep(2 * (attempt + 1))

    return local_probe(bundle, runtime.path, check=check, environment=env)


def _clean_previous_layout(target):
    """Retire only recognizable app-owned installations after acceptance."""
    from .updater_worker import _remove_entry

    candidates = list((target / "versions").glob("*/manifest.json"))
    candidates.append(target / "bundled/manifest.json")
    for manifest in candidates:
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if value.get("implementation") == "brainicism" and str(
                value.get("bundle_id", "")
            ).startswith(BUNDLE_PREFIX):
                _remove_entry(manifest.parent, target)
        except (OSError, ValueError):
            continue
    try:
        (target / "versions").rmdir()  # Only an empty directory.
    except OSError:
        pass
    for name in ("active.json", "previous.json", "pending.json", "failed-pending.json"):
        path = target / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if str(value.get("bundle_id", "")).startswith(BUNDLE_PREFIX):
                path.unlink()
        except (OSError, ValueError):
            pass


def install_release(
    metadata: dict,
    target: Path,
    transport,
    *,
    deno_path=None,
    progress=None,
    status=None,
    plugin_archive: Path | None = None,
    packaged: bool = False,
    ytdlp_path: Path | None = None,
    before_install=None,
):
    metadata = validate_release_metadata(metadata)
    runtime = installation_runtime(deno_path)
    progress = progress or (lambda *_: None)
    status = status or (lambda *_: None)
    target = filesystem_path(target)
    target.mkdir(parents=True, exist_ok=True)
    if ytdlp_path is None:
        from ..youtube.yt_dlp_cli import resolve_yt_dlp_exe

        ytdlp_path = resolve_yt_dlp_exe()
    with tempfile.TemporaryDirectory(prefix=".upstream-", dir=target) as directory:
        temporary = Path(directory)
        bundle = temporary / "bin/pot-provider"
        bundle.mkdir(parents=True)
        plugin_zip = temporary / "plugin.zip"
        if plugin_archive:
            shutil.copyfile(plugin_archive, plugin_zip)
        else:
            transport.download(metadata["asset_url"], plugin_zip, metadata["sha256"], progress)
        if (
            plugin_zip.stat().st_size != metadata["size"]
            or digest(plugin_zip) != metadata["sha256"]
        ):
            raise ComponentError("worker_hash_mismatch")
        source_url = f"https://github.com/{UPSTREAM_REPOSITORY}/archive/refs/tags/{metadata['release_tag']}.zip"
        source_zip = temporary / "source.zip"
        transport.download(source_url, source_zip, progress=progress)
        source = temporary / "source"
        _extract_source(source_zip, source)
        version = metadata["upstream_version"]
        if (
            json.loads((source / "server/package.json").read_text(encoding="utf-8"))["version"]
            != version
        ):
            raise ComponentError("pot_package_identity")
        shutil.copytree(source / "server", bundle / "server")
        _prepare_plugins(plugin_zip, source, bundle)
        shutil.copy2(source / "LICENSE", bundle / "LICENSE")
        deno_version = runtime.version
        lock = digest(bundle / "server/deno.lock")
        _install_dependencies(bundle, runtime.path, transport, status, temporary)
        if digest(bundle / "server/deno.lock") != lock:
            raise ComponentError("pot_package_identity")
        cleanup = prune_dependencies(bundle / "server")
        from loguru import logger

        logger.info(
            "POT dependencies: {:.2f} -> {:.2f} MiB; removed {} files and {} packages",
            cleanup["before_bytes"] / 1048576,
            cleanup["after_bytes"] / 1048576,
            cleanup["removed_files"],
            cleanup["removed_packages"],
        )
        files = {}
        for path in bundle.rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ComponentError("pot_package_path", path.name)
            if path.is_file():
                files[path.relative_to(bundle).as_posix()] = {
                    "size": path.stat().st_size,
                    "sha256": digest(path),
                }
        manifest = {
            "schema": 2,
            "implementation": "brainicism",
            "bundle_id": metadata["bundle_id"],
            "bundle_revision": 1,
            "upstream_version": version,
            "server_version": version,
            "plugin_version": version,
            "platform": "windows",
            "architecture": "x64",
            "min_app_version": "3.7.7-rc.1",
            "deno_compatibility": {
                "minimum": "2.9.7",
                "before": f"{int(deno_version.split('.')[0]) + 1}.0.0",
                "tested": [deno_version],
            },
            "entrypoint": "server/src/main.ts",
            "files": files,
            "plugin_archive": {"size": metadata["size"], "sha256": metadata["sha256"]},
            "source_provenance": {
                "repository": UPSTREAM_REPOSITORY,
                "tag": metadata["release_tag"],
                "source_url": source_url,
                "source_sha256": digest(source_zip),
                "plugin_url": metadata["asset_url"],
                "plugin_sha256": metadata["sha256"],
                "deno_lock_sha256": lock,
            },
        }
        atomic_json(bundle / "manifest.json", manifest)
        status("validate")
        prepared = read_bundle(bundle, verify=True)
        _accept(prepared, runtime, ytdlp_path, transport, packaged=packaged)
        if before_install:
            before_install(bundle)
        from ..youtube.yt_dlp_cli import legacy_pot_directories
        from .pot_component import PotBundle
        from .updater_worker import install_group

        destination = PotBundle(target, manifest)
        legacy = legacy_pot_directories(destination.plugin_root)
        install_group(
            [(bundle / name, target / name) for name in ("server", "LICENSE", "manifest.json")]
            + [(prepared.plugin_archive, destination.plugin_archive)]
            + [(None, path) for path in legacy],
            root=target.parent,
            validate=lambda: _accept(
                read_bundle(target, verify=True), runtime, ytdlp_path, transport, packaged=packaged
            ),
        )
    _clean_previous_layout(target)
    return read_bundle(target)
