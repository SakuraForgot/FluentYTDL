"""解析结果 TTL 缓存的分桶限容与键前缀（P2.1 / P2.2）。

关注点是"播放列表逐条深解析不能挤掉弹窗结果"这条回归点，以及缓存返回的是
深拷贝副本（调用方就地改写不得污染缓存）。P2.2 追加频道 `channel_tab` 分桶、
负结果不入缓存、封面模式 `read_cache=False` 三组。不触网、不起子进程。
"""

import importlib
import sys
import time
from pathlib import Path

import pytest

# Resolve src/ for direct execution
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from fluentytdl.youtube.youtube_service import YoutubeService  # noqa: E402

# 五个 mode 的完整清单，与 YoutubeService._PARSE_CACHE_LIMITS 一一对应。
# 新增 mode 时这里会连带失败，提醒补上分桶用例。
ALL_MODES = ("dialog", "playlist_flat", "vr", "entry_detail", "channel_tab")


@pytest.fixture
def svc():
    """单例服务 + 每次用例前清空缓存，避免用例之间互相污染。

    同时把 `pot_provider_enabled` 摁成 False：本文件测的是分桶/隔离这些缓存机制，
    而 `_pot_state_unstable()` 在"POT 开着但没预热"时会整体停写（测试进程里
    pot_manager 永远不 warm）。直接改 config 字典而非 set()，不落盘。
    """
    from fluentytdl.core.config_manager import config_manager

    original_pot = config_manager.config.get("pot_provider_enabled", False)
    config_manager.config["pot_provider_enabled"] = False
    s = YoutubeService()
    s.invalidate_parse_cache()
    try:
        yield s
    finally:
        s.invalidate_parse_cache()
        config_manager.config["pot_provider_enabled"] = original_pot


def _key(svc, url, mode):
    return svc._parse_cache_key(url, mode, {})


def test_key_carries_mode_prefix(svc):
    assert _key(svc, "https://youtu.be/87DyyMV0kCY", "entry_detail").startswith("entry_detail:")
    assert _key(svc, "https://youtu.be/87DyyMV0kCY", "dialog").startswith("dialog:")


def test_same_url_different_mode_are_distinct_entries(svc):
    """dialog 与 entry_detail 的 ydl_opts 不同，绝不能互相命中。"""
    url = "https://youtu.be/87DyyMV0kCY"
    svc._parse_cache_put(_key(svc, url, "dialog"), {"title": "from dialog"})
    assert svc._parse_cache_get(_key(svc, url, "entry_detail")) is None


def test_entry_detail_flood_does_not_evict_dialog(svc):
    """本节的关键回归点：爬一个大列表不得挤掉弹窗/列表缓存。"""
    dialog_key = _key(svc, "https://youtu.be/dialogvideo", "dialog")
    flat_key = _key(svc, "https://youtube.com/playlist?list=PLxx", "playlist_flat")
    svc._parse_cache_put(dialog_key, {"title": "dialog"})
    svc._parse_cache_put(flat_key, {"entries": [1, 2, 3]})

    limit = svc._parse_cache_limit_for("entry_detail")
    for i in range(limit * 3):
        svc._parse_cache_put(_key(svc, f"https://youtu.be/vid{i:08d}", "entry_detail"), {"i": i})

    assert svc._parse_cache_get(dialog_key) is not None
    assert svc._parse_cache_get(flat_key) is not None
    assert svc._parse_cache_buckets().get("entry_detail", 0) <= limit


def test_bucket_evicts_oldest_within_same_mode(svc):
    limit = svc._parse_cache_limit_for("entry_detail")
    first = _key(svc, "https://youtu.be/firstentry0", "entry_detail")
    svc._parse_cache_put(first, {"n": 0})
    for i in range(limit):
        svc._parse_cache_put(_key(svc, f"https://youtu.be/vid{i:08d}", "entry_detail"), {"i": i})
    # 桶满后最旧的一条被淘汰
    assert svc._parse_cache_get(first) is None


