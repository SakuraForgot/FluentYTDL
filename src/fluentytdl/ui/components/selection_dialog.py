from __future__ import annotations

import time
from collections import deque
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    ComboBox,
    ImageLabel,
    IndeterminateProgressRing,
    LineEdit,
    MessageBox,
    MessageBoxBase,
    PrimaryPushButton,
    PushButton,
    RadioButton,
    ScrollArea,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
)

from ...download.workers import EntryDetailWorker, InfoExtractWorker, VRInfoExtractWorker
from ...processing import subtitle_service
from ...utils.filesystem import sanitize_filename
from ...utils.image_loader import ImageLoader
from ...youtube.youtube_service import YoutubeServiceOptions, YtDlpAuthOptions
from .cover_selector import CoverSelectorWidget
from .format_selector import VideoFormatSelectorWidget
from .subtitle_selector import SubtitleSelectorWidget
from .vr_format_selector import VR_PRESETS, VRFormatSelectorWidget

# ---- 字幕容器兼容性辅助函数 ----
# MP4 和 MKV 都支持字幕嵌入（FFmpeg 自动将 SRT 转为 mov_text），只有 WebM 不支持 SRT/ASS
_SUBTITLE_COMPATIBLE_CONTAINERS = {"mp4", "mkv", "mov", "m4v"}


def _ensure_subtitle_compatible_container(opts: dict[str, Any]) -> None:
    """
    确保容器格式兼容字幕嵌入。

    只有当 embedsubtitles=True 时才需要检查：
    - MP4/MKV/MOV 等已经支持字幕嵌入 → 保持不变
    - WebM 不支持 SRT/ASS → 改用 MKV
    - 未指定容器（原盘模式）→ 默认 MKV
    """
    if not opts.get("embedsubtitles"):
        return

    fmt = (opts.get("merge_output_format") or "").lower()
    if fmt in _SUBTITLE_COMPATIBLE_CONTAINERS:
        # MP4/MKV 等已支持，不覆盖用户选择
        return
    elif fmt == "webm":
        # WebM 不支持 SRT/ASS 字幕嵌入
        opts["merge_output_format"] = "mkv"
    elif not fmt:
        # 未指定容器格式（原盘/默认），使用 MKV 确保兼容
        opts["merge_output_format"] = "mkv"
    # 其他格式保持不变，由用户自行负责


def _get_table_selection_qss() -> str:
    from qfluentwidgets import isDarkTheme

    is_dark = isDarkTheme()
    sel_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "#E8E8E8"
    sel_fg = "#ffffff" if is_dark else "#000000"
    sel_bd = "rgba(255, 255, 255, 0.15)" if is_dark else "#C0C0C0"
    hov_bg = "rgba(255, 255, 255, 0.04)" if is_dark else "#F3F3F3"

    return f"""
QTableWidget {{
    background-color: transparent;
    outline: none;
    border: none;
}}
QTableWidget::item {{
    padding-left: 8px;
}}
QTableWidget::item:selected {{
    background-color: {sel_bg};
    color: {sel_fg};
    border: 1px solid {sel_bd};
    border-radius: 6px;
    font-weight: 600;
}}
QTableWidget::item:hover {{
    background-color: {hov_bg};
    border-radius: 6px;
}}
"""


class SimplePresetWidget(QWidget):
    """简易模式下的预设选项卡片"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        # 创建滚动区域
        scroll_area = ScrollArea(self)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        scroll_area.setWidgetResizable(True)
        scroll_area.setMaximumHeight(450)  # 限制最大高度

        # 滚动内容容器
        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        self.v_layout = QVBoxLayout(content_widget)
        self.v_layout.setSpacing(12)
        self.v_layout.setContentsMargins(10, 10, 10, 10)

        self.btn_group = QButtonGroup(self)

        # Define presets
        # (id, title, description, format_selector, post_args)
        self.presets = [
            # === 推荐选项 ===
            (
                "best_mp4",
                "🎬 最佳画质 (MP4)",
                "推荐。自动选择最佳画质并封装为 MP4，兼容性最好。",
                "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4] / bv*+ba/b",
                {"merge_output_format": "mp4"},
            ),
            (
                "best_raw",
                "🎯 最佳画质 (原盘)",
                "追求极致画质。通常为 WebM/MKV 格式，适合本地播放。",
                "bestvideo+bestaudio/best",
                {},
            ),
            # === 分辨率限制 ===
            (
                "2160p",
                "📺 2160p 4K (MP4)",
                "限制最高分辨率为 4K，超高清画质。",
                "bv*[height<=2160][ext=mp4]+ba[ext=m4a]/b[height<=2160][ext=mp4] / bv*[height<=2160]+ba/b[height<=2160]",
                {"merge_output_format": "mp4"},
            ),
            (
                "1440p",
                "📺 1440p 2K (MP4)",
                "限制最高分辨率为 2K，高清画质。",
                "bv*[height<=1440][ext=mp4]+ba[ext=m4a]/b[height<=1440][ext=mp4] / bv*[height<=1440]+ba/b[height<=1440]",
                {"merge_output_format": "mp4"},
            ),
            (
                "1080p",
                "📺 1080p 高清 (MP4)",
                "限制最高分辨率为 1080p，平衡画质与体积。",
                "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4] / bv*[height<=1080]+ba/b[height<=1080]",
                {"merge_output_format": "mp4"},
            ),
            (
                "720p",
                "📺 720p 标清 (MP4)",
                "限制最高分辨率为 720p，适合移动设备。",
                "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4] / bv*[height<=720]+ba/b[height<=720]",
                {"merge_output_format": "mp4"},
            ),
            (
                "480p",
                "📺 480p (MP4)",
                "限制最高分辨率为 480p，节省空间。",
                "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4] / bv*[height<=480]+ba/b[height<=480]",
                {"merge_output_format": "mp4"},
            ),
            (
                "360p",
                "📺 360p (MP4)",
                "限制最高分辨率为 360p，最小体积。",
                "bv*[height<=360][ext=mp4]+ba[ext=m4a]/b[height<=360][ext=mp4] / bv*[height<=360]+ba/b[height<=360]",
                {"merge_output_format": "mp4"},
            ),
            # === 纯音频 ===
            (
                "audio_mp3",
                "🎵 纯音频 (MP3 - 320k)",
                "仅下载音频并转码为 MP3。",
                "bestaudio/best",
                {"extract_audio": True, "audio_format": "mp3", "audio_quality": "320K"},
            ),
        ]

        self.radios = []

        for i, (pid, title, desc, fmt, args) in enumerate(self.presets):
            container = QFrame(self)
            container.setStyleSheet(
                ".QFrame { background-color: rgba(255, 255, 255, 0.05); border-radius: 6px; border: 1px solid rgba(0,0,0,0.05); }"
            )
            h_layout = QHBoxLayout(container)

            rb = RadioButton(title, container)
            # Store preset data in dynamic properties for easy retrieval
            rb.setProperty("preset_id", pid)
            rb.setProperty("format_str", fmt)
            rb.setProperty("extra_args", args)

            self.btn_group.addButton(rb, i)
            self.radios.append(rb)

            desc_label = CaptionLabel(desc, container)
            desc_label.setTextColor(QColor(120, 120, 120), QColor(150, 150, 150))
            desc_label.setWordWrap(True)

            h_layout.addWidget(rb)
            h_layout.addWidget(desc_label, 1)

            self.v_layout.addWidget(container)

        # 设置滚动区域
        scroll_area.setWidget(content_widget)
        main_layout.addWidget(scroll_area)

        # Select first by default
        if self.radios:
            self.radios[0].setChecked(True)

    def get_current_selection(self) -> dict:
        """Return format selector string and extra args."""
        btn = self.btn_group.checkedButton()
        if not btn:
            return {}
        return {
            "format": btn.property("format_str"),
            "extra": btn.property("extra_args"),
            "id": btn.property("preset_id"),
        }


def _format_duration(seconds: Any) -> str:
    try:
        s = int(seconds)
    except Exception:
        return "--:--"
    if s < 0:
        return "--:--"
    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def _format_upload_date(value: Any) -> str:
    s = str(value or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s or "-"


def _format_size(value: Any) -> str:
    try:
        n = int(value)
    except Exception:
        return "-"
    if n <= 0:
        return "-"
    units = ["B", "KB", "MB", "GB"]
    x = float(n)
    for u in units:
        if x < 1024 or u == units[-1]:
            if u in ("B", "KB"):
                return f"{int(round(x))}{u}"
            return f"{x:.1f}{u}"
        x /= 1024
    return f"{n}B"


class PlaylistPreviewWidget(QWidget):
    """Left preview widget: checkbox + 16:9 thumbnail."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self.checkbox = QCheckBox(self)
        self.checkbox.setTristate(False)

        self.thumb_label = QLabel(self)
        self.thumb_label.setFixedSize(150, 84)
        self.thumb_label.setScaledContents(True)
        from qfluentwidgets import isDarkTheme

        thumb_bg = "rgba(255, 255, 255, 0.06)" if isDarkTheme() else "rgba(0, 0, 0, 0.06)"
        self.thumb_label.setStyleSheet(f"background-color: {thumb_bg}; border-radius: 8px;")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.thumb_label)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        try:
            if event.button() == Qt.MouseButton.LeftButton:
                self.checkbox.setChecked(not self.checkbox.isChecked())
                event.accept()
                return
        except Exception:
            pass
        super().mousePressEvent(event)


