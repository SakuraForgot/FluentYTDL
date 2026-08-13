from __future__ import annotations

import re
import time
from collections import deque
from enum import Enum
from functools import partial
from typing import Any

from PySide6.QtCore import QModelIndex, QPoint, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QStyleOptionViewItem,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    ComboBox,
    ImageLabel,
    IndeterminateProgressRing,
    LineEdit,
    MessageBox,
    PrimaryPushButton,
    PushButton,
    SegmentedWidget,
    StrongBodyLabel,
    SubtitleLabel,
    SwitchButton,
)
from qframelesswindow import FramelessWindow

from fluentytdl.ui.components.common.themed_title_bar import ThemedTitleBar
from fluentytdl.ui.components.dialogs.section_range_selector import SectionRangeSelector
from fluentytdl.ui.components.dialogs.selection_dialog import (
    PlaylistFormatDialog,
    PlaylistPreviewWidget,
    _clean_audio_formats,
    _clean_video_formats,
    _format_duration,
    _format_size,
    _format_upload_date,
    _infer_entry_thumbnail,
    _infer_entry_url,
)
from fluentytdl.ui.components.platforms.cover import CoverSelectorWidget
from fluentytdl.ui.components.platforms.subtitle import SubtitleSelectorWidget
from fluentytdl.ui.components.platforms.vr import VR_PRESETS, VRFormatSelectorWidget
from fluentytdl.ui.components.platforms.youtube import VideoFormatSelectorWidget

from ....core.section_download import build_section_opts, section_filename_suffix
from ....download.extract_manager import AsyncExtractManager
from ....download.workers import EntryDetailWorker, InfoExtractWorker, VRInfoExtractWorker
from ....models.mappers import VideoInfoMapper
from ....models.subtitle_config import PlaylistSubtitleOverride
from ....models.video_info import VideoInfo
from ....models.video_task import VideoTask
from ....processing import subtitle_service
from ....utils.filesystem import sanitize_filename
from ....utils.image_loader import get_image_loader
from ....utils.logger import logger
from ....utils.paths import resource_path
from ....utils.validators import UrlValidator
from ....youtube.youtube_service import YoutubeServiceOptions
from ...delegates.playlist_delegate import PlaylistItemDelegate
from ...dialogs.playlist_subtitle_dialog import PlaylistSubtitleConfigDialog
from ...dialogs.subtitle_picker_dialog import SubtitlePickerDialog, SubtitlePickerResult
from ...models.playlist_model import PlaylistListModel
from ...playlist_scheduler import PlaylistScheduler


class _PlaylistModelRowProxy:
    """将原有 ActionWidget 写入路径映射到 PlaylistListModel。"""

    def __init__(self, row: int, model: PlaylistListModel, notify_row_changed) -> None:
        self._row = row
        self._model = model
        self._notify_row_changed = notify_row_changed
        self._batch_mode = False
        self._batch_dirty = False

        outer = self

        class _QualityButtonProxy:
            def setText(self_, text: str) -> None:
                idx = outer._model.index(outer._row, 0)
                task = outer._model.get_task(idx)
                if task is not None:
                    new_text = str(text)
                    changed = task.custom_options.format != new_text or task.is_parsing
                    task.custom_options.format = new_text
                    task.is_parsing = False
                    if changed:
                        if outer._batch_mode:
                            outer._batch_dirty = True
                        else:
                            outer._notify_row_changed(outer._row)

            def setToolTip(self_, _text: str) -> None:
                pass

        class _InfoLabelProxy:
            def setText(self_, _text: str) -> None:
                pass

        self.qualityButton = _QualityButtonProxy()
        self.infoLabel = _InfoLabelProxy()

    def begin_batch(self) -> None:
        """Suppress per-property notifications until end_batch()."""
        self._batch_mode = True
        self._batch_dirty = False

    def end_batch(self) -> None:
        """Flush a single notification if any property changed during the batch."""
        self._batch_mode = False
        if self._batch_dirty:
            self._batch_dirty = False
            self._notify_row_changed(self._row)

    def set_loading(self, loading: bool, text: str | None = None) -> None:
        idx = self._model.index(self._row, 0)
        task = self._model.get_task(idx)
        if task is None:
            return
        changed = task.is_parsing != bool(loading)
        task.is_parsing = bool(loading)
        if text is not None:
            new_text = str(text)
            if task.custom_options.format != new_text:
                task.custom_options.format = new_text
                changed = True
        if changed:
            if self._batch_mode:
                self._batch_dirty = True
            else:
                self._notify_row_changed(self._row)


class WindowState(Enum):
    LOADING = "loading"
    CONTENT = "content"
    ERROR_COOKIE = "error_cookie"
    ERROR_NETWORK = "error_network"
    ERROR_GENERIC = "error_generic"


def _extract_subtitles_from_obj(info: Any) -> dict[str, Any]:
    """从 DTO 对象属性中提取手动字幕数据（回退路径防御）"""
    subs = getattr(info, "subtitles", None)
    if not isinstance(subs, dict) or not subs:
        return {}
    result: dict[str, Any] = {}
    for lang, tracks in subs.items():
        if isinstance(tracks, list):
            result[str(lang)] = [
                {
                    "url": getattr(t, "url", ""),
                    "ext": getattr(t, "ext", "vtt"),
                    "name": getattr(t, "name", ""),
                }
                if not isinstance(t, dict)
                else t
                for t in tracks
            ]
    return result


def _extract_auto_captions_from_obj(info: Any) -> dict[str, Any]:
    """从 DTO 对象属性中提取自动字幕数据（回退路径防御）"""
    auto = getattr(info, "automatic_captions", None)
    if isinstance(auto, dict) and auto:
        return dict(auto)
    return {}


def _normalize_info_payload(info: Any) -> dict[str, Any]:
    """Normalize extraction payload to a dict for legacy UI code paths."""
    if isinstance(info, dict):
        return info

    raw_dict = getattr(info, "raw_dict", None)
    if isinstance(raw_dict, dict) and raw_dict:
        return raw_dict

    entries_raw = getattr(info, "entries", [])
    entries: list[dict[str, Any]] = []
    if isinstance(entries_raw, list):
        for entry in entries_raw:
            if isinstance(entry, dict):
                entries.append(entry)
                continue
            entry_raw = getattr(entry, "raw_dict", None)
            if isinstance(entry_raw, dict) and entry_raw:
                entries.append(entry_raw)
                continue
            entries.append(
                {
                    "id": str(getattr(entry, "id", "") or ""),
                    "title": str(getattr(entry, "title", "") or ""),
                    "uploader": str(getattr(entry, "uploader", "") or ""),
                    "duration": int(getattr(entry, "duration", 0) or 0),
                    "thumbnail": str(getattr(entry, "thumbnail", "") or ""),
                    "webpage_url": str(getattr(entry, "webpage_url", "") or ""),
                }
            )

    formats_raw = getattr(info, "formats", [])
    formats: list[dict[str, Any]] = []
    if isinstance(formats_raw, list):
        for fmt in formats_raw:
            if isinstance(fmt, dict):
                formats.append(fmt)
                continue
            formats.append(
                {
                    "format_id": str(getattr(fmt, "format_id", "") or ""),
                    "ext": str(getattr(fmt, "ext", "") or ""),
                    "vcodec": str(getattr(fmt, "vcodec", "none") or "none"),
                    "acodec": str(getattr(fmt, "acodec", "none") or "none"),
                    "filesize": int(getattr(fmt, "filesize", 0) or 0),
                    "fps": float(getattr(fmt, "fps", 0.0) or 0.0),
                    "height": int(getattr(fmt, "height", 0) or 0),
                    "width": int(getattr(fmt, "width", 0) or 0),
                    "url": str(getattr(fmt, "url", "") or ""),
                    "format_note": str(getattr(fmt, "format_note", "") or ""),
                    "resolution": str(getattr(fmt, "resolution", "") or ""),
                    "vbr": float(getattr(fmt, "vbr", 0.0) or 0.0),
                    "abr": float(getattr(fmt, "abr", 0.0) or 0.0),
                    "tbr": float(getattr(fmt, "tbr", 0.0) or 0.0),
                    "container": str(getattr(fmt, "container", "") or ""),
                    "protocol": str(getattr(fmt, "protocol", "") or ""),
                    "video_ext": str(getattr(fmt, "video_ext", "") or ""),
                    "audio_ext": str(getattr(fmt, "audio_ext", "") or ""),
                }
            )

    is_playlist = bool(getattr(info, "is_playlist", False) or entries)
    normalized: dict[str, Any] = {
        "id": str(getattr(info, "id", "") or ""),
        "title": str(getattr(info, "title", "") or ""),
        "uploader": str(getattr(info, "uploader", "") or ""),
        "duration": int(getattr(info, "duration", 0) or 0),
        "thumbnail": str(getattr(info, "thumbnail", "") or ""),
        "is_live": bool(getattr(info, "is_live", False)),
        "view_count": int(getattr(info, "view_count", 0) or 0),
        "like_count": int(getattr(info, "like_count", 0) or 0),
        "channel": str(getattr(info, "channel", "") or ""),
        "channel_id": str(getattr(info, "channel_id", "") or ""),
        "upload_date": str(getattr(info, "upload_date", "") or ""),
        "webpage_url": str(getattr(info, "webpage_url", "") or ""),
        "formats": formats,
        "entries": entries,
        "subtitles": _extract_subtitles_from_obj(info),
        "automatic_captions": _extract_auto_captions_from_obj(info),
    }
    if is_playlist:
        normalized["_type"] = "playlist"
    vr_mode = getattr(info, "vr_mode", None)
    if vr_mode is not None:
        normalized["__fluentytdl_vr_mode"] = bool(vr_mode)
    return normalized


def _section_stream_metadata(
    formats: Any, format_expr: str, section_duration: float, full_duration: float
) -> tuple[str, int]:
    """Describe the selected source streams and estimate their clip bytes."""
    if not isinstance(formats, list) or section_duration <= 0 or full_duration <= 0:
        return "", 0
    selected_ids = set(re.findall(r"(?<![\w-])(\d+)(?![\w-])", format_expr or ""))
    selected = [
        item
        for item in formats
        if isinstance(item, dict) and str(item.get("format_id") or "") in selected_ids
    ]
    has_video = any(str(item.get("vcodec") or "none").lower() != "none" for item in selected)
    has_audio = any(str(item.get("acodec") or "none").lower() != "none" for item in selected)
    has_muxed = any(
        str(item.get("vcodec") or "none").lower() != "none"
        and str(item.get("acodec") or "none").lower() != "none"
        for item in selected
    )
    layout = (
        "muxed"
        if has_muxed
        else ("video_audio" if has_video and has_audio else ("video" if has_video else "audio"))
    )
    source_bytes = sum(
        int(item.get("filesize") or item.get("filesize_approx") or 0) for item in selected
    )
    if not source_bytes:
        # YouTube often omits an exact file size but supplies total bitrate.
        # Estimate the requested range from stream bitrate in that case.
        total_kbps = sum(float(item.get("tbr") or 0.0) for item in selected)
        if total_kbps > 0:
            source_bytes = int(total_kbps * 1000 / 8 * full_duration)
    return layout, int(source_bytes * section_duration / full_duration) if source_bytes else 0


