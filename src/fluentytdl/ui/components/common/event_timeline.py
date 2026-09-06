"""Observability Event 的分组时间线视图（日志查看器的第二页）。

「全部日志」那一页是**按时间**排的一条文本流，而排查一次下载真正要问的是
「这个任务这一轮都经过了什么」。并发下载时两者不是同一件事 —— 三个任务的行在文本流里
交错，读到的永远是三条链的碎片。事件里带着 flow/task/run/attempt 四级标识，
这一页就是把它们重新分组回原来的形状。

分组键是 `flow → task → run → attempt → stage`（`docs/RULES.md` §5 的五级标识），
但**占位符层级会被跳过**：解析阶段的事件 `task=-` `run=-`（那时任务还没入库），
硬按五层套下去只会让它们白挂几层空壳。

分组标签刻意沿用日志文本里的写法（`flow k72f` / `task 42` / `[download]`），
两页对照着看时才不用在脑子里做一次翻译。
"""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime
from typing import Any

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionViewItem, QTreeWidgetItem
from qfluentwidgets import (
    FluentIcon,
    TreeItemDelegate,
    TreeWidget,
    getFont,
    isDarkTheme,
    qconfig,
    setCustomStyleSheet,
    themeColor,
)

from ....observability import NO_ID, get_trace_dir, render_fields

#: 每种 kind 先看哪几个字段 —— 详情列把它们排到最前面。
#:
#: 依据是"这条事件存在的理由"：`diagnosis` 是为了 `code`，`actual` 是为了
#: `missing`，`transition` 是为了那对 from→to。字段名与各 emit 点一致，
#: 排在前面的键缺失时自动跳过（事件字段本就不是每条都齐）。
_HEADLINE_KEYS: dict[str, tuple[str, ...]] = {
    "stage": ("phase", "worker", "component", "mode"),
    "decision": ("subsystem", "code", "action"),
    "expect": ("artifacts", "sub_langs", "audio_langs"),
    "actual": ("matched", "missing", "unexpected"),
    "signal": ("code", "reason"),
    "diagnosis": ("code", "category", "severity"),
    "recovery": ("decision", "reason", "confidence"),
    "retry": ("code", "trigger", "delay_sec"),
    "transition": ("from", "to"),
    "outcome": ("outcome", "recovered", "degraded", "missing"),
    "config": ("scope", "key", "old", "new"),
    "argv": ("component", "label"),
    "identity": ("db_id",),
}

#: kind → 图标。13 种 kind 是**封闭集合**（`docs/RULES.md` §5），所以这张表能配全。
#:
#: 图标只是给扫读用的第二编码，语义仍由 kind 那个词承担 —— 靠颜色/形状单独表达
#: 级别对色弱不友好，「级别」那一列的文字始终保留。
_KIND_ICONS: dict[str, FluentIcon] = {
    "stage": FluentIcon.FLAG,  # 走到了某个阶段 —— 一个里程碑
    "decision": FluentIcon.FILTER,  # 从候选里挑一个
    "expect": FluentIcon.CHECKBOX,  # 用户勾了什么
    "actual": FluentIcon.VIEW,  # 实际观察到什么
    "signal": FluentIcon.RINGER,  # 注意到一个征兆（不是判定）
    "diagnosis": FluentIcon.CANCEL,  # 失败的最终判定
    "recovery": FluentIcon.RETURN,  # 从非零退出码里捞回来
    "retry": FluentIcon.SYNC,
    "transition": FluentIcon.RIGHT_ARROW,  # from → to
    "outcome": FluentIcon.COMPLETED,
    "config": FluentIcon.SETTING,
    "argv": FluentIcon.COMMAND_PROMPT,
    "identity": FluentIcon.LINK,  # 把 flow 钉到 task 上
}

#: 标识键 + sink 元数据：它们已经体现在树的层级和「时间」列里，详情列不再重复。
_SKIP_IN_DETAIL = frozenset(
    {"kind", "stage", "session", "flow", "task", "run", "attempt", "_ts", "_time", "_level"}
)

#: 事件字段 dict 存在这个 role 上（含回填历史），导 bug 包和重新过滤都从这里取。
_ROLE_EVENT = Qt.ItemDataRole.UserRole
#: 分组节点的完整路径键，淘汰时用来把 `_groups` 里的索引一起删掉。
_ROLE_PATH = Qt.ItemDataRole.UserRole + 1

