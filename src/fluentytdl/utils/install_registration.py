"""Record an installation's custom data root without storing account information."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def register_data_root(app_dir: Path, data_dir: Path) -> None:
    """Best effort; the uninstaller verifies both registration and ownership marker."""
    try:
        app_dir, data_dir = app_dir.resolve(), data_dir.resolve()
        record = {"app_dir": str(app_dir), "data_dir": str(data_dir)}
        marker = data_dir / ".fluentytdl-data-owner.json"
        marker.write_text(json.dumps(record), encoding="utf-8")
        directory = Path(os.environ["LOCALAPPDATA"]) / "FluentYTDL/installations"
        directory.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(str(app_dir).casefold().encode()).hexdigest()
        temporary = directory / f"{key}.tmp"
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(directory / f"{key}.json")
        shared = app_dir / "bin/installations"
        shared.mkdir(parents=True, exist_ok=True)
        data_key = hashlib.sha256(str(data_dir).casefold().encode()).hexdigest()
        temporary = shared / f"{data_key}.tmp"
        temporary.write_text(json.dumps(record), encoding="utf-8")
        temporary.replace(shared / f"{data_key}.json")
    except (OSError, KeyError):
        pass
