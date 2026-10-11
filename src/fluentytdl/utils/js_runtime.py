"""Deno identity for both the POT server and yt-dlp's JavaScript execution."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from .component_paths import managed_candidates
from .paths import find_bundled_executable, get_clean_env


@dataclass(frozen=True)
class DenoRuntime:
    path: Path | None
    source: str
    version: str = ""


_selection_lock = RLock()
_selection = None


def select_verified_deno(runtime: DenoRuntime, config) -> None:
    """Share the POT-verified choice with yt-dlp; never persist a custom override."""
    global _selection
    with _selection_lock:
        _selection = (
            runtime,
            str(config.get("js_runtime") or "auto"),
            str(config.get("js_runtime_path") or ""),
        )


def clear_deno_selection() -> None:
    global _selection
    with _selection_lock:
        _selection = None


def resolve_deno(config=None) -> DenoRuntime:
    if config is None:
        from ..core.config_manager import config_manager

        config = config_manager
    strategy = str(config.get("js_runtime") or "auto").lower()
    configured = str(config.get("js_runtime_path") or "").strip()
    if configured and strategy == "deno":
        return DenoRuntime(Path(configured), "custom")
    with _selection_lock:
        if _selection and _selection[1:] == (strategy, str(config.get("js_runtime_path") or "")):
            selected = _selection[0]
            if selected.path and selected.path.is_file():
                return selected
    for path in managed_candidates("deno", "deno.exe"):
        if path.is_file():
            return DenoRuntime(path, "managed")
    bundled = find_bundled_executable("deno/deno.exe", "deno.exe", "js/deno.exe")
    if bundled:
        return DenoRuntime(bundled, "bundled")
    system = shutil.which("deno")
    return DenoRuntime(Path(system) if system else None, "path" if system else "missing")


def probe_deno(runtime: DenoRuntime, timeout: float = 5) -> DenoRuntime:
    if runtime.path is None or not runtime.path.is_file():
        raise FileNotFoundError("pot_runtime_missing")
    result = subprocess.run(
        [str(runtime.path), "--version"],
        capture_output=True,
        timeout=timeout,
        env=get_clean_env(),
        stdin=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    match = re.search(rb"(?m)^deno (\d+\.\d+\.\d+)", result.stdout)
    if result.returncode or not match:
        raise ValueError("pot_runtime_invalid")
    return DenoRuntime(runtime.path, runtime.source, match[1].decode())