def test_bucket_limits_are_independent(svc):
    """每个 mode 各自计数，互不占用对方额度。"""
    for mode in ALL_MODES:
        limit = svc._parse_cache_limit_for(mode)
        for i in range(limit + 5):
            svc._parse_cache_put(_key(svc, f"https://youtu.be/{mode}{i:06d}", mode), {"i": i})
    buckets = svc._parse_cache_buckets()
    for mode in ALL_MODES:
        assert buckets[mode] == svc._parse_cache_limit_for(mode)


def test_hit_returns_isolated_copy(svc):
    """调用方会把整个 info 塞进 UI 的行数据并就地改写，缓存必须不受影响。"""
    key = _key(svc, "https://youtu.be/87DyyMV0kCY", "entry_detail")
    svc._parse_cache_put(key, {"formats": [{"format_id": "137"}]})
    got, _age = svc._parse_cache_get(key)
    got["formats"].append({"format_id": "mutated"})
    again, _age2 = svc._parse_cache_get(key)
    assert len(again["formats"]) == 1


def test_put_snapshots_at_write_time(svc):
    """put 之后改写源 dict 不得回写进缓存。"""
    key = _key(svc, "https://youtu.be/87DyyMV0kCY", "vr")
    info = {"__fluentytdl_vr_mode": True, "formats": []}
    svc._parse_cache_put(key, info)
    info["formats"].append({"format_id": "late"})
    got, _ = svc._parse_cache_get(key)
    assert got["formats"] == []


def test_empty_info_is_not_cached(svc):
    key = _key(svc, "https://youtu.be/87DyyMV0kCY", "entry_detail")
    svc._parse_cache_put(key, {})
    assert svc._parse_cache_get(key) is None


def test_invalidate_clears_every_bucket(svc):
    for mode in ALL_MODES:
        svc._parse_cache_put(_key(svc, f"https://youtu.be/{mode}", mode), {"m": mode})
    svc.invalidate_parse_cache("test")
    assert svc._parse_cache_buckets() == {}


def test_no_write_while_pot_warming(svc, monkeypatch):
    """POT 预热窗口内不得写缓存：那时 extractor_args 还是 fetch_pot=never，
    预热完成后键会变，写进去的条目永远命不中，只会白占桶。"""
    monkeypatch.setattr(YoutubeService, "_pot_state_unstable", staticmethod(lambda: True))
    key = _key(svc, "https://youtu.be/87DyyMV0kCY", "entry_detail")
    svc._parse_cache_put(key, {"formats": [{"format_id": "137"}]})
    assert svc._parse_cache_get(key) is None
    assert svc._parse_cache_buckets() == {}


def test_pot_disabled_is_a_stable_state(svc):
    """POT 关闭时 fetch_pot=never 恒定，键本来就稳，不该被误判成不稳定而停写。"""
    from fluentytdl.core.config_manager import config_manager

    original = config_manager.get("pot_provider_enabled", False)
    config_manager.config["pot_provider_enabled"] = False
    try:
        assert svc._pot_state_unstable() is False
    finally:
        config_manager.config["pot_provider_enabled"] = original


# ── P2.2-B 字幕：钉住"视频 / 字幕共用同一条 dialog 缓存" ────────────────────


def test_subtitle_specific_opts_do_not_split_dialog_key(svc):
    """视频 / 字幕 / 封面三种模式今天都走 extract_info_for_dialog_sync，且
    `start_extraction` 把 `_current_options` 统一置 None，因此 ydl_opts 完全一致、
    共用同一条 dialog 缓存条目。

    这是巧合而不是设计：谁给某个模式加一个**进指纹**的专属 option，就会静默劈叉成
    两条，缓存收益悄无声息减半而没有任何测试拦得住。本例把"字幕/封面这类只影响
    落盘、不影响 `-J` 输出的选项不得进指纹"钉死。
    """
    url = "https://youtu.be/87DyyMV0kCY"
    base = {"cookiefile": None, "proxy": ""}
    baseline = svc._parse_cache_key(url, "dialog", base)

    for extra in (
        {"writesubtitles": True},
        {"writeautomaticsub": True, "subtitleslangs": ["zh-Hans", "en"]},
        {"writethumbnail": True},
        {"skip_download": True},
        {"retries": 5, "socket_timeout": 30},  # 重试/超时同样与结果无关
    ):
        assert svc._parse_cache_key(url, "dialog", {**base, **extra}) == baseline, extra


