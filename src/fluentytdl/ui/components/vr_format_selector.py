from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
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

from .badges import QualityCellWidget


def _analyze_format_tags(r: dict) -> list[tuple[str, str]]:
    """Generates badge data for format details: [(text, color_style), ...]"""
    tags = []

    # 1. HDR
    dyn = str(r.get("dynamic_range") or "SDR").upper()
    if dyn != "SDR":
        tags.append((dyn, "gold"))

    # 2. FPS
    fps = r.get("fps")
    if fps and fps > 30:
        tags.append((f"{int(fps)}FPS", "red"))

    # 3. Codec
    # Video
    vc = str(r.get("vcodec") or "none").lower()
    if "av01" in vc:
        tags.append(("AV1", "blue"))
    elif "vp9" in vc:
        tags.append(("VP9", "green"))
    elif "avc1" in vc or "h264" in vc:
        tags.append(("H.264", "gray"))

    # Audio
    ac = str(r.get("acodec") or "none").lower()
    if "opus" in ac:
        tags.append(("Opus", "green"))
    elif "mp4a" in ac or "aac" in ac:
        tags.append(("AAC", "gray"))

    return tags


# ── VR 场景化预设定义 ─────────────────────────────────────────
#
# 设计原则：用户不关心 Equirectangular 还是 Mesh，他们只关心"我用什么设备看"。
# VP9 优先于 AV1：Quest 2（最大存量设备）不支持 AV1 硬解。
#

VR_PRESETS: list[tuple[str, str, str, str, dict[str, Any]]] = [
    # (id, title, description, format_selector, post_args)
    (
        "vr_headset",
        "\U0001f941 VR \u5934\u663e\u539f\u751f (\u63a8\u8350)",
        "Quest / Pico / Vision Pro \u7b49\u5934\u663e\u7528\u6237\u3002"
        "\u6700\u9ad8\u753b\u8d28\uff0c\u4fdd\u7559\u539f\u59cb\u6295\u5f71\uff0cVP9/AV1 \u7f16\u7801\uff0cMKV \u5c01\u88c5\u3002",
        "bv*[vcodec^=vp9]+ba/bv*[vcodec^=av01]+ba/bv*+ba/b",
        {"merge_output_format": "mkv"},
    ),
    (
        "vr_compat",
        "\U0001f4f1 \u901a\u7528\u517c\u5bb9",
        "\u624b\u673a / \u7535\u8111 / PotPlayer / \u65e7\u8bbe\u5907\u3002"
        "\u5f3a\u5236 MP4 + H.264 \u4f18\u5148\uff0c\u82e5\u6e90\u4e3a EAC \u683c\u5f0f\u5c06\u81ea\u52a8\u8f6c\u7801\uff08\u8017\u65f6\u8f83\u957f\uff09\u3002",
        "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b",
        {"merge_output_format": "mp4", "__vr_convert_eac": True},
    ),
    (
        "vr_3d_cinema",
        "\U0001f453 3D \u5f71\u9662",
        "\u53ea\u770b 3D \u7acb\u4f53\u6548\u679c\uff08VR180 \u4e3a\u4e3b\uff09\u3002"
        "\u4f18\u5148\u7b5b\u9009\u7acb\u4f53 3D \u6d41 (TB/SBS)\u3002",
        "bv*+ba/b",
        {"merge_output_format": "mkv", "__vr_prefer_stereo": True},
    ),
    (
        "vr_panorama",
        "\U0001f310 \u5168\u666f\u6f2b\u6e38",
        "\u98ce\u666f / \u7eaa\u5f55\u7247 / 2D \u5168\u666f\u89c6\u9891\u3002"
        "\u4f18\u5148\u7b5b\u9009 Mono 360\u00b0 \u6d41\u3002",
        "bv*+ba/b",
        {"__vr_prefer_mono": True},
    ),
    (
        "vr_audio",
        "\U0001f3b5 \u4ec5\u97f3\u9891",
        "\u4ec5\u63d0\u53d6 VR \u89c6\u9891\u97f3\u8f68\uff0c\u8f6c\u7801\u4e3a MP3 (320kbps)\u3002",
        "bestaudio/best",
        {"extract_audio": True, "audio_format": "mp3", "audio_quality": "320K"},
    ),
]

