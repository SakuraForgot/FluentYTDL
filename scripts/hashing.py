"""Single canonical SHA256 helpers for the build/release scripts.

Every script under ``scripts/`` that hashes a file or a byte string funnels
through here. Before this module the same streaming loop was copy-pasted five
times (build.py, fetch_tools.py, generate_manifest.py, archive_compat.py) with
two different chunk sizes; the digest is identical regardless of chunk size, so
consolidating them changes no output — only the number of places a bug could
hide.

Deliberately NOT covered: ``build.py::source_fingerprint()`` builds an
*incremental* digest (git diff + commit + untracked bytes), which is a
different construction, not a file/bytes hash. It keeps its own ``hashlib`` use.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024


def sha256_file(file_path: Path) -> str:
    """Streaming SHA256 of a file's contents (lowercase hex)."""
    digest = hashlib.sha256()
    with open(file_path, "rb") as stream:
        for chunk in iter(lambda: stream.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    """SHA256 of an in-memory byte string (lowercase hex)."""
    return hashlib.sha256(data).hexdigest()
