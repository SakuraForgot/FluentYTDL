"""Owned updater/probe process trees end without touching unrelated processes."""

import subprocess
import sys
import time

import pytest

from fluentytdl.utils.owned_process import attach_job, close_job

pytestmark = [
    pytest.mark.windows_only,
    pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object"),
]


def test_job_closes_cli_bootstrap_children(tmp_path):
    psutil = pytest.importorskip("psutil")
    pytest.importorskip("win32job")
    identity = tmp_path / "child.pid"
    child = "import os,sys,time;open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(60)"
    parent = "import subprocess,sys,time;time.sleep(.15);subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2]]);time.sleep(60)"
    process = subprocess.Popen(
        [sys.executable, "-c", parent, child, str(identity)],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        attach_job(process)
        assert process._owned_job is not None
        deadline = time.monotonic() + 5
        while not identity.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        owned_child = psutil.Process(int(identity.read_text()))
        close_job(process)
        process.wait(timeout=3)
        owned_child.wait(timeout=3)
    finally:
        close_job(process)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
