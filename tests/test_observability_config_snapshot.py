"""生效配置快照与变更追踪（`kind=config`），以及它接进启动路径的那一段。

**为什么这份测试必须存在。** 快照是每个会话的**第一条** Observability 事件 —— 用户贴上来
的日志开头，"当时 `subtitle_enabled` 是真是假、代理走的是哪一档"全靠它。而这条链路上
每一层都按硬规则 5 裹了 try/except：`config_manager.set()` 的 import、
`emit_config_change()` 自己、`startup_info._log_config_snapshot()`、
`_install_observability_sinks()`。任何一处签名漂移或键名笔误都**不会报错**，只会退化成
一行 `logger.debug`，然后 JSONL 里从此再没有配置快照 —— 正是这套层要消灭的静默退化。

钉住四件事：

1. **56 个快照键都是真键** —— `ConfigManager.DEFAULT_CONFIG` 里查不到的键，`get()` 静默
   返回 `None`，快照行就永远显示 `subtitle_enabled=None`；
2. **脱敏是硬要求** —— 快照里躺着 `proxy_url`（可能含账号密码）、`download_dir`
   （含 Windows 用户名）、`youtube_po_token`（凭据本体），而日志是用户会直接贴进
   GitHub Issue 的东西；
3. **`config_manager.set()` 真的会落 change 事件**，且信号回灌那种"写回同一个值"要丢掉；
4. **启动路径真的调了它们**，顺序是先清扫后装 sink（反了的话清扫会去碰 JSONL sink
   刚打开的句柄 —— Windows 上那是删不掉的文件，每次启动都在同一处失败）。
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault(
    "FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fluentytdl-cfgsnap-test-")
)

from loguru import logger  # noqa: E402

from fluentytdl.observability.config_snapshot import (  # noqa: E402
    CONFIG_SNAPSHOT_KEYS,
    WATCHED_CONFIG_KEYS,
    emit_config_change,
    emit_config_snapshot,
    sanitize_config_value,
    snapshot_fields,
)
from fluentytdl.observability.sanitize import MASK  # noqa: E402


@pytest.fixture
def events():
    """只收 Observability Event，不含纯文本日志行。"""
    captured: list[dict] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"]["fytdl"])),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _getter(values: dict):
    """`config_manager.get` 的替身。

    传 callable 而不是实例是刻意的：`observability` 不许依赖 `core`
    （`config_manager.set()` 反过来要调本模块，互相 import 就成环）。
    """
    return lambda key, default=None: values.get(key, default)


# ── 键集本身 ────────────────────────────────────────────────


def test_every_snapshot_key_exists_in_the_real_config():
    """笔误的键名不会报错，只会让那一行永远是 `None`。

    这是整份快照唯一会静默失效的方式：`ConfigManager.get()` 是
    `self.config.get(key, default)`，查不到就给 `None`，而 `config_manager` 会把
    `DEFAULT_CONFIG` 之外的键当过时键剪掉（`config_manager.py:214`）。
    """
    from fluentytdl.core.config_manager import ConfigManager

    missing = [k for k in CONFIG_SNAPSHOT_KEYS if k not in ConfigManager.DEFAULT_CONFIG]
    assert missing == [], missing


def test_snapshot_keys_are_unique_and_ordered():
    """有序是刻意的 —— 两次启动的快照行要能直接 diff，dict 的偶然顺序做不到。"""
    assert isinstance(CONFIG_SNAPSHOT_KEYS, tuple)
    assert len(set(CONFIG_SNAPSHOT_KEYS)) == len(CONFIG_SNAPSHOT_KEYS)


def test_watched_keys_are_the_same_set_as_the_snapshot():
    """ "启动时值得记"和"改动时值得记"是同一个判断，分成两份必然漂移。"""
    assert WATCHED_CONFIG_KEYS == frozenset(CONFIG_SNAPSHOT_KEYS)


def test_privacy_sensitive_keys_are_all_covered():
    """这三类值一旦原样落盘就是泄漏，而日志是用户会直接贴进 Issue 的东西。"""
    fields = snapshot_fields(
        _getter(
            {
                "proxy_url": "socks5://alice:s3cret@10.0.0.1:1080",
                "download_dir": str(Path.home() / "Videos" / "FluentYTDL"),
                "youtube_po_token": "MnRfXXXXXXXXXXXXXXXXXX",
            }
        )
    )
    blob = repr(fields)
    assert "alice" not in blob and "s3cret" not in blob
    assert Path.home().name not in blob
    assert "MnRf" not in blob


# ── 脱敏 ────────────────────────────────────────────────────


def test_token_records_only_whether_it_is_set():
    """凭据本体绝不落盘，但"有没有设"本身是排查 403 的前提。"""
    assert sanitize_config_value("youtube_po_token", "MnRf...") == MASK
    assert sanitize_config_value("youtube_po_token", "") == ""


def test_proxy_keeps_host_and_port_but_drops_credentials():
    """host:port 对排查有用（"挂的是哪个代理"），账号密码没有。"""
    out = sanitize_config_value("proxy_url", "socks5://alice:s3cret@10.0.0.1:1080")
    assert "10.0.0.1" in out and "1080" in out
    assert "alice" not in out and "s3cret" not in out


def test_empty_proxy_stays_empty_instead_of_becoming_a_mask():
    """没设代理和"设了但涂掉了"必须一眼可分。"""
    assert sanitize_config_value("proxy_url", "") == ""


def test_path_folds_home_but_keeps_structure():
    """盘符与目录结构对排查有用，泄漏的只有用户名那一段。"""
    out = sanitize_config_value("download_dir", str(Path.home() / "Videos"))
    assert Path.home().name not in out
    assert "Videos" in out


@pytest.mark.parametrize("value", [True, False, 0, 3, "vtt", ["zh-Hans", "en"], None])
def test_plain_values_pass_through_untouched(value):
    """其余键本来就是布尔/数字/枚举，涂它们只会毁掉可读性。"""
    assert sanitize_config_value("subtitle_enabled", value) == value


# ── 快照 ────────────────────────────────────────────────────


def test_snapshot_covers_all_keys_even_when_unset(events):
    """缺键也要出现在快照里 —— "这个键当时没值"本身就是线索。"""
    emit_config_snapshot(_getter({}))
    event = next(e for e in events if e.get("scope") == "snapshot")
    for key in CONFIG_SNAPSHOT_KEYS:
        assert key in event, key


def test_snapshot_is_one_event_at_startup_stage(events):
    """固定 `stage=startup`：`stage=startup kind=config` 要能直接 grep。

    `Stage` 是封闭集合，里面没有"设置页"这一档，也不该为一条日志给它开一个 ——
    那个枚举描述的是下载流水线的位置。
    """
    emit_config_snapshot(_getter({"subtitle_enabled": True}))
    snapshots = [e for e in events if e.get("kind") == "config" and e.get("scope") == "snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0]["stage"] == "startup"
    assert snapshots[0]["subtitle_enabled"] is True


def test_one_unreadable_key_does_not_kill_the_whole_snapshot(events):
    """剩下 55 个键仍然有排查价值，不能因为一个键抛异常就整份消失。"""

    def hostile(key, default=None):
        if key == "proxy_mode":
            raise RuntimeError("boom")
        return "ok"

    emit_config_snapshot(hostile)
    event = next(e for e in events if e.get("scope") == "snapshot")
    assert event["proxy_mode"] == "<unreadable>"
    assert event["subtitle_enabled"] == "ok"


def test_snapshot_never_raises_into_the_caller():
    """硬规则 5：启动日志再有价值也不该拦住启动。"""
    emit_config_snapshot(None)  # type: ignore[arg-type]


def test_snapshot_is_json_safe(events):
    """文本 sink 打得出来、JSONL sink 却 TypeError，是硬规则 5 在最坏时刻失效。"""
    import json

    emit_config_snapshot(_getter({"download_dir": Path.home() / "Videos"}))
    event = next(e for e in events if e.get("scope") == "snapshot")
    json.dumps(event)  # 不抛即通过


# ── 变更 ────────────────────────────────────────────────────


def test_change_records_old_and_new(events):
    emit_config_change("subtitle_enabled", False, True)
    event = next(e for e in events if e.get("scope") == "change")
    assert (event["key"], event["old"], event["new"]) == ("subtitle_enabled", False, True)
    assert event["stage"] == "startup"


def test_change_ignores_unwatched_keys(events):
    """主题色、欢迎页看过没有这类键进来只会稀释信息。"""
    emit_config_change("theme_color", "#000", "#fff")
    assert not [e for e in events if e.get("scope") == "change"]


def test_change_ignores_writing_back_the_same_value(events):
    """设置页初始化时的信号回灌会写回大量当前值。

    不过滤掉，打开一次设置页就能刷出几十条毫无信息量的 change 事件。
    """
    emit_config_change("rate_limit", "5M", "5M")
    assert not [e for e in events if e.get("scope") == "change"]


def test_change_sanitizes_both_sides(events):
    """old 也要脱敏 —— 换代理那一刻，旧凭据同样会落盘。"""
    emit_config_change(
        "proxy_url",
        "socks5://alice:s3cret@10.0.0.1:1080",
        "http://127.0.0.1:7890",
    )
    event = next(e for e in events if e.get("scope") == "change")
    assert "alice" not in str(event["old"]) and "s3cret" not in str(event["old"])
    assert "127.0.0.1" in str(event["new"])


def test_change_never_raises_into_the_caller():
    """脱敏和 `old == new` 跑在 `config_manager.set()` 的栈帧里，
    `emit_event` 的 try/except 兜不到 —— 而保存设置显然是业务。"""

    class Hostile:
        def __eq__(self, other):
            raise RuntimeError("boom")

    emit_config_change("rate_limit", Hostile(), Hostile())


def test_config_manager_set_actually_emits_a_change(events, tmp_path, monkeypatch):
    """真走一遍 `config_manager.set()` —— 那句 import 在它自己的 try 之外。

    这是唯一能发现"函数级 import 写错了路径"的测试：写错了不会报错，
    只会让每一次改设置都静默跳过记录。
    """
    from fluentytdl.core.config_manager import ConfigManager

    cfg = ConfigManager()
    monkeypatch.setattr(cfg, "config_file", str(tmp_path / "config.json"), raising=False)
    monkeypatch.setattr(cfg, "save", lambda: None, raising=False)

    cfg.config["subtitle_enabled"] = False
    cfg.set("subtitle_enabled", True)

    event = next(e for e in events if e.get("scope") == "change")
    assert (event["key"], event["old"], event["new"]) == ("subtitle_enabled", False, True)


# ── 启动路径 ────────────────────────────────────────────────


def test_startup_sweeps_before_installing_sinks(monkeypatch):
    """顺序反了的话，清扫会去碰 JSONL sink 刚打开的句柄。

    Windows 上那是一个删不掉的文件 —— 清扫会在每次启动的同一处失败，而它整段裹了
    try/except，所以永远不会有人发现 trace 目录再也没被清过。
    """
    import fluentytdl.observability as obs
    from fluentytdl.utils import startup_info

    calls: list[str] = []
    monkeypatch.setattr(obs, "sweep_trace_dir", lambda: calls.append("sweep"))
    monkeypatch.setattr(obs, "install_sinks", lambda: calls.append("install"))

    startup_info._install_observability_sinks()
    assert calls == ["sweep", "install"]


def test_startup_sink_failure_does_not_stop_startup(monkeypatch):
    """硬规则 5：日志系统装不上也不该拦住启动。"""
    import fluentytdl.observability as obs
    from fluentytdl.utils import startup_info

    def boom():
        raise RuntimeError("no disk")

    monkeypatch.setattr(obs, "sweep_trace_dir", boom)
    startup_info._install_observability_sinks()


def test_startup_emits_the_config_snapshot(events):
    """启动路径真的会落那一条快照 —— 它是 bug 包的第一份材料。"""
    from fluentytdl.utils import startup_info

    startup_info._log_config_snapshot()
    snapshots = [e for e in events if e.get("kind") == "config" and e.get("scope") == "snapshot"]
    assert len(snapshots) == 1
    # 真实单例读出来的值，不是替身 —— 键名对不上会在这里现形
    assert "subtitle_enabled" in snapshots[0]


def test_startup_snapshot_failure_does_not_stop_startup(monkeypatch):
    from fluentytdl.observability import config_snapshot as cs_mod
    from fluentytdl.utils import startup_info

    def boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(cs_mod, "emit_config_snapshot", boom)
    startup_info._log_config_snapshot()
