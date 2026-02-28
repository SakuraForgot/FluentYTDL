from __future__ import annotations

import time
from collections import deque
from functools import partial
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
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
    SubtitleLabel,
    SwitchButton,
)
from qframelesswindow import FramelessWindow

from ...download.workers import EntryDetailWorker, InfoExtractWorker, VRInfoExtractWorker
from ...processing import subtitle_service
from ...utils.filesystem import sanitize_filename
from ...utils.image_loader import get_image_loader
from ...utils.paths import resource_path
from ...youtube.youtube_service import YoutubeServiceOptions
from .cover_selector import CoverSelectorWidget
from .format_selector import VideoFormatSelectorWidget
from .selection_dialog import (
    PlaylistActionWidget,
    PlaylistFormatDialog,
    PlaylistInfoWidget,
    PlaylistPreviewWidget,
    _clean_audio_formats,
    _clean_video_formats,
    _ensure_subtitle_compatible_container,
    _format_duration,
    _format_size,
    _format_upload_date,
    _get_table_selection_qss,
    _infer_entry_thumbnail,
    _infer_entry_url,
)
from .subtitle_selector import SubtitleSelectorWidget
from .vr_format_selector import VR_PRESETS, VRFormatSelectorWidget


class DownloadConfigWindow(FramelessWindow):
    """
    独立非模态下载配置窗口
    """

    downloadRequested = Signal(list)  # 发送任务列表 [tasks]
    windowClosed = Signal(object)  # 发送自身引用，用于主窗口清理

    request_vr_switch = Signal(str)  # 请求切换到 VR 模式
    request_normal_switch = Signal(str)  # 请求切换回普通模式

    def __init__(
        self,
        url: str,
        parent=None,
        *,
        vr_mode: bool = False,
        mode: str = "default",
        smart_detect: bool = False,
    ):
        # parent=None ensures independent window behavior (taskbar icon, not always-on-top of main)
        super().__init__(parent=None)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)

        self.url = url
        self._vr_mode = vr_mode or (mode == "vr")
        self._mode = mode
        self._smart_detect = smart_detect
        self.video_info: dict[str, Any] | None = None
        try:
            from ...core.config_manager import config_manager

            self._download_dir = str(config_manager.get("download_dir") or "").strip()
        except Exception:
            self._download_dir = ""
        self._download_dir_edit: LineEdit | None = None

        self.titleBar.raise_()

        # === UI Init ===
        self.setWindowTitle("新建任务")
        self.resize(760, 520)

        # Center on parent if available
        if parent:
            geo = parent.geometry()
            x = geo.center().x() - self.width() // 2
            y = geo.center().y() - self.height() // 2
            self.move(x, y)

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

        self.cancelButton = PushButton("取消", self)
        self.yesButton = PrimaryPushButton("下载", self)
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
        self.download_tasks: list[dict[str, Any]] = []

        self._subtitle_embed_choice: bool | None = None
        self._subtitle_choice_made = False

        # P4: 使用全局单例，所有窗口共享同一个 NetworkManager
        self.image_loader = get_image_loader()
        self.image_loader.loaded.connect(self._on_thumb_loaded)
        self.image_loader.loaded_with_url.connect(self._on_thumb_loaded_with_url)
        self.image_loader.failed.connect(self._on_thumb_failed)

        self.thumb_label: ImageLabel | None = None

        # playlist UI state
        self._playlist_rows: list[dict[str, Any]] = []
        self._table: QTableWidget | None = None
        self._thumb_label_by_row: dict[int, QLabel] = {}
        self._preview_widget_by_row: dict[int, PlaylistPreviewWidget] = {}
        self._action_widget_by_row: dict[int, PlaylistActionWidget] = {}
        self._thumb_cache: dict[str, Any] = {}
        self._thumb_url_to_rows: dict[str, set[int]] = {}
        self._thumb_requested: set[str] = set()
        self._thumb_pending: deque[str] = deque()  # O(1) popleft
        self._thumb_inflight: int = 0
        self._thumb_max_concurrent: int = 12

        self._detail_queue: deque[int] = deque()
        self._detail_inflight_row: int | None = None
        self._detail_loaded: set[int] = set()
        self._detail_retry_count: dict[int, int] = {}  # row -> 已重试次数
        self._last_interaction = time.monotonic()
        self._lazy_paused: bool = False  # 用户手动暂停后台解析

        self._idle_timer = QTimer(self)
        self._idle_timer.setInterval(2000)
        self._idle_timer.timeout.connect(self._on_idle_tick)

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

        # 内容容器
        self.contentWidget = QWidget()
        self.contentLayout = QVBoxLayout(self.contentWidget)
        self.contentLayout.setContentsMargins(0, 0, 0, 0)
        self.contentLayout.setSpacing(12)
        self.viewLayout.addWidget(self.contentWidget)
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

        # --- 面板 1: DLE 登录 ---
        dle_panel = QWidget()
        dle_lay = QVBoxLayout(dle_panel)
        dle_lay.setContentsMargins(0, 4, 0, 0)
        dle_lay.setSpacing(6)
        dle_hint = CaptionLabel(
            "将打开独立浏览器窗口，请登录 YouTube 账号。\n登录完成后将自动提取 Cookie 并重新解析。",
            dle_panel,
        )
        dle_lay.addWidget(dle_hint)
        self._dleRetryBtn = PrimaryPushButton("登录 YouTube 并重试", dle_panel)
        self._dleRetryBtn.clicked.connect(self._on_dle_retry_clicked)
        dle_lay.addWidget(self._dleRetryBtn)
        self._dleStatusLabel = CaptionLabel("", dle_panel)
        dle_lay.addWidget(self._dleStatusLabel)
        self._authStack.addWidget(dle_panel)

        # --- 面板 2: 浏览器提取 ---
        extract_panel = QWidget()
        extract_lay = QVBoxLayout(extract_panel)
        extract_lay.setContentsMargins(0, 4, 0, 0)
        extract_lay.setSpacing(6)
        extract_hint = CaptionLabel(
            "从本地已登录的浏览器中直接提取 Cookie。\n"
            "Chromium 内核浏览器 (Edge/Chrome) 可能需要管理员权限。",
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
                "百分浏览器 (Cent)",
            ]
        )
        self._extractRetryBtn = PrimaryPushButton("提取并重试", extract_row)
        self._extractRetryBtn.clicked.connect(self._on_extract_retry_clicked)
        extract_h.addWidget(self._extractCombo, 1)
        extract_h.addWidget(self._extractRetryBtn)
        extract_lay.addWidget(extract_row)
        self._authStack.addWidget(extract_panel)

        # --- 面板 3: 手动导入 ---
        import_panel = QWidget()
        import_lay = QVBoxLayout(import_panel)
        import_lay.setContentsMargins(0, 4, 0, 0)
        import_lay.setSpacing(6)
        import_hint = CaptionLabel(
            "选择已有的 cookies.txt 文件 (Netscape 格式)。\n"
            "可使用浏览器扩展 (如 Get cookies.txt LOCALLY) 导出。",
            import_panel,
        )
        import_lay.addWidget(import_hint)
        self._importRetryBtn = PrimaryPushButton("选择文件并重试", import_panel)
        self._importRetryBtn.clicked.connect(self._on_import_retry_clicked)
        import_lay.addWidget(self._importRetryBtn)
        self._authStack.addWidget(import_panel)

        # 绑定分段选择器
        self._authSegment.addItem(
            routeKey="dle", text="🔑 登录", onClick=lambda: self._authStack.setCurrentIndex(0)
        )
        self._authSegment.addItem(
            routeKey="extract", text="🚀 提取", onClick=lambda: self._authStack.setCurrentIndex(1)
        )
        self._authSegment.addItem(
            routeKey="import", text="📄 导入", onClick=lambda: self._authStack.setCurrentIndex(2)
        )
        self._authSegment.setCurrentItem("dle")

        self.viewLayout.addWidget(self.retryWidget)
        self.retryWidget.hide()

        # ========== 网络诊断面板 ==========
        self.networkDiagWidget = QWidget(self)
        net_layout = QVBoxLayout(self.networkDiagWidget)
        net_layout.setContentsMargins(0, 8, 0, 0)
        net_layout.setSpacing(8)
        self._netDiagLabel = CaptionLabel(
            "无法正常访问 YouTube，可能的原因：\n"
            "• 未配置或未启动代理软件\n"
            "• 代理节点被 YouTube 封锁 / 限流\n"
            "• DNS 被污染或 SSL 证书被干扰",
            self.networkDiagWidget,
        )
        net_layout.addWidget(self._netDiagLabel)
        net_btn_row = QWidget(self.networkDiagWidget)
        net_btn_h = QHBoxLayout(net_btn_row)
        net_btn_h.setContentsMargins(0, 0, 0, 0)
        net_btn_h.setSpacing(8)
        self._openProxyBtn = PushButton("打开代理设置", net_btn_row)
        self._openProxyBtn.clicked.connect(self._on_open_proxy_settings)
        self._probeBtn = PrimaryPushButton("检测网络连通性", net_btn_row)
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

        # 启动解析
        self.start_extraction()

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

    def _build_single_option_switches(self) -> QWidget:
        from ...core.config_manager import config_manager

        container = QWidget(self.contentWidget)
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(18)

        title = CaptionLabel("下载选项", container)
        layout.addWidget(title)

        def _add_toggle(text: str, checked: bool) -> SwitchButton:
            wrap = QWidget(container)
            row = QHBoxLayout(wrap)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(8)
            label = CaptionLabel(text, wrap)
            toggle = SwitchButton(wrap)
            toggle.setChecked(checked)
            row.addWidget(label)
            row.addWidget(toggle)
            layout.addWidget(wrap)
            return toggle

        sub_enabled = config_manager.get_subtitle_config().enabled
        self.subtitle_check = _add_toggle("下载字幕", sub_enabled)

        thumb_enabled = bool(config_manager.get("embed_thumbnail", True))
        self.cover_check = _add_toggle("下载封面", thumb_enabled)

        meta_enabled = bool(config_manager.get("embed_metadata", True))
        self.metadata_check = _add_toggle("下载元数据", meta_enabled)

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
            tasks = self.get_selected_tasks()
            if tasks:
                self.downloadRequested.emit(tasks)
                self.close()
        except Exception as e:
            # TODO: Show error
            print(f"Error getting tasks: {e}")

    def _apply_dialog_size_for_mode(self) -> None:
        if self._is_playlist:
            size = (980, 620)
        elif self._vr_mode:
            size = (880, 620)
        else:
            size = (760, 520)

        self.resize(*size)

    def _stop_background_parsing(self) -> None:
        if self._is_closing:
            return
        self._is_closing = True
        try:
            self._idle_timer.stop()
            self._detail_queue.clear()
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

    def start_extraction(self) -> None:
        self._is_closing = False
        try:
            if self.worker:
                self.worker.cancel()
        except Exception:
            pass

        self._set_loading_ui(
            "正在使用 VR 模式解析..." if self._vr_mode else "正在解析链接...",
            show_ring=True,
        )
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

    def _check_is_vr_content(self, info: dict[str, Any]) -> bool:
        # 1. 检查 projection 字段
        proj = str(info.get("projection") or "").lower()
        if proj in ("equirectangular", "mesh", "360", "vr180"):
            return True

        # 2. 检查标签
        tags = [str(t).lower() for t in (info.get("tags") or [])]
        if any(k in tags for k in ("360 video", "vr video", "360°", "vr180")):
            return True

        # 3. 检查标题
        title = str(info.get("title") or "").lower()
        keywords = (
            "360 video",
            "360 movie",
            "vr 360",
            "360°",
            "vr180",
            "180 vr",
            "3d 180",
            "3d 360",
        )
        if any(k in title for k in keywords):
            return True

        return False

    def _ask_switch_to_normal(self) -> bool:
        """询问用户是否切换回普通模式"""
        box = MessageBox(
            "检测到普通视频",
            "检测到当前链接似乎是普通视频，但您正在使用 VR 模式解析。\n\n"
            "VR 模式可能无法正确获取普通视频的格式，且不支持部分功能。\n"
            "建议切换回普通模式。",
            self,
        )
        box.yesButton.setText("切换回普通模式")
        box.cancelButton.setText("保持 VR 模式")
        return bool(box.exec())

    def on_parse_success(self, info: dict[str, Any]) -> None:
        if self._is_closing:
            return

        # === 智能 VR 检测 ===
        if self._smart_detect:
            is_vr = self._check_is_vr_content(info)

            # 1. 普通模式 -> VR 模式
            if not self._vr_mode and is_vr:
                self.request_vr_switch.emit(self.url)
                self.close()
                return

            # 2. VR 模式 -> 普通模式
            elif self._vr_mode and not is_vr:
                if self._ask_switch_to_normal():
                    self.request_normal_switch.emit(self.url)
                    self.close()
                    return
        # ===================

        self.video_info = info
        self.loadingWidget.hide()
        self.retryWidget.hide()

        # 解析成功 → 重置连续失败计数
        try:
            from ...auth.cookie_probe_throttle import cookie_probe_throttle

            cookie_probe_throttle.record_download_success()
        except Exception:
            pass
        if self._error_label:
            self._error_label.deleteLater()
            self._error_label = None

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
            self.titleLabel.hide()
            self.yesButton.setEnabled(True)
            self.setup_content_ui(info)

        self.contentWidget.show()

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
            from ...auth.auth_service import AuthSourceType, auth_service
            from ...auth.cookie_sentinel import cookie_sentinel

            if auth_service.current_source == AuthSourceType.NONE:
                return  # 未启用验证，不预检

            if not cookie_sentinel.exists:
                self._cookieWarningLabel.setText(
                    "⚠️ 尚未获取 Cookie — 解析可能因登录要求而失败。"
                    "建议先在「设置 > 账户」中获取 Cookie。"
                )
                self._cookieWarningLabel.show()
                return

            info = cookie_sentinel.get_status_info()

            if not info.get("cookie_valid"):
                msg = info.get("cookie_valid_msg", "Cookie 无效")
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
                    self._cookieWarningLabel.setText("⚠️ Cookie 已过期，建议前往设置页刷新。")
                self._cookieWarningLabel.show()
        except Exception:
            pass  # 预检失败不影响正常流程

    def on_parse_error(self, err_data: dict) -> None:
        if self._is_closing:
            return
        self.loadingWidget.hide()
        self.titleLabel.setText("解析失败")
        self.titleLabel.show()
        if self._error_label:
            self._error_label.deleteLater()

        raw_error = str(err_data.get("raw_error") or "")

        from ...utils.error_parser import ErrorCategory, classify_error, parse_ytdlp_error

        friendly_title, friendly_content = parse_ytdlp_error(raw_error)
        category = classify_error(raw_error) if raw_error else ErrorCategory.OTHER

        title = friendly_title if raw_error else str(err_data.get("title") or "解析失败")
        content = friendly_content if raw_error else str(err_data.get("content") or "")
        suggestion = str(err_data.get("suggestion") or "")

        text = f"{title}\n\n{content}"
        if suggestion and not raw_error:
            text += f"\n\n建议操作：\n{suggestion}"

        self._error_label = CaptionLabel(text, self)
        try:
            self._error_label.setWordWrap(True)
        except Exception:
            pass
        self.viewLayout.addWidget(self._error_label)

        # === 根据分类决定显示哪个面板 ===
        if category == ErrorCategory.COOKIE:
            self.retryWidget.show()
            self.networkDiagWidget.hide()
            # 记录失败 + 渐进式引导
            try:
                from ...auth.cookie_probe_throttle import cookie_probe_throttle

                cookie_probe_throttle.record_download_failure("cookie")
                if cookie_probe_throttle.should_suggest_alternative:
                    alt_label = CaptionLabel(
                        f"⚠️ 已连续 {cookie_probe_throttle.consecutive_failures} 次 Cookie 失败，"
                        "建议切换到「🔑 登录」模式 或 使用 Firefox 浏览器提取",
                        self,
                    )
                    alt_label.setWordWrap(True)
                    alt_label.setStyleSheet(
                        "QLabel { color: #e65100; font-weight: bold; padding: 4px 0; }"
                    )
                    self.retryLayout.insertWidget(0, alt_label)
            except Exception:
                pass
            # POT 服务状态提示
            try:
                from ...core.config_manager import config_manager

                if config_manager.get("pot_provider_enabled", True):
                    from ...youtube.pot_manager import pot_manager

                    if not pot_manager.is_running():
                        pot_label = CaptionLabel(
                            "⚠️ POT Provider 服务未运行 — 这可能是触发机器人检测的主要原因。"
                            "请前往「设置 → 组件」检查 POT Provider 是否已安装。",
                            self,
                        )
                        pot_label.setWordWrap(True)
                        pot_label.setStyleSheet(
                            "QLabel { color: #c62828; font-weight: bold; padding: 4px 0; }"
                        )
                        self.retryLayout.insertWidget(0, pot_label)
            except Exception:
                pass
        elif category == ErrorCategory.NETWORK:
            self.retryWidget.hide()
            self.networkDiagWidget.show()
            self._netProbeResult.setText("")
            try:
                from ...auth.cookie_probe_throttle import cookie_probe_throttle

                cookie_probe_throttle.record_download_failure("network")
            except Exception:
                pass
        elif category == ErrorCategory.AMBIGUOUS:
            # 403 模糊情况 → 异步探测后决定
            self.retryWidget.hide()
            self.networkDiagWidget.hide()
            self._run_connectivity_probe(friendly_title, raw_error)
            try:
                from ...auth.cookie_probe_throttle import cookie_probe_throttle

                cookie_probe_throttle.record_download_failure("ambiguous")
            except Exception:
                pass
        else:
            self.retryWidget.hide()
            self.networkDiagWidget.hide()

        # Build Report Issue button
        if raw_error:
            if not hasattr(self, "reportBtn"):
                from qfluentwidgets import FluentIcon

                self.reportBtn = PushButton("反馈此错误", self)
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

        from ...utils.error_parser import generate_issue_url

        issue_url = generate_issue_url(title, raw_error)
        QDesktopServices.openUrl(QUrl(issue_url))

    def _on_open_proxy_settings(self) -> None:
        """打开代理设置（跳转主窗口设置页）"""

        # 尝试通知主窗口打开设置页
        try:
            parent = self.parent()
            while parent is not None:
                if hasattr(parent, "show_settings_network"):
                    parent.show_settings_network()
                    return
                parent = parent.parent()
        except Exception:
            pass
        # 兜底：显示提示
        self._netProbeResult.setText("请手动前往主界面「设置 > 网络连接」配置代理。")

    def _on_probe_connectivity(self) -> None:
        """用户手动点击检测连通性"""
        self._probeBtn.setEnabled(False)
        self._probeBtn.setText("检测中...")
        self._netProbeResult.setText("")

        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _ProbeWorker(QThread):
            finished = QSignal(bool)

            def run(self):
                from ...utils.error_parser import probe_youtube_connectivity

                result = probe_youtube_connectivity(timeout=8.0)
                self.finished.emit(result)

        self._probe_worker = _ProbeWorker(self)

        def _on_done(reachable: bool):
            self._probeBtn.setEnabled(True)
            self._probeBtn.setText("检测网络连通性")
            if reachable:
                self._netProbeResult.setText(
                    "✅ YouTube 可达 — 网络正常。\n"
                    "错误可能是 Cookie 失效导致，请尝试重新获取 Cookie。"
                )
                # 网络通但报了错 → 可能其实是 Cookie 问题，显示重试面板
                self.retryWidget.show()
            else:
                self._netProbeResult.setText("❌ 无法连接 YouTube — 请检查代理/VPN 是否正常运行。")

        self._probe_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._probe_worker.start()

    def _run_connectivity_probe(self, friendly_title: str, raw_error: str) -> None:
        """
        403 模糊错误 → 异步探测 YouTube 连通性后决定显示哪个面板。
        探测期间显示加载提示。
        """
        if self._error_label:
            self._error_label.setText(f"{friendly_title}\n\n正在诊断原因（检测网络连通性）...")

        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _ProbeWorker(QThread):
            finished = QSignal(bool)

            def run(self):
                from ...utils.error_parser import probe_youtube_connectivity

                result = probe_youtube_connectivity(timeout=8.0)
                self.finished.emit(result)

        self._ambig_probe_worker = _ProbeWorker(self)

        def _on_done(reachable: bool):
            if self._is_closing:
                return
            if reachable:
                # 网络通 → Cookie 问题
                if self._error_label:
                    self._error_label.setText(
                        "需要验证 (Cookie 缺失或失效)\n\n"
                        "网络连通正常，YouTube 拒绝了请求。\n"
                        "这通常表示 Cookie 已失效或未配置，请在下方重新获取。"
                    )
                self.retryWidget.show()
                self.networkDiagWidget.hide()
            else:
                # 网络不通 → 网络问题
                if self._error_label:
                    self._error_label.setText(
                        "网络连接异常\n\n"
                        "无法连接到 YouTube 服务器。\n"
                        "请检查代理/VPN 是否正常运行，或在下方进行网络诊断。"
                    )
                self.retryWidget.hide()
                self.networkDiagWidget.show()
                self._netProbeResult.setText("⚠️ 自动探测：无法连接 YouTube")

        self._ambig_probe_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._ambig_probe_worker.start()

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

        self._set_loading_ui("正在重试解析...", show_ring=True)

        if self._vr_mode:
            w = VRInfoExtractWorker(self.url)
        else:
            w = InfoExtractWorker(self.url, self._current_options)

        w.finished.connect(self.on_parse_success)
        w.error.connect(self.on_parse_error)
        self.worker = w
        w.start()

    def _on_dle_retry_clicked(self) -> None:
        """DLE 登录模式重试"""
        from ...auth.auth_service import AuthSourceType, auth_service
        from ...auth.cookie_sentinel import cookie_sentinel

        self._dleRetryBtn.setEnabled(False)
        self._dleRetryBtn.setText("正在启动浏览器...")
        self._dleStatusLabel.setText("请在浏览器中登录 YouTube 账号")

        # 切换到 DLE 模式
        auth_service.set_source(AuthSourceType.DLE, auto_refresh=False)

        # 在后台线程执行 DLE 登录
        from PySide6.QtCore import QThread
        from PySide6.QtCore import Signal as QSignal

        class _DLEWorker(QThread):
            finished = QSignal(bool, str)

            def run(self):
                try:
                    success, msg = cookie_sentinel.force_refresh_with_uac()
                    self.finished.emit(success, msg)
                except Exception as e:
                    self.finished.emit(False, str(e))

        self._dle_worker = _DLEWorker(self)

        def _on_done(success: bool, msg: str):
            self._dleRetryBtn.setEnabled(True)
            self._dleRetryBtn.setText("登录 YouTube 并重试")
            if success:
                self._dleStatusLabel.setText("✅ 登录成功，正在重新解析...")
                self._retry_parse_with_auth()
            else:
                clean = msg
                if clean.startswith("刷新异常: "):
                    clean = clean[len("刷新异常: ") :]
                self._dleStatusLabel.setText(f"❌ {clean}")

        self._dle_worker.finished.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        self._dle_worker.start()

    def _on_extract_retry_clicked(self) -> None:
        """浏览器提取模式重试"""
        from ...auth.auth_service import AuthSourceType, auth_service
        from ...auth.cookie_sentinel import cookie_sentinel

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
            self._extractRetryBtn.setText("提取并重试")
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
        from ...auth.auth_service import AuthSourceType, auth_service

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 Cookie 文件",
            "",
            "Cookie 文件 (*.txt);;所有文件 (*)",
        )
        if not file_path:
            return

        # 设置为文件模式
        auth_service.set_source(AuthSourceType.FILE, auto_refresh=False)
        auth_service._current_file_path = file_path

        from ...core.config_manager import config_manager

        config_manager.set("cookie_file_path", file_path)

        self._retry_parse_with_auth()

    # === 单视频 UI ===

    def setup_content_ui(self, info: dict[str, Any]) -> None:
        # 1. Top Info Card
        top_card = CardWidget(self.contentWidget)
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
        self.selector_widget = VideoFormatSelectorWidget(info, self.contentWidget)
        self.contentLayout.addWidget(self.selector_widget)
        self.options_container = self._build_single_option_switches()
        self.contentLayout.addWidget(self.options_container)

    def setup_vr_mode_ui(self, info: dict[str, Any]) -> None:
        self.selector_widget = VRFormatSelectorWidget(info, self.contentWidget)
        self.contentLayout.addWidget(self.selector_widget)
        self.options_container = self._build_single_option_switches()
        self.contentLayout.addWidget(self.options_container)

    def setup_subtitle_mode_ui(self, info: dict[str, Any]) -> None:
        self.yesButton.setText("下载字幕")
        self.selector_widget = SubtitleSelectorWidget(info, self.contentWidget)
        self.selector_widget.embedCheck.setChecked(False)
        self.selector_widget.embedCheck.hide()
        self.contentLayout.addWidget(self.selector_widget)

    def setup_cover_mode_ui(self, info: dict[str, Any]) -> None:
        self.yesButton.setText("下载封面")
        self.selector_widget = CoverSelectorWidget(info, self.contentWidget)
        self.contentLayout.addWidget(self.selector_widget)

    # === 播放列表 UI ===

    def setup_playlist_ui(self, info: dict[str, Any]) -> None:
        title = str(info.get("title") or "播放列表")
        count = 0
        entries = info.get("entries") or []
        if isinstance(entries, list):
            count = len(entries)

        self.titleLabel.setText(f"播放列表：{title}（{count} 条）")
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
        toolbar.setSpacing(12)

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

        # === 额外下载选项 (播放列表) ===
        from qfluentwidgets import CheckBox

        from ...core.config_manager import config_manager

        # 字幕开关
        sub_enabled = config_manager.get_subtitle_config().enabled
        self.playlist_subtitle_check = CheckBox("下载字幕", self.contentWidget)
        self.playlist_subtitle_check.setChecked(sub_enabled)

        # 封面开关
        thumb_enabled = bool(config_manager.get("embed_thumbnail", True))
        self.playlist_cover_check = CheckBox("下载封面", self.contentWidget)
        self.playlist_cover_check.setChecked(thumb_enabled)

        # 元数据开关
        meta_enabled = bool(config_manager.get("embed_metadata", True))
        self.playlist_metadata_check = CheckBox("下载元数据", self.contentWidget)
        self.playlist_metadata_check.setChecked(meta_enabled)

        # Add widgets to toolbar in a single row
        toolbar.addWidget(self.selectAllBtn)
        toolbar.addWidget(self.unselectAllBtn)
        toolbar.addWidget(self.invertSelectBtn)

        toolbar.addSpacing(16)
        toolbar.addWidget(self.playlist_subtitle_check)
        toolbar.addWidget(self.playlist_cover_check)
        toolbar.addWidget(self.playlist_metadata_check)

        toolbar.addSpacing(16)
        toolbar.addWidget(self.type_combo)
        toolbar.addWidget(self.preset_combo)
        toolbar.addWidget(self.applyPresetBtn)

        if self._mode in ("subtitle", "cover"):
            self.playlist_subtitle_check.hide()
            self.playlist_cover_check.hide()
            self.playlist_metadata_check.hide()
            self.type_combo.hide()
            self.preset_combo.hide()
            self.applyPresetBtn.hide()

        # 暂停 / 继续后台解析
        from qfluentwidgets import SwitchButton as _SwitchBtn

        self.lazyPauseBtn = _SwitchBtn(self.contentWidget)
        self.lazyPauseBtn.setText("暂停解析")
        self.lazyPauseBtn.setChecked(False)
        self.lazyPauseBtn.checkedChanged.connect(self._on_lazy_pause_changed)
        toolbar.addSpacing(12)
        toolbar.addWidget(self.lazyPauseBtn)

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

        # cell click for format picker
        table.cellClicked.connect(self._on_table_cell_clicked)

        # fill rows
        self._build_playlist_rows(info)
        self._refresh_progress_label()
        self._update_download_btn_state()

        # kick off progressive detail fill
        # 使用 0ms 延迟等 Qt 完成布局后取真实可见行
        self._idle_timer.start()
        from PySide6.QtCore import QTimer as _QT

        _QT.singleShot(0, self._enqueue_visible_as_initial)

        # 延迟加载缩略图
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
                    "custom_selection_data": None,
                    "custom_summary": None,
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

    def _on_table_cell_clicked(self, row: int, col: int) -> None:
        self._last_interaction = time.monotonic()
        if col != 2:  # In SelectionDialog it was 8? No, col 2 is action in my setup.
            # Wait, SelectionDialog _on_table_cell_clicked checked for col != 8?
            # SelectionDialog setColumnCount(3), so col indices are 0, 1, 2.
            # Maybe SelectionDialog had a different column count or I misread.
            # Ah, I see: `if col != 8: return` in SelectionDialog.
            # But here I set 3 columns.
            # Let's assume action column is 2.
            # But wait, action widget has a button, button click is handled by `_on_playlist_quality_clicked`.
            # `cellClicked` is for clicking the cell background.
            # If the user clicks the cell (not the button), we might want to trigger something.
            # For now, let's just ignore cell clicks if button handles it.
            pass
        # However, let's replicate logic if needed.
        # If I want to trigger picker on cell click too:
        if col == 2:
            self._on_playlist_quality_clicked(row)

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

        if self._mode == "subtitle":
            aw.set_loading(False)
            if data.get("custom_summary"):
                aw.qualityButton.setText(data["custom_summary"])
            else:
                aw.qualityButton.setText("独立字幕")
            aw.infoLabel.setText("")
            return
        elif self._mode == "cover":
            aw.set_loading(False)
            if data.get("custom_summary"):
                aw.qualityButton.setText(data["custom_summary"])
            else:
                aw.qualityButton.setText("独立封面")
            aw.infoLabel.setText("")
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

        if row not in self._detail_loaded:
            if mode == 2:
                aw.set_loading(False)
                aw.qualityButton.setText("音频(自动)")
                aw.infoLabel.setText("待解析大小")
                return
            aw.set_loading(True, "待加载")
            aw.infoLabel.setText("")
            return

        if data.get("custom_selection_data"):
            aw.set_loading(False)
            aw.qualityButton.setText(str(data.get("custom_summary") or "已自定义"))
            aw.infoLabel.setText("使用自定义配置")
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
                    else (data.get("audio_best_text") or "音频(自动)")
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
                    else (data.get("audio_best_text") or "音频-")
                )
                aw.qualityButton.setText(f"{chosen or '视频已选'} + {audio_brief}")
                chosen_fmt = None
                override_id = str(data.get("override_format_id") or "")
                for f in data.get("video_formats") or []:
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
                "视频 ", best.get("filesize") or best.get("filesize_approx"), best.get("ext")
            )
            a_line = ""
            if chosen_audio:
                a_line = "\n" + _format_info_line(
                    "音频 ",
                    chosen_audio.get("filesize") or chosen_audio.get("filesize_approx"),
                    chosen_audio.get("ext"),
                )

            aw.infoLabel.setText(v_line + a_line)
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
        if self.preset_combo is not None:
            self.preset_combo.setEnabled(index in (0, 1))
        for r in range(len(self._playlist_rows)):
            self._auto_apply_row_preset(r)
        self._update_download_btn_state()

    def _on_table_scrolled(self, _value: int) -> None:
        self._last_interaction = time.monotonic()
        self._reprioritize_detail_queue()  # 重锤定：可见区插队首
        self._load_thumbs_for_visible_rows()
        self._maybe_start_next_detail()

    def _visible_row_range(self) -> tuple[int, int]:
        table = self._table
        if table is None:
            return (0, -1)
        first = table.rowAt(0)
        if first < 0:
            first = 0
        last = table.rowAt(table.viewport().height() - 1)
        if last < 0:
            last = min(table.rowCount() - 1, first + 12)
        return (first, last)

    def _enqueue_visible_as_initial(self) -> None:
        """0ms 延迟后获取真实可见行并优先入队"""
        first, last = self._visible_row_range()
        rows = list(range(first, min(last + 4, len(self._playlist_rows))))
        self._enqueue_detail_rows(rows, priority=True)
        self._maybe_start_next_detail()

    def _reprioritize_detail_queue(self) -> None:
        """滚动后重锤定：可见区未加载行插队首，其余保留在尾"""
        first, last = self._visible_row_range()
        # 可见区 + 向下缓冲 5 行
        visible = list(range(max(0, first - 2), min(len(self._playlist_rows), last + 6)))
        # 保留当前队列中不属于可见区的行
        rest = [r for r in self._detail_queue if r not in visible]

        self._detail_queue.clear()
        # 可见区行按顶到底预加到队首（反序 appendleft 丽正序出队）
        for r in reversed(visible):
            if r not in self._detail_loaded and r != self._detail_inflight_row:
                self._detail_queue.appendleft(r)
        # 其余行追加到尾部
        for r in rest:
            if r not in self._detail_loaded and r != self._detail_inflight_row:
                self._detail_queue.append(r)

    def _enqueue_detail_for_visible_rows(self) -> None:
        first, last = self._visible_row_range()
        first = max(0, first - 3)
        last = min(len(self._playlist_rows) - 1, last + 6)
        rows = list(range(first, last + 1))
        self._enqueue_detail_rows(rows, priority=False)

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
            self._thumb_pending.append(url)  # deque.append
            self._thumb_requested.add(url)
        self._process_thumb_queue()

    def _process_thumb_queue(self) -> None:
        while self._thumb_pending and self._thumb_inflight < self._thumb_max_concurrent:
            url = self._thumb_pending.popleft()  # O(1) - deque
            self._thumb_inflight += 1
            self.image_loader.load(url, target_size=(150, 84), radius=8)

    def _load_thumbs_for_visible_rows(self) -> None:
        first, last = self._visible_row_range()
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

    def _on_thumb_loaded(self, pixmap) -> None:
        # Legacy callback for single video thumb
        if self.thumb_label:
            self.thumb_label.setPixmap(pixmap)

    def _on_thumb_failed(self, url: str) -> None:
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
            data["custom_selection_data"] = None
            data["custom_summary"] = None
            self._auto_apply_row_preset(row)
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
        for r in rows:
            if r < 0 or r >= len(self._playlist_rows):
                continue
            if r in self._detail_loaded:
                continue
            if self._detail_inflight_row == r:
                continue
            if r in self._detail_queue:
                continue
            if priority:
                self._detail_queue.appendleft(r)
            else:
                self._detail_queue.append(r)

    def _maybe_start_next_detail(self) -> None:
        if self._lazy_paused:
            return
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
            thumb = str(self._playlist_rows[row].get("thumbnail") or "").strip()
            if not thumb:
                thumb = _infer_entry_thumbnail(info)
                if thumb:
                    self._playlist_rows[row]["thumbnail"] = thumb
                    self._thumb_url_to_rows.setdefault(thumb, set()).add(row)
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
            self._auto_apply_row_preset(row)

        self._refresh_progress_label()
        self._update_download_btn_state()
        self._maybe_start_next_detail()

    def _on_detail_error(self, row: int, msg: str) -> None:
        if self._is_closing:
            return
        self._detail_inflight_row = None
        retries = self._detail_retry_count.get(row, 0)
        if retries < 1:
            # 自动重试一次，插入队首
            self._detail_retry_count[row] = retries + 1
            self._detail_queue.appendleft(row)
        else:
            aw = self._action_widget_by_row.get(row)
            if aw is not None:
                aw.set_loading(False, "解析失败(点重试)")
                aw.infoLabel.setText("")
                aw.qualityButton.setToolTip(msg)
        self._maybe_start_next_detail()

    def _on_idle_tick(self) -> None:
        if not self._is_playlist:
            return
        if self._lazy_paused:
            return
        if time.monotonic() - self._last_interaction < 2.0:
            return
        if self._detail_inflight_row is None and not self._detail_queue:
            # 一次入队最多 3 个未加载行，加速后台补全
            count = 0
            for i in range(len(self._playlist_rows)):
                if i not in self._detail_loaded:
                    self._detail_queue.append(i)
                    count += 1
                    if count >= 3:
                        break
        self._maybe_start_next_detail()

    def _on_lazy_pause_changed(self, checked: bool) -> None:
        """用户手动暂停/恢复后台详情解析"""
        self._lazy_paused = checked
        if hasattr(self, "lazyPauseBtn"):
            self.lazyPauseBtn.setText("已暂停" if checked else "暂停解析")
        if not checked:
            # 恢复：立即重锚点并继续
            self._reprioritize_detail_queue()
            self._maybe_start_next_detail()

    def _open_row_format_picker(self, row: int) -> None:
        if not (0 <= row < len(self._playlist_rows)):
            return
        if row not in self._detail_loaded:
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
                data["manual_override"] = True
                data["override_format_id"] = None
                data["override_text"] = None
                data["audio_override_format_id"] = None
                data["audio_override_text"] = None
                data["audio_manual_override"] = False
                self._auto_apply_row_preset(row)

    def get_selected_tasks(self) -> list[tuple[str, str, dict[str, Any], str | None]]:
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
                if isinstance(self.selector_widget, SubtitleSelectorWidget):
                    ydl_opts.update(self.selector_widget.get_opts())

                ydl_opts["skip_download"] = True
                ydl_opts["writethumbnail"] = False
                ydl_opts["embedthumbnail"] = False
                ydl_opts["addmetadata"] = False
                ydl_opts["embedsubtitles"] = False
                ydl_opts["sponsorblock_remove"] = None
                ydl_opts["sponsorblock_mark"] = None
                ydl_opts["postprocessors"] = []

                tasks.append((f"[字幕] {title}", url, ydl_opts, thumb))
                return tasks

            elif self._mode == "cover":
                if isinstance(self.selector_widget, CoverSelectorWidget):
                    url = self.selector_widget.get_selected_url() or url
                    _ = self.selector_widget.get_selected_ext()
                    ydl_opts["skip_download"] = False
                    ydl_opts["writethumbnail"] = False
                    ydl_opts["embedthumbnail"] = False
                    ydl_opts["addmetadata"] = False
                    ydl_opts["embedsubtitles"] = False
                    ydl_opts["sponsorblock_remove"] = None
                    ydl_opts["sponsorblock_mark"] = None
                    ydl_opts["postprocessors"] = []
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

                    # ========== VR 格式检测 ==========
                    vr_only_ids = info.get("__vr_only_format_ids") or []
                    android_vr_ids = info.get("__android_vr_format_ids") or []
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
            else:
                ydl_opts["format"] = "bestvideo+bestaudio/best"

            # Apply checkbox overrides if available (Default Mode)
            sub_config_override = None
            if hasattr(self, "subtitle_check"):
                import copy

                from ...core.config_manager import config_manager

                # Subtitles
                sub_config_override = copy.deepcopy(config_manager.get_subtitle_config())
                sub_config_override.enabled = self.subtitle_check.isChecked()

                # Cover
                is_cover = self.cover_check.isChecked()
                ydl_opts["writethumbnail"] = is_cover
                ydl_opts["embedthumbnail"] = is_cover

                # Metadata
                ydl_opts["addmetadata"] = self.metadata_check.isChecked()

            # 字幕集成
            if self.video_info:
                if self._subtitle_choice_made:
                    embed_override = self._subtitle_embed_choice
                else:
                    try:
                        embed_override = self._check_subtitle_and_ask(config=sub_config_override)
                    except ValueError as e:
                        print(f"[DEBUG] get_selected_tasks: User cancelled - {e}")
                        return []
                    except Exception as e:
                        print(
                            f"[ERROR] get_selected_tasks: Exception in _check_subtitle_and_ask - {e}"
                        )
                        embed_override = None

                subtitle_opts = subtitle_service.apply(
                    video_id=self.video_info.get("id", ""),
                    video_info=self.video_info,
                    user_config=sub_config_override,
                )
                ydl_opts.update(subtitle_opts)

                if embed_override is not None:
                    if sub_config_override:
                        embed_type = sub_config_override.embed_type
                    else:
                        from ...core.config_manager import config_manager as cfg

                        embed_type = cfg.get_subtitle_config().embed_type

                    if embed_type == "soft":
                        ydl_opts["embedsubtitles"] = embed_override
                    elif embed_type == "external":
                        ydl_opts["embedsubtitles"] = False

                _ensure_subtitle_compatible_container(ydl_opts)

            self._apply_download_dir_to_opts(ydl_opts)

            tasks.append((title, url, ydl_opts, thumb))
            return tasks

        # 2. Playlist Mode
        import copy

        from ...core.config_manager import config_manager

        # Prepare Overrides
        pl_sub_override = copy.deepcopy(config_manager.get_subtitle_config())
        if hasattr(self, "playlist_subtitle_check"):
            pl_sub_override.enabled = self.playlist_subtitle_check.isChecked()

        pl_cover_enabled = True
        if hasattr(self, "playlist_cover_check"):
            pl_cover_enabled = self.playlist_cover_check.isChecked()
        else:
            pl_cover_enabled = bool(config_manager.get("embed_thumbnail", True))

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
                row_opts["postprocessors"] = []

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
                                if pl_sub_override.format in ["srt", "ass", "vtt"]:
                                    row_opts["convertsubtitles"] = pl_sub_override.format

                self._apply_download_dir_to_opts(row_opts)
                tasks.append((f"[字幕] {title}", url, row_opts, thumb))
                continue

            elif self._mode == "cover":
                row_opts["skip_download"] = True
                row_opts["writethumbnail"] = True
                row_opts["embedthumbnail"] = False
                row_opts["addmetadata"] = False
                row_opts["embedsubtitles"] = False
                row_opts["sponsorblock_remove"] = None
                row_opts["sponsorblock_mark"] = None
                row_opts["postprocessors"] = []

                custom_sel = row_data.get("custom_selection_data") or {}
                if custom_sel.get("cover_url"):
                    url = custom_sel["cover_url"]
                    if custom_sel.get("cover_ext"):
                        row_opts["outtmpl"] = (
                            f"{sanitize_filename(title)}.{custom_sel['cover_ext']}"
                        )
                    else:
                        row_opts["outtmpl"] = f"{sanitize_filename(title)}.%(ext)s"
                else:
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

            elif self._vr_mode:
                # VR Auto/Simple
                if bool(row_data.get("manual_override")) and row_data.get("override_format_id"):
                    row_opts["format"] = f"{row_data['override_format_id']}+bestaudio/best"
                else:
                    row_opts["format"] = vr_preset_fmt or "bestvideo+bestaudio/best"
                    row_opts.update(vr_preset_args)

            else:
                # Standard Playlist Logic
                ov_fid = row_data.get("override_format_id")
                aud_fid = row_data.get("audio_best_format_id")
                aud_manual_fid = row_data.get("audio_override_format_id")

                mode = int(self.type_combo.currentIndex()) if self.type_combo else 0

                if mode == 2:  # Audio only
                    if aud_manual_fid:
                        row_opts["format"] = aud_manual_fid
                    elif aud_fid:
                        row_opts["format"] = aud_fid
                    else:
                        row_opts["format"] = "bestaudio/best"
                    row_opts["extract_audio"] = True

                elif mode == 1:  # Video only
                    if ov_fid:
                        row_opts["format"] = ov_fid
                    else:
                        h = self._current_playlist_preset_height()
                        if h:
                            row_opts["format"] = f"bv*[height<={h}]+ba/b[height<={h}]"
                        else:
                            row_opts["format"] = "bestvideo+bestaudio/best"

                else:  # AV Muxed
                    if ov_fid:
                        target_audio = (
                            aud_manual_fid if row_data.get("audio_manual_override") else aud_fid
                        )
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
                        else:
                            row_opts["format"] = "bestvideo+bestaudio/best"

            # === Apply Common Overrides (Sub/Cover/Meta) ===

            # 1. Cover & Metadata
            row_opts["writethumbnail"] = pl_cover_enabled
            row_opts["embedthumbnail"] = pl_cover_enabled
            row_opts["addmetadata"] = pl_meta_enabled

            # 2. Subtitles
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
                        if pl_sub_override.format in ["srt", "ass", "vtt"]:
                            row_opts["convertsubtitles"] = pl_sub_override.format

            self._apply_download_dir_to_opts(row_opts)

            tasks.append((title, url, row_opts, thumb))

        return tasks

    def _check_subtitle_and_ask(self, config=None) -> bool | None:
        """
        检查字幕配置并弹出询问对话框
        """
        if not self.video_info:
            return None

        from ...core.config_manager import config_manager
        from ...processing.subtitle_manager import extract_subtitle_tracks

        subtitle_config = config or config_manager.get_subtitle_config()

        if not subtitle_config.enabled:
            return None

        tracks = extract_subtitle_tracks(self.video_info)

        if not tracks:
            # 视频没有字幕，提示用户
            box = MessageBox(
                "⚠️ 无可用字幕",
                "此视频没有可用字幕。\n\n是否继续下载（无字幕）？",
                parent=self,
            )
            box.yesButton.setText("继续下载")
            box.cancelButton.setText("取消")
            if not box.exec():
                raise ValueError("用户取消下载：无字幕")
            return None

        # 有字幕，检查是否需要询问嵌入模式
        if subtitle_config.embed_mode == "ask":
            available_langs = [t.lang_code for t in tracks[:5]]
            lang_display = ", ".join(available_langs)
            if len(tracks) > 5:
                lang_display += f" 等 {len(tracks)} 种语言"

            box = MessageBox(
                "检测到字幕",
                f"此视频包含 {len(tracks)} 个字幕轨道 ({lang_display})。\n\n是否将字幕嵌入视频？",
                parent=self,
            )
            box.yesButton.setText("嵌入字幕")
            box.cancelButton.setText("不嵌入")

            # 这里的 Cancel 按钮意味着 "不嵌入"，而不是 "取消下载"
            # MessageBox 的 exec 返回 True (yes) 或 False (cancel)
            # 所以如果返回 False，我们返回 False (不嵌入)，而不是抛出异常

            result = box.exec()
            self._subtitle_choice_made = True
            self._subtitle_embed_choice = bool(result)
            return bool(result)

        return None
