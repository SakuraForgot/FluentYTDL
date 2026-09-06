"""无 `detail` 的行：偏好在下载路径上被换成真实字幕键（决策 1「两者结合」）。

这是报告 `FluentYTDL-字幕下载问题排查报告.md` 里四条任务字幕全空的**确切路径**：
播放列表行未逐个解析，`row_data["detail"]` 是 `None`，生产端把
`["zh-Hans", "en"]` 原样塞进 `subtitleslangs` —— 而 `--sub-langs` 的每一项被
yt-dlp 当成锚定正则，裸 `en` 匹配不到真实键 `en-GB`，一个 `.vtt` 都不写。

生产端改成只声明意图（`declare_subtitle_intent`）之后，这里钉住迟解析的出口：

1. 拿到 info → 真实字幕键，且**与逐行解析过的行完全一致**（含 `type_preference`）；
2. 拿不到 info → 锚定正则回落，**绝不**静默关掉字幕；
3. 取消要透传，其余异常只降级不失败；
4. 只算一次 —— 重试不重复发网络请求，也不写回共享的 `self.opts`。

全程不碰网络：`extract_info_sync` 由测试替身接管。
"""

import os
import sys
import tempfile
from pathlib import Path

# config_manager / logger 在 import 期就会落地文件，先把数据目录挪到临时目录
os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-deftest-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.download import workers as workers_mod  # noqa: E402
from fluentytdl.download.workers import DownloadWorker  # noqa: E402
from fluentytdl.models.subtitle_config import (  # noqa: E402
    SUBTITLE_CONFIG_KEY,
    SUBTITLE_PREFS_KEY,
    SUBTITLE_RESOLUTION_KEY,
    SubtitleConfig,
    SubtitleTypePreference,
)
from fluentytdl.processing.subtitle_service import declare_subtitle_intent  # noqa: E402
from fluentytdl.youtube.yt_dlp_cli import YtDlpCancelled  # noqa: E402

URL = "https://www.youtube.com/watch?v=MSJMJxd1udk"


def _sub_entry(ext: str = "vtt", name: str = "") -> list[dict]:
    return [{"ext": ext, "url": f"https://example.invalid/{ext}", "name": name}]


# 与 test_subtitle_service_resolution.py 同一份实测形状（BBC 6 Minute English）
BBC_INFO = {
    "id": "MSJMJxd1udk",
    "language": "en-GB",
    "subtitles": {"en-GB": _sub_entry(name="English (United Kingdom)")},
    "automatic_captions": {
        "en-en-GB": _sub_entry(name="English (United Kingdom) from English (United Kingdom)"),
        "zh-Hans-en-GB": _sub_entry(name="Chinese (Simplified) from English (United Kingdom)"),
    },
}

# 播放列表未逐行解析时 info 就是这个样子 —— 没有 subtitles / automatic_captions
FLAT_INFO = {"id": "MSJMJxd1udk", "title": "...", "_type": "url"}


def _config(**kwargs) -> SubtitleConfig:
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("type_preference", SubtitleTypePreference.ALL)
    return SubtitleConfig(**kwargs)


def _worker(
    prefs: list[str],
    config: SubtitleConfig,
    cached_info: dict | None,
    **extra_opts,
) -> DownloadWorker:
    """造一个只带字幕意图的 worker（不启动线程，只调解析方法）。"""
    opts: dict = {"format": "bv*+ba/b", **extra_opts}
    opts.update(declare_subtitle_intent(prefs, config))
    return DownloadWorker(URL, opts, cached_info=cached_info)