def test_result_changing_opts_do_split_dialog_key(svc):
    """反向：真正影响格式列表的选项必须进指纹，否则"改了设置结果不变"。"""
    url = "https://youtu.be/87DyyMV0kCY"
    base = {"cookiefile": None, "proxy": ""}
    baseline = svc._parse_cache_key(url, "dialog", base)

    for extra in (
        {"proxy": "http://127.0.0.1:1080"},
        {"extractor_args": {"youtube": {"player_client": ["web"]}}},
        {"format_sort": ["res:1080"]},
        {"format": "bv*+ba/b"},
        {"extract_flat": "in_playlist"},
    ):
        assert svc._parse_cache_key(url, "dialog", {**base, **extra}) != baseline, extra


# ── P2.2-A 频道：channel_tab 分桶 ──────────────────────────────────────────


def test_channel_tabs_are_distinct_entries(svc):
    """三个标签页必须各占一条：tab 已经在 normalized_url 后缀里，
    所以 _parse_cache_key 不需要额外字段就能区分。"""
    base = "https://www.youtube.com/@somechannel"
    keys = {
        tab: svc._parse_cache_key(svc._normalize_channel_url(base, tab), "channel_tab", {})
        for tab in ("videos", "shorts", "streams")
    }
    assert len(set(keys.values())) == 3


def test_channel_tab_bucket_limit_is_three_channels(svc):
    """9 = 3 个频道 × 3 个标签页。改小了会让"换个频道再切回来"必然 miss。"""
    assert svc._parse_cache_limit_for("channel_tab") == 9


def test_channel_flood_does_not_evict_dialog(svc):
    """爬一堆频道标签页不得挤掉弹窗结果——分桶的核心回归点。"""
    dialog_key = _key(svc, "https://youtu.be/dialogvideo", "dialog")
    svc._parse_cache_put(dialog_key, {"title": "dialog"})

    limit = svc._parse_cache_limit_for("channel_tab")
    for i in range(limit * 3):
        url = svc._normalize_channel_url(f"https://www.youtube.com/@ch{i:04d}", "videos")
        svc._parse_cache_put(svc._parse_cache_key(url, "channel_tab", {}), {"entries": [{"i": i}]})

    assert svc._parse_cache_get(dialog_key) is not None
    assert svc._parse_cache_buckets().get("channel_tab", 0) <= limit


def test_oversized_entries_are_not_cached(svc):
    """频道 / 大列表的 flat dump 可能上千条，deepcopy 成本高于收益，直接不写。"""
    url = svc._normalize_channel_url("https://www.youtube.com/@bigchannel", "videos")
    key = svc._parse_cache_key(url, "channel_tab", {})
    over = svc._PARSE_CACHE_MAX_ENTRIES + 1
    svc._parse_cache_put(key, {"entries": [{"id": f"v{i}"} for i in range(over)]})
    assert svc._parse_cache_get(key) is None

    at_limit = svc._PARSE_CACHE_MAX_ENTRIES
    svc._parse_cache_put(key, {"entries": [{"id": f"v{i}"} for i in range(at_limit)]})
    assert svc._parse_cache_get(key) is not None


# ── P2.2-A3 负结果绝不进缓存 ───────────────────────────────────────────────


