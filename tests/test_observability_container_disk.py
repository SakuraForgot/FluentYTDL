"""容器改写与磁盘预检的观测契约。

两件事在命令行上都**看不出来**，而它们各自对应一类高频求助：

- 容器改写（`utils/container_compat.py`）—— "我明明选了 MP4，出来的却是 MKV"。
  `ensure_*` 是原地改写，改完之后 argv 里只剩改后的值，原值无处可查。
- 磁盘预检（`download/executor.py`）—— "下载到最后失败了，是不是磁盘满了？"
  yt-dlp 的写盘错误本身不带"开跑前还剩多少"，那个上下文只能由我们在 `Popen` 前落。

两组断言的共同主题是**「只在有事时说话」**：改写只在值真的变了时记（否则
`count(kind=decision subsystem=container)` 不再等于"容器被系统改过几次"），
而磁盘那条即便一切正常也要留一行 DEBUG —— 没有它，"日志里没提磁盘"既可能是空间充足，
也可能是这段代码压根没跑。
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-ctnr-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from fluentytdl.download import executor as executor_mod  # noqa: E402
from fluentytdl.observability import new_flow  # noqa: E402
from fluentytdl.utils.container_compat import (  # noqa: E402
    ensure_audio_multistream_compatible_container,
    ensure_subtitle_compatible_container,
)


@pytest.fixture
def events():
    """收集本次测试期间的 fytdl 事件。

    `_level` 是**本夹具加的**，不是事件字段 —— 级别决定这条会不会冒到控制台
    （console sink 是 INFO），而"该不该冒"正是这几条断言要锁的东西之一。
    """
    captured: list[dict] = []
    sink_id = logger.add(
        lambda m: captured.append(dict(m.record["extra"]["fytdl"], _level=m.record["level"].name)),
        level="TRACE",
        filter=lambda record: "fytdl" in record.get("extra", {}),
    )
    try:
        yield captured
    finally:
        logger.remove(sink_id)


def _containers(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "decision" and e.get("subsystem") == "container"]


# ── 字幕兼容改写 ────────────────────────────────────────────


def test_multi_lang_upgrade_records_the_reason_and_the_count(events):
    """mp4 + 多语言字幕 → mkv。语言条数必须一起记。

    `bcp47.resolve_requested(per_pref_limit=1)` 的默认值就是为了不在这里凭空触发升级；
    哪天它松了，`sub_langs_n` 是唯一的现场证据。
    """
    opts = {
        "embedsubtitles": True,
        "merge_output_format": "mp4",
        "subtitleslangs": ["en", "ja"],
    }
    ensure_subtitle_compatible_container(opts, trace=new_flow(stage="select"))

    assert opts["merge_output_format"] == "mkv"
    (decision,) = _containers(events)
    assert decision["reason"] == "subtitle_multi_lang"
    assert decision["container_before"] == "mp4"
    assert decision["container"] == "mkv"
    assert decision["sub_langs_n"] == 2
    # 改写是**正确行为**（不改会产出播不了的文件），不是警告；它只是不可见。
    assert decision["_level"] == "DEBUG"


def test_webm_cannot_hold_subtitles(events):
    opts = {"embedsubtitles": True, "merge_output_format": "webm", "subtitleslangs": ["en"]}
    ensure_subtitle_compatible_container(opts, trace=new_flow(stage="select"))

    (decision,) = _containers(events)
    assert decision["reason"] == "subtitle_webm_incompatible"
    assert decision["container_before"] == "webm"
    assert decision["container"] == "mkv"


def test_unset_container_defaults_to_mkv(events):
    """没人指定容器时升 mkv —— `container_before` 是 None，不是空串。"""
    opts = {"embedsubtitles": True, "subtitleslangs": ["en"]}
    ensure_subtitle_compatible_container(opts, trace=new_flow(stage="select"))

    (decision,) = _containers(events)
    assert decision["reason"] == "subtitle_container_unset"
    assert decision["container_before"] is None
    assert decision["container"] == "mkv"


@pytest.mark.parametrize(
    "opts",
    [
        # mp4 单字幕：mov_text 单轨可用，保持原样
        {"embedsubtitles": True, "merge_output_format": "mp4", "subtitleslangs": ["en"]},
        # mkv 本来就行
        {"embedsubtitles": True, "merge_output_format": "mkv", "subtitleslangs": ["en", "ja"]},
        # 压根没要嵌字幕，函数直接返回
        {"merge_output_format": "webm", "subtitleslangs": ["en", "ja"]},
    ],
)
def test_checked_and_left_alone_is_not_a_decision(events, opts):
    """ "检查过、保持原样"不记事件。

    记下来只是噪音，而且会让 `count(kind=decision subsystem=container)` 不再等于
    "容器被系统改过几次" —— 那个计数正是排查"谁把我的容器换了"的入口。
    """
    before = opts.get("merge_output_format")
    ensure_subtitle_compatible_container(opts, trace=new_flow(stage="select"))

    assert opts.get("merge_output_format") == before
    assert not _containers(events)


def test_no_trace_means_no_event(events):
    """不传 trace 就完全不记 —— 观测是 best-effort，不许影响改写本身（硬规则 5）。"""
    opts = {"embedsubtitles": True, "merge_output_format": "webm", "subtitleslangs": ["en"]}
    ensure_subtitle_compatible_container(opts)

    assert opts["merge_output_format"] == "mkv"  # 改写照做
    assert not events


# ── 多音轨改写 ──────────────────────────────────────────────


def test_multistream_upgrade_carries_the_track_count(events):
    opts = {"merge_output_format": "mp4"}
    ensure_audio_multistream_compatible_container(opts, 3, trace=new_flow(stage="select"))

    (decision,) = _containers(events)
    assert decision["reason"] == "audio_multistream"
    assert decision["container_before"] == "mp4"
    assert decision["container"] == "mkv"
    assert decision["audio_tracks"] == 3


def test_single_track_is_left_alone(events):
    opts = {"merge_output_format": "mp4"}
    ensure_audio_multistream_compatible_container(opts, 1, trace=new_flow(stage="select"))

    assert opts["merge_output_format"] == "mp4"
    assert not _containers(events)


def test_two_rewrites_in_a_row_are_two_events(events):
    """字幕升级完再问多音轨 —— 第二次容器已经是 mkv 了，不该再记一条。

    实际装配顺序就是这样（`download_config_window` 里两个 `ensure_*` 紧挨着调），
    所以"第二条静默"不是理论情况，而是每次多音轨下载的常态。
    """
    trace = new_flow(stage="select")
    opts = {"embedsubtitles": True, "merge_output_format": "mp4", "subtitleslangs": ["en", "ja"]}
    ensure_subtitle_compatible_container(opts, trace=trace)
    ensure_audio_multistream_compatible_container(opts, 2, trace=trace)

    (decision,) = _containers(events)
    assert decision["reason"] == "subtitle_multi_lang"


# ── 磁盘预检 ────────────────────────────────────────────────


class _Usage:
    """`shutil.disk_usage()` 的最小替身（只需要三个属性）。"""

    def __init__(self, free: int, total: int = 500 * 1024**3):
        self.free = free
        self.total = total
        self.used = total - free


def _disks(events: list[dict]) -> list[dict]:
    return [e for e in events if e["kind"] == "signal" and e.get("subsystem") == "disk"]


def test_ample_space_still_leaves_a_line(events, tmp_path, monkeypatch):
    """空间充足也要记，只是 DEBUG 级不刷控制台。

    没有这条，"日志里没提磁盘"就有两种解释：空间充足，或这段代码压根没跑。
    排除法需要反证，反证就是这一行。
    """
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _Usage(free=400 * 1024**3))
    executor_mod._emit_disk_space_signal(str(tmp_path))

    (signal,) = _disks(events)
    assert signal["code"] == "disk_space_ok"
    assert signal["_level"] == "DEBUG"
    assert signal["available_bytes"] == 400 * 1024**3
    assert signal["shortfall_bytes"] is None
    assert signal["stage"] == "preflight"


def test_low_space_escalates_to_warning(events, tmp_path, monkeypatch):
    """只剩 200 MB —— WARNING，要冒到控制台。

    它**不阻止下载**：开跑前的体积预估不可靠（DASH 分流各算一份 + 合并产物 +
    后处理临时文件都不在 `filesize_approx` 里），拿它当门禁只会误杀。这条的作用是
    让随后 yt-dlp 那句写盘错误**有上下文**。
    """
    free = 200 * 1024**2
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _Usage(free=free))
    executor_mod._emit_disk_space_signal(str(tmp_path))

    (signal,) = _disks(events)
    assert signal["code"] == "disk_space_low"
    assert signal["_level"] == "WARNING"
    assert signal["available_bytes"] == free
    assert signal["shortfall_bytes"] == 1024**3 - free
    assert signal["probe_error"] is None


def test_probe_failure_is_not_the_same_as_a_full_disk(events, tmp_path, monkeypatch):
    """探不出来 ≠ 确实不够。

    两种情况在 `SpaceCheckResult` 里都是 `sufficient=False, available_bytes=0`，
    可前者是我们瞎了、后者是磁盘真满了。混成一个 code，"下载失败是不是因为磁盘满"
    就永远答不上来 —— 所以必须靠 `probe_error` 分开。
    """

    def _boom(_path):
        raise PermissionError("access denied")

    monkeypatch.setattr(shutil, "disk_usage", _boom)
    executor_mod._emit_disk_space_signal(str(tmp_path))

    (signal,) = _disks(events)
    assert signal["code"] == "disk_space_unknown"
    assert signal["probe_error"] == "PermissionError"
    assert signal["available_bytes"] == 0


def test_the_path_is_recorded_without_the_username(events, monkeypatch):
    """目录本身是线索（哪个盘、哪个子目录），但日志是用户会直接贴进 Issue 的东西。"""
    monkeypatch.setattr(shutil, "disk_usage", lambda p: _Usage(free=400 * 1024**3))
    home = str(Path.home())
    executor_mod._emit_disk_space_signal(os.path.join(home, "Videos", "FluentYTDL"))

    (signal,) = _disks(events)
    assert signal["dir"].startswith("~")
    assert Path.home().name not in signal["dir"]


def test_a_broken_probe_can_never_break_the_download(events, tmp_path, monkeypatch):
    """硬规则 5：观测失败绝不回传业务层。

    这条守的是"日志系统在错误现场自己炸掉"那类事故 —— `_emit_disk_space_signal()`
    就在 `Popen` 前一行，它抛异常等于整个下载起不来。
    """

    def _explode(*_args, **_kwargs):
        raise RuntimeError("sensor on fire")

    monkeypatch.setattr(executor_mod, "check_space_for_download", _explode)
    executor_mod._emit_disk_space_signal(str(tmp_path))  # 不抛

    assert not _disks(events)
