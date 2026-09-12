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
    DELIVERED_MEDIA,
    DELIVERED_PREFIX,
    DELIVERED_THUMBNAIL,
    EMBEDDED_SUBTITLE,
    EMBEDDED_THUMBNAIL,
    MEDIA,
    SUBTITLE_ANY,
    THUMBNAIL,
    TaskTrace,
    audio_langs_from_opts,
    container_token,
    delivery_tokens,
    embed_tokens,
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


def _trace() -> TaskTrace:
    return TaskTrace(task_id="42", stage="download")


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
    """主媒体一定要交到用户手上，所以 `delivered:media` 与 `media` 同生。

    `container:*` 刻意**不**进 delivery —— 容器是主文件的属性，不是一个交付物。
    """
    expected = expected_artifacts({"merge_output_format": "mp4"})
    assert expected == {MEDIA, DELIVERED_MEDIA, "container:mp4"}


def test_skip_download_expects_no_media():
    """纯字幕 / 纯封面模式不该期望主媒体 —— 连带也不该期望 `delivered:media`。"""
    expected = expected_artifacts(
        {"skip_download": True, "merge_output_format": "mp4", "writethumbnail": True}
    )
    assert expected == {THUMBNAIL, DELIVERED_THUMBNAIL}


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
    assert expected_artifacts(opts) == {MEDIA, DELIVERED_MEDIA}


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
    assert expected_artifacts(opts) == {
        "subtitle:zh-Hans-en-GB",
        "subtitle:ja",
        "delivered:subtitle:zh-Hans-en-GB",
        "delivered:subtitle:ja",
    }


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
    assert expected_artifacts(opts) == {"subtitle:en", "delivered:subtitle:en"}


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
    """期望 `en`、实际拿到 `en-GB` —— 本项目最常见的正常情形，不许判降级。

    交付侧一起喂：加了 `delivered:*` 之后光有 `found_artifacts()` 已经不够，
    而这恰恰是**语言粒度规则两侧必须一致**的地方 —— `delivery_tokens()` 复用
    `found_artifacts()` 就是为了让 `delivered:subtitle:any` 也自动带出来。
    """
    opts = {
        "merge_output_format": "mkv",
        "writesubtitles": True,
        "subtitleslangs": ["en(-.+)?"],
        SUBTITLE_RESOLUTION_KEY: {"mode": "pattern", "prefs": ["en"]},
    }
    delivered = ["/d/out/Title.mkv", "/d/out/Title.en-GB.vtt"]
    expected = expected_artifacts(opts)
    found = found_artifacts(["/d/out/Title.en-GB.vtt"], output_path="/d/out/Title.mkv")
    assert expected - (found | delivery_tokens(delivered)) == set()


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


# ── 三层事实：`embedded:*`（取得≠嵌入）与 `delivered:*`（取得≠交付）────


def test_embedded_expected_from_the_embed_switches():
    """请求过嵌入就要有落点 —— 否则「外挂文件都在但容器里没有字幕轨」推不出任何 missing。"""
    expected = expected_artifacts({"embedsubtitles": True, "embedthumbnail": True})
    assert EMBEDDED_SUBTITLE in expected
    assert EMBEDDED_THUMBNAIL in expected
    assert expected_artifacts({}) & {EMBEDDED_SUBTITLE, EMBEDDED_THUMBNAIL} == set()


def test_embed_tokens_come_from_structured_evidence():
    """实际来源是 `manifest.embed_evidence`，不是 opts 里那个开关、也不是 rc。"""
    assert embed_tokens({"subtitle", "thumbnail"}) == {EMBEDDED_SUBTITLE, EMBEDDED_THUMBNAIL}
    assert embed_tokens(set()) == set()
    assert embed_tokens(["", "  ", None]) == set()  # type: ignore[list-item]


def test_delivery_tokens_reuse_the_acquisition_vocabulary():
    """语言键的粒度规则两侧必须自动一致 —— 所以内部复用 `found_artifacts()`。

    认得出语言就同时记 `any` 和具体键，认不出就只有 `any`；两边各写一遍分支的话，
    正则回落模式下取得侧记 `subtitle:any` 而交付侧记 `delivered:subtitle:en-GB`，
    期望永远配不上，又是一个假降级。
    """
    assert delivery_tokens(["/u/Title.mkv", "/u/Title.en-GB.srt", "/u/Title.jpg"]) == {
        "delivered:media",
        "delivered:subtitle:any",
        "delivered:subtitle:en-GB",
        "delivered:thumbnail",
    }
    assert delivery_tokens(["/u/Title.vtt"]) == {"delivered:subtitle:any"}
    assert delivery_tokens([]) == set()


