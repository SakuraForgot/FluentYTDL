from __future__ import annotations

import os
import re
import subprocess
from typing import Any

from PySide6.QtCore import QCoreApplication, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget
from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    InfoBar,
    InfoBarPosition,
    MessageBox,
    ProgressBar,
    StrongBodyLabel,
    ToolTipFilter,
    ToolTipPosition,
    TransparentToolButton,
)

from ....download.download_manager import download_manager
from ....download.workers import DownloadWorker
from ....utils.image_loader import ImageLoader

_ANSI_ESCAPE_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", text or "")


def _format_bytes(num_bytes: float | int | None) -> str:
    if num_bytes is None:
        return "0 B"
    try:
        value = float(num_bytes)
    except Exception:
        return "0 B"
    if value <= 0:
        return "0 B"

    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    return f"{value:.2f} {units[unit_index]}"


def _format_time(seconds: int | float | None) -> str:
    if seconds is None:
        return "--:--"
    try:
        total_seconds = int(seconds)
    except Exception:
        return "--:--"
    if total_seconds < 0:
        return "--:--"
    minutes, sec = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{sec:02d}"
    return f"{minutes:02d}:{sec:02d}"


def _infer_stream_label(d: dict[str, Any]) -> str:
    """尽力识别当前下载的是视频流、音频流还是字幕/封面。"""

    def tr(text: str) -> str:
        return QCoreApplication.translate("DownloadCard", text)

    # 1. 优先使用执行器传入的明确标签 (Aria2c / Executor Injected)
    label = d.get("label")
    if label and label in (tr("视频"), tr("音频"), tr("字幕"), tr("封面")):
        return f"[{label}]"

    # 2. 检查 info_dict (Native yt-dlp)
    info = d.get("info_dict")
    if isinstance(info, dict):
        vcodec = info.get("vcodec")
        acodec = info.get("acodec")
        if isinstance(vcodec, str) and isinstance(acodec, str):
            if vcodec != "none" and acodec == "none":
                return tr("[视频]")
            if vcodec == "none" and acodec != "none":
                return tr("[音频]")

    # 3. 检查文件名后缀
    filename = d.get("filename")
    if isinstance(filename, str):
        lower = filename.lower()
        if lower.endswith((".vtt", ".srt", ".ass", ".lrc")):
            return tr("[字幕]")
        if lower.endswith((".jpg", ".jpeg", ".webp", ".png")):
            return tr("[封面]")
        if lower.endswith((".json", ".xml")):
            return tr("[元数据]")
        if lower.endswith((".m4a", ".mp3", ".aac", ".opus", ".ogg", ".flac", ".wav")):
            return tr("[音频]")
        if lower.endswith((".mp4", ".webm", ".mkv", ".mov", ".avi")):
            return tr("[视频]")

    # 4. 有标签但未命中特定类型
    if label:
        return f"[{label}]"

    return tr("[下载]")


