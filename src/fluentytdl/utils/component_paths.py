"""Managed component locations, shared by execution and installation.

Never install into a configured or PATH executable. An unwritable application
directory has one per-installation fallback, used by readers as well as writers.
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from . import paths


def component_roots() -> tuple[Path, Path]:
    application = paths.frozen_app_dir() if paths.is_frozen() else paths.project_root()
    primary = application / ("bin" if paths.is_frozen() else "assets/bin")
    identity = hashlib.sha256(str(application.resolve()).casefold().encode()).hexdigest()[:16]
    fallback = paths.user_data_dir() / "components" / identity
    return primary, fallback


def managed_candidates(key: str, filename: str) -> tuple[Path, ...]:
    primary, fallback = component_roots()
    # Once a fallback install exists, it must not lose precedence after elevation.
    return fallback / key / filename, primary / key / filename


def component_target(key: str) -> Path:
    primary, fallback = component_roots()
    if (fallback / key).exists():
        return fallback / key
    target = primary / key
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=target):
            pass
        return target
    except OSError:
        target = fallback / key
        target.mkdir(parents=True, exist_ok=True)
        return target