_VR_PLAYBACK_HINT = "\U0001f4a1 \u64ad\u653e\u63d0\u793a\uff1a\u8bf7\u5728\u64ad\u653e\u5668\u624b\u52a8\u9009\u62e9 VR \u6a21\u5f0f\uff08180\u00b0/360\u00b0/TB/SBS\uff09"


# ── QSS ──────────────────────────────────────────────────────


def _get_table_selection_qss() -> str:
    from qfluentwidgets import isDarkTheme

    is_dark = isDarkTheme()
    sel_bg = "rgba(255, 255, 255, 0.08)" if is_dark else "#E8E8E8"
    sel_fg = "#ffffff" if is_dark else "#000000"
    sel_bd = "rgba(255, 255, 255, 0.15)" if is_dark else "#D0D0D0"
    hov_bg = "rgba(255, 255, 255, 0.04)" if is_dark else "#F3F3F3"
    border = "rgba(255, 255, 255, 0.06)" if is_dark else "rgba(0, 0, 0, 0.06)"
    hover_border = "rgba(255, 255, 255, 0.1)" if is_dark else "rgba(0, 0, 0, 0.1)"

    return f"""
QTableWidget {{
    background-color: transparent;
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


# ── 辅助函数 ──────────────────────────────────────────────────


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


# ── 简易模式预设列表 ────────────────────────────────────────


class VRPresetWidget(QWidget):
    """VR 简易模式：场景化预设卡片列表"""

    def __init__(self, parent=None):
        super().__init__(parent)

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(0)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = ScrollArea(self)
        scroll_area.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")
        scroll_area.setWidgetResizable(True)
        scroll_area.setMaximumHeight(450)

        content_widget = QWidget()
        content_widget.setStyleSheet("background-color: transparent;")
        self.v_layout = QVBoxLayout(content_widget)
        self.v_layout.setSpacing(12)
        self.v_layout.setContentsMargins(10, 10, 10, 10)

        self.btn_group = QButtonGroup(self)
        self.radios: list[RadioButton] = []

        for i, (pid, title, desc, fmt, args) in enumerate(VR_PRESETS):
            container = QFrame(self)
            from qfluentwidgets import isDarkTheme

            card_bd = "rgba(255, 255, 255, 0.08)" if isDarkTheme() else "rgba(0, 0, 0, 0.05)"
            container.setStyleSheet(
                f".QFrame {{ background-color: rgba(255, 255, 255, 0.05); border-radius: 6px; border: 1px solid {card_bd}; }}"
            )
            h_layout = QHBoxLayout(container)

            rb = RadioButton(title, container)
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

        # 播放提示
        hint_label = CaptionLabel(_VR_PLAYBACK_HINT, content_widget)
        hint_label.setTextColor(QColor(100, 100, 100), QColor(160, 160, 160))
        hint_label.setWordWrap(True)
        self.v_layout.addWidget(hint_label)

        scroll_area.setWidget(content_widget)
        main_layout.addWidget(scroll_area)

        # 默认选中第一个
        if self.radios:
            self.radios[0].setChecked(True)

    def get_current_selection(self) -> dict[str, Any]:
        btn = self.btn_group.checkedButton()
        if not btn:
            return {}
        return {
            "format": btn.property("format_str"),
            "extra_opts": dict(btn.property("extra_args") or {}),
        }


# ── 专业模式格式表 ──────────────────────────────────────────


class VRFormatTableWidget(QWidget):
    """VR 专业模式：双表格选择视频+音频 format_id，含投影/立体 badge"""

    selectionChanged = Signal()

    _VCOLS = ["\u7c7b\u578b", "\u8d28\u91cf", "\u7acb\u4f53", "\u6295\u5f71", "\u8be6\u60c5"]
    _ACOLS = ["\u7c7b\u578b", "\u8d28\u91cf", "\u8be6\u60c5"]

    def __init__(self, info: dict[str, Any], parent=None):
        super().__init__(parent)
        self._info = info

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 模式下拉
        self.mode_combo = ComboBox(self)
        self.mode_combo.addItems(
            [
                "\u7ec4\u5408 (\u89c6\u9891 + \u97f3\u9891)",
                "\u4ec5\u89c6\u9891",
                "\u4ec5\u97f3\u9891",
            ]
        )
        self.mode_combo.setCurrentIndex(0)
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        layout.addWidget(self.mode_combo)

        # VR 过滤器
        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(12)

        self._filter_3d = QCheckBox("\u4ec5 3D \u7acb\u4f53", self)
        self._filter_3d.stateChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_3d)

        self._filter_8k = QCheckBox("\u4ec5 8K+", self)
        self._filter_8k.stateChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_8k)

        self._filter_no_av1 = QCheckBox("\u6392\u9664 AV1", self)
        self._filter_no_av1.stateChanged.connect(self._on_filter_changed)
        filter_row.addWidget(self._filter_no_av1)

        filter_row.addStretch(1)
        layout.addLayout(filter_row)

        # Split Container
        self.split_container = QWidget(self)
        split_layout = QVBoxLayout(self.split_container)
        split_layout.setContentsMargins(0, 0, 0, 0)
        split_layout.setSpacing(8)

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

        self.video_label = StrongBodyLabel("\u89c6\u9891\u6d41", self.video_container)
        v_layout.addWidget(self.video_label)

        self.video_table = QTableWidget(self.video_container)
        self.video_table.setColumnCount(len(self._VCOLS))
        self.video_table.setHorizontalHeaderLabels(self._VCOLS)
        self.video_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.video_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.video_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.video_table.verticalHeader().setVisible(False)
        self.video_table.setStyleSheet(_get_table_selection_qss())
        self.video_table.setShowGrid(False)
        self.video_table.setAlternatingRowColors(True)
        self.video_table.setWordWrap(False)
        try:
            self.video_table.verticalHeader().setDefaultSectionSize(42)
            self.video_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Fixed
            )
            self.video_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Fixed
            )
            self.video_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Fixed
            )
            self.video_table.horizontalHeader().setSectionResizeMode(
                3, QHeaderView.ResizeMode.Fixed
            )
            self.video_table.horizontalHeader().setSectionResizeMode(
                4, QHeaderView.ResizeMode.Stretch
            )
            self.video_table.setColumnWidth(0, 60)
            self.video_table.setColumnWidth(1, 130)
            self.video_table.setColumnWidth(2, 100)
            self.video_table.setColumnWidth(3, 80)
        except Exception:
            pass
        self.video_table.setMaximumHeight(220)
        self.video_table.itemSelectionChanged.connect(self._on_video_selected)
        v_layout.addWidget(self.video_table)

        split_layout.addWidget(self.video_container)

        # Audio Section
        self.audio_container = QFrame(self.split_container)
        self.audio_container.setStyleSheet(
            f".QFrame {{ background-color: {card_bg}; border: 1px solid {card_bd}; border-radius: 8px; }}"
        )
        a_layout = QVBoxLayout(self.audio_container)
        a_layout.setContentsMargins(8, 8, 8, 8)

        self.audio_label = StrongBodyLabel("\u97f3\u9891\u6d41", self.audio_container)
        a_layout.addWidget(self.audio_label)

        self.audio_table = QTableWidget(self.audio_container)
        self.audio_table.setColumnCount(len(self._ACOLS))
        self.audio_table.setHorizontalHeaderLabels(self._ACOLS)
        self.audio_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.audio_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.audio_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.audio_table.verticalHeader().setVisible(False)
        self.audio_table.setStyleSheet(_get_table_selection_qss())
        self.audio_table.setShowGrid(False)
        self.audio_table.setAlternatingRowColors(True)
        self.audio_table.setWordWrap(False)
        try:
            self.audio_table.verticalHeader().setDefaultSectionSize(42)
            self.audio_table.horizontalHeader().setSectionResizeMode(
                0, QHeaderView.ResizeMode.Fixed
            )
            self.audio_table.horizontalHeader().setSectionResizeMode(
                1, QHeaderView.ResizeMode.Fixed
            )
            self.audio_table.horizontalHeader().setSectionResizeMode(
                2, QHeaderView.ResizeMode.Stretch
            )
            self.audio_table.setColumnWidth(0, 60)
            self.audio_table.setColumnWidth(1, 130)
        except Exception:
            pass
        self.audio_table.setMaximumHeight(180)
        self.audio_table.itemSelectionChanged.connect(self._on_audio_selected)
        a_layout.addWidget(self.audio_table)

        split_layout.addWidget(self.audio_container)

        layout.addWidget(self.split_container)

        # 选择摘要
        self.summary_label = CaptionLabel("", self)
        layout.addWidget(self.summary_label)

        # 内部状态
        self._video_rows: list[dict[str, Any]] = []
        self._audio_rows: list[dict[str, Any]] = []
        self._selected_video_id: str | None = None
        self._selected_audio_id: str | None = None

        # 保存原始格式列表引用（过滤器刷新时用）
        self._all_video_fmts: list[dict[str, Any]] = []

        self._populate(info)

    # ── VR 过滤器 ──

    def _on_filter_changed(self, _state: int = 0) -> None:
        """过滤器勾选变化时重新填充视频表"""
        self._fill_video_table(self._all_video_fmts)

    # ── 填充表格 ──

    def _populate(self, info: dict[str, Any]) -> None:
        formats = info.get("formats") or []

        # [VR Compatibility] 仅显示 android_vr 客户端支持的格式
        compatible_ids = set(info.get("__android_vr_format_ids") or [])
        should_filter = bool(compatible_ids)

        # 视频流（有 vcodec 且不是 none）
        video_fmts: list[dict[str, Any]] = []
        for f in formats:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("format_id") or "")
            if should_filter and fid not in compatible_ids:
                continue
            if f.get("vcodec") in (None, "none"):
                continue
            h = int(f.get("height") or 0)
            if h < 360:
                continue
            video_fmts.append(f)

        self._all_video_fmts = video_fmts
        self._fill_video_table(video_fmts)

        # 音频流
        audio_fmts: list[dict[str, Any]] = []
        for f in formats:
            if not isinstance(f, dict):
                continue
            fid = str(f.get("format_id") or "")
            if should_filter and fid not in compatible_ids:
                continue
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
            audio_fmts.append({**f, "_abr": abr})
        audio_fmts.sort(key=lambda x: x["_abr"], reverse=True)

        self.audio_table.setRowCount(len(audio_fmts))
        self._audio_rows = []
        for i, f in enumerate(audio_fmts):
            abr = f["_abr"]
            acodec = str(f.get("acodec") or "?")[:12]
            sz = _format_size(f.get("filesize") or f.get("filesize_approx"))
            ext = str(f.get("ext") or "?")
            fid = str(f.get("format_id") or "?")

            # Col 0: Type Icon
            container = QWidget()
            container.setStyleSheet("background: transparent;")
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            iw = IconWidget(FluentIcon.MUSIC)
            iw.setFixedSize(16, 16)
            layout.addWidget(iw)

            self.audio_table.setItem(i, 0, QTableWidgetItem(""))
            self.audio_table.setCellWidget(i, 0, container)

            # Col 1: Quality
            q_w = QualityCellWidget(
                [], f"{abr}kbps", parent=self.audio_table, alignment=Qt.AlignmentFlag.AlignCenter
            )
            self.audio_table.setCellWidget(i, 1, q_w)

            # Col 2: Details (Badges + Text)
            detail_text = f"{ext} • {acodec} • {sz} • {fid}"
            detail_badges = _analyze_format_tags(f)

            d_w = QualityCellWidget(
                detail_badges,
                detail_text,
                parent=self.audio_table,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )

            item = QTableWidgetItem("")
            self.audio_table.setItem(i, 2, item)
            self.audio_table.setCellWidget(i, 2, d_w)

            self._audio_rows.append({"format_id": fid, "abr": abr, "ext": ext})

        # 默认选中第一行音频
        if self._audio_rows:
            self.audio_table.selectRow(0)

    def _fill_video_table(self, video_fmts: list[dict[str, Any]]) -> None:
        """根据过滤器和投影排序填充视频表"""
        # 应用过滤器
        filtered: list[dict[str, Any]] = []
        for f in video_fmts:
            if self._filter_3d.isChecked():
                stereo = str(f.get("__vr_stereo_mode") or "unknown")
                if not stereo.startswith("stereo"):
                    continue
            if self._filter_8k.isChecked():
                h = int(f.get("height") or 0)
                if h < 4320:
                    continue
            if self._filter_no_av1.isChecked():
                vc = str(f.get("vcodec") or "").lower()
                if vc.startswith("av01"):
                    continue
            filtered.append(f)

        # 排序: 3D+Equi -> 3D+Mesh -> Mono+Equi -> EAC -> unknown; 同类按高度降序
        _STEREO_ORDER = {"stereo_tb": 0, "stereo_sbs": 0, "mono": 1, "unknown": 2}
        _PROJ_ORDER = {"equirectangular": 0, "mesh": 1, "eac": 2, "unknown": 3}

        def _sort_key(f: dict[str, Any]) -> tuple:
            stereo = str(f.get("__vr_stereo_mode") or "unknown")
            proj = str(f.get("__vr_projection") or "unknown")
            h = int(f.get("height") or 0)
            return (_STEREO_ORDER.get(stereo, 9), _PROJ_ORDER.get(proj, 9), -h)

        filtered.sort(key=_sort_key)

        # 填充表格
        self.video_table.setRowCount(len(filtered))
        self._video_rows = []
        for i, f in enumerate(filtered):
            h = int(f.get("height") or 0)
            vcodec = str(f.get("vcodec") or "?")[:12]
            fps = f.get("fps") or ""
            sz = _format_size(f.get("filesize") or f.get("filesize_approx"))
            ext = str(f.get("ext") or "?")
            fid = str(f.get("format_id") or "?")

            # Col 0: Type Icon
            container = QWidget()
            container.setStyleSheet("background: transparent;")
            layout = QHBoxLayout(container)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
            iw = IconWidget(FluentIcon.VIDEO)
            iw.setFixedSize(16, 16)
            layout.addWidget(iw)

            self.video_table.setItem(i, 0, QTableWidgetItem(""))
            self.video_table.setCellWidget(i, 0, container)

            # Col 1: Quality (Res + HDR badge)
            res_str = f"{h}p"
            badges: list[tuple[str, str]] = []
            dr = f.get("dynamic_range") or ""
            if "HDR" in str(dr):
                badges.append(("HDR", "blue"))
            try:
                if fps and float(fps) > 30:
                    res_str += f" {int(float(fps))}fps"
            except Exception:
                pass
            self.video_table.setCellWidget(
                i,
                1,
                QualityCellWidget(
                    badges, res_str, parent=self.video_table, alignment=Qt.AlignmentFlag.AlignCenter
                ),
            )

            # Col 2: 立体模式 badge
            stereo = str(f.get("__vr_stereo_mode") or "unknown")
            stereo_badge: list[tuple[str, str]] = []
            if stereo.startswith("stereo"):
                label = "3D TB" if stereo == "stereo_tb" else "3D SBS"
                stereo_badge.append((label, "blue"))
            elif stereo == "mono":
                stereo_badge.append(("2D", "gray"))
            else:
                stereo_badge.append(("?", "gray"))
            self.video_table.setCellWidget(
                i,
                2,
                QualityCellWidget(
                    stereo_badge,
                    "",
                    parent=self.video_table,
                    alignment=Qt.AlignmentFlag.AlignCenter,
                ),
            )

            # Col 3: 投影类型 badge (颜色区分可播性)
            proj = str(f.get("__vr_projection") or "unknown")
            proj_badge: list[tuple[str, str]] = []
            if proj == "equirectangular":
                proj_badge.append(("Equi", "green"))
            elif proj == "mesh":
                proj_badge.append(("Mesh", "orange"))
            elif proj == "eac":
                proj_badge.append(("EAC", "red"))
            else:
                proj_badge.append(("?", "gray"))
            self.video_table.setCellWidget(
                i,
                3,
                QualityCellWidget(
                    proj_badge, "", parent=self.video_table, alignment=Qt.AlignmentFlag.AlignCenter
                ),
            )

            # Col 4: Details (Badges + Text)
            detail_text = f"{ext} • {vcodec} • {sz} • {fid}"
            detail_badges = _analyze_format_tags(f)

            # Use Left alignment for details as it can be long
            d_w = QualityCellWidget(
                detail_badges,
                detail_text,
                parent=self.video_table,
                alignment=Qt.AlignmentFlag.AlignCenter,
            )

            item = QTableWidgetItem("")
            self.video_table.setItem(i, 4, item)
            self.video_table.setCellWidget(i, 4, d_w)

            self._video_rows.append({"format_id": fid, "height": h, "ext": ext})

        # 自动选中第一行
        if self._video_rows:
            self.video_table.selectRow(0)

    # ── 事件 ──

    def _on_mode_changed(self, index: int) -> None:
        # 0=组合, 1=仅视频, 2=仅音频
        self.video_label.setVisible(index != 2)
        self.video_table.setVisible(index != 2)
        self.audio_label.setVisible(index != 1)
        self.audio_table.setVisible(index != 1)
        self._update_summary()
        self.selectionChanged.emit()

    def _on_video_selected(self) -> None:
        rows = self.video_table.selectionModel().selectedRows()
        if rows:
            r = rows[0].row()
            if 0 <= r < len(self._video_rows):
                self._selected_video_id = self._video_rows[r]["format_id"]
        self._update_summary()
        self.selectionChanged.emit()

    def _on_audio_selected(self) -> None:
        rows = self.audio_table.selectionModel().selectedRows()
        if rows:
            r = rows[0].row()
            if 0 <= r < len(self._audio_rows):
                self._selected_audio_id = self._audio_rows[r]["format_id"]
        self._update_summary()
        self.selectionChanged.emit()

    def _update_summary(self) -> None:
        mode = self.mode_combo.currentIndex()
        parts = []
        if mode != 2 and self._selected_video_id:
            parts.append(f"\u89c6\u9891: {self._selected_video_id}")
        if mode != 1 and self._selected_audio_id:
            parts.append(f"\u97f3\u9891: {self._selected_audio_id}")
        self.summary_label.setText(
            "  |  ".join(parts) if parts else "\u8bf7\u9009\u62e9\u683c\u5f0f"
        )

    # ── 公开接口 ──

    def get_selection_result(self) -> dict[str, Any]:
        mode = self.mode_combo.currentIndex()

        if mode == 2:
            # 仅音频
            aid = self._selected_audio_id
            return {
                "format": str(aid) if aid else "bestaudio/best",
                "extra_opts": {},
            }
        elif mode == 1:
            # 仅视频
            vid = self._selected_video_id
            return {
                "format": str(vid) if vid else "bestvideo/best",
                "extra_opts": {},
            }
        else:
            # 组合
            vid = self._selected_video_id
            aid = self._selected_audio_id
            if vid and aid:
                v_ext = None
                a_ext = None
                for r in self._video_rows:
                    if r["format_id"] == vid:
                        v_ext = r.get("ext")
                        break
                for r in self._audio_rows:
                    if r["format_id"] == aid:
                        a_ext = r.get("ext")
                        break
                merge = _choose_lossless_merge_container(v_ext, a_ext)
                extra: dict[str, Any] = {}
                if merge:
                    extra["merge_output_format"] = merge
                return {
                    "format": f"{vid}+{aid}",
                    "extra_opts": extra,
                }
            elif vid:
                return {"format": f"{vid}+bestaudio/best", "extra_opts": {}}
            else:
                return {"format": "bestvideo+bestaudio/best", "extra_opts": {}}


# ── 组合控件：简易/专业切换 ─────────────────────────────────


class VRFormatSelectorWidget(QWidget):
    """VR 格式选择器：简易模式预设 + 专业模式表格"""

    selectionChanged = Signal()

    def __init__(self, info: dict[str, Any], parent=None):
        super().__init__(parent)
        self._info = info

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 模式切换标签
        self.mode_seg = SegmentedWidget(self)
        self.mode_seg.addItem("simple", "\u7b80\u6613\u6a21\u5f0f")
        self.mode_seg.addItem("pro", "\u4e13\u4e1a\u6a21\u5f0f")
        self.mode_seg.setCurrentItem("simple")
        self.mode_seg.currentItemChanged.connect(self._on_mode_switch)
        layout.addWidget(self.mode_seg)

        # 简易模式
        self.preset_widget = VRPresetWidget(self)
        layout.addWidget(self.preset_widget)

        # 专业模式
        self.pro_widget = VRFormatTableWidget(info, self)
        self.pro_widget.selectionChanged.connect(self.selectionChanged)
        self.pro_widget.hide()
        layout.addWidget(self.pro_widget)

    def _on_mode_switch(self, key: str) -> None:
        if key == "simple":
            self.preset_widget.show()
            self.pro_widget.hide()
        else:
            self.preset_widget.hide()
            self.pro_widget.show()
        self.selectionChanged.emit()

    def get_selection_result(self) -> dict[str, Any]:
        """返回当前选择：{format: str, extra_opts: dict}"""
        if self.mode_seg.currentItem() == "simple":
            res = self.preset_widget.get_current_selection()
        else:
            res = self.pro_widget.get_selection_result()

        # 注入 VR 元数据 (从 self._info 获取)
        summary = self._info.get("__vr_projection_summary") or {}
        proj = summary.get("primary_projection")
        stereo = summary.get("primary_stereo")

        if proj and stereo:
            extra = res.setdefault("extra_opts", {})
            extra["__vr_projection"] = proj
            extra["__vr_stereo_mode"] = stereo

        return res

    def get_summary_text(self) -> str:
        sel = self.get_selection_result()
        return sel.get("format", "bestvideo+bestaudio/best")
