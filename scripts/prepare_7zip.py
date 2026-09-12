"""Fetch the latest official standalone 7-Zip; never use a historical pin."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fetch_tools import sha256_file

CACHE = Path(__file__).resolve().parents[1] / "build" / "tools" / "7zip"


def prepare(cache: Path = CACHE, *, allow_environment: bool = True) -> Path:
    provided = os.environ.get("FLUENTYTDL_7ZIP_DIR") if allow_environment else None
    if provided:
        directory = Path(provided)
        metadata = json.loads((directory / "version.json").read_text(encoding="utf-8"))
        if sha256_file(directory / "7za.exe") != metadata["sha256"]:
            raise RuntimeError("Snapshot 7-Zip console hash mismatch")
        if not (directory / "License.txt").is_file():
            raise RuntimeError("Snapshot 7-Zip license missing")
        return directory

    from component_snapshot import ReleaseSource

    source = ReleaseSource("ip7z/7zip")
    extra = [name for name in source.assets if re.fullmatch(r"7z\d+-extra\.7z", name)]
    if len(extra) != 1 or "7zr.exe" not in source.assets:
        raise RuntimeError("Latest official 7-Zip release has no unique Extra/bootstrap assets")
    cache.mkdir(parents=True, exist_ok=True)
    source.download("https://github.com/7zr.exe", cache / "7zr.exe")
    source.download("https://github.com/" + extra[0], cache / "extra.7z")
    with tempfile.TemporaryDirectory(prefix="7zip_", dir=cache) as tmp:
        subprocess.run(
            [
                str((cache / "7zr.exe").resolve()),
                "x",
                str((cache / "extra.7z").resolve()),
                f"-o{tmp}",
                "-y",
            ],
            check=True,
            capture_output=True,
            timeout=120,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for source_path, name in (
            (Path(tmp) / "x64/7za.exe", "7za.exe"),
            (Path(tmp) / "License.txt", "License.txt"),
        ):
            if not source_path.is_file():
                raise RuntimeError(f"Latest 7-Zip payload missing {name}")
            shutil.copy2(source_path, cache / name)
    subprocess.run(
        [str((cache / "7za.exe").resolve()), "i"], check=True, capture_output=True, timeout=30
    )
    metadata = {
        **source.metadata(),
        "version": source.release["tag_name"],
        "sha256": sha256_file(cache / "7za.exe"),
    }
    (cache / "version.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return cache


if __name__ == "__main__":
    print(prepare(allow_environment=False))
