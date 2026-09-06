"""日志查看器 —— 独立非模态窗口。

两页并列：

- **全部日志** —— 整个进程的文本流，按级别/关键词过滤。适合"刚才那一瞬发生了什么"。
- **任务时间线** —— Observability Event 按 flow → task → run → attempt → stage 分组。
  适合"这个任务这一轮都经过了什么"。并发下载时文本流里三个任务的行是交错的，
  只看文本永远只能读到三条链的碎片。

两页共用同一套级别/搜索过滤条件，切页时筛选结果对得上。

**为什么是独立窗口而不是遮罩对话框**：原先是 `MessageBoxBase`（`MaskDialogBase` +
`exec()`）。看日志的唯一用途是**对照**正在跑的下载 —— 哪个任务卡住了、进度停在哪一格、
这时候点重试会发生什么。而遮罩把主窗口整片盖住、`exec()` 又把事件循环阻塞住，恰好
把"对照"变成了"轮流看"，连另开一个下载都做不到。现在它有自己的任务栏条目，可以拖到
第二块屏上和主窗口并排放，一边看一边操作。
"""

from __future__ import annotations

import io
import os
import re
import zipfile
from collections import deque
from pathlib import Path

from PySide6.QtCore import Slot
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QWidget,
)
from qfluentwidgets import (
    CaptionLabel,
    ComboBox,
    FluentIcon,
    InfoBarPosition,
    SearchLineEdit,
    SegmentedWidget,
    SubtitleLabel,
    ToolButton,
    ToolTipFilter,
    ToolTipPosition,
)

from ....utils.log_signal_handler import log_signal_handler
from ....utils.logger import LOG_DIR
from ..common.custom_info_bar import InfoBar
from ..common.event_timeline import EventTimelineView
from ..common.standalone_window import StandaloneWindow

# 日志级别颜色映射
LEVEL_COLORS = {
    "DEBUG": "#888888",
    "INFO": "#2196F3",
    "SUCCESS": "#4CAF50",
    "WARNING": "#FF9800",
    "ERROR": "#F44336",
    "CRITICAL": "#9C27B0",
}

# 日志级别排序（用于过滤）
LEVEL_ORDER = ["DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"]

#: 回填多少行历史（跨文件累计）。
HISTORY_LINES = 500

#: loguru **默认**文件格式 —— `utils/logger.py` 的文件 sink 没传 `format=`：
#: ``2026-08-25 14:03:12.345 | INFO     | fluentytdl.download.workers:run:812 - 消息``
#: 级别被 `{level: <8}` 补空格到 8 位，所以 `\s*` 不能省。
_FILE_LINE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} (?P<time>\d{2}:\d{2}:\d{2})\.\d+ \| "
    r"(?P<level>[A-Z]+)\s*\| (?P<module>\S+) - (?P<msg>.*)$"
)