class _FakeExtract:
    """`extract_info_sync` 的替身：记调用次数，好证明"只解析一次"。"""

    def __init__(self, result=None, exc: BaseException | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls = 0

    def __call__(self, url, options=None, **kwargs):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result


@pytest.fixture
def no_network(monkeypatch):
    """默认封网：没显式装替身的用例一旦发请求就直接失败。"""

    def _boom(*args, **kwargs):
        raise AssertionError("迟解析不应该在这条路径上发网络请求")

    monkeypatch.setattr(workers_mod.youtube_service, "extract_info_sync", _boom)
    return monkeypatch


# ── 生产端：只声明意图 ───────────────────────────────────────


def test_declare_intent_touches_no_download_flags() -> None:
    """`declare_subtitle_intent` 只能返回三个私有键。

    多设一个 `writesubtitles` / `embedsubtitles` 就会踩掉调用点自己的嵌入决策 ——
    纯字幕模式明确要 `embedsubtitles=False`，配置窗口的外挂模式也一样。
    """
    opts = declare_subtitle_intent(["zh-Hans", "en"], _config())

    assert set(opts) == {SUBTITLE_PREFS_KEY, SUBTITLE_CONFIG_KEY, SUBTITLE_RESOLUTION_KEY}
    assert opts[SUBTITLE_PREFS_KEY] == ["zh-Hans", "en"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "deferred"


def test_intent_carries_type_preference() -> None:
    """`writeautomaticsub` 是个布尔，表达不了 `type_preference`。

    少了随行的 `SUBTITLE_CONFIG_KEY`，迟解析只能猜全局设置，未解析的行就会下到
    用户明确排除的轨道 —— 与解析过的行行为不一致。
    """
    config = _config(type_preference=SubtitleTypePreference.MANUAL_ONLY, max_languages=3)
    carried = SubtitleConfig.from_dict(declare_subtitle_intent(["en"], config)[SUBTITLE_CONFIG_KEY])

    assert carried.type_preference == SubtitleTypePreference.MANUAL_ONLY
    assert carried.max_languages == 3


# ── 出口 1：拿到 info → 真实字幕键 ───────────────────────────


def test_cached_info_resolves_to_real_codes(no_network) -> None:
    """报告的验收点：**绝不再出现裸 `zh-Hans,en`**。

    `cached_info` 已经在 `:734` 喂给 executor，所以这条路径零额外网络请求 ——
    `no_network` fixture 就是这句话的断言。
    """
    worker = _worker(["zh-Hans", "en"], _config(), BBC_INFO)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts["subtitleslangs"] == ["zh-Hans-en-GB", "en-GB"]
    assert opts["subtitleslangs"] != ["zh-Hans", "en"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "exact"
    # 意图键必须被消费掉，不能漏进 ydl_opts
    assert SUBTITLE_PREFS_KEY not in opts
    assert SUBTITLE_CONFIG_KEY not in opts


def test_flat_info_fetches_then_resolves(monkeypatch) -> None:
    """flat info 没有字幕字段，得补一次请求 —— 这是"两者结合"的后半段。"""
    fake = _FakeExtract(result=BBC_INFO)
    monkeypatch.setattr(workers_mod.youtube_service, "extract_info_sync", fake)

    worker = _worker(["en"], _config(), FLAT_INFO)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert fake.calls == 1
    assert opts["subtitleslangs"] == ["en-GB"]


def test_type_preference_parity_with_parsed_rows(no_network) -> None:
    """默认 `MANUAL_AND_ASR` 排除自动翻译，于是 `zh-Hans` 命中不了 `zh-Hans-en-GB`。

    这跟 `test_subtitle_service_resolution.py::test_type_preference_gates_translated_tracks`
    是同一个结论 —— 未解析的行和解析过的行必须给出**一样**的答案。想要自动翻译字幕
    得把 `type_preference` 设成 `ALL`；关键是"没拿到"现在有据可查，不再无声消失。
    """
    config = _config(type_preference=SubtitleTypePreference.MANUAL_AND_ASR)
    worker = _worker(["zh-Hans", "en"], config, BBC_INFO)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts["subtitleslangs"] == ["en-GB"]
    assert opts[SUBTITLE_RESOLUTION_KEY]["missed"] == ["zh-Hans"]


def test_available_lists_unfiltered_codes(no_network) -> None:
    """`available` 报的是**过滤前**的真实清单。

    `features._explain_missing` 拿它渲染"该视频只有 …"。若这里先按 type_preference
    过滤，用户会看到"无可用字幕"这种误导性结论 —— 而报告作者当初正是自己跑
    `--list-subs` 才拿到这份清单的。
    """
    config = _config(type_preference=SubtitleTypePreference.MANUAL_ONLY)
    worker = _worker(["ja"], config, BBC_INFO)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert set(opts[SUBTITLE_RESOLUTION_KEY]["available"]) == {
        "en-GB",
        "en-en-GB",
        "zh-Hans-en-GB",
    }


def test_video_without_any_subtitle_is_no_match(no_network) -> None:
    """拿到了 info 却一条轨道都没有 —— 这个视频真的没字幕，显式关掉。

    与"拿不到 info"必须区别对待：那才是回落正则的场合。`no_network` 一并钉住
    "不为已解析的行新增网络请求"：`subtitles={} automatic_captions={}` 是完整解析
    的产物，为它再解析一次纯属浪费。
    """
    worker = _worker(["en"], _config(), {"id": "x", "subtitles": {}, "automatic_captions": {}})
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "no_match"
    assert opts["writesubtitles"] is False
    assert opts["writeautomaticsub"] is False


def test_half_populated_info_is_not_authoritative(monkeypatch) -> None:
    """只有一个硬编码 `"subtitles": {}` 的合成 dict **不算**权威答案。

    `selection_dialog._normalize_info_payload:145` 就产出这种形状。若按"键在不在"
    一刀切采信它，那条路上的每个任务都会被判成"这视频没有字幕"—— 等于从另一个
    方向复现本次要修的 bug。所以判据是两个键都在。
    """
    fake = _FakeExtract(result=BBC_INFO)
    monkeypatch.setattr(workers_mod.youtube_service, "extract_info_sync", fake)

    worker = _worker(["en"], _config(), {"id": "x", "title": "...", "subtitles": {}})
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert fake.calls == 1
    assert opts["subtitleslangs"] == ["en-GB"]


# ── 出口 2：拿不到 info → 锚定正则，绝不静默关掉 ─────────────


def test_extract_failure_falls_back_to_pattern(monkeypatch) -> None:
    """取 info 失败是家常事（限流、cookie 过期）。字幕是 best-effort：

    既不能让下载任务失败，也不能静默把字幕关掉 —— 回落成锚定正则交给 yt-dlp
    自己匹配。用 `en(-.+)?` 而不是 `en.*`，后者会连 `eng` / `enm` 一起收。
    """
    monkeypatch.setattr(
        workers_mod.youtube_service,
        "extract_info_sync",
        _FakeExtract(exc=RuntimeError("HTTP Error 429: Too Many Requests")),
    )

    worker = _worker(["zh-Hans", "en"], _config(), None)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts["subtitleslangs"] == ["zh-Hans(-.+)?", "en(-.+)?"]
    assert opts["writesubtitles"] is True
    assert opts[SUBTITLE_RESOLUTION_KEY]["mode"] == "pattern"


def test_pattern_mode_honours_manual_only(monkeypatch) -> None:
    """正则分不清人工 / 自动生成 / 自动翻译，`MANUAL_AND_ASR` 在这条路上没法执行。

    `MANUAL_ONLY` 是唯一表达得出来的那一种：不给 `--write-auto-subs` 就是了。
    """
    monkeypatch.setattr(
        workers_mod.youtube_service, "extract_info_sync", _FakeExtract(exc=RuntimeError("boom"))
    )

    config = _config(type_preference=SubtitleTypePreference.MANUAL_ONLY)
    worker = _worker(["en"], config, None)
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts["writeautomaticsub"] is False


def test_cancel_is_not_swallowed_as_pattern(monkeypatch) -> None:
    """取消必须透传。

    `except Exception` 若排在 `except YtDlpCancelled` 前面，用户点了取消之后任务
    还会接着跑完字幕下载 —— 顺序在这里是语义，不是风格。
    """
    monkeypatch.setattr(
        workers_mod.youtube_service, "extract_info_sync", _FakeExtract(exc=YtDlpCancelled())
    )

    worker = _worker(["en"], _config(), None)
    with pytest.raises(YtDlpCancelled):
        worker._resolve_subtitle_prefs(dict(worker.opts))


# ── 只算一次，且不写回共享状态 ───────────────────────────────


def test_resolution_is_cached_across_retries(monkeypatch) -> None:
    """规则驱动的自动重试会再跑一遍 `run()` —— 不能每次都补一次网络请求。"""
    fake = _FakeExtract(result=BBC_INFO)
    monkeypatch.setattr(workers_mod.youtube_service, "extract_info_sync", fake)

    worker = _worker(["en"], _config(), FLAT_INFO)
    first, second = dict(worker.opts), dict(worker.opts)
    worker._resolve_subtitle_prefs(first)
    worker._resolve_subtitle_prefs(second)

    assert fake.calls == 1
    assert first["subtitleslangs"] == second["subtitleslangs"] == ["en-GB"]


def test_shared_opts_survive_resolution(no_network) -> None:
    """解析只改传进去的那份 dict。

    `self.opts` 是重试的原料（也会被 UI 读），意图键被 pop 掉就再也解析不出来了。
    `run()` 传的是 deepcopy，`_run_lightweight_extract` 传的是浅拷贝 —— 两条路
    都不能落到 `self.opts` 上。
    """
    worker = _worker(["en"], _config(), BBC_INFO)
    worker._resolve_subtitle_prefs(dict(worker.opts))

    assert worker.opts[SUBTITLE_PREFS_KEY] == ["en"]
    assert SUBTITLE_CONFIG_KEY in worker.opts


def test_no_prefs_is_a_noop(no_network) -> None:
    """已逐行解析的行早就有真实字幕键了，迟解析不能去动它。"""
    worker = DownloadWorker(URL, {"subtitleslangs": ["en-GB"], "writesubtitles": True})
    opts = dict(worker.opts)
    worker._resolve_subtitle_prefs(opts)

    assert opts["subtitleslangs"] == ["en-GB"]
