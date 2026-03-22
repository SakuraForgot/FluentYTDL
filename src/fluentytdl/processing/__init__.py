"""
FluentYTDL 后处理功能域

包含音频处理、字幕管理、片段下载、广告跳过等功能。
"""

from .audio_processor import AudioProcessor, audio_processor
from .section_download import (
    TimeRange,
    build_section_opts,
    lossless_cut,
    parse_time_input,
    parse_time_range,
)
from .sponsorblock import (
    SponsorBlockConfig,
    build_sponsorblock_opts,
    extract_chapters,
    get_available_categories,
    sponsorblock_config,
)
from .subtitle_manager import (
    SubtitleTrack,
    extract_subtitle_tracks,
    get_subtitle_languages,
)
from .subtitle_processor import (
    SubtitleProcessor,
    SubtitleProcessResult,
    subtitle_processor,
)
from .subtitle_service import (
    MultiLanguageStrategy,
    SingleLanguageStrategy,
    SmartStrategy,
    SubtitleService,
    SubtitleStrategy,
    subtitle_service,
)

__all__ = [
    "AudioProcessor",
    "audio_processor",
    "extract_subtitle_tracks",
    "get_subtitle_languages",
    "SubtitleTrack",
    "SubtitleService",
    "subtitle_service",
    "SubtitleStrategy",
    "SmartStrategy",
    "SingleLanguageStrategy",
    "MultiLanguageStrategy",
    "SubtitleProcessor",
    "subtitle_processor",
    "SubtitleProcessResult",
    "TimeRange",
    "parse_time_input",
    "parse_time_range",
    "build_section_opts",
    "lossless_cut",
    "SponsorBlockConfig",
    "sponsorblock_config",
    "build_sponsorblock_opts",
    "extract_chapters",
    "get_available_categories",
]
