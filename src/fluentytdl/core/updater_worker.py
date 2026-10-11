"""Component installer subprocess. Validate first, commit as a group, never kill users' tools."""

from __future__ import annotations

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

ERR_BAD_INPUT = "worker_bad_input"
ERR_MISSING_PARAMS = "worker_missing_params"
ERR_HASH_MISMATCH = "worker_hash_mismatch"
ERR_DOWNLOAD_FAILED = "worker_download_failed"


class HashMismatch(Exception):
    pass


def _retry_file_operation(operation):
    # Windows may briefly retain handles to freshly copied native modules.
    # A persistent lock still fails and leaves rollback to the journal.
    for attempt in range(21):
        try:
            return operation()
        except PermissionError:
            if attempt == 20:
                raise
            time.sleep(0.25)


def print_message(msg_type, **kwargs):
    print(json.dumps({"type": msg_type, **kwargs}), flush=True)


def print_error(code, detail=""):
    from ..utils.diagnostic_text import redact_detail

    print_message("error", code=code, msg=code, detail=redact_detail(detail))


def validate_executable(path: Path, *, run: bool = True):
    with path.open("rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:2] != b"MZ":
            raise ValueError(f"Invalid Windows executable: {path.name}")
        stream.seek(struct.unpack_from("<I", header, 60)[0])
        if stream.read(4) != b"PE\0\0":
            raise ValueError(f"Invalid PE signature: {path.name}")
    if run:
        option = "-version" if path.name.lower() in {"ffmpeg.exe", "ffprobe.exe"} else "--version"
        result = subprocess.run(
            [str(path), option],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=8,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if result.returncode:
            raise ValueError(f"Executable probe failed: {path.name} exit={result.returncode}")


def _copy_entry(source, target):
    if source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _remove_entry(path, root):
    """Deletion is restricted to a checked member of this managed transaction."""
    if (
        path.is_symlink()
        or path.is_junction()
        or not path.resolve().is_relative_to(root.resolve())
        or path.resolve() == root.resolve()
    ):
        raise ValueError("Invalid component transaction path")
    if path.is_dir():
        _retry_file_operation(lambda: shutil.rmtree(path))
    else:
        _retry_file_operation(lambda: path.unlink(missing_ok=True))


def install_group(files: list[tuple[Path, Path]], *, root=None, validate=None):
    """Same-volume journal/backup before any replacement, rollback on any failure."""
    from .pot_component import ComponentError, atomic_json, contained, filesystem_path

    target_dir = filesystem_path(root or files[0][1].parent)
    target_dir.mkdir(parents=True, exist_ok=True)
    journal = target_dir / ".install-journal.json"
    recover_install(target_dir)
    transaction = target_dir / f".install-{uuid.uuid4().hex}"
    transaction.mkdir()
    entries = []
    try:
        for index, (source, target) in enumerate(files):
            target = filesystem_path(target)
            relative = target.relative_to(target_dir).as_posix()
            contained(target_dir, relative)
            prepared = transaction / f"{index}.prepared"
            if source is not None:
                _copy_entry(source, prepared)
            backup = transaction / f"{index}.previous"
            existed = target.exists()
            if existed:
                _copy_entry(target, backup)
            entries.append(
                {
                    "target": relative,
                    "prepared": prepared.name,
                    "backup": backup.name,
                    "existed": existed,
                    "remove": source is None,
                }
            )
        atomic_json(
            journal, {"directory": transaction.name, "entries": entries, "committed": False}
        )
        for entry in entries:
            target = contained(target_dir, entry["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_dir() or entry["remove"]:
                _remove_entry(target, target_dir)
            if not entry["remove"]:
                prepared = transaction / entry["prepared"]
                _retry_file_operation(lambda s=prepared, t=target: os.replace(s, t))
        if validate:
            validate()
        atomic_json(journal, {"directory": transaction.name, "entries": entries, "committed": True})
    except Exception as error:
        if journal.exists():
            recover_install(target_dir)
        raise ComponentError("worker_commit_failed", str(error)) from error
    finally:
        if journal.exists():
            data = json.loads(journal.read_text())
            if data["committed"]:
                try:
                    recover_install(target_dir)
                except OSError:
                    pass  # Keep the committed journal for cleanup at next startup.
        else:
            _remove_entry(transaction, target_dir)


def recover_install(target_dir):
    from .pot_component import contained, filesystem_path

    target_dir = filesystem_path(target_dir)
    journal = target_dir / ".install-journal.json"
    if not journal.exists():
        return
    value = json.loads(journal.read_text(encoding="utf-8"))
    if not isinstance(value.get("entries"), list) or not 0 < len(value["entries"]) <= 16:
        raise ValueError("Invalid component transaction entries")
    transaction = contained(target_dir, value["directory"])
    if not transaction.name.startswith(".install-") or transaction.parent != target_dir:
        raise ValueError("Invalid component transaction directory")
    if not value["committed"]:
        for item in value["entries"]:
            if any(Path(item[key]).name != item[key] for key in ("backup", "prepared")):
                raise ValueError("Invalid component transaction path")
            target = contained(target_dir, item["target"])
            if item["existed"]:
                backup = contained(transaction, item["backup"])

                def _restore(target=target, backup=backup):
                    # Symmetry with the forward os.replace: a lock that outlives the
                    # forward retry window must not abandon rollback mid-restore.
                    if backup.is_dir() or target.is_dir():
                        _remove_entry(target, target_dir)
                    elif target.exists():
                        target.unlink()
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _copy_entry(backup, target)

                _retry_file_operation(_restore)
            else:
                _remove_entry(target, target_dir)
    if not value["committed"]:
        # Recovery is complete; a repeated startup only needs to clean backups.
        from .pot_component import atomic_json

        atomic_json(journal, {**value, "committed": True})
    _remove_entry(transaction, target_dir)
    journal.unlink()


def safe_install(source_path, target_path, *, manifest=None, before_install=None):
    validate_executable(Path(source_path))
    files = [(Path(source_path), Path(target_path))]
    if manifest is not None:
        sidecar = Path(source_path).parent / "manifest.json"
        sidecar.write_text(json.dumps(manifest), encoding="utf-8")
        files.append((sidecar, Path(target_path).parent / "manifest.json"))
    if before_install:
        before_install(Path(source_path))
    install_group(files)


def handle_zip(zip_path, target_exe, dest_dir, extra_exes, *, validate=None, before_install=None):
    with tempfile.TemporaryDirectory(prefix=".extract-", dir=dest_dir) as directory:
        with zipfile.ZipFile(zip_path) as archive:
            files = []
            for name in (target_exe.name, *extra_exes):
                matches = [
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                    and item.filename.rsplit("/", 1)[-1].casefold() == name.casefold()
                ]
                if len(matches) != 1:
                    raise FileNotFoundError(f"Expected exactly one {name} in archive")
                extracted = Path(directory) / name
                with archive.open(matches[0]) as source, extracted.open("wb") as output:
                    shutil.copyfileobj(source, output)
                validate_executable(extracted)
                files.append((extracted, dest_dir / name))
        if before_install:
            before_install(files[0][0])
        if validate:
            validate(files[0][0])
        install_group(files)


def validate_pot_dependency(key, path):
    """Check local compatibility before replacing Deno or the extraction engine."""
    from .pot_component import ComponentError, local_probe, resolve_bundle, runtime_compatible

    try:
        bundle = resolve_bundle(verify=True)
    except ComponentError:
        # A missing/damaged POT cannot be a compatibility baseline. Candidates
        # still pass their own executable validation; POT readiness is checked
        # again after activation. This also allows repairing Deno before POT.
        return
    if key == "deno":
        from ..utils.js_runtime import DenoRuntime, probe_deno

        runtime = probe_deno(DenoRuntime(path, "candidate"))
        if not runtime_compatible(bundle, runtime.version):
            raise ComponentError("pot_runtime_incompatible")
        local_probe(bundle, path)
    elif key == "yt-dlp":
        from ..utils.paths import get_clean_env
        from ..youtube.pot_probe import probe_provider

        probe_provider(
            path,
            bundle.plugin_root,
            "http://127.0.0.1:1",
            "",
            timeout=8,
            env=get_clean_env(),
            token_required=False,
            default_plugins=False,
        )


def recover_managed_installs():
    """Called once after the application's single-instance lock, before services."""
    from ..utils.component_paths import component_roots

    failures = []
    for root in component_roots():
        for key in ("", "yt-dlp", "ffmpeg", "deno", "atomicparsley"):
            try:
                recover_install(root / key)
            except (OSError, ValueError, KeyError, TypeError) as error:
                failures.append((key, type(error).__name__))
    return failures


def run_worker():
    try:
        config = json.loads(sys.stdin.readline())
    except Exception as error:
        print_error(ERR_BAD_INPUT, type(error).__name__)
        return 1
    if not all(config.get(key) for key in ("key", "target_exe")) or not (
        config.get("local_source") or (config.get("url") and config.get("expected_sha256"))
    ):
        print_error(ERR_MISSING_PARAMS, "A verified download URL, target and SHA256 are required")
        return 1
    stage = "download"

    def prepare_status(status):
        nonlocal stage
        if status in {"preparing", "validate"}:
            stage = "prepare" if status == "preparing" else "validate"
        print_message("status", msg=status)

    def before_install(candidate):
        nonlocal stage
        stage = "commit"
        if config.get("activation_gate"):
            print_message("activation_request")
            grant = json.loads(sys.stdin.readline())
            if grant != {"activate": True}:
                raise ValueError("Component activation was not granted")
        # The active POT bundle may have changed during the download. Validate
        # against the current bundle after acquiring the application's gate.
        if config["key"] in {"deno", "yt-dlp"}:
            validate_pot_dependency(config["key"], candidate)

    try:
        from .update_transport import Transport

        target = Path(config["target_exe"])
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".download-", dir=target.parent) as directory:
            payload = Path(directory) / target.name
            if config.get("local_source"):
                from .pot_component import digest

                stage = "validate"
                print_message("status", msg=stage)
                source = Path(config["local_source"])
                if config["key"] == "pot-provider":
                    from .pot_upstream import (
                        install_release,
                        local_plugin_release,
                    )

                    transport = Transport(
                        config.get("proxy_mode") or "off", config.get("proxy_url") or ""
                    )
                    try:
                        bundle = install_release(
                            local_plugin_release(source, transport),
                            target.parent,
                            transport,
                            deno_path=Path(config["deno_path"])
                            if config.get("deno_path")
                            else None,
                            plugin_archive=source,
                            ytdlp_path=Path(config["ytdlp_path"])
                            if config.get("ytdlp_path")
                            else None,
                            before_install=before_install,
                            progress=lambda percent: print_message("progress", percent=percent),
                            status=prepare_status,
                        )
                    finally:
                        transport.session.close()
                    print_message("done", bundle_id=bundle.bundle_id)
                    return 0
                files = []
                for original, destination in [
                    (source, target),
                    *(
                        (source.parent / name, target.parent / name)
                        for name in config.get("extra_exes", [])
                    ),
                ]:
                    prepared = Path(directory) / destination.name
                    before = digest(original)
                    shutil.copy2(original, prepared)
                    if digest(prepared) != before:
                        raise HashMismatch("Local file changed while copying")
                    validate_executable(prepared)
                    files.append((prepared, destination))
                if config["key"] == "yt-dlp":
                    from ..utils.ytdlp_runtime import probe_version

                    identity = probe_version(files[0][0])
                    sidecar = Path(directory) / "manifest.json"
                    sidecar.write_text(
                        json.dumps({"version": identity.version, "channel": identity.channel}),
                        encoding="utf-8",
                    )
                    files.append((sidecar, target.parent / "manifest.json"))
                before_install(files[0][0])
                install_group(files)
                print_message("done")
                return 0
            transport = Transport(
                config.get("proxy_mode") or "off",
                config.get("proxy_url") or "",
                progress=lambda attempt: print_message("status", msg="retrying", attempt=attempt),
            )
            try:
                if config["key"] == "pot-provider":
                    from .pot_upstream import install_release

                    bundle = install_release(
                        config.get("pot_release"),
                        target.parent,
                        transport,
                        deno_path=Path(config["deno_path"]) if config.get("deno_path") else None,
                        ytdlp_path=Path(config["ytdlp_path"]) if config.get("ytdlp_path") else None,
                        before_install=before_install,
                        progress=lambda percent: print_message("progress", percent=percent),
                        status=prepare_status,
                    )
                    print_message("done", bundle_id=bundle.bundle_id)
                    return 0
                transport.download(
                    config["url"],
                    payload,
                    config["expected_sha256"],
                    lambda percent: print_message("progress", percent=percent),
                )
            finally:
                transport.session.close()
            stage = "validate"
            print_message("status", msg=stage)
            if urlsplit(config["url"]).path.lower().endswith(".zip"):
                handle_zip(
                    payload,
                    target,
                    target.parent,
                    config.get("extra_exes", []),
                    before_install=before_install,
                )
            else:
                validate_executable(payload)
                manifest = (
                    {
                        "version": config.get("expected_version", ""),
                        "channel": config.get("expected_channel", ""),
                    }
                    if config["key"] == "yt-dlp"
                    else None
                )
                safe_install(payload, target, manifest=manifest, before_install=before_install)
            print_message("done")
        return 0
    except Exception as error:
        code = getattr(error, "code", "")
        if code == "update_hash":
            code = ERR_HASH_MISMATCH
        print_error(code or f"worker_{stage}_failed", f"{type(error).__name__}: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(run_worker())
