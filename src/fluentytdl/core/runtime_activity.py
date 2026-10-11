"""Coordinate component changes with this application's CLI subprocesses only."""

from __future__ import annotations

import subprocess
import threading
import time
from contextlib import contextmanager

_condition = threading.Condition(threading.RLock())
_processes: set[subprocess.Popen] = set()
_changing = False
_readers = 0


class YtDlpLaunchCancelled(InterruptedError):
    """The request was cancelled before its CLI process could start."""


def check_launch_cancelled(cancel_check):
    if cancel_check is not None and cancel_check():
        raise YtDlpLaunchCancelled("ytdlp_cancelled")


def active_count() -> int:
    with _condition:
        _processes.difference_update(p for p in tuple(_processes) if p.poll() is not None)
        return len(_processes)


@contextmanager
def reading_components(cancel):
    """Keep a running provider's files stable without blocking other readers."""
    global _readers
    with _condition:
        while _changing:
            if cancel.is_set():
                raise InterruptedError("component_check_cancelled")
            _condition.wait(0.1)
        if cancel.is_set():
            raise InterruptedError("component_check_cancelled")
        _readers += 1
    try:
        yield
    finally:
        with _condition:
            _readers -= 1
            _condition.notify_all()


@contextmanager
def changing_components(cancel=None):
    global _changing
    with _condition:
        while _changing:
            if cancel is not None and cancel.is_set():
                raise InterruptedError("component_change_cancelled")
            _condition.wait(0.1)
        _changing = True
    try:
        while active_count() or _readers:
            if cancel is not None and cancel.wait(0.1):
                raise InterruptedError("component_change_cancelled")
            if cancel is None:
                time.sleep(0.1)
        if cancel is not None and cancel.is_set():
            raise InterruptedError("component_change_cancelled")
        yield
    finally:
        with _condition:
            _changing = False
            _condition.notify_all()


def ytdlp_popen(command, *, cancel_check=None, **kwargs):
    """Register before launch so a concurrent update cannot pass an empty check.

    Refresh the local POT endpoint after a queued component activation. The active
    service is allowed to change while a request waits to start, never mid-process.
    """
    command = list(command)
    needs_pot = any("youtubepot-bgutilhttp:base_url=" in str(arg) for arg in command)
    deadline = time.monotonic() + 90
    while True:
        check_launch_cancelled(cancel_check)
        with _condition:
            while _changing:
                check_launch_cancelled(cancel_check)
                if time.monotonic() >= deadline:
                    raise TimeoutError("component_change_wait_timeout")
                _condition.wait(0.1)
            if needs_pot:
                from ..youtube.pot_manager import pot_manager

                if not pot_manager.is_warm:
                    # Release the gate before shared readiness acquires it.
                    ready = False
                else:
                    ready = True
                    args = pot_manager.get_extractor_args()
                    from ..utils.js_runtime import resolve_deno
                    from ..youtube.yt_dlp_cli import prepare_yt_dlp_env, resolve_yt_dlp_exe

                    command[0] = str(resolve_yt_dlp_exe() or command[0])
                    for index, arg in enumerate(command):
                        if str(arg).startswith("youtubepot-bgutilhttp:base_url="):
                            command[index] = args
                        elif (
                            index
                            and command[index - 1] == "--js-runtimes"
                            and str(arg).startswith("deno")
                        ):
                            runtime = resolve_deno().path
                            if runtime:
                                command[index] = f"deno:{runtime}"
                    env = prepare_yt_dlp_env(command=command)
                    env.update(
                        {
                            k: v
                            for k, v in (kwargs.get("env") or {}).items()
                            if k not in {"PATH", "PYTHONPATH"}
                        }
                    )
                    kwargs["env"] = env
            else:
                ready = True
            if ready:
                check_launch_cancelled(cancel_check)
                process = subprocess.Popen(command, **kwargs)
                _processes.add(process)
                return process
        if time.monotonic() >= deadline or not pot_manager.wait_until_ready(
            timeout=min(45, max(0, deadline - time.monotonic())), cancel_check=cancel_check
        ):
            raise RuntimeError("pot_not_ready")


def ytdlp_run(command, *, capture_output=False, timeout=None, check=False, **kwargs):
    if capture_output:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    with ytdlp_popen(command, **kwargs) as process:
        try:
            output, errors = process.communicate(timeout=timeout)
        except BaseException:
            process.kill()
            process.wait()
            raise
        result = subprocess.CompletedProcess(command, process.returncode, output, errors)
        if check:
            result.check_returncode()
        return result
