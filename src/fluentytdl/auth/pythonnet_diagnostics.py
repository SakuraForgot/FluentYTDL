"""Best-effort, read-only evidence for the isolated login process."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import struct
import sys
from pathlib import Path


def runtime_evidence() -> dict:
    result = {"bits": struct.calcsize("P") * 8, "frozen": bool(getattr(sys, "frozen", False))}
    for package in ("pythonnet", "clr-loader", "pywebview"):
        try:
            result[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            result[package] = "unknown"
    files = {}
    for module, relative in (
        ("pythonnet", "runtime/Python.Runtime.dll"),
        ("clr_loader", f"ffi/dlls/{'amd64' if result['bits'] == 64 else 'x86'}/ClrLoader.dll"),
        (
            "webview",
            f"lib/runtimes/win-{'x64' if result['bits'] == 64 else 'x86'}/native/WebView2Loader.dll",
        ),
    ):
        try:
            spec = importlib.util.find_spec(module)
            if spec is None or spec.origin is None:
                files[module] = {"exists": False}
                continue
            path = Path(spec.origin).parent / relative
            info = {"exists": path.is_file()}
            if info["exists"]:
                info["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                info["zone_identifier"] = Path(str(path) + ":Zone.Identifier").is_file()
            files[module] = info
        except Exception as exc:
            files[module] = {"probe_error": type(exc).__name__}
    result["files"] = files
    return result
