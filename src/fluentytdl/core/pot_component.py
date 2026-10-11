"""Original bgutil component contract, shared runtime and fixed installation paths.

This module has no Qt or application-state imports at module scope; build tools
and the installer subprocess use the same validation as the running application.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

IMPLEMENTATION = "brainicism"
UPSTREAM_REPOSITORY = "Brainicism/bgutil-ytdlp-pot-provider"
BUNDLE_PREFIX = "pot-original-v"
MANIFEST_NAME = "manifest.json"
HTTP_PLUGIN_FILES = ("getpot_bgutil.py", "getpot_bgutil_http.py")
PLUGIN_ASSET = "bgutil-ytdlp-pot-provider.zip"


def bundle_order(manifest: dict) -> tuple[int, ...]:
    return (*map(int, manifest["upstream_version"].split(".")), manifest["bundle_revision"])


def filesystem_path(path: Path) -> Path:
    """npm trees exceed MAX_PATH even under ordinary Windows installation roots."""
    path = path.absolute()
    value = str(path)
    if os.name == "nt" and not value.startswith("\\\\?\\"):
        value = "\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value
    return Path(value)


def runtime_path(path: Path) -> Path:
    # Deno canonicalizes module paths without the Win32 extended prefix. Passing
    # it as cwd makes relative read/FFI grants disagree with those canonical paths.
    value = str(path)
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


class ComponentError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code, self.detail = code, detail
        super().__init__(f"{code}: {detail}" if detail else code)


def valid_runtime_rules(rules) -> bool:
    if not isinstance(rules, dict):
        return False
    values = [rules.get("minimum"), rules.get("before")]
    return all(isinstance(v, str) and re.fullmatch(r"\d+\.\d+\.\d+", v) for v in values) and tuple(
        map(int, values[0].split("."))
    ) < tuple(map(int, values[1].split(".")))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _relative_parts(relative: str) -> tuple[str, ...]:
    name = PurePosixPath(relative)
    if (
        not relative
        or name.is_absolute()
        or "\\" in relative
        or ":" in relative
        or any(
            part in {"", "..", "."}
            or part.endswith((".", " "))
            or re.fullmatch(r"(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?", part)
            for part in relative.split("/")
        )
    ):
        raise ComponentError("pot_package_path", relative)
    return name.parts


def contained(root: Path, relative: str) -> Path:
    parts = _relative_parts(relative)
    candidate = root.joinpath(*parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ComponentError("pot_package_path", relative)
    return candidate


@dataclass(frozen=True)
class PotBundle:
    root: Path
    manifest: dict

    @property
    def bundle_id(self) -> str:
        return self.manifest["bundle_id"]

    @property
    def version(self) -> str:
        return self.manifest["upstream_version"]

    @property
    def plugin_root(self) -> Path:
        return self.root.parent / "yt-dlp" / "yt-dlp-plugins"

    @property
    def plugin_archive(self) -> Path:
        return self.plugin_root / PLUGIN_ASSET

    def command(self, deno: Path, port: int, *, offline: bool = False) -> list[str]:
        return [
            str(deno),
            "run",
            "--no-config",
            "--no-lock",
            "--no-prompt",
            "--cached-only",
            "--no-remote",
            "--node-modules-dir=manual",
            "--allow-env",
            "--allow-net=127.0.0.1" if offline else "--allow-net",
            "--allow-ffi=.",
            "--allow-read=..",
            "../src/main.ts",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

    @property
    def cwd(self) -> Path:
        return runtime_path(self.root / "server/node_modules")


def read_bundle(root: Path, *, verify: bool = False) -> PotBundle:
    root = filesystem_path(root)
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ComponentError("pot_package_missing", str(root)) from error
    if not isinstance(manifest, dict):
        raise ComponentError("pot_package_manifest")
    version = str(manifest.get("upstream_version", ""))
    revision = manifest.get("bundle_revision")
    if (
        manifest.get("schema") != 2
        or manifest.get("implementation") != IMPLEMENTATION
        or not re.fullmatch(r"\d+\.\d+\.\d+", version)
        or not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or manifest.get("bundle_id") != f"{BUNDLE_PREFIX}{version}-r{revision}"
        or manifest.get("server_version") != version
        or manifest.get("plugin_version") != version
        or manifest.get("platform") != "windows"
        or manifest.get("architecture") != "x64"
        or manifest.get("entrypoint") != "server/src/main.ts"
        or not valid_runtime_rules(manifest.get("deno_compatibility"))
    ):
        raise ComponentError("pot_package_incompatible")
    from .. import __version__
    from ..utils.app_version import version_key

    try:
        if version_key(manifest.get("min_app_version", "")) > version_key(__version__):
            raise ValueError("Application update required")
    except ValueError as error:
        raise ComponentError("pot_app_incompatible") from error
    files = manifest.get("files")
    required = {
        "server/src/main.ts",
        "server/src/utils.ts",
        "server/package.json",
        "server/package-lock.json",
        "server/node_modules/canvas/build/Release/canvas.node",
    }
    if not isinstance(files, dict) or not required <= files.keys():
        raise ComponentError("pot_package_incomplete")
    if verify:
        # Inspect directory links once before hashing. Resolving every npm file
        # and all its parents independently makes Windows startup exceed the
        # readiness budget even when the component is healthy.
        for path in (root / "server").rglob("*"):
            if path.is_symlink() or path.is_junction():
                raise ComponentError("pot_package_path", path.name)
            if (
                path.is_file()
                and path != root / MANIFEST_NAME
                and path.relative_to(root).as_posix() not in files
            ):
                raise ComponentError("pot_package_unlisted", path.name)
    for name, expected in files.items():
        parts = _relative_parts(name)
        if parts[0] not in {"server", "LICENSE"}:
            raise ComponentError("pot_package_path", name)
        if (
            not isinstance(expected, dict)
            or type(expected.get("size")) is not int
            or expected["size"] < 0
            or not re.fullmatch(r"[a-f0-9]{64}", str(expected.get("sha256", "")))
        ):
            raise ComponentError("pot_package_manifest", name)
        if verify or name in required:
            path = root.joinpath(*parts) if verify else contained(root, name)
            if not path.is_file() or path.is_symlink() or path.is_junction():
                raise ComponentError("pot_package_incomplete", name)
            if path.stat().st_size != expected.get("size") or digest(path) != expected["sha256"]:
                raise ComponentError("pot_package_corrupt", name)
    bundle = PotBundle(root.resolve(), manifest)
    archive = bundle.plugin_archive
    expected = manifest.get("plugin_archive", {})
    if (
        not archive.is_file()
        or archive.is_symlink()
        or archive.stat().st_size != expected.get("size")
        or digest(archive) != expected.get("sha256")
    ):
        raise ComponentError("pot_package_corrupt", PLUGIN_ASSET)
    return bundle


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def resolve_bundle(*, verify: bool = False) -> PotBundle:
    from ..utils.component_paths import managed_candidates

    for manifest in managed_candidates("pot-provider", MANIFEST_NAME):
        if manifest.is_file():
            return read_bundle(manifest.parent, verify=verify)
    raise ComponentError("pot_package_missing")


def runtime_compatible(bundle: PotBundle, version: str) -> bool:
    def number(text):
        return tuple(int(part) for part in text.split("."))

    try:
        rules = bundle.manifest["deno_compatibility"]
        return number(rules["minimum"]) <= number(version) < number(rules["before"])
    except (KeyError, TypeError, ValueError):
        return False


def local_probe(
    bundle: PotBundle,
    deno: Path,
    *,
    timeout: float = 12,
    cancel=None,
    on_process=None,
    check=None,
    environment=None,
):
    """Start one owned server; optionally verify its real Token path before stopping."""
    import socket

    from ..utils.paths import get_clean_env

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with tempfile.TemporaryDirectory(
        prefix="pot-acceptance-", dir=filesystem_path(Path(tempfile.gettempdir()))
    ) as cache:
        env = environment.copy() if environment is not None else get_clean_env()
        env.update(DENO_DIR=cache, DENO_NO_UPDATE_CHECK="1", PATH="")
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                bundle.command(deno, port, offline=check is None),
                cwd=bundle.cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if on_process:
                on_process(process)
            try:
                deadline = time.monotonic() + timeout
                http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                while time.monotonic() < deadline and process.poll() is None:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError("pot_check_cancelled")
                    try:
                        with http.open(f"http://127.0.0.1:{port}/ping", timeout=0.5) as reply:
                            ready = json.load(reply).get("version") == bundle.version
                    except (OSError, ValueError):
                        ready = False
                    if ready:
                        return check(f"http://127.0.0.1:{port}") if check else None
                    time.sleep(0.1)
                from ..utils.diagnostic_text import redact_detail

                log.seek(0)
                detail = redact_detail(log.read().decode("utf-8", "replace")[-1600:])
                raise ComponentError("pot_local_start_failed", f"exit={process.poll()}; {detail}")
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                if on_process:
                    on_process(None)