@pytest.fixture
def stub_ytdlp(monkeypatch):
    """把 yt-dlp 子进程换成可编程的桩，并记录调用次数。

    返回一个 `set_result(fn)` 闭包：传可调用对象即作为 run_dump_single_json 的实现。
    `calls` 列表用于断言"到底有没有真的去跑子进程"——这是缓存命中/落空的唯一硬证据。
    """
    # 注意：不能写 `from fluentytdl.youtube import youtube_service` —— 包的 __init__
    # 把同名的**单例实例**重导出了，那样拿到的是实例而不是模块，setattr 会打错地方。
    mod = importlib.import_module("fluentytdl.youtube.youtube_service")

    calls: list[str] = []
    state: dict[str, object] = {"impl": lambda url, opts, **kw: {"id": "stub"}}

    def _run(url, opts, **kwargs):
        calls.append(url)
        return state["impl"](url, opts, **kwargs)  # type: ignore[operator]

    monkeypatch.setattr(mod, "locate_runtime_tool", lambda *a, **k: "yt-dlp.exe")
    monkeypatch.setattr(mod, "run_dump_single_json", _run)
    # build_ydl_options 会读 cookie 文件与配置，与本文件要测的东西无关；
    # 固定成空 dict 让缓存键只由 url + mode 决定。
    monkeypatch.setattr(YoutubeService, "build_ydl_options", lambda self, *a, **k: {})

    def _set(impl):
        state["impl"] = impl

    return type("Stub", (), {"calls": calls, "set_result": staticmethod(_set)})()


def test_channel_tab_failure_is_not_cached(svc, stub_ytdlp):
    """一次瞬时网络失败若被缓存 5 分钟，用户重试整个 TTL 内都看不到那个标签页。

    对应验收项 4：断网解析频道 → 恢复网络立刻重试必须真的重新发请求。
    错误文案刻意避开 "does not have a … tab"，以免走进标签页兜底逻辑。
    """
    url = "https://www.youtube.com/@somechannel"

    def _boom(u, opts, **kw):
        raise RuntimeError("Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo>")

    stub_ytdlp.set_result(_boom)
    with pytest.raises(RuntimeError):
        svc.extract_channel_flat(url, tab="videos", base_ydl_opts={})
    assert svc._parse_cache_buckets() == {}

    # 恢复网络：必须真的再发一次请求并成功，而不是沿用 unsupported
    stub_ytdlp.set_result(lambda u, opts, **kw: {"entries": [{"id": "a"}], "title": "ch"})
    info = svc.extract_channel_flat(url, tab="videos", base_ydl_opts={})
    assert info["entries"] == [{"id": "a"}]
    assert len(stub_ytdlp.calls) == 2


def test_channel_tab_hit_skips_subprocess_and_carries_tab(svc, stub_ytdlp):
    """成功结果进缓存；二次调用零子进程，且 `__fluentytdl_tab` 是成品的一部分。"""
    url = "https://www.youtube.com/@somechannel"
    stub_ytdlp.set_result(lambda u, opts, **kw: {"entries": [{"id": "a"}], "title": "ch"})

    first = svc.extract_channel_flat(url, tab="videos", base_ydl_opts={})
    assert first["__fluentytdl_tab"] == "videos"
    assert len(stub_ytdlp.calls) == 1

    second = svc.extract_channel_flat(url, tab="videos", base_ydl_opts={})
    assert second["entries"] == [{"id": "a"}]
    assert second["__fluentytdl_tab"] == "videos"
    assert len(stub_ytdlp.calls) == 1  # 没有再起子进程


def test_cache_hit_stamps_the_requested_tab(svc, stub_ytdlp):
    """`_normalize_channel_url` 把未识别的 tab 折叠成 /videos，两个 tab 会算出同一个键。
    命中时必须按**本次请求**的 tab 打标记，否则上层会把结果记到错误的标签页下。"""
    url = "https://www.youtube.com/@somechannel"
    stub_ytdlp.set_result(lambda u, opts, **kw: {"entries": [{"id": "a"}]})

    svc.extract_channel_flat(url, tab="videos", base_ydl_opts={})
    hit = svc.extract_channel_flat(url, tab="podcasts", base_ydl_opts={})
    assert len(stub_ytdlp.calls) == 1  # 确认走的是缓存命中这条路
    assert hit["__fluentytdl_tab"] == "podcasts"


