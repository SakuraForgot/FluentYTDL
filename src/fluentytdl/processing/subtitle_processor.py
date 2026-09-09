"""
字幕后处理器

职责收缩成一件事：**校验调用方交来的字幕文件，并把结果说清楚**。

- 完整性校验（空文件、编码、SRT 时间码）
- 结构化结果，让"字幕开了却一个都没有"必定说得出原因

## 定位逻辑为什么整体退休了

以前这里有四级定位（精确路径 → 语言模板 → 前缀扫描 → 同目录兜底），因为"这个任务
产出了哪些字幕"从来没有一个单一表示，只能反复猜。四级里有两级是**扫目录**，而调用方
（`features.SubtitleFeature`）在内嵌模式下会删掉这里返回的文件 —— 于是第 4 级
`LOCATE_DIR_SCAN` 会把**目录里所有字幕**交给删除逻辑，删除范围可以远大于本任务产出。
那正是《下载产物事务层》诊断 C 点名的"误删"根因之一。

现在角色只在进入 `Manifest` 时判定一次（由 yt-dlp 的 `Writing video subtitles to:`
声明，或由 `reconcile()` 做唯一一次集中式兜底分类），调用方直接传
`manifest.kept("subtitle")` 进来。这个模块不再有"找"这个动作，也就不可能找错。

保留下来的是它真正的价值：校验、`invalid_files`、以及那套让 UI 能说人话的 `reason`。

两个历史坑（`FluentYTDL-字幕下载问题排查报告.md`）：

1. 定位只有一招 `parent_dir.glob(f"{stem}.*")`，`stem` 未转义 —— YouTube 标题里
   遍地都是的 `[` `]` `?` 会被 glob 当通配符，直接失配。**清单化之后这个坑不复存在**：
   路径是被报告的，不是被拼出来的。
2. 找不到时返回 `success=True, message="未找到字幕文件"` —— UI 侧什么都不显示，
   用户看到的就是"字幕开了但没有字幕"，且无从判断是没匹配上语言、被限速、
   还是文件名对不上。这一条仍然由 `reason=not_found` 守着。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.logger import logger
from .subtitle_manager import SUBTITLE_FORMATS

# 后缀白名单唯一来源就是 `SUBTITLE_FORMATS`。这里以前另写了一份
# `[".srt", ".ass", ".vtt"]` 的字面量，`.lrc` 就是那么丢的。
SUBTITLE_SUFFIXES = frozenset(f".{ext}" for ext in SUBTITLE_FORMATS)


@dataclass
class SubtitleProcessResult:
    """字幕后处理结果"""

    success: bool
    message: str
    processed_files: list[str]  # 校验通过的字幕文件路径

    reason: str | None = None
    """失败/跳过原因码，供上层组织用户可见文案：

    - `disabled` —— 本次任务没开字幕（`success=True`，纯跳过）
    - `video_missing` —— 视频文件不存在，无从关联字幕
    - `not_found` —— 调用方一个字幕路径都没给出（或给出的都已不在盘上）：
      yt-dlp 一个字幕文件都没写出来
    - `all_invalid` —— 文件在，但全部校验失败（空文件 / 编码坏 / 缺时间码）
    """

    invalid_files: list[tuple[str, str]] = field(default_factory=list)
    """`(路径, 原因)`。内嵌模式下这些残骸也要一起清掉。"""


class SubtitleProcessor:
    """字幕后处理器 - 单例"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def process(
        self,
        output_path: str | None,
        subtitle_paths: Iterable[str] | None,
        opts: dict[str, Any],
        status_callback: Callable[[str], None] | None = None,
    ) -> SubtitleProcessResult:
        """
        校验本任务产出的字幕文件

        Args:
            output_path: 视频输出路径。**只用来判断"视频本身在不在"** —— 字幕不再
                从它的 stem 上拼名字，所以它是不是分片文件已经无关紧要
            subtitle_paths: 本任务产出的字幕文件路径，由调用方从清单里取
                （`manifest.kept("subtitle")`）。这里只做后缀过滤、去重、存在性检查，
                **不扫任何目录** —— 角色已经在进入清单时判定过一次了
            opts: yt-dlp 选项字典；这里只读 `writesubtitles` / `writeautomaticsub`
                判断本次是否开了字幕
            status_callback: 状态回调，用于把结果冒到 UI

        Returns:
            SubtitleProcessResult: 处理结果
        """

        def _notify(msg: str) -> None:
            if status_callback is None:
                return
            try:
                status_callback(msg)
            except Exception:
                # UI 侧回调抛异常不该把字幕后处理带走
                logger.exception("字幕状态回调异常")

        logger.info("字幕后处理开始 - output_path={}", output_path)

        # 检查是否启用了字幕下载
        if not opts.get("writesubtitles") and not opts.get("writeautomaticsub"):
            logger.debug("字幕下载未启用，跳过后处理")
            return SubtitleProcessResult(
                success=True,
                message="字幕下载未启用",
                processed_files=[],
                reason="disabled",
            )

        if not output_path or not os.path.exists(output_path):
            logger.warning("视频文件不存在，无法进行字幕后处理: {}", output_path)
            return SubtitleProcessResult(
                success=False,
                message="视频文件不存在",
                processed_files=[],
                reason="video_missing",
            )

        # 1. 过滤调用方给的候选（后缀白名单 + 去重 + 存在性）
        #    先物化一次：调用方给的可能是生成器（`kept()` 的推导式），而下面的
        #    not_found 分支还要报候选数量 —— 迭代两次会把它报成 0。
        candidates = [str(p) for p in (subtitle_paths or ())]
        subtitle_files = self._pick_subtitles(Path(p) for p in candidates)

        if not subtitle_files:
            # 清单里没有字幕：yt-dlp 确实没写出任何字幕文件。原因（语言没匹配上 / 限速 /
            # 需要 PO Token）由调用方结合 `__fluentytdl_subtitle_resolution` 解释，
            # 这里只负责如实上报"一个都没有"。
            logger.warning(
                "未找到字幕文件（清单里没有字幕产物）: video={} candidates={}",
                Path(output_path).name,
                len(candidates),
            )
            return SubtitleProcessResult(
                success=False,
                message="未找到字幕文件",
                processed_files=[],
                reason="not_found",
            )

        logger.info("清单里有 {} 个字幕文件，开始校验", len(subtitle_files))

        # 2. 验证字幕文件完整性
        processed_files: list[str] = []
        invalid_files: list[tuple[str, str]] = []
        for sub_file in subtitle_files:
            is_valid, reason = self._validate_subtitle_file(sub_file)
            if is_valid:
                processed_files.append(str(sub_file))
                logger.info("✓ 字幕文件有效: {}", sub_file.name)
            else:
                invalid_files.append((str(sub_file), reason))
                logger.warning("✗ 字幕文件无效: {} - {}", sub_file.name, reason)
                _notify(f"⚠️ 字幕文件异常: {sub_file.name}（{reason}）")

        # 3. 返回处理结果
        if not processed_files:
            return SubtitleProcessResult(
                success=False,
                message=f"{len(invalid_files)} 个字幕文件全部校验失败",
                processed_files=[],
                reason="all_invalid",
                invalid_files=invalid_files,
            )

        _notify(f"[字幕处理] ✓ 已就绪 {len(processed_files)} 个字幕文件")
        return SubtitleProcessResult(
            success=True,
            message=f"成功处理 {len(processed_files)} 个字幕文件",
            processed_files=processed_files,
            invalid_files=invalid_files,
        )

    @staticmethod
    def _pick_subtitles(paths: Iterable[Path]) -> list[Path]:
        """从候选路径里挑出真实存在的字幕文件，按平台规则去重后定序。

        **纯函数，不列目录。** 清单里可能混着 media / thumbnail（调用方要是把
        `kept()` 整个传进来），所以后缀白名单仍然要过一遍；`presence` 与实际磁盘之间
        也可能有时间差（杀软隔离），所以存在性也仍然要查一遍 —— 查到不在就是少一个
        字幕，不是去别处找。
        """
        picked: dict[str, Path] = {}
        for path in paths:
            if path.suffix.lower() not in SUBTITLE_SUFFIXES:
                continue
            try:
                if not path.is_file():
                    continue
                key = os.path.normcase(str(path.resolve()))
            except OSError:
                continue
            picked.setdefault(key, path)
        return sorted(picked.values(), key=lambda p: p.name)

    def _validate_subtitle_file(self, subtitle_path: Path) -> tuple[bool, str]:
        """
        验证字幕文件完整性

        Returns:
            (is_valid, reason)
        """
        if not subtitle_path.exists():
            return False, "文件不存在"

        if subtitle_path.stat().st_size == 0:
            return False, "文件大小为 0"

        try:
            # 尝试读取文件内容（检查编码和基本格式）
            content = subtitle_path.read_text(encoding="utf-8")

            if len(content.strip()) == 0:
                return False, "文件内容为空"

            # 基本格式检查 (SRT 格式应该包含时间码)
            if subtitle_path.suffix.lower() == ".srt":
                if "-->" not in content:
                    return False, "SRT 格式缺少时间码"

            return True, "文件有效"

        except UnicodeDecodeError:
            return False, "编码错误"
        except Exception as e:
            return False, f"读取失败: {str(e)}"


# 单例实例
subtitle_processor = SubtitleProcessor()