class LogViewerWindow(StandaloneWindow):
    """实时日志查看器（独立窗口）"""

    MAX_LINES = 1000  # 缓冲区与文本页共同的最大行数

    #: 再小四列就排不开了。`SIZE_BOUNDS` 的下限（760×520）是"我替你选的初始大小"，
    #: 这个是"再小没法用"。
    MIN_SIZE = (680, 460)

    def __init__(self, anchor: QWidget | None = None):
        super().__init__(anchor=anchor)
        self.setWindowTitle(self.tr("运行日志"))

        self._log_buffer: deque[tuple[str, str, str, str]] = deque(maxlen=self.MAX_LINES)
        self._current_filter_level = self.tr("全部")
        self._current_search = ""
        self._auto_scroll = True
        self._capturing = False

        self._setup_ui()
        self._connect_signals()
        # 先回填历史、再接实时信号：反过来的话，这中间产生的日志会排在历史前面，
        # 而时间线页更糟 —— 同一条事件可能既在 JSONL 里又从信号来一遍。
        self._load_existing_logs()
        self.timelineView.load_recent()
        # 回填完再套一次筛选。少了这一句，下拉框写着一个级别、树里却是全量事件 ——
        # 用户得先随便动一下控件才会一致，在此之前界面在说谎。
        self.timelineView.apply_filter(self._should_show)
        self._start_log_capture()

    # ── 历史回填 ─────────────────────────────────────────────

    def _load_existing_logs(self):
        """回填历史日志：今天的 `app_*.log` **以及压缩过的往日日志**。

        `rotation="00:00" + compression="zip"` 意味着昨天之前的日志全在 `.log.zip`
        里。原先只读 `app_{today}.log`，于是刚过零点或刚重启时这一页几乎是空的，
        而"昨晚那次失败"恰恰是最常要回看的东西。
        """
        try:
            entries: list[tuple[str, str, str, str]] = []
            budget = HISTORY_LINES
            for path in reversed(self._history_files()):  # 从新到旧凑够 budget 行
                if budget <= 0:
                    break
                lines = self._tail_lines(path, budget)
                if not lines:
                    continue
                entries[:0] = self._parse_lines(lines)  # 旧文件排在前面
                budget -= len(lines)

            for time_str, level, module, msg in entries:
                self._log_buffer.append((time_str, level, module, msg))
                if self._should_show(level, msg):
                    self._append_log_line(time_str, level, module, msg)
        except Exception:
            pass  # 加载失败不影响实时日志

    @staticmethod
    def _history_files() -> list[Path]:
        """`app_*.log` 与 `app_*.log.zip`，按文件名里的日期从旧到新。

        不按 mtime 排：zip 的 mtime 是**压缩时刻**（轮转那一瞬），同一批被 loguru
        补压缩时会挤在同一秒，排出来的先后是随机的。文件名里的日期才是内容的日期。
        """
        try:
            log_dir = Path(LOG_DIR)
            files = [*log_dir.glob("app_*.log"), *log_dir.glob("app_*.log.zip")]

            def sort_key(path: Path) -> tuple[str, str]:
                match = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
                return (match.group(1) if match else "", path.name)

            return sorted(files, key=sort_key)
        except Exception:
            return []

    @staticmethod
    def _tail_lines(path: Path, limit: int) -> list[str]:
        """取文件末尾 `limit` 行（zip 会解压其中所有成员）。

        用 `deque(maxlen=...)` 逐行喂：一天的 DEBUG 日志能有几百 MB，
        `readlines()` 会把整份读进内存，而这里只需要末尾几百行。
        """
        try:
            if path.suffix.lower() == ".zip":
                collected: deque[str] = deque(maxlen=limit)
                with zipfile.ZipFile(path) as zf:
                    for name in zf.namelist():
                        if name.endswith("/"):
                            continue
                        with zf.open(name) as raw:
                            stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                            collected.extend(deque(stream, maxlen=limit))
                return list(collected)
            with open(path, encoding="utf-8", errors="replace") as f:
                return list(deque(f, maxlen=limit))
        except Exception:
            return []

    @staticmethod
    def _parse_lines(lines: list[str]) -> list[tuple[str, str, str, str]]:
        """按 loguru 默认格式解析历史行。

        解析不出来的行（异常堆栈、多行 message）**继承上一条的时间与级别** ——
        否则一段 traceback 会和它的 ERROR 头分家：筛 ERROR 时只剩一行"Uncaught
        exception"，堆栈全被滤掉。

        原先每行都被塞成 `("--:--:--", "INFO", "file", line)`，历史日志因此既没有
        真实时间也没有真实级别，级别下拉框筛 ERROR 时历史里的错误一条都筛不出来。
        """
        out: list[tuple[str, str, str, str]] = []
        time_str, level = "--:--:--", "INFO"
        for raw in lines:
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            match = _FILE_LINE_RE.match(line)
            if match:
                time_str = match.group("time")
                level = match.group("level")
                out.append((time_str, level, match.group("module"), match.group("msg")))
            else:
                out.append((time_str, level, "", line))
        return out

    # ── UI ──────────────────────────────────────────────────

    def _setup_ui(self):
        """构建 UI"""
        # 标题。`ThemedTitleBar` 刻意只放三个系统按钮、不画标题文字，所以窗口名字在
        # 这里出现一次；顶部 44px 内容边距就是给标题栏留的位置。
        self.titleLabel = SubtitleLabel(self.tr("📋 运行日志"), self)
        self.viewLayout.addWidget(self.titleLabel)

        # 页面切换
        self.viewSwitcher = SegmentedWidget(self)
        self.viewSwitcher.addItem("text", self.tr("全部日志"))
        self.viewSwitcher.addItem("timeline", self.tr("任务时间线"))
        self.viewLayout.addWidget(self.viewSwitcher)

        # 工具栏（两页共用）
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 8, 0, 8)

        # 级别过滤。默认 DEBUG 而不是 INFO：决策链（`kind=decision`，bcp47 展开 /
        # 格式打分 / 音轨注入）刻意打在 DEBUG —— 它高频，不该盖住正常日志。可默认
        # 筛到 INFO 的话，时间线页一打开就把这一整层滤掉了，而"字幕为什么没下到"
        # 的答案恰恰只在那一层。DEBUG 与列表里的"全部"在 `LEVEL_ORDER` 上等价，
        # 这里选 DEBUG 是为了让下拉框显示的东西和实际看到的东西对得上。
        self.levelCombo = ComboBox()
        self.levelCombo.addItems([self.tr("全部"), "DEBUG", "INFO", "WARNING", "ERROR"])
        self.levelCombo.setCurrentText("DEBUG")
        self._current_filter_level = "DEBUG"
        toolbar_layout.addWidget(self.levelCombo)

        # 搜索框
        self.searchEdit = SearchLineEdit()
        self.searchEdit.setPlaceholderText(self.tr("搜索日志..."))
        self.searchEdit.setFixedWidth(200)
        toolbar_layout.addWidget(self.searchEdit)

        toolbar_layout.addStretch()

        # 导出 bug 包（只在时间线页有意义：要先有选中的任务）
        self.exportBtn = ToolButton(FluentIcon.ZIP_FOLDER)
        self.exportBtn.setToolTip(self.tr("导出选中任务的 bug 包"))
        self.exportBtn.installEventFilter(
            ToolTipFilter(self.exportBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        self.exportBtn.setVisible(False)
        toolbar_layout.addWidget(self.exportBtn)

        # 清屏按钮
        self.clearBtn = ToolButton(FluentIcon.DELETE)
        self.clearBtn.setToolTip(self.tr("清屏"))
        self.clearBtn.installEventFilter(
            ToolTipFilter(self.clearBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        toolbar_layout.addWidget(self.clearBtn)

        # 打开目录按钮
        self.openDirBtn = ToolButton(FluentIcon.FOLDER)
        self.openDirBtn.setToolTip(self.tr("打开日志目录"))
        self.openDirBtn.installEventFilter(
            ToolTipFilter(self.openDirBtn, showDelay=300, position=ToolTipPosition.BOTTOM)
        )
        toolbar_layout.addWidget(self.openDirBtn)

        self.viewLayout.addWidget(toolbar)

        # 日志显示区。这一页刻意保留控制台配色：`LEVEL_COLORS` 那六个值是照着深色
        # 背景调的，跟着主题变浅只会让 DEBUG 的灰字消失。新的时间线页走 isDarkTheme()。
        self.logView = QPlainTextEdit()
        self.logView.setReadOnly(True)
        # 文档行数封顶。改成独立窗口后它可以开着几个小时，而 `_log_buffer` 的
        # `maxlen` 只管缓冲区、管不到已经插进文档里的行 —— 少了这一句，一次长时间
        # 的批量下载能把这个文本框喂到几十万行。
        self.logView.setMaximumBlockCount(self.MAX_LINES)
        log_font = QFont("Consolas")
        log_font.setPointSize(10)
        self.logView.setFont(log_font)
        self.logView.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.logView.setStyleSheet("""
            QPlainTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: 1px solid #3c3c3c;
                border-radius: 4px;
            }
        """)
        self.logView.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        # 任务时间线
        self.timelineView = EventTimelineView(self)
        self.timelineView.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.logView)
        self.stack.addWidget(self.timelineView)
        self.viewLayout.addWidget(self.stack, 1)
        self.viewSwitcher.setCurrentItem("text")

        # 状态栏
        status_layout = QHBoxLayout()
        self.statusLabel = CaptionLabel(self.tr("日志目录: {}").format(LOG_DIR))
        self.statusLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        status_layout.addWidget(self.statusLabel)
        status_layout.addStretch()

        self.lineCountLabel = CaptionLabel(self.tr("0 行"))
        self.lineCountLabel.setTextColor(QColor(96, 96, 96), QColor(210, 210, 210))
        status_layout.addWidget(self.lineCountLabel)

        status_widget = QWidget()
        status_widget.setLayout(status_layout)
        self.viewLayout.addWidget(status_widget)

    def _connect_signals(self):
        """连接信号"""
        self.levelCombo.currentTextChanged.connect(self._on_filter_changed)
        self.searchEdit.textChanged.connect(self._on_search_changed)
        self.clearBtn.clicked.connect(self._clear_log)
        self.openDirBtn.clicked.connect(self._open_log_dir)
        self.exportBtn.clicked.connect(self._export_bundle)
        self.viewSwitcher.currentItemChanged.connect(self._on_view_changed)

        # 滚动检测（用户滚动时暂停自动滚动）
        self.logView.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def _start_log_capture(self):
        """开始捕获日志"""
        log_signal_handler.install()
        log_signal_handler.log_received.connect(self._on_log_received)
        log_signal_handler.event_received.connect(self._on_event_received)
        self._capturing = True

    def _stop_log_capture(self):
        """停止捕获日志。可以重复调用。

        用一个标志位挡住第二次，而不是只靠 try/except：PySide6 对"断开一个没连上的
        信号"发的是 `RuntimeWarning` 而不是抛 `RuntimeError`，`except` 接不住，于是
        重复调用会往 stderr 吐两行 "Failed to disconnect"。而重复调用是正常的 ——
        `closeEvent` 走一遍，调用方在关窗前主动收尾又走一遍。
        """
        if not self._capturing:
            return
        self._capturing = False
        for signal, slot in (
            (log_signal_handler.log_received, self._on_log_received),
            (log_signal_handler.event_received, self._on_event_received),
        ):
            try:
                signal.disconnect(slot)
            except RuntimeError:
                pass  # C++ 侧已经没了

    # ── 文本页 ───────────────────────────────────────────────

    @Slot(str, str, str, str)
    def _on_log_received(self, time: str, level: str, module: str, message: str):
        """接收日志"""
        self._log_buffer.append((time, level, module, message))

        # 检查是否需要显示
        if self._should_show(level, message):
            self._append_log_line(time, level, module, message)

    def _should_show(self, level: str, message: str) -> bool:
        """检查日志是否应该显示"""
        # 级别过滤
        if self._current_filter_level != self.tr("全部"):
            try:
                filter_idx = LEVEL_ORDER.index(self._current_filter_level)
                log_idx = LEVEL_ORDER.index(level) if level in LEVEL_ORDER else 1
                if log_idx < filter_idx:
                    return False
            except ValueError:
                pass

        # 搜索过滤
        if self._current_search:
            if self._current_search.lower() not in message.lower():
                return False

        return True

    def _append_log_line(self, time: str, level: str, module: str, message: str):
        """追加一行日志"""
        color = LEVEL_COLORS.get(level, "#d4d4d4")

        # 格式化日志行
        module_short = module.split(".")[-1] if module else ""
        if module_short:
            line = f"[{time}] [{level:8}] [{module_short}] {message}"
        else:
            line = f"[{time}] [{level:8}] {message}"

        # 使用 HTML 着色
        cursor = self.logView.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        cursor.insertText(line + "\n", fmt)

        # 自动滚动
        if self._auto_scroll:
            self.logView.verticalScrollBar().setValue(self.logView.verticalScrollBar().maximum())

        # 更新行数
        self._update_line_count()

    def _update_line_count(self):
        """更新计数显示（跟随当前页面：文本页数行，时间线页数事件）"""
        stack = getattr(self, "stack", None)
        if stack is not None and stack.currentIndex() == 1:
            self.lineCountLabel.setText(
                self.tr("{} 条事件").format(self.timelineView.event_count())
            )
            return
        count = self.logView.document().lineCount()
        self.lineCountLabel.setText(self.tr("{} 行").format(count))

    # ── 时间线页 ─────────────────────────────────────────────

    @Slot(dict)
    def _on_event_received(self, event: dict):
        """接收 Observability Event（`log_signal_handler` 从 `extra["fytdl"]` 转出来的）"""
        self.timelineView.add_event(event)
        self._update_line_count()

    @Slot(str)
    def _on_view_changed(self, key: str):
        """切页：导出按钮只在时间线页出现（它要按选中的任务导）"""
        self.stack.setCurrentIndex(1 if key == "timeline" else 0)
        self.exportBtn.setVisible(key == "timeline")
        self._update_line_count()

    @Slot()
    def _export_bundle(self):
        """把选中任务的现场打成 zip（JSONL + raw 原文 + 配置快照 + 工具链版本）"""
        identity = self.timelineView.selected_identity()
        if identity is None:
            InfoBar.warning(
                title=self.tr("请先选中一个任务"),
                content=self.tr("在时间线里点一下 task 节点或它下面的任意事件"),
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return

        task_id, flow_id = identity
        # 函数级 import：`bundle` 只在这一处用到，且会拉起 zipfile —— 没必要让每次
        # 打开日志窗口都为它付启动开销。
        from ....observability.bundle import export_bug_bundle

        path = export_bug_bundle(task_id, flow_id=flow_id or None)
        if path is None:
            InfoBar.error(
                title=self.tr("导出失败"),
                content=self.tr("详情见日志"),
                position=InfoBarPosition.TOP,
                parent=self,
            )
            return
        InfoBar.success(
            title=self.tr("已导出 bug 包"),
            content=f"logs/bundles/{path.name}",
            duration=4000,
            position=InfoBarPosition.TOP,
            parent=self,
        )

    # ── 过滤 / 清屏 ──────────────────────────────────────────

    @Slot(str)
    def _on_filter_changed(self, level: str):
        """级别过滤变化"""
        self._current_filter_level = level
        self._refresh_display()

    @Slot(str)
    def _on_search_changed(self, text: str):
        """搜索变化"""
        self._current_search = text
        self._refresh_display()

    def _refresh_display(self):
        """刷新显示（重新应用过滤）——两页共用同一个谓词"""
        self.logView.clear()

        for time, level, module, message in self._log_buffer:
            if self._should_show(level, message):
                self._append_log_line(time, level, module, message)

        self.timelineView.apply_filter(self._should_show)
        self._update_line_count()

    @Slot()
    def _clear_log(self):
        """清屏"""
        self._log_buffer.clear()
        self.logView.clear()
        self.timelineView.clear_all()
        self._update_line_count()

    @Slot()
    def _open_log_dir(self):
        """打开日志目录"""
        try:
            if os.name == "nt":
                os.startfile(LOG_DIR)
            else:
                import subprocess

                subprocess.run(["xdg-open", LOG_DIR])
        except Exception:
            pass

    @Slot(int)
    def _on_scroll(self, value: int):
        """滚动事件处理"""
        sb = self.logView.verticalScrollBar()
        # 如果用户滚动到接近底部，恢复自动滚动
        self._auto_scroll = (sb.maximum() - value) < 50

    def closeEvent(self, event):
        """关窗：先摘掉日志信号，再交给基类销毁。

        顺序不能反：`WA_DeleteOnClose` 之后这个对象就没了，而 loguru 的 sink 是
        `enqueue=True`（跨线程），队列里可能还压着几条要投给已死控件的记录。
        """
        self._stop_log_capture()
        super().closeEvent(event)