#: `(kind, 颜色)` → 图标。树里可能有 3000 片叶子，而 kind 只有 13 种、级别只有 6 档 ——
#: 不缓存的话 `FluentIcon.icon(color=...)` 会为每片叶子重写一遍 SVG 源再重新解析。
_icon_cache: dict[tuple[str, int], QIcon] = {}


def _level_color(level: str) -> QColor | None:
    """级别 → **整行**前景色；`INFO` 返回 None 表示继承主题色。

    深浅两套值都给全（CLAUDE.md §3 禁止硬编码颜色，这里按 `isDarkTheme()` 分支）：
    浅色主题下的 `#888` 灰几乎看不见，深色主题下的 `#F44336` 红又刺眼。
    """
    dark = isDarkTheme()
    if level == "DEBUG":
        return QColor("#9E9E9E") if dark else QColor("#7A7A7A")
    if level == "SUCCESS":
        return QColor("#6FCF7F") if dark else QColor("#1B7F33")
    if level == "WARNING":
        return QColor("#FFB74D") if dark else QColor("#A35B00")
    if level == "ERROR":
        return QColor("#FF8A80") if dark else QColor("#C62828")
    if level == "CRITICAL":
        return QColor("#EA80FC") if dark else QColor("#6A1B9A")
    return None


def _badge_color(level: str) -> QColor:
    """级别 → 「级别」那一列自己的颜色。

    比 `_level_color()` 多给 INFO 一个值：整行跟着主题走没问题，但那一列要是也跟着，
    它就退化成一列灰扑扑的英文单词。给了颜色之后那一列成了色标带，扫一眼就知道
    哪几行是 DEBUG 噪音、哪一行是 WARNING。
    """
    color = _level_color(level)
    if color is not None:
        return color
    return QColor("#4FA3F7") if isDarkTheme() else QColor("#1565C0")


def _mono_font() -> QFont:
    """等宽字体，用于「时间」和「详情」两列。

    详情列是 `key=value` 串，比例字体下 `=` 参差不齐、数字也对不上列。
    **像素**而非磅值：qfluentwidgets 的 `getFont()` 用的是 `setPixelSize`，
    混着用会让等宽列比旁边的列高出一截，行高跟着不齐。
    """
    font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPixelSize(13)
    return font


def _kind_icon(kind: str, color: QColor) -> QIcon | None:
    """kind → 按级别染色的图标（缓存）。拿不到就返回 None —— 图标只是装饰。"""
    icon = _KIND_ICONS.get(kind)
    if icon is None:
        return None
    key = (kind, color.rgba())
    cached = _icon_cache.get(key)
    if cached is None:
        try:
            cached = icon.icon(color=color)
        except Exception:
            return None
        _icon_cache[key] = cached
    return cached


