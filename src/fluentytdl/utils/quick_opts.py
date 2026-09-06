from typing import Any

from ..models.quick_download_params import QuickDownloadParams


def quick_params_to_opts(params: QuickDownloadParams) -> dict[str, Any]:
    """将快速下载面板的 UI 参数转换为 yt-dlp 选项字典。

    快速模式的 opts 自包含，不继承精细模式的设置。
    """
    opts: dict[str, Any] = {}

    # === 下载类型 + 质量 → format 字符串 ===
    if params.download_type == "audio_only":
        opts["format"] = "bestaudio/best"
    elif params.download_type == "video_only":
        if params.max_height:
            opts["format"] = f"bv*[height<={params.max_height}]/bv*"
        else:
            opts["format"] = "bestvideo/bv*"
    else:  # video_audio
        if params.max_height:
            opts["format"] = f"bv*[height<={params.max_height}]+ba/b[height<={params.max_height}]/b"
        else:
            opts["format"] = "bv*+ba/b"

    # === 音频提取/转换 ===
    if params.download_type != "video_only":
        has_audio_pp = False
        pp = {"key": "FFmpegExtractAudio"}

        if params.audio_format and params.audio_format != "自动推断":
            pp["preferredcodec"] = params.audio_format.lower()
            has_audio_pp = True

        if params.audio_quality:
            pp["preferredquality"] = str(params.audio_quality)
            has_audio_pp = True

        if has_audio_pp:
            if "preferredcodec" not in pp:
                pp["preferredcodec"] = "best"
            opts["postprocessors"] = opts.get("postprocessors", []) + [pp]
            # 如果不是纯音频模式，但却要求了音频格式转换，则保留原视频文件并提取音频
            if params.download_type != "audio_only":
                opts["keepvideo"] = True

    # === 容器 ===
    if params.container and params.container != "自动推断":
        opts["merge_output_format"] = params.container.lower()

    # === 元数据偏好 ===
    if params.embed_metadata:
        opts["addmetadata"] = True
        opts["postprocessors"] = opts.get("postprocessors", []) + [{"key": "FFmpegMetadata"}]
    if params.embed_thumbnail:
        opts["writethumbnail"] = True
        opts["embedthumbnail"] = True
        opts["convert_thumbnail"] = "jpg"
        opts["__fluentytdl_keep_thumbnail"] = True
    elif params.download_thumbnail:
        opts["writethumbnail"] = True
        opts["embedthumbnail"] = False
        opts["convert_thumbnail"] = "jpg"
        opts["__fluentytdl_keep_thumbnail"] = True

    # === 字幕 ===
    if params.subtitle_enabled:
        # 局部 import：`processing` 在 `utils` 之上（同文件 `:84` 取 `config_manager`
        # 已是同样的处理）。把偏好→真实字幕键的换算集中在 subtitle_service 一处，
        # 比在这里复制一份私有键的形状更重要。
        from ..models.subtitle_config import SubtitleConfig, SubtitleTypePreference
        from ..processing.subtitle_service import declare_subtitle_intent

        opts["writesubtitles"] = True
        # 快速模式 opts 自包含，所以上下文用**默认** SubtitleConfig，只把面板上真有的
        # 那一项映射进去：勾了"自动字幕"就连自动翻译一起收（`ALL`）—— YouTube 上不少
        # 视频的中文只有自动翻译版本；没勾就是明确的"只要人工字幕"。
        sub_config = SubtitleConfig(
            enabled=True,
            enable_auto_captions=params.subtitle_auto_captions,
            type_preference=(
                SubtitleTypePreference.ALL
                if params.subtitle_auto_captions
                else SubtitleTypePreference.MANUAL_ONLY
            ),
        )
        # 直写 `subtitleslangs` 会把 `zh-Hans` / `en` 这样的偏好送进 `--sub-langs`，
        # 而那里的每一项是锚定正则，匹配不到真实键 `en-GB`。
        opts.update(declare_subtitle_intent(params.subtitle_languages, sub_config))
        if params.subtitle_auto_captions:
            opts["writeautomaticsub"] = True
        if params.subtitle_embed:
            opts["embedsubtitles"] = True

    # === SponsorBlock ===
    if params.sponsorblock_enabled:
        if params.sponsorblock_action == "remove":
            opts["sponsorblock_remove"] = params.sponsorblock_categories
        else:
            opts["sponsorblock_mark"] = params.sponsorblock_categories

    # === 质量意图标记（质量守卫使用） ===
    from ..core.config_manager import config_manager

    opts["__fluentytdl_quality_intent"] = {
        "source": "quick_mode",
        "download_type": params.download_type,
        "target_height": params.max_height,
        "fallback_policy": str(config_manager.get("quality_guard_mode", "warn")),
    }

    # === 下载位置 ===
    if params.download_dir:
        opts["paths"] = {"home": params.download_dir}

    return opts


def merge_quick_opts(quick_opts: dict[str, Any], base_opts: dict[str, Any]) -> dict[str, Any]:
    """合并快速模式选项和基础选项。

    规则：quick_opts 中用户显式设置的值优先。
    base_opts 中的基础设施配置（cookie, proxy, ffmpeg_location 等）作为底层。
    """
    merged = base_opts.copy()

    # 快速模式的显式选项覆盖基础选项
    for k, v in quick_opts.items():
        if k.startswith("__fluentytdl_"):
            merged[k] = v  # 内部标记直接传递
        elif k in (
            "format",
            "merge_output_format",
            "noplaylist",
            "playlist_items",
            "writesubtitles",
            "subtitleslangs",
            "writeautomaticsub",
            "embedsubtitles",
            "writethumbnail",
            "embedthumbnail",
            "addmetadata",
            "sponsorblock_remove",
            "sponsorblock_mark",
            "outtmpl",
            "paths",
        ):
            merged[k] = v  # 用户选择的参数优先
        elif k == "keepvideo":
            merged[k] = v
        elif k == "postprocessors":
            # 后处理器合并而非覆盖
            existing = merged.get("postprocessors", [])
            merged["postprocessors"] = existing + v

    return merged
