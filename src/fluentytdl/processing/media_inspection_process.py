"""Bounded, cancellable direct-child execution for the media inspector."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable

from ..utils.paths import get_clean_env


class InspectionError(Exception):
    pass


class InspectionCancelled(Exception):
    pass


def run_reader(
    argv: list[str],
    cancel: Callable[[], bool],
    *,
    request: bytes = b"",
    timeout: float = 60,
    limit: int = 8 * 1024 * 1024,
) -> bytes:
    if cancel():
        raise InspectionCancelled()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=get_clean_env(),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except OSError as exc:
        raise InspectionError("tool_unavailable") from exc
    overflow = threading.Event()
    output = bytearray()

    def drain(stream, budget, capture):
        total = 0
        try:
            while block := stream.read(16384):
                total += len(block)
                if total > budget:
                    overflow.set()
                elif capture:
                    output.extend(block)
        except (OSError, ValueError):
            pass

    def send():
        try:
            proc.stdin.write(request)
            proc.stdin.close()
        except (OSError, ValueError):
            pass

    threads = [
        threading.Thread(target=drain, args=(proc.stdout, limit, True), daemon=True),
        threading.Thread(target=drain, args=(proc.stderr, 65536, False), daemon=True),
        threading.Thread(target=send, daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel():
                raise InspectionCancelled()
            if overflow.is_set():
                raise InspectionError("output_limit")
            if time.monotonic() >= deadline:
                raise InspectionError("reader_timeout")
            if proc.poll() is not None:
                break
            time.sleep(0.03)
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            raise InspectionError("reader_timeout")
        if overflow.is_set():
            raise InspectionError("output_limit")
        if proc.returncode:
            raise InspectionError("reader_failed")
        return bytes(output)
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)
        for thread in threads:
            thread.join(timeout=1)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            stream.close()
