"""
字幕后处理器

下载结束后确认字幕**真的落盘了**：
- 四级定位（精确路径 → 语言模板 → 前缀扫描 → 同目录兜底）
- 完整性校验（空文件、编码、SRT 时间码）
- 结构化结果，让"字幕开了却一个都没有"必定说得出原因

两个历史坑（`FluentYTDL-字幕下载问题排查报告.md`）：

1. 定位只有一招 `parent_dir.glob(f"{stem}.*")`，`stem` 未转义 —— YouTube 标题里
   遍地都是的 `[` `]` `?` 会被 glob 当通配符，直接失配；而 executor 早就把精确的
   `.vtt` 路径记进 `dest_paths` 了，这里一次都没查过。
2. 找不到时返回 `success=True, message="未找到字幕文件"` —— UI 侧什么都不显示，
   用户看到的就是"字幕开了但没有字幕"，且无从判断是没匹配上语言、被限速、
   还是文件名对不上。
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

# _find_subtitle_files 命中的定位级别，只用于日志与测试断言
LOCATE_DEST_PATHS = "dest_paths"
LOCATE_LANG_PATTERN = "lang_pattern"
LOCATE_PREFIX_SCAN = "prefix_scan"
LOCATE_DIR_SCAN = "dir_scan"
LOCATE_NONE = "none"


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
    - `not_found` —— 四级定位全空：yt-dlp 一个字幕文件都没写出来
    - `all_invalid` —— 文件在，但全部校验失败（空文件 / 编码坏 / 缺时间码）
    """

    located_by: str | None = None
    """哪一级定位命中的（`LOCATE_*`）。兜底级命中意味着文件名对不上，值得记一笔。"""

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
        opts: dict[str, Any],
        status_callback: Callable[[str], None] | None = None,
        *,
        dest_paths: Iterable[str] | None = None,
        allow_dir_scan: bool = False,
    ) -> SubtitleProcessResult:
        """
        执行字幕后处理

        Args:
            output_path: 视频输出路径（应当已被 `find_final_merged_file()` 纠正过）
            opts: yt-dlp 选项字典；这里读 `writesubtitles` / `writeautomaticsub`
                与 `subtitleslangs`（第 2 级定位的语言来源）
            status_callback: 状态回调，用于把结果冒到 UI。**以前一直没人调用它**
            dest_paths: executor 记录的实际产出路径（`on_file_created`），第 1 级定位
            allow_dir_scan: 是否允许第 4 级"同目录全扫"。**只有任务沙盒目录能开**，
                见 `_find_subtitle_files()` 里的说明

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

        video_path = Path(output_path)
        sub_langs = [str(x) for x in (opts.get("subtitleslangs") or [])]

        # 1. 查找字幕文件
        subtitle_files, located_by = self._find_subtitle_files(
            video_path,
            dest_paths=dest_paths,
            sub_langs=sub_langs,
            allow_dir_scan=allow_dir_scan,
        )

        if not subtitle_files:
            # 四级都空：yt-dlp 确实没写出任何字幕文件。原因（语言没匹配上 / 限速 /
            # 需要 PO Token）由调用方结合 `__fluentytdl_subtitle_resolution` 解释，
            # 这里只负责如实上报"一个都没有"。
            logger.warning(
                "未找到字幕文件（四级定位全空）: video={} sub_langs={} dir_scan={}",
                video_path.name,
                sub_langs,
                allow_dir_scan,
            )
            return SubtitleProcessResult(
                success=False,
                message="未找到字幕文件",
                processed_files=[],
                reason="not_found",
            )

        logger.info("找到 {} 个字幕文件（定位方式: {}）", len(subtitle_files), located_by)

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
                located_by=located_by,
                invalid_files=invalid_files,
            )

        _notify(f"[字幕处理] ✓ 已就绪 {len(processed_files)} 个字幕文件")
        return SubtitleProcessResult(
            success=True,
            message=f"成功处理 {len(processed_files)} 个字幕文件",
            processed_files=processed_files,
            located_by=located_by,
            invalid_files=invalid_files,
        )

    def _find_subtitle_files(
        self,
        video_path: Path,
        *,
        dest_paths: Iterable[str] | None = None,
        sub_langs: Iterable[str] | None = None,
        allow_dir_scan: bool = False,
    ) -> tuple[list[Path], str]:
        """四级定位与视频关联的字幕文件，先命中的级别直接返回。

        1. `dest_paths` —— executor 从 `Writing video subtitles to:` 解析出的精确路径，
           最可信；这一级以前完全没用上
        2. `<stem>.<lang>.<ext>` —— lang 取 `opts["subtitleslangs"]`，ext 取
           `SUBTITLE_FORMATS`（`--convert-subs` 会改后缀，所以四种都试）
        3. 前缀扫描 —— `iterdir()` + `name.startswith(stem + ".")`。**替掉了未转义的
           `glob(f"{stem}.*")`**：标题里的 `[` `]` `?` 曾让整个定位直接失配
        4. 同目录兜底全扫 —— yt-dlp 在 Windows 上的 stdout 可能丢特殊 Unicode 字符
           （如 U+30FB），解析出的名字与磁盘不一致，前三级都会扑空

        > ⚠️ 第 4 级只在 `allow_dir_scan=True` 时启用，而它**只对任务沙盒目录成立**。
        > 调用方（`features.SubtitleFeature`）在内嵌模式下会 `os.remove` 掉这里返回的
        > 文件；在共享的下载目录里全扫，等于拿别的视频的字幕去喂删除逻辑。

        Returns:
            `(字幕文件列表, 命中的定位级别)`；全空时返回 `([], LOCATE_NONE)`
        """
        parent_dir = video_path.parent

        # ── 1. executor 记下的精确路径 ──
        hits = self._pick_subtitles(Path(p) for p in (dest_paths or ()))
        if hits:
            return hits, LOCATE_DEST_PATHS

        # 分片文件（`Title.f136.mp4`）的 stem 带着 `.f136`，拼不出字幕名。
        # `find_final_merged_file()` 通常已经纠正过 output_path，这里只是保险。
        stems = [video_path.stem]
        base = Path(video_path.stem).stem  # 去掉最后一段后缀式片段
        if base != video_path.stem and base and _is_fragment_tag(video_path.stem[len(base) + 1 :]):
            stems.append(base)

        # ── 2. `<stem>.<lang>.<ext>` ──
        exact: list[Path] = []
        for stem in stems:
            for lang in sub_langs or ():
                if not _is_safe_lang_segment(lang):
                    continue
                for ext in SUBTITLE_FORMATS:
                    exact.append(parent_dir / f"{stem}.{lang}.{ext}")
        hits = self._pick_subtitles(exact)
        if hits:
            return hits, LOCATE_LANG_PATTERN

        # ── 3. 前缀扫描 ──
        try:
            entries = list(parent_dir.iterdir())
        except OSError as e:
            logger.warning("字幕定位失败，无法读取目录 {} - {}", parent_dir, e)
            return [], LOCATE_NONE

        prefixes = tuple(f"{stem}." for stem in stems)
        hits = self._pick_subtitles(p for p in entries if p.name.startswith(prefixes))
        if hits:
            return hits, LOCATE_PREFIX_SCAN

        # ── 4. 同目录兜底全扫（仅沙盒）──
        if not allow_dir_scan:
            return [], LOCATE_NONE

        hits = self._pick_subtitles(entries)
        if hits:
            logger.warning(
                "路径不匹配兜底生效: 视频名 {} 与磁盘上的字幕文件名不一致，"
                "已通过目录扫描定位到 {} 个字幕文件",
                video_path.name,
                len(hits),
            )
            return hits, LOCATE_DIR_SCAN

        return [], LOCATE_NONE

    @staticmethod
    def _pick_subtitles(paths: Iterable[Path]) -> list[Path]:
        """从候选路径里挑出真实存在的字幕文件，按平台规则去重后定序。"""
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


def _is_fragment_tag(tag: str) -> bool:
    """`f136` 这类分片标记 —— 只有它才允许从 stem 上剥掉。

    不能无条件剥最后一段：`Ep.01` 的 `01`、`S01E02.1080p` 的 `1080p` 都不是分片标记，
    剥掉之后拼出来的字幕名反而对不上。
    """
    return len(tag) > 1 and tag[0] in "fF" and tag[1:].isdigit()


def _is_safe_lang_segment(lang: str) -> bool:
    """语言段能不能直接拼进文件名。

    `subtitleslangs` 里可能是回落正则（`en(-.+)?`），拼出来的路径根本不存在，
    `is_file()` 自然会滤掉，无害。真正要拦的是路径分隔符和 `..` —— 它们能把候选
    路径带出目标目录。
    """
    if not lang or lang in (".", ".."):
        return False
    return not any(sep in lang for sep in ("/", "\\", os.sep, os.altsep or "/"))


# 单例实例
subtitle_processor = SubtitleProcessor()
