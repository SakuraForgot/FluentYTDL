from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon,
    IconWidget,
    RadioButton,
    ScrollArea,
    SegmentedWidget,
    StrongBodyLabel,
)

from ...core.config_manager import config_manager
from ...utils.format_scorer import ScoringContext, decide_merge_container, score_audio_format
from .badges import QualityCellWidget


def _get_table_selection_qss() -> str:
    from qfluentwidgets import isDarkTheme

    is_dark = isDarkTheme()
    sel_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "#E8E8E8"
    sel_fg = "#ffffff" if is_dark else "#000000"
    sel_bd = "rgba(255, 255, 255, 0.15)" if is_dark else "#C0C0C0"
    hov_bg = "rgba(255, 255, 255, 0.04)" if is_dark else "#F3F3F3"
    border = "rgba(255, 255, 255, 0.06)" if is_dark else "rgba(0, 0, 0, 0.06)"
    hover_border = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.1)"

    return f"""
QTableWidget {{
    background-color: transparent;
    selection-background-color: transparent;
    outline: none;
    border: none;
}}
QTableWidget::item {{
    padding-left: 0px;
    border: 1px solid {border};
    margin-top: 3px;
    margin-bottom: 3px;
    margin-left: 4px;
    margin-right: 4px;
    border-radius: 6px;
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
    border: 1px solid {hover_border};
    border-radius: 6px;
}}
"""


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


def _choose_lossless_merge_container(video_ext: str | None, audio_ext: str | None) -> str | None:
    v = str(video_ext or "").strip().lower()
    a = str(audio_ext or "").strip().lower()
    if not v or not a:
        return None
    if v == "webm" and a == "webm":
        return "webm"
    if v in {"mp4", "m4v"} and a in {"m4a", "aac", "mp4"}:
        return "mp4"
    return "mkv"


def _analyze_format_tags(r: dict) -> list[tuple[str, str]]:
    """Generates badge data for format details: [(text, color_style), ...]"""
    tags = []

    # 1. HDR
    dyn = str(r.get("dynamic_range") or "SDR").upper()
    if dyn != "SDR":
        # Usually HDR10, HLG, etc.
        tags.append((dyn, "gold"))

    # 2. FPS
    fps = r.get("fps")
    if fps and fps > 30:
        tags.append((f"{int(fps)}FPS", "red"))

    # 3. Audio Language / Track Type (Multi-Language support)
    lang = str(r.get("language") or "").strip()
    if lang:
        # Check if original / default
        track_type = str(r.get("audio_track_type") or "").lower()
        # Original track usually marked by youtube or has language="original" in yt-dlp
        if track_type == "original" or lang.lower() == "orig" or lang.lower() == "original":
            tags.append(("原音", "green"))
        else:
            tags.append((f"[{lang.upper()}]", "blue"))

    # 4. Codec
    # Video
    vc = str(r.get("vcodec") or "none").lower()
    if "av01" in vc:
        tags.append(("AV1", "blue"))
    elif "vp9" in vc:
        tags.append(("VP9", "green"))
    elif "avc1" in vc or "h264" in vc:
        # Gray for older/compatible codec
        tags.append(("H.264", "gray"))

    # Audio
    ac = str(r.get("acodec") or "none").lower()
    if "opus" in ac:
        tags.append(("Opus", "green"))
    elif "mp4a" in ac or "aac" in ac:
        tags.append(("AAC", "gray"))

    return tags


