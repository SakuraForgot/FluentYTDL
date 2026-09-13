"""
音频处理模块

负责:
- 音频格式预设管理
- 封面/元数据嵌入
- 音量标准化 (FFmpeg loudnorm)
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP

from fluentytdl.utils.localized_log import log_text
from fluentytdl.utils.ui_text import tr_text

from ..core.config_manager import config_manager
from ..utils.logger import logger
from ..utils.paths import find_bundled_executable, get_clean_env, is_frozen, locate_runtime_tool


@dataclass
class AudioPreset:
    """音频预设配置"""

    id: str
    name: str
    description: str
    format: str  # yt-dlp format string
    codec: str  # 输出编码 (mp3, aac, flac, opus, etc.)
    quality: str  # 质量参数 (比特率或 VBR 等级)
    embed_thumbnail: bool  # 是否嵌入封面
    embed_metadata: bool  # 是否嵌入元数据
    normalize: bool  # 是否音量标准化


class AudioPresetManager:
    """音频预设管理器"""

    # 内置预设
    BUILTIN_PRESETS: dict[str, AudioPreset] = {
        "mp3_320": AudioPreset(
            id="mp3_320",
            name=QT_TRANSLATE_NOOP("RuntimeText", "MP3 320K (推荐)"),
            description=QT_TRANSLATE_NOOP("RuntimeText", "高品质 MP3，兼容性最佳"),
            format="bestaudio/best",
            codec="mp3",
            quality="320K",
            embed_thumbnail=True,
            embed_metadata=True,
            normalize=False,
        ),
        "mp3_192": AudioPreset(
            id="mp3_192",
            name="MP3 192K",
            description=QT_TRANSLATE_NOOP("RuntimeText", "标准品质 MP3，体积较小"),
            format="bestaudio/best",
            codec="mp3",
            quality="192K",
            embed_thumbnail=True,
            embed_metadata=True,
            normalize=False,
        ),
        "mp3_v0": AudioPreset(
            id="mp3_v0",
            name="MP3 VBR V0",
            description=QT_TRANSLATE_NOOP("RuntimeText", "VBR 最高品质 (~245kbps)"),
            format="bestaudio/best",
            codec="mp3",
            quality="0",  # VBR 等级
            embed_thumbnail=True,
            embed_metadata=True,
            normalize=False,
        ),
        "aac_256": AudioPreset(
            id="aac_256",
            name="AAC 256K",
            description=QT_TRANSLATE_NOOP("RuntimeText", "Apple/YouTube 原生格式"),
            format="bestaudio[ext=m4a]/bestaudio/best",
            codec="aac",
            quality="256K",
            embed_thumbnail=True,
            embed_metadata=True,
            normalize=False,
        ),
        "flac": AudioPreset(
            id="flac",
            name=QT_TRANSLATE_NOOP("RuntimeText", "FLAC (无损)"),
            description=QT_TRANSLATE_NOOP("RuntimeText", "无损压缩，体积较大"),
            format="bestaudio/best",
            codec="flac",
            quality="",  # 无损不需要比特率
            embed_thumbnail=False,  # FLAC 封面支持有限
            embed_metadata=True,
            normalize=False,
        ),
        "opus_128": AudioPreset(
            id="opus_128",
            name="Opus 128K",
            description=QT_TRANSLATE_NOOP("RuntimeText", "现代编码，高效压缩"),
            format="bestaudio[ext=webm]/bestaudio/best",
            codec="opus",
            quality="128K",
            embed_thumbnail=False,  # Opus/WebM 封面支持有限
            embed_metadata=True,
            normalize=False,
        ),
        "wav": AudioPreset(
            id="wav",
            name=QT_TRANSLATE_NOOP("RuntimeText", "WAV (无压缩)"),
            description=QT_TRANSLATE_NOOP("RuntimeText", "原始音频，体积最大"),
            format="bestaudio/best",
            codec="wav",
            quality="",
            embed_thumbnail=False,
            embed_metadata=False,
            normalize=False,
        ),
        "best_original": AudioPreset(
            id="best_original",
            name=QT_TRANSLATE_NOOP("RuntimeText", "保持原格式"),
            description=QT_TRANSLATE_NOOP("RuntimeText", "不转码，直接提取最佳音频流"),
            format="bestaudio/best",
            codec="",  # 不转码
            quality="",
            embed_thumbnail=True,
            embed_metadata=True,
            normalize=False,
        ),
    }

    @classmethod
    def get_preset(cls, preset_id: str) -> AudioPreset | None:
        """获取预设配置"""
        preset = cls.BUILTIN_PRESETS.get(preset_id)
        return (
            replace(preset, name=tr_text(preset.name), description=tr_text(preset.description))
            if preset
            else None
        )

    @classmethod
    def get_all_presets(cls) -> list[AudioPreset]:
        """获取所有预设"""
        return [
            replace(p, name=tr_text(p.name), description=tr_text(p.description))
            for p in cls.BUILTIN_PRESETS.values()
        ]

    @classmethod
    def get_preset_names(cls) -> list[tuple[str, str]]:
        """获取预设 ID 和名称列表，用于 UI 下拉框"""
        return [(p.id, tr_text(p.name)) for p in cls.BUILTIN_PRESETS.values()]


class AudioProcessor:
    """音频处理器

    提供音频后处理功能：
    - 封面嵌入
    - 元数据嵌入
    - 音量标准化
    """

    _instance: AudioProcessor | None = None

    def __new__(cls) -> AudioProcessor:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def _get_ffmpeg_path(self) -> Path | None:
        """获取 FFmpeg 路径"""
        # 配置文件路径
        cfg_path = str(config_manager.get("ffmpeg_path") or "").strip()
        if cfg_path and Path(cfg_path).exists():
            return Path(cfg_path)

        # 项目 bin 目录
        try:
            return locate_runtime_tool("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
        except FileNotFoundError:
            pass

        if is_frozen():
            p = find_bundled_executable("ffmpeg.exe", "ffmpeg/ffmpeg.exe")
            if p:
                return p

        # 系统 PATH
        which = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
        return Path(which) if which else None

    def build_yt_dlp_options(
        self, preset: AudioPreset | None = None, custom_opts: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """构建 yt-dlp 音频下载选项

        Args:
            preset: 音频预设（可选）
            custom_opts: 自定义选项覆盖（可选）

        Returns:
            yt-dlp 格式的配置字典
        """
        custom_opts = custom_opts or {}

        # 如果没有预设，使用配置中的默认值或直接返回
        if not preset:
            default_preset_id = config_manager.get("audio_default_preset", "mp3_320")
            preset = AudioPresetManager.get_preset(default_preset_id)
            if not preset:
                preset = AudioPresetManager.BUILTIN_PRESETS["mp3_320"]

        ydl_opts: dict[str, Any] = {
            "format": custom_opts.get("format") or preset.format,
        }

        # 后处理器列表
        postprocessors: list[dict[str, Any]] = []

        # 音频提取/转码
        if preset.codec:
            pp_audio: dict[str, Any] = {
                "key": "FFmpegExtractAudio",
                "preferredcodec": preset.codec,
            }
            if preset.quality:
                # quality 可以是比特率 "320K" 或 VBR 等级 "0"
                quality = preset.quality.rstrip("Kk")
                pp_audio["preferredquality"] = quality
            postprocessors.append(pp_audio)

        # 元数据嵌入
        embed_metadata = custom_opts.get("embed_metadata", preset.embed_metadata)
        if embed_metadata:
            postprocessors.append({"key": "FFmpegMetadata"})

        # 封面嵌入
        embed_thumbnail = custom_opts.get("embed_thumbnail", preset.embed_thumbnail)
        if embed_thumbnail:
            ydl_opts["writethumbnail"] = True
            postprocessors.append({"key": "EmbedThumbnail"})

        # 音量标准化
        normalize = custom_opts.get("normalize", preset.normalize)
        normalize = normalize or config_manager.get("audio_normalize", False)
        if normalize:
            # 使用 FFmpeg loudnorm 滤镜
            # 参考: https://ffmpeg.org/ffmpeg-filters.html#loudnorm
            target_lufs = config_manager.get("audio_target_lufs", -14)
            target_tp = config_manager.get("audio_target_tp", -1)
            target_lra = config_manager.get("audio_target_lra", 11)

            # 注意: yt-dlp 的 FFmpegPostProcessor 不直接支持 loudnorm
            # 我们需要使用 postprocessor_args
            ydl_opts["postprocessor_args"] = {
                "ffmpeg": ["-af", f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}"]
            }

        if postprocessors:
            ydl_opts["postprocessors"] = postprocessors

        return ydl_opts

    def normalize_audio_file(
        self,
        input_path: str,
        output_path: str,
        target_lufs: float = -14,
        target_tp: float = -1,
        target_lra: float = 11,
    ) -> bool:
        """对已存在的音频文件进行音量标准化 —— **纯 transformer：只读 input、只写 output**。

        以前 `output_path` 可以省略，省略时走原地分支：先 `input_p.unlink()` 再
        `output_p.rename(input_p)`，两行之间断电 = 用户的音频原件已经消失、新内容还挂在
        `.normalized.mp3` 这种临时名上。后来收敛成一次 `Path.replace()` 关掉了数据丢失
        窗口，但「谁来采纳这份新内容」仍然由本模块自己决定，越过了下载产物事务层。

        现在落点由调用方给（`StagingArea.reserve_workfile()`，在沙盒 `.work/` 里），
        采纳由 `replace_artifact_content()` 做。**失败时不清理 `output_path`** —— 那是
        沙盒的东西，随 `rmtree` 消失；在这里删它反而违反「销毁与位移只经过 StagingArea」。

        Args:
            input_path: 输入文件路径，全程只读
            output_path: 标准化之后的落点
            target_lufs: 目标响度 (dB LUFS)，默认 -14
            target_tp: 目标真峰值 (dB TP)，默认 -1
            target_lra: 目标响度范围 (LU)，默认 11

        Returns:
            是否成功
        """
        ffmpeg = self._get_ffmpeg_path()
        if not ffmpeg:
            log_text(logger, "error", "FFmpeg 未找到，无法进行音量标准化")
            return False

        input_p = Path(input_path)
        if not input_p.exists():
            log_text(logger, "error", "输入文件不存在: {0}", input_path)
            return False

        output_p = Path(output_path)

        try:
            # 构建 FFmpeg 命令
            cmd = [
                str(ffmpeg),
                "-i",
                str(input_p),
                "-af",
                f"loudnorm=I={target_lufs}:TP={target_tp}:LRA={target_lra}",
                "-y",  # 覆盖输出
                str(output_p),
            ]

            kwargs: dict[str, Any] = {}
            if os.name == "nt":
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
                errors="replace",
                env=env,
                **kwargs,
            )

            if result.returncode != 0:
                log_text(logger, "error", "音量标准化失败: {0}", result.stderr)
                return False

            if not output_p.exists():
                log_text(logger, "error", "音量标准化未产出文件: {0}", output_p)
                return False

            log_text(logger, "info", "音量标准化完成: {0} → {1}", input_path, output_p)
            return True

        except Exception as e:
            log_text(logger, "exception", "音量标准化异常: {0}", e)
            return False

    def embed_cover_art(self, audio_path: str, cover_path: str, output_path: str) -> bool:
        """为音频文件嵌入封面 —— **纯 transformer：只读 input、只写 output**。

        与 `normalize_audio_file` 同构：落点由调用方给（沙盒 `.work/`），采纳由
        `StagingArea.replace_artifact_content()` 做，失败不清理 `output_path`。

        Args:
            audio_path: 音频文件路径，全程只读
            cover_path: 封面图片路径
            output_path: 嵌好之后的落点

        Returns:
            是否成功
        """
        ffmpeg = self._get_ffmpeg_path()
        if not ffmpeg:
            log_text(logger, "error", "FFmpeg 未找到，无法嵌入封面")
            return False

        audio_p = Path(audio_path)
        cover_p = Path(cover_path)

        if not audio_p.exists() or not cover_p.exists():
            log_text(logger, "error", "音频或封面文件不存在")
            return False

        output_p = Path(output_path)

        try:
            ext = audio_p.suffix.lower()

            # MP3: 使用 id3v2 封面
            if ext == ".mp3":
                cmd = [
                    str(ffmpeg),
                    "-i",
                    str(audio_p),
                    "-i",
                    str(cover_p),
                    "-map",
                    "0:a",
                    "-map",
                    "1:v",
                    "-c:a",
                    "copy",
                    "-c:v",
                    "mjpeg",
                    "-id3v2_version",
                    "3",
                    "-metadata:s:v",
                    "title=Album cover",
                    "-metadata:s:v",
                    "comment=Cover (front)",
                    "-y",
                    str(output_p),
                ]
            # M4A/AAC: 使用 mp4 封面
            elif ext in (".m4a", ".aac", ".mp4"):
                cmd = [
                    str(ffmpeg),
                    "-i",
                    str(audio_p),
                    "-i",
                    str(cover_p),
                    "-map",
                    "0:a",
                    "-map",
                    "1:v",
                    "-c:a",
                    "copy",
                    "-c:v",
                    "mjpeg",
                    "-disposition:v:0",
                    "attached_pic",
                    "-y",
                    str(output_p),
                ]
            else:
                log_text(logger, "warning", "不支持为 {0} 格式嵌入封面", ext)
                return False

            kwargs: dict[str, Any] = {}
            if os.name == "nt":
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
                errors="replace",
                env=env,
                **kwargs,
            )

            if result.returncode != 0:
                log_text(logger, "error", "封面嵌入失败: {0}", result.stderr)
                return False

            if not output_p.exists():
                log_text(logger, "error", "封面嵌入未产出文件: {0}", output_p)
                return False

            log_text(logger, "info", "封面嵌入完成: {0} → {1}", audio_path, output_p)
            return True

        except Exception as e:
            log_text(logger, "exception", "封面嵌入异常: {0}", e)
            return False


# 全局单例
audio_processor = AudioProcessor()