class _TimelineDelegate(TreeItemDelegate):
    """行级的悬停 / 选中绘制。

    上游 `TreeItemDelegate` 有三处在这棵五级深的树上会被放大成「选中状态很诡异」：

    1. 悬停与选中**共用一个刷子**（`QColor(c, c, c, 9)` —— 3.5% 不透明度），
       点中一行和鼠标扫过一行长得一模一样，看不出到底选了谁；
    2. 那条 3px 主题色指示条画在 viewport 的**绝对 x=4**，而叶子缩进五级约 100px ——
       指示条飘在离它所指那行一百像素远的地方，看着像是别的行的标记；
    3. `paint()` 先 `super().paint()` 再画背景，**背景盖在文字上**。上游靠 alpha=9
       看不出来，可一旦把选中态调到看得见，字就被糊住了。

    这里改成：先铺整行底色（悬停淡、选中用主题色且明显更重），再把 state 里的
    Selected / MouseOver 位摘掉交给上游画文字 —— 摘掉是为了不让它把自己那层背景
    再糊上来。指示条挪到 `option.rect.x()`，也就是该行内容真正开始的位置。
    """

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # 整行只在第 0 列画一次。Qt 画一行时会把每一列都过一遍 paint，逐列各画一个
        # 圆角矩形的话，选中的行看着像四颗分开的胶囊。树的单元格**不做裁剪** ——
        # 上游那条画在 x=4 的指示条能从第 3 列的 paint 里露出来，就是这个事实的证明。
        if index.column() == 0 and (selected or hovered):
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            self._fill_row(painter, option, selected, hovered)
            painter.restore()

        clean = QStyleOptionViewItem(option)
        clean.state &= ~QStyle.StateFlag.State_Selected
        clean.state &= ~QStyle.StateFlag.State_MouseOver
        super().paint(painter, clean, index)

    def _fill_row(
        self, painter: QPainter, option: QStyleOptionViewItem, selected: bool, hovered: bool
    ) -> None:
        view = self.parent()
        try:
            width = view.viewport().width()
            scrolled = view.horizontalScrollBar().value() != 0
        except Exception:  # 脱离视图单独构造 delegate 时（测试里就会）
            width, scrolled = option.rect.right(), False

        painter.setBrush(self._row_brush(selected, hovered))
        painter.drawRoundedRect(
            QRectF(4, option.rect.y() + 2, max(0.0, width - 8), option.rect.height() - 4), 5, 5
        )

        # 指示条跟着内容缩进走。横向滚动时 `option.rect.x()` 会跑到可视区左边以外，
        # 那时干脆不画（上游也是这么避的）。
        if selected and not scrolled:
            painter.setBrush(themeColor())
            painter.drawRoundedRect(
                QRectF(
                    option.rect.x() + 3,
                    option.rect.y() + 6,
                    3,
                    max(2.0, option.rect.height() - 12),
                ),
                1.5,
                1.5,
            )

    @staticmethod
    def _row_brush(selected: bool, hovered: bool) -> QColor:
        """选中和悬停必须一眼可分 —— 上游给的是同一个 3.5% 灰。"""
        if selected:
            color = QColor(themeColor())
            color.setAlpha(64 if hovered else 52)
            return color
        return QColor(255, 255, 255, 14) if isDarkTheme() else QColor(0, 0, 0, 10)


