"""Quiet, cancellable tool execution. Tool output never enters application logs."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable

from ..utils.paths import get_clean_env


class MetadataError(Exception):
    """A stable, non-localized failure code (never a raw tool error)."""


class MetadataCancelled(Exception):
    pass


def run_tool(argv: list[str], cancel: Callable[[], bool], *, timeout: float = 180) -> str:
    if cancel():
        raise MetadataCancelled()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            env=get_clean_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        raise MetadataError("tool_unavailable") from exc
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel():
                raise MetadataCancelled()
            if time.monotonic() >= deadline:
                raise MetadataError("tool_timeout")
            try:
                output, _ = process.communicate(timeout=0.15)
                if process.returncode:
                    raise MetadataError("tool_failed")
                return output
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
