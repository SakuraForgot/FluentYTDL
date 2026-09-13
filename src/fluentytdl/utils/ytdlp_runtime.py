"""yt-dlp executable identity. No application configuration or UI dependencies."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from .paths import find_bundled_executable, get_clean_env, is_frozen, locate_runtime_tool


@dataclass(frozen=True)
class RuntimeIdentity:
    path: Path | None
    source: str = ""


def resolve_runtime(
    configured: str = "", *, locate=None, legacy=None, frozen=None, which=None
) -> RuntimeIdentity:
    """Preserve execution priority: override, local/PATH lookup, frozen legacy, PATH."""
    locate = locate or locate_runtime_tool
    legacy = legacy or find_bundled_executable
    which = which or shutil.which
    frozen = is_frozen() if frozen is None else frozen
    if configured and Path(configured).exists():
        return RuntimeIdentity(Path(configured).resolve(), "custom")
    candidates = ("yt-dlp.exe", "yt-dlp/yt-dlp.exe", "yt_dlp/yt-dlp.exe")
    try:
        path = locate(*candidates)
    except FileNotFoundError:
        path = legacy(*candidates) if frozen else None
        if path is None:
            found = which("yt-dlp") or which("yt-dlp.exe")
            return (
                RuntimeIdentity(Path(found).resolve(), "path") if found else RuntimeIdentity(None)
            )
    path = Path(path).resolve()
    on_path = which("yt-dlp.exe") or which("yt-dlp")
    source = "path" if on_path and Path(on_path).resolve() == path else "bundled"
    return RuntimeIdentity(path, source)


@dataclass(frozen=True)
class RuntimeVersion:
    version: str = "unknown"
    channel: str = ""
    status: str = "unknown"


_lock = Lock()
_versions: dict[tuple, RuntimeVersion] = {}


def invalidate_version_cache() -> None:
    with _lock:
        _versions.clear()


def probe_version(path: Path | None) -> RuntimeVersion:
    """Read this executable's own identity, never substitute another binary/sidecar."""
    if path is None:
        return RuntimeVersion(status="missing")
    try:
        path = path.resolve()
        stat = path.stat()
        key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return RuntimeVersion(status="missing")
    with _lock:
        if key in _versions:
            return _versions[key]
        try:
            # No URL: reports version/debug identity without making extraction requests.
            result = subprocess.run(
                [str(path), "--ignore-config", "--verbose"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                env=get_clean_env(),
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            match = re.search(r"yt-dlp version ([\w-]+)@([^\s]+)", result.stdout + result.stderr)
            if not match:
                return RuntimeVersion()
            version = RuntimeVersion(match[2], match[1], "ok")
            _versions.clear()
            _versions[key] = version
            return version
        except subprocess.TimeoutExpired:
            return RuntimeVersion(status="timeout")
        except OSError:
            return RuntimeVersion(status="error")
