"""产物词汇表（`observability/artifacts.py`）的测试。

这里全是纯函数，不需要 Qt、不碰磁盘 —— 也正因如此值得测得细：`expect`/`actual`
是硬规则 3 的两个输入，算错任何一边的后果都不是崩溃，而是**日志从此说谎**
（真降级不报、或者每个正常任务都报降级，后者更糟：狼来了三次就没人看了）。

重点覆盖三件容易写错的事：

1. `no_match` 模式下 `writesubtitles` 是 **False**，但那恰恰是最该报 degraded 的场景
2. 正则回落模式（`en` → 实际 `en-GB`）不许产生假 `missing`
3. `container:` 只能从主输出路径推，不能从 DASH 中间流推
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("FLUENTYTDL_DATA_DIR_OVERRIDE", tempfile.mkdtemp(prefix="fytdl-art-"))

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402
from loguru import logger  # noqa: E402

from fluentytdl.models.subtitle_config import SUBTITLE_RESOLUTION_KEY  # noqa: E402
from fluentytdl.observability import (  # noqa: E402
    MEDIA,
    SUBTITLE_ANY,
    THUMBNAIL,
    TaskTrace,
    audio_langs_from_opts,
    container_token,
    emit_actual,
    emit_expect,
    expect_fields,
    expected_artifacts,
    found_artifacts,
    subtitle_token,
)


@pytest.fixture
def events():
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


# ── 词汇表本身 ──────────────────────────────────────────────


def test_subtitle_token_keeps_real_keys():
    assert subtitle_token("zh-Hans") == "subtitle:zh-Hans"
    assert subtitle_token("en_US") == "subtitle:en_US"
    assert subtitle_token(" pt-BR ") == "subtitle:pt-BR"


@pytest.mark.parametrize("value", ["", "   ", "all", "ALL", "en(-.+)?", "-live_chat", "*"])
def test_subtitle_token_falls_back_to_any_when_not_concrete(value):
    """指不出具体语言的输入必须收敛到 `subtitle:any`。

    否则 `subtitle:en(-.+)?` 这种 token 会进期望集合，而实际侧永远产不出同样的字符串
    —— 一个永远无法被满足的期望 = 永久假降级。
    """
    assert subtitle_token(value) == SUBTITLE_ANY


def test_container_token_normalizes():
    assert container_token(".MP4") == "container:mp4"
    assert container_token("mkv") == "container:mkv"


# ── 期望侧 ──────────────────────────────────────────────────


def test_expected_media_and_container():
    expected = expected_artifacts({"merge_output_format": "mp4"})
    assert expected == {MEDIA, "container:mp4"}


def test_skip_download_expects_no_media():
    """纯字幕 / 纯封面模式不该期望主媒体。"""
    expected = expected_artifacts(
        {"skip_download": True, "merge_output_format": "mp4", "writethumbnail": True}
    )
    assert expected == {THUMBNAIL}


@pytest.mark.parametrize(
    "opts",
    [
        {"merge_output_format": "mp4/mkv"},  # 候选链：期望本身不是单一值
        {"merge_output_format": "mp4", "extractaudio": True},  # 音频提取会整体改后缀
        {"merge_output_format": "mp4", "recodevideo": "webm"},
        {
            "merge_output_format": "mp4",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}],
        },
    ],
)
def test_container_expectation_abstains_when_ext_may_be_rewritten(opts):
    """拿不准就不断言 —— 少报一个 token 好过制造假不符。"""
    assert expected_artifacts(opts) == {MEDIA}


def test_thumbnail_expected_only_when_requested():
    assert THUMBNAIL in expected_artifacts({"writethumbnail": True})
    assert THUMBNAIL not in expected_artifacts({"writethumbnail": False})
    assert THUMBNAIL not in expected_artifacts({})


def test_exact_mode_expects_matched_and_missed_keys():
    """迟解析精确命中：命中的按真实键记，没命中的偏好也要记。

    没命中的那几条必须留在期望里 —— "你要的 ja 这个视频没有" 正是要让它出现在
    `missing` 里的信息，抹掉它日志就又变成"一切正常"。
    """
    opts = {
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["zh-Hans-en-GB"],
        SUBTITLE_RESOLUTION_KEY: {
            "mode": "exact",
            "prefs": ["zh-Hans", "ja"],
            "matched": ["zh-Hans-en-GB"],
            "missed": ["ja"],
            "available": ["en", "zh-Hans-en-GB"],
        },
    }
    assert expected_artifacts(opts) == {"subtitle:zh-Hans-en-GB", "subtitle:ja"}


def test_no_match_mode_still_expects_subtitles_despite_subs_disabled():
    """**头号回归点。**

    `build_no_match_opts()` 会把 `writesubtitles` / `writeautomaticsub` 显式关成
    False（视频确实没有匹配的字幕，开着只是白跑）。若按布尔判"用户要不要字幕"，
    这里就会得出空期望 → `degraded=False` → 日志宣布一切正常，而用户勾了中文字幕
    却一个字都没拿到。计划里那一行验证要求的正是 `degraded=True`。
    """
    opts = {
        "writesubtitles": False,
        "writeautomaticsub": False,
        "embedsubtitles": False,
        SUBTITLE_RESOLUTION_KEY: {
            "mode": "no_match",
            "prefs": ["zh-Hans"],
            "matched": [],
            "missed": ["zh-Hans"],
            "available": ["en", "ko"],
        },
    }
    assert "subtitle:zh-Hans" in expected_artifacts(opts)


def test_pattern_mode_expects_only_any():
    """正则回落：真实键未知（`en` 可能落成 `en-GB`），只能断言"至少一条"。"""
    opts = {
        "writesubtitles": True,
        "subtitleslangs": ["en(-.+)?"],
        SUBTITLE_RESOLUTION_KEY: {
            "mode": "pattern",
            "prefs": ["en"],
            "matched": [],
            "missed": [],
            "available": [],
        },
    }
    subs = {t for t in expected_artifacts(opts) if t.startswith("subtitle")}
    assert subs == {SUBTITLE_ANY}


def test_no_resolution_meta_falls_back_to_sub_langs():
    opts = {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["en", "-live_chat"]}
    # 排除项（`-` 前缀）不是请求项
    assert expected_artifacts(opts) == {"subtitle:en"}


def test_subtitles_off_expects_nothing():
    assert expected_artifacts({"skip_download": True, "writesubtitles": False}) == set()


def test_audio_langs_reads_the_injection_input():
    """读 `_fytdl_audio_langs` —— 和注入端同一个输入，两处不许分叉。

    `orig` 不算一个语言请求（原音是 `_fytdl_audio_strategy`，是策略）。
    以前这里读 `format_sort` 里的 `lang:xx`，那些条目在排序器里从来无效。
    """
    assert audio_langs_from_opts({"_fytdl_audio_langs": ["ja", "orig", "ja"]}) == ["ja"]
    assert audio_langs_from_opts({}) == []
    # 旧键不再被认：留着会让"请求了日语"与"没什么可注入的"同时出现在日志里
    assert audio_langs_from_opts({"format_sort": ["lang:ja"]}) == []


def test_expect_fields_flattens_resolution_meta():
    """字段必须是平铺的 —— JSONL 要能直接 `jq 'select(.sub_mode=="no_match")'`。"""
    fields = expect_fields(
        {
            "writesubtitles": True,
            "_fytdl_audio_langs": ["ja"],
            SUBTITLE_RESOLUTION_KEY: {
                "mode": "no_match",
                "prefs": ["zh-Hans"],
                "matched": [],
                "missed": ["zh-Hans"],
                "available": ["en"],
            },
        }
    )
    assert fields["sub_mode"] == "no_match"
    assert fields["sub_missed"] == ["zh-Hans"]
    assert fields["sub_available"] == ["en"]
    assert fields["audio_langs"] == ["ja"]
    # 音轨语言只作为字段出现，**不产 token**（验不了，见模块 docstring）
    assert not any("ja" in t for t in fields["artifacts"])


# ── 实际侧 ──────────────────────────────────────────────────


def test_found_classifies_by_extension():
    found = found_artifacts(
        ["/d/out/Title.en-GB.vtt", "/d/out/Title.webp"],
        output_path="/d/out/Title.mkv",
    )
    assert found == {MEDIA, "container:mkv", THUMBNAIL, SUBTITLE_ANY, "subtitle:en-GB"}


def test_found_always_records_any_alongside_concrete_subtitle():
    """实际集合是「找到了什么」的纯函数：有字幕就同时记 `any` 和具体语言。

    这是 `pattern` 模式不产生假 `missing` 的机制 —— 匹配逻辑不许渗进 `actual` 那侧。
    """
    found = found_artifacts(["/d/out/Title.en-GB.vtt"])
    assert SUBTITLE_ANY in found and "subtitle:en-GB" in found


def test_pattern_mode_end_to_end_is_not_degraded():
    """期望 `en`、实际拿到 `en-GB` —— 本项目最常见的正常情形，不许判降级。"""
    opts = {
        "merge_output_format": "mkv",
        "writesubtitles": True,
        "subtitleslangs": ["en(-.+)?"],
        SUBTITLE_RESOLUTION_KEY: {"mode": "pattern", "prefs": ["en"]},
    }
    expected = expected_artifacts(opts)
    found = found_artifacts(["/d/out/Title.en-GB.vtt"], output_path="/d/out/Title.mkv")
    assert expected - found == set()


def test_container_comes_only_from_output_path():
    """DASH 中间流不许参与容器判定。

    `Title.f399.mp4` + `Title.f251.webm` 都在 `dest_paths` 里；让它们参与的话，
    "要 mp4 却合成了 mkv" 会被中间流那个 `.mp4` 掩盖成匹配 —— 正好瞒掉要查的东西。
    """
    found = found_artifacts(
        ["/d/out/Title.f399.mp4", "/d/out/Title.f251.webm"],
        output_path="/d/out/Title.mkv",
    )
    assert "container:mkv" in found
    assert "container:mp4" not in found
    assert "container:webm" not in found


def test_sidecar_files_are_not_media():
    """`.info.json` 满足不了"主文件到手了" —— 那恰恰是下载失败时最爱留下的东西。"""
    assert found_artifacts(["/d/out/Title.info.json", "/d/out/Title.mp4.part"]) == set()


def test_subtitle_lang_survives_dots_in_title():
    assert "subtitle:en" in found_artifacts(["/d/out/S01.E02.en.srt"])


def test_subtitle_without_lang_segment_only_yields_any():
    found = found_artifacts(["/d/out/Title.vtt"])
    assert found == {SUBTITLE_ANY}


# ── 与 TaskTrace 的联动（硬规则 3）───────────────────────────


def _trace() -> TaskTrace:
    return TaskTrace(task_id="42", stage="download")


def test_emit_expect_feeds_trace(events):
    """只 emit 不喂 trace，日志里有期望而 `outcome` 永远 `degraded=false`。"""
    trace = _trace()
    emit_expect({"merge_output_format": "mp4", "writethumbnail": True}, trace=trace)
    assert trace.expected_artifacts == {MEDIA, "container:mp4", THUMBNAIL}
    assert [e["kind"] for e in events] == ["expect"]
    assert events[0]["stage"] == "select"


def test_emit_actual_reports_missing_and_marks_degraded(events):
    trace = _trace()
    emit_expect(
        {
            "merge_output_format": "mkv",
            SUBTITLE_RESOLUTION_KEY: {"mode": "no_match", "prefs": ["zh-Hans"], "missed": []},
        },
        trace=trace,
    )
    emit_actual([], trace=trace, output_path="/d/out/Title.mkv")

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["matched"] is False
    assert actual["missing"] == ["subtitle:zh-Hans"]
    assert trace.degraded is True
    assert trace.missing == ["subtitle:zh-Hans"]


def test_missing_artifacts_are_logged_at_warning():
    """缺产物必须是 WARNING。

    落 INFO 的话，这类"任务成功了但结果不对"只有翻文件 sink 才看得到 —— 而它正是
    整轮重构要抓的那一类。断言 loguru record 的等级，不是事件字段：`level` 是 sink
    的路由依据，不进 `as_dict()`。
    """
    levels: list[str] = []
    sink_id = logger.add(
        lambda m: levels.append(m.record["level"].name),
        level="TRACE",
        filter=lambda record: record.get("extra", {}).get("fytdl", {}).get("kind") == "actual",
    )
    try:
        degraded = _trace()
        degraded.expect_artifacts({THUMBNAIL})
        emit_actual([], trace=degraded)

        clean = _trace()
        clean.expect_artifacts({THUMBNAIL})
        emit_actual(["/d/out/Title.webp"], trace=clean)
    finally:
        logger.remove(sink_id)

    assert levels == ["WARNING", "INFO"]


def test_emit_actual_matched_when_everything_arrived(events):
    trace = _trace()
    emit_expect({"merge_output_format": "mkv", "writethumbnail": True}, trace=trace)
    emit_actual(["/d/out/Title.webp"], trace=trace, output_path="/d/out/Title.mkv")

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["matched"] is True
    assert actual["missing"] == []
    assert trace.degraded is False


def test_unexpected_artifacts_do_not_count_as_degraded(events):
    """多拿到东西不是降级（硬规则 3 的方向性），但要能查到。"""
    trace = _trace()
    emit_expect({"merge_output_format": "mkv"}, trace=trace)
    emit_actual(["/d/out/Title.webp"], trace=trace, output_path="/d/out/Title.mkv")

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["unexpected"] == [THUMBNAIL]
    assert trace.degraded is False


def test_thumbnail_not_requested_and_absent_is_not_degraded(events):
    """计划里硬规则 3 的那一行验证：用户没勾封面 + 封面不存在 → `degraded is False`。

    被减数是**用户请求的**，不是"系统支持的"。
    """
    trace = _trace()
    emit_expect({"merge_output_format": "mp4", "writethumbnail": False}, trace=trace)
    emit_actual([], trace=trace, output_path="/d/out/Title.mp4")
    assert trace.degraded is False


def test_actual_field_shape_is_constant(events):
    """`jq 'select(.kind=="actual") | .missing'` 不该时有时无。"""
    trace = _trace()
    emit_actual([], trace=trace)
    actual = next(e for e in events if e["kind"] == "actual")
    for key in ("matched", "expected", "actual", "missing", "unexpected"):
        assert key in actual


def test_emit_survives_garbage_opts(events):
    """硬规则 5：观测失败绝不回传业务层。"""

    class _Hostile(dict):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("boom")

    trace = _trace()
    assert emit_expect(_Hostile(), trace=trace) == set()
    assert emit_actual([object()], trace=trace) is not None  # type: ignore[arg-type]


def test_emit_works_without_trace(events):
    """GUI 线程上 `current_flow()` 恒为 None，缺 trace 也必须照发。"""
    emit_expect({"merge_output_format": "mp4"}, trace=None)
    assert [e["kind"] for e in events] == ["expect"]
