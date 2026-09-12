import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fluentytdl.utils import app_restart


def test_wait_does_not_accept_running_parent():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"])
    try:
        assert not app_restart.wait_for_parent_exit(child.pid, timeout=0.02)
    finally:
        child.terminate()
        child.wait(timeout=5)
    assert app_restart.wait_for_parent_exit(child.pid, timeout=1)


def test_wait_rejects_self_and_invalid_pid():
    assert not app_restart.wait_for_parent_exit(os.getpid(), timeout=0)
    assert not app_restart.wait_for_parent_exit(-1, timeout=0)


def test_wait_blocks_until_real_process_exits():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"])
    try:
        assert app_restart.wait_for_parent_exit(child.pid, timeout=5)
        assert child.poll() == 0
    finally:
        child.wait(timeout=5)


def test_restart_is_deferred_and_preserves_arguments(monkeypatch):
    calls = []
    state = {"restart_requested": False}
    app = SimpleNamespace(property=state.get, setProperty=state.__setitem__)
    monkeypatch.setattr(app_restart.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["C:/app with spaces/main.py", "--custom", "中文路径"])
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    app_restart.launch_pending_restart(app)
    assert not calls
    state["restart_requested"] = True
    app_restart.launch_pending_restart(app)
    assert calls == [[sys.executable, *sys.argv, "--restart-parent-pid", str(os.getpid())]]
    assert not state["restart_requested"]
    app_restart.launch_pending_restart(app)
    assert len(calls) == 1


def test_frozen_restart_does_not_pass_executable_twice(monkeypatch):
    calls = []
    state = {"restart_requested": True}
    app = SimpleNamespace(property=state.get, setProperty=state.__setitem__)
    monkeypatch.setattr(app_restart.subprocess, "Popen", lambda args: calls.append(args))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", ["FluentYTDL.exe", "--admin-mode"])
    app_restart.launch_pending_restart(app)
    assert calls[0] == [sys.executable, "--admin-mode", "--restart-parent-pid", str(os.getpid())]
