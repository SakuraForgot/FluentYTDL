"""Bound the lifetime of an owned Windows CLI and its bootstrap children."""

import sys


def attach_job(process):
    if sys.platform != "win32" or not isinstance(getattr(process, "pid", None), int):
        return
    job = None
    try:
        import win32api
        import win32con
        import win32job

        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        handle = win32api.OpenProcess(win32con.PROCESS_ALL_ACCESS, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, handle)
        finally:
            handle.Close()
        process._owned_job = job
    except Exception as error:
        if job is not None:
            job.Close()
        from loguru import logger

        logger.warning("CLI process job unavailable: {}", type(error).__name__)


def close_job(process):
    job = getattr(process, "_owned_job", None)
    if job is not None:
        process._owned_job = None
        job.Close()