def test_delivery_never_talks_about_containers():
    """容器是主文件的**属性**，不是一个交付物；"交付了 mp4 容器"没有意义。"""
    tokens = delivery_tokens(["/u/Title.mp4"])
    assert tokens == {"delivered:media"}
    assert not any(t.startswith(f"{DELIVERED_PREFIX}container") for t in tokens)


def test_delivery_expectation_hangs_on_the_keep_flags():
    """交付期望挂在「另存」开关上，不是挂在 `writesubtitles` / `writethumbnail` 上。

    嵌入成功且用户没要外挂时，字幕本来就不该出现在用户目录里 —— 期望它就是制造假降级。
    """
    base = {
        "skip_download": True,
        "writesubtitles": True,
        "subtitleslangs": ["ja"],
        "writethumbnail": True,
    }
    kept = expected_artifacts({**base})
    assert "delivered:subtitle:ja" in kept and DELIVERED_THUMBNAIL in kept

    dropped = expected_artifacts(
        {**base, "__fluentytdl_keep_subtitle": False, "__fluentytdl_keep_thumbnail": False}
    )
    assert not any(t.startswith(DELIVERED_PREFIX) for t in dropped)
    # 取得侧不受影响：字幕/封面照样该被拿到，只是不该交到用户目录
    assert {"subtitle:ja", THUMBNAIL} <= dropped


def test_missing_keep_flag_still_expects_delivery():
    """硬约束 2：保留标志缺失 ⇒ 保留。所以缺省也要期望交付。"""
    expected = expected_artifacts(
        {
            "skip_download": True,
            "writesubtitles": True,
            "subtitleslangs": ["ja"],
            "writethumbnail": True,
        }
    )
    assert "delivered:subtitle:ja" in expected
    assert DELIVERED_THUMBNAIL in expected


def test_embed_failure_is_the_only_thing_missing(events):
    """空洞一：外挂文件都在、字幕不缺，可容器里没有字幕轨。

    没有 `embedded:*` 的话 `expected − actual` 是空集 —— 日志宣布一切正常。
    """
    trace = _trace()
    emit_expect(
        {
            "merge_output_format": "mkv",
            "writesubtitles": True,
            "subtitleslangs": ["en"],
            "embedsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
        },
        trace=trace,
    )
    emit_actual(
        ["/sandbox/payload/Title.en.vtt"],
        trace=trace,
        output_path="/sandbox/payload/Title.mkv",
        extra_actual=delivery_tokens(["/u/Title.mkv"]),  # 嵌入证据为空
    )

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["missing"] == [EMBEDDED_SUBTITLE]
    assert trace.degraded is True


def test_physical_loss_becomes_degraded_through_the_delivery_layer(events):
    """空洞二：报告过创建、随后文件没了 —— `actual` 按契约仍有它，`delivered:*` 没有。

    `actual` 的判据是"报告过创建"（嵌进去了别误报成没拿到），那条契约不能为此破坏；
    所以存在性搬到交付侧表达，两件事各有各的 token。
    """
    trace = _trace()
    emit_expect(
        {"skip_download": True, "writesubtitles": True, "subtitleslangs": ["ja"]}, trace=trace
    )
    emit_actual(
        ["/sandbox/payload/Title.ja.vtt"],  # 报告过创建，历史事实不丢
        trace=trace,
        extra_actual=delivery_tokens([]),  # journal 里没有 published 成员
    )

    actual = next(e for e in events if e["kind"] == "actual")
    assert "subtitle:ja" in actual["actual"], "历史事实不许因为文件没了就被抹掉"
    assert actual["missing"] == ["delivered:subtitle:ja"]
    assert trace.degraded is True