class EventTimelineView(TreeWidget):
    """按 flow/task/run/attempt/stage 分组的事件树。

    只认**扁平 dict**（`Event.as_dict()` 的形态），不 import 事件层的任何类型 ——
    实时事件来自 `log_signal_handler.event_received`，历史事件来自 trace 目录的
    JSONL，两条来源的形状本来就一样。
    """

    #: 树里最多留多少条事件。超了按**最旧的一条**淘汰（连带清掉空掉的分组节点）：
    #: 一次 Playlist 下载能产生上万条，全留着树会卡，而要看的永远是最近这些。
    MAX_EVENTS = 3000

    #: 滚动条离底部多远就认为「用户在往回翻」，此后不再自动跟到最新一条。
    #: 少了这道判断，读旧事件时每来一条新事件视图就被拽走一次，选中的行也跟着跑掉。
    AUTO_SCROLL_SLACK = 40

    #: 单元格左内边距，覆盖 qfluentwidgets 自带的 20px（见 `_ITEM_QSS`）。
    #:
    #: **不能低于 11**：第 0 列的展开箭头正好占着 item rect 最左 10px —— 实测 depth=d
    #: 的 item rect 从 `indentation*(d+1)` 起，而 `TreeWidget.viewportEvent` 把箭头的
    #: 点击区写成 `d*indentation()+20`，两者同一个起点。压到 10 以下文字就压在箭头上。
    ITEM_PADDING_LEFT = 14

    #: 第 0 列宽度的上下限，实际宽度按内容算（`_fit_col0`）。
    #:
    #: 原先钉死 264 —— 那是「最深 5 级 + 最长的 kind」才需要的宽度，而解析段的事件只有
    #: 两级（`task=-`/`run=-` 不建层级），于是「时间线」和「级别」之间空出一百多像素。
    #: 上限拦住深层树把「详情」列挤没；下限保证浅树时几列不会挤成一团。
    COL0_BOUNDS = (150, 260)

    #: 「级别」/「时间」两列的宽度。
    #:
    #: 都按最长的内容 + 两侧内边距算：`CRITICAL` 在 13px Segoe UI 下约 54px、
    #: `17:31:41` 在 13px Consolas 下约 57px（8 × 7.15），加 `ITEM_PADDING_LEFT` + 2
    #: 还各有十来像素余量。原先的 78/96 是在 20px 内边距下量的，内边距降下来就多余了。
    COL_LEVEL_WIDTH = 76
    COL_TIME_WIDTH = 84

    #: 追加在 qfluentwidgets 自带样式后面的那一条覆盖规则（`%d` 是 `ITEM_PADDING_LEFT`）。
    #: 只有一处格式化，值和注释不会各说各话。
    _ITEM_QSS = "QTreeView::item { padding-left: %dpx; padding-right: 2px; }"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(4)
        self.setHeaderLabels([self.tr("时间线"), self.tr("级别"), self.tr("时间"), self.tr("详情")])
        self.setBorderVisible(True)
        self.setBorderRadius(8)

        # qfluentwidgets 的 `QTreeView::item` 是 `padding: 4px; padding-left: 20px`，
        # 那 20px 加在**每一列**上：四列 80px 全是死区，而「级别」「时间」这种窄列被它
        # 吃掉了近三分之一。走 `setCustomStyleSheet` 而不是 `setStyleSheet` —— 后者会
        # 被主题切换时的 `styleSheetManager` 整片重写掉，前者是官方的追加口，实测切两次
        # 主题仍在。只覆盖左右，上下那 4px 留着（行高靠它）。
        item_qss = self._ITEM_QSS % self.ITEM_PADDING_LEFT
        setCustomStyleSheet(self, item_qss, item_qss)

        self._col0_width = self.COL0_BOUNDS[0]
        self._col0_locked = False
        self.setColumnWidth(0, self._col0_width)
        self.setColumnWidth(1, self.COL_LEVEL_WIDTH)
        self.setColumnWidth(2, self.COL_TIME_WIDTH)
        self.header().setStretchLastSection(True)
        # 四列一律左对齐。居中过的「级别」/「时间」会在列里浮动 —— `INFO` 短、
        # `WARNING` 长，于是每行的间距都不一样宽，扫下来就是「间隔忽大忽小」。
        # 左对齐让每列有一条固定的左边缘，眼睛顺着走。
        header_item = self.headerItem()
        for col in range(4):
            header_item.setTextAlignment(
                col, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )

        # 缩进保持默认的 20，**不要改**：qfluentwidgets 把展开箭头的点击区硬编码成
        # `level*indentation()+20` 起 10px（`TreeWidget.viewportEvent`），而箭头本身
        # 画在 `moveLeft(15)` 处（`TreeViewBase.drawBranches`）—— 只有 indentation==20
        # 时两者才对得上。曾设成 14：箭头画在 29..43、点击区却是 34..44，点箭头有一半
        # 概率只选中不展开，看着就像「选中状态很诡异」。
        self.setSelectionBehavior(TreeWidget.SelectionBehavior.SelectRows)
        # 悬停/选中要整行亮，不是只亮鼠标底下那一格。
        self.setAllColumnsShowFocus(True)
        self.setItemDelegate(_TimelineDelegate(self))
        # 所有行都是 13px，行高本就一致；显式声明能让 3000 行时省掉逐行测高。
        self.setUniformRowHeights(True)

        self._mono = _mono_font()
        #: 分组层级 → (字体, 取色器)。分组标签原先走 `item.font(0)` 再 setBold ——
        #: 那返回的是**默认构造的 QFont**（系统默认字族+磅值），装回去就把叶子那边的
        #: `getFont(13)` 覆盖掉了，于是分组行和事件行是两套字体，整棵树看着发毛。
        self._tier_font = getFont(13, QFont.Weight.DemiBold)
        # 量第 0 列宽度用。字体不随主题变，缓存两个 QFontMetrics 即可；叶子在第 0 列
        # 没设 FontRole，上游 `initStyleOption` 会兜底成 `getFont(13)`，所以量的是它。
        self._leaf_metrics = QFontMetrics(getFont(13))
        self._tier_metrics = QFontMetrics(self._tier_font)
        self._groups: dict[tuple[str, ...], QTreeWidgetItem] = {}
        self._leaves: deque[QTreeWidgetItem] = deque()
        self._predicate = None  # 当前过滤谓词，新事件进来时要照它判一次
        self._auto_scroll = True

        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.header().sectionResized.connect(self._on_section_resized)

        try:
            # 两个信号都要接：`themeChanged` 管深浅（`_level_color` 那两套值），
            # `themeColorChanged` 管强调色（`flow` 分组和选中条用的 `themeColor()`）。
            qconfig.themeChanged.connect(self._recolor)
            qconfig.themeColorChanged.connect(self._recolor)
        except Exception:
            pass  # 主题信号接不上只是颜色不跟着切，不该让整个视图建不起来

    # ── 第 0 列自适应 ────────────────────────────────────────

    def _fit_col0(self, depth: int, text: str, *, metrics: QFontMetrics, icon: bool) -> None:
        """把第 0 列撑到刚好放得下这一行，**只增不减**，并夹在 `COL0_BOUNDS` 内。

        不用 `QHeaderView.ResizeToContents`：那会在每次插入时向 delegate 逐行问
        sizeHint，而这棵树可以有 3000 行。宽度这里能直接算 —— 缩进是
        `indentation() * (depth + 1)`（`rootIsDecorated()` 多占一级，实测如此）。

        只增不减是刻意的：宽度跟着最新一条事件来回抽，读起来比留点空白更难受。
        """
        if self._col0_locked:
            return
        need = (
            self.indentation() * (depth + 1)
            + self.ITEM_PADDING_LEFT
            + (self.iconSize().width() + 4 if icon else 0)
            + metrics.horizontalAdvance(text)
            + 8  # 右侧留一点，别让文字贴着下一列的分隔线
        )
        low, high = self.COL0_BOUNDS
        width = max(low, min(high, need))
        if width > self._col0_width:
            self._col0_width = width
            self.setColumnWidth(0, width)

    def _on_section_resized(self, index: int, _old: int, new: int) -> None:
        """用户手动拖过第 0 列之后就不再自动撑宽 —— 那个宽度是他自己定的。

        自己调 `setColumnWidth()` 也会走这个信号，靠「新宽度是不是我们刚算出来那个」
        区分：`_fit_col0` 先赋 `_col0_width` 再调 `setColumnWidth`，所以这里读到的
        一定是最新值，相等就说明这次是我们自己干的。
        """
        if index == 0 and new != self._col0_width:
            self._col0_locked = True

    def _on_scrolled(self, value: int) -> None:
        """用户往回翻时停掉自动跟随，滚回底部再恢复。

        只有真正的滚动会走到这里 —— 往树里挂 item 只抬高滚动条的 `maximum`，不发
        `valueChanged`，所以自动跟随不会被自己插进去的事件误关掉。
        """
        sb = self.verticalScrollBar()
        self._auto_scroll = (sb.maximum() - value) < self.AUTO_SCROLL_SLACK

    # ── 写入 ────────────────────────────────────────────────

    def add_event(self, event: dict) -> None:
        """挂一条事件。整段吞异常：日志视图绝不能因为一条畸形事件炸掉整个对话框。"""
        try:
            self._add_event(event)
        except Exception:
            pass

    def _add_event(self, event: dict) -> None:
        parent: QTreeWidgetItem | None = None
        key: tuple[str, ...] = ()
        depth = -1
        for token, label in self._group_path(event):
            key = (*key, token)
            depth += 1
            item = self._groups.get(key)
            if item is None:
                item = self._make_group(label, key, parent, event, depth)
            parent = item

        kind = str(event.get("kind") or "?")
        leaf = QTreeWidgetItem(
            [
                kind,
                str(event.get("_level") or ""),
                str(event.get("_time") or self._time_of(event)),
                self._detail(event),
            ]
        )
        leaf.setData(0, _ROLE_EVENT, event)
        leaf.setToolTip(3, self._tooltip(event))
        # 时间与详情走等宽：详情是 `key=value` 串，比例字体下 `=` 参差不齐。
        # 对齐方式一律走默认的左对齐（见 `__init__` 里那段）。
        leaf.setFont(2, self._mono)
        leaf.setFont(3, self._mono)
        self._paint(leaf)
        self._fit_col0(depth + 1, kind, metrics=self._leaf_metrics, icon=True)
        if parent is None:
            self.addTopLevelItem(leaf)
        else:
            parent.addChild(leaf)

        if self._predicate is not None and not self._matches(event):
            leaf.setHidden(True)
            self._hide_empty_ancestors(leaf.parent())
        else:
            self._reveal(leaf)

        self._leaves.append(leaf)
        self._evict_overflow()
        # 被过滤挡掉的叶子不值得把视图拽过去 —— 那会滚到一片空白上。
        if self._auto_scroll and not leaf.isHidden():
            self.scrollToItem(leaf)

    def _make_group(
        self,
        label: str,
        key: tuple[str, ...],
        parent: QTreeWidgetItem | None,
        event: dict,
        depth: int,
    ) -> QTreeWidgetItem:
        item = QTreeWidgetItem([label, "", "", ""])
        item.setFont(0, self._tier_font)
        # 标识随分组一起存：导 bug 包时用户点的往往是 `task 42` 这个分组节点本身，
        # 而不是它底下某条事件。
        item.setData(
            0, _ROLE_EVENT, {"flow": event.get("flow"), "task": event.get("task"), "_group": True}
        )
        item.setData(0, _ROLE_PATH, key)
        self._tint_group(item)
        self._fit_col0(depth, label, metrics=self._tier_metrics, icon=False)
        self._groups[key] = item
        if parent is None:
            self.addTopLevelItem(item)
        else:
            parent.addChild(item)
        item.setExpanded(True)
        return item

    def _tint_group(self, item: QTreeWidgetItem) -> None:
        """按层级给分组标签上色。

        五级同一个粗体的话，`flow` / `task` / `run` / `attempt` / `[stage]` 只能靠缩进
        区分，而缩进在深处是一堆等宽的空白。这里让最外层的 `flow` 拿主题色（它是这一
        整条链的锚点），`run` / `attempt` 压成次要色（它们多半只有一份、是结构而不是
        信息），`task` 和 `[stage]` 保持正文色 —— 那两层才是真正要读的。
        """
        path = item.data(0, _ROLE_PATH)
        tier = path[-1].split(":", 1)[0] if isinstance(path, tuple) and path else ""
        if tier == "flow":
            item.setForeground(0, QBrush(themeColor()))
        elif tier in ("run", "attempt"):
            item.setForeground(
                0, QBrush(QColor(255, 255, 255, 150) if isDarkTheme() else QColor(0, 0, 0, 140))
            )
        else:
            # 清 role 而不是塞默认 QBrush —— 默认构造的是黑色，深色主题下就是黑字。
            item.setData(0, Qt.ItemDataRole.ForegroundRole, None)

    def _group_path(self, event: dict) -> list[tuple[str, str]]:
        """事件 → 分组路径 `[(去重键, 显示标签), ...]`。

        `-`（`NO_ID`）一律不建层级。解析阶段的事件没有 task / run，硬套五层会让
        「flow → - → - → attempt 0」这种空壳占满半个屏幕。
        """
        flow = str(event.get("flow") or NO_ID)
        task = str(event.get("task") or NO_ID)
        run = str(event.get("run") or NO_ID)
        stage = str(event.get("stage") or NO_ID)

        path: list[tuple[str, str]] = [
            (f"flow:{flow}", f"flow {flow}" if flow != NO_ID else self.tr("（无 flow）"))
        ]
        if task != NO_ID:
            path.append((f"task:{task}", f"task {task}"))
        if run != NO_ID:
            path.append((f"run:{run}", f"run {run}"))
            # attempt 只在 run 之下才有意义（`attempt` 属于 run，见硬规则 4 的层次）。
            # 恒定建这一层而不是"只在 >0 时建"：不然重试一发生，attempt 0 的事件
            # 挂在 run 上、attempt 1 的事件缩进一级，同一轮的东西看着像两回事。
            try:
                attempt = int(event.get("attempt") or 0)
            except (TypeError, ValueError):
                attempt = 0
            path.append((f"attempt:{attempt}", f"attempt {attempt}"))
        if stage != NO_ID:
            path.append((f"stage:{stage}", f"[{stage}]"))
        return path

    # ── 渲染 ────────────────────────────────────────────────

    def _detail(self, event: dict) -> str:
        """详情列：先排该 kind 的关键字段，再跟其余字段，格式与日志文本一致。"""
        keys = _HEADLINE_KEYS.get(str(event.get("kind") or ""), ())
        ordered: dict[str, Any] = {k: event[k] for k in keys if k in event}
        for k, v in event.items():
            if k in _SKIP_IN_DETAIL or k in ordered:
                continue
            ordered[k] = v
        return render_fields(ordered)

    def _tooltip(self, event: dict) -> str:
        """悬浮时给全量字段，一行一个 —— 详情列再宽也放不下 `argv`。"""
        return "\n".join(f"{k}={v}" for k, v in event.items() if k not in ("_ts",))

    def _paint(self, leaf: QTreeWidgetItem) -> None:
        """给一片叶子上色并挂图标。

        必须幂等 —— 主题切换时 `_recolor()` 会对全部叶子重跑一遍。
        """
        event = leaf.data(0, _ROLE_EVENT) or {}
        level = str(event.get("_level") or "")
        color = _level_color(level)
        badge = _badge_color(level)
        for col in range(self.columnCount()):
            if col == 1:
                # 「级别」这一列永远自带颜色（连 INFO 都有），它是整棵树的色标带：
                # 眼睛顺着这一列往下扫就能找到那几条 WARNING，不用逐行读文字。
                leaf.setForeground(col, QBrush(badge))
            elif color is None:
                # INFO 不指定颜色，跟着主题走。清 role 而不是塞一个默认 QBrush ——
                # 默认构造出来的是黑色 NoBrush，在深色主题下就是一行黑字。
                leaf.setData(col, Qt.ItemDataRole.ForegroundRole, None)
            else:
                leaf.setForeground(col, QBrush(color))

        icon = _kind_icon(str(event.get("kind") or ""), badge)
        if icon is not None:
            leaf.setIcon(0, icon)

    def _recolor(self) -> None:
        """主题切换后重刷颜色 —— 深色下的浅红在浅色主题里几乎看不见。

        图标一起重生成：`_kind_icon()` 是把颜色**改写进 SVG 源**再解析的，主题换了
        原来那批图标还留着旧主题的颜色。缓存不用清 —— 键里带着 rgba，新主题算出来
        的是另一个键。
        """
        try:
            for leaf in self._leaves:
                self._paint(leaf)
            for group in self._groups.values():
                self._tint_group(group)
        except Exception:
            pass

    @staticmethod
    def _time_of(event: dict) -> str:
        """回填历史时 `_time` 不存在，从 `_ts` 还原（JSONL 里的唯一时间来源）。"""
        try:
            return datetime.fromtimestamp(float(event["_ts"])).strftime("%H:%M:%S")
        except Exception:
            return "--:--:--"

    # ── 过滤 ────────────────────────────────────────────────

    def apply_filter(self, predicate) -> None:
        """按 `predicate(level, text) -> bool` 显示/隐藏叶子节点。

        分组节点只要还有一个可见后代就留着 —— 否则筛 ERROR 时会剩一堆空的
        `flow …` 壳子，而"哪个任务出的错"恰恰要靠这些壳子看出来。
        """
        self._predicate = predicate
        try:
            for i in range(self.topLevelItemCount()):
                self._filter_item(self.topLevelItem(i))
        except Exception:
            pass

    def _filter_item(self, item: QTreeWidgetItem) -> bool:
        if item.childCount() == 0:
            event = item.data(0, _ROLE_EVENT) or {}
            if event.get("_group"):
                item.setHidden(True)  # 被淘汰掏空的分组节点
                return False
            visible = self._matches(event)
            item.setHidden(not visible)
            return visible
        visible = False
        for i in range(item.childCount()):
            if self._filter_item(item.child(i)):
                visible = True
        item.setHidden(not visible)
        return visible

    def _matches(self, event: dict) -> bool:
        if self._predicate is None:
            return True
        try:
            return bool(
                self._predicate(str(event.get("_level") or "INFO"), self._searchable(event))
            )
        except Exception:
            return True

    def _searchable(self, event: dict) -> str:
        """搜索面：kind + stage + 全部字段。搜 `429` 或 `subtitle` 都该命中。"""
        return f"{event.get('kind', '')} {event.get('stage', '')} {self._detail(event)}"

    def _reveal(self, leaf: QTreeWidgetItem) -> None:
        """把新叶子的祖先链取消隐藏 —— 过滤生效期间来的新事件也要能露出来。"""
        node = leaf.parent()
        while node is not None:
            node.setHidden(False)
            node = node.parent()

    def _hide_empty_ancestors(self, node: QTreeWidgetItem | None) -> None:
        """新叶子被过滤挡掉时，把因它而新建、却一个可见后代都没有的分组藏起来。

        少了这一步，筛选生效期间来的事件会留下一串空壳：`flow k72f → task 42 →
        run A → attempt 0` 四层俱在，点开却什么都没有 —— 看着像"这个任务的事件被
        吞掉了"。`apply_filter()` 的整树重扫本来会收拾它们，但它只在用户动筛选条件
        时才跑。
        """
        while node is not None:
            if any(not node.child(i).isHidden() for i in range(node.childCount())):
                return  # 还有可见后代，再往上都不用动
            node.setHidden(True)
            node = node.parent()

    # ── 淘汰 / 清空 ──────────────────────────────────────────

    def _evict_overflow(self) -> None:
        while len(self._leaves) > self.MAX_EVENTS:
            leaf = self._leaves.popleft()
            parent = leaf.parent()
            if parent is None:
                idx = self.indexOfTopLevelItem(leaf)
                if idx >= 0:
                    self.takeTopLevelItem(idx)
                continue
            parent.removeChild(leaf)
            self._prune(parent)

    def _prune(self, item: QTreeWidgetItem | None) -> None:
        """自下而上删掉空掉的分组节点，同时清掉 `_groups` 的索引。

        不清索引的话，同一个 `flow`/`task` 之后再来事件会命中一个**已经不在树里**的
        节点，新事件就此消失（Qt 侧那个 item 已被回收）。
        """
        while item is not None and item.childCount() == 0:
            parent = item.parent()
            path = item.data(0, _ROLE_PATH)
            if isinstance(path, tuple):
                self._groups.pop(path, None)
            if parent is None:
                idx = self.indexOfTopLevelItem(item)
                if idx >= 0:
                    self.takeTopLevelItem(idx)
            else:
                parent.removeChild(item)
            item = parent

    def clear_all(self) -> None:
        self.clear()
        self._groups.clear()
        self._leaves.clear()
        # 清屏后第 0 列回到下限：`_fit_col0` 只增不减，不收一下的话「清屏」之后那一列
        # 还留着上一批深层事件撑出来的宽度，空树上就是一片空白。
        if not self._col0_locked:
            self._col0_width = self.COL0_BOUNDS[0]
            self.setColumnWidth(0, self._col0_width)

    def event_count(self) -> int:
        """树里当前的事件条数（不含分组节点）。"""
        return len(self._leaves)

    # ── 选中 / 回填 ──────────────────────────────────────────

    def selected_identity(self) -> tuple[str, str] | None:
        """当前选中项所属的 `(task_id, flow_id)`；选中的是解析段则返回 None。

        沿祖先链往上找：用户点在 `[download]` 分组或某条事件上时，task 都在上面。
        """
        item = self.currentItem()
        while item is not None:
            event = item.data(0, _ROLE_EVENT) or {}
            task = str(event.get("task") or NO_ID)
            if task not in ("", NO_ID, "None"):
                return task, str(event.get("flow") or "")
            item = item.parent()
        return None

    def load_recent(self, max_files: int = 3, max_lines: int = 600) -> int:
        """回填 trace 目录里最近的 JSONL 事件，返回条数。

        没有这一步，时间线页在打开时是空的 —— 而用户打开日志查看器的典型时机
        恰恰是"刚刚失败了"，那时该看的事件全都已经发生完了。

        按 `_ts` 全局排序后再挂：多个 flow 的文件各自有序，拼起来不排就会出现
        「flow A 的收尾排在 flow B 的开头之前」这种读不通的顺序。
        """
        try:
            trace_dir = get_trace_dir()
            files = sorted(
                (p for p in trace_dir.glob("*.jsonl") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )[-max_files:]
            events: list[dict] = []
            for path in files:
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        for line in deque(f, maxlen=max_lines):
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                event = json.loads(line)
                            except Exception:
                                continue
                            if isinstance(event, dict):
                                events.append(event)
                except Exception:
                    continue
            events.sort(key=lambda e: float(e.get("_ts") or 0.0))
            for event in events:
                event.setdefault("_time", self._time_of(event))
                event.setdefault("_level", "INFO")
                self.add_event(event)
            return len(events)
        except Exception:
            return 0
