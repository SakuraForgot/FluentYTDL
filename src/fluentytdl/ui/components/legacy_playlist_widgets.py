from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    BodyLabel,
    CaptionLabel,
    IndeterminateProgressRing,
    MessageBoxBase,
    PushButton,
    RadioButton,
    ScrollArea,
    SubtitleLabel,
    ToolTipFilter,
    ToolTipPosition,
)

from .cover_selector import CoverSelectorWidget
from .format_selector import VideoFormatSelectorWidget
from .subtitle_selector import SubtitleSelectorWidget
from .vr_format_selector import VRFormatSelectorWidget

# ---- 字幕容器兼容性辅助函数 ----
# MP4 和 MKV 都支持字幕嵌入（FFmpeg 自动将 SRT 转为 mov_text），只有 WebM 不支持 SRT/ASS
_SUBTITLE_COMPATIBLE_CONTAINERS = {"mp4", "mkv", "mov", "m4v"}


def _ensure_subtitle_compatible_container(opts: dict[str, Any]) -> None:
    """
    确保容器格式兼容字幕嵌入。

    只有当 embedsubtitles=True 时才需要检查：
    - 多语言字幕 (subtitleslangs > 1) + mp4 → 升级 mkv
      （mp4 mov_text 只能为实嵌入单轨，多轨在大多数播放器中被静默丢弃）
    - WebM 不支持 SRT/ASS 字幕嵌入 → 改用 MKV
    - 未指定容器（原盘模式）→ 默认 MKV
    - MP4 + 单字幕 → 保持（FFmpeg mov_text 单轨可用）
    - MKV/MOV 等 → 保持
    """
    if not opts.get("embedsubtitles"):
        return

    fmt = (opts.get("merge_output_format") or "").lower()

    # 多语言字幕嵌入：mp4 mov_text 多轨支持差，必须升级 mkv
    sub_langs = opts.get("subtitleslangs") or []
    if isinstance(sub_langs, list) and len(sub_langs) > 1 and (fmt == "mp4" or not fmt):
        opts["merge_output_format"] = "mkv"
        return

    if fmt in _SUBTITLE_COMPATIBLE_CONTAINERS:
        # 单字幕 mp4/mkv/mov 等，已支持，保持
        return
    elif fmt == "webm":
        # WebM 不支持 SRT/ASS 字幕嵌入
        opts["merge_output_format"] = "mkv"
    elif not fmt:
        # 未指定容器（原盘/默认），使用 MKV 确保兼容
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


def _infer_entry_thumbnail(entry: Any | dict[str, Any]) -> str:
    """推断视频条目的缩略图 URL，优先使用中等质量以加速加载"""
    if not isinstance(entry, dict):
        return str(getattr(entry, "thumbnail", "") or "").strip()
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
            opts = self.selector.get_opts()  # type: ignore[attr-defined]
            return {"extra_opts": opts, "format": "subtitle_custom"}
        elif self._mode == "cover":
            url = self.selector.get_selected_url()  # type: ignore[attr-defined]
            ext = self.selector.get_selected_ext()  # type: ignore[attr-defined]
            return {"cover_url": url, "cover_ext": ext, "format": "cover_custom"}
        else:
            return self.selector.get_selection_result()  # type: ignore[attr-defined]

    def get_summary(self) -> str:
        if self._mode == "subtitle":
            selected = self.selector.get_selected_tracks()  # type: ignore[attr-defined]
            if not selected:
                return "未选择字幕"
            return f"已选择 {len(selected)} 种语言"
        elif self._mode == "cover":
            ext = self.selector.get_selected_ext()  # type: ignore[attr-defined]
            return f"已选择 {ext.upper()} 封面"
        else:
            return self.selector.get_summary_text()  # type: ignore[attr-defined]