def test_successful_embed_without_external_is_not_degraded(events):
    """上一条的对照组：嵌入成功 + 用户没要外挂 ⇒ 不期望 `delivered:subtitle:*`。

    加这组 token 最大的风险就是制造假降级，这条守的正是那一面。
    """
    trace = _trace()
    emit_expect(
        {
            "merge_output_format": "mkv",
            "writesubtitles": True,
            "subtitleslangs": ["ja"],
            "embedsubtitles": True,
            "__fluentytdl_keep_subtitle": False,
        },
        trace=trace,
    )
    emit_actual(
        ["/sandbox/payload/Title.ja.vtt"],
        trace=trace,
        output_path="/sandbox/payload/Title.mkv",
        extra_actual=delivery_tokens(["/u/Title.mkv"]) | embed_tokens({"subtitle"}),
    )

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["missing"] == []
    assert trace.degraded is False


def test_extra_actual_is_optional_and_hostile_input_is_survivable(events):
    """`extra_actual` 缺省 = 老行为；硬规则 5 在这条路上同样成立。"""
    trace = _trace()
    assert emit_actual(["/d/out/Title.mp4"], trace=trace) == {MEDIA}

    class _Hostile:
        def __iter__(self):
            raise RuntimeError("boom")

    assert emit_actual(["/d/out/Title.mp4"], trace=trace, extra_actual=_Hostile()) == set()


# ── 与 TaskTrace 的联动（硬规则 3）───────────────────────────


def test_emit_expect_feeds_trace(events):
    """只 emit 不喂 trace，日志里有期望而 `outcome` 永远 `degraded=false`。"""
    trace = _trace()
    emit_expect({"merge_output_format": "mp4", "writethumbnail": True}, trace=trace)
    assert trace.expected_artifacts == {
        MEDIA,
        DELIVERED_MEDIA,
        "container:mp4",
        THUMBNAIL,
        DELIVERED_THUMBNAIL,
    }
    assert [e["kind"] for e in events] == ["expect"]
    assert events[0]["stage"] == "select"


def test_emit_actual_reports_missing_and_marks_degraded(events):
    """主媒体交付了、字幕一条都没有 —— 两层各缺一个，两个 token 都得进 `missing`。"""
    trace = _trace()
    emit_expect(
        {
            "merge_output_format": "mkv",
            SUBTITLE_RESOLUTION_KEY: {"mode": "no_match", "prefs": ["zh-Hans"], "missed": []},
        },
        trace=trace,
    )
    emit_actual(
        [],
        trace=trace,
        output_path="/d/out/Title.mkv",
        extra_actual=delivery_tokens(["/d/out/Title.mkv"]),
    )

    missing = ["delivered:subtitle:zh-Hans", "subtitle:zh-Hans"]
    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["matched"] is False
    assert actual["missing"] == missing
    assert trace.degraded is True
    assert trace.missing == missing


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
    emit_actual(
        ["/d/out/Title.webp"],
        trace=trace,
        output_path="/d/out/Title.mkv",
        extra_actual=delivery_tokens(["/u/Title.mkv", "/u/Title.webp"]),
    )

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["matched"] is True
    assert actual["missing"] == []
    assert trace.degraded is False


def test_unexpected_artifacts_do_not_count_as_degraded(events):
    """多拿到东西不是降级（硬规则 3 的方向性），但要能查到。"""
    trace = _trace()
    emit_expect({"merge_output_format": "mkv"}, trace=trace)
    emit_actual(
        ["/d/out/Title.webp"],
        trace=trace,
        output_path="/d/out/Title.mkv",
        extra_actual=delivery_tokens(["/u/Title.mkv"]),
    )

    actual = next(e for e in events if e["kind"] == "actual")
    assert actual["unexpected"] == [THUMBNAIL]
    assert trace.degraded is False


def test_thumbnail_not_requested_and_absent_is_not_degraded(events):
    """计划里硬规则 3 的那一行验证：用户没勾封面 + 封面不存在 → `degraded is False`。

    被减数是**用户请求的**，不是"系统支持的"。
    """
    trace = _trace()
    emit_expect({"merge_output_format": "mp4", "writethumbnail": False}, trace=trace)
    emit_actual(
        [],
        trace=trace,
        output_path="/d/out/Title.mp4",
        extra_actual=delivery_tokens(["/u/Title.mp4"]),
    )
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