# ── P2.2-C 封面：不读缓存，但照常写 ────────────────────────────────────────


def test_entry_detail_reads_cache_by_default(svc, stub_ytdlp):
    """默认行为的对照组：命中即返回，不起子进程。"""
    url = "https://youtu.be/87DyyMV0kCY"
    key = svc._parse_cache_key(url, "entry_detail", {})
    svc._parse_cache_put(key, {"id": "cached", "thumbnails": [{"url": "https://i.ytimg.com/OLD"}]})

    got = svc.extract_info_sync(url)
    assert got["id"] == "cached"
    assert stub_ytdlp.calls == []


def test_cover_mode_skips_read_but_still_writes(svc, stub_ytdlp):
    """封面模式 read_cache=False：跳过读（陈旧直链会 403/404），但写照常——
    这一趟本来就是新鲜的，写回去顺带把共享条目刷新，对视频模式只有好处。"""
    url = "https://youtu.be/87DyyMV0kCY"
    key = svc._parse_cache_key(url, "entry_detail", {})
    svc._parse_cache_put(key, {"id": "stale", "thumbnails": [{"url": "https://i.ytimg.com/OLD"}]})

    stub_ytdlp.set_result(
        lambda u, opts, **kw: {"id": "fresh", "thumbnails": [{"url": "https://i.ytimg.com/NEW"}]}
    )
    got = svc.extract_info_sync(url, read_cache=False)

    assert got["id"] == "fresh"
    assert len(stub_ytdlp.calls) == 1  # 确实绕过了缓存，真的跑了一次
    cached, _age = svc._parse_cache_get(key)
    assert cached["id"] == "fresh"  # 陈旧条目被这一趟刷新掉了


def test_extract_video_info_forwards_read_cache(svc, stub_ytdlp):
    """EntryDetailWorker 走的是 extract_video_info 这个 wrapper，
    read_cache 必须能透过它传下去，否则封面那条链只修了一半。"""
    url = "https://youtu.be/87DyyMV0kCY"
    key = svc._parse_cache_key(url, "entry_detail", {})
    svc._parse_cache_put(key, {"id": "cached"})

    stub_ytdlp.set_result(lambda u, opts, **kw: {"id": "fresh"})
    assert svc.extract_video_info(url)["id"] == "cached"
    assert svc.extract_video_info(url, read_cache=False)["id"] == "fresh"


# ── 保留时间：默认半小时 + 设置页可控 ─────────────────────────────────────────


def test_default_retention_is_half_an_hour():
    """设置页「解析结果保留时间」的默认档。改这个值要连带改
    `YoutubeService._parse_cache_ttl()` 的兜底默认，两处必须一致。"""
    from fluentytdl.core.config_manager import ConfigManager

    assert ConfigManager.DEFAULT_CONFIG["parse_cache_ttl_seconds"] == 1800


def test_ttl_fallback_matches_the_config_default(svc, monkeypatch):
    """配置里缺键或存了脏值时，服务层兜底也必须是半小时，不能退回旧的 300。"""
    from fluentytdl.core.config_manager import config_manager

    monkeypatch.delitem(config_manager.config, "parse_cache_ttl_seconds", raising=False)
    assert svc._parse_cache_ttl() == 1800.0

    config_manager.config["parse_cache_ttl_seconds"] = "不是数字"
    assert svc._parse_cache_ttl() == 1800.0


