"""POT 预热退避（`ensure_warm_async` / `_schedule_warm_retry`）的确定性回归。

对应"断网 → 恢复"这条人工用例的可自动化部分：断网期间预热必然失败，此时
既不能永久放弃（否则网络恢复后 POT 再也起不来），也不能每次解析都重试（否则
每解析一次就白起一次进程、卡一次 20s 的 verify_token_generation）。

用假时钟推进 monotonic，不 sleep、不触网、不起子进程。
"""

import importlib
import sys
import threading
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# 必须走 importlib：`fluentytdl.youtube` 把 `pot_manager` 这个名字重导出成了
# 单例*实例*，`from ... import pot_manager` 拿到的是对象而不是模块。
pot_mod = importlib.import_module("fluentytdl.youtube.pot_manager")
POTManager = pot_mod.POTManager


@pytest.fixture
def mgr():
    """裸实例：绕开 __init__ 里的 atexit 注册与 Job Object 创建。"""
    m = POTManager.__new__(POTManager)
    m._lock = threading.Lock()
    m._warm_event = threading.Event()
    m._warm_thread = None
    m._warm_attempts = 0
    m._warm_retry_at = 0.0
    m._active_port = 0
    m._is_running = False
    m._process = None
    m._last_token_len = 0
    m._last_minter_size = None
    return m


@pytest.fixture
def clock(monkeypatch):
    """可手动推进的 monotonic 时钟。"""

    class Clock:
        def __init__(self):
            self.now = 1000.0

        def advance(self, dt):
            self.now += dt

    c = Clock()
    monkeypatch.setattr(pot_mod.time, "monotonic", lambda: c.now)
    return c


def test_backoff_schedule_escalates_then_plateaus(mgr, clock):
    """退避必须递增到上限后封顶，而不是无限增长或永远停在 30s。"""
    expected = [30.0, 120.0, 300.0, 300.0, 300.0]
    for i, delay in enumerate(expected, start=1):
        base = clock.now
        mgr._schedule_warm_retry("server_down")
        assert mgr._warm_attempts == i
        assert mgr._warm_retry_at == pytest.approx(base + delay), f"第 {i} 次退避应为 {delay}s"
        clock.advance(delay)


def test_no_retry_inside_backoff_window(mgr, clock, monkeypatch):
    """退避窗口内绝不能起新预热线程 —— 否则每次解析都白起一次进程。"""
    spawned = _stub_thread(monkeypatch)

    mgr._schedule_warm_retry("server_down")  # 30s 窗口
    clock.advance(29.0)
    mgr.ensure_warm_async()
    assert not spawned, "退避未到期就重试了"

    clock.advance(2.0)  # 越过 30s
    mgr.ensure_warm_async()
    assert len(spawned) == 1, "退避到期后必须允许重试"


def test_warm_success_resets_attempts(mgr, clock, monkeypatch):
    """一次成功必须把计数清零，否则网络恢复后仍背着上一轮的 5 分钟退避。"""
    mgr._schedule_warm_retry("server_down")
    mgr._schedule_warm_retry("token_fail")
    assert mgr._warm_attempts == 2

    monkeypatch.setattr(mgr, "is_running", lambda: True)
    monkeypatch.setattr(mgr, "verify_token_generation", lambda timeout=20.0: (True, "ok"))
    monkeypatch.setattr(mgr, "verify_plugin_loadable", lambda: (True, "ok"))
    monkeypatch.setattr(mgr, "check_minter_health", lambda timeout=3.0: (True, "ok"))
    monkeypatch.setattr(POTManager, "_probe_deno", staticmethod(lambda: True))

    mgr._warm_worker()
    assert mgr._warm_attempts == 0


def test_already_warm_short_circuits(mgr, clock, monkeypatch):
    """已经 warm 就不该再起线程。"""
    spawned = _stub_thread(monkeypatch)
    mgr._warm_event.set()
    mgr.ensure_warm_async()
    assert not spawned


def test_status_brief_reports_remaining_backoff(mgr, clock, monkeypatch):
    """退避期间状态摘要要说清"还剩多久"，否则用户只看到"未运行"会以为坏了。"""
    _enable_pot(monkeypatch)
    monkeypatch.setattr(mgr, "is_running", lambda: False)

    mgr._schedule_warm_retry("server_down")
    clock.advance(10.0)
    brief = mgr.status_brief()
    assert "20s" in brief and "1" in brief, brief


def test_status_brief_backoff_wins_over_running(mgr, clock, monkeypatch):
    """断网这条真实路径：服务是本地进程、起得来，只有铸 Token 失败。

    此时 is_running()=True 而预热线程早已退出。若摘要先看 running 就会一直显示
    "预热中…"，把"正在退避、当前没人干活"说成"马上就好" —— 断网实测时用户在
    UI 上就完全看不出退避在跑。
    """
    _enable_pot(monkeypatch)
    monkeypatch.setattr(mgr, "is_running", lambda: True)
    mgr._active_port = 4416

    mgr._schedule_warm_retry("token_fail")
    clock.advance(5.0)
    brief = mgr.status_brief()
    assert "25s" in brief, brief
    assert "预热中" not in brief, f"退避中却报预热中: {brief}"


def test_status_brief_warm_ignores_stale_retry_at(mgr, clock, monkeypatch):
    """已 warm 就是已就绪 —— 上一轮失败留下的 _warm_retry_at 不该把它盖掉。"""
    _enable_pot(monkeypatch)
    monkeypatch.setattr(mgr, "is_running", lambda: True)
    mgr._active_port = 4416
    mgr._last_token_len = 232

    mgr._schedule_warm_retry("token_fail")
    mgr._warm_event.set()
    brief = mgr.status_brief()
    assert "已就绪" in brief and "232" in brief, brief


def _enable_pot(monkeypatch) -> None:
    from fluentytdl.core.config_manager import config_manager

    monkeypatch.setitem(config_manager.config, "pot_provider_enabled", True)


class _NoopThread:
    def start(self):
        pass

    def is_alive(self):
        return False


def _stub_thread(monkeypatch) -> list[dict]:
    """把 `pot_manager` 模块看到的 threading 换成壳，记录起线程的意图。

    只替换模块属性，不动全局 `threading.Thread` —— 后者会连累测试进程里
    其他正在起的线程。Lock/Event 原样透传，模块里别处还要用。
    """
    spawned: list[dict] = []

    class _Shim:
        Lock = threading.Lock
        Event = threading.Event

        @staticmethod
        def Thread(**kw):  # noqa: N802 - 镜像 threading 的命名
            spawned.append(kw)
            return _NoopThread()

    monkeypatch.setattr(pot_mod, "threading", _Shim)
    return spawned
