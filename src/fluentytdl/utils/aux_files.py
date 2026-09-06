"""与主文件同名的附属产物（封面 / 字幕 / 歌词）。

「彻底删除」一个任务时它们必须跟着走，否则用户删掉任务后目录里还剩一堆
`Title.en-GB.vtt` / `Title.webp`。

收到这里之前有两个毛病：

1. 后缀清单有两份副本（`core/controller.py` 与 `download/workers.py:885-896`），
   「加一种字幕格式」得改两个地方，漏掉哪个只表现成"有时删不干净"。
2. 删除侧用 `base_name + ext` 精确拼接，**永远匹配不到真实的字幕文件名** ——
   yt-dlp 写出来的是 `Title.en-GB.vtt`，主名和后缀之间夹着语言代码。
   见 `FluentYTDL-字幕下载问题排查报告.md`。
"""

from __future__ import annotations

import os

#: 封面图。
IMAGE_EXTS: tuple[str, ...] = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)

#: 字幕 / 歌词。必须覆盖 `processing.subtitle_manager.SUBTITLE_FORMATS` 的每一项，
#: 由 `tests/test_aux_files.py` 机械保证 —— 这里不能反向 import：`utils/` 是底层，
#: 而 `subtitle_manager` 依赖 PySide6。
SUBTITLE_EXTS: tuple[str, ...] = (
    ".srt",
    ".ass",
    ".vtt",
    ".lrc",
)

# 分成两组是因为**清理和分类是两件事**：删任务时只关心"是不是附属产物"（合起来用），
# 而 `observability/artifacts.py` 要把一个文件名判成 `thumbnail` 还是
# `subtitle:<lang>`（分开用）。两处共用这一份声明，加一种字幕格式仍然只改一个地方。
AUX_EXTS: tuple[str, ...] = IMAGE_EXTS + SUBTITLE_EXTS


def is_aux_name(name: str) -> bool:
    """文件名是否是附属产物（只看后缀，大小写不敏感）。"""
    return name.lower().endswith(AUX_EXTS)


def aux_files(final_path: str) -> list[str]:
    """列出与 `final_path` 同主名的附属文件（只返回真实存在的）。

    按目录扫描 + 前缀匹配，而不是拼接固定后缀：`Title.mkv` 的字幕叫
    `Title.en-GB.vtt`、`Title.zh-Hans-en-GB.srt`，语言代码是运行时才知道的。

    前缀要求（`Title.`）加后缀白名单一起限定范围：同名视频本体不会被当成附属产物，
    同目录里别的视频（`Another.webp`）也进不来。
    """
    if not final_path:
        return []

    directory, filename = os.path.split(final_path)
    stem = os.path.splitext(filename)[0]
    if not stem:
        return []

    try:
        entries = os.listdir(directory or ".")
    except OSError:
        return []

    prefix = os.path.normcase(stem + ".")
    found: list[str] = []
    for name in entries:
        if not is_aux_name(name) or not os.path.normcase(name).startswith(prefix):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            found.append(path)
    return sorted(found)
