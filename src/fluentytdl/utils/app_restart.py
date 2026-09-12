"""Restart after normal shutdown; the successor waits before touching app data."""

from __future__ import annotations

import os
import subprocess
import sys
import time


def wait_for_parent_exit(pid: int, timeout: float = 60) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 87  # Process already exited.
        try:
            return kernel.WaitForSingleObject(handle, int(timeout * 1000)) == 0
        finally:
            kernel.CloseHandle(handle)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def request_restart(window) -> None:
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    app.setProperty("restart_requested", True)
    # Use the same worker/database/theme-listener shutdown as explicit Quit.
    window.quit_app()


def launch_pending_restart(app) -> None:
    if not app.property("restart_requested"):
        return
    args = sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv
    command = [sys.executable, *args, "--restart-parent-pid", str(os.getpid())]
    subprocess.Popen(command)
    app.setProperty("restart_requested", False)
