"""The release archive protocol: LZMA2 only, verified by the legacy decoder."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

CLI_OPTIONS = ["-t7z", "-mx=7", "-m0=LZMA2", "-mf=off", "-md=32m", "-mhc=off", "-mmt=on"]


def python_filters() -> list[dict]:
    import py7zr

    return [{"id": py7zr.FILTER_LZMA2, "dict_size": 32 * 1024 * 1024, "preset": 7}]


def file_hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.relative_to(root).as_posix()] = digest.hexdigest()
    return result


def verify_archive(archive: Path, source: Path, extra: dict[str, Path] | None = None) -> None:
    """Reject incompatible methods and incomplete payloads before publication."""
    import py7zr

    expected = file_hashes(source)
    for name, path in (extra or {}).items():
        expected[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    with py7zr.SevenZipFile(archive, "r") as reader:
        methods = reader.archiveinfo().method_names
        if set(methods) - {"LZMA2", "COPY"}:
            raise RuntimeError(f"Incompatible release codecs in {archive.name}: {methods}")
        with tempfile.TemporaryDirectory(prefix="fluentytdl_verify_") as tmp:
            reader.extractall(tmp)
            if file_hashes(Path(tmp)) != expected:
                raise RuntimeError(f"Release archive content/hash mismatch: {archive.name}")
    reports = Path(
        os.environ.get(
            "FLUENTYTDL_BUILD_REPORTS",
            Path(__file__).resolve().parents[1] / "build" / "archive-reports",
        )
    )
    reports.mkdir(parents=True, exist_ok=True)
    (reports / f"{archive.name}.json").write_text(
        json.dumps(
            {
                "archive": archive.name,
                "decoder": f"py7zr {py7zr.__version__}",
                "methods": methods,
                "files": expected,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"✓ Archive verified with py7zr: {archive.name} ({len(expected)} files, {methods})")


def verify_frozen_updater(archive: Path, updater: Path) -> None:
    """Exercise the shipped executable with no system 7z, compare both decoders."""
    import os

    import py7zr

    with tempfile.TemporaryDirectory(prefix="fluentytdl_smoke_") as tmp:
        reference, actual = Path(tmp) / "reference", Path(tmp) / "中文 path (1)"
        with py7zr.SevenZipFile(archive, "r") as reader:
            reader.extractall(reference)
        env = dict(os.environ, PATH="")
        subprocess.run(
            [str(updater.resolve()), "--verify-archive", str(archive.resolve()), str(actual)],
            check=True,
            env=env,
            timeout=600,
        )
        if file_hashes(reference) != file_hashes(actual):
            raise RuntimeError("Frozen updater extraction differs from legacy decoder")
    print(f"✓ Frozen updater verified: {archive.name}")
    report = (
        Path(
            os.environ.get(
                "FLUENTYTDL_BUILD_REPORTS",
                Path(__file__).resolve().parents[1] / "build" / "archive-reports",
            )
        )
        / f"{archive.name}.json"
    )
    if report.is_file():
        data = json.loads(report.read_text(encoding="utf-8"))
        data["frozen_updater"] = "passed (bundled decoder required, empty PATH, Unicode path)"
        report.write_text(json.dumps(data, indent=2), encoding="utf-8")