class DownloadItemCard(CardWidget):
    """智能下载卡片：支持 暂停/继续/删除"""

    remove_requested = Signal(QWidget)
    state_changed = Signal(str)
    selection_changed = Signal(bool)

    def __init__(self, worker: DownloadWorker, title: str, opts: dict[str, Any], parent=None):
        super().__init__(parent)
        self.worker = worker
        self.title_text = title
        self.url = worker.url
        self.opts = dict(opts)

        self.image_loader = ImageLoader(self)
        self.image_loader.loaded.connect(self._on_thumb_loaded)

        # === 样式布局 ===
        self.setFixedHeight(84)
        self.hLayout = QHBoxLayout(self)
        self.hLayout.setContentsMargins(16, 16, 16, 16)
        self.hLayout.setSpacing(16)

        # 0. 批量选择复选框（默认隐藏）
        self.selectBox = QCheckBox(self)
        self.selectBox.setTristate(False)
        self.selectBox.setVisible(False)
        self.selectBox.toggled.connect(self.selection_changed)
        self.hLayout.addWidget(self.selectBox)

        # 1. 左侧缩略图 (16:9)
        self.iconLabel = QLabel(self)
        self.iconLabel.setFixedSize(80, 45)

        # Style setup moved to _update_style

        self.iconLabel.setScaledContents(True)
        self.hLayout.addWidget(self.iconLabel)

        # 2. 中间信息区
        self.infoLayout = QVBoxLayout()
        self.infoLayout.setSpacing(4)
        self.infoLayout.setAlignment(Qt.AlignmentFlag.AlignVCenter)

        self.titleLabel = StrongBodyLabel(self.title_text, self)
        self.titleLabel.setWordWrap(False)

        self.progressBar = ProgressBar(self)
        self.progressBar.setValue(0)
        self.progressBar.setFixedHeight(4)

        self.statusLabel = CaptionLabel(self.tr("等待开始..."), self)
        self.statusLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))

        self.infoLayout.addWidget(self.titleLabel)
        self.infoLayout.addWidget(self.progressBar)
        self.infoLayout.addWidget(self.statusLabel)
        self.hLayout.addLayout(self.infoLayout, 1)

        # 3. 右侧按钮区
        self.actionBtn = TransparentToolButton(FluentIcon.PAUSE, self)
        self.actionBtn.setToolTip(self.tr("暂停任务"))
        self.actionBtn.installEventFilter(
            ToolTipFilter(self.actionBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.actionBtn.clicked.connect(self.on_action_clicked)

        self.folderBtn = TransparentToolButton(FluentIcon.FOLDER, self)
        self.folderBtn.setToolTip(self.tr("打开文件夹"))
        self.folderBtn.installEventFilter(
            ToolTipFilter(self.folderBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.folderBtn.setEnabled(False)
        self.folderBtn.clicked.connect(self._open_output_location)

        self.deleteBtn = TransparentToolButton(FluentIcon.DELETE, self)
        self.deleteBtn.setToolTip(self.tr("删除任务"))
        self.deleteBtn.installEventFilter(
            ToolTipFilter(self.deleteBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.deleteBtn.clicked.connect(self.on_delete_clicked)

        self.reportBtn = TransparentToolButton(FluentIcon.GITHUB, self)
        self.reportBtn.setToolTip(self.tr("反馈此错误"))
        self.reportBtn.installEventFilter(
            ToolTipFilter(self.reportBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.reportBtn.setVisible(False)
        self.reportBtn.clicked.connect(self.on_report_clicked)

        self.hLayout.addWidget(self.reportBtn)
        self.hLayout.addWidget(self.actionBtn)
        self.hLayout.addWidget(self.folderBtn)
        self.hLayout.addWidget(self.deleteBtn)

        # === 信号连接 ===
        self._bind_worker(self.worker)

        self._output_path: str | None = None

        # Explicit state for grouping/filtering.
        # running / queued / paused / completed / error
        self._state: str = "queued"

        from qfluentwidgets import qconfig

        qconfig.themeChanged.connect(self._update_style)
        self._update_style()

    def _update_style(self):
        from qfluentwidgets import isDarkTheme

        bg_alpha = "rgba(255, 255, 255, 0.06)" if isDarkTheme() else "rgba(0, 0, 0, 0.06)"
        self.iconLabel.setStyleSheet(f"background-color: {bg_alpha}; border-radius: 6px;")

    def state(self) -> str:
        return self._state

    def set_state(self, state: str) -> None:
        s = str(state or "").strip().lower()
        if s not in {"running", "queued", "paused", "completed", "error"}:
            s = "queued"
        if s == self._state:
            return
        self._state = s
        self.state_changed.emit(self._state)

    def set_selection_mode(self, enabled: bool) -> None:
        self.selectBox.setVisible(bool(enabled))
        if not enabled:
            try:
                self.selectBox.setChecked(False)
            except Exception:
                pass

    def is_selected(self) -> bool:
        try:
            return bool(self.selectBox.isVisible() and self.selectBox.isChecked())
        except Exception:
            return False

    def _bind_worker(self, worker: DownloadWorker) -> None:
        self.worker = worker
        self.worker.progress.connect(self.update_progress, Qt.ConnectionType.UniqueConnection)
        self.worker.status_msg.connect(self.update_status, Qt.ConnectionType.UniqueConnection)
        self.worker.completed.connect(self.on_finished, Qt.ConnectionType.UniqueConnection)
        self.worker.error.connect(self.on_error, Qt.ConnectionType.UniqueConnection)
        try:
            self.worker.output_path_ready.connect(
                self._on_output_path_ready, Qt.ConnectionType.UniqueConnection
            )
        except Exception:
            pass

        # Also forward to MainWindow if it provides a structured error dialog.
        try:
            print("[DEBUG] Connecting worker.error to _forward_error_to_window")
            self.worker.error.connect(
                self._forward_error_to_window, Qt.ConnectionType.UniqueConnection
            )
            print("[DEBUG] Connected successfully")
        except Exception as e:
            print(f"[ERROR] Failed to connect _forward_error_to_window: {e}")

    def _forward_error_to_window(self, err_data: dict) -> None:
        print(f"[DEBUG] _forward_error_to_window called on DownloadCard for {self.url}")
        try:
            win = self.window()
            print(f"[DEBUG] DownloadCard.window() is {win}")
            handler = getattr(win, "on_worker_error", None)
            print(f"[DEBUG] handler found: {handler}")
            if callable(handler):
                handler(err_data)
        except Exception as e:
            print(f"[ERROR] Exception in _forward_error_to_window: {e}")

    def _jump_to_settings(self, hint: str) -> None:
        """跳转到设置页并显示提示"""
        try:
            main_win = self.window()
            # 优先使用 switchTo(settings_interface)
            settings_iface = getattr(main_win, "settings_interface", None)
            if settings_iface is not None:
                main_win.switchTo(settings_iface)  # type: ignore[attr-defined]
            else:
                handler = getattr(main_win, "switch_to_settings", None)
                if callable(handler):
                    handler()
            InfoBar.info(
                hint,
                self.tr("操作完成后可重试下载"),
                parent=main_win,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        except Exception:
            pass

    def _retry_download(self) -> None:
        try:
            download_manager.remove_worker(self.worker)
        except Exception:
            pass

        new_worker = download_manager.create_worker(self.url, self.opts)
        self._bind_worker(new_worker)
        started = download_manager.start_worker(new_worker)

        try:
            self.progressBar.setValue(0)
        except Exception:
            pass
        if hasattr(self.progressBar, "setError"):
            try:
                self.progressBar.setError(False)
            except Exception:
                pass
        try:
            self.folderBtn.setEnabled(False)
        except Exception:
            pass
        try:
            self.actionBtn.setIcon(FluentIcon.PAUSE)
            self.actionBtn.setToolTip(self.tr("暂停任务"))
        except Exception:
            pass
        try:
            self.statusLabel.setText(self.tr("正在重试...") if started else self.tr("排队中..."))
        except Exception:
            pass
        self.set_state("running" if started else "queued")

    def update_progress(self, d: dict[str, Any]) -> None:
        if d.get("status") == "downloading":
            self.set_state("running")
            stream_label = _infer_stream_label(d)

            # 轻量提取通道（字幕/封面）可能只提供百分比。
            pct_raw = d.get("__fluentytdl_percent")
            if pct_raw is not None:
                try:
                    pct = int(float(pct_raw))
                except Exception:
                    pct = 0
                pct = max(0, min(100, pct))
                self.progressBar.setValue(pct)
                self.statusLabel.setText(f"{stream_label} 提取中 {pct}%")
                return

            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes") or 0
            speed = d.get("speed") or 0
            eta = d.get("eta")

            if total and total > 0:
                self.progressBar.setValue(int(downloaded / total * 100))
                size_str = f"{_format_bytes(downloaded)} / {_format_bytes(total)}"
            else:
                self.progressBar.setValue(0)
                size_str = f"{_format_bytes(downloaded)} / ?"

            speed_str = f"{_format_bytes(speed)}/s"
            eta_str = f"剩余 {_format_time(eta)}"

            self.statusLabel.setText(f"{stream_label} {speed_str} • {size_str} • {eta_str}")

    def update_status(self, msg: str) -> None:
        clean_msg = _strip_ansi(msg)
        self.statusLabel.setText(clean_msg)

        # 合并/处理阶段：给用户明确感知（进度条可保持接近满格）
        if self.tr("合并") in clean_msg or self.tr("处理") in clean_msg:
            self.progressBar.setValue(99)

        clean_msg.lower()
        if self.tr("排队") in clean_msg or self.tr("等待") in clean_msg:
            self.set_state("queued")
        if self.tr("暂停") in clean_msg:
            self.set_state("paused")

    def on_finished(self) -> None:
        self.progressBar.setValue(100)
        self.statusLabel.setText(self.tr("下载完成"))
        self.set_state("completed")
        self.actionBtn.setEnabled(False)
        self.folderBtn.setEnabled(True)
        InfoBar.info(
            self.tr("下载完成"),
            self.title_text,
            parent=self.window(),
            position=InfoBarPosition.TOP_RIGHT,
        )

    def _on_output_path_ready(self, path: str) -> None:
        p = str(path or "").strip()
        self._output_path = p or None

    def _open_output_location(self) -> None:
        # Prefer selecting the concrete output file if we have it.
        p = self._output_path or getattr(self.worker, "output_path", None)
        if isinstance(p, str) and p.strip():
            p = os.path.abspath(p)
            if os.name == "nt" and os.path.exists(p):
                try:
                    subprocess.Popen(["explorer", "/select,", p])
                    return
                except Exception:
                    pass

        # Fallback: open download directory.
        d = getattr(self.worker, "download_dir", None)
        if isinstance(d, str) and d.strip():
            d = os.path.abspath(d)
        else:
            d = os.path.abspath(os.getcwd())

        try:
            QDesktopServices.openUrl(QUrl.fromLocalFile(d))
        except Exception:
            try:
                if os.name == "nt":
                    os.startfile(d)  # type: ignore[attr-defined]
            except Exception:
                pass

    def on_error(self, err_data: dict) -> None:
        # 兼容 Diagnosis.to_dict() 的结构化 dict 与旧的纯字符串 dict
        if "code" in err_data and "severity" in err_data:
            friendly_title = err_data.get("user_title", self.tr("下载出错"))
            err_msg = err_data.get("user_message", self.tr("未知错误"))
            fix_action = err_data.get("fix_action")
            recovery_hint = err_data.get("recovery_hint")
            technical_detail = err_data.get("technical_detail", "")
        else:
            err_msg = str(err_data.get("raw_error") or err_data.get("content") or "")
            friendly_title = str(err_data.get("title") or self.tr("下载出错"))
            fix_action = None
            recovery_hint = None
            technical_detail = err_msg

        self.statusLabel.setText(self.tr("出错"))
        self.set_state("error")
        if hasattr(self.progressBar, "setError"):
            self.progressBar.setError(True)
        self.actionBtn.setEnabled(True)
        self.actionBtn.setIcon(FluentIcon.PLAY)
        self.actionBtn.setToolTip(self.tr("继续/重试"))

        if fix_action:
            # 如果包含诊断出的修复方案，使用 MessageBox 弹出并引导修复
            try:
                from fluentytdl.ui.components.settings.fix_registry import execute_fix_action

                detail_text = (
                    f"{technical_detail[:1000]}..."
                    if len(technical_detail) > 1000
                    else technical_detail
                )
                box = MessageBox(
                    friendly_title,
                    f"{err_msg}\n\n[技术详情]\n{detail_text}",
                    parent=self.window(),
                )
                box.yesButton.setText(recovery_hint or self.tr("去处理"))
                box.cancelButton.setText(self.tr("关闭"))
                if box.exec():
                    execute_fix_action(fix_action, self)
            except Exception:
                pass
        else:
            # 否则显示普通错误通知条
            InfoBar.error(
                friendly_title,
                err_msg,
                parent=self.window(),
                position=InfoBarPosition.TOP_RIGHT,
            )

        # Issue URL handler
        issue_url = err_data.get("issue_url")
        if issue_url:
            self.reportBtn.setVisible(True)
            self._current_issue_url = issue_url

        # If format is not available, offer downgrade/reselect.
        err_code = err_data.get("code")
        # 兼容新规则引擎抛出的 FORMAT_UNAVAILABLE(3) 或旧版降级关键词
        lower = (err_msg or "").lower()
        if (
            err_code == 3
            or "requested format is not available" in lower
            or "no video formats" in lower
            or "no formats" in lower
            or "requested format" in lower
            and "not available" in lower
        ):
            self._maybe_handle_format_unavailable(err_msg)

    def on_report_clicked(self) -> None:
        issue_url = getattr(self, "_current_issue_url", None)
        if issue_url:
            QDesktopServices.openUrl(QUrl(issue_url))

    def _maybe_handle_format_unavailable(self, err_msg: str) -> None:
        raw_height = self.opts.get("__fluentytdl_quality_height")
        try:
            current_height = int(raw_height) if raw_height is not None else None
        except Exception:
            current_height = None

        # Only handle strict height presets.
        if not current_height:
            return

        box = MessageBox(
            self.tr("预设质量不可用"),
            f"当前任务预设为 {current_height}p，但该视频可能不提供该档位。\n\n"
            f"错误信息：{err_msg}\n\n" + self.tr("可选择自动降低档位重试，或手动调整格式。"),
            parent=self.window(),
        )
        box.yesButton.setText(self.tr("自动降档重试"))
        box.cancelButton.setText(self.tr("手动调整"))
        if not box.exec():
            # Manual adjust: open selection dialog for this single video
            try:
                from fluentytdl.ui.components.dialogs.selection_dialog import SelectionDialog
            except Exception:
                return

            dlg = SelectionDialog(self.url, self.window())
            if not dlg.exec():
                return

            new_opts = dlg.get_download_options()
            if isinstance(new_opts, dict) and new_opts:
                # Keep internal meta unless user switched to non-height preset.
                self.opts.update(new_opts)
                self.opts.pop("__fluentytdl_quality_height", None)

                try:
                    download_manager.remove_worker(self.worker)
                except Exception:
                    pass

                new_worker = download_manager.create_worker(self.url, self.opts)
                self._bind_worker(new_worker)
                started = download_manager.start_worker(new_worker)
                self.progressBar.setValue(0)
                if hasattr(self.progressBar, "setError"):
                    self.progressBar.setError(False)
                self.folderBtn.setEnabled(False)
                self.actionBtn.setIcon(FluentIcon.PAUSE)
                self.actionBtn.setToolTip(self.tr("暂停任务"))
                self.statusLabel.setText(
                    self.tr("已手动调整格式，开始下载...")
                    if started
                    else self.tr("已手动调整格式，排队中...")
                )
            return

        next_height = self._next_lower_height(current_height)
        if next_height is None:
            InfoBar.warning(
                self.tr("无法继续降档"),
                self.tr("已是最低预设档位，建议手动调整格式。"),
                parent=self.window(),
                position=InfoBarPosition.TOP_RIGHT,
            )
            return

        self.opts["__fluentytdl_quality_height"] = next_height
        self._apply_height_preset_to_opts(next_height)

        # Retry by recreating worker
        try:
            download_manager.remove_worker(self.worker)
        except Exception:
            pass

        new_worker = download_manager.create_worker(self.url, self.opts)
        self._bind_worker(new_worker)
        started = download_manager.start_worker(new_worker)
        self.progressBar.setValue(0)
        if hasattr(self.progressBar, "setError"):
            self.progressBar.setError(False)
        self.actionBtn.setIcon(FluentIcon.PAUSE)
        self.actionBtn.setToolTip(self.tr("暂停任务"))
        self.statusLabel.setText(
            f"自动降档至 {next_height}p，开始下载..."
            if started
            else f"自动降档至 {next_height}p，排队中..."
        )

    @staticmethod
    def _next_lower_height(current: int) -> int | None:
        ladder = [2160, 1440, 1080, 720, 480, 360]
        try:
            i = ladder.index(current)
        except ValueError:
            # pick nearest lower
            lower = [h for h in ladder if h < current]
            return lower[0] if lower else None
        return ladder[i + 1] if i + 1 < len(ladder) else None

    def _apply_height_preset_to_opts(self, height: int) -> None:
        """Apply strict height preset to yt-dlp format expression."""

        # Keep it strict (height=), so unavailable will still trigger prompt and ladder.
        self.opts["format"] = f"bestvideo[height={height}]+bestaudio/best"

    def on_action_clicked(self) -> None:
        if self.worker.isRunning():
            # 暂停
            self.worker.stop()
            self.actionBtn.setIcon(FluentIcon.PLAY)
            self.actionBtn.setToolTip(self.tr("继续下载"))
            self.statusLabel.setText(self.tr("已暂停"))
            self.set_state("paused")
        else:
            # 继续（重建 worker，利用 yt-dlp 断点续传）
            try:
                download_manager.remove_worker(self.worker)
            except Exception:
                pass

            new_worker = download_manager.create_worker(self.url, self.opts)
            self._bind_worker(new_worker)
            started = download_manager.start_worker(new_worker)

            self.progressBar.setValue(0)
            if hasattr(self.progressBar, "setError"):
                self.progressBar.setError(False)
            self.folderBtn.setEnabled(False)
            self.actionBtn.setIcon(FluentIcon.PAUSE)
            self.actionBtn.setToolTip(self.tr("暂停任务"))
            self.statusLabel.setText(self.tr("正在恢复...") if started else self.tr("排队中..."))
            self.set_state("running" if started else "queued")

    def on_delete_clicked(self) -> None:
        self.remove_requested.emit(self)

    def load_thumbnail(self, url: str) -> None:
        # 80x45 为 16:9，小圆角
        self.image_loader.load(url, target_size=(80, 45), radius=6)

    def _on_thumb_loaded(self, pixmap) -> None:
        self.iconLabel.setPixmap(pixmap)
