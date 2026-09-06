"""严格画质档位（`bestvideo[height=N]`）的降档推算。

这套逻辑原先长在 `ui/components/home/download_card.py` 上（`_next_lower_height` /
`_apply_height_preset_to_opts`）。那个卡片组件在列表虚拟化之后就再没人实例化过，
于是「预设 1080p，但这个视频只有 720p」的**自动降档重试**入口跟着一起消失了 ——
delegate 从来没有重新实现它。这里把它抽成纯函数：

* 没有 Qt 依赖，可以单元测试（`tests/test_quality_presets.py`）；
* 「档位怎么降」和「谁来弹窗、谁来重建 worker」分开，后者是 UI / controller 的事。

**严格档位（`height=`）与上限档位（`height<=`）不是一回事**：只有前者会因为片源缺档
而直接失败，也只有前者需要降档。`selection_dialog` 写入 `__fluentytdl_quality_height`
时用的正是严格档位（`#L3345-L3370`），所以这个键的存在即「这是严格档位任务」。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# 与 `selection_dialog` 的 `height_map`（2160p(严格) … 360p(严格)）一一对应。
# 顺序即降档顺序，不要改成升序。
HEIGHT_LADDER: tuple[int, ...] = (2160, 1440, 1080, 720, 480, 360)

# 只改严格等号档位。`height<=720` 里 `t` 和 `=` 之间隔着 `<`，所以这个模式天然
# 匹配不到上限档位，不需要额外的 lookbehind。
_STRICT_HEIGHT_RE = re.compile(r"height=\d+")


def current_preset_height(ydl_opts: Mapping[str, Any] | None) -> int | None:
    """读出任务当初选的严格档位；不是严格档位任务就返回 None。"""
    if not ydl_opts:
        return None
    raw = ydl_opts.get("__fluentytdl_quality_height")
    if raw is None:
        return None
    try:
        height = int(raw)
    except (TypeError, ValueError):
        # 用户机器上的历史 opts，别假设这里一定是数字
        return None
    return height if height > 0 else None


def next_lower_height(current: int) -> int | None:
    """比 `current` 低一档的预设高度；已经是最低档（或更低）时返回 None。

    `current` 不在阶梯上（例如手动填过 900）时退回「阶梯里第一个比它低的档位」，
    而不是直接放弃 —— 否则这种任务永远降不了档。
    """
    try:
        i = HEIGHT_LADDER.index(int(current))
    except (TypeError, ValueError):
        lower = [h for h in HEIGHT_LADDER if h < current]
        return lower[0] if lower else None
    return HEIGHT_LADDER[i + 1] if i + 1 < len(HEIGHT_LADDER) else None


def downgrade_format_expr(fmt: str, new_height: int) -> str:
    """把 format 表达式里的严格高度全部换成 `new_height`。

    原实现是无条件覆写成 `bestvideo[height=N]+bestaudio/best`，这会**丢掉**用户在
    选择对话框里挑的音轨 id 和 video-only 任务的 `[acodec=none]` 分支
    （`selection_dialog#L3345-L3370` 会生成这两种形状）。改成就地替换数字，
    表达式的其余部分原样保留；实在找不到严格档位时才退回那个兜底表达式。

    仍然保持严格等号：降完一档若还是拿不到，下一次错误会再触发一次降档，
    阶梯才走得下去。
    """
    if fmt:
        replaced, n = _STRICT_HEIGHT_RE.subn(f"height={new_height}", fmt)
        if n:
            return replaced
    return f"bestvideo[height={new_height}]+bestaudio/best"


def downgraded_opts(ydl_opts: Mapping[str, Any], new_height: int) -> dict[str, Any]:
    """返回降档后的 opts **副本**（不改入参）。

    只动 `format` 和 `__fluentytdl_quality_height` 两个键：输出目录、字幕、
    后处理设置都得原样带过去，否则「降一档重试」会顺手把用户其他设置也重置掉。
    """
    new_opts = dict(ydl_opts)
    new_opts["format"] = downgrade_format_expr(str(new_opts.get("format") or ""), new_height)
    new_opts["__fluentytdl_quality_height"] = new_height
    return new_opts
