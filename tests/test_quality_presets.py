"""严格画质档位降档推算的无头测试。

这套逻辑是从 `ui/components/home/download_card.py` 抢救出来的：那个卡片组件在列表
虚拟化之后再没被实例化过，于是「预设 1080p 但片源只有 720p」的自动降档重试入口
静默消失了很久。抽成纯函数之后就该有测试钉住它，尤其是三条容易写坏的性质：

1. **只动严格档位**：`height<=720`（上限档位）不该被改写 —— 它本来就能匹配到更低画质，
   降它一档等于莫名其妙地降低用户的上限；
2. **保留表达式其余部分**：原实现无条件覆写成 `bestvideo[height=N]+bestaudio/best`，
   会丢掉选择对话框挑的音轨 id 和 video-only 的 `[acodec=none]` 分支；
3. **opts 只改两个键**：输出目录、字幕、后处理设置必须原样带过去。

纯函数，不导入 Qt、不碰 DB。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest  # noqa: E402

from fluentytdl.utils.quality_presets import (  # noqa: E402
    HEIGHT_LADDER,
    current_preset_height,
    downgrade_format_expr,
    downgraded_opts,
    next_lower_height,
)


def test_ladder_is_descending_and_unique() -> None:
    """阶梯顺序即降档顺序 —— 写成升序会让「降一档」变成升一档。"""
    assert list(HEIGHT_LADDER) == sorted(HEIGHT_LADDER, reverse=True)
    assert len(set(HEIGHT_LADDER)) == len(HEIGHT_LADDER)


@pytest.mark.parametrize(
    ("current", "expected"),
    [(2160, 1440), (1440, 1080), (1080, 720), (720, 480), (480, 360)],
)
def test_next_lower_walks_the_ladder(current: int, expected: int) -> None:
    assert next_lower_height(current) == expected


def test_lowest_rung_has_no_next() -> None:
    """最低档必须返回 None，调用方靠它提示「已是最低档，请手动调整」。"""
    assert next_lower_height(HEIGHT_LADDER[-1]) is None


def test_off_ladder_height_falls_to_nearest_lower() -> None:
    """手填过 900 这类不在阶梯上的高度也要能降 —— 否则这种任务永远卡在失败态。"""
    assert next_lower_height(900) == 720
    assert next_lower_height(4320) == 2160
    assert next_lower_height(120) is None


@pytest.mark.parametrize(
    "opts",
    [
        {},
        {"format": "bestvideo+bestaudio/best"},
        {"__fluentytdl_quality_height": 0},
        {"__fluentytdl_quality_height": None},
        {"__fluentytdl_quality_height": "not-a-number"},
    ],
)
def test_non_strict_preset_reads_as_none(opts: dict) -> None:
    """`__fluentytdl_quality_height` 的存在即「这是严格档位任务」，其余一律 None。"""
    assert current_preset_height(opts) is None


def test_strict_preset_reads_through_strings() -> None:
    # 历史 ydl_opts 是 JSON 落库的，档位可能是字符串
    assert current_preset_height({"__fluentytdl_quality_height": 1080}) == 1080
    assert current_preset_height({"__fluentytdl_quality_height": "720"}) == 720
    assert current_preset_height(None) is None


def test_downgrade_preserves_audio_id() -> None:
    """选择对话框挑过音轨时 format 是 `bestvideo[height=N]+<id>`，那个 id 不能丢。"""
    assert downgrade_format_expr("bestvideo[height=1080]+251", 720) == "bestvideo[height=720]+251"


def test_downgrade_rewrites_every_strict_rung() -> None:
    """video-only 预设是三段回退表达式，三处高度要一起降。"""
    src = "bestvideo[height=1080][acodec=none]/bestvideo[height=1080]/bestvideo[acodec=none]/bestvideo"
    assert downgrade_format_expr(src, 720) == (
        "bestvideo[height=720][acodec=none]/bestvideo[height=720]/bestvideo[acodec=none]/bestvideo"
    )


@pytest.mark.parametrize("src", ["", "bv*[height<=1080]+ba/b[height<=1080]/b", "bestaudio/best"])
def test_downgrade_falls_back_when_no_strict_rung(src: str) -> None:
    """没有严格档位可改时退回兜底表达式，**绝不**去动 `height<=` 的上限档位。"""
    out = downgrade_format_expr(src, 720)
    assert out == "bestvideo[height=720]+bestaudio/best"
    assert "height<=1080" not in out


def test_downgraded_opts_touches_only_two_keys() -> None:
    original = {
        "format": "bestvideo[height=1080]+bestaudio/best",
        "__fluentytdl_quality_height": 1080,
        "outtmpl": "D:/out/%(title)s.%(ext)s",
        "writesubtitles": True,
        "postprocessors": [{"key": "FFmpegMetadata"}],
    }
    snapshot = dict(original)

    new_opts = downgraded_opts(original, 720)

    # 入参不许被改（调用方还要用它做失败回退 / 日志）
    assert original == snapshot
    assert new_opts["format"] == "bestvideo[height=720]+bestaudio/best"
    assert new_opts["__fluentytdl_quality_height"] == 720
    for key in ("outtmpl", "writesubtitles", "postprocessors"):
        assert new_opts[key] == original[key]


def test_full_ladder_walk_converges() -> None:
    """反复降档必须终止在最低档 —— 循环调用是 UI 的真实用法（错一次降一档）。"""
    opts = {"format": "bestvideo[height=2160]+bestaudio/best", "__fluentytdl_quality_height": 2160}
    seen = []
    while True:
        height = current_preset_height(opts)
        assert height is not None
        nxt = next_lower_height(height)
        if nxt is None:
            break
        opts = downgraded_opts(opts, nxt)
        seen.append(nxt)
        assert len(seen) <= len(HEIGHT_LADDER)  # 防死循环

    assert seen == list(HEIGHT_LADDER[1:])
    assert opts["format"] == f"bestvideo[height={HEIGHT_LADDER[-1]}]+bestaudio/best"
