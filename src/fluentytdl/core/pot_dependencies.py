"""Prepare and audit the same POT dependency tree for updates and packaging.

Versions are resolved by Deno using upstream's lockfile. This module only walks
the installed tree; it does not resolve versions or download packages.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .pot_component import ComponentError, filesystem_path

_PACKAGE_NAME = re.compile(r"(?:@[^./\\:]+/)?[^./\\:][^/\\:]*\Z")
_UNUSED_DIRS = {
    "test",
    "tests",
    "__tests__",
    "example",
    "examples",
    "benchmark",
    "benchmarks",
    "docs",
    "doc",
    "coverage",
    ".github",
    ".vscode",
    ".idea",
}
_UNUSED_SUFFIXES = (
    ".js.map",
    ".cjs.map",
    ".mjs.map",
    ".d.ts",
    ".d.cts",
    ".d.mts",
    ".d.ts.map",
    ".d.cts.map",
    ".d.mts.map",
    ".tsbuildinfo",
)


def _json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except (OSError, ValueError):
        pass
    raise ComponentError("worker_validate_failed", f"Invalid dependency metadata: {path.name}")


def write_production_lock(server: Path, destination: Path) -> None:
    """Drop development *roots* in a temporary lock; keep all locked resolutions.

    Deno 2.9.7's hoisted installer retains dev roots from lock.specifiers even
    with --prod (also with --entrypoint). Removing only these roots lets Deno
    retain shared/peer/optional dependencies and their original integrity hashes.
    Neither upstream package.json nor the delivered deno.lock is modified.
    """
    package = _json(server / "package.json")
    lock = _json(server / "deno.lock")
    development = set(package.get("devDependencies", {})) - (
        set(package.get("dependencies", {})) | set(package.get("optionalDependencies", {}))
    )
    specifiers = lock.get("specifiers")
    if not isinstance(specifiers, dict):
        raise ComponentError("worker_validate_failed", "Missing Deno locked specifiers")
    lock["specifiers"] = {
        key: value
        for key, value in specifiers.items()
        if not (key.startswith("npm:") and key[4:].rsplit("@", 1)[0] in development)
    }
    destination.write_text(json.dumps(lock, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _inventory(server: Path):
    modules = server / "node_modules"
    if modules.is_symlink() or modules.is_junction() or not modules.is_dir():
        raise ComponentError("worker_validate_failed", "Invalid node_modules directory")
    packages, files, directories = {}, [], []
    for current, dirs, names in os.walk(modules, followlinks=False):
        directory = Path(current)
        for name in (*dirs, *names):
            path = directory / name
            if path.is_symlink() or path.is_junction():
                raise ComponentError("pot_package_path", path.relative_to(modules).as_posix())
        directories.extend(directory / name for name in dirs)
        files.extend(directory / name for name in names)
        if "package.json" in names and (
            directory.parent.name == "node_modules"
            or (
                directory.parent.name.startswith("@")
                and directory.parent.parent.name == "node_modules"
            )
        ):
            packages[directory] = _json(directory / "package.json")
    return modules, packages, files, directories


def _production_packages(server: Path, packages: dict) -> set[Path]:
    keep = set()
    pending = [(server, _json(server / "package.json"))]
    while pending:
        parent, package = pending.pop()
        optional = package.get("optionalDependencies", {})
        peers = package.get("peerDependencies", {})
        dependencies = {**package.get("dependencies", {}), **peers, **optional}
        for name in dependencies:
            if not _PACKAGE_NAME.fullmatch(name):
                raise ComponentError("pot_package_path", name)
            resolved = None
            for ancestor in (parent, *parent.parents):
                if not ancestor.is_relative_to(server):
                    break
                if ancestor.name != "node_modules":
                    candidate = ancestor / "node_modules" / name
                    if candidate in packages:
                        resolved = candidate
                        break
            if resolved is None:
                if name in optional or (
                    name in peers
                    and name not in package.get("dependencies", {})
                    and package.get("peerDependenciesMeta", {}).get(name, {}).get("optional")
                ):
                    continue
                raise ComponentError("worker_validate_failed", f"Missing POT dependency: {name}")
            if resolved not in keep:
                keep.add(resolved)
                pending.append((resolved, packages[resolved]))
    if not keep:
        raise ComponentError("worker_validate_failed", "Empty POT production dependency tree")
    return keep


def _runtime_targets(package: dict) -> set[str]:
    def strings(value):
        if isinstance(value, str):
            yield value.removeprefix("./")
        elif isinstance(value, dict):
            for key, child in value.items():
                if key != "types":
                    yield from strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child)

    return set(strings({key: package.get(key) for key in ("main", "module", "exports", "browser")}))


def _unused_file(path: Path, package: dict) -> bool:
    parts = [part.casefold() for part in path.parts]
    name = parts[-1]
    # Licenses can live inside docs/tests/source folders. Keep their original paths.
    if any(
        part.startswith(("license", "licence", "copying", "copyright", "notice")) for part in parts
    ):
        return False
    targets = _runtime_targets(package)
    # Some packages expose runtime code under a directory conventionally used for
    # examples/tests. Keep that entire directory, including dynamically read data.
    for target in targets:
        if target == path.as_posix() or target == "*":
            return False
        target_parts = target.casefold().split("/")
        if any(
            part in _UNUSED_DIRS and parts[: index + 1] == target_parts[: index + 1]
            for index, part in enumerate(target_parts[:-1])
        ):
            return False
    if name == "package.json":
        return False
    if name.endswith(_UNUSED_SUFFIXES) or any(part in _UNUSED_DIRS for part in parts[:-1]):
        return True
    if (
        name.startswith(("changelog", "history", "contributing"))
        and (path.suffix.casefold() in {".md", ".markdown", ".rst", ".txt"} or not path.suffix)
    ) or (name.startswith("tsconfig") and name.endswith(".json")):
        return True
    if name.startswith((".eslintrc", ".prettierrc")):
        return True
    if name in {".npmignore", ".gitignore", ".editorconfig", ".travis.yml", "eslint.config.js"}:
        return True
    # Canvas ships native build projects alongside the required Release binaries.
    return package.get("name") == "canvas" and (
        name.endswith((".cc", ".h", ".gyp", ".gypi", ".sln", ".vcxproj", ".vcxproj.filters"))
        or "obj" in parts[:-1]
    )


def _cleanup_plan(server: Path):
    server = filesystem_path(server.resolve())
    modules, packages, files, directories = _inventory(server)
    keep = _production_packages(server, packages)
    remove = []
    for path in files:
        owner = path.parent
        while owner != modules and owner not in packages:
            owner = owner.parent
        if owner == modules:
            relative = path.relative_to(modules)
            if relative.parts[0] == ".bin" or (
                relative.parts[0] == ".deno" and relative.as_posix() != ".deno/.deno.lock"
            ):
                remove.append(path)
            elif relative.as_posix() != ".deno/.deno.lock":
                raise ComponentError("worker_validate_failed", "Unrecognized node_modules content")
        elif owner not in keep or _unused_file(path.relative_to(owner), packages[owner]):
            remove.append(path)
    return files, directories, remove, len(packages.keys() - keep)


def verify_dependencies(server: Path) -> None:
    """Read-only packaging gate; replayed snapshots cannot bypass cleanup."""
    _, _, remove, _ = _cleanup_plan(server)
    if remove:
        raise ComponentError(
            "worker_validate_failed", f"POT contains {len(remove)} non-runtime files"
        )


def prune_dependencies(server: Path) -> dict[str, int]:
    """Only call on a staged candidate, before its manifest and runtime checks."""
    files, directories, remove, extra_packages = _cleanup_plan(server)
    before = sum(path.stat().st_size for path in files)
    removed = sum(path.stat().st_size for path in remove)
    for path in remove:
        path.unlink()
    for directory in sorted(directories, key=lambda p: len(p.parts), reverse=True):
        if not any(directory.iterdir()):
            directory.rmdir()
    verify_dependencies(server)
    return {
        "before_bytes": before,
        "after_bytes": before - removed,
        "removed_files": len(remove),
        "removed_packages": extra_packages,
    }