def test_zero_retention_stops_both_read_and_write(svc):
    """设置页的「不保留」档写 0。它必须同时停掉读和写，而不是只停读——
    只停读会让条目照常堆在内存里白占桶。"""
    from fluentytdl.core.config_manager import config_manager

    original = config_manager.config.get("parse_cache_ttl_seconds")
    config_manager.config["parse_cache_ttl_seconds"] = 0
    try:
        key = _key(svc, "https://youtu.be/87DyyMV0kCY", "dialog")
        svc._parse_cache_put(key, {"formats": [{"format_id": "137"}]})
        assert svc._parse_cache_buckets() == {}
        assert svc._parse_cache_get(key) is None
    finally:
        config_manager.config["parse_cache_ttl_seconds"] = original


def test_shortening_retention_expires_old_entries_without_clearing(svc):
    """缩短保留时间不需要清缓存：`_parse_cache_get` 每次都拿**当前**时长比对年龄。
    这条钉住设置页 handler 里「只在改成 0 时才清」的那个决定。"""
    from fluentytdl.core.config_manager import config_manager

    original = config_manager.config.get("parse_cache_ttl_seconds")
    config_manager.config["parse_cache_ttl_seconds"] = 1800
    try:
        key = _key(svc, "https://youtu.be/87DyyMV0kCY", "dialog")
        svc._parse_cache_put(key, {"formats": [{"format_id": "137"}]})
        # 把这条倒推成 10 分钟前写入的，避免依赖真实时钟
        with svc._parse_cache_lock:
            _stored_at, info = svc._parse_cache[key]
            svc._parse_cache[key] = (time.monotonic() - 600, info)

        assert svc._parse_cache_get(key) is not None  # 半小时档内，仍然命中

        config_manager.config["parse_cache_ttl_seconds"] = 300  # 用户改到 5 分钟档
        assert svc._parse_cache_get(key) is None  # 无需清缓存，读取时自然淘汰
    finally:
        config_manager.config["parse_cache_ttl_seconds"] = original


# ── 旧安装的一次性迁移（300 → 1800） ──────────────────────────────────────────
#
# `config_manager.save()` 写的是整份合并后的配置，所以任何存过盘的旧安装磁盘上都躺着
# 一个 300。只改 DEFAULT_CONFIG 迁移不了它们，于是有了 `_migrate_parse_cache_ttl`。
# 下面四条钉住它的边界；纯字典进出，不碰磁盘。


def _migrate(data: dict) -> dict:
    from fluentytdl.core.config_manager import ConfigManager

    merged = {**ConfigManager.DEFAULT_CONFIG, **data}
    ConfigManager._migrate_parse_cache_ttl(data, merged)
    return merged


def test_migration_lifts_the_old_default():
    """旧安装存的 300 是旧默认值，不可能是用户的选择——那时候设置页根本没有这张卡片。"""
    merged = _migrate({"parse_cache_ttl_seconds": 300})
    assert merged["parse_cache_ttl_seconds"] == 1800
    assert merged["parse_cache_ttl_migrated"] is True


def test_migration_runs_only_once():
    """标记位落盘后，用户在设置页真挑了「5 分钟」，下次启动不能被悄悄改回半小时。
    这是整个迁移里唯一会伤到用户的失败模式。"""
    merged = _migrate({"parse_cache_ttl_seconds": 300, "parse_cache_ttl_migrated": True})
    assert merged["parse_cache_ttl_seconds"] == 300


def test_migration_leaves_hand_edited_values_alone():
    """只认 300 这一个旧默认值；手改过 config.json 的其它值一概不动。"""
    merged = _migrate({"parse_cache_ttl_seconds": 60})
    assert merged["parse_cache_ttl_seconds"] == 60
    assert merged["parse_cache_ttl_migrated"] is True


def test_migration_survives_a_garbage_value():
    """`_load()` 整段外面套着 except，这里抛一次异常会把用户的**整份**配置回退成默认值。
    所以比较刻意用 `== 300` 而不是 `int(...)`，脏值只能原样放过。"""
    merged = _migrate({"parse_cache_ttl_seconds": "不是数字"})
    assert merged["parse_cache_ttl_seconds"] == "不是数字"
    assert merged["parse_cache_ttl_migrated"] is True
