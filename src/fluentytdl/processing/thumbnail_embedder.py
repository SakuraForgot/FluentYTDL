"""
封面嵌入后处理器

独立于 yt-dlp 的封面嵌入处理器：
- 使用外置 AtomicParsley 处理 MP4/M4A 等格式（最可靠）
- 使用 FFmpeg 处理 MKV/WEBM 等格式
- 使用 mutagen 处理 MP3/FLAC/OGG 等音频格式
- 自动跳过不支持的格式并给出提示
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..utils.logger import logger
from ..utils.paths import frozen_app_dir, is_frozen
from .thumbnail_embed import (
    get_thumbnail_support,
)


class EmbedTool(Enum):
    """封面嵌入工具"""

    ATOMICPARSLEY = "atomicparsley"  # MP4/M4A 最佳选择
    FFMPEG = "ffmpeg"  # MKV/WEBM 等
    MUTAGEN = "mutagen"  # MP3/FLAC/OGG 音频


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
    UNSUPPORTED_FORMATS = {"wav", "aiff", "ts", "m2ts", "vob", "rm", "rmvb", "flv"}

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
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """
        嵌入封面到视频/音频文件

        Args:
            video_path: 视频/音频文件路径
            thumbnail_path: 封面图片路径
            progress_callback: 进度回调函数

        Returns:
            EmbedResult 对象
        """
        video_path = Path(video_path)
        thumbnail_path = Path(thumbnail_path)

        if not video_path.exists():
            return EmbedResult(False, None, f"视频文件不存在: {video_path}")

        if not thumbnail_path.exists():
            return EmbedResult(False, None, f"封面文件不存在: {thumbnail_path}")

        ext = video_path.suffix.lower().lstrip(".")

        # 检查是否支持
        if ext in self.UNSUPPORTED_FORMATS:
            info = get_thumbnail_support(ext)
            return EmbedResult(
                success=False,
                tool_used=None,
                message=f"{ext.upper()} 格式不支持封面嵌入: {info.note}",
                skipped=True,
            )

        # 获取推荐工具
        tool = self.get_recommended_tool(ext)
        if tool is None:
            return EmbedResult(success=False, tool_used=None, message="没有可用的封面嵌入工具")

        # 执行嵌入
        if tool == EmbedTool.ATOMICPARSLEY:
            return self._embed_with_atomicparsley(video_path, thumbnail_path, progress_callback)
        elif tool == EmbedTool.FFMPEG:
            return self._embed_with_ffmpeg(video_path, thumbnail_path, progress_callback)
        elif tool == EmbedTool.MUTAGEN:
            return self._embed_with_mutagen(video_path, thumbnail_path, progress_callback)

        return EmbedResult(False, None, "未知错误")

    def _embed_with_atomicparsley(
        self,
        video_path: Path,
        thumbnail_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 AtomicParsley 嵌入封面"""
        ap_path = self._find_atomicparsley()
        if not ap_path:
            return EmbedResult(False, EmbedTool.ATOMICPARSLEY, "AtomicParsley 不可用")

        if progress_callback:
            progress_callback("正在使用 AtomicParsley 嵌入封面...")

        try:
            cmd = [str(ap_path), str(video_path), "--artwork", str(thumbnail_path), "--overWrite"]

            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", **kwargs
            )

            if result.returncode == 0:
                logger.info(f"AtomicParsley 封面嵌入成功: {video_path}")
                return EmbedResult(True, EmbedTool.ATOMICPARSLEY, "封面嵌入成功")
            else:
                error_msg = result.stderr or result.stdout or "未知错误"
                logger.error(f"AtomicParsley 失败: {error_msg}")
                return EmbedResult(
                    False, EmbedTool.ATOMICPARSLEY, f"AtomicParsley 错误: {error_msg}"
                )

        except Exception as e:
            logger.error(f"AtomicParsley 异常: {e}")
            return EmbedResult(False, EmbedTool.ATOMICPARSLEY, f"AtomicParsley 异常: {e}")

    def _embed_with_ffmpeg(
        self,
        video_path: Path,
        thumbnail_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 FFmpeg 嵌入封面"""
        ffmpeg_path = self._find_ffmpeg()
        if not ffmpeg_path:
            return EmbedResult(False, EmbedTool.FFMPEG, "FFmpeg 不可用")

        if progress_callback:
            progress_callback("正在使用 FFmpeg 嵌入封面...")

        ext = video_path.suffix.lower()

        # 创建临时输出文件
        # 指定 dir=video_path.parent 确保临时文件在同一驱动器，避免 os.replace 跨盘移动失败 (WinError 17)
        temp_fd, temp_path = tempfile.mkstemp(suffix=ext, dir=video_path.parent)
        os.close(temp_fd)

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
                    temp_path,
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
                    temp_path,
                ]

            kwargs = {}
            if sys.platform == "win32":
                si = subprocess.STARTUPINFO()
                si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                si.wShowWindow = 0
                kwargs["startupinfo"] = si
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", errors="ignore", **kwargs
            )

            if result.returncode == 0 and os.path.exists(temp_path):
                # 替换原文件
                os.replace(temp_path, video_path)
                logger.info(f"FFmpeg 封面嵌入成功: {video_path}")
                return EmbedResult(True, EmbedTool.FFMPEG, "封面嵌入成功")
            else:
                error_msg = result.stderr or "未知错误"
                logger.error(f"FFmpeg 失败: {error_msg}")
                return EmbedResult(False, EmbedTool.FFMPEG, f"FFmpeg 错误: {error_msg}")

        except Exception as e:
            logger.error(f"FFmpeg 异常: {e}")
            return EmbedResult(False, EmbedTool.FFMPEG, f"FFmpeg 异常: {e}")

        finally:
            # 清理临时文件
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except Exception:
                    pass

    def _embed_with_mutagen(
        self,
        video_path: Path,
        thumbnail_path: Path,
        progress_callback: Callable[[str], None] | None = None,
    ) -> EmbedResult:
        """使用 mutagen 嵌入封面（用于音频文件）"""
        if not self._check_mutagen():
            return EmbedResult(False, EmbedTool.MUTAGEN, "mutagen 库不可用")

        if progress_callback:
            progress_callback("正在使用 mutagen 嵌入封面...")

        ext = video_path.suffix.lower().lstrip(".")

        try:
            # 读取封面数据
            with open(thumbnail_path, "rb") as f:
                thumbnail_data = f.read()

            if ext == "mp3":
                return self._embed_mp3(video_path, thumbnail_data)
            elif ext == "flac":
                return self._embed_flac(video_path, thumbnail_data)
            elif ext in ("ogg", "opus"):
                return self._embed_ogg(video_path, thumbnail_data)
            else:
                return EmbedResult(False, EmbedTool.MUTAGEN, f"mutagen 不支持 {ext} 格式")

        except Exception as e:
            logger.error(f"mutagen 异常: {e}")
            return EmbedResult(False, EmbedTool.MUTAGEN, f"mutagen 异常: {e}")

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
            logger.info(f"mutagen MP3 封面嵌入成功: {file_path}")
            return EmbedResult(True, EmbedTool.MUTAGEN, "封面嵌入成功")

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, f"MP3 封面嵌入失败: {e}")

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

            logger.info(f"mutagen FLAC 封面嵌入成功: {file_path}")
            return EmbedResult(True, EmbedTool.MUTAGEN, "封面嵌入成功")

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, f"FLAC 封面嵌入失败: {e}")

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

            logger.info(f"mutagen OGG 封面嵌入成功: {file_path}")
            return EmbedResult(True, EmbedTool.MUTAGEN, "封面嵌入成功")

        except Exception as e:
            return EmbedResult(False, EmbedTool.MUTAGEN, f"OGG 封面嵌入失败: {e}")


# 全局实例
thumbnail_embedder = ThumbnailEmbedder()
