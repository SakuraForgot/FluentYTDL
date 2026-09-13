"""
封面嵌入后处理器

独立于 yt-dlp 的封面嵌入处理器：
- 使用外置 AtomicParsley 处理 MP4/M4A 等格式（最可靠）
- 使用 FFmpeg 处理 MKV/WEBM 等格式
- 使用 mutagen 处理 MP3/FLAC/OGG 等音频格式
- 自动跳过不支持的格式并给出提示
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..utils.logger import logger
from ..utils.paths import frozen_app_dir, get_clean_env, is_frozen
from .thumbnail_embed import (
    get_thumbnail_support,
)


class EmbedTool(Enum):
    """封面嵌入工具"""

    ATOMICPARSLEY = "atomicparsley"  # MP4/M4A 最佳选择
    FFMPEG = "ffmpeg"  # MKV/WEBM 等
    MUTAGEN = "mutagen"  # MP3/FLAC/OGG 音频

    @property
    def mutates_in_place(self) -> bool:
        """这个后端是不是**只会改写自己手上那份文件**。

        AtomicParsley 走 `--overWrite`、mutagen 走 `audio.save()`，两者都没有
        「输出到另一个路径」的用法。ffmpeg 相反，本来就是 `-i 源 → 新文件`。

        调用侧靠它决定 `reserve_workfile()` 要不要 `seed_from` —— 原地型拿到的
        output 必须是源字节的副本，这样源 artifact 全程只读，失败时主媒体原样不动。
        """
        return self is not EmbedTool.FFMPEG


@dataclass
class EmbedResult:
    """封面嵌入结果"""

    success: bool
    tool_used: EmbedTool | None
    message: str
    skipped: bool = False  # 是否因不支持而跳过


class ThumbnailEmbedder:
    """
    封面嵌入器

    支持多种工具和格式，自动选择最佳嵌入方案。
    """

    # AtomicParsley 最佳支持的格式
    ATOMICPARSLEY_FORMATS = {"mp4", "m4v", "m4a", "m4b", "mov", "3gp"}

    # FFmpeg 支持的格式
    FFMPEG_FORMATS = {"mkv", "mka", "webm", "avi", "wmv", "asf", "wma"}

    # mutagen 支持的格式
    MUTAGEN_FORMATS = {"mp3", "flac", "ogg", "opus"}

    # 不支持封面嵌入的格式（黑名单）
    UNSUPPORTED_FORMATS = {
        "wav",
        "aiff",
        "ts",
        "m2ts",
        "vob",
        "rm",
        "rmvb",
        "flv",
        "jpg",
        "jpeg",
        "png",
        "webp",
        "gif",
        "bmp",
        "tiff",
    }

    def __init__(self):
        self._atomicparsley_path: Path | None = None
        self._ffmpeg_path: Path | None = None
        self._mutagen_available: bool | None = None

    def _get_bin_dir(self) -> Path:
        """获取 bin 目录路径"""
        if is_frozen():
            return frozen_app_dir() / "bin"
        else:
            return Path(__file__).parents[3] / "assets" / "bin"

    def _find_atomicparsley(self) -> Path | None:
        """查找 AtomicParsley 可执行文件"""
        if self._atomicparsley_path:
            return self._atomicparsley_path

        # 1. 检查 bin/atomicparsley/
        bin_path = self._get_bin_dir() / "atomicparsley" / "AtomicParsley.exe"
        if bin_path.exists():
            self._atomicparsley_path = bin_path
            return bin_path

        # 2. 检查 bin/yt-dlp/ (兼容之前的测试位置)
        ytdlp_path = self._get_bin_dir() / "yt-dlp" / "AtomicParsley.exe"
        if ytdlp_path.exists():
            self._atomicparsley_path = ytdlp_path
            return ytdlp_path

        # 3. 检查 PATH
        which_path = shutil.which("AtomicParsley")
        if which_path:
            self._atomicparsley_path = Path(which_path)
            return self._atomicparsley_path

        return None

    def _find_ffmpeg(self) -> Path | None:
        """查找 FFmpeg 可执行文件"""
        if self._ffmpeg_path:
            return self._ffmpeg_path

        # 1. 检查 bin/ffmpeg/
        bin_path = self._get_bin_dir() / "ffmpeg" / "ffmpeg.exe"
        if bin_path.exists():
            self._ffmpeg_path = bin_path
            return bin_path

        # 2. 检查 PATH
        which_path = shutil.which("ffmpeg")
        if which_path:
            self._ffmpeg_path = Path(which_path)
            return self._ffmpeg_path

        return None

    def _check_mutagen(self) -> bool:
        """检查 mutagen 是否可用"""
        if self._mutagen_available is not None:
            return self._mutagen_available

        try:
            import importlib.util

            self._mutagen_available = importlib.util.find_spec("mutagen") is not None
        except ImportError:
            self._mutagen_available = False

        return self._mutagen_available

    def get_tool_status(self) -> dict[str, bool]:
        """获取各工具的可用状态"""
        return {
            "atomicparsley": self._find_atomicparsley() is not None,
            "ffmpeg": self._find_ffmpeg() is not None,
            "mutagen": self._check_mutagen(),
        }

    def is_available(self) -> bool:
        """检查是否有任何封面嵌入工具可用"""
        status = self.get_tool_status()
        return any(status.values())

    def get_recommended_tool(self, extension: str) -> EmbedTool | None:
        """根据文件格式获取推荐的嵌入工具"""
        ext = extension.lower().lstrip(".")

        # 不支持的格式
        if ext in self.UNSUPPORTED_FORMATS:
            return None

        # AtomicParsley 格式
        if ext in self.ATOMICPARSLEY_FORMATS:
            if self._find_atomicparsley():
                return EmbedTool.ATOMICPARSLEY
            elif self._find_ffmpeg():
                # 降级到 FFmpeg
                return EmbedTool.FFMPEG

        # FFmpeg 格式
        if ext in self.FFMPEG_FORMATS:
            if self._find_ffmpeg():
                return EmbedTool.FFMPEG

        # mutagen 格式
        if ext in self.MUTAGEN_FORMATS:
            if self._check_mutagen():
                return EmbedTool.MUTAGEN
            elif self._find_ffmpeg():
                # 降级到 FFmpeg
                return EmbedTool.FFMPEG

        # 未知格式，尝试 FFmpeg
        if self._find_ffmpeg():
            return EmbedTool.FFMPEG

        return None

    def embed_thumbnail(
        self,
        video_path: str | Path,
        thumbnail_path: str | Path,
        output_path: str | Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """嵌入封面 —— **纯 transformer：只读 input、只写 output**。

        以前这个方法直接改写 `video_path`：ffmpeg 分支自己
        `mkstemp(dir=video_path.parent)`（临时文件落在 payload 里）、自己
        `os.replace(temp, video)`、失败自己 `os.remove(temp)`。三件事都越过了下载
        产物事务层 —— 崩在中途会留下一个 `tmpXXXX.mp4`，而它一旦被下一个 run 的
        `reconcile()` 看到就成了清单里一个假的 media。

        现在落点由调用方给（`StagingArea.reserve_workfile()`，在 `.work/` 里，
        `reconcile()` 永远看不到），采纳由 `replace_artifact_content()` 做。

        **失败时不清理 `output_path`** —— 那是沙盒的东西，随 `rmtree` 消失；这里去
        删它反而会违反「销毁与位移只经过 StagingArea」。

        Args:
            video_path: 源文件，全程只读
            thumbnail_path: 封面图片路径
            output_path: 嵌好之后的落点。原地改写型后端（见
                `EmbedTool.mutates_in_place`）要求调用方**先把源字节播种进来**
                （`reserve_workfile(..., seed_from=...)`）
            progress_callback: 进度回调函数

        Returns:
            EmbedResult 对象
        """
        video_path = Path(video_path)
        thumbnail_path = Path(thumbnail_path)
        output_path = Path(output_path)

        if not video_path.exists():
            return EmbedResult(False, None, tr_text("视频文件不存在: {0}", video_path))

        if not thumbnail_path.exists():
            return EmbedResult(False, None, tr_text("封面文件不存在: {0}", thumbnail_path))

        ext = video_path.suffix.lower().lstrip(".")

        # 检查是否支持
        if ext in self.UNSUPPORTED_FORMATS:
            info = get_thumbnail_support(ext)
            return EmbedResult(
                success=False,
                tool_used=None,
                message=tr_text("{0} 格式不支持封面嵌入: {1}", ext.upper(), info.note),
                skipped=True,
            )

        # 获取推荐工具
        tool = self.get_recommended_tool(ext)
        if tool is None:
            return EmbedResult(
                success=False, tool_used=None, message=tr_text("没有可用的封面嵌入工具")
            )

        if tool.mutates_in_place and not output_path.exists():
            # 正常路径上调用方已经用 `reserve_workfile(seed_from=...)` 播种过了。这里兜一层
            # 是因为「选哪个工具」被算了两次（调用方一次、这里一次），中间万一因为工具
            # 可用性变化而分叉，原地型后端会拿到一个不存在的 output 直接失败。
            # 纯创建，不动任何既有文件。
            try:
                shutil.copy2(video_path, output_path)
            except OSError as e:
                return EmbedResult(False, tool, tr_text("无法准备嵌入落点: {0}", e))

        # 执行嵌入
        if tool == EmbedTool.ATOMICPARSLEY:
            return self._embed_with_atomicparsley(output_path, thumbnail_path, progress_callback)
        elif tool == EmbedTool.FFMPEG:
            return self._embed_with_ffmpeg(
                video_path, thumbnail_path, output_path, progress_callback
            )
        elif tool == EmbedTool.MUTAGEN:
            return self._embed_with_mutagen(output_path, thumbnail_path, progress_callback)

        return EmbedResult(False, None, tr_text("未知错误"))

    def _embed_with_atomicparsley(
        self,
        target: Path,
        thumbnail_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 AtomicParsley 嵌入封面 —— `--overWrite` 只能原地改写，所以 `target`
        必须已经是**播种好的副本**（见 `EmbedTool.mutates_in_place`），源文件不在这里出现。"""
        ap_path = self._find_atomicparsley()
        if not ap_path:
            return EmbedResult(False, EmbedTool.ATOMICPARSLEY, tr_text("AtomicParsley 不可用"))

        if progress_callback:
            progress_callback(tr_text("正在使用 AtomicParsley 嵌入封面..."))

        try:
            cmd = [str(ap_path), str(target), "--artwork", str(thumbnail_path), "--overWrite"]

            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            env = get_clean_env()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                env=env,
                **kwargs,
            )

            if result.returncode == 0:
                log_text(logger, "info", "AtomicParsley 封面嵌入成功: {0}", target)
                return EmbedResult(True, EmbedTool.ATOMICPARSLEY, tr_text("封面嵌入成功"))
            else:
                error_msg = result.stderr or result.stdout or tr_text("未知错误")
                log_text(logger, "error", "AtomicParsley 失败: {0}", error_msg)
                return EmbedResult(
                    False, EmbedTool.ATOMICPARSLEY, tr_text("AtomicParsley 错误: {0}", error_msg)
                )

        except Exception as e:
            log_text(logger, "error", "AtomicParsley 异常: {0}", e)
            return EmbedResult(
                False, EmbedTool.ATOMICPARSLEY, tr_text("AtomicParsley 异常: {0}", e)
            )

    def _embed_with_ffmpeg(
        self,
        video_path: Path,
        thumbnail_path: Path,
        output_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 FFmpeg 嵌入封面 —— `-i 源 → output_path`，源全程只读。

        这里以前有三处越权：`mkstemp(dir=video_path.parent)` 把临时文件造在 payload 里、
        `os.replace(temp, video_path)` 直接覆盖事务持有的主媒体、`finally` 里
        `os.remove(temp)` 自己清理。现在落点由调用方（事务层）给，失败也不清理它 ——
        它在沙盒的 `.work/` 里，随 `rmtree` 消失。
        """
        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            return EmbedResult(False, EmbedTool.FFMPEG, tr_text("FFmpeg 不可用"))

        if progress_callback:
            progress_callback(tr_text("正在使用 FFmpeg 嵌入封面..."))

        ext = video_path.suffix.lower()
        out = str(output_path)

        try:
            # 根据格式选择不同的嵌入方式
            if ext in (".mkv", ".mka", ".webm"):
                # MKV/WebM: 作为附件流嵌入
                # -map 0 确保原始文件的所有流（包括多字幕轨）都被复制
                cmd = [
                    str(ffmpeg_path),
                    "-y",
                    "-i",
                    str(video_path),
                    "-map",
                    "0",
                    "-attach",
                    str(thumbnail_path),
                    "-metadata:s:t",
                    "mimetype=image/jpeg",
                    "-metadata:s:t",
                    "filename=cover.jpg",
                    "-c",
                    "copy",
                    out,
                ]
            else:
                # MP4 等: 作为视频流嵌入
                cmd = [
                    str(ffmpeg_path),
                    "-y",
                    "-i",
                    str(video_path),
                    "-i",
                    str(thumbnail_path),
                    "-map",
                    "0",
                    "-map",
                    "1",
                    "-c",
                    "copy",
                    "-disposition:v:1",
                    "attached_pic",
                    out,
                ]

            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            env = get_clean_env()
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                env=env,
                **kwargs,
            )

            if result.returncode == 0 and output_path.exists():
                log_text(logger, "info", "FFmpeg 封面嵌入成功: {0}", output_path)
                return EmbedResult(True, EmbedTool.FFMPEG, tr_text("封面嵌入成功"))
            else:
                error_msg = result.stderr or tr_text("未知错误")
                log_text(logger, "error", "FFmpeg 失败: {0}", error_msg)
                return EmbedResult(False, EmbedTool.FFMPEG, tr_text("FFmpeg 错误: {0}", error_msg))

        except Exception as e:
            log_text(logger, "error", "FFmpeg 异常: {0}", e)
            return EmbedResult(False, EmbedTool.FFMPEG, tr_text("FFmpeg 异常: {0}", e))

    def _embed_with_mutagen(
        self,
        target: Path,
        thumbnail_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 mutagen 嵌入封面（用于音频文件）—— `audio.save()` 只能原地改写，
        所以 `target` 必须已经是**播种好的副本**（见 `EmbedTool.mutates_in_place`）。"""
        if not self._check_mutagen():
            return EmbedResult(False, EmbedTool.MUTAGEN, tr_text("mutagen 库不可用"))

        if progress_callback:
            progress_callback(tr_text("正在使用 mutagen 嵌入封面..."))

        ext = target.suffix.lower().lstrip(".")

        try:
            # 读取封面数据
            with open(thumbnail_path, "rb") as f:
                thumbnail_data = f.read()

            if ext == "mp3":
                return self._embed_mp3(target, thumbnail_data)
            elif ext == "flac":
                return self._embed_flac(target, thumbnail_data)
            elif ext in ("ogg", "opus"):
                return self._embed_ogg(target, thumbnail_data)
            else:
                return EmbedResult(
                    False, EmbedTool.MUTAGEN, tr_text("mutagen 不支持 {0} 格式", ext)
                )

        except Exception as e:
            log_text(logger, "error", "mutagen 异常: {0}", e)
            return EmbedResult(False, EmbedTool.MUTAGEN, tr_text("mutagen 异常: {0}", e))

    def _embed_mp3(self, file_path: Path, thumbnail_data: bytes) -> EmbedResult:
        """嵌入 MP3 封面"""
        try:
            # 类型检查器可能对 mutagen 的导出有警告，但运行时是正常的
            from mutagen.id3 import APIC, ID3, ID3NoHeaderError  # type: ignore
            from mutagen.mp3 import MP3

            try:
                audio = MP3(str(file_path), ID3=ID3)
            except ID3NoHeaderError:
                audio = MP3(str(file_path))
                audio.add_tags()

            # 移除现有封面
            if audio.tags is not None:
                audio.tags.delall("APIC")

                # 添加新封面
                audio.tags.add(
                    APIC(
                        encoding=3,  # UTF-8
                        mime="image/jpeg",
                        type=3,  # Cover (front)
                        desc="Cover",
                        data=thumbnail_data,
                    )
                )

            audio.save()
            log_text(logger, "info", "mutagen MP3 封面嵌入成功: {0}", file_path)
            return EmbedResult(True, EmbedTool.MUTAGEN, tr_text("封面嵌入成功"))

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, tr_text("MP3 封面嵌入失败: {0}", e))

    def _embed_flac(self, file_path: Path, thumbnail_data: bytes) -> EmbedResult:
        """嵌入 FLAC 封面"""
        try:
            from mutagen.flac import FLAC, Picture

            audio = FLAC(str(file_path))

            # 清除现有图片
            audio.clear_pictures()

            # 创建新图片
            pic = Picture()
            pic.type = 3  # Cover (front)
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = thumbnail_data

            audio.add_picture(pic)
            audio.save()

            log_text(logger, "info", "mutagen FLAC 封面嵌入成功: {0}", file_path)
            return EmbedResult(True, EmbedTool.MUTAGEN, tr_text("封面嵌入成功"))

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, tr_text("FLAC 封面嵌入失败: {0}", e))

    def _embed_ogg(self, file_path: Path, thumbnail_data: bytes) -> EmbedResult:
        """嵌入 OGG/Opus 封面"""
        try:
            import base64

            from mutagen.flac import Picture
            from mutagen.oggopus import OggOpus
            from mutagen.oggvorbis import OggVorbis

            ext = file_path.suffix.lower()

            if ext == ".opus":
                audio = OggOpus(str(file_path))
            else:
                audio = OggVorbis(str(file_path))

            # 创建 Picture 对象并 base64 编码
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Cover"
            pic.data = thumbnail_data

            # OGG 使用 METADATA_BLOCK_PICTURE
            audio["METADATA_BLOCK_PICTURE"] = [base64.b64encode(pic.write()).decode("ascii")]
            audio.save()

            log_text(logger, "info", "mutagen OGG 封面嵌入成功: {0}", file_path)
            return EmbedResult(True, EmbedTool.MUTAGEN, tr_text("封面嵌入成功"))

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, tr_text("OGG 封面嵌入失败: {0}", e))


# 全局实例
thumbnail_embedder = ThumbnailEmbedder()
