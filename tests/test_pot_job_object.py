"""POT 进程纳管的回归点：Job Object 必须是匿名的（Windows）。

这条曾经真出过问题：job 名字写死成 "FluentYTDL_POT_Job"，而 CreateJobObject
遇到已存在的名字会返回*现有* job 的句柄，KILL_ON_JOB_CLOSE 只在最后一个句柄
关闭时才触发。于是两个实例同时跑时，A 被强杀后 B 仍持有句柄 → A 的 POT 服务
变成孤儿进程活下来。

修的时候还踩了第二个坑：名字传 None，pywin32 直接抛
"None is not a valid string in this context"，`_job_handle` 变成 None，
子进程根本没被纳管 —— 比具名版更糟且完全静默。所以必须传空字符串。

不起 POT 服务、不触网，只验 job 的创建方式与标志位。
"""

import sys
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 是 Windows 专有机制")

win32job = pytest.importorskip("win32job")

from fluentytdl.youtube.pot_manager import POTManager  # noqa: E402


def test_job_object_is_anonymous(monkeypatch):
    """必须匿名建 job，且名字是空字符串而不是 None。"""
    calls: list[tuple] = []
    real_create = win32job.CreateJobObject

    def spy(sa, name):
        calls.append((sa, name))
        return real_create(sa, name)

    monkeypatch.setattr(win32job, "CreateJobObject", spy)

    mgr = POTManager.__new__(POTManager)
    mgr._setup_job_object()

    assert calls, "没有调用 CreateJobObject —— job 根本没建"
    _sa, name = calls[0]
    assert name is not None, "名字传 None 会让 pywin32 抛异常，子进程将完全不受纳管"
    assert name == "", f"job 必须匿名，实际名字={name!r}（具名会被跨进程共享）"
    assert mgr._job_handle is not None, "建 job 失败会静默退化成不纳管子进程"


def test_job_object_kills_children_on_close():
    """KILL_ON_JOB_CLOSE 必须置位，否则父进程被强杀时 POT 服务会残留。"""
    mgr = POTManager.__new__(POTManager)
    mgr._setup_job_object()
    assert mgr._job_handle is not None

    info = win32job.QueryInformationJobObject(
        mgr._job_handle, win32job.JobObjectExtendedLimitInformation
    )
    flags = info["BasicLimitInformation"]["LimitFlags"]
    assert flags & win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


def test_setup_job_object_never_raises(monkeypatch):
    """建 job 失败只能降级为 None，绝不能把异常抛给 __init__ —— 那会让整个单例炸掉。"""

    def boom(*_a, **_kw):
        raise OSError("simulated failure")

    monkeypatch.setattr(win32job, "CreateJobObject", boom)

    mgr = POTManager.__new__(POTManager)
    mgr._setup_job_object()
    assert mgr._job_handle is None