class DownloadConfigWindow(FramelessWindow):
    """
    独立非模态下载配置窗口
    """

    downloadRequested = Signal(list)  # 发送任务列表 [tasks]
    windowClosed = Signal(object)  # 发送自身引用，用于主窗口清理

    # 第二个参数携带切换前那一次「已经解析完成」的 info_dict（可能为 None）。
    # 新窗口用它先把标题/缩略图画出来，不必让用户对着空白转圈等第二轮子进程。
    request_vr_switch = Signal(str, object)  # 请求切换到 VR 模式
    request_normal_switch = Signal(str, object)  # 请求切换回普通模式

    def __init__(
        self,
        url: str,
        parent=None,
        *,
        vr_mode: bool = False,
        mode: str = "default",
        smart_detect: bool = False,
        playlist_flat: bool = False,
        target_tab: str | None = None,
        preloaded_info: dict[str, Any] | None = None,
    ):
        # parent=None ensures independent window behavior (taskbar icon, not always-on-top of main)
        super().__init__(parent=None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.url = url
        self._vr_mode = vr_mode or (mode == "vr")
        self._mode = mode
        self._smart_detect = smart_detect
        self._playlist_flat = playlist_flat
        # 智能模式切换时上一轮的解析结果。只用于渲染加载页的预览条，
        # 绝不参与格式选择/下载——VR 与普通模式的格式列表来源不同，必须以本轮解析为准。
        self._preloaded_info = preloaded_info if isinstance(preloaded_info, dict) else None
        self._preview_thumb_url = ""
        self._preview_show_on_thumb = False
        self._window_state = WindowState.LOADING
        self.video_info: dict[str, Any] | None = None
        self.video_info_dto: VideoInfo | None = None
        try:
            from ....core.config_manager import config_manager

            self._download_dir = str(config_manager.get("download_dir") or "").strip()
        except Exception:
            self._download_dir = ""
        self._download_dir_edit: LineEdit | None = None

        # 换成跟随主题的标题栏，否则深色模式下最小化/最大化/关闭按钮是黑图标
        self.setTitleBar(ThemedTitleBar(self))
        self.titleBar.raise_()

        # === UI Init ===
        self.setWindowTitle(self.tr("新建任务"))
        init_w, init_h = 600, 400

        # 解除硬限制，完全由几何动画或手动设定控制
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        # 初始居中（计算独立的目标几何图形）
        target_geo = self._get_target_geometry(init_w, init_h, 40)
        self.setGeometry(target_geo)

        # 主布局容器
        self.main_widget = QWidget(self)
        self.v_layout = QVBoxLayout(self.main_widget)
        self.v_layout.setContentsMargins(24, 48, 24, 24)  # 顶部留出标题栏空间
        self.v_layout.setSpacing(16)

        # 内容区域 (View Layout)
        self.viewLayout = QVBoxLayout()
        self.viewLayout.setSpacing(10)
        self.v_layout.addLayout(self.viewLayout)

        # 底部按钮区域
        self.buttonLayout = QHBoxLayout()
        self.buttonLayout.setSpacing(12)
        self.buttonLayout.addStretch(1)

        self.cancelButton = PushButton(self.tr("取消"), self)
        self.yesButton = PrimaryPushButton(self.tr("下载"), self)
        self.yesButton.setDisabled(True)

        self.buttonLayout.addWidget(self.cancelButton)
        self.buttonLayout.addWidget(self.yesButton)

        self.v_layout.addLayout(self.buttonLayout)

        # 布局设置到窗口
        self.setLayout(self.v_layout)

        # 连接按钮
        self.cancelButton.clicked.connect(self.close)
        self.yesButton.clicked.connect(self._on_download_clicked)

        # === 状态初始化 ===
        self._is_playlist = False
        self._is_channel = False
        self._target_tab = target_tab or "all"  # user's initial selection
        self._channel_tab = self._target_tab  # current selected tab in UI
        self._channel_caches = {
            "all": {"status": "unloaded", "data": None},
            "videos": {"status": "unloaded", "data": None},
            "shorts": {"status": "unloaded", "data": None},
            "streams": {"status": "unloaded", "data": None},
        }
        self._channel_reverse = False
        self.download_tasks: list[dict[str, Any]] = []
        self._active_workers: list[QThread] = []

        self._subtitle_embed_choice: bool | None = None
        self._subtitle_choice_made = False
        self._subtitle_pick_result: SubtitlePickerResult | None = None
        self._section_selector: SectionRangeSelector | None = None
        self._subtitle_state_before_section: tuple[bool, bool] | None = None
        self._playlist_sub_override: PlaylistSubtitleOverride | None = None

        # P4: 使用全局单例，所有窗口共享同一个 NetworkManager
        self.image_loader = get_image_loader()
        self.image_loader.loaded.connect(self._on_thumb_loaded)
        self.image_loader.loaded_with_url.connect(self._on_thumb_loaded_with_url)
        self.image_loader.failed.connect(self._on_thumb_failed)

        self.thumb_label: ImageLabel | None = None

        # playlist UI state
        self._playlist_rows: list[dict[str, Any]] = []
        self._table: QTableWidget | None = None
        self._list_view: QListView | None = None
        self._playlist_model: PlaylistListModel | None = None
        self._playlist_delegate: PlaylistItemDelegate | None = None
        self._extract_manager: AsyncExtractManager | None = None
        self._thumb_label_by_row: dict[int, QLabel] = {}
        self._preview_widget_by_row: dict[int, PlaylistPreviewWidget] = {}
        self._action_widget_by_row: dict[int, Any] = {}
        self._thumb_cache: dict[str, Any] = {}
        self._thumb_url_to_rows: dict[str, set[int]] = {}
        self._thumb_applied_rows: dict[int, str] = {}
        self._thumb_requested: set[str] = set()
        self._thumb_pending: deque[str] = deque()  # O(1) popleft
        self._thumb_retry_count: dict[str, int] = {}
        self._thumb_inflight: int = 0
        self._thumb_max_concurrent: int = 4

        self._build_chunk_entries: list[dict[str, Any]] = []
        self._build_chunk_offset: int = 0
        self._build_chunk_size: int = 30
        self._build_is_chunking: bool = False

        self._scroll_throttle_timer = QTimer(self)
        self._scroll_throttle_timer.setSingleShot(True)
        self._scroll_throttle_timer.setInterval(50)
        self._scroll_throttle_timer.timeout.connect(self._on_scroll_throttled)

        self._last_interaction = time.monotonic()
        self._lazy_paused: bool = (
            False  # 用户手动暂停后台解析（初始缓存值，实际状态在 scheduler 中）
        )
        self._scheduler: PlaylistScheduler | None = None  # 播放列表调度器（build 完成后创建）

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(2000)
        self._idle_timer.timeout.connect(self._on_idle_tick)

        self._detail_finalize_rows: set[int] = set()
        self._detail_finalize_timer = QTimer(self)
        self._detail_finalize_timer.setSingleShot(True)
        self._detail_finalize_timer.setInterval(80)
        self._detail_finalize_timer.timeout.connect(self._flush_detail_finalizations)

        self._thumb_init_timer = QTimer(self)
        self._thumb_init_timer.setSingleShot(True)
        self._thumb_init_timer.setInterval(0)
        self._thumb_init_timer.timeout.connect(self._on_thumb_init_timeout)

        # UI 初始化：顶部标题
        self.titleLabel = SubtitleLabel("", self)
        self.titleLabel.hide()
        self.viewLayout.addWidget(self.titleLabel)

        # 解析中加载页
        self.loadingWidget = QWidget(self)
        self.loadingLayout = QVBoxLayout(self.loadingWidget)
        self.loadingLayout.setContentsMargins(0, 0, 0, 0)
        self.loadingLayout.setSpacing(12)
        self.loadingLayout.addStretch(1)

        self.loadingTitleLabel = SubtitleLabel(
            self.tr("正在使用 VR 模式解析...") if self._vr_mode else self.tr("正在解析链接..."),
            self.loadingWidget,
        )
        self.loadingTitleLabel.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self.loadingLayout.addWidget(self.loadingTitleLabel, 0, Qt.AlignmentFlag.AlignHCenter)

        # 加载页预览条：智能模式切换（普通 ⇄ VR）时，上一轮解析结果直接带过来先渲染，
        # 把「4 秒白屏转圈」变成「立刻看到是哪个视频，格式还在解析」。
        # 只放标题 + 缩略图：不放格式列表，避免用户对着上一轮模式的格式做选择。
        self.previewWidget = QWidget(self.loadingWidget)
        preview_h = QHBoxLayout(self.previewWidget)
        preview_h.setContentsMargins(0, 0, 0, 0)
        preview_h.setSpacing(12)

        self.previewThumb = ImageLabel(self.previewWidget)
        self.previewThumb.setFixedSize(160, 90)
        self.previewThumb.setScaledContents(True)
        self.previewThumb.setBorderRadius(8, 8, 8, 8)
        preview_h.addWidget(self.previewThumb)

        self.previewTitleLabel = StrongBodyLabel("", self.previewWidget)
        self.previewTitleLabel.setWordWrap(True)
        self.previewTitleLabel.setMaximumWidth(320)
        preview_h.addWidget(self.previewTitleLabel, 1)

        self.previewWidget.hide()
        self.loadingLayout.addWidget(self.previewWidget, 0, Qt.AlignmentFlag.AlignHCenter)

        self.loadingRing = IndeterminateProgressRing(self.loadingWidget)
        self.loadingRing.setFixedSize(46, 46)
        self.loadingLayout.addWidget(self.loadingRing, 0, Qt.AlignmentFlag.AlignCenter)

        self.loadingLayout.addStretch(1)
        self.viewLayout.addWidget(self.loadingWidget, 1)

        # 内容容器
        self.contentWidget = QWidget()
        self.contentLayout = QVBoxLayout(self.contentWidget)
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(12)
        self.viewLayout.addWidget(self.contentWidget, 1)

        # 深浅模式适配
        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()
        self.contentWidget.hide()

        # ========== Cookie 预检警告条 ==========
        self._cookieWarningLabel = CaptionLabel("", self)
        self._cookieWarningLabel.setWordWrap(True)
        self._cookieWarningLabel.setStyleSheet(
            "QLabel { background: rgba(255, 193, 7, 0.15); padding: 8px; "
            "border-radius: 6px; color: #b8860b; }"
        )
        self._cookieWarningLabel.hide()
        self.viewLayout.addWidget(self._cookieWarningLabel)
        self._run_cookie_precheck()

        # ========== 身份验证重试面板 ==========
        self.retryWidget = QWidget(self)
        self.retryLayout = QVBoxLayout(self.retryWidget)
        self.retryLayout.setContentsMargins(0, 8, 0, 0)
        self.retryLayout.setSpacing(10)

        # 分段选择器
        self._authSegment = SegmentedWidget(self.retryWidget)
        self.retryLayout.addWidget(self._authSegment)

        # 3 个面板容器
        from PySide6.QtWidgets import QStackedWidget

        self._authStack = QStackedWidget(self.retryWidget)
        self.retryLayout.addWidget(self._authStack)

        # Helper for auth panel hint labels
        def create_hint_label(text: str, parent: QWidget):
            from PySide6.QtWidgets import QFrame

            container = QFrame(parent)
            container.setStyleSheet(
                "QFrame { background: rgba(128, 128, 128, 0.1); border-radius: 6px; }"
            )
            lay = QVBoxLayout(container)
            lay.setContentsMargins(10, 10, 10, 10)
            lbl = CaptionLabel(text, container)
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
            return container

        # --- 面板 1: WebView2 登录 ---
        dle_panel = QWidget()
        dle_lay = QVBoxLayout(dle_panel)
        dle_lay.setContentsMargins(0, 4, 0, 0)
        dle_lay.setSpacing(10)
        dle_hint = create_hint_label(
            self.tr(
                "将打开独立浏览器窗口，请登录 YouTube 账号。\n登录完成后将自动提取 Cookie 并重新解析。"
            ),
            dle_panel,
        )
        dle_lay.addWidget(dle_hint)
        webview2_account_row = QWidget(dle_panel)
        webview2_account_h = QHBoxLayout(webview2_account_row)
        webview2_account_h.setContentsMargins(0, 0, 0, 0)
        webview2_account_h.setSpacing(8)
        self._webview2AccountCombo = ComboBox(webview2_account_row)
        webview2_account_h.addWidget(self._webview2AccountCombo, 1)
        dle_lay.addWidget(webview2_account_row)
        self._dleRetryBtn = PrimaryPushButton(self.tr("登录 YouTube 并重试"), dle_panel)
        self._dleRetryBtn.clicked.connect(self._on_webview2_retry_clicked)
        dle_lay.addWidget(self._dleRetryBtn)
        self._dleStatusLabel = CaptionLabel("", dle_panel)
        dle_lay.addWidget(self._dleStatusLabel)
        self._authStack.addWidget(dle_panel)

        # --- 面板 2: 浏览器提取 ---
        extract_panel = QWidget()
        extract_lay = QVBoxLayout(extract_panel)
        extract_lay.setContentsMargins(0, 4, 0, 0)
        extract_lay.setSpacing(10)
        extract_hint = create_hint_label(
            self.tr("从本地已登录的浏览器中直接提取 Cookie。\n")
            + self.tr("Chromium 内核浏览器 (Edge/Chrome) 可能需要管理员权限。"),
            extract_panel,
        )
        extract_lay.addWidget(extract_hint)
        extract_row = QWidget(extract_panel)
        extract_h = QHBoxLayout(extract_row)
        extract_h.setContentsMargins(0, 0, 0, 0)
        extract_h.setSpacing(8)
        self._extractCombo = ComboBox(extract_row)
        self._extractCombo.addItems(
            [
                "Microsoft Edge",
                "Google Chrome",
                "Firefox",
                "Chromium",
                "Brave",
                "Opera",
                "Opera GX",
                "Vivaldi",
                "LibreWolf",
                self.tr("百分浏览器 (Cent)"),
            ]
        )
        self._extractRetryBtn = PrimaryPushButton(self.tr("提取并重试"), extract_row)
        self._extractRetryBtn.clicked.connect(self._on_extract_retry_clicked)
        extract_h.addWidget(self._extractCombo, 1)
        extract_h.addWidget(self._extractRetryBtn)
        extract_lay.addWidget(extract_row)
        self._authStack.addWidget(extract_panel)

        # --- 面板 3: 手动导入 ---
        import_panel = QWidget()
        import_lay = QVBoxLayout(import_panel)
        import_lay.setContentsMargins(0, 4, 0, 0)
        import_lay.setSpacing(10)
        import_hint = create_hint_label(
            self.tr("选择已有的 cookies.txt 文件 (Netscape 格式)。\n")
            + self.tr("可使用浏览器扩展 (如 Get cookies.txt LOCALLY) 导出。"),
            import_panel,
        )
        import_lay.addWidget(import_hint)
        self._importRetryBtn = PrimaryPushButton(self.tr("选择文件并重试"), import_panel)
        self._importRetryBtn.clicked.connect(self._on_import_retry_clicked)
        import_lay.addWidget(self._importRetryBtn)
        self._authStack.addWidget(import_panel)

        # --- 面板 4: 组件更新 ---
        update_panel = QWidget()
        update_lay = QVBoxLayout(update_panel)
        update_lay.setContentsMargins(0, 4, 0, 0)
        update_lay.setSpacing(10)
        update_hint = create_hint_label(
            self.tr("当前解析失败可能受限于平台最新的反爬风控机制（如 poToken 等）。\n")
            + self.tr("建议立即检测并更新 yt-dlp 核心解析组件。"),
            update_panel,
        )
        update_lay.addWidget(update_hint)
        update_row = QWidget(update_panel)
        update_h = QHBoxLayout(update_row)
        update_h.setContentsMargins(0, 0, 0, 0)
        update_h.setSpacing(8)
        self._updateRetryBtn = PrimaryPushButton(self.tr("一键检测并更新 yt-dlp"), update_row)
        self._updateRetryBtn.clicked.connect(self._on_update_retry_clicked)
        update_h.addWidget(self._updateRetryBtn)
        self._updateStatusLabel = CaptionLabel("", update_row)
        update_h.addWidget(self._updateStatusLabel)

        self._updateRing = IndeterminateProgressRing(update_row)
        self._updateRing.setFixedSize(20, 20)
        self._updateRing.hide()
        update_h.addWidget(self._updateRing)

        update_h.addStretch(1)
        update_lay.addWidget(update_row)
        self._authStack.addWidget(update_panel)

        # 绑定分段选择器
        self._authSegment.addItem(routeKey="webview2", text=self.tr("🔑 登录"))
        self._authSegment.addItem(routeKey="extract", text=self.tr("🚀 提取"))
        self._authSegment.addItem(routeKey="import", text=self.tr("📄 导入"))
        self._authSegment.addItem(routeKey="update", text=self.tr("⚙️ 更新"))

        # 移除 onClick 参数，改为监听 currentItemChanged
        self._authSegment.currentItemChanged.connect(
            lambda key: self._authStack.setCurrentIndex(
                {"webview2": 0, "extract": 1, "import": 2, "update": 3}.get(key, 0)
            )
        )
        self._authSegment.setCurrentItem("webview2")
        self._authStack.setCurrentIndex(0)

        # 初始化 WebView2 账号列表
        self._webview2_account_ids: list[str] = []
        self._reload_webview2_account_combo()

        self.viewLayout.addWidget(self.retryWidget)
        self.retryWidget.hide()

        # ========== 网络诊断面板 ==========
        self.networkDiagWidget = QWidget(self)
        net_layout = QVBoxLayout(self.networkDiagWidget)
        net_layout.setContentsMargins(0, 8, 0, 0)
        net_layout.setSpacing(8)
        self._netDiagLabel = CaptionLabel(
            self.tr("无法正常访问 YouTube，可能的原因：\n")
            + self.tr("• 未配置或未启动代理软件\n")
            + self.tr("• 代理节点被 YouTube 封锁 / 限流\n")
            + self.tr("• DNS 被污染或 SSL 证书被干扰"),
            self.networkDiagWidget,
        )
        net_layout.addWidget(self._netDiagLabel)
        net_btn_row = QWidget(self.networkDiagWidget)
        net_btn_h = QHBoxLayout(net_btn_row)
        net_btn_h.setContentsMargins(0, 0, 0, 0)
        net_btn_h.setSpacing(8)
        self._openProxyBtn = PushButton(self.tr("打开代理设置"), net_btn_row)
        self._openProxyBtn.clicked.connect(self._on_open_proxy_settings)
        self._probeBtn = PrimaryPushButton(self.tr("检测网络连通性"), net_btn_row)
        self._probeBtn.clicked.connect(self._on_probe_connectivity)
        net_btn_h.addWidget(self._openProxyBtn)
        net_btn_h.addWidget(self._probeBtn)
        net_btn_h.addStretch(1)
        net_layout.addWidget(net_btn_row)
        self._netProbeResult = CaptionLabel("", self.networkDiagWidget)
        net_layout.addWidget(self._netProbeResult)
        self.viewLayout.addWidget(self.networkDiagWidget)
        self.networkDiagWidget.hide()

        self._error_label: CaptionLabel | None = None
        self.video_formats: list[dict[str, Any]] = []
        self._current_options: YoutubeServiceOptions | None = None

        self._is_closing = False
        self.worker: InfoExtractWorker | VRInfoExtractWorker | None = None
        self._detail_worker: EntryDetailWorker | None = None

        self.type_combo: ComboBox | None = None
        self.preset_combo: ComboBox | None = None
        self.selector_widget: (
            VideoFormatSelectorWidget
            | VRFormatSelectorWidget
            | SubtitleSelectorWidget
            | CoverSelectorWidget
            | None
        ) = None

        self._playlist_sub_override: PlaylistSubtitleOverride | None = None
        self._playlist_format_override = None

        # 绑定全局组件更新信号
        from ....core.dependency_manager import dependency_manager

        dependency_manager.check_finished.connect(self._on_dep_check_finished)
        dependency_manager.install_finished.connect(self._on_dep_install_finished)
        dependency_manager.check_error.connect(self._on_dep_error)
        dependency_manager.download_error.connect(self._on_dep_error)

        # 启动解析
        self.start_extraction()

    def _ensure_download_dir_bar(self) -> None:
        wrap = QWidget(self.contentWidget)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        label = CaptionLabel(self.tr("下载位置"), wrap)
        edit = LineEdit(wrap)
        edit.setText(self._download_dir)
        try:
            edit.setClearButtonEnabled(True)
        except Exception:
            pass

        def _on_text_changed(text: str) -> None:
            self._download_dir = str(text or "").strip()

        edit.textChanged.connect(_on_text_changed)

        pick_btn = PushButton(self.tr("选择..."), wrap)
        pick_btn.clicked.connect(self._on_pick_download_dir)

        row.addWidget(label)
        row.addWidget(edit, 1)
        row.addWidget(pick_btn)

        self._download_dir_edit = edit
        self.contentLayout.addWidget(wrap)

    def _add_labeled_toggle(
        self,
        layout: QHBoxLayout,
        container: QWidget,
        text: str,
        checked: bool,
    ) -> SwitchButton:
        wrap = QWidget(container)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        label = CaptionLabel(text, wrap)
        toggle = SwitchButton(wrap)
        toggle.setChecked(checked)
        toggle.setOnText("")
        toggle.setOffText("")

        row.addWidget(label)
        row.addWidget(toggle)
        layout.addWidget(wrap)
        return toggle

    def _on_pick_download_dir(self) -> None:
        start_dir = self._download_dir or ""
        folder = QFileDialog.getExistingDirectory(self, self.tr("选择下载目录"), start_dir)
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

    def _on_subtitle_pick_clicked(self):
        """打开字幕精选对话框"""
        if not self.video_info:
            return

        # 获取当前容器格式（从格式选择器读取用户手动设置的容器覆盖）
        container = None
        if isinstance(self.selector_widget, (VideoFormatSelectorWidget, VRFormatSelectorWidget)):
            container = getattr(self.selector_widget, "get_container_override", lambda: None)()

        dialog = SubtitlePickerDialog(
            self.video_info,
            container,
            initial_result=getattr(self, "_subtitle_pick_result", None),
            parent=self,
        )

        if dialog.exec():
            result = dialog.get_result()
            self._subtitle_pick_result = result
            # 更新按钮文本以反映选择状态
            n = len(result.selected_tracks)
            if n > 0:
                self.subtitle_pick_btn.setText(f"已选 {n} 种字幕 ✓")
            else:
                self.subtitle_pick_btn.setText(self.tr("选择字幕…"))

    def _on_playlist_subtitle_config_clicked(self):
        """打开播放列表字幕设置对话框"""
        dialog = PlaylistSubtitleConfigDialog(self._playlist_sub_override, self)
        if dialog.exec():
            self._playlist_sub_override = dialog.get_override()
            langs = self._playlist_sub_override.target_languages
            if not langs:
                self.playlist_subtitle_pick_btn.setText(self.tr("未选择语言"))
            else:
                self.playlist_subtitle_pick_btn.setText(f"已选 {len(langs)} 种语言 ✓")

    def _build_single_option_switches(self, platform: str = "youtube") -> QWidget:
        from ....core.config_manager import config_manager

        container = QWidget(self.contentWidget)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        title = CaptionLabel(self.tr("下载选项"), container)
        layout.addWidget(title)

        is_twitter = platform == "twitter"

        sub_enabled = config_manager.get_subtitle_config().enabled
        self.subtitle_check = self._add_labeled_toggle(
            layout, container, self.tr("下载字幕"), sub_enabled
        )

        # 新增：字幕精选按钮（仅在字幕开关打开时可用）
        self.subtitle_pick_btn = PushButton(self.tr("选择字幕…"), container)
        self.subtitle_pick_btn.setEnabled(sub_enabled)
        self.subtitle_pick_btn.clicked.connect(self._on_subtitle_pick_clicked)
        layout.addWidget(self.subtitle_pick_btn)

        if is_twitter:
            self.subtitle_check.hide()
            self.subtitle_pick_btn.hide()

        # 联动：开关变化时控制按钮可用性
        self.subtitle_check.checkedChanged.connect(
            lambda checked: self.subtitle_pick_btn.setEnabled(checked)
        )

        dl_thumb = bool(config_manager.get("download_thumbnail", False))
        self.cover_check = self._add_labeled_toggle(
            layout, container, self.tr("独立封面"), dl_thumb
        )

        em_thumb = bool(config_manager.get("embed_thumbnail", False))
        self.embed_check = self._add_labeled_toggle(
            layout, container, self.tr("嵌入视频"), em_thumb
        )

        self.cover_check.checkedChanged.connect(
            lambda checked: self.embed_check.setChecked(False) if checked else None
        )
        self.embed_check.checkedChanged.connect(
            lambda checked: self.cover_check.setChecked(False) if checked else None
        )

        meta_enabled = bool(config_manager.get("embed_metadata", True))
        self.metadata_check = self._add_labeled_toggle(
            layout, container, self.tr("下载元数据"), meta_enabled
        )

        layout.addStretch(1)
        return container

    def _build_playlist_option_switches(self) -> QWidget:
        from ....core.config_manager import config_manager

        container = QWidget(self.contentWidget)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        if self._mode == "subtitle":
            title = CaptionLabel(self.tr("字幕下载选项"), container)
            layout.addWidget(title)

            layout.addWidget(CaptionLabel(self.tr("全局下载语言:"), container))
            from qfluentwidgets import ComboBox

            from ....processing.subtitle_manager import COMMON_SUBTITLE_LANGUAGES

            self.playlist_subtitle_lang_combo = ComboBox(container)
            self.playlist_subtitle_lang_combo.addItem(
                self.tr("智能选择 (默认)"), userData="__smart__"
            )
            for code, name in COMMON_SUBTITLE_LANGUAGES:
                from PySide6.QtCore import QCoreApplication

                display_name = QCoreApplication.translate("Subtitle", name)
                self.playlist_subtitle_lang_combo.addItem(display_name, userData=code)

            sub_cfg = config_manager.get_subtitle_config()
            if len(sub_cfg.default_languages) >= 1:
                idx = self.playlist_subtitle_lang_combo.findData(sub_cfg.default_languages[0])
                if idx >= 0:
                    self.playlist_subtitle_lang_combo.setCurrentIndex(idx)

            self.playlist_subtitle_lang_combo.currentIndexChanged.connect(
                lambda: config_manager.set(
                    "subtitle_default_languages",
                    [
                        self.playlist_subtitle_lang_combo.itemData(
                            self.playlist_subtitle_lang_combo.currentIndex()
                        )
                    ]
                    if self.playlist_subtitle_lang_combo.itemData(
                        self.playlist_subtitle_lang_combo.currentIndex()
                    )
                    != "__smart__"
                    else ["zh-Hans", "en"],
                )
            )

            layout.addWidget(self.playlist_subtitle_lang_combo)

            layout.addWidget(CaptionLabel(self.tr("全局字幕格式:"), container))

            self.playlist_subtitle_format_combo = ComboBox(container)
            self.playlist_subtitle_format_combo.addItems(["srt", "ass", "vtt", "lrc", "json3"])

            last_fmt = config_manager.get("subtitle_output_format", "srt")
            if last_fmt in ["srt", "ass", "vtt", "lrc", "json3"]:
                self.playlist_subtitle_format_combo.setCurrentText(last_fmt)

            self.playlist_subtitle_format_combo.currentTextChanged.connect(
                lambda text: config_manager.set("subtitle_output_format", text)
            )

            layout.addWidget(self.playlist_subtitle_format_combo)
            layout.addStretch(1)
            return container

        if self._mode == "cover":
            # 封面解析模式不需要字幕和视频嵌入选项
            layout.addStretch(1)
            return container

        title = CaptionLabel(self.tr("下载选项"), container)
        layout.addWidget(title)

        sub_enabled = config_manager.get_subtitle_config().enabled
        self.playlist_subtitle_check = self._add_labeled_toggle(
            layout, container, self.tr("下载字幕"), sub_enabled
        )

        self.playlist_subtitle_pick_btn = PushButton(self.tr("字幕设置…"), container)
        self.playlist_subtitle_pick_btn.setEnabled(sub_enabled)
        self.playlist_subtitle_pick_btn.clicked.connect(self._on_playlist_subtitle_config_clicked)
        layout.addWidget(self.playlist_subtitle_pick_btn)

        self.playlist_subtitle_check.checkedChanged.connect(
            lambda checked: self.playlist_subtitle_pick_btn.setEnabled(checked)
        )

        dl_thumb = bool(config_manager.get("download_thumbnail", False))
        self.playlist_cover_check = self._add_labeled_toggle(
            layout, container, self.tr("独立封面"), dl_thumb
        )

        em_thumb = bool(config_manager.get("embed_thumbnail", False))
        self.playlist_embed_check = self._add_labeled_toggle(
            layout, container, self.tr("嵌入视频"), em_thumb
        )

        self.playlist_cover_check.checkedChanged.connect(
            lambda checked: self.playlist_embed_check.setChecked(False) if checked else None
        )
        self.playlist_embed_check.checkedChanged.connect(
            lambda checked: self.playlist_cover_check.setChecked(False) if checked else None
        )

        meta_enabled = bool(config_manager.get("embed_metadata", True))
        self.playlist_metadata_check = self._add_labeled_toggle(
            layout, container, self.tr("下载元数据"), meta_enabled
        )

        layout.addStretch(1)
        return container

    # === 窗口逻辑 ===

    def closeEvent(self, event) -> None:
        self._stop_background_parsing()
        self.windowClosed.emit(self)
        super().closeEvent(event)

    def _on_download_clicked(self):
        # 获取任务并发送信号
        try:
            if hasattr(self, "_preflight_warnings"):
                self._preflight_warnings.clear()

            tasks = self.get_selected_tasks()
            if not tasks:
                return

            warnings = getattr(self, "_preflight_warnings", [])
            if warnings:
                # 弹窗询问
                from ...dialogs.quality_report_dialog import QualityReportDialog

                if not QualityReportDialog(warnings, self).exec():
                    return

            self.downloadRequested.emit(tasks)
            self.close()
        except Exception as e:
            logger.exception("_on_download_clicked 异常")
            from qfluentwidgets import InfoBar

            InfoBar.error(
                self.tr("构建下载任务失败"),
                str(e),
                duration=8000,
                parent=self,
            )

    def _apply_dialog_size_for_mode(self) -> None:
        if self._is_playlist:
            w, h = 980, 760
            y_offset = 30
            x_offset = 25
        elif self._vr_mode:
            w, h = 880, 750
            y_offset = 80
            x_offset = 0
        elif self._mode in ("subtitle", "cover"):
            w, h = 760, 520
            y_offset = 30
            x_offset = 0
        else:
            # 单视频窗口已加高到 880，再上移就会顶到屏幕上沿（被 y<0 钳制），
            # 所以这里不做垂直偏移，严格按视觉中心摆放。
            w, h = 760, 880
            y_offset = 0
            x_offset = 0

        target_geo = self._get_target_geometry(w, h, y_offset, x_offset)

        from PySide6.QtCore import QEasingCurve, QPropertyAnimation

        # 动画期间放开尺寸限制
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)

        self.geo_anim = QPropertyAnimation(self, b"geometry")
        self.geo_anim.setDuration(250)
        self.geo_anim.setStartValue(self.geometry())
        self.geo_anim.setEndValue(target_geo)
        self.geo_anim.setEasingCurve(QEasingCurve.Type.InOutQuad)

        def on_anim_finished():
            self.setMinimumWidth(w)
            self.setMaximumWidth(w + 60)

        self.geo_anim.finished.connect(on_anim_finished)
        self.geo_anim.start()

    def _get_target_geometry(self, w: int, h: int, y_offset: int, x_offset: int = 0):
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QGuiApplication
        from PySide6.QtWidgets import QApplication

        main_window = None
        for widget in QApplication.topLevelWidgets():
            if widget.objectName() == "MainWindow" or type(widget).__name__ == "MainWindow":
                main_window = widget
                break

        if main_window:
            geo = main_window.geometry()
            cx = geo.center().x()
            cy = geo.center().y()
        else:
            screen = QGuiApplication.primaryScreen().availableGeometry()
            cx = screen.center().x()
            cy = screen.center().y()

        # 严格计算左上角坐标，并应用独立的水平偏移
        x = cx - w // 2 + x_offset
        # 根据独立的 y_offset 偏移视觉中心
        y = cy - h // 2 - y_offset

        # 防止移出屏幕顶部
        if y < 0:
            y = 0

        return QRect(x, y, w, h)

    def _stop_background_parsing(self) -> None:
        if self._is_closing:
            return
        self._is_closing = True
        try:
            self._build_is_chunking = False
            self._thumb_init_timer.stop()
            self._scroll_throttle_timer.stop()
            self._idle_timer.stop()
            self._thumb_pending.clear()
            if self._scheduler is not None:
                self._scheduler.stop_all()
            if self._extract_manager is not None:
                self._extract_manager.cancel_all()
            for w in self._active_workers:
                if hasattr(w, "cancel"):
                    w.cancel()
                w.quit()
                w.wait(200)
            self._active_workers.clear()
            if self.worker:
                self.worker.cancel()
            if self._detail_worker:
                self._detail_worker.cancel()
        except Exception:
            pass
        # P4: 单例 ImageLoader 需要断开本窗口的信号，避免窗口销毁后回调野指针
        try:
            self.image_loader.loaded.disconnect(self._on_thumb_loaded)
            self.image_loader.loaded_with_url.disconnect(self._on_thumb_loaded_with_url)
            self.image_loader.failed.disconnect(self._on_thumb_failed)
        except Exception:
            pass

        try:
            from ....core.dependency_manager import dependency_manager

            dependency_manager.check_finished.disconnect(self._on_dep_check_finished)
            dependency_manager.install_finished.disconnect(self._on_dep_install_finished)
            dependency_manager.check_error.disconnect(self._on_dep_error)
            dependency_manager.download_error.disconnect(self._on_dep_error)
        except Exception:
            pass

    def _switch_to_state(
        self, state: WindowState, title: str = "", show_ring: bool = False
    ) -> None:
        """Central state machine for the main panel visibility."""
        # 记录当前状态：窗口尚未 show() 时子控件的 isVisible() 一律为 False，
        # 靠它判断「还在加载页吗」会误判，所以这里留一份权威值。
        self._window_state = state
        self.loadingWidget.setVisible(state == WindowState.LOADING)
        self.contentWidget.setVisible(state == WindowState.CONTENT)
        self.retryWidget.setVisible(state == WindowState.ERROR_COOKIE)
        self.networkDiagWidget.setVisible(state == WindowState.ERROR_NETWORK)

        # Generic error uses viewLayout directly, but we hide others
        if state in (
            WindowState.LOADING,
            WindowState.ERROR_COOKIE,
            WindowState.ERROR_NETWORK,
            WindowState.ERROR_GENERIC,
        ):
            self.titleLabel.show() if state != WindowState.LOADING else self.titleLabel.hide()

        if state == WindowState.LOADING:
            self.loadingTitleLabel.setText(title)
            self.loadingRing.setVisible(show_ring)

    def start_extraction(self) -> None:
        self._is_closing = False
        self.video_info = None
        self.video_info_dto = None
        self._t_extract_start = time.perf_counter()
        self._first_paint_logged = False
        try:
            if self.worker:
                self.worker.cancel()
        except Exception:
            pass

        self._switch_to_state(
            WindowState.LOADING,
            self.tr("正在使用 VR 模式解析...") if self._vr_mode else self.tr("正在解析链接..."),
            show_ring=True,
        )
        self._show_parse_preview()
        self._current_options = None

        self._is_channel = UrlValidator.is_channel_url(self.url)

        if self._is_channel:
            from ....download.workers import ChannelExtractWorker

            target_tabs = (
                ["videos", "shorts", "streams"] if self._target_tab == "all" else [self._target_tab]
            )
            w = ChannelExtractWorker(self.url, target_tabs, self._current_options)
            w.progress.connect(self._on_channel_progress)
            w.finished_all.connect(self.on_channel_parse_success)
            w.error.connect(self.on_parse_error)
            self.worker = w
            w.start()
        elif self._vr_mode:
            w = VRInfoExtractWorker(self.url)
            w.finished.connect(self.on_parse_success)
            w.error.connect(self.on_parse_error)
            self.worker = w
            w.start()
        else:
            w = InfoExtractWorker(
                self.url,
                self._current_options,
                playlist_flat=self._playlist_flat,
                # 封面模式不读缓存：选中的 thumbnails[].url 会直接变成下载任务的 URL
                read_cache=self._mode != "cover",
            )
            w.finished.connect(self.on_parse_success)
            w.error.connect(self.on_parse_error)
            self.worker = w
            w.start()

    def _show_parse_preview(self) -> None:
        """渲染加载页预览条（标题 + 缩略图），让加载页在 yt-dlp 返回前就有内容。

        两个来源，按信息量从高到低：
        1. preloaded_info —— 智能模式切换（普通 ⇄ VR）带过来的上一轮解析结果，
           标题和缩略图都是真的。
        2. 仅凭 URL 推出的 YouTube 缩略图 —— 覆盖「粘贴链接直接解析」这个最常见
           的场景。标题此时还不知道，只铺图。

        缩略图走全局 image_loader（异步 QNetworkAccessManager，不占 UI 线程），
        与正文那张共用缓存，命中时几乎瞬时。
        """
        info = self._preloaded_info
        title = str((info or {}).get("title") or "").strip()
        thumb_url = _infer_entry_thumbnail(info) if info else ""

        if not thumb_url:
            # 播放列表 / 频道 / 非 YouTube 链接取不到 ID，这里自然返回空串。
            thumb_url = UrlValidator.youtube_thumbnail_url(self.url)

        self._preview_thumb_url = ""
        self._preview_show_on_thumb = False
        self.previewWidget.hide()

        if not title and not thumb_url:
            return

        self.previewTitleLabel.setText(title)
        self.previewTitleLabel.setVisible(bool(title))
        self.previewThumb.setVisible(bool(thumb_url))

        if thumb_url:
            self._preview_thumb_url = thumb_url
            try:
                self.image_loader.load(thumb_url, target_size=(160, 90), radius=8)
            except Exception:
                pass

        if title:
            self.previewWidget.show()
        else:
            # 没有标题时整条只有一张图，先别显示：否则会先摆一个 160×90 的空框，
            # 图到了才填上，比什么都不显示更像故障。等图到了再一次性亮出来。
            self._preview_show_on_thumb = True

    def _on_channel_progress(self, msg: str) -> None:
        self.loadingTitleLabel.setText(msg)

    def on_channel_parse_success(self, results: dict) -> None:
        if self._is_closing:
            return

        # Update cache pool
        for tab, cache_data in results.items():
            self._channel_caches[tab] = cache_data

        info_dict = self._build_channel_info()
        if info_dict is None:
            # 一个标签页都没加载成功
            self.on_parse_error(
                {"title": self.tr("解析失败"), "content": self.tr("未能找到任何频道内容。")}
            )
            return

        # Calculate max tab count to update UI
        if all(
            self._channel_caches.get(t, {}).get("status") in ("loaded", "unsupported")
            for t in ["videos", "shorts", "streams"]
        ):
            self._channel_caches["all"]["status"] = "loaded"

        self.on_parse_success(info_dict)

    def _build_channel_info(self) -> dict[str, Any] | None:
        """把 `_channel_caches` 里当前标签页的数据拼成一个 playlist 形态的 info_dict。

        解析完成与纯缓存切换两条路径原本各写了一份拼装逻辑，语义已经漂移
        （前者不管选了哪个标签页都把三个 tab 全串上）。合并成一份，顺带让
        "只看某个标签页"在两条路径上一致。

        排序在这里做，不发 `--playlist-reverse`：后者与 `--lazy-playlist` 语义相冲
        （要倒序就得先拿全），且每切一次排序都要重跑 1~3 次子进程。本地反转是瞬时的，
        reverse 因此也不必进解析缓存的键。反转按 tab 各自进行再串联，与
        `--playlist-reverse` 的逐 tab 语义一致——tab 之间仍是 videos→shorts→streams。

        Returns:
            拼好的 info_dict；一个标签页都没加载成功时返回 None。
        """
        tabs = (
            ["videos", "shorts", "streams"] if self._channel_tab == "all" else [self._channel_tab]
        )

        combined_entries: list[Any] = []
        # 频道级元数据（标题、uploader 等）取**选中范围内**第一个加载成功的 tab。
        # 不能扫全部缓存：只看 shorts 而 shorts 没加载出来时，会拿 videos 的元数据
        # 配一个空 entries 端出去，把"这个标签页没数据"伪装成"这个频道是空的"。
        base_info: dict[str, Any] | None = None
        for tab in tabs:
            cache = self._channel_caches.get(tab, {})
            if cache.get("status") != "loaded" or not cache.get("data"):
                continue
            if base_info is None:
                base_info = cache["data"]
            entries = list(cache["data"].get("entries") or [])
            if self._channel_reverse:
                entries.reverse()
            combined_entries.extend(entries)

        if base_info is None:
            return None

        info_dict = dict(base_info)
        info_dict["entries"] = combined_entries
        info_dict["_type"] = "playlist"
        return info_dict

    def _ask_switch_to_normal(self) -> bool:
        """询问用户是否切换回普通模式"""
        box = MessageBox(
            self.tr("检测到普通视频"),
            self.tr("检测到当前链接似乎是普通视频，但您正在使用 VR 模式解析。\n\n")
            + self.tr("VR 模式可能无法正确获取普通视频的格式，且不支持部分功能。\n")
            + self.tr("建议切换回普通模式。"),
            self,
        )
        box.yesButton.setText(self.tr("切换回普通模式"))
        box.cancelButton.setText(self.tr("保持 VR 模式"))
        return bool(box.exec())

    def on_parse_success(self, info: Any) -> None:
        if self._is_closing:
            return

        info_dict = _normalize_info_payload(info)
        if not info_dict:
            self.on_parse_error(
                {
                    "title": self.tr("解析失败"),
                    "content": self.tr("返回了无法识别的视频信息类型"),
                    "raw_error": f"unexpected payload type: {type(info)!r}",
                }
            )
            return

        # === 智能 VR 检测 ===
        if self._smart_detect:
            from ....core.video_analyzer import check_is_vr_content

            is_vr = check_is_vr_content(info_dict)

            # 1. 普通模式 -> VR 模式
            # 这一轮解析（几秒级子进程）的结果在新窗口里没法当格式来源用，
            # 但标题/缩略图是通用的，一并带过去先渲染首屏。
            if not self._vr_mode and is_vr:
                self.request_vr_switch.emit(self.url, info_dict)
                self.close()
                return

            # 2. VR 模式 -> 普通模式
            elif self._vr_mode and not is_vr:
                if self._ask_switch_to_normal():
                    self.request_normal_switch.emit(self.url, info_dict)
                    self.close()
                    return
        # ===================

        self.video_info = info_dict
        parsed_is_playlist = str(info_dict.get("_type") or "").lower() == "playlist" or bool(
            info_dict.get("entries")
        )
        source_type = (
            "playlist_entry" if parsed_is_playlist else ("vr_single" if self._vr_mode else "single")
        )
        try:
            self.video_info_dto = VideoInfoMapper.from_raw(info_dict, source_type=source_type)
        except Exception:
            self.video_info_dto = None

        # 解析成功
        if self._error_label:
            self._error_label.deleteLater()
            self._error_label = None

        self._clear_content_layout()
        self._is_playlist = str(info_dict.get("_type") or "").lower() == "playlist" or bool(
            info_dict.get("entries")
        )

        # 频道检测：通过 URL 模式判断
        self._is_channel = UrlValidator.is_channel_url(self.url)

        self._apply_dialog_size_for_mode()

        if self._is_playlist:
            self.titleLabel.show()
            self.yesButton.setEnabled(False)
            self.setup_playlist_ui(info_dict)
        else:
            self.titleLabel.hide()
            self.yesButton.setEnabled(True)
            self.setup_content_ui(info_dict)

        self._switch_to_state(WindowState.CONTENT)
        self._log_first_paint()

    def _log_first_paint(self) -> None:
        """[DialogRender] 首屏可见耗时（P0 基线）：start_extraction → CONTENT 状态。"""
        if getattr(self, "_first_paint_logged", False):
            return
        start = getattr(self, "_t_extract_start", None)
        if start is None:
            return
        self._first_paint_logged = True
        mode = "VR" if self._vr_mode else ("Channel" if self._is_channel else "Video")
        if self._is_playlist:
            mode = "Playlist"
        logger.info(
            "[DialogRender] 首屏可见耗时 {:.2f}s mode={} url={}",
            time.perf_counter() - start,
            mode,
            self.url,
        )

    def _clear_content_layout(self) -> None:
        def _clear_layout(layout) -> None:
            while layout.count():
                child = layout.takeAt(0)
                w = child.widget()
                if w:
                    w.deleteLater()
                    continue
                child_layout = child.layout()
                if child_layout:
                    _clear_layout(child_layout)

        _clear_layout(self.contentLayout)

    def _run_cookie_precheck(self) -> None:
        """窗口打开时本地预检 Cookie 状态（零网络消耗）"""
        try:
            from ....auth.auth_service import AuthSourceType, auth_service
            from ....auth.cookie_sentinel import cookie_sentinel

            if auth_service.current_source == AuthSourceType.NONE:
                return  # 未启用验证，不预检

            if not cookie_sentinel.exists:
                self._cookieWarningLabel.setText(
                    self.tr("⚠️ 尚未获取 Cookie — 解析可能因登录要求而失败。")
                    + self.tr("建议先在「设置 > 账户」中获取 Cookie。")
                )
                self._cookieWarningLabel.show()
                return

            info = cookie_sentinel.get_status_info()

            if not info.get("cookie_valid"):
                msg = info.get("cookie_valid_msg", self.tr("Cookie 无效"))
                self._cookieWarningLabel.setText(
                    f"⚠️ {msg}，解析可能失败。建议前往设置页刷新 Cookie。"
                )
                self._cookieWarningLabel.show()
            elif info.get("expiring_soon"):
                earliest = info.get("earliest_expiry")
                if earliest is not None and earliest > 0:
                    mins = int(earliest / 60)
                    self._cookieWarningLabel.setText(
                        f"⏳ Cookie 将在 {mins} 分钟后过期，建议提前刷新。"
                    )
                else:
                    self._cookieWarningLabel.setText(
                        self.tr("⚠️ Cookie 已过期，建议前往设置页刷新。")
                    )
                self._cookieWarningLabel.show()
        except Exception:
            pass  # 预检失败不影响正常流程

    def _cookie_warning_stylesheet(self) -> str:
        """返回主题感知的 Cookie 警告条样式"""
        try:
            from qfluentwidgets import isDarkTheme

            color = "#f0c040" if isDarkTheme() else "#b8860b"
        except Exception:
            color = "#b8860b"
        return (
            f"QLabel {{ background: rgba(255, 193, 7, 0.15); padding: 8px; "
            f"border-radius: 6px; color: {color}; }}"
        )

    def _clear_error_augment_widgets(self) -> None:
        """清理上次 on_parse_error 注入到 retryLayout 的临时提示 label"""
        for attr in ("_alt_label", "_pot_label"):
            w = getattr(self, attr, None)
            if w is not None:
                try:
                    w.setParent(None)
                    w.deleteLater()
                except Exception:
                    pass
                setattr(self, attr, None)

    def on_parse_error(self, err_data: dict) -> None:
        if self._is_closing:
            return
        self._clear_error_augment_widgets()
        self._cookieWarningLabel.setStyleSheet(self._cookie_warning_stylesheet())
        self._cookieWarningLabel.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.loadingWidget.hide()
        self.titleLabel.setText(self.tr("解析失败"))
        self.titleLabel.show()
        if self._error_label:
            self._error_label.deleteLater()

        raw_error = str(err_data.get("raw_error") or "")

        from ....diagnostics import diagnose

        # 上游已经带来结构化诊断时直接复用；否则就地重跑一次引擎。
        if err_data.get("code"):
            category = str(err_data.get("category") or "unknown")
            fix_action = err_data.get("fix_action")
            friendly_title = err_data.get("user_title", "")
            friendly_content = err_data.get("user_message", "")
        else:
            diag = diagnose(1, raw_error)
            category = diag.category
            fix_action = diag.fix_action
            friendly_title = diag.user_title
            friendly_content = diag.user_message

        title = (
            friendly_title if friendly_title else str(err_data.get("title") or self.tr("解析失败"))
        )
        content = friendly_content if friendly_content else str(err_data.get("content") or "")
        suggestion = str(err_data.get("suggestion") or "")
        technical_detail = str(err_data.get("technical_detail") or raw_error)

        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QVBoxLayout
        from qfluentwidgets import BodyLabel, PushButton, SimpleCardWidget, TextEdit

        self._error_container = SimpleCardWidget(self)
        err_layout = QVBoxLayout(self._error_container)
        err_layout.setContentsMargins(16, 16, 16, 16)
        err_layout.setSpacing(12)

        text = f"{title}\n\n{content}"
        if suggestion and not raw_error:
            text += f"\n\n建议操作：\n{suggestion}"

        msg_label = BodyLabel(text, self._error_container)
        msg_label.setWordWrap(True)
        msg_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        err_layout.addWidget(msg_label)

        if technical_detail:
            toggle_btn = PushButton(self.tr("查看技术详情"), self._error_container)
            detail_edit = TextEdit(self._error_container)
            detail_edit.setReadOnly(True)
            detail_edit.setPlainText(technical_detail)

            # Explicitly set Consolas font to avoid Fixedsys crash
            detail_font = QFont("Consolas", 10)
            detail_font.setStyleHint(QFont.StyleHint.Monospace)
            detail_edit.setFont(detail_font)

            detail_edit.setMaximumHeight(150)
            detail_edit.hide()

            def _toggle_detail():
                if detail_edit.isHidden():
                    detail_edit.show()
                    toggle_btn.setText(self.tr("隐藏技术详情"))
                else:
                    detail_edit.hide()
                    toggle_btn.setText(self.tr("查看技术详情"))

            toggle_btn.clicked.connect(_toggle_detail)
            err_layout.addWidget(toggle_btn)
            err_layout.addWidget(detail_edit)

        self._error_label = self._error_container

        # Insert error label right after the titleLabel
        idx = self.viewLayout.indexOf(self.titleLabel)
        self.viewLayout.insertWidget(idx + 1 if idx >= 0 else 1, self._error_label)

        # === 决定显示哪个面板 ===
        # 优先看规则表给出的 fix_action —— 那是规则作者对"该怎么修"的明确指示；
        # 没有 fix_action 时才退回按 category 粗分。
        if fix_action in ("update_component", "refresh_pot"):
            self._switch_to_state(WindowState.ERROR_COOKIE)
            self._authSegment.setCurrentItem("update")
            self.networkDiagWidget.hide()
        elif fix_action in ("extract_cookie", "relogin"):
            self._switch_to_state(WindowState.ERROR_COOKIE)
            self._authSegment.setCurrentItem("webview2")
            self.networkDiagWidget.hide()
        elif fix_action == "switch_proxy" or category == "network":
            self._switch_to_state(WindowState.ERROR_NETWORK)
            self._netProbeResult.setText("")
        elif category == "auth":
            self._switch_to_state(WindowState.ERROR_COOKIE)
            self._authSegment.setCurrentItem("webview2")
            self.networkDiagWidget.hide()
        elif category in ("toolchain", "unknown"):
            self._switch_to_state(WindowState.ERROR_COOKIE)
            self._authSegment.setCurrentItem("update")
            self.networkDiagWidget.hide()
        else:
            # media / filesystem：换 Cookie 或换节点都无济于事，只展示信息
            self._switch_to_state(WindowState.ERROR_GENERIC)
            self.retryWidget.hide()
            self.networkDiagWidget.hide()

        # Build Report Issue button
        if raw_error:
            if not hasattr(self, "reportBtn"):
                from qfluentwidgets import FluentIcon

                self.reportBtn = PushButton(self.tr("反馈此错误"), self)
                self.reportBtn.setIcon(FluentIcon.GITHUB)
                self.reportBtn.clicked.connect(
                    lambda: self._on_report_clicked(friendly_title, raw_error)
                )
                self.viewLayout.addWidget(self.reportBtn, alignment=Qt.AlignmentFlag.AlignLeft)
            else:
                try:
                    self.reportBtn.clicked.disconnect()
                except Exception:
                    pass
                self.reportBtn.clicked.connect(
                    lambda: self._on_report_clicked(friendly_title, raw_error)
                )
                self.reportBtn.show()

    def _on_report_clicked(self, title: str, raw_error: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from ....utils.error_parser import generate_issue_url

        issue_url = generate_issue_url(title, raw_error)
        QDesktopServices.openUrl(QUrl(issue_url))

    def _on_open_proxy_settings(self) -> None:
        """打开代理设置（跳转主窗口设置页）"""

        # 尝试通知主窗口打开设置页
        try:
            parent = self.parent()
            while parent is not None:
                if hasattr(parent, "show_settings_network"):
                    parent.show_settings_network()  # type: ignore[attr-defined]
                    return
                parent = parent.parent()
        except Exception:
            pass
        # 兜底：显示提示
        self._netProbeResult.setText(self.tr("请手动前往主界面「设置 > 网络连接」配置代理。"))

    def _on_probe_connectivity(self) -> None:
        """用户手动点击检测连通性"""
        self._probeBtn.setEnabled(False)
        self._probeBtn.setText(self.tr("检测中..."))
        self._netProbeResult.setText("")

        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _ProbeWorker(QThread):
            finished = QSignal(bool)

            def run(self):
                from ....utils.error_parser import probe_youtube_connectivity

                result = probe_youtube_connectivity(timeout=8.0)
                self.finished.emit(result)

        self._probe_worker = _ProbeWorker(self)

        def _on_done(reachable: bool):
            self._probeBtn.setEnabled(True)
            self._probeBtn.setText(self.tr("检测网络连通性"))
            if reachable:
                self._netProbeResult.setText(
                    self.tr("✅ YouTube 可达 — 网络正常。\n")
                    + self.tr("错误可能是 Cookie 失效导致，请尝试重新获取 Cookie。")
                )
                # 网络通但报了错 → 可能其实是 Cookie 问题，显示重试面板
                self.retryWidget.show()
            else:
                self._netProbeResult.setText(
                    self.tr("❌ 无法连接 YouTube — 请检查代理/VPN 是否正常运行。")
                )

        self._probe_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._probe_worker.start()

    def _retry_parse_with_auth(self) -> None:
        """重试解析（使用 auth_service 管理的 Cookie）"""
        self._is_closing = False
        try:
            if self.worker:
                self.worker.cancel()
        except Exception:
            pass
        if self._error_label:
            self._error_label.deleteLater()
            self._error_label = None
        self.retryWidget.hide()

        # 不传 cookies_from_browser，由 auth_service 的 cookie file 提供
        self._current_options = None

        # 用户显式重试意味着"上一次结果不可接受"，必须绕过 TTL 缓存重新走一遍子进程。
        try:
            from ....youtube.youtube_service import youtube_service

            youtube_service.invalidate_parse_cache("用户手动重试解析")
        except Exception:
            pass

        self._switch_to_state(WindowState.LOADING, self.tr("正在重试解析..."), show_ring=True)

        if self._vr_mode:
            w = VRInfoExtractWorker(self.url)
        else:
            w = InfoExtractWorker(self.url, self._current_options)

        w.finished.connect(self.on_parse_success)
        w.error.connect(self.on_parse_error)
        self.worker = w
        w.start()

    def _on_webview2_retry_clicked(self) -> None:
        """WebView2 登录模式重试"""
        from ....auth.auth_service import AuthSourceType, auth_service
        from ....auth.cookie_sentinel import cookie_sentinel

        self._reload_webview2_account_combo()

        # 先按下拉框切换当前 WebView2 账号
        idx = self._webview2AccountCombo.currentIndex()
        if 0 <= idx < len(self._webview2_account_ids):
            auth_service.set_current_webview2_account(self._webview2_account_ids[idx])

        account = (
            auth_service._webview2_accounts.get(self._webview2_account_ids[idx])
            if (0 <= idx < len(self._webview2_account_ids))
            else None
        )
        platform = account.platform if account else "youtube"
        account_name = account.localized_name if account else self.tr("默认账号")

        self._dleRetryBtn.setEnabled(False)
        self._dleRetryBtn.setText(self.tr("正在启动浏览器..."))
        self._dleStatusLabel.setText(
            self.tr("正在后台提取 {} 登录态，若提取失败将自动显示登录窗口...").format(account_name)
        )

        # 切换到 WebView2 模式
        auth_service.set_source(AuthSourceType.WEBVIEW2, auto_refresh=False)

        # 在后台线程执行 WebView2 登录
        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _DLEWorker(QThread):
            finished = QSignal(bool, str)

            def run(self):
                try:
                    success, msg = cookie_sentinel.force_refresh_with_uac(platform=platform)
                    self.finished.emit(success, msg)
                except Exception as e:
                    self.finished.emit(False, str(e))

        self._webview2_worker = _DLEWorker(self)

        def _on_done(success: bool, msg: str):
            self._dleRetryBtn.setEnabled(True)
            self._dleRetryBtn.setText(self.tr("登录 YouTube 并重试"))
            if success:
                try:
                    from ....auth.cookie_sentinel import cookie_sentinel

                    cur_acc = auth_service.get_current_webview2_account(platform=platform)
                    acc_cookie = cur_acc.cached_cookie_path if cur_acc else self.tr("未知")
                    self._dleStatusLabel.setText(
                        self.tr(
                            "✅ {} 登录成功，正在重新解析...\n账号文件: {}\n统一文件: {}"
                        ).format(account_name, acc_cookie, cookie_sentinel.cookie_path)
                    )
                except Exception:
                    self._dleStatusLabel.setText(
                        self.tr("✅ {} 登录成功，正在重新解析...").format(account_name)
                    )
                self._retry_parse_with_auth()
            else:
                clean = msg
                if clean.startswith(self.tr("刷新异常: ")):
                    clean = clean[len(self.tr("刷新异常: ")) :]
                self._dleStatusLabel.setText(f"❌ {clean}")

        self._webview2_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._webview2_worker.start()

    def _reload_webview2_account_combo(self) -> None:
        """刷新 WebView2 账号下拉列表"""
        try:
            from ....auth.auth_service import auth_service

            accounts = auth_service.list_webview2_accounts(platform=None)
            self._webview2_account_ids = [a.account_id for a in accounts]

            self._webview2AccountCombo.blockSignals(True)
            self._webview2AccountCombo.clear()
            for acc in accounts:
                plat_name = (
                    "YouTube"
                    if acc.platform == "youtube"
                    else ("X" if acc.platform == "twitter" else acc.platform.capitalize())
                )
                label = f"[{plat_name}] {acc.localized_name}"
                if acc.is_default:
                    label += self.tr(" (默认)")
                self._webview2AccountCombo.addItem(label)

            # 优先选中 YouTube 的当前账号，或者根据当前解析的链接选择平台
            # 这里简单起见默认选中 YouTube 的
            cur = auth_service.get_current_webview2_account_id("youtube")
            if cur in self._webview2_account_ids:
                self._webview2AccountCombo.setCurrentIndex(self._webview2_account_ids.index(cur))
            elif self._webview2_account_ids:
                self._webview2AccountCombo.setCurrentIndex(0)
            self._webview2AccountCombo.blockSignals(False)
        except Exception:
            pass

    def _on_extract_retry_clicked(self) -> None:
        """浏览器提取模式重试"""
        from ....auth.auth_service import AuthSourceType, auth_service
        from ....auth.cookie_sentinel import cookie_sentinel

        idx = self._extractCombo.currentIndex()
        source_map = [
            AuthSourceType.EDGE,
            AuthSourceType.CHROME,
            AuthSourceType.FIREFOX,
            AuthSourceType.CHROMIUM,
            AuthSourceType.BRAVE,
            AuthSourceType.OPERA,
            AuthSourceType.OPERA_GX,
            AuthSourceType.VIVALDI,
            AuthSourceType.LIBREWOLF,
            AuthSourceType.CENT,
        ]
        source = source_map[idx] if 0 <= idx < len(source_map) else AuthSourceType.EDGE
        browser_name = self._extractCombo.currentText()

        self._extractRetryBtn.setEnabled(False)
        self._extractRetryBtn.setText(f"正在从 {browser_name} 提取...")

        auth_service.set_source(source, auto_refresh=True)

        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _ExtractWorker(QThread):
            finished = QSignal(bool, str)

            def run(self):
                try:
                    success, msg = cookie_sentinel.force_refresh_with_uac()
                    self.finished.emit(success, msg)
                except Exception as e:
                    self.finished.emit(False, str(e))

        self._extract_worker = _ExtractWorker(self)

        def _on_done(success: bool, msg: str):
            self._extractRetryBtn.setEnabled(True)
            self._extractRetryBtn.setText(self.tr("提取并重试"))
            if success:
                self._retry_parse_with_auth()
            else:
                from qfluentwidgets import InfoBar

                InfoBar.error(
                    f"{browser_name} 提取失败",
                    msg,
                    duration=8000,
                    parent=self,
                )

        self._extract_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._extract_worker.start()

    def _on_import_retry_clicked(self) -> None:
        """手动导入模式重试"""
        from ....auth.auth_service import AuthSourceType, auth_service

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("选择 Cookie 文件"),
            "",
            self.tr("Cookie 文件 (*.txt);;所有文件 (*)"),
        )
        if not file_path:
            return

        # 设置为文件模式
        auth_service.set_source(AuthSourceType.FILE, file_path=file_path, auto_refresh=False)

    def _on_update_retry_clicked(self) -> None:
        """一键检测并更新 yt-dlp"""
        self._updateRetryBtn.setEnabled(False)
        self._updateRing.show()
        self._updateStatusLabel.setText(self.tr("正在检查更新..."))

        from ....core.dependency_manager import dependency_manager

        dependency_manager.check_update("yt-dlp")

    def _on_dep_check_finished(self, component: str, data: dict) -> None:
        if component != "yt-dlp" or getattr(self, "_is_closing", True):
            return
        # check update_available
        if data.get("update_available", False) or data.get("local_version") == self.tr("未安装"):
            self._updateStatusLabel.setText(self.tr("发现新版本，正在后台下载安装..."))
            from ....core.dependency_manager import dependency_manager

            dependency_manager.install_component("yt-dlp")
        else:
            self._updateRetryBtn.setEnabled(True)
            self._updateRing.hide()
            if data.get("latest") == "unknown":
                self._updateStatusLabel.setText(
                    self.tr("❌ 检查更新失败，请检查网络连接或更换组件更新源。")
                )
            else:
                self._updateStatusLabel.setText(
                    self.tr("✅ 当前已是最新版本或配置未变更，建议尝试更换代理节点。")
                )

    def _on_dep_install_finished(self, component: str) -> None:
        if component != "yt-dlp" or getattr(self, "_is_closing", True):
            return
        self._updateRetryBtn.setEnabled(True)
        self._updateRing.hide()
        self._updateStatusLabel.setText(self.tr("✅ 组件更新完成！正在自动重试..."))
        QTimer.singleShot(1000, self._retry_parse_with_auth)

    def _on_dep_error(self, component: str, msg: str) -> None:
        if component != "yt-dlp" or getattr(self, "_is_closing", True):
            return
        self._updateRetryBtn.setEnabled(True)
        self._updateRing.hide()
        self._updateStatusLabel.setText(f"❌ 更新异常: {msg}")

        from ....core.config_manager import config_manager

        config_manager.set("cookie_file", "")

        self._retry_parse_with_auth()

    # === 单视频 UI ===

    def setup_content_ui(self, info: dict[str, Any]) -> None:
        # 1. Top Info Card
        from PySide6.QtWidgets import QSizePolicy

        top_card = CardWidget(self.contentWidget)
        top_card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        top_h = QHBoxLayout(top_card)
        top_h.setContentsMargins(16, 16, 16, 16)
        top_h.setSpacing(16)

        # Thumb
        self.thumb_label = ImageLabel(top_card)
        self.thumb_label.setImage(str(resource_path("assets", "logo.png")))
        self.thumb_label.setFixedSize(160, 90)
        self.thumb_label.setScaledContents(True)
        self.thumb_label.setBorderRadius(8, 8, 8, 8)

        thumb_url = _infer_entry_thumbnail(info)
        if thumb_url:
            self.image_loader.load(thumb_url, target_size=(160, 90), radius=8)

        # Info
        info_v = QVBoxLayout()
        title = str(info.get("title") or "Unknown Title")
        uploader = str(info.get("uploader") or info.get("uploader_id") or "Unknown Uploader")
        duration = _format_duration(info.get("duration"))
        view_count = f"{int(info.get('view_count') or 0):,} 次观看"
        upload_date = _format_upload_date(info.get("upload_date"))

        title_lbl = SubtitleLabel(title, top_card)
        title_lbl.setWordWrap(True)

        meta_lbl = CaptionLabel(f"{uploader} • {duration}\n{upload_date} • {view_count}", top_card)
        meta_lbl.setTextColor(QColor(96, 96, 96), QColor(208, 208, 208))

        info_v.addWidget(title_lbl)
        info_v.addWidget(meta_lbl)

        if self.video_info_dto and 0 < self.video_info_dto.max_video_height <= 720:
            warn_lbl = CaptionLabel(
                f"⚠️ 警告: 该视频受限，最高仅支持 {self.video_info_dto.max_video_height}p 提取",
                top_card,
            )
            warn_lbl.setStyleSheet(
                "QLabel { color: #b8860b; font-weight: bold; background: rgba(255, 193, 7, 0.15); padding: 4px 8px; border-radius: 4px; }"
            )
            info_v.addWidget(warn_lbl)

        info_v.addStretch(1)

        top_h.addWidget(self.thumb_label)
        top_h.addLayout(info_v, 1)

        self.contentLayout.addWidget(top_card)

        # 2. Mode Specific Selector
        if self._mode == "subtitle":
            self.setup_subtitle_mode_ui(info)
        elif self._mode == "cover":
            self.setup_cover_mode_ui(info)
        elif self._vr_mode:
            self.setup_vr_mode_ui(info)
        else:
            self.setup_default_mode_ui(info)

        if self._mode not in ("subtitle", "cover"):
            self._ensure_download_dir_bar()

    def setup_default_mode_ui(self, info: dict[str, Any]) -> None:
        from fluentytdl.ui.components.platforms.youtube import VideoFormatSelectorWidget

        from ....utils.url_router import url_router

        platform = url_router.detect_platform(self.url) if self.url else "youtube"
        self.selector_widget = VideoFormatSelectorWidget(info, self.contentWidget)

        self.contentLayout.addWidget(self.selector_widget)

        self.options_container = self._build_single_option_switches(platform=platform)
        self.contentLayout.addWidget(self.options_container)

        # Sections require a known finite duration and are intentionally scoped
        # to normal YouTube videos in this first release.
        duration = float(info.get("duration") or 0.0)
        if platform == "youtube" and duration > 0 and not info.get("is_live"):
            self._section_selector = SectionRangeSelector(duration, self.contentWidget)
            self._section_selector.enabledChanged.connect(self._on_section_enabled_changed)
            self._section_selector.selectionChanged.connect(self._on_section_selection_changed)
            self.contentLayout.addWidget(self._section_selector)

    def _on_section_selection_changed(self) -> None:
        """Do not route a single-video control through playlist button logic."""
        selector = self._section_selector
        if not self._is_playlist and selector is not None:
            self.yesButton.setEnabled(selector.is_valid())

    def _on_section_enabled_changed(self, enabled: bool) -> None:
        """Keep subtitles mutually exclusive with v1 clip downloads."""
        if not hasattr(self, "subtitle_check") or not hasattr(self, "subtitle_pick_btn"):
            return
        if enabled:
            self._subtitle_state_before_section = (
                self.subtitle_check.isChecked(),
                self.subtitle_pick_btn.isEnabled(),
            )
            self.subtitle_check.setChecked(False)
            self.subtitle_check.setEnabled(False)
            self.subtitle_pick_btn.setEnabled(False)
            self.subtitle_pick_btn.setToolTip(self.tr("视频裁切暂不支持字幕"))
        else:
            was_enabled, _ = self._subtitle_state_before_section or (False, False)
            self.subtitle_check.setEnabled(True)
            self.subtitle_check.setChecked(was_enabled)
            self.subtitle_pick_btn.setEnabled(was_enabled)
            self.subtitle_pick_btn.setToolTip("")
            self._subtitle_state_before_section = None

    def setup_vr_mode_ui(self, info: dict[str, Any]) -> None:
        self.selector_widget = VRFormatSelectorWidget(info, self.contentWidget)

        self.contentLayout.addWidget(self.selector_widget)
        self.options_container = self._build_single_option_switches()
        self.contentLayout.addWidget(self.options_container)

    def setup_subtitle_mode_ui(self, info: dict[str, Any]) -> None:
        self.yesButton.setText(self.tr("下载字幕"))
        self.selector_widget = SubtitleSelectorWidget(info, self.contentWidget)
        self.selector_widget.embedCheck.setChecked(False)
        self.selector_widget.embedCheck.hide()
        self.contentLayout.addWidget(self.selector_widget)

    def setup_cover_mode_ui(self, info: dict[str, Any]) -> None:
        self.yesButton.setText(self.tr("下载封面"))
        self.selector_widget = CoverSelectorWidget(info, self.contentWidget)
        self.contentLayout.addWidget(self.selector_widget)

    # === 播放列表 UI ===

    def setup_playlist_ui(self, info: dict[str, Any]) -> None:
        title = str(info.get("title") or self.tr("播放列表"))
        count = 0
        entries = info.get("entries") or []
        if isinstance(entries, list):
            count = len(entries)

        if self._is_channel:
            self.titleLabel.setText(self.tr("频道：{}（{} 条）").format(title, count))
        else:
            self.titleLabel.setText(self.tr("播放列表：{}（{} 条）").format(title, count))
        self.titleLabel.show()

        # 频道专属控件：标签页切换 + 排序
        if self._is_channel:
            channel_controls = QHBoxLayout()
            channel_controls.setContentsMargins(0, 0, 0, 0)
            channel_controls.setSpacing(12)

            tab_label = CaptionLabel(self.tr("内容类型"), self.contentWidget)
            channel_controls.addWidget(tab_label)

            self._channel_tab_combo = ComboBox(self.contentWidget)
            self._channel_tab_combo_mapper = []

            # 动态生成 ComboBox 选项
            for tab, name in [
                ("all", self.tr("全部")),
                ("videos", self.tr("常规视频")),
                ("shorts", "Shorts"),
                ("streams", self.tr("直播回放")),
            ]:
                cache = self._channel_caches.get(tab, {})
                if cache.get("status") != "unsupported":
                    self._channel_tab_combo.addItem(name, userData=tab)
                    self._channel_tab_combo_mapper.append(tab)

            # 设置当前选中
            if self._channel_tab in self._channel_tab_combo_mapper:
                idx = self._channel_tab_combo_mapper.index(self._channel_tab)
                self._channel_tab_combo.setCurrentIndex(idx)
            self._channel_tab_combo.currentIndexChanged.connect(self._on_channel_tab_changed)
            channel_controls.addWidget(self._channel_tab_combo)

            sort_label = CaptionLabel(self.tr("排序"), self.contentWidget)
            channel_controls.addWidget(sort_label)

            self._channel_sort_combo = ComboBox(self.contentWidget)
            self._channel_sort_combo.addItems([self.tr("最新在前"), self.tr("最旧在前")])
            if self._channel_reverse:
                self._channel_sort_combo.setCurrentIndex(1)
            self._channel_sort_combo.currentIndexChanged.connect(self._on_channel_sort_changed)
            channel_controls.addWidget(self._channel_sort_combo)

            channel_controls.addStretch(1)
            self.contentLayout.addLayout(channel_controls)

        # header row (progress)
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)

        self.progressRing = IndeterminateProgressRing(self.contentWidget)
        self.progressRing.setFixedSize(16, 16)
        self.progressRing.hide()

        self.progressLabel = CaptionLabel(self.tr("详情补全：0/0"), self.contentWidget)
        header_row.addStretch(1)
        header_row.addWidget(self.progressRing)
        header_row.addWidget(self.progressLabel)
        self.contentLayout.addLayout(header_row)

        # batch actions row
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.setSpacing(12)

        self.selectAllBtn = PushButton(self.tr("全选"), self.contentWidget)
        self.unselectAllBtn = PushButton(self.tr("取消"), self.contentWidget)
        self.invertSelectBtn = PushButton(self.tr("反选"), self.contentWidget)

        self.applyPresetBtn = PrimaryPushButton(self.tr("重新套用预设"), self.contentWidget)

        self.type_combo = ComboBox(self.contentWidget)
        # 0=音视频，1=仅视频，2=仅音频
        self.type_combo.addItems([self.tr("音视频"), self.tr("仅视频"), self.tr("仅音频")])
        self.type_combo.currentIndexChanged.connect(self._on_playlist_type_changed)

        self.preset_combo = ComboBox(self.contentWidget)
        if self._vr_mode:
            # VR 模式使用场景化预设
            for pid, title, _, _, _ in VR_PRESETS:
                self.preset_combo.addItem(title, userData=pid)
        else:
            self.preset_combo.addItems(
                [
                    self.tr("最高质量(自动)"),
                    self.tr("2160p(严格)"),
                    self.tr("1440p(严格)"),
                    self.tr("1080p(严格)"),
                    self.tr("720p(严格)"),
                    self.tr("480p(严格)"),
                    self.tr("360p(严格)"),
                ]
            )
        self.preset_combo.currentIndexChanged.connect(self._on_playlist_preset_changed)

        self.globalFormatBtn = PushButton(self.tr("格式设置…"), self.contentWidget)
        self.globalFormatBtn.clicked.connect(self._on_global_format_clicked)

        # Add widgets to toolbar in a single row
        toolbar.addWidget(self.selectAllBtn)
        toolbar.addWidget(self.unselectAllBtn)
        toolbar.addWidget(self.invertSelectBtn)

        toolbar.addSpacing(16)

        if self._vr_mode:
            toolbar.addWidget(self.type_combo)
            toolbar.addWidget(self.preset_combo)
            toolbar.addWidget(self.applyPresetBtn)
            self.globalFormatBtn.hide()
        else:
            toolbar.addWidget(self.globalFormatBtn)
            self.type_combo.hide()
            self.preset_combo.hide()
            self.applyPresetBtn.hide()

            # Initial default global format for non-VR playlists
            if self._is_playlist and self._mode not in ("subtitle", "cover"):
                from ....models.playlist_format import PlaylistGlobalFormatOverride

                self._playlist_format_override = PlaylistGlobalFormatOverride(
                    download_type="video_audio",
                    preset_id="best_raw",
                    preset_intent={"type": "video_audio", "max_height": None, "prefer_ext": None},
                )

        if self._mode in ("subtitle", "cover"):
            self.type_combo.hide()
            self.preset_combo.hide()
            self.globalFormatBtn.hide()
            self.applyPresetBtn.hide()

        toolbar.addStretch(1)

        # 并发控制
        self.concurrency_label = CaptionLabel(self.tr("解析并发:"), self.contentWidget)
        self.concurrency_combo = ComboBox(self.contentWidget)
        self.concurrency_combo.addItems(["1", "2", "3", "5", "8", "12", "16"])

        from ....core.config_manager import config_manager

        curr_val = int(config_manager.get("playlist_extract_concurrency", 2))
        try:
            idx = ["1", "2", "3", "5", "8", "12", "16"].index(str(curr_val))
            self.concurrency_combo.setCurrentIndex(idx)
        except ValueError:
            self.concurrency_combo.setCurrentIndex(1)  # fallback to 2

        self.concurrency_combo.currentIndexChanged.connect(self._on_concurrency_changed)

        toolbar.addWidget(self.concurrency_label)
        toolbar.addWidget(self.concurrency_combo)

        self.contentLayout.addLayout(toolbar)

        list_view = QListView(self.contentWidget)
        list_view.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        list_view.setMouseTracking(False)
        list_view.setUniformItemSizes(True)
        list_view.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        list_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        list_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        list_view.viewport().setAutoFillBackground(True)
        list_view.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        list_view.setStyleSheet(
            "QListView { border: none; background: transparent; outline: none; }"
        )

        playlist_model = PlaylistListModel(list_view)
        playlist_delegate = PlaylistItemDelegate(list_view)
        list_view.setModel(playlist_model)
        list_view.setItemDelegate(playlist_delegate)
        list_view.verticalScrollBar().valueChanged.connect(self._on_scroll_value_changed)
        list_view.clicked.connect(self._on_list_item_clicked)

        self._list_view = list_view
        self._playlist_model = playlist_model
        self._playlist_delegate = playlist_delegate
        from ....core.config_manager import config_manager

        concurrency = int(config_manager.get("playlist_extract_concurrency", 2))
        self._extract_manager = AsyncExtractManager(max_concurrent=concurrency, parent=self)

        self.contentLayout.addWidget(list_view)

        self.playlist_options_container = self._build_playlist_option_switches()
        self.contentLayout.addWidget(self.playlist_options_container)

        # wire actions
        self.selectAllBtn.clicked.connect(self._select_all)
        self.unselectAllBtn.clicked.connect(self._unselect_all)
        self.invertSelectBtn.clicked.connect(self._invert_select)
        self.applyPresetBtn.clicked.connect(self._apply_preset_to_selected)

        # fill rows in chunks
        self._build_playlist_rows(info)

    def _build_playlist_rows(self, info: dict[str, Any]) -> None:
        entries = info.get("entries") or []
        if not isinstance(entries, list):
            entries = []

        self._playlist_rows = []
        self._thumb_url_to_rows = {}
        self._thumb_applied_rows = {}
        self._thumb_requested = set()
        self._thumb_pending.clear()
        self._thumb_inflight = 0
        self._thumb_retry_count = {}
        self._action_widget_by_row = {}

        model = self._playlist_model
        if model is None:
            return

        model.clear()
        self._build_chunk_entries = entries
        self._build_chunk_offset = 0
        self._build_is_chunking = True
        self._process_next_build_chunk()

    def _process_next_build_chunk(self) -> None:
        if self._is_closing or not self._build_is_chunking:
            return

        model = self._playlist_model
        if model is None:
            return

        entries = self._build_chunk_entries
        offset = self._build_chunk_offset
        end = min(offset + self._build_chunk_size, len(entries))

        tasks: list[VideoTask] = []
        for row in range(offset, end):
            entry = entries[row]
            if not isinstance(entry, dict):
                entry = {}

            url = _infer_entry_url(entry)
            title = str(entry.get("title") or "-")
            uploader = str(
                entry.get("uploader") or entry.get("channel") or entry.get("uploader_id") or "-"
            )
            duration = _format_duration(entry.get("duration"))
            upload_date = _format_upload_date(entry.get("upload_date"))
            playlist_index = str(entry.get("playlist_index") or (row + 1))
            vid = str(entry.get("id") or "-")
            thumb = _infer_entry_thumbnail(entry)

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
                    "status": self.tr("未选择"),
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
                    "custom_selection_data": None,
                    "custom_summary": None,
                }
            )

            if thumb:
                self._thumb_url_to_rows.setdefault(thumb, set()).add(row)

            task = VideoTask(url=url)
            task.id = vid
            task.title = title
            task.uploader = uploader
            task.duration_str = duration
            task.upload_date = upload_date
            task.thumbnail_url = thumb
            task.thumbnail.status = "idle"
            task.is_parsing = False
            tasks.append(task)

            self._action_widget_by_row[row] = _PlaylistModelRowProxy(
                row, model, self._update_playlist_row_view
            )

        model.addTasks(tasks)
        self._build_chunk_offset = end

        if end < len(entries):
            QTimer.singleShot(0, self._process_next_build_chunk)
        else:
            self._build_is_chunking = False
            self._build_chunk_entries = []
            self._on_build_chunks_complete()

    def _on_build_chunks_complete(self) -> None:
        if self._is_closing:
            return

        self._refresh_progress_label()
        self._update_download_btn_state()
        self._setup_scheduler()
        self._thumb_init_timer.start()
        self._ensure_download_dir_bar()
        QTimer.singleShot(50, self._initial_viewport_scan)
        QTimer.singleShot(200, lambda: self._scheduler.start_crawl() if self._scheduler else None)

    def _setup_scheduler(self) -> None:
        """创建 PlaylistScheduler，接管所有详情抓取调度逻辑。"""
        mgr = self._extract_manager
        if mgr is None:
            return

        def _get_url(row: int) -> str | None:
            if 0 <= row < len(self._playlist_rows):
                return str(self._playlist_rows[row].get("url") or "").strip() or None
            return None

        from ....core.config_manager import config_manager

        concurrency = int(config_manager.get("playlist_extract_concurrency", 3))
        scheduler = PlaylistScheduler(
            extract_manager=mgr,
            get_row_url=_get_url,
            total_rows=lambda: len(self._playlist_rows),
            options=self._current_options,
            vr_mode=self._vr_mode,
            exec_limit=concurrency,
            parent=self,
            read_cache=self._mode != "cover",
        )
        scheduler.detail_finished.connect(self._on_scheduler_detail_finished)
        scheduler.detail_error.connect(self._on_scheduler_detail_error)
        scheduler.row_started.connect(self._schedule_deferred_parsing_indicator)
        scheduler.lazy_paused = self._lazy_paused
        self._scheduler = scheduler

        # 封面模式走旁路，不做实际抓取
        if self._mode == "cover":
            for row in range(len(self._playlist_rows)):
                QTimer.singleShot(0, partial(self._process_cover_bypass, row))

    # ── 频道标签页/排序切换 ────────────────────────────────────────────────

    def _on_channel_tab_changed(self, index: int) -> None:
        if (
            not hasattr(self, "_channel_tab_combo_mapper")
            or index < 0
            or index >= len(self._channel_tab_combo_mapper)
        ):
            return
        new_tab = self._channel_tab_combo_mapper[index]
        if new_tab == self._channel_tab:
            return
        self._channel_tab = new_tab
        self._reload_channel()

    def _on_channel_sort_changed(self, index: int) -> None:
        new_reverse = index == 1  # 0=最新在前, 1=最旧在前
        if new_reverse == self._channel_reverse:
            return
        self._channel_reverse = new_reverse
        self._reload_channel()

    def _reload_channel(self) -> None:
        """切换频道标签页或排序后重新加载"""
        # 1. 停止当前所有后台解析
        self._stop_background_parsing()
        self._is_closing = False  # 重置关闭标记

        # 2. 清空当前列表
        self._playlist_rows = []
        self._action_widget_by_row = {}
        self._thumb_url_to_rows = {}
        self._thumb_applied_rows = {}
        self._thumb_requested = set()
        self._thumb_pending.clear()
        self._thumb_inflight = 0
        self._thumb_retry_count = {}

        if self._playlist_model is not None:
            self._playlist_model.clear()

        # 重新连接 image_loader 信号（_stop_background_parsing 断开了它们）
        try:
            self.image_loader.loaded.connect(self._on_thumb_loaded)
            self.image_loader.loaded_with_url.connect(self._on_thumb_loaded_with_url)
            self.image_loader.failed.connect(self._on_thumb_failed)
        except Exception:
            pass

        # 重新连接 dependency_manager 信号
        try:
            from ....core.dependency_manager import dependency_manager

            dependency_manager.check_finished.connect(self._on_dep_check_finished)
            dependency_manager.install_finished.connect(self._on_dep_install_finished)
            dependency_manager.check_error.connect(self._on_dep_error)
            dependency_manager.download_error.connect(self._on_dep_error)
        except Exception:
            pass

        # 3. 检查缓存状态，决定是否需要发网络请求
        if self._channel_tab == "all":
            # 对于 all，检查是否所有的 target 都是 loaded 或 unsupported
            needs_fetch = [
                tab
                for tab in ["videos", "shorts", "streams"]
                if self._channel_caches.get(tab, {}).get("status") == "unloaded"
            ]
        else:
            needs_fetch = (
                [self._channel_tab]
                if self._channel_caches.get(self._channel_tab, {}).get("status") == "unloaded"
                else []
            )

        if not needs_fetch:
            # 全部在缓存中，直接拼装并渲染（切排序走的就是这条路：零子进程、瞬时）
            info_dict = self._build_channel_info()
            if info_dict is None:
                self.on_parse_error(
                    {"title": self.tr("切换失败"), "content": self.tr("找不到有效的频道数据。")}
                )
                return

            # 由于没有发起请求，我们手动调用 parse_success
            # 为了防止卡死UI或状态机，通过 QTimer singleShot 调用
            from PySide6.QtCore import QTimer

            QTimer.singleShot(0, lambda: self.on_parse_success(info_dict))
            return

        # 4. 需要拉取数据
        # 切换到 loading 状态
        tab_name = {
            "all": self.tr("全部"),
            "videos": self.tr("常规视频"),
            "shorts": "Shorts",
            "streams": self.tr("直播回放"),
        }.get(self._channel_tab, self._channel_tab)
        sort_name = self.tr("最旧在前") if self._channel_reverse else self.tr("最新在前")
        self._switch_to_state(
            WindowState.LOADING,
            f"正在加载频道{tab_name}（{sort_name}）...",
            show_ring=True,
        )

        from ....download.workers import ChannelExtractWorker

        w = ChannelExtractWorker(self.url, needs_fetch, self._current_options)
        w.progress.connect(self._on_channel_progress)
        w.finished_all.connect(self.on_channel_parse_success)
        w.error.connect(self.on_parse_error)
        self.worker = w
        w.start()

    def _on_playlist_row_checked(self, row: int, checked: bool) -> None:
        # Legacy callback for old widget path; retained for safety.
        if not (0 <= row < len(self._playlist_rows)):
            return
        self._playlist_rows[row]["selected"] = bool(checked)
        self._playlist_rows[row]["status"] = self.tr("已选择") if checked else self.tr("未选择")
        self._update_download_btn_state()

    def _on_playlist_quality_clicked(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        if self._scheduler is None:
            return
        if not self._scheduler.is_loaded(row):
            aw = self._action_widget_by_row.get(row)
            if aw is not None:
                aw.set_loading(True, self.tr("获取中..."))
            self._scheduler.enqueue_foreground(row)
        else:
            self._open_row_format_picker(row)

    def _current_playlist_preset_height(self) -> int | None:
        preset_text = (
            self.preset_combo.currentText()
            if self.preset_combo is not None
            else self.tr("最高质量(自动)")
        )
        height_map = {
            self.tr("2160p(严格)"): 2160,
            self.tr("1440p(严格)"): 1440,
            self.tr("1080p(严格)"): 1080,
            self.tr("720p(严格)"): 720,
            self.tr("480p(严格)"): 480,
            self.tr("360p(严格)"): 360,
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

        # Intercept setText to append subtitle indicator
        original_set_text = aw.qualityButton.setText

        def _set_text_with_indicator(text: str) -> None:
            if self._mode not in ("subtitle", "cover") and data.get("subtitle_override"):
                text = str(text) + " [Cc]"
            original_set_text(text)

        aw.qualityButton.setText = _set_text_with_indicator

        # Batch all property writes so only one dataChanged is emitted at the end
        aw.begin_batch()
        try:
            self._auto_apply_row_preset_inner(row, data, aw)
        finally:
            aw.qualityButton.setText = original_set_text
            aw.end_batch()

    def _auto_apply_row_preset_inner(
        self, row: int, data: dict, aw: _PlaylistModelRowProxy
    ) -> None:
        if self._mode == "subtitle":
            aw.set_loading(False)
            if data.get("custom_summary"):
                aw.qualityButton.setText(data["custom_summary"])
            else:
                aw.qualityButton.setText(self.tr("独立字幕"))
            aw.infoLabel.setText("")
            return
        elif self._mode == "cover":
            aw.set_loading(False)
            if data.get("custom_summary"):
                aw.qualityButton.setText(data["custom_summary"])
            else:
                aw.qualityButton.setText(self.tr("独立封面"))
            aw.infoLabel.setText("")
            return

        mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0

        def _format_audio_brief(a: dict[str, Any] | None) -> str:
            if not a:
                return self.tr("音频-")
            try:
                abr_int = int(a.get("abr") or 0)
            except Exception:
                abr_int = 0
            return self.tr("音频{}k").format(abr_int) if abr_int > 0 else self.tr("音频-")

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

        if not (self._scheduler and self._scheduler.is_loaded(row)):
            if mode == 2:
                aw.set_loading(False)
                aw.qualityButton.setText(self.tr("音频(自动)"))
                aw.infoLabel.setText(self.tr("待解析大小"))
                return
            aw.set_loading(True, self.tr("待加载"))
            aw.infoLabel.setText("")
            return

        if data.get("custom_selection_data"):
            aw.set_loading(False)
            aw.qualityButton.setText(str(data.get("custom_summary") or self.tr("已自定义")))
            aw.infoLabel.setText(self.tr("使用自定义配置"))
            return

        override = getattr(self, "_playlist_format_override", None)
        if override is not None:
            aw.set_loading(False)
            from fluentytdl.ui.components.platforms.youtube import resolve_global_format

            fmt_str, _ = resolve_global_format(data.get("detail"), override)

            parts = fmt_str.split("+")
            vid = parts[0] if len(parts) > 0 else ""
            aid = parts[1] if len(parts) > 1 else ""

            # Get the full unfiltered formats list from detail
            detail_info = data.get("detail") or {}
            full_fmts = detail_info.get("formats") or []

            v_fmt = next(
                (f for f in full_fmts if str(f.get("format_id") or f.get("id")) == vid), None
            )
            a_fmt = next(
                (f for f in full_fmts if str(f.get("format_id") or f.get("id")) == aid), None
            )

            if not v_fmt and not a_fmt and override.download_type != "audio_only":
                pid = getattr(override, "preset_id", None)
                preset_map = {
                    "best_mp4": self.tr("最佳画质"),
                    "best_raw": self.tr("最佳画质(原盘)"),
                    "2160p": "2160p",
                    "1440p": "1440p",
                    "1080p": "1080p",
                    "720p": "720p",
                    "480p": "480p",
                    "360p": "360p",
                    "best_video": self.tr("最佳质量(无声)"),
                    "1080p_video": self.tr("1080p(无声)"),
                    "audio_best": self.tr("最佳音质"),
                    "audio_high": self.tr("高品质音频"),
                    "audio_std": self.tr("标准音频"),
                }
                aw.qualityButton.setText(preset_map.get(pid, self.tr("全局格式")))
                c_info = (
                    override.container_override.upper()
                    if getattr(override, "container_override", None)
                    else self.tr("自动容器")
                )
                if getattr(override, "download_type", None) == "audio_only":
                    c_info = (
                        override.audio_format_override.upper()
                        if override.audio_format_override
                        else self.tr("自动格式")
                    )
                aw.infoLabel.setText(f"全局: {c_info}")
                return

            if override.download_type == "audio_only":
                a_fmt = next(
                    (f for f in full_fmts if str(f.get("format_id") or f.get("id")) == vid), None
                )
                aw.qualityButton.setText(
                    _format_audio_brief(a_fmt) if a_fmt else self.tr("音频(自动)")
                )
                if a_fmt:
                    aw.infoLabel.setText(
                        _format_info_line(
                            "",
                            a_fmt.get("filesize") or a_fmt.get("filesize_approx"),
                            a_fmt.get("ext"),
                        )
                    )
                else:
                    aw.infoLabel.setText("-")
            else:
                v_h = v_fmt.get("height") if v_fmt else None
                v_text = f"{v_h}p" if v_h else self.tr("最佳画质")
                a_text = (
                    _format_audio_brief(a_fmt)
                    if a_fmt
                    else ("" if override.download_type == "video_only" else self.tr("音频(自动)"))
                )

                if override.download_type == "video_only":
                    aw.qualityButton.setText(v_text)
                else:
                    aw.qualityButton.setText(f"{v_text} + {a_text}")

                v_size = v_fmt.get("filesize") or v_fmt.get("filesize_approx") if v_fmt else None
                v_ext = v_fmt.get("ext") if v_fmt else None
                v_line = _format_info_line(self.tr("视频 "), v_size, v_ext) if v_fmt else ""

                a_line = ""
                if a_fmt:
                    a_size = a_fmt.get("filesize") or a_fmt.get("filesize_approx")
                    a_ext = a_fmt.get("ext")
                    a_line = "\n" + _format_info_line(self.tr("音频 "), a_size, a_ext)
                aw.infoLabel.setText(v_line + a_line if (v_line or a_line) else "-")
            return

        audio_fmts: list[dict[str, Any]] = data.get("audio_formats") or []

        def _find_video_ext_for_row() -> str | None:
            vid = str(data.get("override_format_id") or "").strip()
            if not vid:
                return None
            for vf in data.get("video_formats") or []:
                if str(vf.get("id") or "") == vid:
                    return str(vf.get("ext") or "").strip().lower() or None
            return None

        def _choose_best_audio() -> dict[str, Any] | None:
            if not audio_fmts:
                return None
            if mode != 0:
                return audio_fmts[0]
            vext = _find_video_ext_for_row()
            if not vext:
                return audio_fmts[0]
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
            aw.set_loading(False)
            aw.qualityButton.setText(
                str(
                    data.get("audio_override_text")
                    if bool(data.get("audio_manual_override"))
                    else (data.get("audio_best_text") or self.tr("音频(自动)"))
                )
            )
            if chosen_audio:
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
                audio_brief = (
                    data.get("audio_override_text")
                    if bool(data.get("audio_manual_override"))
                    else (data.get("audio_best_text") or self.tr("音频-"))
                )
                aw.qualityButton.setText(f"{chosen or self.tr('视频已选')} + {audio_brief}")
                chosen_fmt = None
                override_id = str(data.get("override_format_id") or "")
                for f in data.get("video_formats") or []:
                    if str(f.get("id") or "") == override_id:
                        chosen_fmt = f
                        break
                v_line = _format_info_line(
                    self.tr("视频 "),
                    (chosen_fmt or {}).get("filesize"),
                    (chosen_fmt or {}).get("ext"),
                )
                a_line = _format_info_line(
                    self.tr("音频 "),
                    (chosen_audio or {}).get("filesize"),
                    (chosen_audio or {}).get("ext"),
                )
                aw.infoLabel.setText(v_line + "\n" + a_line)
                return
            aw.qualityButton.setText(chosen or self.tr("已手动选择"))
            return

        # VR 模式下的自动选择模拟（用于 UI 显示）
        if self._vr_mode:
            fmts = data.get("video_formats") or []
            if not fmts:
                aw.set_loading(False)
                aw.qualityButton.setText(self.tr("无可用格式"))
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
                    if f.get("ext") == "mp4":
                        best = f
                        break

            # 如果没找到或者其他预设，取第一个（因为通常第一个是质量最好的）
            if not best:
                best = fmts[0]

            fid = best.get("id")
            if fid:
                data["override_format_id"] = str(fid)

            # VR 模式下通常不需要显示音频组合，直接显示 VR 格式描述
            # 构造 VR 描述
            h = best.get("height") or 0
            vc = str(best.get("vcodec") or "")[:4]
            # 这里缺少 3D/投影 信息，UI 显示可能不如 SelectionDialog 完美，但够用了
            # 用户可以通过点击进去看详情
            data["override_text"] = f"{h}p ({vc})"
            data["manual_override"] = False

            aw.set_loading(False)
            aw.qualityButton.setText(str(data["override_text"] or ""))

            # 显示详细信息（包含音频信息）
            v_line = _format_info_line(
                self.tr("视频 "),
                best.get("filesize") or best.get("filesize_approx"),
                best.get("ext"),
            )
            a_line = ""
            if chosen_audio:
                a_line = "\n" + _format_info_line(
                    self.tr("音频 "),
                    chosen_audio.get("filesize") or chosen_audio.get("filesize_approx"),
                    chosen_audio.get("ext"),
                )

            aw.infoLabel.setText(v_line + a_line)
            return

        fmts: list[dict[str, Any]] = data.get("video_formats") or []
        if not fmts:
            aw.set_loading(False)
            aw.qualityButton.setText(self.tr("无可用格式"))
            aw.infoLabel.setText("")
            return

        preset_height = self._current_playlist_preset_height()
        if preset_height is None:
            best = fmts[0]
        else:
            candidates = [f for f in fmts if int(f.get("height") or 0) == preset_height]
            if not candidates:
                aw.set_loading(False)
                aw.qualityButton.setText(self.tr("无匹配(点选)"))
                if mode == 0:
                    a_line = _format_info_line(
                        self.tr("音频 "),
                        (chosen_audio or {}).get("filesize"),
                        (chosen_audio or {}).get("ext"),
                    )
                    aw.infoLabel.setText(self.tr("可手动选择\n") + a_line)
                else:
                    aw.infoLabel.setText(self.tr("可手动选择"))
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
        data["manual_override"] = False

        aw.set_loading(False)
        if mode == 1:
            aw.qualityButton.setText(str(data["override_text"] or ""))
            aw.infoLabel.setText(_format_info_line("", best.get("filesize"), best.get("ext")))
            return

        audio_brief = (
            data.get("audio_override_text")
            if bool(data.get("audio_manual_override"))
            else (data.get("audio_best_text") or self.tr("音频-"))
        )
        aw.qualityButton.setText(f"{data.get('override_text') or ''} + {audio_brief}")
        v_line = _format_info_line(self.tr("视频 "), best.get("filesize"), best.get("ext"))
        a_line = _format_info_line(
            self.tr("音频 "), (chosen_audio or {}).get("filesize"), (chosen_audio or {}).get("ext")
        )
        aw.infoLabel.setText(v_line + "\n" + a_line)

    def _on_playlist_preset_changed(self, _index: int) -> None:
        self._playlist_format_override = None
        for r in range(len(self._playlist_rows)):
            self._auto_apply_row_preset(r)
        self._update_download_btn_state()

    def _on_playlist_type_changed(self, index: int) -> None:
        self._playlist_format_override = None
        if self.preset_combo is not None:
            self.preset_combo.setEnabled(index in (0, 1))
        for r in range(len(self._playlist_rows)):
            self._auto_apply_row_preset(r)
        self._update_download_btn_state()

    def _on_concurrency_changed(self, index: int) -> None:
        vals = [1, 2, 3, 5, 8, 12, 16]
        if 0 <= index < len(vals):
            new_val = vals[index]
            from ....core.config_manager import config_manager

            config_manager.set("playlist_extract_concurrency", new_val)
            if self._extract_manager:
                self._extract_manager.set_concurrency(new_val)
            if self._scheduler:
                self._scheduler.set_concurrency(new_val)

    def _on_global_format_clicked(self):
        from ...dialogs.playlist_format_dialog import PlaylistFormatConfigDialog

        dialog = PlaylistFormatConfigDialog(
            current_override=self._playlist_format_override, parent=self
        )
        if dialog.exec():
            self._playlist_format_override = dialog.get_override()

            t = self._playlist_format_override.download_type
            idx = {"video_audio": 0, "video_only": 1, "audio_only": 2}.get(t, 0)
            self.type_combo.blockSignals(True)
            self.type_combo.setCurrentIndex(idx)
            if self.preset_combo is not None:
                self.preset_combo.setEnabled(idx in (0, 1))
            self.type_combo.blockSignals(False)

            for r in range(len(self._playlist_rows)):
                self._auto_apply_row_preset(r)
            self._update_download_btn_state()

    def _set_row_parsing(self, row: int, is_parsing: bool) -> None:
        if self._playlist_model is None:
            return
        idx = self._playlist_model.index(row, 0)
        task = self._playlist_model.get_task(idx)
        if task is not None and task.is_parsing != is_parsing:
            task.is_parsing = is_parsing
            self._schedule_playlist_row_update(row)

    def _schedule_deferred_parsing_indicator(self, row: int) -> None:
        """Show '解析中…' only if the extraction hasn't completed within 800ms.

        Increased from 300ms to 800ms so that typical fast extractions (400-600ms)
        skip the intermediate self.tr('解析中') state entirely, eliminating the visible
        triple-flash (待加载 → 解析中 → 格式).
        """

        def _apply():
            if self._is_closing or self._playlist_model is None:
                return  # Window already closed
            # 已完成（成功或失败）则跳过，避免 self.tr("待加载 → 解析中 → 格式") 三段式闪烁
            if self._scheduler and (
                self._scheduler.is_loaded(row) or self._scheduler.is_failed(row)
            ):
                return
            self._set_row_parsing(row, True)

        QTimer.singleShot(800, _apply)

    def _schedule_playlist_row_update(self, row: int) -> None:
        if self._playlist_model is None:
            return
        self._playlist_model.mark_row_dirty(row)

    def _update_playlist_row_view(self, row: int) -> None:
        view = self._list_view
        model = self._playlist_model
        if view is None or model is None:
            return
        if not (0 <= row < model.rowCount()):
            return
        view.update(model.index(row, 0))

    def _enqueue_detail_finalization(self, row: int) -> None:
        if self._is_closing:
            return
        if not (0 <= row < len(self._playlist_rows)):
            return
        self._detail_finalize_rows.add(row)
        if not self._detail_finalize_timer.isActive():
            self._detail_finalize_timer.start()

    def _flush_detail_finalizations(self) -> None:
        if self._is_closing:
            self._detail_finalize_rows.clear()
            return
        if not self._detail_finalize_rows:
            return

        rows = sorted(self._detail_finalize_rows)
        self._detail_finalize_rows.clear()

        for row in rows:
            self._auto_apply_row_preset(row)

        self._refresh_progress_label()
        self._update_download_btn_state()

    def _schedule_playlist_rows_update(self, rows: list[int] | set[int] | range) -> None:
        for row in rows:
            self._schedule_playlist_row_update(int(row))

    def _initial_viewport_scan(self) -> None:
        if not self._is_closing:
            self._on_list_scrolled(0)

    def _on_scroll_value_changed(self, _value: int) -> None:
        if not self._scroll_throttle_timer.isActive():
            self._scroll_throttle_timer.start()

    def _on_scroll_throttled(self) -> None:
        self._on_list_scrolled(0)

    def _on_list_scrolled(self, _value: int) -> None:
        if self._is_closing or self._scheduler is None:
            return
        first, last = self._visible_row_range()
        self._scheduler.set_viewport(first, last)
        self._load_thumbs_for_visible_rows()

    def _on_list_item_clicked(self, index: QModelIndex) -> None:
        if self._playlist_delegate is None or self._list_view is None:
            return
        row = index.row()
        viewport = self._list_view.viewport()
        pos = viewport.mapFromGlobal(QCursor.pos())
        option = QStyleOptionViewItem()
        option.rect = self._list_view.visualRect(index)
        hit = self._playlist_delegate.hit_test(pos, option)
        if hit in ("checkbox", "row"):
            self._toggle_row_selection(row)
        elif hit == "action_btn":
            self._on_playlist_quality_clicked(row)

    def _toggle_row_selection(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        new_val = not bool(self._playlist_rows[row].get("selected"))
        self._playlist_rows[row]["selected"] = new_val
        self._playlist_rows[row]["status"] = self.tr("已选择") if new_val else self.tr("未选择")
        if self._playlist_model is not None:
            idx = self._playlist_model.index(row, 0)
            task = self._playlist_model.get_task(idx)
            if task is not None:
                task.selected = new_val
                self._schedule_playlist_row_update(row)
        self._update_download_btn_state()

    def _visible_row_range(self) -> tuple[int, int]:
        view = self._list_view
        model = self._playlist_model
        if view is None or model is None:
            return (0, -1)
        first_idx = view.indexAt(QPoint(0, 0))
        first = first_idx.row() if first_idx.isValid() else 0
        if first < 0:
            first = 0
        last_idx = view.indexAt(QPoint(0, view.viewport().height() - 1))
        last = last_idx.row()
        if last < 0:
            last = min(model.rowCount() - 1, first + 8)
        return (first, last)

    def _on_thumb_init_timeout(self) -> None:
        if self._is_closing or not self._is_playlist:
            return
        self._load_thumbs_batch(0, min(20, len(self._playlist_rows) - 1))

    def _load_thumbs_batch(self, first: int, last: int) -> None:
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
            self._thumb_pending.append(url)
            self._thumb_requested.add(url)
        self._process_thumb_queue()

    def _process_thumb_queue(self) -> None:
        while self._thumb_pending and self._thumb_inflight < self._thumb_max_concurrent:
            best_idx = self._pick_best_thumb_index()
            try:
                url = self._thumb_pending[best_idx]
                del self._thumb_pending[best_idx]
            except Exception:
                url = self._thumb_pending.popleft()
            self._thumb_inflight += 1
            self.image_loader.load(url, target_size=(150, 84), radius=8)

    def _pick_best_thumb_index(self) -> int:
        if not self._thumb_pending:
            return 0

        first, last = self._visible_row_range()
        if first > last:
            return 0

        viewport_center = (first + last) / 2.0
        best_idx = 0
        best_distance = float("inf")

        for idx, url in enumerate(self._thumb_pending):
            rows = self._thumb_url_to_rows.get(url, set())
            if not rows:
                continue
            min_dist = min(abs(row - viewport_center) for row in rows)
            if min_dist < best_distance:
                best_distance = min_dist
                best_idx = idx

        return best_idx

    def _load_thumbs_for_visible_rows(self) -> None:
        first, last = self._visible_row_range()
        first = max(0, first - 8)
        last = min(len(self._playlist_rows) - 1, last + 15)
        self._load_thumbs_batch(first, last)

    def _apply_thumb_to_row(self, row: int, url: str) -> None:
        pix = self._thumb_cache.get(url)
        if pix is None:
            return
        if self._thumb_applied_rows.get(row) == url:
            return
        if self._playlist_delegate is not None and self._playlist_model is not None:
            self._playlist_delegate.set_pixmap(url, pix)
            self._thumb_applied_rows[row] = url
            self._update_playlist_row_view(row)

    def _on_thumb_loaded_with_url(self, url: str, pixmap) -> None:
        self._thumb_inflight = max(0, self._thumb_inflight - 1)
        self._process_thumb_queue()

        if self._is_closing:
            return

        # 加载页预览条那张图：只认自己请求的那个 URL，与播放列表的行匹配互不干扰。
        if self._preview_thumb_url and str(url or "").strip() == self._preview_thumb_url:
            try:
                self.previewThumb.setImage(pixmap)
                self.previewThumb.setFixedSize(160, 90)
                self.previewThumb.setBorderRadius(8, 8, 8, 8)
                if self._preview_show_on_thumb:
                    self._preview_show_on_thumb = False
                    # 仍停在加载页才亮出来；解析已完成就不要再往回插一条预览。
                    if self._window_state == WindowState.LOADING:
                        self.previewWidget.show()
            except Exception:
                pass

        if not self._is_playlist:
            return
        u = str(url or "").strip()
        if not u:
            return
        self._thumb_cache[u] = pixmap

        # 1. 尝试匹配推特实体化列表的缩略图
        if hasattr(self, "_twitter_thumb_labels"):
            tw_label = self._twitter_thumb_labels.get(u)
            if tw_label is not None:
                try:
                    tw_label.setImage(pixmap)
                except Exception:
                    pass

        # 2. 匹配常规虚拟化表格的缩略图
        affected_rows = self._thumb_url_to_rows.get(u, set())
        if not affected_rows:
            return
        if self._playlist_delegate is not None:
            self._playlist_delegate.set_pixmap(u, pixmap)
        if self._playlist_model is not None:
            first_visible, last_visible = self._visible_row_range()
            for row in affected_rows:
                self._thumb_applied_rows[row] = u
                if row < first_visible or row > last_visible:
                    continue
                self._update_playlist_row_view(row)

    def _on_thumb_loaded(self, pixmap) -> None:
        # Legacy callback for single video thumb
        if self.thumb_label:
            self.thumb_label.setPixmap(pixmap)

    def _on_thumb_failed(self, url: str) -> None:
        self._thumb_inflight = max(0, self._thumb_inflight - 1)
        failed_url = str(url or "").strip()
        if failed_url:
            retries = self._thumb_retry_count.get(failed_url, 0)
            if retries < 2 and failed_url in self._thumb_url_to_rows:
                self._thumb_retry_count[failed_url] = retries + 1
                self._thumb_requested.discard(failed_url)
                self._thumb_pending.appendleft(failed_url)
                QTimer.singleShot((retries + 1) * 400, self._process_thumb_queue)
                return
        self._process_thumb_queue()

    def _select_all(self) -> None:
        self._set_all_checks(True)

    def _unselect_all(self) -> None:
        self._set_all_checks(False)

    def _invert_select(self) -> None:
        for row in range(len(self._playlist_rows)):
            self._playlist_rows[row]["selected"] = not bool(
                self._playlist_rows[row].get("selected")
            )
            self._playlist_rows[row]["status"] = (
                self.tr("已选择") if self._playlist_rows[row]["selected"] else self.tr("未选择")
            )
            if self._playlist_model is not None:
                idx = self._playlist_model.index(row, 0)
                task = self._playlist_model.get_task(idx)
                if task is not None:
                    task.selected = bool(self._playlist_rows[row]["selected"])
        self._schedule_playlist_rows_update(range(len(self._playlist_rows)))
        self._update_download_btn_state()

    def _set_all_checks(self, checked: bool) -> None:
        for row in range(len(self._playlist_rows)):
            self._playlist_rows[row]["selected"] = bool(checked)
            self._playlist_rows[row]["status"] = self.tr("已选择") if checked else self.tr("未选择")
            if self._playlist_model is not None:
                idx = self._playlist_model.index(row, 0)
                task = self._playlist_model.get_task(idx)
                if task is not None:
                    task.selected = bool(checked)
        self._schedule_playlist_rows_update(range(len(self._playlist_rows)))
        self._update_download_btn_state()

    def _apply_preset_to_selected(self) -> None:
        for row, data in enumerate(self._playlist_rows):
            if not data.get("selected"):
                continue
            data["override_format_id"] = None
            data["override_text"] = None
            data["manual_override"] = False
            data["audio_override_format_id"] = None
            data["audio_override_text"] = None
            data["audio_manual_override"] = False
            data["custom_selection_data"] = None
            data["custom_summary"] = None
            self._auto_apply_row_preset(row)
        self._update_download_btn_state()

    def _update_download_btn_state(self) -> None:
        any_selected = any(bool(r.get("selected")) for r in self._playlist_rows)
        self.yesButton.setEnabled(bool(any_selected))
        if not any_selected:
            self.yesButton.setText(self.tr("下载"))
            return
        mode = int(self.type_combo.currentIndex()) if self.type_combo is not None else 0
        if mode == 2:
            self.yesButton.setText(self.tr("下载"))
            return
        selected_rows = [i for i, r in enumerate(self._playlist_rows) if r.get("selected")]
        pending = [
            i
            for i in selected_rows
            if self._scheduler is not None
            and not self._scheduler.is_loaded(i)
            and not self._scheduler.is_failed(i)
        ]
        if pending:
            self.yesButton.setText(self.tr("下载（剩余 {} 个解析中...）").format(len(pending)))
        else:
            self.yesButton.setText(self.tr("下载"))

    def _refresh_progress_label(self) -> None:
        if hasattr(self, "progressLabel"):
            total = len(self._playlist_rows)
            done = self._scheduler.done_count() if self._scheduler is not None else 0
            self.progressLabel.setText(self.tr("详情补全：{}/{}").format(done, total))
            try:
                if hasattr(self, "progressRing"):
                    self.progressRing.setVisible(done < total)
            except Exception:
                pass

    def _on_scheduler_detail_finished(self, row: int, info: Any) -> None:
        """PlaylistScheduler.detail_finished → 更新 UI 层数据。"""
        if self._is_closing:
            return
        if not (0 <= row < len(self._playlist_rows)):
            return

        info_dict = _normalize_info_payload(info)

        row_data = self._playlist_rows[row]
        thumb = str(row_data.get("thumbnail") or "").strip()
        if not thumb:
            thumb = _infer_entry_thumbnail(info_dict)
            if thumb:
                row_data["thumbnail"] = thumb
                self._thumb_url_to_rows.setdefault(thumb, set()).add(row)
        if thumb:
            if thumb in self._thumb_cache:
                # 应用已缓存的缩略图；后续由 _auto_apply_row_preset 合并成单次重绘
                pix = self._thumb_cache.get(thumb)
                if pix is not None and self._playlist_delegate is not None:
                    self._playlist_delegate.set_pixmap(thumb, pix)
                    self._thumb_applied_rows[row] = thumb
            elif thumb not in self._thumb_requested:
                self._thumb_pending.appendleft(thumb)
                self._thumb_requested.add(thumb)
                self._process_thumb_queue()

        formats = _clean_video_formats(info_dict)
        audio_formats = _clean_audio_formats(info_dict)
        highest = formats[0]["height"] if formats else None

        row_data["detail"] = info_dict
        row_data["video_formats"] = formats
        row_data["audio_formats"] = audio_formats
        row_data["highest_height"] = highest
        row_data["status"] = self.tr("已选择") if row_data.get("selected") else self.tr("未选择")

        if self._playlist_model is not None:
            idx = self._playlist_model.index(row, 0)
            task = self._playlist_model.get_task(idx)
            if task is not None:
                task.thumbnail_url = thumb
                task.raw_info = info_dict
                task.video_formats = formats
                task.audio_formats = audio_formats
                task.has_error = False
                task.error_msg = ""
                task.is_parsing = False
                raw_date = _format_upload_date(info_dict.get("upload_date"))
                if raw_date and raw_date != "-":
                    task.upload_date = raw_date
                    row_data["upload_date"] = raw_date
                # 不在此处调度重绘；_auto_apply_row_preset 会合并成单次 dataChanged

        self._enqueue_detail_finalization(row)

    def _on_scheduler_detail_error(self, row: int, msg: str) -> None:
        """PlaylistScheduler.detail_error → 标记错误并更新 UI。"""
        if self._is_closing:
            return
        if not (0 <= row < len(self._playlist_rows)):
            return

        if self._playlist_model is not None:
            idx = self._playlist_model.index(row, 0)
            task = self._playlist_model.get_task(idx)
            if task is not None:
                task.has_error = True
                task.error_msg = msg
                task.is_parsing = False
                self._schedule_playlist_row_update(row)

        self._refresh_progress_label()
        self._update_download_btn_state()

    def _process_cover_bypass(self, row: int) -> None:
        if self._is_closing:
            return
        if self._scheduler and self._scheduler.is_loaded(row):
            return
        if not (0 <= row < len(self._playlist_rows)):
            return

        data = self._playlist_rows[row]
        info = {
            "id": data.get("id"),
            "url": data.get("url"),
            "title": data.get("title"),
            "thumbnail": data.get("thumbnail"),
            "uploader": data.get("uploader"),
            "duration": data.get("duration"),
        }

        data["detail"] = info
        data["video_formats"] = []
        data["audio_formats"] = []
        data["highest_height"] = None
        if self._scheduler is not None:
            self._scheduler.mark_row_loaded(row)

        if self._playlist_model is not None:
            idx = self._playlist_model.index(row, 0)
            task = self._playlist_model.get_task(idx)
            if task is not None:
                task.raw_info = info
                task.video_formats = []
                task.audio_formats = []
                task.has_error = False
                task.error_msg = ""
                task.is_parsing = False
                # Do NOT schedule here; _auto_apply_row_preset handles it.

        self._enqueue_detail_finalization(row)

    def _on_idle_tick(self) -> None:
        if not self._is_playlist:
            return
        if self._scheduler is None or self._scheduler.lazy_paused:
            return
        if time.monotonic() - self._last_interaction < 2.0:
            return
        if not self._scheduler.is_crawl_active:
            self._scheduler.start_crawl()

    def _on_lazy_pause_changed(self, checked: bool) -> None:
        """用户手动暂停/恢复后台详情解析"""
        self._lazy_paused = checked
        if hasattr(self, "lazyPauseBtn"):
            self.lazyPauseBtn.setText(self.tr("已暂停") if checked else self.tr("暂停解析"))
        if self._scheduler is None:
            return
        self._scheduler.lazy_paused = checked
        if checked:
            self._scheduler.stop_crawl()
        else:
            self._initial_viewport_scan()
            self._scheduler.start_crawl()

    def _open_row_format_picker(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        if not (self._scheduler and self._scheduler.is_loaded(row)):
            return
        data = self._playlist_rows[row]
        info = data.get("detail")
        if not info:
            return

        dialog = PlaylistFormatDialog(info, self, vr_mode=self._vr_mode, mode=self._mode)
        if dialog.exec():
            sel = dialog.get_selection()
            if sel and sel.get("format"):
                data["custom_selection_data"] = sel
                data["custom_summary"] = dialog.get_summary()
                if hasattr(dialog, "get_subtitle_override"):
                    data["subtitle_override"] = dialog.get_subtitle_override()
                data["manual_override"] = True
                data["override_format_id"] = None
                data["override_text"] = None
                data["audio_override_format_id"] = None
                data["audio_override_text"] = None
                data["audio_manual_override"] = False
                self._auto_apply_row_preset(row)

    def _handle_container_conflict(self, ydl_opts: dict) -> bool:
        from qfluentwidgets import BodyLabel, MessageBoxBase, PushButton, SubtitleLabel

        from ....utils.container_compat import (
            check_audio_multistream_container_compat,
            check_subtitle_container_compat,
        )

        container = ydl_opts.get("merge_output_format")
        if not container:
            return True

        # 1. Check audio track conflict
        audio_count = ydl_opts.get("__audio_track_count", 1)
        audio_conflict = check_audio_multistream_container_compat(container, audio_count)

        if audio_conflict:

            class AudioConflictDialog(MessageBoxBase):
                def __init__(self, parent=None):
                    super().__init__(parent)
                    self.titleLabel = SubtitleLabel(self.tr("多音轨容器兼容性警告"), self)
                    self.viewLayout.addWidget(self.titleLabel)
                    self.msgLabel = BodyLabel(
                        audio_conflict + self.tr("\n\n请选择解决方案："), self
                    )
                    self.msgLabel.setWordWrap(True)
                    self.viewLayout.addWidget(self.msgLabel)
                    self.widget.setMinimumWidth(400)
                    self.yesButton.setText(self.tr("切换为 MKV"))
                    self.cancelButton.setText(self.tr("保持原格式 (不推荐)"))
                    self.result_action = "keep"

                def accept(self):
                    self.result_action = "mkv"
                    super().accept()

                def reject(self):
                    self.result_action = "keep"
                    super().reject()

            dialog = AudioConflictDialog(self)
            dialog.exec()
            if dialog.result_action == "mkv":
                ydl_opts["merge_output_format"] = "mkv"
                container = "mkv"  # update for following checks
            # if keep, we do nothing and proceed

        # 2. Check subtitle conflict
        is_embed = ydl_opts.get("embedsubtitles", False)
        lang_count = len(ydl_opts.get("subtitleslangs", [])) if is_embed else 0

        conflict_msg = check_subtitle_container_compat(container, is_embed, lang_count)
        if not conflict_msg:
            return True

        from qfluentwidgets import MessageBoxBase

        class ConflictDialog(MessageBoxBase):
            def __init__(self, parent=None):
                super().__init__(parent)
                self.titleLabel = SubtitleLabel(self.tr("容器格式冲突"), self)
                self.viewLayout.addWidget(self.titleLabel)
                self.msgLabel = BodyLabel(conflict_msg + self.tr("\n\n请选择解决方案："), self)
                self.msgLabel.setWordWrap(True)
                self.viewLayout.addWidget(self.msgLabel)
                self.widget.setMinimumWidth(400)
                self.yesButton.setText(self.tr("切换为 MKV"))
                self.cancelButton.setText(self.tr("字幕改为外挂"))
                self.abortBtn = PushButton(self.tr("取消"))
                self.buttonLayout.insertWidget(0, self.abortBtn)
                self.abortBtn.clicked.connect(self.reject)
                self.cancelButton.clicked.disconnect()
                self.cancelButton.clicked.connect(self._accept_external)
                self.result_action = "abort"

            def accept(self):
                self.result_action = "mkv"
                super().accept()

            def _accept_external(self):
                self.result_action = "external"
                super().accept()

        dialog = ConflictDialog(self)
        if dialog.exec():
            if dialog.result_action == "mkv":
                ydl_opts["merge_output_format"] = "mkv"
                return True
            elif dialog.result_action == "external":
                ydl_opts["embedsubtitles"] = False
                return True

        return False

    def get_selected_tasks(self) -> list[tuple[str, str, dict[str, Any], str | None]]:
        tasks = []

        # 1. Single Video Mode
        if not self._is_playlist:
            if not self.video_info:
                logger.debug("get_selected_tasks: video_info is None")
                return []

            info = self.video_info
            dto = self.video_info_dto
            url = dto.source_url if dto and dto.source_url else _infer_entry_url(info)
            title = dto.title if dto and dto.title else str(info.get("title") or "Unknown")
            thumb = (
                dto.thumbnail_url if dto and dto.thumbnail_url else str(info.get("thumbnail") or "")
            )

            ydl_opts: dict[str, Any] = {}

            # Mode specific handling
            if self._mode == "subtitle":
                title_prefix = self.tr("[字幕]")
                if isinstance(self.selector_widget, SubtitleSelectorWidget):
                    ydl_opts.update(self.selector_widget.get_opts())
                    langs, _, _ = self.selector_widget.get_selected_language_codes()
                    if langs:
                        title_prefix = f"[字幕 ({', '.join(langs)})]"

                ydl_opts["skip_download"] = True
                ydl_opts["writethumbnail"] = False
                ydl_opts["embedthumbnail"] = False
                ydl_opts["addmetadata"] = False
                ydl_opts["embedsubtitles"] = False
                ydl_opts["sponsorblock_remove"] = None
                ydl_opts["sponsorblock_mark"] = None
                ydl_opts["postprocessors"] = []

                tasks.append((f"{title_prefix} {title}", url, ydl_opts, thumb))
                return tasks

            elif self._mode == "cover":
                if isinstance(self.selector_widget, CoverSelectorWidget):
                    url = self.selector_widget.get_selected_url() or url
                    _ = self.selector_widget.get_selected_ext()
                    ydl_opts.clear()  # 清理掉所有上一层合并的冗余配置
                    ydl_opts["__fluentytdl_is_cover_direct"] = True
                    safe_title = sanitize_filename(title)
                    ydl_opts["outtmpl"] = f"{safe_title}.%(ext)s"
                else:
                    ydl_opts["skip_download"] = True
                    ydl_opts["writethumbnail"] = True
                    ydl_opts["embedthumbnail"] = False
                    ydl_opts["addmetadata"] = False
                    ydl_opts["embedsubtitles"] = False
                    ydl_opts["sponsorblock_remove"] = None
                    ydl_opts["sponsorblock_mark"] = None
                    ydl_opts["postprocessors"] = []

                tasks.append((f"[封面] {title}", url, ydl_opts, thumb))
                return tasks

            # Delegate to the format selector component
            if isinstance(
                self.selector_widget, (VideoFormatSelectorWidget, VRFormatSelectorWidget)
            ):
                sel = self.selector_widget.get_selection_result()
                if sel and sel.get("format"):
                    ydl_opts["format"] = sel["format"]
                    ydl_opts.update(sel.get("extra_opts") or {})
                    try:
                        ydl_opts["__fluentytdl_format_note"] = (
                            self.selector_widget.get_summary_text()
                        )
                    except Exception:
                        pass

                    # ========== VR 格式检测 ==========
                    vr_only_ids = (
                        dto.vr_only_format_ids
                        if dto is not None
                        else (info.get("__vr_only_format_ids") or [])
                    )
                    android_vr_ids = (
                        dto.android_vr_format_ids
                        if dto is not None
                        else (info.get("__android_vr_format_ids") or [])
                    )
                    if vr_only_ids:
                        selected_format = sel["format"]
                        for vr_id in vr_only_ids:
                            if vr_id in selected_format:
                                ydl_opts["__fluentytdl_use_android_vr"] = True
                                ydl_opts["__android_vr_format_ids"] = android_vr_ids
                                break

                    if self._vr_mode:
                        ydl_opts["__fluentytdl_use_android_vr"] = True
                else:
                    ydl_opts["format"] = "bestvideo+bestaudio/best"
                    ydl_opts["__fluentytdl_format_note"] = self.tr("最佳画质")
            else:
                ydl_opts["format"] = "bestvideo+bestaudio/best"
                ydl_opts["__fluentytdl_format_note"] = self.tr("最佳画质")

            # Apply checkbox overrides if available (Default Mode)
            from ....utils.url_router import url_router

            is_twitter = url_router.detect_platform(url) == "twitter"

            sub_config_override = None
            if is_twitter:
                ydl_opts["writethumbnail"] = False
                ydl_opts["embedthumbnail"] = False
                ydl_opts["addmetadata"] = False
                ydl_opts["writesubtitles"] = False
                ydl_opts["embedsubtitles"] = False
                ydl_opts["__fluentytdl_keep_thumbnail"] = False
            elif hasattr(self, "subtitle_check"):
                import copy

                from ....core.config_manager import config_manager

                # Subtitles
                sub_config_override = copy.deepcopy(config_manager.get_subtitle_config())
                sub_config_override.enabled = self.subtitle_check.isChecked()

                # Cover
                is_cover = self.cover_check.isChecked()
                is_embed = getattr(self, "embed_check", self.cover_check).isChecked()
                if is_cover or is_embed:
                    ydl_opts["writethumbnail"] = True
                    ydl_opts["convert_thumbnail"] = "jpg"
                else:
                    ydl_opts["writethumbnail"] = False
                ydl_opts["__fluentytdl_keep_thumbnail"] = is_cover
                ydl_opts["embedthumbnail"] = is_embed

                # Metadata
                ydl_opts["addmetadata"] = self.metadata_check.isChecked()

            section_selector = getattr(self, "_section_selector", None)
            section_enabled = bool(section_selector and section_selector.is_enabled())

            # 字幕集成。裁切下载在首期与字幕互斥，避免原时间轴字幕错位。
            if self.video_info and not is_twitter and not section_enabled:
                pick = getattr(self, "_subtitle_pick_result", None)
                if pick and pick.selected_tracks:
                    from ....processing.subtitle_service import build_subtitle_opts_from_tracks

                    sub_opts = build_subtitle_opts_from_tracks(pick.selected_tracks)
                    ydl_opts.update(sub_opts)

                    ydl_opts["embedsubtitles"] = pick.embed_subtitles
                    if pick.output_format:
                        ydl_opts["convertsubtitles"] = pick.output_format
                else:
                    embed_override = None

                    subtitle_opts = subtitle_service.apply(
                        video_id=(
                            dto.video_id if dto is not None else self.video_info.get("id", "")
                        ),
                        video_info=self.video_info,
                        user_config=sub_config_override,
                    )
                    ydl_opts.update(subtitle_opts)

                    if embed_override is not None:
                        if sub_config_override:
                            embed_type = sub_config_override.embed_type
                        else:
                            from ....core.config_manager import config_manager as cfg

                            embed_type = cfg.get_subtitle_config().embed_type

                        if embed_type == "soft":
                            ydl_opts["embedsubtitles"] = embed_override
                        elif embed_type == "external":
                            ydl_opts["embedsubtitles"] = False

                # Check explicit conflict
                from ....utils.container_compat import (
                    ensure_audio_multistream_compatible_container,
                    ensure_subtitle_compatible_container,
                )

                override_fmt = getattr(
                    self.selector_widget, "get_container_override", lambda: None
                )()
                audio_count = ydl_opts.get("__audio_track_count", 1)

                if override_fmt:
                    if not self._handle_container_conflict(ydl_opts):
                        return []
                else:
                    ensure_subtitle_compatible_container(ydl_opts)
                    ensure_audio_multistream_compatible_container(ydl_opts, audio_count)

            # === Quality Guard Pre-flight Check (Single Video) ===
            if self._mode not in ("subtitle", "cover") and not ydl_opts.get("skip_download"):
                from ....download.quality_guard import resolve_format_with_guard

                original_format = ydl_opts.get("format", "bestvideo+bestaudio/best")
                intent_max_height = None
                intent_preset_id = None

                if isinstance(self.selector_widget, VideoFormatSelectorWidget):
                    sel = getattr(self.selector_widget, "simple_widget", None)
                    if sel:
                        curr_sel = sel.get_current_selection()
                        if curr_sel and curr_sel.get("intent"):
                            intent_max_height = curr_sel["intent"].get("max_height")
                            intent_preset_id = curr_sel["intent"].get("id")

                formats_list = info.get("formats")

                final_format, final_opts, intent_obj, verdict = resolve_format_with_guard(
                    format_str=original_format,
                    extra_opts=ydl_opts,
                    formats_list=formats_list,
                    intent_max_height=intent_max_height,
                    intent_preset_id=intent_preset_id,
                    download_type="video_audio",
                    source_path="simple",
                )

                ydl_opts = final_opts
                ydl_opts["format"] = final_format

                if verdict and not verdict.passed:
                    if not hasattr(self, "_preflight_warnings"):
                        self._preflight_warnings = []
                    self._preflight_warnings.append((title, verdict))

            if section_enabled and section_selector:
                time_range = section_selector.get_time_range()
                if time_range is None:
                    raise ValueError(self.tr("视频裁切时间范围无效"))
                ydl_opts.update(build_section_opts(time_range, section_selector.get_cut_mode()))
                layout, estimated_bytes = _section_stream_metadata(
                    info.get("formats"),
                    str(ydl_opts.get("format") or ""),
                    time_range.duration_seconds or 0.0,
                    float(info.get("duration") or 0.0),
                )
                ydl_opts["__fluentytdl_section_stream_layout"] = layout
                ydl_opts["__fluentytdl_section_estimated_bytes"] = estimated_bytes
                ydl_opts["writesubtitles"] = False
                ydl_opts["writeautomaticsub"] = False
                ydl_opts["embedsubtitles"] = False
                ydl_opts["outtmpl"] = f"%(title)s{section_filename_suffix(time_range)}.%(ext)s"

            self._apply_download_dir_to_opts(ydl_opts)

            tasks.append((title, url, ydl_opts, thumb))
            return tasks

        # 2. Playlist Mode
        if getattr(self, "_twitter_selectors", None) is not None:
            for entry, selector in self._twitter_selectors:
                url = _infer_entry_url(entry) or self.url
                title = entry.get("title") or self.tr("未命名推文媒体")
                thumb = entry.get("thumbnail") or ""

                ydl_opts = {}
                ydl_opts["writethumbnail"] = False
                ydl_opts["embedthumbnail"] = False
                ydl_opts["addmetadata"] = False
                ydl_opts["writesubtitles"] = False
                ydl_opts["embedsubtitles"] = False
                ydl_opts["__fluentytdl_keep_thumbnail"] = False

                sel = selector.get_selection_result()
                if sel and sel.get("format"):
                    ydl_opts["format"] = sel["format"]
                    ydl_opts.update(sel.get("extra_opts") or {})
                    try:
                        ydl_opts["__fluentytdl_format_note"] = selector.get_summary_text()
                    except Exception:
                        pass
                else:
                    ydl_opts["format"] = "bestvideo+bestaudio/best"
                    ydl_opts["__fluentytdl_format_note"] = self.tr("最佳画质")

                self._apply_download_dir_to_opts(ydl_opts)
                tasks.append((title, url, ydl_opts, thumb))
            return tasks

        import copy

        from ....core.config_manager import config_manager

        # Prepare Overrides
        pl_sub_override = copy.deepcopy(config_manager.get_subtitle_config())
        if self._mode == "subtitle" and hasattr(self, "playlist_subtitle_format_combo"):
            pl_sub_override.enabled = True
            pl_sub_override.output_format = self.playlist_subtitle_format_combo.currentText()

            if hasattr(self, "playlist_subtitle_lang_combo"):
                lang_code = self.playlist_subtitle_lang_combo.itemData(
                    self.playlist_subtitle_lang_combo.currentIndex()
                )
                if lang_code and lang_code != "__smart__":
                    pl_sub_override.default_languages = [lang_code]
                else:
                    pl_sub_override.default_languages = []
        elif hasattr(self, "playlist_subtitle_check"):
            pl_sub_override.enabled = self.playlist_subtitle_check.isChecked()

        pl_cover_enabled = True
        pl_embed_enabled = True
        if hasattr(self, "playlist_cover_check"):
            pl_cover_enabled = self.playlist_cover_check.isChecked()
            pl_embed_enabled = getattr(
                self, "playlist_embed_check", self.playlist_cover_check
            ).isChecked()
        else:
            pl_cover_enabled = bool(config_manager.get("download_thumbnail", False))
            pl_embed_enabled = bool(config_manager.get("embed_thumbnail", False))

        pl_meta_enabled = True
        if hasattr(self, "playlist_metadata_check"):
            pl_meta_enabled = self.playlist_metadata_check.isChecked()
        else:
            pl_meta_enabled = bool(config_manager.get("embed_metadata", True))

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

        for _i, row_data in enumerate(self._playlist_rows):
            if _i > 0 and _i % 20 == 0:
                from PySide6.QtWidgets import QApplication

                QApplication.processEvents()

            if not row_data.get("selected"):
                continue

            url = str(row_data.get("url"))
            title = str(row_data.get("title"))
            thumb = str(row_data.get("thumbnail"))

            row_opts = {}

            if self._mode == "subtitle":
                row_opts["skip_download"] = True
                row_opts["writethumbnail"] = False
                row_opts["embedthumbnail"] = False
                row_opts["addmetadata"] = False
                row_opts["embedsubtitles"] = False
                row_opts["sponsorblock_remove"] = None
                row_opts["sponsorblock_mark"] = None

                custom_sel = row_data.get("custom_selection_data") or {}

                if custom_sel.get("extra_opts"):
                    # Use explicit interactive options
                    row_opts.update(custom_sel["extra_opts"])
                else:
                    if row_data.get("detail"):
                        sub_opts = subtitle_service.apply(
                            video_id=str(row_data.get("id")),
                            video_info=row_data["detail"],
                            user_config=pl_sub_override,
                        )
                        row_opts.update(sub_opts)
                    else:
                        if pl_sub_override.enabled:
                            row_opts["writesubtitles"] = True
                            row_opts["writeautomaticsub"] = pl_sub_override.enable_auto_captions
                            row_opts["subtitleslangs"] = pl_sub_override.default_languages
                            if pl_sub_override.embed_type == "external":
                                if pl_sub_override.output_format:
                                    row_opts["convertsubtitles"] = pl_sub_override.output_format

                self._apply_download_dir_to_opts(row_opts)
                tasks.append((f"[字幕] {title}", url, row_opts, thumb))
                continue

            elif self._mode == "cover":
                custom_sel = row_data.get("custom_selection_data") or {}
                if custom_sel.get("cover_url"):
                    url = custom_sel["cover_url"]
                    # 这是一个直接的图片 URL，清除所有视频相关的冗余配置
                    keys_to_remove = list(row_opts.keys())
                    for k in keys_to_remove:
                        if k not in ["paths", "outtmpl"]:
                            row_opts.pop(k, None)
                    row_opts["__fluentytdl_is_cover_direct"] = True
                    if custom_sel.get("cover_ext"):
                        row_opts["outtmpl"] = (
                            f"{sanitize_filename(title)}.{custom_sel['cover_ext']}"
                        )
                    else:
                        row_opts["outtmpl"] = f"{sanitize_filename(title)}.%(ext)s"
                else:
                    row_opts["skip_download"] = True
                    row_opts["writethumbnail"] = True
                    row_opts["embedthumbnail"] = False
                    row_opts["addmetadata"] = False
                    row_opts["embedsubtitles"] = False
                    row_opts["sponsorblock_remove"] = None
                    row_opts["sponsorblock_mark"] = None
                    row_opts["postprocessors"] = []
                    safe_title = sanitize_filename(title)
                    row_opts["outtmpl"] = f"{safe_title}.%(ext)s"

                self._apply_download_dir_to_opts(row_opts)
                tasks.append((f"[封面] {title}", url, row_opts, thumb))
                continue

            # VR 模式注入
            if self._vr_mode:
                row_opts["__fluentytdl_use_android_vr"] = True
                # 如果详情已加载，传递 VR 格式 ID 以供过滤
                if row_data.get("detail"):
                    row_opts["__android_vr_format_ids"] = row_data["detail"].get(
                        "__android_vr_format_ids", []
                    )

            # Determine Base Options (Format/Quality)
            if row_data.get("custom_selection_data"):
                # Custom Selection
                sel = row_data.get("custom_selection_data")
                if sel and sel.get("format"):
                    row_opts["format"] = sel["format"]
                    row_opts.update(sel.get("extra_opts") or {})
                    if row_data.get("custom_summary"):
                        row_opts["__fluentytdl_format_note"] = row_data["custom_summary"]

            elif self._vr_mode:
                # VR Auto/Simple
                preset_title = (
                    self.preset_combo.currentText()
                    if getattr(self, "preset_combo", None)
                    else self.tr("VR 模式")
                )
                if bool(row_data.get("manual_override")) and row_data.get("override_format_id"):
                    row_opts["format"] = f"{row_data['override_format_id']}+bestaudio/best"
                    row_opts["__fluentytdl_format_note"] = row_data.get("override_text") or self.tr(
                        "自定义"
                    )
                else:
                    row_opts["format"] = vr_preset_fmt or "bestvideo+bestaudio/best"
                    row_opts.update(vr_preset_args)
                    row_opts["__fluentytdl_format_note"] = preset_title

            elif getattr(self, "_playlist_format_override", None) is not None:
                # Playlist Global Format Config
                from fluentytdl.ui.components.platforms.youtube import resolve_global_format

                fmt_str, e_opts = resolve_global_format(
                    row_data.get("detail"), self._playlist_format_override
                )
                row_opts["format"] = fmt_str
                row_opts.update(e_opts)

                pid = getattr(self._playlist_format_override, "preset_id", None)
                preset_map = {
                    "best_mp4": self.tr("最佳画质"),
                    "best_raw": self.tr("最佳画质(原盘)"),
                    "2160p": "2160p",
                    "1440p": "1440p",
                    "1080p": "1080p",
                    "720p": "720p",
                    "480p": "480p",
                    "360p": "360p",
                    "best_video": self.tr("最佳质量(无声)"),
                    "1080p_video": self.tr("1080p(无声)"),
                    "audio_best": self.tr("最佳音质"),
                    "audio_high": self.tr("高品质音频"),
                    "audio_std": self.tr("标准音频"),
                }
                preset_name = preset_map.get(pid, pid) if pid else self.tr("全局格式")
                row_opts["__fluentytdl_format_note"] = f"[全局] {preset_name}"

            else:
                # Standard Playlist Logic
                ov_fid = row_data.get("override_format_id")
                aud_fid = row_data.get("audio_best_format_id")
                aud_manual_fid = row_data.get("audio_override_format_id")

                mode = int(self.type_combo.currentIndex()) if self.type_combo else 0

                if mode == 2:  # Audio only
                    row_opts["__fluentytdl_format_note"] = self.tr("纯音频")
                    if aud_manual_fid:
                        row_opts["format"] = aud_manual_fid
                        row_opts["__fluentytdl_format_note"] = row_data.get(
                            "audio_override_text"
                        ) or self.tr("自定义音频")
                    elif aud_fid:
                        row_opts["format"] = aud_fid
                    else:
                        row_opts["format"] = "bestaudio/best"
                    row_opts["extract_audio"] = True

                elif mode == 1:  # Video only
                    if ov_fid:
                        row_opts["format"] = ov_fid
                        row_opts["__fluentytdl_format_note"] = row_data.get(
                            "override_text"
                        ) or self.tr("自定义视频")
                    else:
                        h = self._current_playlist_preset_height()
                        if h:
                            row_opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
                            row_opts["__fluentytdl_format_note"] = f"{h}p"
                        else:
                            row_opts["format"] = "bestvideo+bestaudio/best"
                            row_opts["__fluentytdl_format_note"] = self.tr("最佳画质")

                else:  # AV Muxed
                    if ov_fid:
                        target_audio = (
                            aud_manual_fid if row_data.get("audio_manual_override") else aud_fid
                        )
                        row_opts["__fluentytdl_format_note"] = row_data.get(
                            "override_text"
                        ) or self.tr("自定义")
                        if target_audio:
                            row_opts["format"] = f"{ov_fid}+{target_audio}"
                            row_opts["merge_output_format"] = "mkv"
                        else:
                            row_opts["format"] = f"{ov_fid}+bestaudio/best"
                    else:
                        h = self._current_playlist_preset_height()
                        if h:
                            row_opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
                            row_opts["merge_output_format"] = "mkv"
                            row_opts["__fluentytdl_format_note"] = f"{h}p"
                        else:
                            row_opts["format"] = "bestvideo+bestaudio/best"
                            row_opts["__fluentytdl_format_note"] = self.tr("最佳画质")

            # === Apply Common Overrides (Sub/Cover/Meta) ===

            # 1. Cover & Metadata
            if pl_cover_enabled or pl_embed_enabled:
                row_opts["writethumbnail"] = True
                row_opts["convert_thumbnail"] = "jpg"
            else:
                row_opts["writethumbnail"] = False
            row_opts["embedthumbnail"] = pl_embed_enabled
            row_opts["__fluentytdl_keep_thumbnail"] = pl_cover_enabled
            row_opts["addmetadata"] = pl_meta_enabled

            # 2. Subtitles
            # 优先级: 单视频覆盖 > 整体覆盖 > 全局配置
            sub_override = row_data.get("subtitle_override")
            if sub_override:
                # 单视频配置 (Phase 2)
                row_opts["writesubtitles"] = sub_override.get("has_manual", True)
                row_opts["writeautomaticsub"] = sub_override.get("has_auto", False)
                row_opts["subtitleslangs"] = sub_override.get("override_languages", [])
                row_opts["embedsubtitles"] = sub_override.get("embed_subtitles", True)
                if sub_override.get("output_format"):
                    row_opts["convertsubtitles"] = sub_override["output_format"]
            elif self._playlist_sub_override is not None:
                # 整体覆盖 (Phase 1)
                override = self._playlist_sub_override
                row_opts["writesubtitles"] = True
                row_opts["writeautomaticsub"] = override.enable_auto_captions
                row_opts["subtitleslangs"] = override.target_languages
                row_opts["embedsubtitles"] = override.embed_subtitles
                if override.output_format:
                    row_opts["convertsubtitles"] = override.output_format
            else:
                # If we have details, use the service to resolve smart logic (e.g. check available languages)
                # If not, blindly apply config to ensure at least default behavior
                if row_data.get("detail"):
                    sub_opts = subtitle_service.apply(
                        video_id=str(row_data.get("id")),
                        video_info=row_data["detail"],
                        user_config=pl_sub_override,
                    )
                    row_opts.update(sub_opts)
                else:
                    # Fallback: manual injection
                    if pl_sub_override.enabled:
                        row_opts["writesubtitles"] = True
                        row_opts["writeautomaticsub"] = pl_sub_override.enable_auto_captions
                        row_opts["subtitleslangs"] = pl_sub_override.default_languages

                        # Embed
                        if pl_sub_override.embed_type == "soft":
                            row_opts["embedsubtitles"] = pl_sub_override.embed_mode != "never"
                        elif pl_sub_override.embed_type == "external":
                            row_opts["embedsubtitles"] = False
                            if pl_sub_override.output_format:
                                row_opts["convertsubtitles"] = pl_sub_override.output_format

            self._apply_download_dir_to_opts(row_opts)

            # === Quality Guard Pre-flight Check (Playlist/Channel) ===
            if self._mode not in ("subtitle", "cover") and not row_opts.get("skip_download"):
                from ....download.quality_guard import resolve_format_with_guard

                original_format = row_opts.get("format", "bestvideo+bestaudio/best")

                intent_max_height = None
                intent_preset_id = None
                download_type = "video_audio"
                source_path = "playlist_standard"

                mode = (
                    int(self.type_combo.currentIndex()) if getattr(self, "type_combo", None) else 0
                )
                if mode == 1:
                    download_type = "video_only"
                elif mode == 2:
                    download_type = "audio_only"

                if row_data.get("custom_selection_data"):
                    sel = row_data["custom_selection_data"]
                    if sel.get("intent"):
                        intent_max_height = sel["intent"].get("max_height")
                        intent_preset_id = sel["intent"].get("preset_id") or sel["intent"].get("id")
                        source_path = "simple"
                elif getattr(self, "_playlist_format_override", None) is not None:
                    po = self._playlist_format_override
                    if getattr(po, "preset_intent", None):
                        intent_max_height = po.preset_intent.get("max_height")
                        intent_preset_id = po.preset_intent.get(
                            "preset_id"
                        ) or po.preset_intent.get("id")
                    source_path = "playlist_global"
                    if getattr(po, "download_type", None) == "audio_only":
                        download_type = "audio_only"
                    elif getattr(po, "download_type", None) == "video_only":
                        download_type = "video_only"
                else:
                    h = self._current_playlist_preset_height()
                    if h:
                        intent_max_height = h
                        intent_preset_id = f"{h}p"

                formats_list = None
                if row_data.get("detail"):
                    formats_list = row_data["detail"].get("formats")

                if row_data.get("detail") and "__fluentytdl_tab" in row_data["detail"]:
                    source_path = "channel"

                final_format, final_opts, intent_obj, verdict = resolve_format_with_guard(
                    format_str=original_format,
                    extra_opts=row_opts,
                    formats_list=formats_list,
                    intent_max_height=intent_max_height,
                    intent_preset_id=intent_preset_id,
                    download_type=download_type,
                    source_path=source_path,
                )

                row_opts = final_opts
                row_opts["format"] = final_format

                if verdict and not verdict.passed:
                    if not hasattr(self, "_preflight_warnings"):
                        self._preflight_warnings = []
                    self._preflight_warnings.append((title, verdict))

            tasks.append((title, url, row_opts, thumb))

        return tasks

    def _update_style(self):
        from PySide6.QtGui import QColor
        from qfluentwidgets import isDarkTheme

        color = QColor(32, 32, 32) if isDarkTheme() else QColor(243, 243, 243)
        self.setStyleSheet(f"DownloadConfigWindow {{ background-color: {color.name()}; }}")
        if hasattr(self.titleBar, "updateStyle"):
            self.titleBar.updateStyle()