class PlaylistInfoWidget(QWidget):
    """Middle info widget: title + meta line."""

    def __init__(self, title: str, meta: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        layout.setSpacing(2)
        # Allow the info area to use full column width; avoid squeezing text into a narrow left block.
        layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.titleLabel = BodyLabel(title or "-", self)
        self.titleLabel.setWordWrap(True)
        self.titleLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        try:
            self.titleLabel.setMaximumHeight(self.titleLabel.fontMetrics().lineSpacing() * 2 + 4)
        except Exception:
            pass

        self.metaLabel = CaptionLabel(meta or "", self)
        self.metaLabel.setWordWrap(True)
        self.metaLabel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        layout.addWidget(self.titleLabel)
        layout.addWidget(self.metaLabel)


class PlaylistActionWidget(QWidget):
    """Right action/status widget: quality selector + status."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(4)
        layout.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        top.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        self.loadingRing = IndeterminateProgressRing(self)
        self.loadingRing.setFixedSize(14, 14)
        self.loadingRing.hide()

        self.qualityButton = PushButton("待加载", self)
        self.qualityButton.setToolTip("点击获取信息/选择格式")
        self.qualityButton.installEventFilter(
            ToolTipFilter(self.qualityButton, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.qualityButton.setMinimumWidth(140)

        top.addWidget(self.loadingRing)
        top.addWidget(self.qualityButton)

        self.infoLabel = CaptionLabel("", self)
        self.infoLabel.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.infoLabel.setWordWrap(True)

        layout.addLayout(top)
        layout.addWidget(self.infoLabel, 0, Qt.AlignmentFlag.AlignHCenter)

    def set_loading(self, loading: bool, text: str | None = None) -> None:
        self.loadingRing.setVisible(bool(loading))
        if text is not None:
            self.qualityButton.setText(str(text))


def _infer_entry_url(entry: dict[str, Any]) -> str:
    # Prefer webpage_url / original_url over url.
    # When yt-dlp -J is combined with -S lang:xx, the top-level "url" field may
    # become the HLS manifest URL of the *sorted-best* format rather than the
    # original YouTube watch page URL.  Passing that HLS URL to the download
    # worker causes the [generic] extractor to kick in, which does not support
    # format selection and fails with "Requested format is not available".
    for key in ("webpage_url", "original_url"):
        val = str(entry.get(key) or "").strip()
        if val.startswith("http://") or val.startswith("https://"):
            return val

    url = str(entry.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url
    vid = str(entry.get("id") or url).strip()
    if vid:
        return f"https://www.youtube.com/watch?v={vid}"
    return url


def _infer_entry_thumbnail(entry: dict[str, Any]) -> str:
    """推断视频条目的缩略图 URL，优先使用中等质量以加速加载"""
    thumb = str(entry.get("thumbnail") or "").strip()

    # 尝试从 thumbnails 列表中找到合适尺寸的缩略图
    thumbs = entry.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        # 优先选择中等质量（~320x180），避免加载过大的图片
        preferred_ids = {"mqdefault", "medium", "default", "sddefault", "hqdefault"}
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            t_id = str(t.get("id") or "").lower()
            if t_id in preferred_ids:
                u = str(t.get("url") or "").strip()
                if u:
                    return u

        # 如果没有找到首选，选择宽度在 200-400 之间的
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            w = t.get("width") or 0
            if 200 <= w <= 400:
                u = str(t.get("url") or "").strip()
                if u:
                    return u

        # 最后回退到第一个可用的
        for t in thumbs:
            if not isinstance(t, dict):
                continue
            u = str(t.get("url") or t.get("src") or "").strip()
            if u:
                return u

    # 如果有直接的 thumbnail 字段，尝试转换为中等质量
    if thumb:
        # YouTube URL 优化：maxresdefault/hqdefault -> mqdefault
        if "i.ytimg.com" in thumb or "i9.ytimg.com" in thumb:
            for high_res in ["maxresdefault", "hqdefault", "sddefault"]:
                if high_res in thumb:
                    return thumb.replace(high_res, "mqdefault")
        return thumb

    return ""


def _clean_video_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    if not isinstance(formats, list):
        return []

    out: list[dict[str, Any]] = []
    seen_height: set[int] = set()
    for f in formats:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") == "none":
            continue
        h = int(f.get("height") or 0)
        if h < 360:
            continue
        if h in seen_height:
            continue
        ext = f.get("ext") or "?"
        fps = f.get("fps")
        res_str = f"{h}p"
        try:
            if fps and float(fps) > 30:
                res_str += f" {int(float(fps))}fps"
        except Exception:
            pass

        # Add filesize to text
        sz = _format_size(f.get("filesize") or f.get("filesize_approx"))
        display_text = f"{res_str} - {ext} ({sz})"

        out.append(
            {
                "text": display_text,
                "id": f.get("format_id"),
                "height": h,
                "fps": fps,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "vcodec": f.get("vcodec"),
            }
        )
        seen_height.add(h)

    out.sort(key=lambda x: int(x.get("height") or 0), reverse=True)
    return out


def _clean_audio_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    formats = info.get("formats") or []
    if not isinstance(formats, list):
        return []

    out: list[dict[str, Any]] = []
    seen_key: set[tuple[int, str, str]] = set()
    for f in formats:
        if not isinstance(f, dict):
            continue
        # audio-only streams
        if f.get("vcodec") != "none":
            continue
        if f.get("acodec") in (None, "none"):
            continue

        abr_raw = f.get("abr") or f.get("tbr") or 0
        try:
            abr = int(float(abr_raw) or 0)
        except Exception:
            abr = 0
        if abr <= 0:
            continue
        ext = str(f.get("ext") or "?").strip().lower() or "?"
        acodec = str(f.get("acodec") or "").strip().lower()
        key = (abr, ext, acodec)
        if key in seen_key:
            continue

        # Add filesize to text
        sz = _format_size(f.get("filesize") or f.get("filesize_approx"))
        display_text = f"{abr}kbps - {ext} ({sz})"

        out.append(
            {
                "text": display_text,
                "id": f.get("format_id"),
                "abr": abr,
                "ext": ext,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }
        )
        seen_key.add(key)

    out.sort(key=lambda x: int(x.get("abr") or 0), reverse=True)
    return out


def _choose_lossless_merge_container(video_ext: str | None, audio_ext: str | None) -> str | None:
    """Choose a container for lossless (stream-copy) merging.

    - Keep original container when it's a common compatible pair (mp4+m4a -> mp4, webm+webm -> webm)
    - Otherwise fallback to mkv (remux only, no re-encode)
    """

    v = str(video_ext or "").strip().lower()
    a = str(audio_ext or "").strip().lower()
    if not v or not a:
        return None

    if v == "webm" and a == "webm":
        return "webm"

    if v in {"mp4", "m4v"} and a in {"m4a", "aac", "mp4"}:
        return "mp4"

    # Best-effort universal container for incompatible pairs
    return "mkv"


class PlaylistFormatDialog(MessageBoxBase):
    """用于播放列表单项的“高级格式选择”弹窗 (复用各类 SelectorWidget)"""

    def __init__(
        self, info: dict[str, Any], parent=None, *, vr_mode: bool = False, mode: str = "default"
    ):
        super().__init__(parent)
        self.widget.setMinimumSize(700, 500)
        self._mode = mode

        self.titleLabel = SubtitleLabel("选择格式", self)
        self.viewLayout.addWidget(self.titleLabel)

        if self._mode == "subtitle":
            self.titleLabel.setText("选择字幕")
            self.selector = SubtitleSelectorWidget(info, self)
        elif self._mode == "cover":
            self.titleLabel.setText("选择封面")
            self.selector = CoverSelectorWidget(info, self)
        elif vr_mode:
            self.selector = VRFormatSelectorWidget(info, self)
        else:
            self.selector = VideoFormatSelectorWidget(info, self)

        self.viewLayout.addWidget(self.selector)

        # Override buttons
        self.yesButton.setText("应用")
        self.cancelButton.setText("取消")

        # Connect selector signal to valid state (optional, defaults are usually valid)
        # self.selector.selectionChanged.connect(self._validate_selection)

    def get_selection(self) -> dict:
        if self._mode == "subtitle":
            opts = self.selector.get_opts()
            return {"extra_opts": opts, "format": "subtitle_custom"}
        elif self._mode == "cover":
            url = self.selector.get_selected_url()
            ext = self.selector.get_selected_ext()
            return {"cover_url": url, "cover_ext": ext, "format": "cover_custom"}
        else:
            return self.selector.get_selection_result()

    def get_summary(self) -> str:
        if self._mode == "subtitle":
            selected = self.selector.get_selected_tracks()
            if not selected:
                return "未选择字幕"
            return f"已选择 {len(selected)} 种语言"
        elif self._mode == "cover":
            ext = self.selector.get_selected_ext()
            return f"已选择 {ext.upper()} 封面"
        else:
            return self.selector.get_summary_text()


class SelectionDialog(MessageBoxBase):
    """智能解析与格式选择弹窗"""

    def __init__(self, url: str, parent=None, *, vr_mode: bool = False, mode: str = "default"):
        super().__init__(parent)
        self.url = url
        self._vr_mode = vr_mode or (mode == "vr")
        self._mode = mode  # default, vr, subtitle, cover
        self.video_info: dict[str, Any] | None = None
        self._is_playlist = False
        self.download_tasks: list[dict[str, Any]] = []
        try:
            from ...core.config_manager import config_manager

            self._download_dir = str(config_manager.get("download_dir") or "").strip()
        except Exception:
            self._download_dir = ""
        self._download_dir_edit: LineEdit | None = None

        # 缓存字幕用户选择，避免在 get_selected_tasks() 中重复弹窗
        self._subtitle_embed_choice: bool | None = None
        self._subtitle_choice_made = False

        self.image_loader = ImageLoader(self)
        self.image_loader.loaded.connect(self._on_thumb_loaded)
        self.image_loader.loaded_with_url.connect(self._on_thumb_loaded_with_url)
        self.image_loader.failed.connect(self._on_thumb_failed)

        self.thumb_label: ImageLabel | None = None

        # legacy single-video combo UI (kept for backward compatibility)
        self.type_combo: ComboBox | None = None
        self.format_combo: ComboBox | None = None
        self.preset_combo: ComboBox | None = None

        # single-video format selection state
        self._single_mode_combo: ComboBox | None = None
        self._single_table: QTableWidget | None = None
        self._single_hint: CaptionLabel | None = None
        self._single_selection_label: CaptionLabel | None = None
        self._single_rows: list[dict[str, Any]] = []
        self._single_selected_video_id: str | None = None
        self._single_selected_audio_id: str | None = None
        self._single_selected_muxed_id: str | None = None

        # playlist UI state
        self._playlist_rows: list[dict[str, Any]] = []
        self._table: QTableWidget | None = None
        self._thumb_label_by_row: dict[int, QLabel] = {}
        self._preview_widget_by_row: dict[int, PlaylistPreviewWidget] = {}
        self._action_widget_by_row: dict[int, PlaylistActionWidget] = {}
        self._thumb_cache: dict[str, Any] = {}
        self._thumb_url_to_rows: dict[str, set[int]] = {}
        self._thumb_requested: set[str] = set()
        self._thumb_pending: list[str] = []  # 待加载的缩略图队列
        self._thumb_inflight: int = 0  # 当前正在下载的数量
        self._thumb_max_concurrent: int = 12  # 最大并发数（图片较小可以更高）

        self._detail_queue: deque[int] = deque()
        self._detail_inflight_row: int | None = None
        self._detail_loaded: set[int] = set()
        self._last_interaction = time.monotonic()

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(2000)
        self._idle_timer.timeout.connect(self._on_idle_tick)

        # 缩略图延迟加载定时器（等待表格布局完成）
        self._thumb_init_timer = QTimer(self)
        self._thumb_init_timer.setSingleShot(True)
        self._thumb_init_timer.setInterval(0)  # 0ms - 在当前事件循环完成后立即执行
        self._thumb_init_timer.timeout.connect(self._on_thumb_init_timeout)

        # UI 初始化：顶部标题（主要用于播放列表；单视频解析成功时隐藏）
        self.titleLabel = SubtitleLabel("", self)
        self.titleLabel.hide()
        self.viewLayout.addWidget(self.titleLabel)

        # 解析中：居中显示（避免左上角一行字 + 巨大空白）
        self.loadingWidget = QWidget(self)
        self.loadingLayout = QVBoxLayout(self.loadingWidget)
        self.loadingLayout.setContentsMargins(0, 0, 0, 0)
        self.loadingLayout.setSpacing(12)
        self.loadingLayout.addStretch(1)

        self.loadingTitleLabel = SubtitleLabel(
            "正在使用 VR 模式解析..." if self._vr_mode else "正在解析链接...",
            self.loadingWidget,
        )
        self.loadingTitleLabel.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.loadingLayout.addWidget(self.loadingTitleLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        self.loadingRing = IndeterminateProgressRing(self.loadingWidget)
        self.loadingRing.setFixedSize(46, 46)
        self.loadingLayout.addWidget(self.loadingRing, 0, Qt.AlignmentFlag.AlignCenter)

        self.loadingLayout.addStretch(1)
        self.viewLayout.addWidget(self.loadingWidget)

        # 内容容器 (初始隐藏)
        self.contentWidget = QWidget()
        self.contentLayout = QVBoxLayout(self.contentWidget)
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(12)
        self.viewLayout.addWidget(self.contentWidget)
        self.contentWidget.hide()

        # 失败重试区（默认隐藏）：用于“需要 Cookies / 不是机器人验证”场景
        self.retryWidget = QWidget(self)
        self.retryLayout = QVBoxLayout(self.retryWidget)
        self.retryLayout.setContentsMargins(0, 0, 0, 0)
        self.retryLayout.setSpacing(8)

        self.retryHint = CaptionLabel(
            "检测到需要身份验证时，可选择从浏览器注入 Cookies 后重试解析。",
            self.retryWidget,
        )
        self.retryLayout.addWidget(self.retryHint)

        self.cookies_combo = ComboBox(self.retryWidget)
        self.cookies_combo.addItems(
            ["不使用 Cookies", "Edge Cookies", "Chrome Cookies", "Firefox Cookies"]
        )
        self.retryLayout.addWidget(self.cookies_combo)

        self.retryBtn = PrimaryPushButton("重试解析", self.retryWidget)
        self.retryBtn.clicked.connect(self._on_retry_clicked)
        self.retryLayout.addWidget(self.retryBtn)

        self.viewLayout.addWidget(self.retryWidget)
        self.retryWidget.hide()

        self._error_label: CaptionLabel | None = None

        # 格式缓存
        self.video_formats: list[dict[str, Any]] = []

        self._current_options: YoutubeServiceOptions | None = None

        # Close/cancel should stop background parsing to avoid crashes and wasted work.
        self._is_closing = False
        self.worker: InfoExtractWorker | VRInfoExtractWorker | None = None
        self._detail_worker: EntryDetailWorker | None = None

        # 启动解析线程
        self.start_extraction()

        # 按钮设置
        self.yesButton.setText("下载")
        self.cancelButton.setText("取消")
        self.yesButton.setDisabled(True)

        try:
            self.cancelButton.clicked.connect(self._on_user_cancel)
        except Exception:
            pass

        # 默认尺寸（解析前先用更紧凑的窗口；解析成功后会按模式调整）
        self.widget.setMinimumSize(760, 480)
        try:
            self.widget.resize(760, 480)
        except Exception:
            pass

    def _ensure_download_dir_bar(self) -> None:
        wrap = QWidget(self.contentWidget)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        label = CaptionLabel("下载位置", wrap)
        edit = LineEdit(wrap)
        edit.setText(self._download_dir)
        try:
            edit.setClearButtonEnabled(True)
        except Exception:
            pass

        def _on_text_changed(text: str) -> None:
            self._download_dir = str(text or "").strip()

        edit.textChanged.connect(_on_text_changed)

        pick_btn = PushButton("选择...", wrap)
        pick_btn.clicked.connect(self._on_pick_download_dir)

        row.addWidget(label)
        row.addWidget(edit, 1)
        row.addWidget(pick_btn)

        self._download_dir_edit = edit
        self.contentLayout.addWidget(wrap)

    def _on_pick_download_dir(self) -> None:
        start_dir = self._download_dir or ""
        folder = QFileDialog.getExistingDirectory(self, "选择下载目录", start_dir)
        if not folder:
            return
        self._download_dir = str(folder).strip()
        if self._download_dir_edit is not None:
            self._download_dir_edit.setText(self._download_dir)

    def _apply_download_dir_to_opts(self, opts: dict[str, Any]) -> None:
        p = str(self._download_dir or "").strip()
        if not p:
            return
        opts["paths"] = {"home": p}
        outtmpl = opts.get("outtmpl")
        if not isinstance(outtmpl, str) or not outtmpl.strip():
            opts["outtmpl"] = "%(title)s.%(ext)s"
        elif ("/" in outtmpl or "\\" in outtmpl) and "%(title)s.%(ext)s" in outtmpl:
            opts["outtmpl"] = "%(title)s.%(ext)s"

    def _apply_dialog_size_for_mode(self) -> None:
        if self._is_playlist:
            size = (980, 620)
        else:
            size = (760, 480)

        self.widget.setMinimumSize(*size)
        try:
            self.widget.resize(*size)
        except Exception:
            pass

    def _on_user_cancel(self) -> None:
        self._stop_background_parsing()

    def reject(self) -> None:  # type: ignore[override]
        self._stop_background_parsing()
        super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._stop_background_parsing()
        super().closeEvent(event)

    def _stop_background_parsing(self) -> None:
        if self._is_closing:
            return
        self._is_closing = True

        try:
            self._idle_timer.stop()
        except Exception:
            pass

        try:
            self._detail_queue.clear()
        except Exception:
            pass

        try:
            if self.worker is not None:
                self.worker.cancel()
        except Exception:
            pass

        try:
            if self._detail_worker is not None:
                self._detail_worker.cancel()
        except Exception:
            pass

    def start_extraction(self) -> None:
        # If user retries or closes quickly, stop any previous parsing.
        self._is_closing = False
        try:
            if self.worker is not None:
                self.worker.cancel()
        except Exception:
            pass

        self._set_loading_ui(
            "正在使用 VR 模式解析..." if self._vr_mode else "正在解析链接...",
            show_ring=True,
        )
        # Start with no cookies; user can retry with cookies.
        self._current_options = None
        if self._vr_mode:
            w = VRInfoExtractWorker(self.url)
        else:
            w = InfoExtractWorker(self.url, self._current_options)
        w.finished.connect(self.on_parse_success)
        w.error.connect(self.on_parse_error)
        self.worker = w
        w.start()

    def _set_loading_ui(self, title: str, *, show_ring: bool) -> None:
        self.loadingTitleLabel.setText(title)
        self.loadingRing.setVisible(show_ring)
        self.loadingWidget.show()
        self.contentWidget.hide()
        self.titleLabel.hide()

    def on_parse_success(self, info: dict[str, Any]) -> None:
        if self._is_closing:
            return
        self.video_info = info
        self.loadingWidget.hide()
        self.retryWidget.hide()
        if self._error_label is not None:
            self._error_label.deleteLater()
            self._error_label = None
        # Rebuild content each time (retry can be triggered)
        self._clear_content_layout()

        self._is_playlist = str(info.get("_type") or "").lower() == "playlist" or bool(
            info.get("entries")
        )
        self._apply_dialog_size_for_mode()
        if self._is_playlist:
            self.titleLabel.show()
            self.yesButton.setEnabled(False)
            self.setup_playlist_ui(info)
        else:
            # 单视频：不占用额外纵向空间显示“解析成功”，用顶部信息区承载
            self.titleLabel.hide()
            self.yesButton.setEnabled(True)
            self.setup_content_ui(info)

        self.contentWidget.show()

    def _clear_content_layout(self) -> None:
        def _clear_layout(layout) -> None:
            while layout.count():
                child = layout.takeAt(0)
                w = child.widget()
                if w is not None:
                    w.deleteLater()
                    continue
                child_layout = child.layout()
                if child_layout is not None:
                    _clear_layout(child_layout)

        _clear_layout(self.contentLayout)

    def on_parse_error(self, err_data: dict) -> None:
        if self._is_closing:
            return
        self.loadingWidget.hide()
        self.titleLabel.setText("解析失败")
        self.titleLabel.show()
        if self._error_label is not None:
            self._error_label.deleteLater()

        title = str(err_data.get("title") or "解析失败")
        content = str(err_data.get("content") or "")
        suggestion = str(err_data.get("suggestion") or "")
        raw_error = str(err_data.get("raw_error") or "")

        text = f"{title}\n\n{content}"
        if suggestion:
            text += f"\n\n建议操作：\n{suggestion}"
        self._error_label = CaptionLabel(text, self)
        try:
            self._error_label.setWordWrap(True)
        except Exception:
            pass
        self.viewLayout.addWidget(self._error_label)

        lower = raw_error.lower()
        if "cookies" in lower or "not a bot" in lower or "sign in" in lower:
            self.retryWidget.show()

    def _on_retry_clicked(self) -> None:
        # Cancel any in-flight parsing before restarting.
        self._is_closing = False
        try:
            if self.worker is not None:
                self.worker.cancel()
        except Exception:
            pass

        # Build options based on user choice
        idx = self.cookies_combo.currentIndex()
        cookies_from_browser: str | None = None
        if idx == 1:
            cookies_from_browser = "edge"
        elif idx == 2:
            cookies_from_browser = "chrome"
        elif idx == 3:
            cookies_from_browser = "firefox"

        options: YoutubeServiceOptions | None = None
        if cookies_from_browser:
            options = YoutubeServiceOptions(
                auth=YtDlpAuthOptions(cookies_from_browser=cookies_from_browser)
            )

        self._current_options = options

        # Reset UI state
        self.yesButton.setDisabled(True)
        self.video_info = None
        self._set_loading_ui("正在解析链接...", show_ring=True)

        if self._error_label is not None:
            self._error_label.deleteLater()
            self._error_label = None

        # Restart worker
        w = InfoExtractWorker(self.url, self._current_options)
        w.finished.connect(self.on_parse_success)
        w.error.connect(self.on_parse_error)
        self.worker = w
        w.start()

    def setup_content_ui(self, info: dict[str, Any]) -> None:
        # Backward-compatible entry point
        self.setup_single_ui(info)

    # ==========================================
    # 场景 B: 单视频 UI
    # ==========================================
    def setup_single_ui(self, info: dict[str, Any]) -> None:
        # 1. 顶部缩略图和信息区域
        h_layout = QHBoxLayout()
        h_layout.setSpacing(20)
        h_layout.setContentsMargins(0, 0, 0, 0)

        # 左侧缩略图
        self.thumb_label = ImageLabel(self.contentWidget)
        self.thumb_label.setFixedSize(200, 112)
        self.thumb_label.setBorderRadius(8, 8, 8, 8)

        thumbnail_url = str(info.get("thumbnail") or "").strip()
        if thumbnail_url:
            if "hqdefault" in thumbnail_url:
                thumbnail_url = thumbnail_url.replace("hqdefault", "maxresdefault")
            self.image_loader.load(thumbnail_url, target_size=(200, 112), radius=8)

        # 右侧信息
        v_info = QVBoxLayout()
        v_info.setAlignment(Qt.AlignmentFlag.AlignTop)
        v_info.setSpacing(6)

        title = str(info.get("title") or "Unknown")
        uploader = str(info.get("uploader") or "Unknown")
        duration_str = str(info.get("duration_string") or "").strip()
        if not duration_str:
            duration_str = _format_duration(info.get("duration"))

        upload_date = _format_upload_date(info.get("upload_date"))
        view_count = info.get("view_count")
        views_str = f"{int(view_count):,} 次观看" if view_count is not None else ""

        title_label = SubtitleLabel(title, self)
        title_label.setWordWrap(True)
        # 移除固定高度限制，让标题自然撑开容器
        # 如果需要限制最大行数，可使用 CSS 的 line-clamp（但 Qt 不原生支持）
        # 或者通过 elide 手动裁剪文本（但会失去完整性）
        # 方案一：完全自适应，让标题自然折行
        v_info.addWidget(title_label)

        meta_line1 = CaptionLabel(f"{uploader} • {duration_str}", self)
        v_info.addWidget(meta_line1)

        extra = [s for s in (upload_date, views_str) if s and s != "-"]
        if extra:
            v_info.addWidget(CaptionLabel(" • ".join(extra), self))

        h_layout.addWidget(self.thumb_label, 0, Qt.AlignmentFlag.AlignTop)
        h_layout.addLayout(v_info, 1)

        self.contentLayout.addLayout(h_layout)
        self.contentLayout.addSpacing(12)

        # 2. VR Projection Banner (VR mode only)
        if self._vr_mode:
            vr_summary = info.get("__vr_projection_summary") or {}
            if vr_summary:
                from qfluentwidgets import CardWidget as _CW

                banner = _CW(self.contentWidget)
                banner.setStyleSheet(
                    "CardWidget { background-color: rgba(0, 120, 215, 0.06); "
                    "border-radius: 8px; border: 1px solid rgba(0, 120, 215, 0.15); }"
                )
                b_layout = QVBoxLayout(banner)
                b_layout.setContentsMargins(16, 12, 16, 12)
                b_layout.setSpacing(4)

                # Build banner text
                stereo = vr_summary.get("primary_stereo", "unknown")
                proj = vr_summary.get("primary_projection", "unknown")

                stereo_map = {
                    "stereo_tb": "\U0001f453 \u7acb\u4f53 3D \u89c6\u9891 (\u4e0a\u4e0b\u5e03\u5c40)",
                    "stereo_sbs": "\U0001f453 \u7acb\u4f53 3D \u89c6\u9891 (\u5de6\u53f3\u5e03\u5c40)",
                    "mono": "\U0001f310 2D \u5168\u666f\u89c6\u9891",
                }
                proj_map = {
                    "equirectangular": "Equirectangular \u6295\u5f71",
                    "mesh": "Mesh \u6295\u5f71 (\u9c7c\u773c)",
                    "eac": "EAC \u6295\u5f71 (\u7acb\u65b9\u4f53)",
                }

                title_text = stereo_map.get(stereo, "\U0001f941 VR \u89c6\u9891")
                proj_text = proj_map.get(proj, "\u672a\u77e5\u6295\u5f71")

                b_title = BodyLabel(title_text, banner)
                b_title.setStyleSheet("font-weight: 600; font-size: 14px;")
                b_layout.addWidget(b_title)

                hint = CaptionLabel(
                    f"\u6295\u5f71\u7c7b\u578b: {proj_text}  \u2022  "
                    f"\u64ad\u653e\u65f6\u8bf7\u5728\u64ad\u653e\u5668\u624b\u52a8\u9009\u62e9 VR \u6a21\u5f0f",
                    banner,
                )
                b_layout.addWidget(hint)

                # EAC warning
                if vr_summary.get("eac_only"):
                    warn = CaptionLabel(
                        "\u26a0\ufe0f \u8be5\u89c6\u9891\u4ec5\u6709 EAC \u6295\u5f71\u6d41\uff0c"
                        "\u666e\u901a\u64ad\u653e\u5668\u53ef\u80fd\u65e0\u6cd5\u6b63\u786e\u663e\u793a\u3002"
                        "\u5efa\u8bae\u4f7f\u7528 VR \u5934\u663e\u6216\u4e13\u4e1a\u64ad\u653e\u5668\u3002",
                        banner,
                    )
                    warn.setStyleSheet("color: #DC3545;")
                    b_layout.addWidget(warn)

                self.contentLayout.addWidget(banner)
                self.contentLayout.addSpacing(8)

        # 3. Format Selector / Mode Specific UI
        if self._mode == "subtitle":
            self.yesButton.setText("下载字幕")
            self._subtitle_selector = SubtitleSelectorWidget(info, self.contentWidget)
            # 隐藏不需要的选项（如嵌入，因为这是纯字幕下载）
            self._subtitle_selector.embedCheck.setChecked(False)
            self._subtitle_selector.embedCheck.hide()
            self.contentLayout.addWidget(self._subtitle_selector)

        elif self._mode == "cover":
            self.yesButton.setText("下载封面")

            self._cover_selector = CoverSelectorWidget(info, self.contentWidget)
            self.contentLayout.addWidget(self._cover_selector)

        elif self._vr_mode:
            self._format_selector = VRFormatSelectorWidget(info, self.contentWidget)
            self.contentLayout.addWidget(self._format_selector)
        else:
            self._format_selector = VideoFormatSelectorWidget(info, self.contentWidget)
            self.contentLayout.addWidget(self._format_selector)

        if self._mode not in ("subtitle", "cover"):
            self._ensure_download_dir_bar()

    # =========================
    # Playlist UI
    # =========================
    def setup_playlist_ui(self, info: dict[str, Any]) -> None:
        title = str(info.get("title") or "播放列表")
        count = 0
        entries = info.get("entries") or []
        if isinstance(entries, list):
            count = len(entries)

        self.titleLabel.setText(f"播放列表：{title}（{count} 条）")

        # show playlist title
        self.titleLabel.show()

        # header row (progress)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        self.progressRing = IndeterminateProgressRing(self.contentWidget)
        self.progressRing.setFixedSize(16, 16)
        self.progressRing.hide()

        self.progressLabel = CaptionLabel("详情补全：0/0", self.contentWidget)
        header_row.addStretch(1)
        header_row.addWidget(self.progressRing)
        header_row.addWidget(self.progressLabel)
        self.contentLayout.addLayout(header_row)

        # batch actions row
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(8)

        self.selectAllBtn = PushButton("全选", self.contentWidget)
        self.unselectAllBtn = PushButton("取消", self.contentWidget)
        self.invertSelectBtn = PushButton("反选", self.contentWidget)

        self.applyPresetBtn = PrimaryPushButton("重新套用预设", self.contentWidget)

        self.type_combo = ComboBox(self.contentWidget)
        # 0=音视频，1=仅视频，2=仅音频
        self.type_combo.addItems(["音视频", "仅视频", "仅音频"])
        self.type_combo.currentIndexChanged.connect(self._on_playlist_type_changed)

        self.preset_combo = ComboBox(self.contentWidget)
        if self._vr_mode:
            # VR 模式使用场景化预设
            for pid, title, _, _, _ in VR_PRESETS:
                self.preset_combo.addItem(title, userData=pid)
        else:
            # 普通模式使用分辨率预设
            self.preset_combo.addItems(
                [
                    "最高质量(自动)",
                    "2160p(严格)",
                    "1440p(严格)",
                    "1080p(严格)",
                    "720p(严格)",
                    "480p(严格)",
                    "360p(严格)",
                ]
            )
        self.preset_combo.currentIndexChanged.connect(self._on_playlist_preset_changed)

        toolbar.addWidget(self.selectAllBtn)
        toolbar.addWidget(self.unselectAllBtn)
        toolbar.addWidget(self.invertSelectBtn)
        toolbar.addSpacing(10)
        toolbar.addWidget(CaptionLabel("下载类型:", self.contentWidget))
        toolbar.addWidget(self.type_combo)
        toolbar.addWidget(CaptionLabel("质量预设:", self.contentWidget))
        toolbar.addWidget(self.preset_combo)
        toolbar.addWidget(self.applyPresetBtn)
        toolbar.addStretch(1)
        self.contentLayout.addLayout(toolbar)

        # table
        table = QTableWidget(self.contentWidget)
        self._table = table
        table.setStyleSheet(_get_table_selection_qss())
        table.setColumnCount(3)
        table.setHorizontalHeaderLabels(["预览", "信息", "操作"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setVisible(False)
        table.setAlternatingRowColors(False)
        table.setShowGrid(False)
        table.verticalScrollBar().valueChanged.connect(self._on_table_scrolled)

        try:
            header = table.horizontalHeader()
            header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
            table.setColumnWidth(0, 190)
            table.setColumnWidth(2, 170)
        except Exception:
            pass

        self.contentLayout.addWidget(table)

        # wire actions
        self.selectAllBtn.clicked.connect(self._select_all)
        self.unselectAllBtn.clicked.connect(self._unselect_all)
        self.invertSelectBtn.clicked.connect(self._invert_select)
        self.applyPresetBtn.clicked.connect(self._apply_preset_to_selected)

        # fill rows
        self._build_playlist_rows(info)
        self._refresh_progress_label()
        self._update_download_btn_state()

        # kick off progressive detail fill
        self._idle_timer.start()
        # Ensure visible rows are queued immediately
        QTimer.singleShot(100, self._enqueue_detail_for_visible_rows)
        self._maybe_start_next_detail()

        # 延迟加载缩略图（等待表格布局完成）
        self._thumb_init_timer.start()

        self._ensure_download_dir_bar()

    def _build_playlist_rows(self, info: dict[str, Any]) -> None:
        entries = info.get("entries") or []
        if not isinstance(entries, list):
            entries = []

        self._playlist_rows = []
        self._thumb_label_by_row = {}
        self._thumb_url_to_rows = {}
        self._thumb_requested = set()
        self._preview_widget_by_row = {}
        self._action_widget_by_row = {}

        table = self._table
        if table is None:
            return

        table.blockSignals(True)
        table.setRowCount(len(entries))

        for row, e in enumerate(entries):
            if not isinstance(e, dict):
                e = {}

            url = _infer_entry_url(e)
            title = str(e.get("title") or "-")
            uploader = str(e.get("uploader") or e.get("channel") or e.get("uploader_id") or "-")
            duration = _format_duration(e.get("duration"))
            upload_date = _format_upload_date(e.get("upload_date"))
            playlist_index = str(e.get("playlist_index") or (row + 1))
            vid = str(e.get("id") or "-")
            thumb = _infer_entry_thumbnail(e)

            self._playlist_rows.append(
                {
                    "url": url,
                    "title": title,
                    "uploader": uploader,
                    "duration": duration,
                    "upload_date": upload_date,
                    "playlist_index": playlist_index,
                    "id": vid,
                    "thumbnail": thumb,
                    "selected": False,
                    "status": "未选择",
                    "detail": None,
                    "video_formats": [],
                    "audio_formats": [],
                    "highest_height": None,
                    "override_format_id": None,
                    "override_text": None,
                    "audio_best_format_id": None,
                    "audio_best_text": None,
                    "audio_override_format_id": None,
                    "audio_override_text": None,
                    "audio_manual_override": False,
                    "manual_override": False,
                }
            )

            # preview column: checkbox + thumbnail
            preview = PlaylistPreviewWidget(table)
            preview.checkbox.toggled.connect(partial(self._on_playlist_row_checked, row))
            table.setCellWidget(row, 0, preview)
            self._preview_widget_by_row[row] = preview

            self._thumb_label_by_row[row] = preview.thumb_label
            if thumb:
                self._thumb_url_to_rows.setdefault(thumb, set()).add(row)

            # info column: title + meta
            meta_parts = [duration]
            if uploader and uploader != "-":
                meta_parts.append(uploader)
            if upload_date and upload_date != "-":
                meta_parts.append(upload_date)
            meta_parts.append(f"#{playlist_index}")
            meta = " · ".join(meta_parts)
            info_widget = PlaylistInfoWidget(title, meta, table)
            table.setCellWidget(row, 1, info_widget)

            # action column: quality/status
            action = PlaylistActionWidget(table)
            action.qualityButton.clicked.connect(partial(self._on_playlist_quality_clicked, row))
            action.set_loading(True, "待加载")
            action.infoLabel.setText("")
            table.setCellWidget(row, 2, action)
            self._action_widget_by_row[row] = action

            table.setRowHeight(row, 92)

        table.blockSignals(False)

    def _on_playlist_row_checked(self, row: int, checked: bool) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        self._playlist_rows[row]["selected"] = bool(checked)
        self._playlist_rows[row]["status"] = "已选择" if checked else "未选择"

        self._update_download_btn_state()
        self._last_interaction = time.monotonic()

    def _on_playlist_quality_clicked(self, row: int) -> None:
        self._last_interaction = time.monotonic()
        if not (0 <= row < len(self._playlist_rows)):
            return
        if row not in self._detail_loaded:
            aw = self._action_widget_by_row.get(row)
            if aw is not None:
                aw.set_loading(True, "获取中...")
            self._enqueue_detail_rows([row], priority=True)
            self._maybe_start_next_detail()
        else:
            self._open_row_format_picker(row)

    def _current_playlist_preset_height(self) -> int | None:
        preset_text = (
            self.preset_combo.currentText() if self.preset_combo is not None else "最高质量(自动)"
        )
        height_map = {
            "2160p(严格)": 2160,
            "1440p(严格)": 1440,
            "1080p(严格)": 1080,
            "720p(严格)": 720,
            "480p(严格)": 480,
            "360p(严格)": 360,
        }
        return height_map.get(str(preset_text))

    def _format_quality_brief(self, fmt: dict[str, Any]) -> str:
        h = int(fmt.get("height") or 0)
        fps = fmt.get("fps")
        if h >= 2160:
            s = "4K"
        elif h >= 1440:
            s = "2K"
        elif h > 0:
            s = f"{h}p"
        else:
            s = "-"
        try:
            if fps and float(fps) > 30:
                s += f" {int(float(fps))}fps"
        except Exception:
            pass
        return s

    def _auto_apply_row_preset(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return

        data = self._playlist_rows[row]
        aw = self._action_widget_by_row.get(row)
        if aw is None:
            return

        mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0

        def _format_audio_brief(a: dict[str, Any] | None) -> str:
            if not a:
                return "音频-"
            try:
                abr_int = int(a.get("abr") or 0)
            except Exception:
                abr_int = 0
            return f"音频{abr_int}k" if abr_int > 0 else "音频-"

        def _format_info_line(prefix: str, size_val: Any, ext_val: Any) -> str:
            size_str = _format_size(size_val)
            ext = str(ext_val or "").strip()
            if size_str != "-" and ext:
                return f"{prefix}{size_str} · {ext}"
            if size_str != "-":
                return f"{prefix}{size_str}"
            if ext:
                return f"{prefix}{ext}"
            return f"{prefix}-"

        # 0=音视频，1=仅视频，2=仅音频
        if row not in self._detail_loaded:
            if mode == 2:
                # 音频模式允许不等详情，先给占位
                aw.set_loading(False)
                aw.qualityButton.setText("音频(自动)")
                aw.infoLabel.setText("待解析大小")
                return
            aw.set_loading(True, "待加载")
            aw.infoLabel.setText("")
            return

        # NEW: Handle advanced custom selection
        if data.get("custom_selection_data"):
            aw.set_loading(False)
            aw.qualityButton.setText(str(data.get("custom_summary") or "已自定义"))
            aw.infoLabel.setText("使用自定义配置")
            return

        audio_fmts: list[dict[str, Any]] = data.get("audio_formats") or []

        def _find_video_ext_for_row() -> str | None:
            # prefer current chosen video id (manual/auto)
            vid = str(data.get("override_format_id") or "").strip()
            if not vid:
                return None
            for vf in data.get("video_formats") or []:
                if str(vf.get("id") or "") == vid:
                    ext = str(vf.get("ext") or "").strip().lower()
                    return ext or None
            return None

        def _choose_best_audio() -> dict[str, Any] | None:
            if not audio_fmts:
                return None
            # audio-only: highest abr is fine
            if mode != 0:
                return audio_fmts[0]

            vext = _find_video_ext_for_row()
            if not vext:
                return audio_fmts[0]

            # Prefer audio container matching the selected video container.
            if vext == "webm":
                for a in audio_fmts:
                    if str(a.get("ext") or "").strip().lower() == "webm":
                        return a
                return audio_fmts[0]

            if vext in {"mp4", "m4v"}:
                for pref in ("m4a", "aac", "mp4"):
                    for a in audio_fmts:
                        if str(a.get("ext") or "").strip().lower() == pref:
                            return a
                return audio_fmts[0]

            return audio_fmts[0]

        best_audio = _choose_best_audio()
        if best_audio and best_audio.get("id"):
            data["audio_best_format_id"] = str(best_audio.get("id"))
        data["audio_best_text"] = _format_audio_brief(best_audio)

        chosen_audio = best_audio
        if bool(data.get("audio_manual_override")):
            wanted_aid = str(data.get("audio_override_format_id") or "")
            for a in audio_fmts:
                if str(a.get("id") or "") == wanted_aid:
                    chosen_audio = a
                    break
            if chosen_audio is not None:
                data["audio_override_text"] = _format_audio_brief(chosen_audio)

        if mode == 2:
            # 仅音频：只展示音频信息
            aw.set_loading(False)
            aw.qualityButton.setText(
                str(
                    data.get("audio_override_text")
                    if bool(data.get("audio_manual_override"))
                    else (data.get("audio_best_text") or "音频(自动)")
                )
            )
            if chosen_audio is not None:
                aw.infoLabel.setText(
                    _format_info_line("", chosen_audio.get("filesize"), chosen_audio.get("ext"))
                )
            else:
                aw.infoLabel.setText("-")
            return

        if bool(data.get("manual_override")):
            aw.set_loading(False)
            chosen = str(data.get("override_text") or "")

            if mode == 0:
                # 音视频：视频手动、音频(自动/手动)
                audio_brief = (
                    data.get("audio_override_text")
                    if bool(data.get("audio_manual_override"))
                    else (data.get("audio_best_text") or "音频-")
                )
                aw.qualityButton.setText(f"{chosen or '视频已选'} + {audio_brief}")
                chosen_fmt = None
                override_id = str(data.get("override_format_id") or "")
                fmts: list[dict[str, Any]] = data.get("video_formats") or []
                for f in fmts:
                    if str(f.get("id") or "") == override_id:
                        chosen_fmt = f
                        break
                v_line = _format_info_line(
                    "视频 ", (chosen_fmt or {}).get("filesize"), (chosen_fmt or {}).get("ext")
                )
                a_line = _format_info_line(
                    "音频 ", (chosen_audio or {}).get("filesize"), (chosen_audio or {}).get("ext")
                )
                aw.infoLabel.setText(v_line + "\n" + a_line)
                return

            # 仅视频
            aw.qualityButton.setText(chosen or "已手动选择")
            return

        # VR 模式下的自动选择模拟（用于 UI 显示）
        if self._vr_mode:
            fmts = data.get("video_formats") or []
            if not fmts:
                aw.set_loading(False)
                aw.qualityButton.setText("无可用格式")
                aw.infoLabel.setText("")
                return

            # 获取当前预设 ID
            if self.preset_combo is None:
                pid = None
            else:
                pid = self.preset_combo.itemData(self.preset_combo.currentIndex())

            # 简单的 Python 端模拟匹配
            best = None
            if pid == "vr_compat":  # 优先 MP4
                for f in fmts:
                    # 注意：fmts 里的元素是我们自己构造的 dict，原始数据在 _raw
                    raw = f.get("_raw") or {}
                    if raw.get("ext") == "mp4":
                        best = f
                        break
            elif pid == "vr_3d_cinema":  # 优先 3D
                for f in fmts:
                    raw = f.get("_raw") or {}
                    if str(raw.get("__vr_stereo_mode") or "").startswith("stereo"):
                        best = f
                        break
            elif pid == "vr_panorama":  # 优先 Mono
                for f in fmts:
                    raw = f.get("_raw") or {}
                    if str(raw.get("__vr_stereo_mode") or "") == "mono":
                        best = f
                        break

            # 默认回退：取第一个（因为 _populate_formats 已经按 VR 质量排过序了）
            if not best:
                best = fmts[0]

            fid = best.get("id")
            if fid:
                data["override_format_id"] = str(fid)

            # VR 模式下通常不需要显示音频组合，直接显示 VR 格式描述
            data["override_text"] = best.get("text")
            data["manual_override"] = False

            aw.set_loading(False)
            aw.qualityButton.setText(str(data["override_text"] or ""))

            # 显示详细信息
            raw = best.get("_raw") or {}
            sz = _format_size(raw.get("filesize") or raw.get("filesize_approx"))
            ext = raw.get("ext")
            aw.infoLabel.setText(f"{sz} · {ext}")
            return

        fmts: list[dict[str, Any]] = data.get("video_formats") or []
        if not fmts:
            aw.set_loading(False)
            aw.qualityButton.setText("无可用格式")
            aw.infoLabel.setText("")
            return

        preset_height = self._current_playlist_preset_height()
        if preset_height is None:
            best = fmts[0]
        else:
            candidates = [f for f in fmts if int(f.get("height") or 0) == preset_height]
            if not candidates:
                aw.set_loading(False)
                aw.qualityButton.setText("无匹配(点选)")
                if mode == 0:
                    a_line = _format_info_line(
                        "音频 ",
                        (chosen_audio or {}).get("filesize"),
                        (chosen_audio or {}).get("ext"),
                    )
                    aw.infoLabel.setText("可手动选择\n" + a_line)
                else:
                    aw.infoLabel.setText("可手动选择")
                data["override_format_id"] = None
                data["override_text"] = None
                return

            def _fps_key(x: dict[str, Any]) -> float:
                try:
                    return float(x.get("fps") or 0)
                except Exception:
                    return 0.0

            best = sorted(candidates, key=_fps_key, reverse=True)[0]

        fid = best.get("id")
        if fid:
            data["override_format_id"] = str(fid)
        data["override_text"] = self._format_quality_brief(best)
        # keep manual_override as-is; this path is for auto video selection
        data["manual_override"] = False

        aw.set_loading(False)
        if mode == 1:
            aw.qualityButton.setText(str(data["override_text"] or ""))
            aw.infoLabel.setText(_format_info_line("", best.get("filesize"), best.get("ext")))
            return

        audio_brief = (
            data.get("audio_override_text")
            if bool(data.get("audio_manual_override"))
            else (data.get("audio_best_text") or "音频-")
        )
        aw.qualityButton.setText(f"{data.get('override_text') or ''} + {audio_brief}")
        v_line = _format_info_line("视频 ", best.get("filesize"), best.get("ext"))
        a_line = _format_info_line(
            "音频 ", (chosen_audio or {}).get("filesize"), (chosen_audio or {}).get("ext")
        )
        aw.infoLabel.setText(v_line + "\n" + a_line)

    def _on_playlist_preset_changed(self, _index: int) -> None:
        for r in range(len(self._playlist_rows)):
            self._auto_apply_row_preset(r)
        self._update_download_btn_state()

    def _on_playlist_type_changed(self, index: int) -> None:
        # Audio-only disables quality preset selection
        if self.preset_combo is not None:
            self.preset_combo.setEnabled(index in (0, 1))
        for r in range(len(self._playlist_rows)):
            self._auto_apply_row_preset(r)
        self._update_download_btn_state()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if self._table is None:
            return
        row = item.row()
        col = item.column()
        if col != 0:
            return
        checked = item.checkState() == Qt.CheckState.Checked
        if 0 <= row < len(self._playlist_rows):
            self._playlist_rows[row]["selected"] = checked
            self._playlist_rows[row]["status"] = "已选择" if checked else "未选择"
            status_item = self._table.item(row, 9)
            if status_item is not None:
                status_item.setText(self._playlist_rows[row]["status"])
        self._update_yes_enabled()
        self._last_interaction = time.monotonic()

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        self._last_interaction = time.monotonic()
        if col != 8:
            return
        if not (0 <= row < len(self._playlist_rows)):
            return
        if row not in self._detail_loaded:
            self._enqueue_detail_rows([row], priority=True)
            self._maybe_start_next_detail()
        else:
            self._open_row_format_picker(row)

    def _on_table_scrolled(self, _value: int) -> None:
        self._last_interaction = time.monotonic()
        self._enqueue_detail_for_visible_rows()
        self._load_thumbs_for_visible_rows()
        self._maybe_start_next_detail()

    def _visible_row_range(self) -> tuple[int, int]:
        table = self._table
        if table is None:
            return (0, -1)

        first = table.rowAt(0)
        # If the table hasn't been drawn yet, rowAt will return -1.
        # Default to showing the first 10 rows in this edge case mapping.
        if first < 0:
            first = 0

        last = table.rowAt(table.viewport().height() - 1)
        if last < 0:
            last = min(table.rowCount() - 1, first + 10)

        return (first, last)

    def _enqueue_detail_for_visible_rows(self) -> None:
        first, last = self._visible_row_range()
        # prefetch slightly ahead and behind
        first = max(0, first - 3)
        last = min(len(self._playlist_rows) - 1, last + 6)
        rows = list(range(first, last + 1))
        # Remove anything out of view from the high priority front of the queue
        self._clean_invisible_from_queue()
        self._enqueue_detail_rows(rows, priority=True)
        self._maybe_start_next_detail()

    def _clean_invisible_from_queue(self) -> None:
        """从队列中清理不再处于当前滚动缓冲区的解析项"""
        first, last = self._visible_row_range()
        buff_first = max(0, first - 8)
        buff_last = min(len(self._playlist_rows) - 1, last + 12)

        # We don't drop them completely so they can load on idle,
        # but we remove them from the front to allow visible items priority.
        # It's simpler to just clear the queue of anything not in buff range,
        # since idle tick will re-add them when needed.
        new_q = deque()
        for r in self._detail_queue:
            if buff_first <= r <= buff_last:
                new_q.append(r)
        self._detail_queue = new_q

    def _on_thumb_init_timeout(self) -> None:
        """延迟加载首批缩略图（等待表格布局完成）"""
        if self._is_closing or not self._is_playlist:
            return
        # 首次加载：预加载更多行（前 20 行）
        self._load_thumbs_batch(0, min(20, len(self._playlist_rows) - 1))

    def _load_thumbs_batch(self, first: int, last: int) -> None:
        """批量加载指定范围的缩略图"""
        for row in range(first, last + 1):
            if not (0 <= row < len(self._playlist_rows)):
                continue
            url = str(self._playlist_rows[row].get("thumbnail") or "").strip()
            if not url:
                continue
            if url in self._thumb_cache:
                self._apply_thumb_to_row(row, url)
                continue
            if url in self._thumb_requested:
                continue
            # 加入待加载队列
            self._thumb_pending.append(url)
            self._thumb_requested.add(url)

        # 启动并发加载
        self._process_thumb_queue()

    def _process_thumb_queue(self) -> None:
        """处理缩略图加载队列，控制并发数"""
        while self._thumb_pending and self._thumb_inflight < self._thumb_max_concurrent:
            url = self._thumb_pending.pop(0)
            self._thumb_inflight += 1
            self.image_loader.load(url, target_size=(150, 84), radius=8)

    def _load_thumbs_for_visible_rows(self) -> None:
        first, last = self._visible_row_range()
        # 扩大预加载范围
        first = max(0, first - 8)
        last = min(len(self._playlist_rows) - 1, last + 15)
        self._load_thumbs_batch(first, last)

    def _apply_thumb_to_row(self, row: int, url: str) -> None:
        pix = self._thumb_cache.get(url)
        lbl = self._thumb_label_by_row.get(row)
        if pix is not None and lbl is not None:
            try:
                lbl.setPixmap(pix)
            except Exception:
                pass

    def _on_thumb_loaded_with_url(self, url: str, pixmap) -> None:
        # 减少并发计数，触发下一批加载
        self._thumb_inflight = max(0, self._thumb_inflight - 1)
        self._process_thumb_queue()

        if self._is_closing:
            return
        if not self._is_playlist:
            return
        u = str(url or "").strip()
        if not u:
            return
        self._thumb_cache[u] = pixmap
        for row in self._thumb_url_to_rows.get(u, set()):
            self._apply_thumb_to_row(row, u)

    def _on_thumb_failed(self, url: str) -> None:
        """缩略图加载失败时的回调"""
        # 减少并发计数，继续处理队列
        self._thumb_inflight = max(0, self._thumb_inflight - 1)
        self._process_thumb_queue()

    def _select_all(self) -> None:
        self._set_all_checks(True)

    def _unselect_all(self) -> None:
        self._set_all_checks(False)

    def _invert_select(self) -> None:
        table = self._table
        if table is None:
            return
        for row in range(len(self._playlist_rows)):
            w = self._preview_widget_by_row.get(row)
            if w is None:
                continue
            cb = w.checkbox
            cb.blockSignals(True)
            cb.setChecked(not cb.isChecked())
            cb.blockSignals(False)
            self._playlist_rows[row]["selected"] = cb.isChecked()
            self._playlist_rows[row]["status"] = "已选择" if cb.isChecked() else "未选择"
        self._update_download_btn_state()

    def _set_all_checks(self, checked: bool) -> None:
        table = self._table
        if table is None:
            return
        for row in range(len(self._playlist_rows)):
            w = self._preview_widget_by_row.get(row)
            if w is None:
                continue
            cb = w.checkbox
            cb.blockSignals(True)
            cb.setChecked(bool(checked))
            cb.blockSignals(False)
            self._playlist_rows[row]["selected"] = bool(checked)
            self._playlist_rows[row]["status"] = "已选择" if checked else "未选择"
        self._update_download_btn_state()

    def _apply_preset_to_selected(self) -> None:
        # This clears per-row overrides for selected rows.
        table = self._table
        if table is None:
            return
        for row, data in enumerate(self._playlist_rows):
            if not data.get("selected"):
                continue
            data["override_format_id"] = None
            data["override_text"] = None
            data["manual_override"] = False
            data["audio_override_format_id"] = None
            data["audio_override_text"] = None
            data["audio_manual_override"] = False
            # Clear advanced selection
            data["custom_selection_data"] = None
            data["custom_summary"] = None

            self._auto_apply_row_preset(row)
        self._update_download_btn_state()

    def _update_yes_enabled(self) -> None:
        self._update_download_btn_state()

    def _update_download_btn_state(self) -> None:
        any_selected = any(bool(r.get("selected")) for r in self._playlist_rows)
        self.yesButton.setEnabled(bool(any_selected))

        if not any_selected:
            self.yesButton.setText("下载")
            return

        mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0
        if mode == 2:
            self.yesButton.setText("下载")
            return

        selected_rows = [i for i, r in enumerate(self._playlist_rows) if r.get("selected")]
        pending = [i for i in selected_rows if i not in self._detail_loaded]
        if pending:
            self.yesButton.setText(f"下载（剩余 {len(pending)} 个解析中...）")
        else:
            self.yesButton.setText("下载")

    def _refresh_progress_label(self) -> None:
        if hasattr(self, "progressLabel"):
            total = len(self._playlist_rows)
            done = len(self._detail_loaded)
            self.progressLabel.setText(f"详情补全：{done}/{total}")
            try:
                if hasattr(self, "progressRing"):
                    self.progressRing.setVisible(done < total)
            except Exception:
                pass

    def _enqueue_detail_rows(self, rows: list[int], priority: bool) -> None:
        for r in reversed(rows) if priority else rows:
            if r < 0 or r >= len(self._playlist_rows):
                continue
            if r in self._detail_loaded:
                continue
            if self._detail_inflight_row == r:
                continue

            # If cover mode bypass is active, we don't need to load info for it.
            # Handle the row immediately via shortcut.
            if self._mode == "cover":
                # We do this asynchronously to avoid freezing the UI thread if many queue up
                QTimer.singleShot(0, partial(self._process_cover_bypass, r))
                continue

            if r in self._detail_queue:
                if priority:
                    self._detail_queue.remove(r)
                    self._detail_queue.appendleft(r)
                continue

            if priority:
                self._detail_queue.appendleft(r)
            else:
                self._detail_queue.append(r)

    def _process_cover_bypass(self, row: int) -> None:
        if self._is_closing or row in self._detail_loaded:
            return

        data = self._playlist_rows[row]
        # Simulate an entry detail using the bare minimum from flat playlist items
        info = {
            "id": data.get("id"),
            "url": data.get("url"),
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "uploader": data.get("uploader"),
            "duration": data.get("duration"),
        }

        # Cleanly bypass full extraction overhead
        self._playlist_rows[row]["detail"] = info
        self._playlist_rows[row]["video_formats"] = []
        self._playlist_rows[row]["audio_formats"] = []
        self._playlist_rows[row]["highest_height"] = None

        self._detail_loaded.add(row)
        self._auto_apply_row_preset(row)

        self._refresh_progress_label()
        self._update_download_btn_state()

    def _maybe_start_next_detail(self) -> None:
        if self._is_closing:
            return
        if self._detail_inflight_row is not None:
            return
        if not self._detail_queue:
            return
        row = self._detail_queue.popleft()
        if row in self._detail_loaded:
            return
        url = str(self._playlist_rows[row].get("url") or "").strip()
        if not url:
            return

        self._detail_inflight_row = row
        aw = self._action_widget_by_row.get(row)
        if aw is not None:
            aw.set_loading(True, "获取中...")
            aw.infoLabel.setText("")

        w = EntryDetailWorker(row, url, self._current_options, vr_mode=self._vr_mode)
        w.finished.connect(self._on_detail_finished)
        w.error.connect(self._on_detail_error)
        w.start()
        self._detail_worker = w

    def _on_detail_finished(self, row: int, info: dict[str, Any]) -> None:
        if self._is_closing:
            return
        self._detail_inflight_row = None
        if 0 <= row < len(self._playlist_rows):
            # backfill thumbnail if missing
            thumb = str(self._playlist_rows[row].get("thumbnail") or "").strip()
            if not thumb:
                thumb = _infer_entry_thumbnail(info)
                if thumb:
                    self._playlist_rows[row]["thumbnail"] = thumb
                    self._thumb_url_to_rows.setdefault(thumb, set()).add(row)
                    # trigger load ASAP
                    if thumb in self._thumb_cache:
                        self._apply_thumb_to_row(row, thumb)
                    else:
                        if thumb not in self._thumb_requested:
                            self._thumb_requested.add(thumb)
                            self.image_loader.load(thumb, target_size=(150, 84), radius=8)

            formats = _clean_video_formats(info)
            audio_formats = _clean_audio_formats(info)
            highest = formats[0]["height"] if formats else None
            self._playlist_rows[row]["detail"] = info
            self._playlist_rows[row]["video_formats"] = formats
            self._playlist_rows[row]["audio_formats"] = audio_formats
            self._playlist_rows[row]["highest_height"] = highest
            self._detail_loaded.add(row)

            # auto apply preset + show size (unless manual override)
            self._auto_apply_row_preset(row)

        self._refresh_progress_label()
        self._update_download_btn_state()
        self._maybe_start_next_detail()

    def _on_detail_error(self, row: int, msg: str) -> None:
        if self._is_closing:
            return
        self._detail_inflight_row = None
        aw = self._action_widget_by_row.get(row)
        if aw is not None:
            aw.set_loading(False, "获取失败(点重试)")
            aw.infoLabel.setText("")
            aw.qualityButton.setToolTip(msg)
        self._maybe_start_next_detail()

    def _on_idle_tick(self) -> None:
        if not self._is_playlist:
            return
        # Wait a moment after user stops scrolling/interacting
        if time.monotonic() - self._last_interaction < 1.0:
            return

        if self._detail_inflight_row is None and not self._detail_queue:
            # When completely idle, start filling in details for the rest of the list
            # We add them with priority=False so they go to the back of the queue
            # and won't block visible items if the user starts scrolling again
            added = 0
            for i in range(len(self._playlist_rows)):
                if i not in self._detail_loaded and i not in self._detail_queue:
                    # Enqueue a small batch to not lock up memory with huge lists
                    self._enqueue_detail_rows([i], priority=False)
                    added += 1
                    if added >= 3:
                        break

        self._maybe_start_next_detail()

    def _open_row_format_picker(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        # Ensure detail is loaded
        if row not in self._detail_loaded:
            return

        data = self._playlist_rows[row]
        info = data.get("detail")
        if not info:
            return

        dialog = PlaylistFormatDialog(info, self, vr_mode=self._vr_mode)
        if dialog.exec():
            sel = dialog.get_selection()
            if sel and sel.get("format"):
                # Valid selection made
                data["custom_selection_data"] = sel
                data["custom_summary"] = dialog.get_summary()
                data["manual_override"] = True

                # Clear legacy fields to avoid confusion
                data["override_format_id"] = None
                data["override_text"] = None
                data["audio_override_format_id"] = None
                data["audio_override_text"] = None
                data["audio_manual_override"] = False

                # Update UI
                self._auto_apply_row_preset(row)
            else:
                # User selected nothing or invalid -> reset?
                # Or just treat as cancel.
                # For now treat as cancel or do nothing.
                pass

    def get_download_tasks(self) -> list[dict[str, Any]]:
        return list(self.download_tasks)

    def get_selected_tasks(self) -> list[tuple[str, str, dict[str, Any], str | None]]:
        """Returns list of (title, url, ydl_opts, thumbnail_url)."""
        tasks = []

        # 1. Single Video Mode
        if not self._is_playlist:
            if not self.video_info:
                print("[DEBUG] get_selected_tasks: video_info is None")
                return []

            info = self.video_info
            url = _infer_entry_url(info)
            title = str(info.get("title") or "Unknown")
            thumb = str(info.get("thumbnail") or "")

            ydl_opts: dict[str, Any] = {}

            # Mode specific handling
            if self._mode == "subtitle":
                if hasattr(self, "_subtitle_selector"):
                    opts = self._subtitle_selector.get_opts()
                    ydl_opts.update(opts)

                # Force subtitle download only
                ydl_opts["skip_download"] = True
                ydl_opts["writethumbnail"] = False
                ydl_opts["embedthumbnail"] = False
                ydl_opts["addmetadata"] = False
                ydl_opts["embedsubtitles"] = False

                # Disable SponsorBlock and other video-specific processing
                ydl_opts["sponsorblock_remove"] = None
                ydl_opts["sponsorblock_mark"] = None
                ydl_opts["postprocessors"] = []

                tasks.append((f"[字幕] {title}", url, ydl_opts, thumb))
                return tasks

            elif self._mode == "cover":
                # Cover specific handling
                if hasattr(self, "_cover_selector"):
                    url = self._cover_selector.get_selected_url() or url
                    # If we have a specific URL, we use it.
                    # Note: We must ensure download_manager can handle it.
                    # Usually if it's a direct image link, yt-dlp works but might need generic extractor.
                    # Or we treat it as a direct download.

                    # Also, we might want to set a specific filename.
                    _ = self._cover_selector.get_selected_ext()

                    # Use "outtmpl" to name the file properly (Title.jpg)
                    # We rely on yt-dlp to download the file at 'url'

                    # If 'url' is the image URL, yt-dlp might download it as a generic file.
                    # We need to make sure we don't try to extract info from it again if possible,
                    # or just let yt-dlp handle the generic file download.

                    # Force overwrite the task URL to the image URL

                    # Options for direct file download
                    ydl_opts["skip_download"] = False  # We WANT to download the image file
                    ydl_opts["writethumbnail"] = False  # We are downloading the image itself
                    ydl_opts["embedthumbnail"] = False
                    ydl_opts["addmetadata"] = False
                    ydl_opts["embedsubtitles"] = False

                    # Disable SponsorBlock
                    ydl_opts["sponsorblock_remove"] = None
                    ydl_opts["sponsorblock_mark"] = None
                    ydl_opts["postprocessors"] = []

                    # Set output template to use video title
                    # Note: We rely on sanitize_filename to make it safe
                    safe_title = sanitize_filename(title)
                    ydl_opts["outtmpl"] = f"{safe_title}.%(ext)s"
                else:
                    # Fallback to default behavior (best cover)
                    ydl_opts["skip_download"] = True
                    ydl_opts["writethumbnail"] = True
                    ydl_opts["embedthumbnail"] = False
                    ydl_opts["addmetadata"] = False
                    ydl_opts["embedsubtitles"] = False

                    # Disable SponsorBlock
                    ydl_opts["sponsorblock_remove"] = None
                    ydl_opts["sponsorblock_mark"] = None
                    ydl_opts["postprocessors"] = []

                tasks.append((f"[封面] {title}", url, ydl_opts, thumb))
                return tasks

            # Delegate to the format selector component
            has_selector = hasattr(self, "_format_selector")
            print(f"[DEBUG] get_selected_tasks: has_format_selector={has_selector}")

            if has_selector:
                sel = self._format_selector.get_selection_result()
                print(f"[DEBUG] get_selected_tasks: selection result = {sel}")
                if sel and sel.get("format"):
                    ydl_opts["format"] = sel["format"]
                    ydl_opts.update(sel.get("extra_opts") or {})

                    # ========== VR 格式检测 ==========
                    # 检查选择的格式是否包含 VR 专属格式 ID
                    vr_only_ids = info.get("__vr_only_format_ids") or []
                    android_vr_ids = info.get("__android_vr_format_ids") or []
                    if vr_only_ids:
                        selected_format = sel["format"]
                        # 检查 format 字符串中是否包含任何 VR 专属 ID
                        for vr_id in vr_only_ids:
                            if vr_id in selected_format:
                                ydl_opts["__fluentytdl_use_android_vr"] = True
                                ydl_opts["__android_vr_format_ids"] = android_vr_ids
                                print(
                                    f"[DEBUG] get_selected_tasks: VR format {vr_id} detected, enabling android_vr client"
                                )
                                print(
                                    f"[DEBUG] get_selected_tasks: android_vr has {len(android_vr_ids)} formats available"
                                )
                                break

                    # VR 模式：始终使用 android_vr 客户端
                    if self._vr_mode:
                        ydl_opts["__fluentytdl_use_android_vr"] = True
                        print("[DEBUG] get_selected_tasks: VR mode, forcing android_vr client")
                else:
                    # 修复：即使没有格式选择，也应该使用默认格式
                    print("[DEBUG] get_selected_tasks: No format in selection, using default")
                    ydl_opts["format"] = "bestvideo+bestaudio/best"
            else:
                # 没有格式选择器，使用默认格式
                print("[DEBUG] get_selected_tasks: No format selector, using default")
                ydl_opts["format"] = "bestvideo+bestaudio/best"

            # 【关键修复】集成字幕服务到新格式选择器路径
            if self.video_info:
                # 优先使用缓存的用户选择（在 accept() 中已询问）
                if self._subtitle_choice_made:
                    print(
                        f"[DEBUG] get_selected_tasks: Using cached subtitle choice: {self._subtitle_embed_choice}"
                    )
                    embed_override = self._subtitle_embed_choice
                else:
                    # 如果没有缓存，再询问（不应该发生，但作为后备）
                    print(
                        "[DEBUG] get_selected_tasks: No cached choice, calling _check_subtitle_and_ask()"
                    )
                    try:
                        embed_override = self._check_subtitle_and_ask()
                        print(f"[DEBUG] get_selected_tasks: embed_override = {embed_override}")
                    except ValueError as e:
                        # 用户取消下载
                        print(f"[DEBUG] get_selected_tasks: User cancelled - {e}")
                        return []
                    except Exception as e:
                        # 其他异常
                        print(
                            f"[ERROR] get_selected_tasks: Exception in _check_subtitle_and_ask - {e}"
                        )
                        import traceback

                        traceback.print_exc()
                        # 继续下载，但不设置字幕
                        embed_override = None

                subtitle_opts = subtitle_service.apply(
                    video_id=self.video_info.get("id", ""),
                    video_info=self.video_info,
                )
                ydl_opts.update(subtitle_opts)

                # 如果用户明确选择了嵌入选项，需要根据 embed_type 来决定行为
                if embed_override is not None:
                    from ...core.config_manager import config_manager as cfg

                    embed_type = cfg.get_subtitle_config().embed_type

                    if embed_type == "soft":
                        # 软嵌入：用户选择覆盖 embedsubtitles
                        ydl_opts["embedsubtitles"] = embed_override
                    elif embed_type == "external":
                        # 外置文件：始终不嵌入，忽略用户的弹窗选择
                        ydl_opts["embedsubtitles"] = False

                    print(
                        f"[DEBUG] get_selected_tasks: embed_type={embed_type}, embed_override={embed_override}, final embedsubtitles={ydl_opts.get('embedsubtitles')}"
                    )

                # 确保容器格式兼容字幕嵌入
                _ensure_subtitle_compatible_container(ydl_opts)

                print(f"[DEBUG] get_selected_tasks: subtitle_opts = {subtitle_opts}")
                print(
                    f"[DEBUG] get_selected_tasks: final embedsubtitles = {ydl_opts.get('embedsubtitles')}"
                )
                print(
                    f"[DEBUG] get_selected_tasks: final merge_output_format = {ydl_opts.get('merge_output_format')}"
                )

            self._apply_download_dir_to_opts(ydl_opts)
            tasks.append((title, url, ydl_opts, thumb))
            return tasks

        # 2. Playlist Mode (Existing Logic)
        for _i, row_data in enumerate(self._playlist_rows):
            if not row_data.get("selected"):
                continue

            # ... (Playlist logic unchanged) ...
            url = str(row_data.get("url"))
            title = str(row_data.get("title"))
            thumb = str(row_data.get("thumbnail"))

            # Base opts
            row_opts = {}

            # Check for manual overrides (from detail view)
            # ...
            # For simplicity, if we haven't loaded detail, we rely on generic "best"
            # If we have detail, we use the specific format IDs calculated in _auto_apply_row_preset

            ov_fid = row_data.get("override_format_id")
            aud_fid = row_data.get("audio_best_format_id")
            aud_manual_fid = row_data.get("audio_override_format_id")

            # Audio-only mode (global combo)
            mode = int(self.type_combo.currentIndex()) if self.type_combo else 0

            if mode == 2:  # Audio only
                if aud_manual_fid:
                    row_opts["format"] = aud_manual_fid
                elif aud_fid:
                    row_opts["format"] = aud_fid
                else:
                    row_opts["format"] = "bestaudio/best"

            elif mode == 1:  # Video only
                if ov_fid:
                    row_opts["format"] = ov_fid
                else:
                    # Fallback to preset height constraint
                    h = self._current_playlist_preset_height()
                    if h:
                        row_opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
                    else:
                        row_opts["format"] = "bestvideo+bestaudio/best"

            else:  # AV Muxed
                if ov_fid:
                    # Specific video selected
                    target_audio = (
                        aud_manual_fid if row_data.get("audio_manual_override") else aud_fid
                    )
                    if target_audio:
                        row_opts["format"] = f"{ov_fid}+{target_audio}"
                        # TODO: merge container logic for playlist?
                        # For now let yt-dlp decide or use mkv
                        row_opts["merge_output_format"] = "mkv"
                    else:
                        row_opts["format"] = f"{ov_fid}+bestaudio/best"
                else:
                    # Auto based on preset
                    h = self._current_playlist_preset_height()
                    if h:
                        row_opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
                        row_opts["merge_output_format"] = "mkv"
                    else:
                        row_opts["format"] = "bestvideo+bestaudio/best"

            self._apply_download_dir_to_opts(row_opts)
            tasks.append((title, url, row_opts, thumb))

        return tasks

    def _check_subtitle_and_ask(self) -> bool | None:
        """
        检查字幕配置并弹出询问对话框

        Returns:
            None: 不需要嵌入或使用默认配置
            True: 用户选择嵌入
            False: 用户选择不嵌入

        Raises:
            ValueError: 用户取消下载
        """
        print("[DEBUG] _check_subtitle_and_ask: Method called")

        if not self.video_info:
            print("[DEBUG] _check_subtitle_and_ask: No video_info, returning None")
            return None

        from ...core.config_manager import config_manager
        from ...processing.subtitle_manager import extract_subtitle_tracks

        subtitle_config = config_manager.get_subtitle_config()
        print(
            f"[DEBUG] _check_subtitle_and_ask: subtitle_enabled={subtitle_config.enabled}, embed_mode={subtitle_config.embed_mode}"
        )

        if not subtitle_config.enabled:
            print("[DEBUG] _check_subtitle_and_ask: Subtitle disabled, returning None")
            return None

        # 检查视频是否有字幕
        tracks = extract_subtitle_tracks(self.video_info)
        print(f"[DEBUG] _check_subtitle_and_ask: Found {len(tracks)} subtitle tracks")

        if not tracks:
            # 视频没有字幕，提示用户
            print("[DEBUG] _check_subtitle_and_ask: No subtitles, showing warning dialog")
            box = MessageBox(
                "⚠️ 无可用字幕",
                "此视频没有可用字幕。\n\n是否继续下载（无字幕）？",
                parent=self,
            )
            box.yesButton.setText("继续下载")
            box.cancelButton.setText("取消")
            print(
                "[DEBUG] _check_subtitle_and_ask: About to call box.exec() for no subtitle warning"
            )
            result = box.exec()
            print(f"[DEBUG] _check_subtitle_and_ask: box.exec() returned {result}")
            if not result:
                print("[DEBUG] _check_subtitle_and_ask: User cancelled, raising ValueError")
                raise ValueError("用户取消下载：无字幕")
            print("[DEBUG] _check_subtitle_and_ask: User continue, returning None")
            return None

        # 有字幕，检查是否需要询问嵌入模式
        if subtitle_config.embed_mode == "ask":
            available_langs = [t.lang_code for t in tracks[:5]]
            lang_display = ", ".join(available_langs)
            if len(tracks) > 5:
                lang_display += f" 等 {len(tracks)} 种语言"

            print(
                f"[DEBUG] _check_subtitle_and_ask: embed_mode is 'ask', showing confirmation dialog with langs: {lang_display}"
            )
            box = MessageBox(
                "📝 字幕嵌入确认",
                f"检测到可用字幕：{lang_display}\n\n"
                f"是否将字幕嵌入到视频文件中？\n"
                f"(嵌入后可在播放器中直接显示)",
                parent=self,
            )
            box.yesButton.setText("嵌入字幕")
            box.cancelButton.setText("仅下载文件")
            print(
                "[DEBUG] _check_subtitle_and_ask: About to call box.exec() for embed confirmation"
            )
            result = box.exec()
            print(
                f"[DEBUG] _check_subtitle_and_ask: box.exec() returned {result} (type: {type(result)})"
            )
            return bool(result)

        print("[DEBUG] _check_subtitle_and_ask: Returning None (use config default)")
        return None  # 使用配置默认值

    def accept(self) -> None:
        if self._is_playlist:
            mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0
            if mode in (0, 1):
                selected_rows = [i for i, r in enumerate(self._playlist_rows) if r.get("selected")]
                pending = [i for i in selected_rows if i not in self._detail_loaded]
                if pending:
                    box = MessageBox(
                        "仍在解析中",
                        f"还有 {len(pending)} 个已勾选条目正在补全信息。\n\n"
                        "你可以继续下载（将按当前预设策略执行），或等待补全完成后再下载。",
                        parent=self,
                    )
                    box.yesButton.setText("继续下载")
                    box.cancelButton.setText("等待补全")
                    if not box.exec():
                        self._enqueue_detail_rows(pending[:6], priority=True)
                        self._maybe_start_next_detail()
                        return
            tasks = self._build_playlist_tasks()
            if not tasks:
                return
            self.download_tasks = tasks
        else:
            # 单个视频下载
            print("[DEBUG] accept: Single video mode")

            # 【关键修复】无论是否有格式选择器，都需要在这里询问字幕
            # 因为 accept() 是在对话框关闭前执行，此时 MessageBox 能正常工作
            # get_selected_tasks() 是在对话框关闭后执行，MessageBox 可能无法正常工作
            if self.video_info is not None and not self._subtitle_choice_made:
                try:
                    print("[DEBUG] accept: Calling _check_subtitle_and_ask()")
                    self._subtitle_embed_choice = self._check_subtitle_and_ask()
                    self._subtitle_choice_made = True
                    print(f"[DEBUG] accept: User choice cached: {self._subtitle_embed_choice}")
                except ValueError:
                    # 用户取消下载
                    print("[DEBUG] accept: User cancelled")
                    return

            # 检查是否有格式选择器
            print("[DEBUG] accept: Checking for format selector")
            has_selector = hasattr(self, "_format_selector")
            print(f"[DEBUG] accept: has_format_selector={has_selector}")

            if has_selector:
                # 有格式选择器：字幕选择已完成，格式处理在 get_selected_tasks() 中完成
                print(
                    "[DEBUG] accept: Has format selector, subtitle choice done, format will be handled in get_selected_tasks"
                )
                # 不设置 download_tasks，让 MainWindow 调用 get_selected_tasks()
                super().accept()
                return

            # 没有格式选择器：使用旧流程（get_download_options）
            print("[DEBUG] accept: No format selector, using legacy flow")

            if self.video_info is not None:
                title = str(self.video_info.get("title") or "未命名任务")
                thumb = str(self.video_info.get("thumbnail") or "").strip() or None
            else:
                title = "未命名任务"
                thumb = None
            self.download_tasks = [
                {
                    "url": self.url,
                    "title": title,
                    "thumbnail": thumb,
                    "opts": self.get_download_options(
                        embed_subtitles_override=self._subtitle_embed_choice
                    ),
                }
            ]
        super().accept()

    def _build_playlist_tasks(self) -> list[dict[str, Any]]:
        selected_rows = [i for i, r in enumerate(self._playlist_rows) if r.get("selected")]
        if not selected_rows:
            return []

        mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0
        preset_text = (
            self.preset_combo.currentText() if self.preset_combo is not None else "最高质量(自动)"
        )

        # VR 模式预设解析
        vr_preset_fmt = None
        vr_preset_args = {}
        if self._vr_mode:
            if self.preset_combo is None:
                pid = None
            else:
                pid = self.preset_combo.itemData(self.preset_combo.currentIndex())
            for p in VR_PRESETS:
                if p[0] == pid:
                    vr_preset_fmt = p[3]
                    vr_preset_args = p[4]
                    break

        height_map = {
            "2160p(严格)": 2160,
            "1440p(严格)": 1440,
            "1080p(严格)": 1080,
            "720p(严格)": 720,
            "480p(严格)": 480,
            "360p(严格)": 360,
        }
        preset_height = height_map.get(preset_text)

        # If we already know some rows' highest quality is below strict preset, prompt once.
        if mode in (0, 1) and preset_height:
            mismatched = []
            for r in selected_rows:
                hh = self._playlist_rows[r].get("highest_height")
                try:
                    hh_int = int(hh) if hh is not None else None
                except Exception:
                    hh_int = None
                if hh_int and hh_int < preset_height:
                    mismatched.append(r)

            if mismatched:
                box = MessageBox(
                    "预设质量不可用",
                    f"有 {len(mismatched)} 个已获取格式的条目最高画质低于 {preset_height}p。\n\n"
                    "可选择自动降低到该视频最高可用档位，或返回手动调整格式。",
                    parent=self,
                )
                box.yesButton.setText("自动降到最高")
                box.cancelButton.setText("手动调整")
                if box.exec():
                    for r in mismatched:
                        data = self._playlist_rows[r]
                        fmts: list[dict[str, Any]] = data.get("video_formats") or []
                        if not fmts:
                            continue
                        best = fmts[0]
                        fid = best.get("id")
                        if fid:
                            data["override_format_id"] = str(fid)
                            data["override_text"] = str(best.get("text") or "")
                            aw = self._action_widget_by_row.get(r)
                            if aw is not None:
                                aw.qualityButton.setText(f"已选择: {data['override_text']}")
                else:
                    # keep dialog open for manual adjustments
                    return []

        tasks: list[dict[str, Any]] = []
        for row in selected_rows:
            data = self._playlist_rows[row]
            url = str(data.get("url") or "").strip()
            if not url:
                continue
            opts: dict[str, Any] = {}

            # VR 模式注入
            if self._vr_mode:
                opts["__fluentytdl_use_android_vr"] = True
                # 如果详情已加载，传递 VR 格式 ID 以供过滤
                if data.get("detail"):
                    opts["__android_vr_format_ids"] = data["detail"].get(
                        "__android_vr_format_ids", []
                    )

            # NEW: Advanced selection
            if data.get("custom_selection_data"):
                sel = data["custom_selection_data"]
                if sel and sel.get("format"):
                    opts["format"] = sel["format"]
                    opts.update(sel.get("extra_opts") or {})
                    # Do not download thumbnail files during download.
                    opts["writethumbnail"] = False
                    opts["addmetadata"] = True

                    tasks.append(
                        {
                            "url": url,
                            "title": str(data.get("title") or "未命名任务"),
                            "thumbnail": str(data.get("thumbnail") or "").strip() or None,
                            "opts": opts,
                        }
                    )
                    continue

            # VR 模式自动/简单选择
            if self._vr_mode:
                if bool(data.get("manual_override")) and data.get("override_format_id"):
                    opts["format"] = f"{data['override_format_id']}+bestaudio/best"
                else:
                    opts["format"] = vr_preset_fmt or "bestvideo+bestaudio/best"
                    opts.update(vr_preset_args)

                opts["writethumbnail"] = False
                opts["addmetadata"] = True
                tasks.append(
                    {
                        "url": url,
                        "title": str(data.get("title") or "未命名任务"),
                        "thumbnail": str(data.get("thumbnail") or "").strip() or None,
                        "opts": opts,
                    }
                )
                continue

            # 0=音视频，1=仅视频，2=仅音频
            audio_id = (
                data.get("audio_override_format_id")
                if bool(data.get("audio_manual_override"))
                else data.get("audio_best_format_id")
            )
            audio_id_str = str(audio_id) if audio_id else None

            # Resolve chosen stream extensions when available (for lossless container decision)
            video_ext: str | None = None
            audio_ext: str | None = None
            try:
                vid = str(data.get("override_format_id") or "").strip()
                if vid:
                    for vf in data.get("video_formats") or []:
                        if str(vf.get("id") or "") == vid:
                            video_ext = str(vf.get("ext") or "").strip() or None
                            break
            except Exception:
                video_ext = None

            try:
                aid = (
                    data.get("audio_override_format_id")
                    if bool(data.get("audio_manual_override"))
                    else data.get("audio_best_format_id")
                )
                aid = str(aid or "").strip()
                if aid:
                    for af in data.get("audio_formats") or []:
                        if str(af.get("id") or "") == aid:
                            audio_ext = str(af.get("ext") or "").strip() or None
                            break
            except Exception:
                audio_ext = None

            if mode == 2:
                # audio-only
                opts["format"] = audio_id_str or "bestaudio/best"
            elif mode == 1:
                # video-only
                override_id = data.get("override_format_id")
                if override_id:
                    opts["format"] = str(override_id)
                else:
                    if preset_height:
                        opts["format"] = (
                            f"bestvideo[height={preset_height}][acodec=none]/"
                            f"bestvideo[height={preset_height}]/"
                            f"bestvideo[acodec=none]/bestvideo"
                        )
                        opts["__fluentytdl_quality_height"] = preset_height
                    else:
                        opts["format"] = "bestvideo[acodec=none]/bestvideo"
                # Do not force output container; keep original stream container.
            else:
                # AV
                override_id = data.get("override_format_id")
                if override_id:
                    if audio_id_str:
                        opts["format"] = f"{override_id}+{audio_id_str}"
                    else:
                        opts["format"] = f"{override_id}+bestaudio/best"
                else:
                    # preset mapping
                    if preset_height:
                        if audio_id_str:
                            opts["format"] = f"bestvideo[height={preset_height}]+{audio_id_str}"
                        else:
                            opts["format"] = f"bestvideo[height={preset_height}]+bestaudio/best"
                        opts["__fluentytdl_quality_height"] = preset_height
                    else:
                        if audio_id_str:
                            opts["format"] = f"bestvideo+{audio_id_str}"
                        else:
                            opts["format"] = "bestvideo+bestaudio/best"
                # Container policy for assembled streams:
                # - If video/audio containers are compatible, keep the original video container.
                # - Otherwise fallback to mkv for compatibility.
                merge_fmt = _choose_lossless_merge_container(video_ext, audio_ext)
                if merge_fmt:
                    opts["merge_output_format"] = merge_fmt

            # Do not download thumbnail files during download.
            opts["writethumbnail"] = False
            opts["addmetadata"] = True

            tasks.append(
                {
                    "url": url,
                    "title": str(data.get("title") or "未命名任务"),
                    "thumbnail": str(data.get("thumbnail") or "").strip() or None,
                    "opts": opts,
                }
            )

        return tasks

    def _on_thumb_loaded(self, pixmap) -> None:
        if self._is_closing:
            return
        if self.thumb_label is not None:
            self.thumb_label.setImage(pixmap)

    def _populate_formats(self, info: dict[str, Any]) -> None:
        """核心逻辑：清洗 formats"""
        if self.type_combo is None or self.format_combo is None:
            return
        formats = info.get("formats", []) or []

        self.video_formats = []
        seen_res: set[int] = set()

        # VR 模式下，使用更复杂的排序和展示逻辑
        if self._vr_mode:
            # 1. 过滤
            compatible_ids = set(info.get("__android_vr_format_ids") or [])
            should_filter = bool(compatible_ids)

            candidates = []
            for f in formats:
                if f.get("vcodec") in (None, "none"):
                    continue
                fid = str(f.get("format_id") or "")
                if should_filter and fid not in compatible_ids:
                    continue
                h = int(f.get("height") or 0)
                if h < 360:
                    continue
                candidates.append(f)

            # 2. 排序 (3D > 投影 > 分辨率)
            _STEREO_ORDER = {"stereo_tb": 0, "stereo_sbs": 0, "mono": 1, "unknown": 2}
            _PROJ_ORDER = {"equirectangular": 0, "mesh": 1, "eac": 2, "unknown": 3}

            def _vr_sort_key(f: dict[str, Any]) -> tuple:
                stereo = str(f.get("__vr_stereo_mode") or "unknown")
                proj = str(f.get("__vr_projection") or "unknown")
                h = int(f.get("height") or 0)
                # 优先显示 stereo_tb/sbs，且优先 equirectangular，分辨率降序
                return (_STEREO_ORDER.get(stereo, 9), _PROJ_ORDER.get(proj, 9), -h)

            candidates.sort(key=_vr_sort_key)

            # 3. 构造显示项
            seen_keys = set()
            for f in candidates:
                h = int(f.get("height") or 0)
                stereo = str(f.get("__vr_stereo_mode") or "unknown")
                proj = str(f.get("__vr_projection") or "unknown")
                vc = str(f.get("vcodec") or "")[:4]

                # 唯一键：高度+立体+投影+编码
                # 这样可以显示不同版本的同一分辨率（比如 VP9 vs AV1）
                # 简化起见，我们只取最高质量的每个分辨率变体
                key = (h, stereo, proj)
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                # 构造文本
                res_str = f"{h}p"
                fps = f.get("fps")
                if fps and fps > 30:
                    res_str += f" {int(fps)}fps"

                stereo_str = ""
                if stereo == "stereo_tb":
                    stereo_str = " [3D TB]"
                elif stereo == "stereo_sbs":
                    stereo_str = " [3D SBS]"
                elif stereo == "mono":
                    stereo_str = " [2D]"

                proj_str = ""
                if proj == "equirectangular":
                    proj_str = " (Equi)"
                elif proj == "mesh":
                    proj_str = " (Mesh)"
                elif proj == "eac":
                    proj_str = " (EAC)"

                ext = f.get("ext") or "?"

                display_text = f"{res_str}{stereo_str}{proj_str} - {ext} ({vc})"

                self.video_formats.append(
                    {
                        "text": display_text,
                        "id": f.get("format_id"),
                        "height": h,
                        # 保存完整对象以便后续使用
                        "_raw": f,
                    }
                )
        else:
            # 普通模式
            for f in formats:
                # 过滤掉仅音频和无效视频
                if f.get("vcodec") == "none":
                    continue

                h = int(f.get("height") or 0)
                if h < 360:
                    continue

                # 构造显示文本
                res_str = f"{h}p"
                fps = f.get("fps")
                if fps and fps > 30:
                    res_str += f" {int(fps)}fps"

                # 去重：仅保留每个分辨率的一条入口（后续可扩展为“推荐/更多”）
                if h not in seen_res:
                    ext = f.get("ext") or "?"
                    self.video_formats.append(
                        {
                            "text": f"{res_str} - {ext}",
                            "id": f.get("format_id"),
                            "height": h,
                        }
                    )
                    seen_res.add(h)

            self.video_formats.sort(key=lambda x: x["height"], reverse=True)

        self._update_format_list()

    def _update_format_list(self) -> None:
        if self.type_combo is None or self.format_combo is None:
            return

        self.format_combo.clear()
        mode = self.type_combo.currentIndex()

        if mode == 0:  # Video + Audio
            for item in self.video_formats:
                self.format_combo.addItem(item["text"], userData=item["id"])
            if self.format_combo.count() > 0:
                self.format_combo.setCurrentIndex(0)
        else:  # Audio Only
            self.format_combo.addItem("最佳质量 (原格式)", userData="bestaudio")

    def get_download_options(self, embed_subtitles_override: bool | None = None) -> dict[str, Any]:
        """
        返回构建好的 yt-dlp options

        Args:
            embed_subtitles_override: 覆盖字幕嵌入选项 (None=使用配置默认, True=嵌入, False=不嵌入)
        """
        opts: dict[str, Any] = {}

        # Prefer new single-video table selection if available
        mode_combo = self._single_mode_combo
        if mode_combo is not None:
            mode = int(
                mode_combo.currentIndex()
            )  # 0=assemble, 1=muxed-only, 2=video-only, 3=audio-only

            def _find_single_ext(fid: str | None) -> str | None:
                if not fid:
                    return None
                for r in self._single_rows:
                    if str(r.get("format_id") or "") == str(fid):
                        ext = str(r.get("ext") or "").strip()
                        return ext or None
                return None

            if mode == 3:
                # audio-only
                aid = self._single_selected_audio_id
                opts["format"] = str(aid) if aid else "bestaudio/best"
            elif mode == 2:
                # video-only
                vid = self._single_selected_video_id
                opts["format"] = str(vid) if vid else "bestvideo[acodec=none]/bestvideo"
            elif mode == 1:
                # muxed-only
                mid = self._single_selected_muxed_id
                opts["format"] = str(mid) if mid else "best[acodec!=none][vcodec!=none]/best"
            else:
                # assemble
                if self._single_selected_video_id and self._single_selected_audio_id:
                    opts["format"] = (
                        f"{self._single_selected_video_id}+{self._single_selected_audio_id}"
                    )
                    v_ext = _find_single_ext(self._single_selected_video_id)
                    a_ext = _find_single_ext(self._single_selected_audio_id)
                    merge_fmt = _choose_lossless_merge_container(v_ext, a_ext)
                    if merge_fmt:
                        opts["merge_output_format"] = merge_fmt
                elif self._single_selected_video_id:
                    opts["format"] = f"{self._single_selected_video_id}+bestaudio/best"
                else:
                    opts["format"] = "bestvideo+bestaudio/best"

            # Do not download thumbnail files during download.
            opts["writethumbnail"] = False
            opts["addmetadata"] = True

            # 集成字幕服务
            if self.video_info:
                subtitle_opts = subtitle_service.apply(
                    video_id=self.video_info.get("id", ""),
                    video_info=self.video_info,
                )
                opts.update(subtitle_opts)

                # 根据 embed_type 应用覆盖选项
                if embed_subtitles_override is not None:
                    from ...core.config_manager import config_manager as cfg

                    embed_type = cfg.get_subtitle_config().embed_type
                    if embed_type == "soft":
                        opts["embedsubtitles"] = embed_subtitles_override
                    else:
                        opts["embedsubtitles"] = False

                _ensure_subtitle_compatible_container(opts)

            return opts

        # Fallback to legacy combo selection (for safety)
        mode = self.type_combo.currentIndex() if self.type_combo is not None else 0
        fmt_id = self.format_combo.currentData() if self.format_combo is not None else None
        if mode == 0:
            if fmt_id:
                opts["format"] = f"{fmt_id}+bestaudio/best"
            else:
                opts["format"] = "bestvideo+bestaudio/best"
        else:
            opts["format"] = "bestaudio/best"
        opts["writethumbnail"] = False
        opts["addmetadata"] = True

        # 集成字幕服务
        if self.video_info:
            subtitle_opts = subtitle_service.apply(
                video_id=self.video_info.get("id", ""),
                video_info=self.video_info,
            )
            opts.update(subtitle_opts)

            # 根据 embed_type 应用覆盖选项
            if embed_subtitles_override is not None:
                from ...core.config_manager import config_manager as cfg

                embed_type = cfg.get_subtitle_config().embed_type
                if embed_type == "soft":
                    opts["embedsubtitles"] = embed_subtitles_override
                else:
                    opts["embedsubtitles"] = False

            _ensure_subtitle_compatible_container(opts)

        return opts
