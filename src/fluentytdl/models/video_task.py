from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .yt_dto import YtFormatDTO, YtMediaDTO

logger = logging.getLogger(__name__)


@dataclass
class DownloadTaskOptions:
    """封装单个视频特有/覆盖的下载选项 (例如自定义格式和参数)"""

    format: str | None = None
    extra_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class VideoFormat:
    """简化版的单个流格式模型"""

    format_id: str
    ext: str
    vcodec: str | None
    acodec: str | None

    # 视频属性
    height: int | None = None
    fps: float | None = None

    # 音频属性
    abr: int | None = None

    # 文件大小 (以字节计)
    filesize: int | None = None

    # 纯文字展示摘要
    display_text: str = ""


@dataclass
class VideoMetadata:
    """轻量基础元数据，仅用于首屏列表渲染。"""

    id: str | None = None
    title: str = "解析中..."
    uploader: str = ""
    duration_str: str = "--:--"
    duration_sec: int | None = None
    upload_date: str = ""


@dataclass
class ThumbnailState:
    """缩略图请求与缓存状态。"""

    url: str = ""
    status: str = "idle"
    cache_key: str | None = None
    requested_at: float = 0.0
    last_visible_at: float = 0.0


@dataclass
class DetailState:
    """重型详情解析状态，进入可视区后再逐步补全。"""

    status: str = "idle"
    error_message: str = ""
    dto: YtMediaDTO | None = None
    video_formats: list[YtFormatDTO] = field(default_factory=list)
    audio_formats: list[YtFormatDTO] = field(default_factory=list)


@dataclass
class SelectionState:
    """用户在 UI 中产生的本地选择，不与抓取结果耦合。"""

    selected: bool = False
    custom_options: DownloadTaskOptions = field(default_factory=DownloadTaskOptions)
    is_manual_override: bool = False
    override_text: str | None = None
    format_note: str = ""


@dataclass
class VideoTask:
    """
    VideoTask 数据模型防腐层。
    UI 层应当只依赖此对象，绝对禁止 UI 直接消化处理 yt-dlp 生成的原生庞大字典。
    """

    url: str
    metadata: VideoMetadata = field(default_factory=VideoMetadata)
    thumbnail: ThumbnailState = field(default_factory=ThumbnailState)
    detail: DetailState = field(default_factory=lambda: DetailState(status="loading"))
    selection: SelectionState = field(default_factory=SelectionState)
    is_live: bool = False
    is_playlist: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str | None:
        return self.metadata.id

    @id.setter
    def id(self, value: str | None) -> None:
        self.metadata.id = value

    @property
    def title(self) -> str:
        return self.metadata.title

    @title.setter
    def title(self, value: str) -> None:
        self.metadata.title = value

    @property
    def uploader(self) -> str:
        return self.metadata.uploader

    @uploader.setter
    def uploader(self, value: str) -> None:
        self.metadata.uploader = value

    @property
    def duration_str(self) -> str:
        return self.metadata.duration_str

    @duration_str.setter
    def duration_str(self, value: str) -> None:
        self.metadata.duration_str = value

    @property
    def duration_sec(self) -> int | None:
        return self.metadata.duration_sec

    @duration_sec.setter
    def duration_sec(self, value: int | None) -> None:
        self.metadata.duration_sec = value

    @property
    def upload_date(self) -> str:
        return self.metadata.upload_date

    @upload_date.setter
    def upload_date(self, value: str) -> None:
        self.metadata.upload_date = value

    @property
    def thumbnail_url(self) -> str:
        return self.thumbnail.url

    @thumbnail_url.setter
    def thumbnail_url(self, value: str) -> None:
        self.thumbnail.url = value

    @property
    def is_parsing(self) -> bool:
        return self.detail.status == "loading"

    @is_parsing.setter
    def is_parsing(self, value: bool) -> None:
        if value:
            self.detail.status = "loading"
            self.detail.error_message = ""
        elif self.detail.status == "loading":
            self.detail.status = "ready"

    @property
    def has_error(self) -> bool:
        return self.detail.status == "error"

    @has_error.setter
    def has_error(self, value: bool) -> None:
        if value:
            self.detail.status = "error"
        elif self.detail.status == "error":
            self.detail.status = "ready"

    @property
    def error_msg(self) -> str:
        return self.detail.error_message

    @error_msg.setter
    def error_msg(self, value: str) -> None:
        self.detail.error_message = value
        if value:
            self.detail.status = "error"

    @property
    def video_formats(self) -> list[YtFormatDTO]:
        return self.detail.video_formats

    @video_formats.setter
    def video_formats(self, value: list[YtFormatDTO]) -> None:
        self.detail.video_formats = value

    @property
    def audio_formats(self) -> list[YtFormatDTO]:
        return self.detail.audio_formats

    @audio_formats.setter
    def audio_formats(self, value: list[YtFormatDTO]) -> None:
        self.detail.audio_formats = value

    @property
    def selected(self) -> bool:
        return self.selection.selected

    @selected.setter
    def selected(self, value: bool) -> None:
        self.selection.selected = value

    @property
    def custom_options(self) -> DownloadTaskOptions:
        return self.selection.custom_options

    @custom_options.setter
    def custom_options(self, value: DownloadTaskOptions) -> None:
        self.selection.custom_options = value

    @property
    def dto(self) -> YtMediaDTO | None:
        return self.detail.dto

    @dto.setter
    def dto(self, value: YtMediaDTO | None) -> None:
        self.detail.dto = value
        if value is not None and self.detail.status != "error":
            self.detail.status = "ready"

    @property
    def is_manual_override(self) -> bool:
        return self.selection.is_manual_override

    @is_manual_override.setter
    def is_manual_override(self, value: bool) -> None:
        self.selection.is_manual_override = value

    @property
    def override_text(self) -> str | None:
        return self.selection.override_text

    @override_text.setter
    def override_text(self, value: str | None) -> None:
        self.selection.override_text = value

    @property
    def format_note(self) -> str:
        return self.selection.format_note

    @format_note.setter
    def format_note(self, value: str) -> None:
        self.selection.format_note = value

    @classmethod
    def from_dto(cls, url: str, dto: YtMediaDTO | None) -> VideoTask:
        """
        工厂方法：将标准的 YtMediaDTO 实例注入领域模型。
        """
        from ..utils.formatters import format_upload_date

        task = cls(url=url)
        if not dto:
            return task

        task.title = dto.title or "未知标题"
        task.id = dto.id
        task.uploader = dto.uploader or ""

        # 时长
        if dto.duration > 0:
            task.duration_sec = dto.duration
            task.duration_str = dto.duration_str

        # 标签
        task.upload_date = format_upload_date(dto.upload_date)
        task.is_live = dto.is_live

        task.thumbnail_url = dto.thumbnail

        task.dto = dto
        task.is_parsing = False
        return task