class SimplePresetWidget(QWidget):
    """简易模式下的预设选项卡片"""

    presetSelected = Signal()

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
        self.btn_group.buttonClicked.connect(self.presetSelected)

        # Define presets: (id, title, description, intent_config)
        self.presets = [
            # === 推荐选项 ===
            (
                "best_mp4",
                "🎬 最佳画质 (MP4)",
                "推荐。自动选择最佳画质并封装为 MP4，兼容性最好。",
                {"type": "video", "max_height": None, "prefer_ext": "mp4"},
            ),
            (
                "best_raw",
                "🎯 最佳画质 (原盘)",
                "追求极致画质。通常为 WebM/MKV 格式，适合本地播放。",
                {"type": "video", "max_height": None, "prefer_ext": None},
            ),
            # === 分辨率限制 ===
            (
                "2160p",
                "📺 2160p 4K (MP4)",
                "限制最高分辨率为 4K，超高清画质。",
                {"type": "video", "max_height": 2160, "prefer_ext": "mp4"},
            ),
            (
                "1440p",
                "📺 1440p 2K (MP4)",
                "限制最高分辨率为 2K，高清画质。",
                {"type": "video", "max_height": 1440, "prefer_ext": "mp4"},
            ),
            (
                "1080p",
                "📺 1080p 高清 (MP4)",
                "限制最高分辨率为 1080p，平衡画质与体积。",
                {"type": "video", "max_height": 1080, "prefer_ext": "mp4"},
            ),
            (
                "720p",
                "📺 720p 标清 (MP4)",
                "限制最高分辨率为 720p，适合移动设备。",
                {"type": "video", "max_height": 720, "prefer_ext": "mp4"},
            ),
            (
                "480p",
                "📺 480p (MP4)",
                "限制最高分辨率为 480p，节省空间。",
                {"type": "video", "max_height": 480, "prefer_ext": "mp4"},
            ),
            (
                "360p",
                "📺 360p (MP4)",
                "限制最高分辨率为 360p，最小体积。",
                {"type": "video", "max_height": 360, "prefer_ext": "mp4"},
            ),
            # === 纯音频 ===
            (
                "audio_mp3",
                "🎵 纯音频 (MP3 - 320k)",
                "仅下载音频并转码为 MP3。",
                {"type": "audio_only", "post_audio_format": "mp3", "post_audio_quality": "320K"},
            ),
        ]

        self.radios = []

        for i, (pid, title, desc, intent) in enumerate(self.presets):
            container = QFrame(self)
            from qfluentwidgets import isDarkTheme

            card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
            container.setStyleSheet(
                f".QFrame {{ background-color: rgba(255, 255, 255, 0.05); border-radius: 6px; border: 1px solid {card_bd}; }}"
            )
            h_layout = QHBoxLayout(container)

            rb = RadioButton(title, container)
            rb.setProperty("preset_id", pid)
            rb.setProperty("intent", intent)

            self.btn_group.addButton(rb, i)
            self.radios.append(rb)

            desc_label = CaptionLabel(desc, container)
            # Make description gray
            desc_label.setStyleSheet("color: #808080;")
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
        btn = self.btn_group.checkedButton()
        if not btn:
            return {}
        return {
            "id": btn.property("preset_id"),
            "intent": btn.property("intent"),
        }


class VideoFormatSelectorWidget(QWidget):
    """
    Encapsulates the logic for selecting video/audio formats.
    Supports "Simple" (presets) and "Advanced" (table) modes.
    """

    selectionChanged = Signal()

    def __init__(self, info: dict[str, Any], parent=None):
        super().__init__(parent)
        self.info = info

        # State for advanced mode
        self._rows: list[dict[str, Any]] = []
        self._selected_video_id: str | None = None
        self._selected_audio_id: str | None = None
        self._selected_muxed_id: str | None = None

        self._current_mode = "simple"

        self._init_ui()
        self._build_rows(info)
        self._refresh_table()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # Mode Switcher
        self.view_switcher = SegmentedWidget(self)
        self.view_switcher.addItem("simple", "简易模式")
        self.view_switcher.addItem("advanced", "专业模式")
        self.view_switcher.setCurrentItem("simple")
        self.view_switcher.currentItemChanged.connect(self._on_mode_changed)
        layout.addWidget(self.view_switcher)

        # Stack
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)

        # Page 1: Simple
        self.simple_widget = SimplePresetWidget(self)
        self.simple_widget.presetSelected.connect(self.selectionChanged)
        self.stack.addWidget(self.simple_widget)

        # Page 2: Advanced
        self.advanced_widget = QWidget(self)
        adv_layout = QVBoxLayout(self.advanced_widget)
        adv_layout.setContentsMargins(0, 0, 0, 0)
        adv_layout.setSpacing(10)

        # Mode Combo
        form_layout = QHBoxLayout()
        form_layout.addWidget(CaptionLabel("下载模式:", self.advanced_widget))
        self.mode_combo = ComboBox(self.advanced_widget)
        self.mode_combo.addItems(["音视频（可组装）", "音视频（整合流）", "仅视频", "仅音频"])
        self.mode_combo.currentIndexChanged.connect(self._refresh_table)
        form_layout.addWidget(self.mode_combo, 1)
        adv_layout.addLayout(form_layout)

        self.hint_label = CaptionLabel(
            "提示：可组装模式仅显示分离流，分别点选“视频”和“音频”即可组装。", self.advanced_widget
        )
        adv_layout.addWidget(self.hint_label)

        # --- Tables Area ---

        # 1. Single Table (for modes 1, 2, 3)
        self.table = self._create_table()
        self.table.cellClicked.connect(self._on_table_clicked)
        adv_layout.addWidget(self.table)

        # 2. Split Container (for mode 0)
        self.split_container = QWidget(self.advanced_widget)
        split_layout = QVBoxLayout(self.split_container)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(10)

        # Video Section
        self.video_container = QFrame(self.split_container)
        from qfluentwidgets import isDarkTheme

        card_bg = "rgba(255, 255, 255, 0.03)" if isDarkTheme() else "rgba(255, 255, 255, 0.7)"
        card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
        self.video_container.setStyleSheet(
            f".QFrame {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
        )
        v_layout = QVBoxLayout(self.video_container)
        v_layout.setContentsMargins(8, 8, 8, 8)
        v_layout.addWidget(StrongBodyLabel("视频流", self.video_container))
        self.video_table = self._create_table()
        self.video_table.cellClicked.connect(self._on_video_table_clicked)
        v_layout.addWidget(self.video_table)
        split_layout.addWidget(self.video_container)

        # Audio Section
        self.audio_container = QFrame(self.split_container)
        self.audio_container.setStyleSheet(
            f".QFrame {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
        )
        a_layout = QVBoxLayout(self.audio_container)
        a_layout.setContentsMargins(8, 8, 8, 8)
        a_layout.addWidget(StrongBodyLabel("音频流", self.audio_container))
        self.audio_table = self._create_table()
        self.audio_table.cellClicked.connect(self._on_audio_table_clicked)
        a_layout.addWidget(self.audio_table)
        split_layout.addWidget(self.audio_container)

        adv_layout.addWidget(self.split_container)

        self.selection_label = CaptionLabel("未选择", self.advanced_widget)
        adv_layout.addWidget(self.selection_label)

        self.stack.addWidget(self.advanced_widget)

    def _create_table(self):
        t = QTableWidget(self.advanced_widget)
        t.setStyleSheet(_get_table_selection_qss())
        t.setColumnCount(3)
        t.setHorizontalHeaderLabels(["类型", "质量", "详情"])
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        t.verticalHeader().setVisible(False)
        t.setAlternatingRowColors(True)
        t.setShowGrid(False)
        t.setWordWrap(False)
        try:
            t.verticalHeader().setDefaultSectionSize(42)
            t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
            t.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
            t.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            t.setColumnWidth(0, 60)
            t.setColumnWidth(1, 130)
        except Exception:
            pass
        return t

    def _on_mode_changed(self, routeKey: str):
        self._current_mode = routeKey
        self.stack.setCurrentIndex(0 if routeKey == "simple" else 1)

    def _build_rows(self, info: dict[str, Any]):
        formats = info.get("formats") or []
        if not isinstance(formats, list):
            return

        candidates = []
        for f in formats:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("format_id") or "").strip()
            if not fid:
                continue

            vcodec = str(f.get("vcodec") or "none")
            acodec = str(f.get("acodec") or "none")
            ext = str(f.get("ext") or "-")
            height = int(f.get("height") or 0)

            kind = "unknown"
            if vcodec != "none" and acodec != "none":
                kind = "muxed"
            elif vcodec != "none" and acodec == "none":
                kind = "video"
            elif vcodec == "none" and acodec != "none":
                kind = "audio"
            else:
                continue

            if kind in ("muxed", "video") and height and height < 144:
                continue

            candidates.append(
                {
                    "kind": kind,
                    "format_id": fid,
                    "ext": ext,
                    "height": height,
                    "vcodec": vcodec,
                    "acodec": acodec,
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                    "fps": f.get("fps"),
                    "vbr": f.get("vbr"),
                    "tbr": f.get("tbr"),
                    "abr": f.get("abr"),
                    "dynamic_range": f.get("dynamic_range"),
                    "language": f.get("language"),
                    "audio_track_type": f.get("audio_track_type"),
                }
            )

        # Sort: muxed first, then video, then audio. Within kind, by height desc.
        candidates.sort(
            key=lambda x: (
                0 if x["kind"] == "muxed" else 1 if x["kind"] == "video" else 2,
                -int(x.get("height") or 0),
            )
        )
        self._rows = candidates

    def _refresh_table_selection_state(self):
        """Reset incompatible selections based on current mode (helper for _refresh_table)."""
        mode = self.mode_combo.currentIndex()
        self.hint_label.setVisible(mode == 0)

        # Clear incompatible selections
        if mode == 0:
            self._selected_muxed_id = None
        if mode == 1:
            self._selected_video_id = None
            self._selected_audio_id = None
        if mode in (2, 3):
            self._selected_muxed_id = None
            if mode == 2:
                self._selected_audio_id = None
            else:
                self._selected_video_id = None

    def _get_best_audio_id(
        self, audio_rows: list[dict], ctx: ScoringContext | None = None
    ) -> str | None:
        """
        自动推断最优的音频流。

        若传入 ctx，直接使用其 preferred_audio_langs；否则从 config_manager 读取。
        评分委托给 format_scorer.score_audio_format，使用等差间距 + BCP-47 别名匹配，
        彻底修复了旧版 10**i 指数间距在第 8 个偏好后 multiplier 归零的精度崩塌问题。
        """
        if not audio_rows:
            return None

        if ctx is None:
            pref_langs = config_manager.get("preferred_audio_languages")
            if not isinstance(pref_langs, list) or not pref_langs:
                pref_langs = ["orig", "zh-Hans", "en"]
            ctx = ScoringContext(preferred_audio_langs=pref_langs)

        best_audio = max(audio_rows, key=lambda r: score_audio_format(r, ctx))
        return best_audio["format_id"]

    def _pick_best_video(self, video_rows: list[dict], intent: dict) -> str | None:
        """
        根据预设意图从分离视频流中挑选最优项。

        修复：容器偏好改为「硬约束先过滤，无结果再降级」，替代原先 +500 分的软偏好
        （+500 在 1080p vs 720p 差异下完全被 h*10000 淹没，实际无效）。
        """
        if not video_rows:
            return None

        max_height = intent.get("max_height")
        prefer_ext = intent.get("prefer_ext")

        # 1. 分辨率上限过滤
        pool = video_rows
        if max_height is not None:
            pool = [r for r in pool if int(r.get("height") or 0) <= max_height]
        if not pool:
            return None

        # 2. 容器硬约束过滤（简易模式 + 有容器偏好时）：先尝试目标容器
        if prefer_ext:
            preferred_pool = [r for r in pool if str(r.get("ext") or "").lower() == prefer_ext]
            if preferred_pool:
                pool = preferred_pool
            # 若目标容器无流，保留全集并由容器决策函数处理结果格式

        # 3. 在约束后的候选集内按分辨率+码率排序
        best = max(
            pool,
            key=lambda r: (
                int(r.get("height") or 0),
                int(r.get("vbr") or r.get("tbr") or 0),
            ),
        )
        return best["format_id"]

    def _pick_best_muxed(self, muxed_rows: list[dict], intent: dict) -> str | None:
        """
        当没有分离流时，从整合流中挑选最优项（容器硬约束策略同 _pick_best_video）。
        """
        if not muxed_rows:
            return None

        max_height = intent.get("max_height")
        prefer_ext = intent.get("prefer_ext")

        pool = muxed_rows
        if max_height is not None:
            pool = [r for r in pool if int(r.get("height") or 0) <= max_height]
        if not pool:
            pool = muxed_rows  # 分辨率门槛无结果时回退全集

        if prefer_ext:
            preferred_pool = [r for r in pool if str(r.get("ext") or "").lower() == prefer_ext]
            if preferred_pool:
                pool = preferred_pool

        best = max(
            pool,
            key=lambda r: (
                int(r.get("height") or 0),
                int(r.get("vbr") or r.get("tbr") or 0),
            ),
        )
        return best["format_id"]

    def _refresh_table(self):
        mode = self.mode_combo.currentIndex()
        self.hint_label.setVisible(mode == 0)

        # Clear incompatible selections
        if mode == 0:
            self._selected_muxed_id = None
        if mode == 1:
            self._selected_video_id = None
            self._selected_audio_id = None
        if mode in (2, 3):
            self._selected_muxed_id = None
            if mode == 2:
                self._selected_audio_id = None
            else:
                self._selected_video_id = None

        if mode == 0:
            # Split View
            self.table.hide()
            self.split_container.show()

            video_rows = [r for r in getattr(self, "_rows", []) if r["kind"] == "video"]
            audio_rows = [r for r in getattr(self, "_rows", []) if r["kind"] == "audio"]

            if not self._selected_audio_id and audio_rows:
                self._selected_audio_id = self._get_best_audio_id(audio_rows)

            if not self._selected_video_id and video_rows:
                self._selected_video_id = video_rows[0]["format_id"]

            self._populate_table(self.video_table, video_rows, self._selected_video_id)
            self._populate_table(self.audio_table, audio_rows, self._selected_audio_id)

        else:
            # Single View
            self.split_container.hide()
            self.table.show()

            view_rows = []
            for r in self._rows:
                k = r["kind"]
                if mode == 1:
                    if k == "muxed":
                        view_rows.append(r)
                elif mode == 2:
                    if k == "video":
                        view_rows.append(r)
                elif mode == 3:
                    if k == "audio":
                        view_rows.append(r)

            if mode == 3 and not self._selected_audio_id and view_rows:
                self._selected_audio_id = self._get_best_audio_id(view_rows)
            if mode == 2 and not self._selected_video_id and view_rows:
                self._selected_video_id = view_rows[0]["format_id"]
            if mode == 1 and not self._selected_muxed_id and view_rows:
                self._selected_muxed_id = view_rows[0]["format_id"]

            sel_id = self._selected_muxed_id
            if mode == 2:
                sel_id = self._selected_video_id
            elif mode == 3:
                sel_id = self._selected_audio_id

            self._populate_table(self.table, view_rows, sel_id)

        self._update_label()
        self.selectionChanged.emit()

    def _populate_table(self, table: QTableWidget, rows: list[dict], selected_id: str | None):
        table.setRowCount(len(rows))
        table.setProperty("_rows", rows)

        for i, r in enumerate(rows):
            kind = r["kind"]

            icon = FluentIcon.VIDEO if kind in ("muxed", "video") else FluentIcon.MUSIC

            # Use a widget to ensure centering
            container = QWidget()
            container.setStyleSheet("background: transparent;")
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            iw = IconWidget(icon)
            iw.setFixedSize(16, 16)
            layout.addWidget(iw)

            item0 = QTableWidgetItem("")
            table.setItem(i, 0, item0)
            table.setCellWidget(i, 0, container)

            q_text = f"{r.get('height')}p" if r.get("height") else f"{int(r.get('abr') or 0)}kbps"
            # Badges for Quality Column (only HDR)
            q_badges = []
            if r.get("dynamic_range") and "HDR" in str(r.get("dynamic_range")):
                q_badges.append(("HDR", "blue"))

            q_w = QualityCellWidget(
                q_badges, q_text, parent=table, alignment=Qt.AlignmentFlag.AlignCenter
            )
            table.setCellWidget(i, 1, q_w)

            # Detail Column: Tags + Size/Ext
            detail_tags = _analyze_format_tags(r)

            sz = _format_size(r.get("filesize"))
            ext = r.get("ext")

            # Construct main text for details
            detail_text = f"{ext} • {sz}"

            # Use QualityCellWidget for Details too
            # We want left alignment generally for details but user requested centered visuals earlier.
            # However, for badges flow, Left or Center?
            # User said "center alignment to achieve visual optimization" previously.
            # Let's keep Center for consistency.
            d_w = QualityCellWidget(
                detail_tags, detail_text, parent=table, alignment=Qt.AlignmentFlag.AlignCenter
            )

            item2 = QTableWidgetItem("")
            table.setItem(i, 2, item2)
            table.setCellWidget(i, 2, d_w)

        self._highlight_table_rows(table, {selected_id} if selected_id else set())

    def _highlight_table_rows(self, table: QTableWidget, selected_ids: set[str]):
        rows = table.property("_rows") or []
        for i in range(table.rowCount()):
            # Reset style
            for j in range(3):
                it = table.item(i, j)
                if it:
                    it.setBackground(QBrush())
                    it.setForeground(QBrush())  # Default

            if i < len(rows):
                fid = rows[i]["format_id"]
                if fid in selected_ids and fid:
                    for j in range(3):
                        it = table.item(i, j)
                        if it:
                            it.setBackground(QColor("#E8E8E8"))
                            it.setForeground(QColor(0, 0, 0))

    def _on_table_clicked(self, row, col):
        rows = self.table.property("_rows")
        if not rows or row >= len(rows):
            return

        r = rows[row]
        fid = r["format_id"]
        mode = self.mode_combo.currentIndex()

        if mode == 1:
            self._selected_muxed_id = fid
        elif mode == 2:
            self._selected_video_id = fid
        elif mode == 3:
            self._selected_audio_id = fid

        self._highlight_table_rows(self.table, {fid} if fid else set())
        self._update_label()
        self.selectionChanged.emit()

    def _on_video_table_clicked(self, row, col):
        rows = self.video_table.property("_rows")
        if not rows or row >= len(rows):
            return
        self._selected_video_id = rows[row]["format_id"]
        self._highlight_table_rows(
            self.video_table, {self._selected_video_id} if self._selected_video_id else set()
        )
        self._update_label()
        self.selectionChanged.emit()

    def _on_audio_table_clicked(self, row, col):
        rows = self.audio_table.property("_rows")
        if not rows or row >= len(rows):
            return
        self._selected_audio_id = rows[row]["format_id"]
        self._highlight_table_rows(
            self.audio_table, {self._selected_audio_id} if self._selected_audio_id else set()
        )
        self._update_label()
        self.selectionChanged.emit()

    def _update_highlight(self):
        # Deprecated by _highlight_table_rows but kept for safety if called elsewhere (unlikely)
        pass

    def _update_label(self):
        mode = self.mode_combo.currentIndex()
        label = self.selection_label

        if mode == 1:
            label.setText("已选：整合流" if self._selected_muxed_id else "请选择：整合流")
        elif mode == 2:
            label.setText("已选：视频流" if self._selected_video_id else "请选择：视频流")
        elif mode == 3:
            label.setText("已选：音频流" if self._selected_audio_id else "请选择：音频流")
        else:
            if self._selected_video_id and self._selected_audio_id:
                label.setText("已选：视频流 + 音频流")
            elif self._selected_video_id:
                label.setText("已选：视频流（将自动匹配最佳音频）")
            elif self._selected_audio_id:
                label.setText("已选：音频流（请再选择一个视频流）")
            else:
                label.setText("未选择")

    def get_selection_result(self) -> dict:
        """Returns {format: str, extra_opts: dict} or {} if invalid."""
        if getattr(self, "_current_mode", "simple") == "simple":
            sel = self.simple_widget.get_current_selection()
            if not sel:
                return {}

            intent = sel.get("intent") or {}
            rows = getattr(self, "_rows", [])
            video_rows = [r for r in rows if r.get("kind") == "video"]
            audio_rows = [r for r in rows if r.get("kind") == "audio"]
            muxed_rows = [r for r in rows if r.get("kind") == "muxed"]

            # ── 构建打分上下文（整合用户设置 + 预设意图 + 字幕配置）──────────
            pref_langs = config_manager.get("preferred_audio_languages")
            if not isinstance(pref_langs, list) or not pref_langs:
                pref_langs = ["orig", "zh-Hans", "en"]

            # 从字幕配置预填充字幕信息（决策时序：此处在 subtitle_service.apply 之前）
            # 最终容器修正由 _ensure_subtitle_compatible_container 兜底，ctx 值仅作预判
            sub_config = config_manager.get_subtitle_config()
            sub_enabled = (
                sub_config.enabled
                and sub_config.embed_type == "soft"
                and sub_config.embed_mode != "never"
            )
            sub_lang_count = len(sub_config.default_languages) if sub_enabled else 0

            ctx = ScoringContext(
                is_simple_mode=True,
                max_height=intent.get("max_height"),
                prefer_ext=intent.get("prefer_ext"),
                preferred_audio_langs=pref_langs,
                embed_subtitles=sub_enabled,
                subtitle_lang_count=sub_lang_count,
            )

            # --- 纯音频模式 ---
            if intent.get("type") == "audio_only":
                best_aud = self._get_best_audio_id(audio_rows, ctx) if audio_rows else None
                extra: dict = {
                    "extract_audio": True,
                    "audio_format": intent.get("post_audio_format", "mp3"),
                    "audio_quality": intent.get("post_audio_quality", "320K"),
                }
                return {"format": best_aud or "bestaudio/best", "extra_opts": extra}

            # --- 含视频模式：用打分引擎挑选最优视频+音频 ---
            best_vid = self._pick_best_video(video_rows, intent)
            best_aud = self._get_best_audio_id(audio_rows, ctx) if audio_rows else None

            extra_opts: dict = {}

            if best_vid and best_aud:
                # 正常组装：视频+音频，容器由统一决策函数确定
                vid_ext = next(
                    (r.get("ext") for r in video_rows if r["format_id"] == best_vid), "mp4"
                )
                aud_ext = next(
                    (r.get("ext") for r in audio_rows if r["format_id"] == best_aud), "m4a"
                )
                merge_fmt = decide_merge_container(vid_ext, aud_ext, ctx)
                extra_opts["merge_output_format"] = merge_fmt
                return {"format": f"{best_vid}+{best_aud}", "extra_opts": extra_opts}

            elif best_vid:
                # 只有视频没有音频 → 降级找整合流
                best_muxed = self._pick_best_muxed(muxed_rows, intent)
                if best_muxed:
                    return {"format": best_muxed, "extra_opts": extra_opts}
                return {"format": best_vid, "extra_opts": extra_opts}

            elif muxed_rows:
                # 完全没有分离视频流 → 使用整合流
                best_muxed = self._pick_best_muxed(muxed_rows, intent)
                if best_muxed:
                    return {"format": best_muxed, "extra_opts": extra_opts}

            # 兜底
            return {"format": "best", "extra_opts": extra_opts}
        else:
            # Advanced 模式：用户手动选定 format_id，容器仍用无损推断
            v = self._selected_video_id
            a = self._selected_audio_id
            m = self._selected_muxed_id

            opts = {}
            if m:
                opts["format"] = m
            elif v and a:
                opts["format"] = f"{v}+{a}"
                vext = next((r["ext"] for r in self._rows if r["format_id"] == v), "mp4")
                aext = next((r["ext"] for r in self._rows if r["format_id"] == a), "m4a")
                merge = _choose_lossless_merge_container(vext, aext)
                if merge:
                    opts["merge_output_format"] = merge
            elif v:
                opts["format"] = v
            elif a:
                opts["format"] = a
            else:
                return {}

            extra_opts = {}
            if "merge_output_format" in opts:
                extra_opts["merge_output_format"] = opts["merge_output_format"]
            return {"format": opts["format"], "extra_opts": extra_opts}

    def get_summary_text(self) -> str:
        """Returns a human-readable summary of the current selection."""
        if getattr(self, "_current_mode", "simple") == "simple":
            # Simple mode: use the checked radio button text
            btn = self.simple_widget.btn_group.checkedButton()
            return btn.text() if btn else "未选择"
        else:
            # Advanced mode: use the label text
            return self.selection_label.text().replace("已选：", "")
